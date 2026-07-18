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
from dso.scoring.ranking_policy import production_ranking_contract
from dso.utils import utc_now, write_json
from dso.versions import (
    BACKTEST_VERSION,
    BENCHMARK_MANIFEST_VERSION,
    MATERIAL_EVIDENCE_VERSION,
    MATERIAL_RESOLVER_VERSION,
    RESEARCH_LABEL_VERSION,
    RESEARCH_RANKER_VERSION,
    STANDARD_CANDIDATE_VERSION,
)


DEFAULT_BENCHMARK_ID = "dso-v1-beta-d10-ab-20260715-r1"
DEFAULT_CROSS_ENTRY_BENCHMARK_ID = "dso-v1-cross-entry-20260718-r2"
HISTORICAL_MATERIAL_BENCHMARK_KIND = "historical_material"
CROSS_ENTRY_BENCHMARK_KIND = "cross_entry"
BENCHMARK_KINDS = {HISTORICAL_MATERIAL_BENCHMARK_KIND, CROSS_ENTRY_BENCHMARK_KIND}
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

CROSS_ENTRY_SOURCE_FILES = tuple(
    sorted(
        (set(DEFAULT_SOURCE_FILES) - {"src/dso/versions.py"})
        | {
            "src/dso/precut.py",
            "src/dso/scoring/ranking_policy.py",
            "src/dso/scoring/scorer.py",
            "src/dso/segments/generator.py",
        }
    )
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
    benchmark_kind: str = HISTORICAL_MATERIAL_BENCHMARK_KIND,
) -> dict:
    value = _normalize_benchmark_id(benchmark_id)
    kind = _normalize_benchmark_kind(benchmark_kind)
    manifest_path = Path(path) if path else benchmark_manifest_path(value)
    if manifest_path.exists():
        raise FileExistsError(f"frozen benchmark already exists: {manifest_path}")

    run_config = {
        "account_id": None,
        "k": 30,
        "strategy": (
            "current_rules"
            if kind == CROSS_ENTRY_BENCHMARK_KIND
            else "research_ranker_v2_9_material_taxonomy"
        ),
        "holdout_policy": "time",
        "label_version": RESEARCH_LABEL_VERSION,
    }
    default_source_files = CROSS_ENTRY_SOURCE_FILES if kind == CROSS_ENTRY_BENCHMARK_KIND else DEFAULT_SOURCE_FILES
    tracked_source_files = list(source_files if source_files is not None else default_source_files)
    snapshot = current_benchmark_snapshot(
        account_id=run_config["account_id"],
        label_version=run_config["label_version"],
        source_files=tracked_source_files,
        benchmark_kind=kind,
    )
    report = _reference_report_snapshot(reference_report_id, comparison_strategies=DEFAULT_COMPARISON_STRATEGIES)
    purpose = (
        "G1 published-short ranking proxy, G2 generated-candidate snapshot, and shared production ranking contract"
        if kind == CROSS_ENTRY_BENCHMARK_KIND
        else "V1 Beta-D10 A/B historical ranker and material evidence checkpoint"
    )
    manifest = {
        "contract_version": BENCHMARK_MANIFEST_VERSION,
        "benchmark_id": value,
        "benchmark_kind": kind,
        "lifecycle": "frozen",
        "created_at": utc_now(),
        "purpose": purpose,
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
                "minimum_gold_queue_coverage": 0.95,
                "minimum_gold_evidence_coverage": 0.85,
                "minimum_evidence_coverage": 0.85,
                "minimum_gold_prediction_coverage": 0.85,
                "maximum_unknown_abstention_rate": 0.15,
                "minimum_canonical_accuracy": 0.85,
                "maximum_severe_error_rate": 0.10,
                "minimum_gain_vs_omni": 0.03,
                "production_weight": False,
            },
            **(
                {
                    "cross_entry_candidate_contract": {
                        "g1_entry": "immutable precut source asset -> one standard candidate",
                        "g2_entry": "program understanding and recall -> generated standard candidates",
                        "shared_ranker": "current_rules/final_score",
                        "research_rankers": "explicit research scope only",
                        "candidate_contract_version": STANDARD_CANDIDATE_VERSION,
                        "g1_proxy_metric": "published historical visible-engagement ranking",
                        "g2_recall_metric": "Recall@K when human segment-recall Gold becomes available",
                    }
                }
                if kind == CROSS_ENTRY_BENCHMARK_KIND
                else {}
            ),
        },
        "snapshot": snapshot,
        "reference_report": report,
        "production_ranking_policy": production_ranking_contract(),
        "cross_entry_readiness": (
            _cross_entry_readiness(snapshot, report) if kind == CROSS_ENTRY_BENCHMARK_KIND else None
        ),
        "known_limitations": [
            "The historical metric is a visible-engagement proxy, not a play-count or publishing forecast.",
            "The frozen D10-B evidence cache currently has low confirmed-Gold coverage.",
            "A frozen result is comparable only when benchmark verification passes without drift.",
            *(
                [
                    "G1 ranking uses published historical shorts as a visible-engagement proxy until real precut batches have feedback.",
                    "G2 Recall@K remains unavailable until human segment-recall Gold is added.",
                ]
                if kind == CROSS_ENTRY_BENCHMARK_KIND
                else []
            ),
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
    benchmark_kind = _normalize_benchmark_kind(
        str(manifest.get("benchmark_kind") or HISTORICAL_MATERIAL_BENCHMARK_KIND)
    )
    run_config = manifest.get("run_config") if isinstance(manifest.get("run_config"), dict) else {}
    frozen_snapshot = manifest.get("snapshot") if isinstance(manifest.get("snapshot"), dict) else {}
    source_files = ((frozen_snapshot.get("source_code") or {}).get("files") or []) if frozen_snapshot else []
    current_snapshot = current_benchmark_snapshot(
        account_id=run_config.get("account_id"),
        label_version=run_config.get("label_version") or RESEARCH_LABEL_VERSION,
        source_files=source_files,
        benchmark_kind=benchmark_kind,
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
        "benchmark_kind": benchmark_kind,
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
        "benchmark_kind": manifest.get("benchmark_kind") or HISTORICAL_MATERIAL_BENCHMARK_KIND,
        "production_ranking_policy": manifest.get("production_ranking_policy") or {},
        "cross_entry_readiness": manifest.get("cross_entry_readiness"),
        "verification": verification,
        "report": report,
    }


def current_benchmark_snapshot(
    *,
    account_id: str | None,
    label_version: str,
    source_files: list[str] | tuple[str, ...],
    benchmark_kind: str = HISTORICAL_MATERIAL_BENCHMARK_KIND,
) -> dict:
    kind = _normalize_benchmark_kind(benchmark_kind)
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
    snapshot = {
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
    if kind == CROSS_ENTRY_BENCHMARK_KIND:
        snapshot["cross_entry_candidates"] = _cross_entry_candidate_snapshot()
        snapshot["production_ranking_policy"] = production_ranking_contract()
    return snapshot


def _cross_entry_candidate_snapshot() -> dict:
    with connect() as conn:
        source_rows = fetch_all(
            conn,
            """
            SELECT id, account_id, title, file_path, duration_seconds, width, height, fps,
                   audio_streams, status, input_mode, content_hash, import_batch_id
            FROM source_videos
            ORDER BY id
            """,
        )
        candidate_rows = fetch_all(
            conn,
            """
            SELECT cs.id, cs.source_video_id, sv.account_id, sv.input_mode AS source_input_mode,
                   sv.duration_seconds AS source_duration_seconds,
                   cs.start_time, cs.end_time, cs.duration_seconds, cs.transcript, cs.summary,
                   cs.primary_topic, cs.song_section_type, cs.music_slice_type, cs.emotion_type,
                   cs.short_video_structure, cs.musical_moment, cs.program_context,
                   cs.comment_trigger, cs.cover_time, cs.status, cs.generation_signals_json,
                   cs.boundary_strategy, cs.boundary_confidence, cs.candidate_origin,
                   cs.boundary_locked, cs.source_content_hash, cs.import_batch_id,
                   cs.candidate_contract_version, ss.final_score, ss.ranker_score,
                   ss.ranker_version, ss.omni_score, ss.omni_confidence, ss.omni_status,
                   ss.hybrid_score, ss.hybrid_rank, ss.hybrid_ranker_version
            FROM candidate_segments cs
            JOIN source_videos sv ON sv.id = cs.source_video_id
            LEFT JOIN slice_scores ss ON ss.candidate_segment_id = cs.id
            ORDER BY cs.id
            """,
        )
        batch_rows = fetch_all(
            conn,
            """
            SELECT id, account_id, title, status, item_count, created_count, reused_count,
                   failed_count, processed_count, contract_version, error_summary
            FROM precut_import_batches
            ORDER BY id
            """,
        )
        item_rows = fetch_all(
            conn,
            """
            SELECT id, batch_id, position, source_name, title, content_hash, size_bytes,
                   source_video_id, candidate_segment_id, ingest_disposition, status, error
            FROM precut_import_items
            ORDER BY batch_id, position, id
            """,
        )

    source_records = []
    for row in source_rows:
        record = {key: value for key, value in row.items() if key != "file_path"}
        record["media_identity"] = _media_content_identity(
            row.get("file_path"),
            declared_hash=row.get("content_hash"),
        )
        source_records.append(record)

    input_mode_counts: dict[str, int] = {}
    for row in source_records:
        key = str(row.get("input_mode") or "program")
        input_mode_counts[key] = input_mode_counts.get(key, 0) + 1

    origin_counts: dict[str, int] = {}
    contract_version_counts: dict[str, int] = {}
    contract_mismatches: list[str] = []
    precut_boundary_violations: list[str] = []
    scored_count = 0
    for row in candidate_rows:
        origin = str(row.get("candidate_origin") or "generated")
        origin_counts[origin] = origin_counts.get(origin, 0) + 1
        version = str(row.get("candidate_contract_version") or "missing")
        contract_version_counts[version] = contract_version_counts.get(version, 0) + 1
        if version != STANDARD_CANDIDATE_VERSION:
            contract_mismatches.append(str(row.get("id") or ""))
        if row.get("final_score") is not None:
            scored_count += 1
        is_precut = origin == "precut" or str(row.get("source_input_mode") or "") == "precut"
        if is_precut and not _precut_boundary_is_locked(row):
            precut_boundary_violations.append(str(row.get("id") or ""))

    candidate_count = len(candidate_rows)
    precut_count = int(origin_counts.get("precut") or 0)
    generated_count = int(origin_counts.get("generated") or 0)
    return {
        "source_count": len(source_records),
        "source_input_mode_counts": dict(sorted(input_mode_counts.items())),
        "sources_sha256": _records_digest(source_records),
        "candidate_count": candidate_count,
        "scored_candidate_count": scored_count,
        "candidate_origin_counts": dict(sorted(origin_counts.items())),
        "candidate_contract_version_counts": dict(sorted(contract_version_counts.items())),
        "candidates_sha256": _records_digest(candidate_rows),
        "precut_batch_count": len(batch_rows),
        "precut_item_count": len(item_rows),
        "precut_batches_sha256": _records_digest(batch_rows),
        "precut_items_sha256": _records_digest(item_rows),
        "contract_checks": {
            "standard_candidate_contract_version": STANDARD_CANDIDATE_VERSION,
            "shared_candidate_contract_observed": candidate_count > 0 and not contract_mismatches,
            "contract_mismatch_count": len(contract_mismatches),
            "contract_mismatch_ids": contract_mismatches[:50],
            "precut_candidate_count": precut_count,
            "precut_boundary_invariant": (
                not precut_boundary_violations if precut_count else None
            ),
            "precut_boundary_violation_count": len(precut_boundary_violations),
            "precut_boundary_violation_ids": precut_boundary_violations[:50],
            "generated_candidate_count": generated_count,
        },
    }


def _precut_boundary_is_locked(row: dict) -> bool:
    start = float(row.get("start_time") or 0.0)
    end = float(row.get("end_time") or 0.0)
    duration = float(row.get("duration_seconds") or 0.0)
    source_duration = float(row.get("source_duration_seconds") or 0.0)
    return (
        int(row.get("boundary_locked") or 0) == 1
        and abs(start) <= 0.001
        and source_duration > 0
        and abs(end - source_duration) <= 0.05
        and abs(duration - source_duration) <= 0.05
    )


def _media_content_identity(path_value: Any, *, declared_hash: Any = "") -> dict:
    declared = str(declared_hash or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", declared):
        return {
            "status": "declared_sha256",
            "sha256": declared,
            "size_bytes": _file_size(path_value),
        }
    path = Path(str(path_value or ""))
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "status": "computed_sha256",
            "sha256": digest.hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    return {
        "status": "missing_media" if not declared else "declared_non_sha256",
        "sha256": declared,
        "size_bytes": 0,
    }


def _file_size(path_value: Any) -> int:
    path = Path(str(path_value or ""))
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _cross_entry_readiness(snapshot: dict, reference_report: dict) -> dict:
    historical = snapshot.get("historical_samples") or {}
    cross_entry = snapshot.get("cross_entry_candidates") or {}
    checks = cross_entry.get("contract_checks") or {}
    policy = snapshot.get("production_ranking_policy") or production_ranking_contract()
    g1_proxy_ready = int(historical.get("count") or 0) >= 100
    g1_real_candidates_observed = int(checks.get("precut_candidate_count") or 0) > 0
    g2_candidates_observed = int(checks.get("generated_candidate_count") or 0) > 0
    g2_recall_gold_available = False
    shared_contract_valid = bool(checks.get("shared_candidate_contract_observed"))
    production_policy_locked = (
        policy.get("default_strategy") == "current_rules"
        and policy.get("default_score_field") == "final_score"
        and policy.get("automatic_promotion") is False
    )
    baseline_ready = g1_proxy_ready and g2_candidates_observed and shared_contract_valid and production_policy_locked
    product_complete = baseline_ready and g1_real_candidates_observed and g2_recall_gold_available
    return {
        "status": (
            "cross_entry_product_complete"
            if product_complete
            else "baseline_frozen_with_known_gaps"
            if baseline_ready
            else "insufficient_cross_entry_baseline"
        ),
        "baseline_ready": baseline_ready,
        "product_complete": product_complete,
        "checks": {
            "g1_historical_proxy_ready": g1_proxy_ready,
            "g1_real_precut_candidates_observed": g1_real_candidates_observed,
            "g2_generated_candidates_observed": g2_candidates_observed,
            "shared_candidate_contract_valid": shared_contract_valid,
            "production_ranking_policy_locked": production_policy_locked,
            "g2_recall_gold_available": g2_recall_gold_available,
        },
        "known_gaps": [
            gap
            for gap, missing in [
                ("real_g1_precut_batch_feedback", not g1_real_candidates_observed),
                ("g2_human_segment_recall_gold", True),
            ]
            if missing
        ],
        "reference_promotion_gate": reference_report.get("promotion_gate") or {},
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


def _normalize_benchmark_kind(value: str) -> str:
    text = str(value or HISTORICAL_MATERIAL_BENCHMARK_KIND).strip().lower()
    if text not in BENCHMARK_KINDS:
        supported = ", ".join(sorted(BENCHMARK_KINDS))
        raise ValueError(f"benchmark_kind must be one of: {supported}")
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
