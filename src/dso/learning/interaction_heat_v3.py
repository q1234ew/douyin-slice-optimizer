from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import zlib
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dso.config import project_root
from dso.versions import (
    INTERACTION_HEAT_ARTIFACT_VERSION,
    INTERACTION_HEAT_LABEL_VERSION,
    INTERACTION_HEAT_SPLIT_VERSION,
)


DEFAULT_INTERACTION_HEAT_ARTIFACT_ID = "dso-interaction-heat-v3-20260720-r3"
DEFAULT_SPLIT_SEED = "dso-interaction-heat-v3-20260720"
METRIC_TARGETS = {
    "likes": "like_heat",
    "comments": "discussion_heat",
    "favorites": "favorite_heat",
    "shares": "share_heat",
}
SPLIT_RANK = {"train": 0, "validation": 1, "test": 2}
_TITLE_CLEANUP = re.compile(r"https?://\S+|[@#《》【】\[\]（）()，,。.!！?？:：;；\"'“”‘’、\s]+")
_DATASET_DATE = re.compile(r"(?:^|_)(20\d{6})(?:_|$)")
_MEDIA_SUFFIXES = frozenset({".mp4", ".mov", ".m4v"})
_ARTIFACT_PAYLOAD_FILES = frozenset(
    {"labels.jsonl", "splits.jsonl", "normalizers.json", "report.json"}
)
_CANONICAL_SOURCE_METADATA = frozenset(
    {
        "source_kind",
        "input_snapshot_sha256",
        "media_index_sha256",
        "execution_environment",
        "git_parent_commit",
        "git_branch",
        "code_sha256",
        "pyproject_sha256",
    }
)


def build_interaction_heat_dataset(
    rows: Iterable[dict[str, Any]],
    *,
    media_sha_by_item: dict[str, list[str]] | None = None,
    min_group_samples: int = 20,
    split_seed: str = DEFAULT_SPLIT_SEED,
) -> dict[str, Any]:
    minimum = max(2, int(min_group_samples or 20))
    prepared = [_prepare_row(dict(row)) for row in rows]
    if not prepared:
        raise ValueError("interaction heat V3 requires at least one sample")
    ids = [row["sample_id"] for row in prepared]
    if len(ids) != len(set(ids)):
        raise ValueError("interaction heat V3 sample IDs must be unique")
    if not all(row["account_id"] for row in prepared):
        raise ValueError("interaction heat V3 requires account_id for every sample")

    source_groups, group_evidence, media_coverage = _build_source_groups(
        prepared,
        media_sha_by_item or {},
    )
    for row in prepared:
        row["source_group_id"] = source_groups[row["sample_id"]]
        row["source_group_evidence"] = group_evidence[row["source_group_id"]]

    time_splits, time_meta = _account_time_splits(prepared)
    account_splits, account_meta = _account_holdout_splits(prepared, split_seed=split_seed)
    split_rows = []
    for row in sorted(prepared, key=lambda item: item["sample_id"]):
        sample_id = row["sample_id"]
        split_rows.append(
            {
                "sample_id": sample_id,
                "account_id": row["account_id"],
                "source_group_id": row["source_group_id"],
                "source_group_evidence": row["source_group_evidence"],
                "published_at": row["published_at"],
                "account_time_split": time_splits[sample_id],
                "account_holdout_split": account_splits[sample_id],
            }
        )

    protocol_fields = {
        "account_time": "account_time_split",
        "account_holdout": "account_holdout_split",
    }
    normalizers: dict[str, Any] = {}
    protocol_labels: dict[str, dict[str, dict[str, Any]]] = {}
    for protocol, split_field in protocol_fields.items():
        split_by_id = {
            item["sample_id"]: item[split_field]
            for item in split_rows
        }
        train_rows = [row for row in prepared if split_by_id[row["sample_id"]] == "train"]
        fitted = _fit_normalizers(train_rows, min_group_samples=minimum)
        missing_train_metrics = [
            metric for metric in METRIC_TARGETS if "global" not in fitted.get(metric, {})
        ]
        if missing_train_metrics:
            raise ValueError(
                f"{protocol} training partition has no normalization values for: "
                + ", ".join(missing_train_metrics)
            )
        normalizers[protocol] = {
            "fit_split": "train",
            "train_sample_count": len(train_rows),
            "min_group_samples": minimum,
            "transform": "log1p then train-fitted empirical midpoint percentile",
            "fallback_order": [
                "account_age_duration",
                "account_age",
                "account",
                "global_age_duration",
                "global_age",
                "global",
            ],
            "metrics": fitted,
        }
        protocol_labels[protocol] = {
            row["sample_id"]: _score_row(row, fitted, min_group_samples=minimum)
            for row in prepared
        }

    labels = []
    for row in sorted(prepared, key=lambda item: item["sample_id"]):
        sample_id = row["sample_id"]
        primary = protocol_labels["account_time"][sample_id]
        labels.append(
            {
                "sample_id": sample_id,
                "account_id": row["account_id"],
                "label_version": INTERACTION_HEAT_LABEL_VERSION,
                "observation_date": row["observed_at"][:10] if row["observed_at"] else "",
                "publication_age_bucket": row["age_bucket"],
                "duration_bucket": row["duration_bucket"],
                "observed_counts": dict(row["counts"]),
                "metric_missing": dict(row["metric_missing"]),
                "targets": primary["targets"],
                "normalization_scopes": primary["normalization_scopes"],
                "normalization_sample_counts": primary["normalization_sample_counts"],
                "confidence": primary["confidence"],
                "protocol_targets": {
                    protocol: protocol_labels[protocol][sample_id]
                    for protocol in protocol_fields
                },
            }
        )

    leakage_audit = {
        "account_time": _leakage_audit(split_rows, "account_time_split"),
        "account_holdout": _leakage_audit(split_rows, "account_holdout_split"),
        "media_sha_available_sample_count": media_coverage,
        "media_sha_unavailable_sample_count": len(prepared) - media_coverage,
        "media_sha_coverage_rate": round(media_coverage / len(prepared), 6),
        "title_near_duplicate_policy": "stable-title equality plus six-band character-trigram MinHash candidate search and Jaccard>=0.90",
        "program_group_policy": "normalized program_name + song_title when both are known",
    }
    input_records = [_input_fingerprint_row(row) for row in sorted(prepared, key=lambda item: item["sample_id"])]
    return {
        "contract_version": INTERACTION_HEAT_ARTIFACT_VERSION,
        "label_version": INTERACTION_HEAT_LABEL_VERSION,
        "split_version": INTERACTION_HEAT_SPLIT_VERSION,
        "sample_count": len(prepared),
        "account_count": len({row["account_id"] for row in prepared}),
        "input_sha256": _records_sha256(input_records),
        "splits": split_rows,
        "labels": labels,
        "normalizers": normalizers,
        "split_summary": {
            "account_time": time_meta,
            "account_holdout": account_meta,
        },
        "leakage_audit": leakage_audit,
        "metric_availability": {
            metric: {
                "available_count": sum(not row["metric_missing"][metric] for row in prepared),
                "missing_count": sum(row["metric_missing"][metric] for row in prepared),
            }
            for metric in METRIC_TARGETS
        },
        "label_distribution": _label_distribution(labels),
    }


def freeze_interaction_heat_artifact(
    *,
    artifact_id: str,
    rows: Iterable[dict[str, Any]],
    output_root: Path,
    media_sha_by_item: dict[str, list[str]] | None = None,
    created_at: str | None = None,
    min_group_samples: int = 20,
    split_seed: str = DEFAULT_SPLIT_SEED,
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _artifact_id(artifact_id)
    root = Path(output_root).resolve()
    target = root / key
    if target.exists():
        raise FileExistsError(f"frozen interaction heat artifact already exists: {target}")
    dataset = build_interaction_heat_dataset(
        rows,
        media_sha_by_item=media_sha_by_item,
        min_group_samples=min_group_samples,
        split_seed=split_seed,
    )
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{key}-", dir=str(root)))
    try:
        labels_path = staging / "labels.jsonl"
        splits_path = staging / "splits.jsonl"
        normalizers_path = staging / "normalizers.json"
        report_path = staging / "report.json"
        _write_jsonl(labels_path, dataset["labels"])
        _write_jsonl(splits_path, dataset["splits"])
        _write_json(normalizers_path, dataset["normalizers"])
        report = {
            "status": "frozen",
            "sample_count": dataset["sample_count"],
            "account_count": dataset["account_count"],
            "metric_availability": dataset["metric_availability"],
            "label_distribution": dataset["label_distribution"],
            "split_summary": dataset["split_summary"],
            "leakage_audit": dataset["leakage_audit"],
            "claim_limit": (
                "Labels represent observed interaction heat without exposure denominators; "
                "they are not play traffic, watch quality, share rate, or follow conversion."
            ),
        }
        _write_json(report_path, report)
        file_digests = {
            "labels.jsonl": _file_sha256(labels_path),
            "splits.jsonl": _file_sha256(splits_path),
            "normalizers.json": _file_sha256(normalizers_path),
            "report.json": _file_sha256(report_path),
        }
        manifest = {
            "contract_version": INTERACTION_HEAT_ARTIFACT_VERSION,
            "artifact_id": key,
            "lifecycle": "frozen",
            "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "goal_alignment": ["G1"],
            "label_version": INTERACTION_HEAT_LABEL_VERSION,
            "split_version": INTERACTION_HEAT_SPLIT_VERSION,
            "sample_count": dataset["sample_count"],
            "account_count": dataset["account_count"],
            "input_sha256": dataset["input_sha256"],
            "source": source_metadata or {},
            "label_schema": {
                "targets": ["like_heat", "share_heat", "favorite_heat", "discussion_heat", "broad_heat"],
                "range": [0.0, 1.0],
                "raw_transform": "log1p(non-negative observed count)",
                "normalization": "train-fitted empirical midpoint percentile with hierarchical fallback",
                "broad_heat": "unweighted mean of available component heat targets; unavailable when fewer than three components exist",
                "missing_semantics": "null heat plus metric_missing=true; observed zero remains a valid zero count",
            },
            "split_policy": {
                "primary": "account-local chronological 70/10/20 with source-group promotion to the latest partition",
                "secondary": (
                    "whole-account 70/10/20 holdout; earlier-fold rows linked to a later "
                    "source group are excluded from evaluation/training"
                ),
                "source_group": ["platform_item_id", "stable/near title", "program+song", "available media SHA-256"],
                "split_seed": split_seed,
                "normalizer_fit_partition": "train only, independently for each protocol",
            },
            "feature_leakage_policy": {
                "forbidden_model_features": [
                    "likes",
                    "comments",
                    "favorites",
                    "shares",
                    "reward_proxy",
                    "normalized_reward",
                    "performance_label",
                    "all direct derivatives of interaction outcomes",
                ],
                "target_only": True,
            },
            "network_request_count": 0,
            "effective_model_cost_cny": "0.000000",
            "production_impact": {
                "database_rows_updated": 0,
                "visible_engagement_v2_overwritten": False,
                "production_weight_changed": False,
                "manual_gold_changed": False,
                "automatic_export": False,
                "automatic_publish": False,
            },
            "runtime": {
                "python": sys.version.split()[0],
                "implementation": sys.implementation.name,
            },
            "files": file_digests,
            "immutability_policy": "Never edit this directory in place; create a new artifact_id for any data, split, label, or code change.",
        }
        manifest["manifest_sha256"] = _manifest_sha256(manifest)
        _write_json(staging / "manifest.json", manifest)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "status": "frozen",
        "artifact_id": key,
        "artifact_dir": str(target),
        "manifest_sha256": manifest["manifest_sha256"],
        "labels_sha256": file_digests["labels.jsonl"],
        "splits_sha256": file_digests["splits.jsonl"],
        "sample_count": dataset["sample_count"],
        "account_count": dataset["account_count"],
        "network_request_count": 0,
        "effective_model_cost_cny": "0.000000",
    }


def load_interaction_heat_rows(db_path: Path) -> list[dict[str, Any]]:
    path = Path(db_path).resolve()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, account_id, dataset_id, program_key, program_name, song_title,
                   platform, platform_item_id, sample_key, title, published_at,
                   collected_at, duration_seconds, likes, comments, favorites, shares,
                   reward_proxy, raw_json
            FROM historical_capture_samples
            WHERE COALESCE(platform_item_id, '') != ''
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()
    prepared = []
    for sqlite_row in rows:
        row = dict(sqlite_row)
        try:
            raw = json.loads(str(row.pop("raw_json") or "{}"))
        except json.JSONDecodeError:
            raw = {}
        clean = raw.get("clean") if isinstance(raw, dict) else {}
        if not isinstance(clean, dict):
            clean = {}
        row["observed_at"] = clean.get("observed_at") or row.get("collected_at") or ""
        sources = clean.get("metric_sources")
        row["metric_sources"] = sources if isinstance(sources, dict) else {}
        if not any(
            bool(row["metric_sources"].get(metric)) and _has_metric_value(row.get(metric))
            for metric in METRIC_TARGETS
        ):
            continue
        prepared.append(row)
    return prepared


def freeze_interaction_heat_from_db(
    *,
    artifact_id: str,
    db_path: Path | None = None,
    output_root: Path | None = None,
    repo_root: Path | None = None,
    created_at: str | None = None,
    min_group_samples: int = 20,
    split_seed: str = DEFAULT_SPLIT_SEED,
) -> dict[str, Any]:
    repo = Path(repo_root or project_root()).resolve()
    database = Path(db_path or repo / "data" / "db" / "dso.sqlite3").resolve()
    rows = load_interaction_heat_rows(database)
    media_sha_by_item, media_meta = _local_media_sha_index(repo)
    source_metadata = {
        "table": "historical_capture_samples",
        "row_filter": "platform_item_id present and at least one interaction metric has explicit provenance and a value; valid zero outcomes are retained",
        "database_path": str(database.relative_to(repo)) if database.is_relative_to(repo) else database.name,
        "database_sha256": _file_sha256(database),
        "media_index": media_meta,
        "git_parent_commit": _git_value(repo, "rev-parse", "HEAD"),
        "git_branch": _git_value(repo, "branch", "--show-current"),
        "code_sha256": _file_sha256(Path(__file__)),
        "pyproject_sha256": _file_sha256(repo / "pyproject.toml"),
    }
    return freeze_interaction_heat_artifact(
        artifact_id=artifact_id,
        rows=rows,
        output_root=Path(output_root or repo / "benchmarks"),
        media_sha_by_item=media_sha_by_item,
        created_at=created_at,
        min_group_samples=min_group_samples,
        split_seed=split_seed,
        source_metadata=source_metadata,
    )


def export_interaction_heat_input_snapshot(
    *,
    db_path: Path,
    input_path: Path,
    media_index_path: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    input_requested = Path(os.path.abspath(Path(input_path).expanduser()))
    media_requested = Path(os.path.abspath(Path(media_index_path).expanduser()))
    input_requested.parent.mkdir(parents=True, exist_ok=True)
    media_requested.parent.mkdir(parents=True, exist_ok=True)
    input_file = input_requested.parent.resolve() / input_requested.name
    media_file = media_requested.parent.resolve() / media_requested.name
    if input_file == media_file:
        raise ValueError("input snapshot and media index must use different paths")
    if os.path.lexists(input_file) or os.path.lexists(media_file):
        raise FileExistsError("interaction heat input snapshot paths must not already exist")
    repo = Path(repo_root or project_root()).resolve()
    rows = load_interaction_heat_rows(Path(db_path))
    input_rows = [
        {
            key: row.get(key)
            for key in (
                "id",
                "account_id",
                "dataset_id",
                "program_key",
                "program_name",
                "song_title",
                "platform",
                "platform_item_id",
                "sample_key",
                "title",
                "published_at",
                "observed_at",
                "duration_seconds",
                "likes",
                "comments",
                "favorites",
                "shares",
                "metric_sources",
            )
        }
        for row in sorted(rows, key=lambda item: str(item.get("id") or ""))
    ]
    media_sha_by_item, media_meta = _local_media_sha_index(repo)
    input_descriptor, input_staging_name = tempfile.mkstemp(
        prefix=f".{input_file.name}-",
        dir=str(input_file.parent),
    )
    media_descriptor, media_staging_name = tempfile.mkstemp(
        prefix=f".{media_file.name}-",
        dir=str(media_file.parent),
    )
    os.close(input_descriptor)
    os.close(media_descriptor)
    input_staging = Path(input_staging_name)
    media_staging = Path(media_staging_name)
    try:
        _write_jsonl(input_staging, input_rows)
        _write_json(media_staging, media_sha_by_item)
        input_sha256 = _file_sha256(input_staging)
        media_sha256 = _file_sha256(media_staging)
        input_staging_stat = input_staging.stat()
        os.link(input_staging, input_file)
        try:
            os.link(media_staging, media_file)
        except Exception:
            try:
                published_stat = input_file.lstat()
            except FileNotFoundError:
                published_stat = None
            if (
                published_stat is not None
                and stat.S_ISREG(published_stat.st_mode)
                and published_stat.st_dev == input_staging_stat.st_dev
                and published_stat.st_ino == input_staging_stat.st_ino
            ):
                input_file.unlink()
            raise
    finally:
        input_staging.unlink(missing_ok=True)
        media_staging.unlink(missing_ok=True)
    return {
        "status": "exported",
        "sample_count": len(input_rows),
        "input_path": str(input_file),
        "input_sha256": input_sha256,
        "media_index_path": str(media_file),
        "media_index_sha256": media_sha256,
        "media_index": media_meta,
        "network_request_count": 0,
        "effective_model_cost_cny": "0.000000",
    }


def freeze_interaction_heat_from_snapshot(
    *,
    artifact_id: str,
    input_path: Path,
    media_index_path: Path,
    output_root: Path,
    repo_root: Path | None = None,
    created_at: str | None = None,
    min_group_samples: int = 20,
    split_seed: str = DEFAULT_SPLIT_SEED,
    execution_environment: str = "isolated_snapshot",
    source_metadata_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_file = Path(input_path).resolve()
    media_file = Path(media_index_path).resolve()
    rows = [
        json.loads(line)
        for line in input_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    media_payload = json.loads(media_file.read_text(encoding="utf-8"))
    if not isinstance(media_payload, dict):
        raise ValueError("media SHA index must be a JSON object")
    media_sha_by_item = {
        str(item_id): [str(value) for value in values]
        for item_id, values in media_payload.items()
        if isinstance(values, list)
    }
    repo = Path(repo_root or project_root()).resolve()
    source_metadata = {
        "source_kind": "minimal_interaction_heat_input_snapshot",
        "input_snapshot_sha256": _file_sha256(input_file),
        "media_index_sha256": _file_sha256(media_file),
        "execution_environment": execution_environment,
        "git_parent_commit": _git_value(repo, "rev-parse", "HEAD"),
        "git_branch": _git_value(repo, "branch", "--show-current"),
        "code_sha256": _file_sha256(Path(__file__)),
        "pyproject_sha256": _file_sha256(repo / "pyproject.toml"),
    }
    if source_metadata_overrides:
        protected = sorted(_CANONICAL_SOURCE_METADATA.intersection(source_metadata_overrides))
        if protected:
            raise ValueError(
                "canonical source metadata cannot be overridden: " + ", ".join(protected)
            )
        source_metadata.update(source_metadata_overrides)
    return freeze_interaction_heat_artifact(
        artifact_id=artifact_id,
        rows=rows,
        output_root=Path(output_root),
        media_sha_by_item=media_sha_by_item,
        created_at=created_at,
        min_group_samples=min_group_samples,
        split_seed=split_seed,
        source_metadata=source_metadata,
    )


def verify_interaction_heat_artifact(
    artifact_dir: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    requested_root = Path(os.path.abspath(Path(artifact_dir).expanduser()))
    if requested_root.is_symlink():
        raise ValueError("artifact directory must not be a symlink")
    root = requested_root.resolve()
    manifest_path = root / "manifest.json"
    manifest_bytes = _read_regular_file_no_follow(manifest_path)
    if manifest_bytes is None:
        raise ValueError("artifact manifest must be a regular file inside the artifact directory")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    expected_files = set(files) == _ARTIFACT_PAYLOAD_FILES
    expected_directory_entries = _ARTIFACT_PAYLOAD_FILES | {"manifest.json"}
    actual_directory_entries = {entry.name for entry in os.scandir(root)}
    exact_directory_entries = actual_directory_entries == expected_directory_entries
    safe_paths = {
        name: not (root / name).is_symlink()
        and (root / name).resolve().parent == root
        for name in _ARTIFACT_PAYLOAD_FILES
    }
    file_checks = {}
    for name in sorted(_ARTIFACT_PAYLOAD_FILES):
        path = root / name
        expected = files.get(name)
        actual = _regular_file_sha256_no_follow(path) if safe_paths[name] else None
        file_checks[name] = bool(
            safe_paths[name]
            and isinstance(expected, str)
            and re.fullmatch(r"[0-9a-f]{64}", expected)
            and actual == expected
        )
    manifest_sha256 = str(manifest.get("manifest_sha256") or "").lower()
    pinned_sha256 = str(expected_manifest_sha256 or "").strip().lower()
    checks = {
        "contract_version": manifest.get("contract_version") == INTERACTION_HEAT_ARTIFACT_VERSION,
        "lifecycle_frozen": manifest.get("lifecycle") == "frozen",
        "manifest_sha256": manifest_sha256 == _manifest_sha256(manifest),
        "trusted_manifest_sha256": bool(
            re.fullmatch(r"[0-9a-f]{64}", pinned_sha256)
            and manifest_sha256 == pinned_sha256
        ),
        "expected_files": expected_files,
        "exact_directory_entries": exact_directory_entries,
        "safe_file_paths": all(safe_paths.values()),
        "files": expected_files and all(file_checks.values()),
        "zero_network": int(manifest.get("network_request_count") or 0) == 0,
        "zero_model_cost": str(manifest.get("effective_model_cost_cny") or "") == "0.000000",
        "v2_not_overwritten": not bool(
            ((manifest.get("production_impact") or {}).get("visible_engagement_v2_overwritten"))
        ),
    }
    return {
        "status": "verified" if all(checks.values()) else "drift_detected",
        "passed": all(checks.values()),
        "artifact_id": manifest.get("artifact_id") or root.name,
        "checks": checks,
        "file_checks": file_checks,
        "manifest_path": str(manifest_path),
    }


def _has_metric_value(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _prepare_row(row: dict[str, Any]) -> dict[str, Any]:
    sample_id = str(row.get("id") or row.get("sample_id") or "").strip()
    account = str(row.get("account_id") or "").strip()
    if not sample_id:
        raise ValueError("interaction heat V3 requires a stable sample ID")
    sources = row.get("metric_sources") if isinstance(row.get("metric_sources"), dict) else None
    counts: dict[str, int | None] = {}
    missing: dict[str, bool] = {}
    for metric in METRIC_TARGETS:
        value = row.get(metric)
        has_value = _has_metric_value(value)
        available = (
            bool(sources.get(metric)) and has_value
            if sources is not None
            else has_value
        )
        if not available:
            counts[metric] = None
            missing[metric] = True
            continue
        assert value is not None
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {metric} count for {sample_id}") from exc
        if number < 0:
            raise ValueError(f"negative {metric} count for {sample_id}")
        counts[metric] = number
        missing[metric] = False
    observed_at = _observation_time(row)
    published_at = _iso_datetime(row.get("published_at"))
    return {
        "sample_id": sample_id,
        "account_id": account,
        "dataset_id": str(row.get("dataset_id") or ""),
        "program_key": str(row.get("program_key") or ""),
        "program_name": str(row.get("program_name") or ""),
        "song_title": str(row.get("song_title") or ""),
        "platform": str(row.get("platform") or "douyin"),
        "platform_item_id": str(row.get("platform_item_id") or ""),
        "sample_key": str(row.get("sample_key") or ""),
        "title": str(row.get("title") or ""),
        "title_key": _stable_title_key(row.get("title")),
        "published_at": published_at.isoformat() if published_at else "",
        "observed_at": observed_at.isoformat() if observed_at else "",
        "age_bucket": _age_bucket(published_at, observed_at),
        "duration_bucket": _duration_bucket(row.get("duration_seconds")),
        "duration_seconds": _nonnegative_float(row.get("duration_seconds")),
        "counts": counts,
        "metric_missing": missing,
    }


def _fit_normalizers(rows: list[dict[str, Any]], *, min_group_samples: int) -> dict[str, Any]:
    del min_group_samples
    grouped: dict[str, dict[str, list[float]]] = {
        metric: defaultdict(list) for metric in METRIC_TARGETS
    }
    for row in rows:
        for metric in METRIC_TARGETS:
            value = row["counts"][metric]
            if value is None:
                continue
            transformed = math.log1p(value)
            for key in _normalizer_keys(row):
                grouped[metric][key].append(transformed)
    fitted: dict[str, Any] = {}
    for metric, groups in grouped.items():
        fitted[metric] = {
            key: _normalizer_payload(key, values)
            for key, values in sorted(groups.items())
        }
    return fitted


def _normalizer_keys(row: dict[str, Any]) -> list[str]:
    account = row["account_id"]
    age = row["age_bucket"]
    duration = row["duration_bucket"]
    return [
        f"account={account}|age={age}|duration={duration}",
        f"account={account}|age={age}",
        f"account={account}",
        f"global|age={age}|duration={duration}",
        f"global|age={age}",
        "global",
    ]


def _normalizer_payload(key: str, values: list[float]) -> dict[str, Any]:
    clean = sorted(float(value) for value in values)
    knots = []
    index = 0
    while index < len(clean):
        value = clean[index]
        end = index + 1
        while end < len(clean) and clean[end] == value:
            end += 1
        percentile = ((index + end) / 2.0) / len(clean)
        knots.append([round(value, 8), round(percentile, 8)])
        index = end
    return {
        "scope": _normalizer_scope(key),
        "sample_count": len(clean),
        "median": round(_quantile(clean, 0.5), 8),
        "iqr": round(_quantile(clean, 0.75) - _quantile(clean, 0.25), 8),
        "knots": knots,
        "training_values_sha256": _records_sha256([round(value, 8) for value in clean]),
    }


def _score_row(
    row: dict[str, Any],
    fitted: dict[str, dict[str, Any]],
    *,
    min_group_samples: int,
) -> dict[str, Any]:
    targets: dict[str, float | None] = {}
    scopes: dict[str, str] = {}
    sample_counts: dict[str, int] = {}
    scope_grades = []
    for metric, target in METRIC_TARGETS.items():
        value = row["counts"][metric]
        if value is None:
            targets[target] = None
            scopes[target] = "missing"
            sample_counts[target] = 0
            scope_grades.append(0.0)
            continue
        normalizer = _select_normalizer(
            row,
            fitted.get(metric) or {},
            min_group_samples=min_group_samples,
        )
        targets[target] = round(_percentile_from_knots(math.log1p(value), normalizer["knots"]), 6)
        scopes[target] = str(normalizer["scope"])
        sample_counts[target] = int(normalizer["sample_count"])
        scope_grades.append(_scope_grade(str(normalizer["scope"])))
    components: list[float] = []
    for target in METRIC_TARGETS.values():
        component = targets[target]
        if component is not None:
            components.append(component)
    targets["broad_heat"] = round(sum(components) / len(components), 6) if len(components) >= 3 else None
    sample_counts["broad_heat"] = min(
        (
            sample_counts[target]
            for target in METRIC_TARGETS.values()
            if targets[target] is not None
        ),
        default=0,
    )
    observed = len(components)
    score = round((sum(scope_grades) / 4.0) * (observed / 4.0), 4)
    if score >= 0.85 and observed == 4:
        grade = "high"
    elif score >= 0.55 and observed >= 3:
        grade = "medium"
    else:
        grade = "low"
    return {
        "targets": targets,
        "normalization_scopes": scopes,
        "normalization_sample_counts": sample_counts,
        "confidence": {
            "grade": grade,
            "score": score,
            "observed_metric_count": observed,
            "age_bucket_known": row["age_bucket"] != "age_unknown",
            "duration_bucket_known": row["duration_bucket"] != "duration_unknown",
        },
    }


def _select_normalizer(
    row: dict[str, Any],
    fitted: dict[str, Any],
    *,
    min_group_samples: int,
) -> dict[str, Any]:
    keys = _normalizer_keys(row)
    fallback = None
    for key in keys:
        item = fitted.get(key)
        if not item:
            continue
        if fallback is None:
            fallback = item
        if int(item.get("sample_count") or 0) >= min_group_samples:
            return item
    if fallback:
        return fallback
    raise ValueError("training partition has no available normalization values for metric")


def _account_time_splits(rows: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, Any]]:
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_account[row["account_id"]].append(row)
    provisional: dict[str, str] = {}
    hash_fallback_count = 0
    for account_rows in by_account.values():
        ordered = sorted(account_rows, key=_time_sort_key)
        n = len(ordered)
        train_end = max(1, math.ceil(n * 0.70))
        validation_end = max(train_end + 1, math.ceil(n * 0.80)) if n >= 3 else train_end
        validation_end = min(validation_end, max(train_end, n - 1)) if n > 1 else n
        for index, row in enumerate(ordered):
            if not row["published_at"]:
                hash_fallback_count += 1
            if index < train_end:
                split = "train"
            elif index < validation_end:
                split = "validation"
            else:
                split = "test"
            provisional[row["sample_id"]] = split
    by_group: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_group[row["source_group_id"]].append(row["sample_id"])
    final = dict(provisional)
    promoted = 0
    for sample_ids in by_group.values():
        group_split = max(
            (provisional[sample_id] for sample_id in sample_ids),
            key=lambda value: SPLIT_RANK[value],
        )
        for sample_id in sample_ids:
            if final[sample_id] != group_split:
                promoted += 1
            final[sample_id] = group_split
    return final, {
        "policy": "account-local chronological 70/10/20; source groups move to the latest member partition",
        "counts": dict(Counter(final.values())),
        "source_group_promoted_row_count": promoted,
        "timestamp_hash_fallback_count": hash_fallback_count,
    }


def _account_holdout_splits(
    rows: list[dict[str, Any]],
    *,
    split_seed: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    accounts = sorted({row["account_id"] for row in rows})
    ordered_accounts = sorted(
        accounts,
        key=lambda account: hashlib.sha256(
            f"{split_seed}|{account}".encode("utf-8")
        ).hexdigest(),
    )
    test_target = max(1, math.ceil(len(accounts) * 0.20))
    validation_target = max(1, math.ceil(len(accounts) * 0.10))
    test_accounts = set(ordered_accounts[:test_target])
    validation_accounts = set(
        ordered_accounts[test_target : test_target + validation_target]
    )
    account_split = {
        account: (
            "test"
            if account in test_accounts
            else "validation"
            if account in validation_accounts
            else "train"
        )
        for account in accounts
    }
    base = {row["sample_id"]: account_split[row["account_id"]] for row in rows}
    final = dict(base)
    by_group: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_group[row["source_group_id"]].append(row["sample_id"])
    excluded_by_origin: Counter[str] = Counter()
    for sample_ids in by_group.values():
        group_splits = {base[sample_id] for sample_id in sample_ids}
        if len(group_splits) <= 1:
            continue
        retained_split = max(group_splits, key=lambda value: SPLIT_RANK[value])
        for sample_id in sample_ids:
            if base[sample_id] != retained_split:
                excluded_by_origin[base[sample_id]] += 1
                final[sample_id] = "excluded_leakage"
    return final, {
        "policy": (
            "whole-account deterministic holdout; rows whose source group reaches a later holdout "
            "fold are excluded from the earlier fold"
        ),
        "counts": dict(Counter(final.values())),
        "account_counts": dict(Counter(account_split.values())),
        "accounts": {
            split: sorted(account for account, value in account_split.items() if value == split)
            for split in ("train", "validation", "test")
        },
        "excluded_leakage_by_original_split": dict(excluded_by_origin),
    }


def _build_source_groups(
    rows: list[dict[str, Any]],
    media_sha_by_item: dict[str, list[str]],
) -> tuple[dict[str, str], dict[str, list[str]], int]:
    parent = {row["sample_id"]: row["sample_id"] for row in rows}
    evidence: dict[str, set[str]] = defaultdict(set)

    def find(sample_id: str) -> str:
        while parent[sample_id] != sample_id:
            parent[sample_id] = parent[parent[sample_id]]
            sample_id = parent[sample_id]
        return sample_id

    def union(left: str, right: str, reason: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)
        evidence[left].add(reason)
        evidence[right].add(reason)

    identity: dict[tuple[str, str], str] = {}
    media_covered = 0
    for row in rows:
        sample_id = row["sample_id"]
        keys: list[tuple[str, str]] = []
        platform_item = row["platform_item_id"]
        if platform_item:
            keys.append(("platform_item_id", f"{row['platform']}:{platform_item}"))
        if row["title_key"]:
            keys.append(("stable_title", row["title_key"]))
        program = _stable_title_key(row["program_name"])
        song = _stable_title_key(row["song_title"])
        if program and song:
            keys.append(("program_song", f"{program}|{song}"))
        media_hashes = sorted(set(media_sha_by_item.get(platform_item) or []))
        if media_hashes:
            media_covered += 1
        for media_sha in media_hashes:
            keys.append(("media_sha256", media_sha))
        for key in keys:
            previous = identity.get(key)
            if previous:
                union(previous, sample_id, key[0])
            else:
                identity[key] = sample_id
            evidence[sample_id].add(key[0])

    _union_near_titles(rows, union)
    members: dict[str, list[str]] = defaultdict(list)
    for sample_id in parent:
        members[find(sample_id)].append(sample_id)
    group_by_id: dict[str, str] = {}
    evidence_by_group: dict[str, list[str]] = {}
    for sample_ids in members.values():
        ordered = sorted(sample_ids)
        group_id = "source-" + hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()[:16]
        reasons = sorted({reason for sample_id in ordered for reason in evidence[sample_id]})
        if not reasons:
            reasons = ["unique_sample"]
        for sample_id in ordered:
            group_by_id[sample_id] = group_id
        evidence_by_group[group_id] = reasons
    return group_by_id, evidence_by_group, media_covered


def _union_near_titles(rows: list[dict[str, Any]], union: Any) -> None:
    title_rows = [(row["sample_id"], row["title_key"]) for row in rows if len(row["title_key"]) >= 8]
    shingles = {sample_id: _char_shingles(title) for sample_id, title in title_rows}
    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for sample_id, grams in shingles.items():
        if not grams:
            continue
        for band in range(6):
            signature = min(zlib.crc32(f"{band}|{gram}".encode("utf-8")) for gram in grams)
            buckets[(band, signature)].append(sample_id)
    candidate_pairs: set[tuple[str, str]] = set()
    for members in buckets.values():
        if len(members) > 256:
            continue
        ordered = sorted(set(members))
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                candidate_pairs.add((left, right))
    for left, right in sorted(candidate_pairs):
        left_grams, right_grams = shingles[left], shingles[right]
        if min(len(left_grams), len(right_grams)) / max(1, max(len(left_grams), len(right_grams))) < 0.80:
            continue
        similarity = len(left_grams & right_grams) / max(1, len(left_grams | right_grams))
        if similarity >= 0.90:
            union(left, right, "near_title_jaccard")


def _leakage_audit(split_rows: list[dict[str, Any]], split_field: str) -> dict[str, Any]:
    group_splits: dict[str, set[str]] = defaultdict(set)
    for row in split_rows:
        split = row[split_field]
        if split == "excluded_leakage":
            continue
        group_splits[row["source_group_id"]].add(split)
    leaking = sorted(group for group, splits in group_splits.items() if len(splits) > 1)
    return {
        "cross_split_source_group_count": len(leaking),
        "cross_split_source_group_ids": leaking[:20],
        "passed": not leaking,
    }


def _label_distribution(labels: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for target in [*METRIC_TARGETS.values(), "broad_heat"]:
        values = sorted(
            float(item["targets"][target])
            for item in labels
            if item["targets"].get(target) is not None
        )
        result[target] = {
            "count": len(values),
            "missing_count": len(labels) - len(values),
            "p05": round(_quantile(values, 0.05), 6) if values else None,
            "p25": round(_quantile(values, 0.25), 6) if values else None,
            "p50": round(_quantile(values, 0.50), 6) if values else None,
            "p75": round(_quantile(values, 0.75), 6) if values else None,
            "p95": round(_quantile(values, 0.95), 6) if values else None,
        }
    result["confidence_grades"] = dict(Counter(item["confidence"]["grade"] for item in labels))
    return result


def _input_fingerprint_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": row["sample_id"],
        "account_id": row["account_id"],
        "dataset_id": row["dataset_id"],
        "platform_item_id": row["platform_item_id"],
        "title_sha256": hashlib.sha256(row["title"].encode("utf-8")).hexdigest(),
        "published_at": row["published_at"],
        "observed_at": row["observed_at"],
        "duration_seconds": row["duration_seconds"],
        "counts": row["counts"],
        "metric_missing": row["metric_missing"],
        "source_group_id": row["source_group_id"],
    }


def _observation_time(row: dict[str, Any]) -> datetime | None:
    for value in (row.get("observed_at"), row.get("collected_at")):
        parsed = _iso_datetime(value)
        if parsed:
            return parsed
    match = _DATASET_DATE.search(str(row.get("dataset_id") or ""))
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_bucket(published: datetime | None, observed: datetime | None) -> str:
    if not published or not observed:
        return "age_unknown"
    days = max(0, int((observed - published).total_seconds() // 86400))
    if days <= 7:
        return "age_0_7d"
    if days <= 30:
        return "age_8_30d"
    if days <= 90:
        return "age_31_90d"
    if days <= 365:
        return "age_91_365d"
    return "age_366d_plus"


def _duration_bucket(value: Any) -> str:
    seconds = _nonnegative_float(value)
    if seconds <= 0:
        return "duration_unknown"
    if seconds <= 15:
        return "duration_0_15s"
    if seconds <= 30:
        return "duration_16_30s"
    if seconds <= 60:
        return "duration_31_60s"
    if seconds <= 180:
        return "duration_61_180s"
    return "duration_180s_plus"


def _stable_title_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = _TITLE_CLEANUP.sub("", text)
    text = re.sub(r"\d+", "#", text)
    return text[:120]


def _char_shingles(value: str, width: int = 3) -> set[str]:
    if len(value) <= width:
        return {value} if value else set()
    return {value[index : index + width] for index in range(len(value) - width + 1)}


def _time_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    timestamp = row["published_at"]
    if timestamp:
        return timestamp, row["sample_id"]
    digest = hashlib.sha256(row["sample_id"].encode("utf-8")).hexdigest()
    return "9999-12-31T23:59:59+00:00", digest


def _scope_grade(scope: str) -> float:
    return {
        "account_age_duration": 1.0,
        "account_age": 0.9,
        "account": 0.8,
        "global_age_duration": 0.7,
        "global_age": 0.6,
        "global": 0.5,
    }.get(scope, 0.0)


def _normalizer_scope(key: str) -> str:
    if key.startswith("account="):
        if "|duration=" in key:
            return "account_age_duration"
        if "|age=" in key:
            return "account_age"
        return "account"
    if "|duration=" in key:
        return "global_age_duration"
    if "|age=" in key:
        return "global_age"
    return "global"


def _percentile_from_knots(value: float, knots: list[list[float]]) -> float:
    if not knots:
        raise ValueError("normalizer knots are empty")
    values = [float(item[0]) for item in knots]
    index = bisect_left(values, value)
    if index < len(values) and values[index] == value:
        return float(knots[index][1])
    if index == 0:
        return 0.0
    if index >= len(values):
        return 1.0
    left_value, left_percentile = map(float, knots[index - 1])
    right_value, right_percentile = map(float, knots[index])
    if right_value <= left_value:
        return left_percentile
    weight = (value - left_value) / (right_value - left_value)
    return max(0.0, min(1.0, left_percentile + weight * (right_percentile - left_percentile)))


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    clean = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, fraction)) * (len(clean) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    return clean[lower] + (clean[upper] - clean[lower]) * (position - lower)


def _nonnegative_float(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def _local_media_sha_index(repo: Path) -> tuple[dict[str, list[str]], dict[str, Any]]:
    roots = [repo / "data" / "douyin_media_assets", repo / "outputs" / "v0.7_media_collection_test"]
    index: dict[str, set[str]] = defaultdict(set)
    path_hashes: dict[Path, str] = {}
    scanned = 0
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _MEDIA_SUFFIXES:
                continue
            scanned += 1
            digest = path_hashes.setdefault(path.resolve(), _file_sha256(path))
            ids = re.findall(r"\d{10,24}", str(path))
            for item_id in ids[-2:]:
                index[item_id].add(digest)
    return (
        {key: sorted(values) for key, values in sorted(index.items())},
        {
            "media_file_count": scanned,
            "platform_item_count": len(index),
            "roots": [str(path.relative_to(repo)) for path in roots if path.exists()],
            "hash_algorithm": "sha256",
        },
    )


def _artifact_id(value: str) -> str:
    key = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", key):
        raise ValueError("artifact_id must be 3-128 safe characters")
    return key


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    path.write_text(text, encoding="utf-8")


def _records_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_sha256(manifest: dict[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    return _records_sha256(payload)


def _read_regular_file_no_follow(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _regular_file_sha256_no_follow(path: Path) -> str | None:
    payload = _read_regular_file_no_follow(path)
    return hashlib.sha256(payload).hexdigest() if payload is not None else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(repo: Path, *args: str) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()
