from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.backtest import (
    RESEARCH_RANKER_V23_STRATEGY,
    RESEARCH_RANKER_V24_STRATEGY,
    RESEARCH_RANKER_V24_WEIGHT_CONFIG,
    _account_ranker_profiles,
    _historical_group_baselines,
    _historical_strategy_scores,
    _history_candidate_index,
    _interaction_thresholds,
    _prepare_history_tokens,
    _score_v24_from_components,
    _select_v24_signal_gate_score,
    _v24_reliable_signal_row,
    _v24_signal_quality,
)
from dso.learning.multimodal_validation import _build_asset_index, _prepare_row
from dso.learning.qwen_embeddings import (
    QWEN_EMBEDDING_MODEL,
    TEXT_EMBEDDING_STRATEGY,
    TEXT_VISUAL_EMBEDDING_STRATEGY,
    VISUAL_EMBEDDING_STRATEGY,
    embedding_coverage_for_entity_ids,
    historical_embedding_backtest_context,
    historical_embedding_strategy_scores,
    historical_embedding_text,
)
from dso.utils import new_id, read_json, utc_now, write_json
from dso.versions import (
    MULTIMODAL_VECTOR_VALUE_VERSION,
    QWEN_EMBEDDING_VERSION,
    RESEARCH_LABEL_VERSION,
    RESEARCH_RANKER_VERSION,
)


MULTIMODAL_VECTOR_EXPERIMENT_VERSION = MULTIMODAL_VECTOR_VALUE_VERSION
DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID = "dso-multimodal-vector-value-20260719-r1"
DEFAULT_PAIR_COUNT = 60
DEFAULT_REFERENCE_PER_LABEL = 60
BLIND_CHOICES = {"left", "right", "tie", "abstain"}
REVIEW_CONFIDENCE = {"low", "medium", "high"}
REVIEW_REASON_TAGS = {
    "hook_clarity",
    "payoff_strength",
    "performance_quality",
    "context_completeness",
    "visual_quality",
    "emotional_value",
    "hard_to_judge",
}
EXPERIMENT_STRATEGIES = (
    "current_rules",
    RESEARCH_RANKER_V24_STRATEGY,
    TEXT_EMBEDDING_STRATEGY,
    VISUAL_EMBEDDING_STRATEGY,
    TEXT_VISUAL_EMBEDDING_STRATEGY,
)
_BENCHMARK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")


def freeze_multimodal_vector_experiment(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    *,
    pair_count: int = DEFAULT_PAIR_COUNT,
    reference_per_label: int = DEFAULT_REFERENCE_PER_LABEL,
) -> dict:
    benchmark_key = _normalize_benchmark_id(benchmark_id)
    pair_target = max(1, min(DEFAULT_PAIR_COUNT, int(pair_count or DEFAULT_PAIR_COUNT)))
    reference_target = max(1, min(120, int(reference_per_label or DEFAULT_REFERENCE_PER_LABEL)))
    path = multimodal_vector_manifest_path(benchmark_key)
    if path.exists():
        raise FileExistsError(f"frozen multimodal vector benchmark already exists: {path}")

    asset_index = _build_asset_index()
    gold = _confirmed_material_gold_rows(asset_index)
    if len(gold) < pair_target:
        raise ValueError(f"insufficient local Material Gold: need {pair_target}, found {len(gold)}")
    anchors = sorted(gold, key=_stable_row_key)[:pair_target]

    all_local = _local_historical_rows(asset_index)
    gold_ids = {str(row.get("id") or "") for row in gold}
    control_pool = [row for row in all_local if str(row.get("id") or "") not in gold_ids]
    controls = _match_controls(anchors, control_pool)
    evaluation_ids = {str(row.get("id") or "") for row in [*anchors, *controls]}
    evaluation_platform_ids = {str(row.get("platform_item_id") or "") for row in [*anchors, *controls]}
    evaluation_title_keys = {_stable_title_key(row.get("title")) for row in [*anchors, *controls]}
    references = _select_reference_rows(
        [
            row
            for row in all_local
            if str(row.get("id") or "") not in evaluation_ids
            and str(row.get("platform_item_id") or "") not in evaluation_platform_ids
            and _stable_title_key(row.get("title")) not in evaluation_title_keys
        ],
        per_label=reference_target,
    )
    if not references:
        raise ValueError("no disjoint high/low reference samples with local media")

    sample_rows = {str(row.get("id") or ""): row for row in [*anchors, *controls, *references]}
    samples = {sample_id: _sample_snapshot(row) for sample_id, row in sorted(sample_rows.items())}
    tasks = []
    for index, (anchor, control) in enumerate(zip(anchors, controls), start=1):
        pair_id = f"pair-{index:03d}"
        anchor_id = str(anchor.get("id") or "")
        control_id = str(control.get("id") or "")
        swap = int(hashlib.sha256(f"{benchmark_key}|{pair_id}".encode("utf-8")).hexdigest()[:2], 16) % 2 == 1
        left_id, right_id = (control_id, anchor_id) if swap else (anchor_id, control_id)
        tasks.append(
            {
                "task_id": pair_id,
                "left_sample_id": left_id,
                "right_sample_id": right_id,
                "anchor_sample_id": anchor_id,
                "control_sample_id": control_id,
                "blind_assignment": "deterministic_sha256",
            }
        )

    manifest = {
        "contract_version": MULTIMODAL_VECTOR_EXPERIMENT_VERSION,
        "benchmark_id": benchmark_key,
        "lifecycle": "frozen",
        "created_at": utc_now(),
        "purpose": "Measure the incremental ranking value of Qwen text and visual embeddings on collected short-video samples.",
        "goal_alignment": ["G1", "G2", "G3"],
        "admission_status": "research_only",
        "production_impact": {
            "ranking_weight_changed": False,
            "writes_material_gold": False,
            "writes_semantic_labels": False,
            "exports_or_publishes": False,
        },
        "data_semantics": {
            "primary_human_target": "blind pairwise preference for entering a controlled publishing test",
            "secondary_proxy": "historical visible-engagement relative label; not views or exposure",
            "platform_outcomes_available": False,
            "claim_limit": "The experiment can validate ranking and retrieval utility, not prove traffic lift.",
        },
        "selection_policy": {
            "anchor_source": "confirmed material_gold_annotations with local video",
            "control_policy": "non-Gold local sample matched by account, interaction contrast, duration, category, and stable title leakage guard",
            "reference_policy": "disjoint account-balanced high/low local samples used only as retrieval evidence",
            "pair_count": len(tasks),
            "reference_per_label_target": reference_target,
            "leakage_guard": ["sample_id", "platform_item_id", "stable_title_key"],
        },
        "embedding": {
            "model_name": QWEN_EMBEDDING_MODEL,
            "model_version": QWEN_EMBEDDING_VERSION,
            "modalities": ["text", "visual"],
            "similarity": "cosine",
            "targeted_build": True,
        },
        "ranking": {
            "baseline": RESEARCH_RANKER_V24_STRATEGY,
            "strategies": list(EXPERIMENT_STRATEGIES),
            "pair_tie_threshold": 0.05,
            "diversity_reranking": False,
        },
        "review": {
            "mode": "blind_pairwise",
            "labels_hidden": ["account_id", "performance_label", "reward_proxy", "anchor_or_control", "strategy_scores"],
            "choices": sorted(BLIND_CHOICES),
            "minimum_evaluable_reviews": 40,
            "reviewer_default": "local",
        },
        "counts": {
            "task_count": len(tasks),
            "evaluation_sample_count": len(evaluation_ids),
            "reference_sample_count": len(references),
            "total_embedding_sample_count": len(samples),
            "reference_labels": dict(Counter(str(row.get("performance_label") or "unknown") for row in references)),
        },
        "evaluation_sample_ids": sorted(evaluation_ids),
        "reference_sample_ids": [str(row.get("id") or "") for row in references],
        "tasks": tasks,
        "samples": samples,
        "versions": {
            "research_labels": RESEARCH_LABEL_VERSION,
            "research_ranker": RESEARCH_RANKER_VERSION,
            "qwen_embedding": QWEN_EMBEDDING_VERSION,
        },
        "immutability_policy": "Never edit or overwrite this manifest. Create a new benchmark_id when samples, media, labels, model inputs, or policy change.",
    }
    manifest["manifest_sha256"] = _canonical_digest(manifest)
    write_json(path, manifest)
    return {
        "status": "frozen",
        "benchmark_id": benchmark_key,
        "manifest_path": str(path),
        "manifest_sha256": manifest["manifest_sha256"],
        "counts": manifest["counts"],
        "production_impact": manifest["production_impact"],
    }


def multimodal_vector_experiment_status(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    *,
    reviewer_id: str = "local",
) -> dict:
    benchmark_key = _normalize_benchmark_id(benchmark_id)
    path = multimodal_vector_manifest_path(benchmark_key)
    if not path.is_file():
        return {
            "contract_version": MULTIMODAL_VECTOR_EXPERIMENT_VERSION,
            "status": "not_frozen",
            "benchmark_id": benchmark_key,
            "recommended_action": "freeze_benchmark",
            "production_weight": False,
        }
    manifest = load_multimodal_vector_manifest(benchmark_key)
    reviews = _review_index(benchmark_key, reviewer_id)
    tasks = manifest.get("tasks") if isinstance(manifest.get("tasks"), list) else []
    pending = [task for task in tasks if str(task.get("task_id") or "") not in reviews]
    evaluation_ids = [str(value) for value in manifest.get("evaluation_sample_ids") or []]
    reference_ids = [str(value) for value in manifest.get("reference_sample_ids") or []]
    evaluation_coverage = embedding_coverage_for_entity_ids(evaluation_ids)
    reference_coverage = embedding_coverage_for_entity_ids(reference_ids)
    report = _latest_report(benchmark_key)
    review_count = len(reviews)
    all_vectors_ready = (
        evaluation_coverage["text_visual_ready_count"] >= len(evaluation_ids)
        and reference_coverage["text_visual_ready_count"] >= len(reference_ids)
    )
    if pending:
        next_action = "build_embeddings" if not all_vectors_ready else "continue_blind_review"
    else:
        next_action = "run_comparison"
    return {
        "contract_version": MULTIMODAL_VECTOR_EXPERIMENT_VERSION,
        "status": "review_complete" if tasks and not pending else "in_progress",
        "benchmark_id": benchmark_key,
        "manifest_sha256": manifest.get("manifest_sha256") or "",
        "admission_status": "research_only",
        "progress": {
            "reviewed_count": review_count,
            "pending_count": len(pending),
            "task_count": len(tasks),
            "review_rate": round(review_count / max(1, len(tasks)), 4),
        },
        "embedding_coverage": {
            "evaluation": _public_coverage(evaluation_coverage),
            "reference": _public_coverage(reference_coverage),
        },
        "current_task": _public_blind_task(pending[0], manifest) if pending else None,
        "latest_result": _public_report(report) if report else None,
        "recommended_action": next_action,
        "production_impact": manifest.get("production_impact") or {},
        "generated_at": utc_now(),
    }


def multimodal_vector_embedding_request(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    manifest = load_multimodal_vector_manifest(benchmark_id)
    entity_ids = [
        *[str(value) for value in manifest.get("evaluation_sample_ids") or []],
        *[str(value) for value in manifest.get("reference_sample_ids") or []],
    ]
    return {
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "entity_ids": list(dict.fromkeys(entity_ids)),
        "entity_type": "historical_sample",
        "modality": "all",
        "force": False,
    }


def save_multimodal_vector_review(
    benchmark_id: str,
    task_id: str,
    payload: dict[str, Any],
) -> dict:
    manifest = load_multimodal_vector_manifest(benchmark_id)
    task = _task_by_id(manifest, task_id)
    choice = str(payload.get("choice") or "").strip().lower()
    confidence = str(payload.get("confidence") or "medium").strip().lower()
    reviewer_id = str(payload.get("reviewer_id") or payload.get("operator") or "local").strip() or "local"
    if choice not in BLIND_CHOICES:
        raise ValueError(f"unsupported blind review choice: {choice}")
    if confidence not in REVIEW_CONFIDENCE:
        raise ValueError(f"unsupported review confidence: {confidence}")
    raw_tags = payload.get("reason_tags") if isinstance(payload.get("reason_tags"), list) else []
    reason_tags = list(dict.fromkeys(str(value).strip() for value in raw_tags if str(value).strip() in REVIEW_REASON_TAGS))
    note = str(payload.get("review_note") or "").strip()[:1000]
    now = utc_now()
    with connect() as conn:
        existing = fetch_one(
            conn,
            "SELECT * FROM multimodal_vector_reviews WHERE benchmark_id = ? AND task_id = ? AND reviewer_id = ?",
            [manifest["benchmark_id"], task_id, reviewer_id],
        )
        row = {
            "id": existing.get("id") if existing else new_id("mmvreview"),
            "benchmark_id": manifest["benchmark_id"],
            "task_id": task_id,
            "reviewer_id": reviewer_id,
            "left_sample_id": str(task.get("left_sample_id") or ""),
            "right_sample_id": str(task.get("right_sample_id") or ""),
            "choice": choice,
            "confidence": confidence,
            "reason_tags_json": json.dumps(reason_tags, ensure_ascii=False),
            "review_note": note,
            "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
            "created_at": existing.get("created_at") if existing else now,
            "updated_at": now,
        }
        if existing:
            conn.execute(
                """
                UPDATE multimodal_vector_reviews
                SET choice = ?, confidence = ?, reason_tags_json = ?, review_note = ?,
                    left_sample_id = ?, right_sample_id = ?, manifest_sha256 = ?, updated_at = ?
                WHERE id = ?
                """,
                [
                    choice,
                    confidence,
                    row["reason_tags_json"],
                    note,
                    row["left_sample_id"],
                    row["right_sample_id"],
                    row["manifest_sha256"],
                    now,
                    row["id"],
                ],
            )
        else:
            insert_row(conn, "multimodal_vector_reviews", row)
        conn.commit()
    return {
        "status": "saved",
        "benchmark_id": manifest["benchmark_id"],
        "task_id": task_id,
        "choice": choice,
        "confidence": confidence,
        "reason_tags": reason_tags,
        "writes_material_gold": False,
        "writes_semantic_labels": False,
        "production_weight": False,
    }


def run_multimodal_vector_comparison(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    *,
    reviewer_id: str = "local",
) -> dict:
    manifest = load_multimodal_vector_manifest(benchmark_id)
    evaluation_ids = [str(value) for value in manifest.get("evaluation_sample_ids") or []]
    reference_ids = [str(value) for value in manifest.get("reference_sample_ids") or []]
    eval_rows = _historical_rows_by_ids(evaluation_ids)
    reference_rows = _historical_rows_by_ids(reference_ids)
    if len(eval_rows) != len(evaluation_ids):
        raise ValueError("frozen evaluation samples no longer match the database")
    if not reference_rows:
        raise ValueError("frozen reference pool is empty")

    scored = _score_experiment_rows(eval_rows, reference_rows)
    score_index = {str(row.get("id") or ""): row for row in scored}
    pairs = []
    for task in manifest.get("tasks") or []:
        left_id = str(task.get("left_sample_id") or "")
        right_id = str(task.get("right_sample_id") or "")
        left = score_index.get(left_id) or {}
        right = score_index.get(right_id) or {}
        predictions = {}
        deltas = {}
        for strategy in EXPERIMENT_STRATEGIES:
            delta = float((left.get("strategy_scores") or {}).get(strategy) or 0.0) - float(
                (right.get("strategy_scores") or {}).get(strategy) or 0.0
            )
            deltas[strategy] = round(delta, 4)
            predictions[strategy] = "tie" if abs(delta) < 0.05 else "left" if delta > 0 else "right"
        pairs.append(
            {
                "task_id": task.get("task_id") or "",
                "left_sample_id": left_id,
                "right_sample_id": right_id,
                "predictions": predictions,
                "score_deltas": deltas,
                "proxy_choice": _proxy_pair_choice(left, right),
            }
        )

    reviews = _review_index(str(manifest["benchmark_id"]), reviewer_id)
    strategy_comparison = {
        strategy: _pairwise_strategy_metrics(pairs, reviews, strategy)
        for strategy in EXPERIMENT_STRATEGIES
    }
    evaluation_coverage = embedding_coverage_for_entity_ids(evaluation_ids)
    reference_coverage = embedding_coverage_for_entity_ids(reference_ids)
    gate = _experiment_gate(strategy_comparison, reviews, evaluation_coverage, reference_coverage)
    report = {
        "contract_version": MULTIMODAL_VECTOR_EXPERIMENT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "status": "ready" if len(reviews) >= 40 else "needs_blind_review",
        "admission_status": "research_only",
        "review_summary": {
            "reviewer_id": reviewer_id,
            "reviewed_count": len(reviews),
            "required_count": 40,
            "pending_count": max(0, len(pairs) - len(reviews)),
            "choice_counts": dict(Counter(str(row.get("choice") or "") for row in reviews.values())),
        },
        "embedding_coverage": {
            "evaluation": _public_coverage(evaluation_coverage),
            "reference": _public_coverage(reference_coverage),
        },
        "strategy_comparison": strategy_comparison,
        "promotion_gate": gate,
        "pair_results": pairs,
        "production_impact": manifest.get("production_impact") or {},
        "limitations": [
            "Historical visible engagement is a proxy because views and exposure are unavailable.",
            "Blind preference measures editorial ranking utility, not platform traffic lift.",
            "Material Gold is not modified and is not treated as propagation Gold.",
        ],
        "generated_at": utc_now(),
    }
    _persist_report(report)
    return _public_report(report)


def multimodal_vector_media_path(benchmark_id: str, task_id: str, side: str) -> Path:
    manifest = load_multimodal_vector_manifest(benchmark_id)
    task = _task_by_id(manifest, task_id)
    side_key = str(side or "").strip().lower()
    if side_key not in {"left", "right"}:
        raise ValueError("side must be left or right")
    sample_id = str(task.get(f"{side_key}_sample_id") or "")
    sample = (manifest.get("samples") or {}).get(sample_id) or {}
    relative = str(((sample.get("media") or {}).get("video") or {}).get("path") or "")
    if not relative:
        raise FileNotFoundError(f"video path missing for blind task: {task_id}/{side_key}")
    root = ensure_data_dirs().root.resolve()
    path = (root / relative).resolve()
    allowed = (ensure_data_dirs().data_dir / "douyin_media_assets").resolve()
    if allowed not in path.parents or not path.is_file():
        raise FileNotFoundError(f"video not available for blind task: {task_id}/{side_key}")
    return path


def multimodal_vector_manifest_path(benchmark_id: str) -> Path:
    return ensure_data_dirs().root / "benchmarks" / f"{_normalize_benchmark_id(benchmark_id)}.json"


def load_multimodal_vector_manifest(benchmark_id: str) -> dict:
    path = multimodal_vector_manifest_path(benchmark_id)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    manifest = read_json(path)
    if manifest.get("contract_version") != MULTIMODAL_VECTOR_EXPERIMENT_VERSION:
        raise ValueError(f"unsupported multimodal vector manifest: {manifest.get('contract_version')}")
    expected = str(manifest.get("manifest_sha256") or "")
    actual = _canonical_digest(manifest)
    if not expected or expected != actual:
        raise ValueError("multimodal vector manifest integrity check failed")
    return manifest


def verify_multimodal_vector_manifest(benchmark_id: str, *, deep: bool = False) -> dict:
    manifest = load_multimodal_vector_manifest(benchmark_id)
    eval_ids = set(str(value) for value in manifest.get("evaluation_sample_ids") or [])
    reference_ids = set(str(value) for value in manifest.get("reference_sample_ids") or [])
    errors = []
    if eval_ids & reference_ids:
        errors.append("evaluation_reference_overlap")
    if len(eval_ids) != len(manifest.get("evaluation_sample_ids") or []):
        errors.append("duplicate_evaluation_sample")
    if deep:
        for sample_id, sample in (manifest.get("samples") or {}).items():
            for file_info in _sample_file_records(sample):
                path = ensure_data_dirs().root / str(file_info.get("path") or "")
                if not path.is_file():
                    errors.append(f"missing_media:{sample_id}:{file_info.get('path')}")
                    continue
                if _file_sha256(path) != str(file_info.get("sha256") or ""):
                    errors.append(f"media_hash_changed:{sample_id}:{file_info.get('path')}")
    return {
        "status": "verified" if not errors else "invalid",
        "passed": not errors,
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "deep": deep,
        "errors": errors,
    }


def _confirmed_material_gold_rows(asset_index: dict[str, dict[str, list[str]]]) -> list[dict]:
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT h.*, g.domain_category AS gold_domain_category,
                   g.material_type AS gold_material_type,
                   g.program_context AS gold_program_context,
                   g.presentation_style AS gold_presentation_style,
                   g.updated_at AS gold_updated_at
            FROM material_gold_annotations g
            JOIN historical_capture_samples h ON h.id = g.sample_id
            WHERE g.review_status = 'confirmed'
            ORDER BY g.updated_at, h.id
            """,
        )
    return [prepared for row in rows if (prepared := _prepared_local_row(row, asset_index))]


def _local_historical_rows(asset_index: dict[str, dict[str, list[str]]]) -> list[dict]:
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT * FROM historical_capture_samples
            WHERE COALESCE(platform_item_id, '') != ''
              AND performance_label IN ('high', 'mid', 'low')
              AND (COALESCE(normalized_reward, 0) > 0 OR COALESCE(reward_proxy, 0) > 0)
            ORDER BY account_id, platform_item_id, id
            """,
        )
    return [prepared for row in rows if (prepared := _prepared_local_row(row, asset_index))]


def _prepared_local_row(row: dict, asset_index: dict[str, dict[str, list[str]]]) -> dict | None:
    prepared = _prepare_row(row, asset_index=asset_index)
    video_paths = ((prepared.get("assets") or {}).get("paths") or {}).get("video") or []
    video = next((Path(value).resolve() for value in video_paths if Path(value).is_file()), None)
    if not video:
        return None
    prepared["_video_path"] = str(video)
    prepared["_visual_paths"] = [
        str(Path(value).resolve())
        for value in [
            *(((prepared.get("assets") or {}).get("paths") or {}).get("cover") or []),
            *(((prepared.get("assets") or {}).get("paths") or {}).get("frame") or []),
        ]
        if Path(value).is_file()
    ][:3]
    return prepared


def _match_controls(anchors: list[dict], pool: list[dict]) -> list[dict]:
    controls = []
    used_ids: set[str] = set()
    used_titles: set[str] = {_stable_title_key(row.get("title")) for row in anchors}
    anchor_platform_ids = {str(row.get("platform_item_id") or "") for row in anchors}
    for anchor in anchors:
        candidates = [
            row
            for row in pool
            if str(row.get("id") or "") not in used_ids
            and str(row.get("platform_item_id") or "") not in anchor_platform_ids
            and _stable_title_key(row.get("title")) not in used_titles
        ]
        if not candidates:
            raise ValueError(f"no leakage-safe control for Material Gold sample {anchor.get('id')}")
        control = min(candidates, key=lambda row: _control_match_key(anchor, row))
        controls.append(control)
        used_ids.add(str(control.get("id") or ""))
        used_titles.add(_stable_title_key(control.get("title")))
    return controls


def _control_match_key(anchor: dict, candidate: dict) -> tuple:
    anchor_label = str(anchor.get("performance_label") or "mid")
    desired = "low" if anchor_label == "high" else "high"
    duration_a = float(anchor.get("duration_seconds") or 0.0)
    duration_b = float(candidate.get("duration_seconds") or 0.0)
    return (
        0 if str(anchor.get("account_id") or "") == str(candidate.get("account_id") or "") else 1,
        0 if str(candidate.get("performance_label") or "") == desired else 1,
        0 if _duration_bucket(duration_a) == _duration_bucket(duration_b) else 1,
        0 if str(anchor.get("content_category") or "") == str(candidate.get("content_category") or "") else 1,
        abs(duration_a - duration_b),
        _stable_row_key(candidate),
    )


def _select_reference_rows(rows: list[dict], *, per_label: int) -> list[dict]:
    selected = []
    for label in ("high", "low"):
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            if str(row.get("performance_label") or "") == label:
                grouped[str(row.get("account_id") or "unknown")].append(row)
        for values in grouped.values():
            values.sort(key=_stable_row_key)
        accounts = sorted(grouped)
        while accounts and sum(1 for row in selected if str(row.get("performance_label") or "") == label) < per_label:
            next_accounts = []
            for account in accounts:
                if grouped[account]:
                    selected.append(grouped[account].pop(0))
                    if sum(1 for row in selected if str(row.get("performance_label") or "") == label) >= per_label:
                        break
                if grouped[account]:
                    next_accounts.append(account)
            accounts = next_accounts
    return selected


def _sample_snapshot(row: dict) -> dict:
    root = ensure_data_dirs().root.resolve()
    video = Path(str(row.get("_video_path") or "")).resolve()
    visuals = [Path(value).resolve() for value in row.get("_visual_paths") or []]
    text = historical_embedding_text(row)
    return {
        "sample_id": str(row.get("id") or ""),
        "account_id": str(row.get("account_id") or ""),
        "dataset_id": str(row.get("dataset_id") or ""),
        "platform_item_id": str(row.get("platform_item_id") or ""),
        "stable_title_key": _stable_title_key(row.get("title")),
        "title": str(row.get("title") or ""),
        "duration_seconds": round(float(row.get("duration_seconds") or 0.0), 3),
        "performance_label": str(row.get("performance_label") or ""),
        "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
        "semantic": {
            field: str(row.get(field) or "")
            for field in ["content_category", "hook_type", "slice_structure", "artist_names", "song_title", "program_name"]
        },
        "material_gold": {
            "domain_category": str(row.get("gold_domain_category") or ""),
            "material_type": str(row.get("gold_material_type") or ""),
            "program_context": str(row.get("gold_program_context") or ""),
            "presentation_style": str(row.get("gold_presentation_style") or ""),
        }
        if row.get("gold_material_type")
        else None,
        "embedding_input": {
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text_length": len(text),
            "visual_source_count": len(visuals),
        },
        "media": {
            "video": _file_record(video, root),
            "visual_sources": [_file_record(path, root) for path in visuals],
        },
    }


def _score_experiment_rows(eval_rows: list[dict], train_rows: list[dict]) -> list[dict]:
    train_basis = _prepare_history_tokens(train_rows)
    eval_basis = _prepare_history_tokens(eval_rows)
    baselines = _historical_group_baselines(train_basis)
    history_index = _history_candidate_index(train_basis)
    thresholds = _interaction_thresholds(train_basis)
    profiles = _account_ranker_profiles(train_basis, thresholds=thresholds)
    embedding_context = historical_embedding_backtest_context([*train_basis, *eval_basis])
    embedding_context["thresholds"] = thresholds
    result = []
    for row in eval_basis:
        scores, components = _historical_strategy_scores(
            row,
            train_basis,
            baselines,
            history_index=history_index,
            thresholds=thresholds,
            account_profiles=profiles,
        )
        gated_row = _v24_reliable_signal_row(row)
        signal_quality = _v24_signal_quality(row, gated_row, components)
        gated_score = _score_v24_from_components(
            components,
            row=row,
            account_profiles=profiles,
            config=RESEARCH_RANKER_V24_WEIGHT_CONFIG,
            signal_quality=signal_quality,
        )
        scores[RESEARCH_RANKER_V24_STRATEGY] = _select_v24_signal_gate_score(
            raw_score=float(scores.get(RESEARCH_RANKER_V23_STRATEGY) or 0.0),
            gated_score=gated_score,
            raw_components=components,
            gated_components=components,
            signal_quality=signal_quality,
        )
        embedding_scores, embedding_components = historical_embedding_strategy_scores(
            row,
            train_basis,
            embedding_context,
            base_score=float(scores.get(RESEARCH_RANKER_V24_STRATEGY) or 50.0),
        )
        scores.update(embedding_scores)
        result.append(
            {
                **row,
                "strategy_scores": scores,
                "component_scores": {**components, **signal_quality, **embedding_components},
            }
        )
    return result


def _pairwise_strategy_metrics(pairs: list[dict], reviews: dict[str, dict], strategy: str) -> dict:
    human_evaluable = 0
    human_matches = 0
    human_decisions = 0
    severe_errors = 0
    proxy_evaluable = 0
    proxy_matches = 0
    for pair in pairs:
        prediction = str((pair.get("predictions") or {}).get(strategy) or "tie")
        review = reviews.get(str(pair.get("task_id") or "")) or {}
        choice = str(review.get("choice") or "")
        if choice in {"left", "right"}:
            human_evaluable += 1
            if prediction in {"left", "right"}:
                human_decisions += 1
                human_matches += int(prediction == choice)
                severe_errors += int(prediction != choice and str(review.get("confidence") or "") == "high")
        proxy_choice = str(pair.get("proxy_choice") or "tie")
        if proxy_choice in {"left", "right"}:
            proxy_evaluable += 1
            proxy_matches += int(prediction == proxy_choice)
    return {
        "strategy": strategy,
        "human_evaluable_count": human_evaluable,
        "human_decision_count": human_decisions,
        "human_pairwise_accuracy": round(human_matches / max(1, human_evaluable), 4),
        "human_abstention_rate": round(1.0 - human_decisions / max(1, human_evaluable), 4),
        "high_confidence_severe_error_count": severe_errors,
        "proxy_evaluable_count": proxy_evaluable,
        "historical_proxy_pairwise_accuracy": round(proxy_matches / max(1, proxy_evaluable), 4),
        "metric_semantics": "human blind preference plus historical visible-engagement proxy; not platform views",
    }


def _experiment_gate(
    comparison: dict[str, dict],
    reviews: dict[str, dict],
    evaluation_coverage: dict,
    reference_coverage: dict,
) -> dict:
    baseline = comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
    fusion = comparison.get(TEXT_VISUAL_EMBEDDING_STRATEGY) or {}
    human_count = int(fusion.get("human_evaluable_count") or 0)
    accuracy_delta = float(fusion.get("human_pairwise_accuracy") or 0.0) - float(baseline.get("human_pairwise_accuracy") or 0.0)
    severe_delta = int(fusion.get("high_confidence_severe_error_count") or 0) - int(
        baseline.get("high_confidence_severe_error_count") or 0
    )
    coverage_ready = (
        float(evaluation_coverage.get("text_ready_rate") or 0.0) >= 0.9
        and float(evaluation_coverage.get("visual_ready_rate") or 0.0) >= 0.9
        and float(reference_coverage.get("text_ready_rate") or 0.0) >= 0.8
        and float(reference_coverage.get("visual_ready_rate") or 0.0) >= 0.8
    )
    evidence_passed = human_count >= 40 and accuracy_delta >= 0.05 and severe_delta <= 0 and coverage_ready
    return {
        "passed": False,
        "research_evidence_passed": evidence_passed,
        "status": "positive_research_signal" if evidence_passed else "research_only",
        "automatic_promotion": False,
        "human_evaluable_count": human_count,
        "required_human_evaluable_count": 40,
        "fusion_accuracy_delta_vs_v2_4": round(accuracy_delta, 4),
        "required_accuracy_delta": 0.05,
        "severe_error_delta_vs_v2_4": severe_delta,
        "coverage_ready": coverage_ready,
        "review_count": len(reviews),
        "decision": "keep_v2_4_and_continue_shadow" if not evidence_passed else "consider_larger_frozen_shadow_benchmark",
        "note": "This gate never changes production weights automatically.",
    }


def _proxy_pair_choice(left: dict, right: dict) -> str:
    delta = float(left.get("normalized_reward") or left.get("reward_proxy") or 0.0) - float(
        right.get("normalized_reward") or right.get("reward_proxy") or 0.0
    )
    return "tie" if abs(delta) < 1e-6 else "left" if delta > 0 else "right"


def _public_blind_task(task: dict, manifest: dict) -> dict:
    samples = manifest.get("samples") if isinstance(manifest.get("samples"), dict) else {}
    left = samples.get(str(task.get("left_sample_id") or "")) or {}
    right = samples.get(str(task.get("right_sample_id") or "")) or {}
    benchmark_id = str(manifest.get("benchmark_id") or "")
    task_id = str(task.get("task_id") or "")
    return {
        "task_id": task_id,
        "position": int(task_id.split("-")[-1]) if task_id.split("-")[-1].isdigit() else 0,
        "left": {
            "label": "A",
            "duration_seconds": left.get("duration_seconds") or 0,
            "media_url": f"/learning/multimodal-vector-experiment/media/{benchmark_id}/{task_id}/left",
        },
        "right": {
            "label": "B",
            "duration_seconds": right.get("duration_seconds") or 0,
            "media_url": f"/learning/multimodal-vector-experiment/media/{benchmark_id}/{task_id}/right",
        },
        "labels_hidden": True,
    }


def _public_coverage(coverage: dict) -> dict:
    return {
        key: coverage.get(key)
        for key in [
            "sample_count",
            "text_ready_count",
            "text_ready_rate",
            "visual_ready_count",
            "visual_ready_rate",
            "text_visual_ready_count",
            "model_name",
        ]
    }


def _public_report(report: dict | None) -> dict | None:
    if not report:
        return None
    return {
        key: report.get(key)
        for key in [
            "contract_version",
            "benchmark_id",
            "manifest_sha256",
            "status",
            "admission_status",
            "review_summary",
            "embedding_coverage",
            "strategy_comparison",
            "promotion_gate",
            "production_impact",
            "limitations",
            "generated_at",
        ]
    }


def _review_index(benchmark_id: str, reviewer_id: str) -> dict[str, dict]:
    with connect() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM multimodal_vector_reviews WHERE benchmark_id = ? AND reviewer_id = ? ORDER BY updated_at",
            [benchmark_id, reviewer_id],
        )
    for row in rows:
        try:
            row["reason_tags"] = json.loads(str(row.get("reason_tags_json") or "[]"))
        except json.JSONDecodeError:
            row["reason_tags"] = []
    return {str(row.get("task_id") or ""): row for row in rows}


def _task_by_id(manifest: dict, task_id: str) -> dict:
    task_key = str(task_id or "").strip()
    for task in manifest.get("tasks") or []:
        if str(task.get("task_id") or "") == task_key:
            return task
    raise KeyError(f"blind review task not found: {task_key}")


def _historical_rows_by_ids(entity_ids: list[str]) -> list[dict]:
    if not entity_ids:
        return []
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"SELECT * FROM historical_capture_samples WHERE id IN ({','.join('?' for _ in entity_ids)})",
            entity_ids,
        )
    by_id = {str(row.get("id") or ""): row for row in rows}
    return [by_id[entity_id] for entity_id in entity_ids if entity_id in by_id]


def _persist_report(report: dict) -> Path:
    root = ensure_data_dirs().root / "outputs" / "multimodal_vector_value" / str(report.get("benchmark_id") or "unknown")
    timestamp = re.sub(r"[^0-9]", "", str(report.get("generated_at") or ""))[:14] or "latest"
    path = root / f"comparison-{timestamp}.json"
    write_json(path, report)
    write_json(root / "latest.json", report)
    return path


def _latest_report(benchmark_id: str) -> dict | None:
    path = ensure_data_dirs().root / "outputs" / "multimodal_vector_value" / benchmark_id / "latest.json"
    return read_json(path) if path.is_file() else None


def _sample_file_records(sample: dict) -> list[dict]:
    media = sample.get("media") if isinstance(sample.get("media"), dict) else {}
    result = []
    if isinstance(media.get("video"), dict):
        result.append(media["video"])
    result.extend(value for value in media.get("visual_sources") or [] if isinstance(value, dict))
    return result


def _file_record(path: Path, root: Path) -> dict:
    resolved = path.resolve()
    try:
        relative = str(resolved.relative_to(root))
    except ValueError:
        raise ValueError(f"experiment media must stay inside project root: {resolved}")
    stat = resolved.stat()
    return {
        "path": relative,
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _file_sha256(resolved),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(payload: dict) -> str:
    normalized = dict(payload)
    normalized["manifest_sha256"] = ""
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_benchmark_id(value: str) -> str:
    benchmark_id = str(value or "").strip()
    if not _BENCHMARK_ID_RE.fullmatch(benchmark_id):
        raise ValueError("benchmark_id must use 3-96 letters, digits, dot, underscore, or dash")
    return benchmark_id


def _stable_row_key(row: dict) -> str:
    raw = "|".join(
        [
            str(row.get("account_id") or ""),
            str(row.get("platform_item_id") or ""),
            str(row.get("id") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable_title_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").strip().lower())[:160]


def _duration_bucket(value: float) -> str:
    if value < 30:
        return "short"
    if value < 60:
        return "medium"
    if value < 180:
        return "long"
    return "very_long"
