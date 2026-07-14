from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from dso.config import project_root
from dso.db.session import connect, fetch_all, fetch_one
from dso.learning.material_evidence import material_evidence_cache_index
from dso.learning.qwen_omni import qwen_omni_shadow_cache_index
from dso.utils import utc_now, write_json
from dso.versions import (
    BACKTEST_VERSION,
    BENCHMARK_MANIFEST_VERSION,
    MATERIAL_EVIDENCE_VERSION,
    MATERIAL_RESOLVER_VERSION,
    RESEARCH_LABEL_VERSION,
    RESEARCH_RANKER_VERSION,
)


DEFAULT_BENCHMARK_ID = "dso-v1-beta-d10-ab-20260715-r1"
BENCHMARK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")

HISTORICAL_FINGERPRINT_FIELDS = (
    "id",
    "account_id",
    "dataset_id",
    "platform_item_id",
    "sample_key",
    "title",
    "views",
    "likes",
    "comments",
    "favorites",
    "shares",
    "follows",
    "content_category",
    "hook_type",
    "slice_structure",
    "program_name",
    "artist_names",
    "song_title",
    "tags",
    "published_at",
    "reward_proxy",
    "normalized_reward",
    "performance_label",
    "label_rank",
    "label_percentile",
    "label_reason",
    "quality_grade",
    "quality_score",
    "feature_version",
    "duration_seconds",
    "media_type",
    "commercial_intent",
    "rights_risk",
    "classification_confidence",
    "semantic_feature_version",
    "research_label_version",
    "semantic_unknown_reason",
    "structure_confidence",
    "structure_evidence",
    "structure_unknown_reason",
    "original_sound_owner",
    "is_original_sound",
    "entity_signal",
)

GOLD_FINGERPRINT_FIELDS = (
    "sample_id",
    "account_id",
    "dataset_id",
    "domain_category",
    "material_type",
    "program_context",
    "presentation_style",
    "review_status",
)

DEFAULT_SOURCE_FILES = (
    "src/dso/versions.py",
    "src/dso/learning/backtest.py",
    "src/dso/learning/benchmark_manifest.py",
    "src/dso/learning/historical_samples.py",
    "src/dso/learning/material_calibration.py",
    "src/dso/learning/material_confusion.py",
    "src/dso/learning/material_evidence.py",
    "src/dso/learning/material_taxonomy.py",
    "src/dso/learning/qwen_omni.py",
)

DEFAULT_COMPARISON_STRATEGIES = (
    "current_rules",
    "semantic_baseline_v2",
    "research_ranker_v2_2",
    "research_ranker_v2_4",
    "research_ranker_v2_8_material_calibrated",
    "research_ranker_v2_9_material_taxonomy",
)

VOLATILE_CACHE_KEYS = {
    "cache_path",
    "clip_path",
    "source_path",
    "report_path",
    "generated_at",
    "created_at",
    "updated_at",
}


def benchmark_manifest_path(benchmark_id: str = DEFAULT_BENCHMARK_ID) -> Path:
    value = _normalize_benchmark_id(benchmark_id)
    return project_root() / "benchmarks" / f"{value}.json"


def load_benchmark_manifest(benchmark_id: str = DEFAULT_BENCHMARK_ID, *, path: str | Path | None = None) -> dict:
    manifest_path = Path(path) if path else benchmark_manifest_path(benchmark_id)
    if not manifest_path.is_file():
        raise FileNotFoundError(str(manifest_path))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("benchmark manifest must be a JSON object")
    if payload.get("contract_version") != BENCHMARK_MANIFEST_VERSION:
        raise ValueError(f"unsupported benchmark manifest contract: {payload.get('contract_version')}")
    _normalize_benchmark_id(str(payload.get("benchmark_id") or ""))
    return payload


def freeze_benchmark_manifest(
    benchmark_id: str = DEFAULT_BENCHMARK_ID,
    *,
    reference_report_id: str | None = None,
    path: str | Path | None = None,
    source_files: list[str] | tuple[str, ...] | None = None,
) -> dict:
    value = _normalize_benchmark_id(benchmark_id)
    manifest_path = Path(path) if path else benchmark_manifest_path(value)
    if manifest_path.exists():
        raise FileExistsError(f"frozen benchmark already exists: {manifest_path}")

    run_config = {
        "account_id": None,
        "k": 30,
        "strategy": "research_ranker_v2_9_material_taxonomy",
        "holdout_policy": "time",
        "label_version": RESEARCH_LABEL_VERSION,
    }
    tracked_source_files = list(source_files if source_files is not None else DEFAULT_SOURCE_FILES)
    snapshot = current_benchmark_snapshot(
        account_id=run_config["account_id"],
        label_version=run_config["label_version"],
        source_files=tracked_source_files,
    )
    report = _reference_report_snapshot(reference_report_id, comparison_strategies=DEFAULT_COMPARISON_STRATEGIES)
    manifest = {
        "contract_version": BENCHMARK_MANIFEST_VERSION,
        "benchmark_id": value,
        "lifecycle": "frozen",
        "created_at": utc_now(),
        "purpose": "V1 Beta-D10 A/B historical ranker and material evidence checkpoint",
        "immutability_policy": "Never edit a frozen manifest. Create a new benchmark_id when data, caches, Gold, source, or evaluation policy changes.",
        "git": {
            "parent_commit": _git_value("rev-parse", "HEAD"),
            "branch_at_freeze": _git_value("branch", "--show-current"),
            "source_state": "content-addressed_worktree_snapshot",
        },
        "versions": {
            "backtest": BACKTEST_VERSION,
            "research_labels": RESEARCH_LABEL_VERSION,
            "research_ranker": RESEARCH_RANKER_VERSION,
            "material_evidence": MATERIAL_EVIDENCE_VERSION,
            "material_resolver": MATERIAL_RESOLVER_VERSION,
        },
        "run_config": run_config,
        "suites": {
            "historical_ranker": {
                "sample_source": "historical_capture_samples",
                "row_filter": "platform_item_id is present and reward_proxy or normalized_reward is positive",
                "split": "account-local published_at 80/20 time split; hash fallback only when timestamps are incomplete",
                "leakage_guard": ["platform_item_id", "stable_title_key"],
                "comparison_strategies": list(DEFAULT_COMPARISON_STRATEGIES),
                "metric_basis": "visible engagement proxy; no play-count imputation",
            },
            "material_resolver_shadow": {
                "queue_limit": 100,
                "include_reviewed": True,
                "window_seconds": 8.0,
                "windows": ["hook", "middle", "payoff"],
                "minimum_cached_gold": 30,
                "minimum_evidence_coverage": 0.85,
                "minimum_canonical_accuracy": 0.85,
                "maximum_severe_error_rate": 0.10,
                "minimum_gain_vs_omni": 0.03,
                "production_weight": False,
            },
        },
        "snapshot": snapshot,
        "reference_report": report,
        "known_limitations": [
            "The historical metric is a visible-engagement proxy, not a play-count or publishing forecast.",
            "The frozen D10-B evidence cache currently has low confirmed-Gold coverage.",
            "A frozen result is comparable only when benchmark verification passes without drift.",
        ],
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "UTF-8 JSON, sorted keys, compact separators; volatile cache paths and timestamps excluded",
            "content_sha256": "",
        },
    }
    manifest["integrity"]["content_sha256"] = benchmark_manifest_digest(manifest)
    write_json(manifest_path, manifest)
    return {"status": "frozen", "manifest_path": str(manifest_path), "manifest": manifest}


def verify_benchmark_manifest(
    benchmark_id: str = DEFAULT_BENCHMARK_ID,
    *,
    path: str | Path | None = None,
) -> dict:
    manifest_path = Path(path) if path else benchmark_manifest_path(benchmark_id)
    manifest = load_benchmark_manifest(benchmark_id, path=manifest_path)
    run_config = manifest.get("run_config") if isinstance(manifest.get("run_config"), dict) else {}
    frozen_snapshot = manifest.get("snapshot") if isinstance(manifest.get("snapshot"), dict) else {}
    source_files = ((frozen_snapshot.get("source_code") or {}).get("files") or []) if frozen_snapshot else []
    current_snapshot = current_benchmark_snapshot(
        account_id=run_config.get("account_id"),
        label_version=run_config.get("label_version") or RESEARCH_LABEL_VERSION,
        source_files=source_files,
    )
    expected_content_digest = str((manifest.get("integrity") or {}).get("content_sha256") or "")
    actual_content_digest = benchmark_manifest_digest(manifest)
    snapshot_checks = {
        key: frozen_snapshot.get(key) == current_snapshot.get(key)
        for key in sorted(set(frozen_snapshot) | set(current_snapshot))
    }
    checks = {
        "manifest_content_matches": bool(expected_content_digest) and expected_content_digest == actual_content_digest,
        "benchmark_id_matches_filename": manifest_path.stem == str(manifest.get("benchmark_id") or ""),
        "lifecycle_is_frozen": manifest.get("lifecycle") == "frozen",
        "snapshot_matches": all(snapshot_checks.values()),
    }
    passed = all(checks.values())
    return {
        "status": "verified" if passed else "drift_detected",
        "passed": passed,
        "benchmark_id": manifest.get("benchmark_id"),
        "manifest_path": str(manifest_path),
        "manifest_content_sha256": expected_content_digest,
        "checks": checks,
        "snapshot_checks": snapshot_checks,
        "drifted_sections": [key for key, matches in snapshot_checks.items() if not matches],
        "frozen_snapshot": frozen_snapshot,
        "current_snapshot": current_snapshot,
    }


def run_frozen_benchmark(
    benchmark_id: str = DEFAULT_BENCHMARK_ID,
    *,
    path: str | Path | None = None,
    allow_drift: bool = False,
) -> dict:
    verification = verify_benchmark_manifest(benchmark_id, path=path)
    if not verification["passed"] and not allow_drift:
        raise ValueError(
            "benchmark drift detected; create a new immutable benchmark manifest instead of comparing incompatible runs"
        )
    manifest = load_benchmark_manifest(benchmark_id, path=path)
    config = manifest.get("run_config") or {}
    from dso.learning.backtest import backtest_rule_ranker

    context = {
        "benchmark_id": manifest["benchmark_id"],
        "manifest_content_sha256": (manifest.get("integrity") or {}).get("content_sha256") or "",
        "verification_status": verification["status"],
        "allow_drift": bool(allow_drift),
    }
    report = backtest_rule_ranker(
        account_id=config.get("account_id"),
        k=int(config.get("k") or 30),
        strategy=str(config.get("strategy") or "research_ranker_v2_9_material_taxonomy"),
        holdout_policy=str(config.get("holdout_policy") or "time"),
        label_version=config.get("label_version") or RESEARCH_LABEL_VERSION,
        benchmark_context=context,
    )
    return {
        "status": "ready" if verification["passed"] else "completed_with_drift",
        "benchmark_id": manifest["benchmark_id"],
        "verification": verification,
        "report": report,
    }


def current_benchmark_snapshot(
    *,
    account_id: str | None,
    label_version: str,
    source_files: list[str] | tuple[str, ...],
) -> dict:
    account = str(account_id or "").strip()
    labels = str(label_version or RESEARCH_LABEL_VERSION).strip()
    clauses = [
        "COALESCE(platform_item_id, '') != ''",
        "(COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0)",
    ]
    params: list[Any] = []
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if labels.lower() not in {"", "all", "any"}:
        clauses.append("research_label_version = ?")
        params.append(labels)
    historical_columns = ", ".join(HISTORICAL_FINGERPRINT_FIELDS)
    gold_clauses = ["review_status = 'confirmed'"]
    gold_params: list[Any] = []
    if account and account.lower() != "all":
        gold_clauses.append("account_id = ?")
        gold_params.append(account)
    with connect() as conn:
        historical_rows = fetch_all(
            conn,
            f"SELECT {historical_columns} FROM historical_capture_samples WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        )
        gold_rows = fetch_all(
            conn,
            f"SELECT {', '.join(GOLD_FINGERPRINT_FIELDS)} FROM material_gold_annotations WHERE {' AND '.join(gold_clauses)} ORDER BY sample_id",
            gold_params,
        )

    sample_ids = {str(row.get("id") or "") for row in historical_rows}
    omni_items = []
    for sample_id, item in sorted(qwen_omni_shadow_cache_index().items()):
        if sample_id not in sample_ids:
            continue
        omni_items.append(
            {
                "sample_id": sample_id,
                "status": item.get("status") or "",
                "raw_semantic_suggestions": item.get("raw_semantic_suggestions") or {},
                "semantic_suggestions": item.get("semantic_suggestions") or {},
                "semantic_quality": item.get("semantic_quality") or {},
                "normalization_version": item.get("normalization_version") or "",
            }
        )
    evidence_items = [
        _without_volatile_cache_values(item)
        for sample_id, item in sorted(material_evidence_cache_index().items())
        if sample_id in sample_ids
    ]
    label_counts: dict[str, int] = {}
    for row in historical_rows:
        key = str(row.get("performance_label") or "unknown")
        label_counts[key] = label_counts.get(key, 0) + 1
    return {
        "historical_samples": {
            "count": len(historical_rows),
            "account_count": len({str(row.get("account_id") or "unknown") for row in historical_rows}),
            "label_counts": dict(sorted(label_counts.items())),
            "fields": list(HISTORICAL_FINGERPRINT_FIELDS),
            "sha256": _records_digest(historical_rows),
        },
        "material_gold": {
            "confirmed_count": len(gold_rows),
            "fields": list(GOLD_FINGERPRINT_FIELDS),
            "sha256": _records_digest(gold_rows),
        },
        "omni_shadow_cache": {
            "ready_count": len(omni_items),
            "sha256": _records_digest(omni_items),
        },
        "material_evidence_cache": {
            "ready_or_partial_count": sum(
                1 for item in evidence_items if item.get("status") in {"ready", "partial"}
            ),
            "record_count": len(evidence_items),
            "sha256": _records_digest(evidence_items),
        },
        "source_code": _source_snapshot(source_files),
    }


def benchmark_manifest_digest(manifest: dict) -> str:
    payload = copy.deepcopy(manifest)
    integrity = payload.get("integrity") if isinstance(payload.get("integrity"), dict) else {}
    integrity["content_sha256"] = ""
    payload["integrity"] = integrity
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _reference_report_snapshot(reference_report_id: str | None, *, comparison_strategies: tuple[str, ...]) -> dict:
    with connect() as conn:
        if reference_report_id:
            row = fetch_one(conn, "SELECT * FROM backtest_reports WHERE id = ?", [reference_report_id])
        else:
            row = fetch_one(conn, "SELECT * FROM backtest_reports ORDER BY created_at DESC LIMIT 1")
    if not row:
        return {"status": "missing", "report_id": reference_report_id or ""}
    try:
        payload = json.loads(row.get("metrics_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    comparison = metrics.get("strategy_comparison") if isinstance(metrics.get("strategy_comparison"), dict) else {}
    selected_comparison = {
        strategy: {
            key: (comparison.get(strategy) or {}).get(key)
            for key in [
                "sample_count",
                "k",
                "ndcg_at_k",
                "topk_lift_vs_random",
                "high_interaction_hit_rate",
                "low_interaction_avoidance_rate",
            ]
        }
        for strategy in comparison_strategies
        if isinstance(comparison.get(strategy), dict)
    }
    return {
        "status": row.get("status") or "",
        "report_id": row.get("id") or "",
        "created_at": row.get("created_at") or "",
        "sample_count": metrics.get("sample_count"),
        "k": metrics.get("k"),
        "strategy": metrics.get("strategy"),
        "holdout_policy_key": metrics.get("holdout_policy_key"),
        "research_label_version": metrics.get("research_label_version"),
        "label_counts": metrics.get("label_counts") or {},
        "split_summary": metrics.get("split_summary") or {},
        "leakage_guard_summary": metrics.get("leakage_guard_summary") or {},
        "material_gold_split": metrics.get("omni_material_gold_split") or {},
        "promotion_gate": metrics.get("promotion_gate") or {},
        "strategy_comparison": selected_comparison,
    }


def _source_snapshot(source_files: list[str] | tuple[str, ...]) -> dict:
    root = project_root()
    digest = hashlib.sha256()
    missing: list[str] = []
    normalized_files = sorted({str(Path(value).as_posix()) for value in source_files})
    for relative in normalized_files:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if not path.is_file():
            missing.append(relative)
            digest.update(b"<missing>")
        else:
            digest.update(path.read_bytes())
        digest.update(b"\n")
    return {"files": normalized_files, "missing_files": missing, "sha256": digest.hexdigest()}


def _records_digest(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical_json(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _without_volatile_cache_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_volatile_cache_values(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE_CACHE_KEYS and key != "raw"
        }
    if isinstance(value, list):
        return [_without_volatile_cache_values(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_benchmark_id(value: str) -> str:
    text = str(value or "").strip()
    if not BENCHMARK_ID_PATTERN.fullmatch(text):
        raise ValueError("benchmark_id must contain only letters, digits, dots, underscores, or hyphens")
    return text


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_root(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()
