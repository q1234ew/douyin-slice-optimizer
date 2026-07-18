from __future__ import annotations

import json
import math
import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.material_calibration import material_gold_annotation_index
from dso.learning.material_taxonomy import (
    MATERIAL_TAXONOMY_MATCH_RELATIONS,
    MATERIAL_TYPE_TAXONOMY_SCORES,
    canonical_material_type as _canonical_material_type,
    material_type_taxonomy_relation as _material_type_taxonomy_relation,
)
from dso.learning.qwen_embeddings import (
    EMBEDDING_RESEARCH_STRATEGIES,
    TEXT_EMBEDDING_STRATEGY,
    TEXT_VISUAL_EMBEDDING_STRATEGY,
    VISUAL_EMBEDDING_STRATEGY,
    embedding_backtest_summary,
    embedding_coverage_for_scope,
    embedding_strategy_gap,
    historical_embedding_backtest_context,
    historical_embedding_strategy_scores,
)
from dso.learning.qwen_omni import omni_annotation_field_guides, qwen_omni_shadow_cache_index, refresh_omni_shadow_for_row
from dso.scoring.ranking_policy import RESEARCH_RANKER_PROMOTION_THRESHOLDS
from dso.utils import clamp, new_id, utc_now
from dso.versions import BACKTEST_VERSION, RESEARCH_LABEL_VERSION, RESEARCH_RANKER_VERSION, SCORER_VERSION


BACKTEST_STRATEGIES = {
    "current_rules",
    "semantic_baseline_v2",
    "research_ranker_v2",
    "research_ranker_v2_1",
    "research_ranker_v2_2",
    "research_ranker_v2_3",
    "research_ranker_v2_4",
    "research_ranker_v2_5_shadow",
    "research_ranker_v2_6_pool",
    "research_ranker_v2_7_material_shadow",
    "research_ranker_v2_8_material_calibrated",
    "research_ranker_v2_9_material_taxonomy",
    TEXT_EMBEDDING_STRATEGY,
    VISUAL_EMBEDDING_STRATEGY,
    TEXT_VISUAL_EMBEDDING_STRATEGY,
    "ranker_without_prototypes",
    "ranker_without_low_risk",
}
RESEARCH_RANKER_V21_STRATEGY = "research_ranker_v2_1"
RESEARCH_RANKER_V22_STRATEGY = "research_ranker_v2_2"
RESEARCH_RANKER_V23_STRATEGY = "research_ranker_v2_3"
RESEARCH_RANKER_V24_STRATEGY = "research_ranker_v2_4"
RESEARCH_RANKER_V25_SHADOW_STRATEGY = "research_ranker_v2_5_shadow"
RESEARCH_RANKER_V26_POOL_STRATEGY = "research_ranker_v2_6_pool"
RESEARCH_RANKER_V27_MATERIAL_STRATEGY = "research_ranker_v2_7_material_shadow"
RESEARCH_RANKER_V28_MATERIAL_STRATEGY = "research_ranker_v2_8_material_calibrated"
RESEARCH_RANKER_V29_TAXONOMY_STRATEGY = "research_ranker_v2_9_material_taxonomy"
RESEARCH_RANKER_V21_WEIGHT_CONFIG = {
    "name": "semantic_guardrail_v2_1",
    "semantic_weight": 1.0,
    "high_similarity_weight": 0.003,
    "low_risk_weight": 0.001,
    "prototype_weight": 0.001,
    "semantic_trust_weight": 0.001,
    "novelty_weight": 0.0,
    "bias": 0.0,
}
RESEARCH_RANKER_V22_WEIGHT_CONFIG = {
    "name": "evidence_quality_dynamic_v2_2",
    "semantic_floor_weight": 0.955,
    "semantic_strong_weight": 0.84,
    "high_similarity_weight": 0.075,
    "low_risk_weight": 0.038,
    "prototype_weight": 0.026,
    "semantic_trust_weight": 0.0,
    "novelty_weight": 0.0,
    "evidence_threshold": 0.35,
    "risk_activation_threshold": 45.0,
    "risk_high_similarity_margin": 0.72,
    "bias": 0.0,
}
RESEARCH_RANKER_V23_WEIGHT_CONFIG = {
    **RESEARCH_RANKER_V22_WEIGHT_CONFIG,
    "name": "diagnostic_diversity_v2_3",
    "account_ready_sample_threshold": 50,
    "account_high_similarity_bonus": 0.0,
    "account_low_risk_guard": 0.0,
    "account_semantic_trust_bonus": 0.0,
    "unknown_core_semantic_penalty": 0.0,
    "title_diversity_penalty": 4.0,
    "song_diversity_penalty": 1.2,
    "artist_diversity_penalty": 0.0,
    "category_diversity_penalty": 0.0,
}
RESEARCH_RANKER_V24_WEIGHT_CONFIG = {
    **RESEARCH_RANKER_V23_WEIGHT_CONFIG,
    "name": "signal_trust_gate_v2_4",
    "title_diversity_penalty": 5.5,
    "song_diversity_penalty": 1.8,
    "trusted_signal_bonus": 0.16,
    "weak_signal_penalty": 0.04,
    "semantic_fallback_floor": 0.82,
}
RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG = {
    **RESEARCH_RANKER_V24_WEIGHT_CONFIG,
    "name": "omni_shadow_calibration_v2_5",
    "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
    "strategy": RESEARCH_RANKER_V25_SHADOW_STRATEGY,
    "omni_baseline_pull_weight": 0.18,
    "omni_content_category_bonus": 0.08,
    "omni_hook_bonus": 0.10,
    "omni_slice_bonus": 0.12,
    "omni_agreement_bonus": 0.10,
    "omni_conflict_penalty": 0.12,
    "omni_min_coverage": 0.34,
    "production_status": "research_only",
}
RESEARCH_RANKER_V26_POOL_WEIGHT_CONFIG = {
    **RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG,
    "name": "omni_evidence_router_v2_6_pool",
    "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
    "strategy": RESEARCH_RANKER_V26_POOL_STRATEGY,
    "pool_k": 30,
    "max_boost": 5.0,
    "max_penalty": 1.2,
    "evidence_only_max_boost": 0.0,
    "quarantine_max_penalty": 0.5,
    "production_status": "pool_research_only",
}
RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG = {
    **RESEARCH_RANKER_V26_POOL_WEIGHT_CONFIG,
    "name": "material_type_trust_router_v2_7",
    "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
    "strategy": RESEARCH_RANKER_V27_MATERIAL_STRATEGY,
    "material_agreement_bonus": 0.42,
    "music_domain_split_bonus": 0.18,
    "program_context_bonus": 0.08,
    "material_conflict_penalty": 0.36,
    "evidence_only_max_boost": 0.55,
    "boost_enabled_max_boost": 1.25,
    "quarantine_max_penalty": 0.45,
    "low_risk_boost_multiplier": 0.35,
    "production_status": "material_research_only",
}
RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG = {
    **RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG,
    "name": "material_gold_trust_router_v2_8",
    "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
    "strategy": RESEARCH_RANKER_V28_MATERIAL_STRATEGY,
    "trusted_min_gold_samples": 12,
    "account_min_gold_samples": 8,
    "trusted_min_accuracy": 0.75,
    "blocked_max_accuracy": 0.55,
    "trusted_max_boost": 0.85,
    "neutral_max_boost": 0.18,
    "trusted_max_penalty": 0.35,
    "neutral_max_penalty": 0.08,
    "production_status": "material_calibration_research_only",
}
RESEARCH_RANKER_V29_TAXONOMY_WEIGHT_CONFIG = {
    **RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG,
    "name": "material_canonical_taxonomy_router_v2_9",
    "strategy": RESEARCH_RANKER_V29_TAXONOMY_STRATEGY,
    "calibration_accuracy_mode": "canonical_material_type",
    "production_status": "material_taxonomy_research_only",
}
OMNI_SHADOW_ABLATION_STRATEGIES = [
    "v25_without_content_category",
    "v25_without_hook_type",
    "v25_without_slice_structure",
    "v25_without_baseline_pull",
    "v25_conflict_penalty_only",
]
OMNI_MATERIAL_ABLATION_STRATEGIES = [
    "v27_without_material_type",
    "v27_material_only",
]
RANKER_TUNING_CANDIDATES = [
    RESEARCH_RANKER_V21_WEIGHT_CONFIG,
    {
        "name": "semantic_plus_high",
        "semantic_weight": 0.98,
        "high_similarity_weight": 0.018,
        "low_risk_weight": 0.003,
        "prototype_weight": 0.004,
        "semantic_trust_weight": 0.003,
        "novelty_weight": 0.0,
        "bias": 0.0,
    },
    {
        "name": "semantic_low_risk_guard",
        "semantic_weight": 1.02,
        "high_similarity_weight": 0.012,
        "low_risk_weight": 0.008,
        "prototype_weight": 0.002,
        "semantic_trust_weight": 0.002,
        "novelty_weight": 0.0,
        "bias": 0.0,
    },
    {
        "name": "evidence_balanced",
        "semantic_weight": 0.9,
        "high_similarity_weight": 0.08,
        "low_risk_weight": 0.025,
        "prototype_weight": 0.016,
        "semantic_trust_weight": 0.01,
        "novelty_weight": 0.004,
        "bias": 0.0,
    },
]
RANKER_TUNING_CANDIDATES_V22 = [
    RESEARCH_RANKER_V22_WEIGHT_CONFIG,
    {
        **RESEARCH_RANKER_V22_WEIGHT_CONFIG,
        "name": "dynamic_high_precision",
        "semantic_floor_weight": 0.985,
        "semantic_strong_weight": 0.92,
        "high_similarity_weight": 0.036,
        "low_risk_weight": 0.062,
        "prototype_weight": 0.012,
    },
    {
        **RESEARCH_RANKER_V22_WEIGHT_CONFIG,
        "name": "dynamic_high_recall",
        "semantic_floor_weight": 0.955,
        "semantic_strong_weight": 0.84,
        "high_similarity_weight": 0.075,
        "low_risk_weight": 0.038,
        "prototype_weight": 0.026,
    },
    {
        **RESEARCH_RANKER_V22_WEIGHT_CONFIG,
        "name": "semantic_guarded_gap",
        "semantic_floor_weight": 0.992,
        "semantic_strong_weight": 0.94,
        "high_similarity_weight": 0.028,
        "low_risk_weight": 0.032,
        "prototype_weight": 0.008,
        "semantic_trust_weight": 0.01,
    },
]
RANKER_TUNING_CANDIDATES_V23 = [
    RESEARCH_RANKER_V23_WEIGHT_CONFIG,
    {
        **RESEARCH_RANKER_V23_WEIGHT_CONFIG,
        "name": "diagnostic_diversity_light",
        "title_diversity_penalty": 2.0,
        "song_diversity_penalty": 0.8,
        "artist_diversity_penalty": 0.0,
        "category_diversity_penalty": 0.0,
    },
    {
        **RESEARCH_RANKER_V23_WEIGHT_CONFIG,
        "name": "diagnostic_diversity_strict",
        "title_diversity_penalty": 5.5,
        "song_diversity_penalty": 1.8,
        "artist_diversity_penalty": 0.0,
        "category_diversity_penalty": 0.0,
    },
]
RANKER_TUNING_CANDIDATES_V24 = [
    RESEARCH_RANKER_V24_WEIGHT_CONFIG,
    {
        **RESEARCH_RANKER_V24_WEIGHT_CONFIG,
        "name": "signal_trust_gate_balanced",
        "title_diversity_penalty": 4.5,
        "song_diversity_penalty": 1.4,
        "trusted_signal_bonus": 0.12,
    },
    {
        **RESEARCH_RANKER_V24_WEIGHT_CONFIG,
        "name": "signal_trust_gate_strict",
        "title_diversity_penalty": 6.5,
        "song_diversity_penalty": 2.2,
        "trusted_signal_bonus": 0.18,
        "weak_signal_penalty": 0.06,
    },
]


def backtest_rule_ranker(
    account_id: str | None = None,
    *,
    k: int = 10,
    strategy: str = RESEARCH_RANKER_V24_STRATEGY,
    holdout_policy: str = "time",
    label_version: str | None = None,
    benchmark_context: dict[str, Any] | None = None,
) -> dict:
    k = max(1, int(k or 10))
    selected_strategy = _normalize_strategy(strategy)
    rows = _backtest_rows(account_id)
    source = "training_samples"
    historical_dataset: dict[str, Any] = {}
    if not rows:
        historical_dataset = _historical_backtest_dataset(
            account_id,
            strategy=selected_strategy,
            holdout_policy=holdout_policy,
            label_version=label_version,
            k=k,
        )
        rows = historical_dataset.get("rows") or []
        source = "historical_capture_samples"
    if not rows:
        report = _report_payload(
            account_id,
            status="insufficient_samples",
            rows=[],
            k=k,
            query_extra={
                "strategy": selected_strategy,
                "holdout_policy": holdout_policy,
                "label_version": label_version or "any",
            },
        )
        _store_report(report)
        return report
    ranked_by_score = _rank_rows(rows)
    ranked_by_reward = _rank_rows(rows, primary="normalized_reward", fallback="reward_proxy")
    strategy_comparison = historical_dataset.get("strategy_comparison") or {}
    per_account_metrics = historical_dataset.get("per_account_metrics") or []
    component_ablation = historical_dataset.get("component_ablation") or {}
    promotion_gate = historical_dataset.get("promotion_gate") or _promotion_gate({}, [])
    metrics = {
        "sample_count": len(rows),
        "k": k,
        "strategy": selected_strategy,
        "ndcg_at_k": _ndcg_at_k(ranked_by_score, k),
        "topk_hit_rate": _topk_hit_rate(ranked_by_score, ranked_by_reward, k),
        "topk_lift_vs_random": _topk_lift_vs_random(ranked_by_score, rows, k),
        "high_interaction_hit_rate": _high_interaction_hit_rate(ranked_by_score, rows, k),
        "low_interaction_avoidance_rate": _low_interaction_avoidance_rate(ranked_by_score, rows, k),
        "calibration_mae": _calibration_mae(rows),
        "closed_loop_rate": 0.0
        if source == "historical_capture_samples"
        else round(min(1.0, len(rows) / max(1, _candidate_count(account_id))), 4),
        "low_exposure_uncertain_rate": _low_exposure_uncertain_rate(rows),
        "sample_source": source,
        "holdout_policy": historical_dataset.get("holdout_policy_text") or _holdout_policy(source, holdout_policy),
        "holdout_policy_key": historical_dataset.get("holdout_policy_key") or holdout_policy,
        "metric_basis": _metric_basis(source),
        "label_counts": _interaction_label_counts(rows),
        "research_label_version": historical_dataset.get("label_version") or label_version or RESEARCH_LABEL_VERSION,
        "research_ranker_version": RESEARCH_RANKER_VERSION,
        "scorer_version": SCORER_VERSION,
        "risk_note": "离线排序研究报告，不代表发布预测、流量预测或播放量预测。",
        "benchmark_manifest": dict(benchmark_context or {}),
        "strategy_comparison": strategy_comparison,
        "per_account_metrics": per_account_metrics,
        "component_ablation": component_ablation,
        "promotion_gate": promotion_gate,
        "weight_config": historical_dataset.get("weight_config") or (
            _weight_config_for_strategy(selected_strategy)
        ),
        "baseline_gap": historical_dataset.get("baseline_gap") or _baseline_gap(strategy_comparison, selected_strategy),
        "semantic_gap_analysis": historical_dataset.get("semantic_gap_analysis") or _semantic_gap_analysis(strategy_comparison, selected_strategy),
        "diagnostic_samples": historical_dataset.get("diagnostic_samples") or {},
        "diversity_summary": historical_dataset.get("diversity_summary") or _diversity_summary(rows, selected_strategy, k=k),
        "leakage_guard_summary": historical_dataset.get("leakage_guard_summary") or {},
        "next_calibration_queue": historical_dataset.get("next_calibration_queue") or [],
        "calibration_summary": historical_dataset.get("calibration_summary")
        or _calibration_summary(strategy_comparison, promotion_gate, selected_strategy),
        "embedding_coverage": historical_dataset.get("embedding_coverage") or embedding_coverage_for_scope(account_id=account_id),
        "embedding_evidence_summary": historical_dataset.get("embedding_evidence_summary") or {},
        "embedding_strategy_gap": historical_dataset.get("embedding_strategy_gap") or embedding_strategy_gap(strategy_comparison, selected_strategy=selected_strategy),
        "omni_shadow_summary": historical_dataset.get("omni_shadow_summary") or {},
        "omni_shadow_ablation": historical_dataset.get("omni_shadow_ablation") or {},
        "omni_shadow_account_metrics": historical_dataset.get("omni_shadow_account_metrics") or [],
        "omni_trust_profiles": historical_dataset.get("omni_trust_profiles") or [],
        "omni_pool_report": historical_dataset.get("omni_pool_report") or {},
        "omni_pool_gate": historical_dataset.get("omni_pool_gate") or {},
        "omni_account_pool_gates": historical_dataset.get("omni_account_pool_gates") or [],
        "omni_account_pool_summary": historical_dataset.get("omni_account_pool_summary") or {},
        "omni_material_report": historical_dataset.get("omni_material_report") or {},
        "omni_material_gate": historical_dataset.get("omni_material_gate") or {},
        "omni_material_gold_set_queue": historical_dataset.get("omni_material_gold_set_queue") or [],
        "omni_material_calibration": historical_dataset.get("omni_material_calibration") or {},
        "omni_material_calibration_holdout": historical_dataset.get("omni_material_calibration_holdout") or {},
        "omni_material_gold_split": historical_dataset.get("omni_material_gold_split") or {},
        "omni_material_router_profiles": historical_dataset.get("omni_material_router_profiles") or [],
        "omni_material_taxonomy_router_profiles": historical_dataset.get("omni_material_taxonomy_router_profiles") or [],
        "omni_material_v28_report": historical_dataset.get("omni_material_v28_report") or {},
        "omni_material_v28_gate": historical_dataset.get("omni_material_v28_gate") or {},
        "omni_material_v29_report": historical_dataset.get("omni_material_v29_report") or {},
        "omni_material_v29_gate": historical_dataset.get("omni_material_v29_gate") or {},
    }
    status = "ready" if len(rows) >= (30 if source == "historical_capture_samples" else 3) else "low_confidence"
    if source == "historical_capture_samples" and not promotion_gate.get("passed"):
        status = "research_ready" if len(rows) >= 30 else status
    report = _report_payload(
        account_id,
        status=status,
        rows=ranked_by_score[:k],
        k=k,
        metrics=metrics,
        query_extra={
            "strategy": selected_strategy,
            "holdout_policy": holdout_policy,
            "label_version": label_version or "any",
        },
    )
    _store_report(report)
    return report


def list_backtest_reports(account_id: str | None = None, limit: int = 10, *, compact: bool = False) -> dict:
    clauses = []
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit or 10)))
    with connect() as conn:
        rows = fetch_all(conn, f"SELECT * FROM backtest_reports{where} ORDER BY created_at DESC LIMIT ?", params)
    for row in rows:
        try:
            payload = json.loads(row.pop("metrics_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        metrics = payload.get("metrics") or {}
        row["metrics"] = _compact_backtest_metrics(metrics) if compact else metrics
        row["top_rows"] = [] if compact else payload.get("top_rows") or []
        row["contract_version"] = BACKTEST_VERSION
        row["generated_at"] = row.get("created_at")
    return {
        "contract_version": BACKTEST_VERSION,
        "account_id": account_id or "all",
        "count": len(rows),
        "reports": rows,
    }


def _compact_backtest_metrics(metrics: dict) -> dict:
    keys = [
        "sample_count",
        "k",
        "strategy",
        "ndcg_at_k",
        "topk_hit_rate",
        "topk_lift_vs_random",
        "high_interaction_hit_rate",
        "low_interaction_avoidance_rate",
        "calibration_mae",
        "closed_loop_rate",
        "low_exposure_uncertain_rate",
        "sample_source",
        "holdout_policy",
        "holdout_policy_key",
        "research_label_version",
        "research_ranker_version",
        "scorer_version",
        "embedding_strategy_gap",
        "weight_config",
        "baseline_gap",
        "calibration_summary",
        "semantic_gap_analysis",
        "diversity_summary",
        "strategy_comparison",
        "omni_account_pool_gates",
        "promotion_gate",
    ]
    result = {key: metrics.get(key) for key in keys if key in metrics}
    diagnostics = metrics.get("diagnostic_samples")
    if isinstance(diagnostics, dict):
        result["diagnostic_samples"] = {
            key: value[:6] if isinstance(value, list) else value
            for key, value in diagnostics.items()
        }
    return result


def run_ranker_tuning(
    account_id: str | None = None,
    *,
    k: int = 10,
    holdout_policy: str = "time",
    max_trials: int = 12,
    label_version: str | None = None,
) -> dict:
    k = max(1, int(k or 10))
    dataset = _historical_backtest_dataset(
        account_id,
        strategy=RESEARCH_RANKER_V24_STRATEGY,
        holdout_policy=holdout_policy,
        label_version=label_version,
        k=k,
    )
    rows = dataset.get("rows") or []
    if not rows:
        return {
            "contract_version": BACKTEST_VERSION,
            "status": "insufficient_samples",
            "account_id": account_id or "all",
            "strategy": RESEARCH_RANKER_V24_STRATEGY,
            "trials": [],
            "best": None,
            "generated_at": utc_now(),
        }
    trials = []
    for config in RANKER_TUNING_CANDIDATES_V24[: max(1, int(max_trials or 1))]:
        trial_rows = _rows_with_v24_config(rows, config)
        metrics = _strategy_metrics(trial_rows, RESEARCH_RANKER_V24_STRATEGY, k=k)
        per_account = _per_account_metrics(trial_rows, RESEARCH_RANKER_V24_STRATEGY)
        gate = _promotion_gate(
            {RESEARCH_RANKER_V24_STRATEGY: metrics},
            per_account,
            strategy=RESEARCH_RANKER_V24_STRATEGY,
        )
        trials.append(
            {
                "weight_config": config,
                "metrics": metrics,
                "per_account_metrics": per_account,
                "promotion_gate": gate,
                "objective_score": _tuning_objective(metrics, gate),
            }
        )
    trials.sort(key=lambda row: float(row.get("objective_score") or 0), reverse=True)
    best = trials[0] if trials else None
    strategy_comparison = dict(dataset.get("strategy_comparison") or {})
    if best:
        strategy_comparison[RESEARCH_RANKER_V24_STRATEGY] = best["metrics"]
    return {
        "contract_version": BACKTEST_VERSION,
        "status": "ready" if best else "empty",
        "account_id": account_id or "all",
        "strategy": RESEARCH_RANKER_V24_STRATEGY,
        "holdout_policy": dataset.get("holdout_policy_key") or holdout_policy,
        "sample_count": len(rows),
        "k": k,
        "trials": trials,
        "best": best,
        "best_weight_config": (best or {}).get("weight_config") if best else None,
        "strategy_comparison": strategy_comparison,
        "per_account_metrics": (best or {}).get("per_account_metrics") if best else [],
        "promotion_gate": (best or {}).get("promotion_gate") if best else _promotion_gate({}, [], strategy=RESEARCH_RANKER_V24_STRATEGY),
        "baseline_gap": _baseline_gap(strategy_comparison, RESEARCH_RANKER_V24_STRATEGY),
        "generated_at": utc_now(),
    }


def semantic_feature_experiment(
    account_id: str | None = None,
    *,
    k: int = 10,
    holdout_policy: str = "time",
    label_version: str | None = None,
    include_field_masks: bool = True,
) -> dict:
    k = max(1, int(k or 10))
    base = _historical_backtest_dataset(
        account_id,
        strategy=RESEARCH_RANKER_V24_STRATEGY,
        holdout_policy=holdout_policy,
        label_version=label_version,
        k=k,
    )
    rows = base.get("rows") or []
    if not rows:
        return {
            "contract_version": BACKTEST_VERSION,
            "status": "insufficient_samples",
            "account_id": account_id or "all",
            "strategy": RESEARCH_RANKER_V24_STRATEGY,
            "sample_count": 0,
            "generated_at": utc_now(),
        }
    base_metrics = _strategy_metrics(rows, RESEARCH_RANKER_V24_STRATEGY, k=k)
    masks = [
        ("mask_content_category", ["content_category"]),
        ("mask_slice_structure", ["slice_structure"]),
        ("mask_artist_names", ["artist_names"]),
        ("mask_song_title", ["song_title"]),
        ("mask_artist_and_song", ["artist_names", "song_title", "original_sound_owner", "entity_signal"]),
        ("mask_core_semantics", ["content_category", "hook_type", "slice_structure"]),
        ("mask_all_semantic_fields", ["content_category", "hook_type", "slice_structure", "artist_names", "song_title", "original_sound_owner", "entity_signal"]),
    ]
    mask_results = []
    if include_field_masks:
        for name, fields in masks:
            masked = _historical_backtest_dataset(
                account_id,
                strategy=RESEARCH_RANKER_V24_STRATEGY,
                holdout_policy=holdout_policy,
                label_version=label_version,
                k=k,
                field_mask=fields,
            )
            metrics = (masked.get("strategy_comparison") or {}).get(RESEARCH_RANKER_V24_STRATEGY) or {}
            mask_results.append(
                {
                    "name": name,
                    "fields": fields,
                    "metrics": metrics,
                    "lift_delta_vs_full": round(
                        float(metrics.get("topk_lift_vs_random") or 0.0)
                        - float(base_metrics.get("topk_lift_vs_random") or 0.0),
                        4,
                    ),
                }
            )
    return {
        "contract_version": BACKTEST_VERSION,
        "status": "ready",
        "account_id": account_id or "all",
        "strategy": RESEARCH_RANKER_V24_STRATEGY,
        "sample_count": len(rows),
        "k": k,
        "holdout_policy": base.get("holdout_policy_key") or holdout_policy,
        "split_summary": base.get("split_summary") or {},
        "coverage": _semantic_feature_coverage(rows),
        "base_metrics": base_metrics,
        "strategy_comparison": base.get("strategy_comparison") or {},
        "field_mask_ablation": mask_results,
        "diagnosis": _semantic_feature_diagnosis(base_metrics, mask_results, rows),
        "generated_at": utc_now(),
    }


def _backtest_rows(account_id: str | None) -> list[dict]:
    query = """
        SELECT ts.id AS training_sample_id, ts.candidate_segment_id, ts.reward_proxy, ts.normalized_reward,
               ts.label_window, ts.sample_source, ms.views, ms.impressions, ms.uncertainty,
               c.music_slice_type, c.duration_seconds, s.final_score, s.rights_risk_score, s.low_originality_score,
               v.account_id
        FROM training_samples ts
        JOIN metric_snapshots ms ON ms.id = ts.metric_snapshot_id
        JOIN candidate_segments c ON c.id = ts.candidate_segment_id
        JOIN source_videos v ON v.id = c.source_video_id
        JOIN slice_scores s ON s.candidate_segment_id = c.id
        WHERE ts.sample_source != 'mock'
    """
    params: list[Any] = []
    if account_id:
        query += " AND v.account_id = ?"
        params.append(account_id)
    with connect() as conn:
        return fetch_all(conn, query, params)


def _normalize_strategy(strategy: str | None) -> str:
    value = str(strategy or RESEARCH_RANKER_V24_STRATEGY).strip()
    return value if value in BACKTEST_STRATEGIES else RESEARCH_RANKER_V24_STRATEGY


def _historical_backtest_dataset(
    account_id: str | None,
    *,
    strategy: str,
    holdout_policy: str,
    label_version: str | None,
    k: int = 10,
    field_mask: list[str] | None = None,
) -> dict:
    all_rows = _historical_rows(account_id, label_version=label_version)
    if not all_rows:
        return {
            "rows": [],
            "strategy_comparison": {},
            "per_account_metrics": [],
            "component_ablation": {},
            "promotion_gate": _promotion_gate({}, []),
            "holdout_policy_key": holdout_policy,
            "holdout_policy_text": _holdout_policy("historical_capture_samples", holdout_policy),
            "label_version": label_version or RESEARCH_LABEL_VERSION,
        }
    omni_cache = qwen_omni_shadow_cache_index()
    all_rows = _attach_omni_shadow_cache(all_rows, omni_cache)
    all_rows = _attach_material_gold_annotations(all_rows, material_gold_annotation_index())
    material_gold_split = _material_gold_calibration_split(all_rows)
    material_calibration_rows = material_gold_split["calibration_rows"]
    material_audit_rows = material_gold_split["audit_rows"]
    material_calibration_ids = {
        str(row.get("id") or row.get("training_sample_id") or "")
        for row in material_calibration_rows
        if row.get("id") or row.get("training_sample_id")
    }
    train_rows, eval_rows, actual_policy, split_summary = _historical_holdout_split(all_rows, holdout_policy)
    if not eval_rows:
        eval_rows = all_rows
    eval_count_before_gold_exclusion = len(eval_rows)
    if material_calibration_ids:
        eval_rows = [
            row
            for row in eval_rows
            if str(row.get("id") or row.get("training_sample_id") or "") not in material_calibration_ids
        ]
    if not eval_rows:
        eval_rows = [
            row
            for row in all_rows
            if str(row.get("id") or row.get("training_sample_id") or "") not in material_calibration_ids
        ]
    train_rows, leakage_summary = _apply_leakage_guard(train_rows or all_rows, eval_rows)
    if field_mask:
        train_rows = _masked_history_rows(train_rows, field_mask)
        eval_rows = _masked_history_rows(eval_rows, field_mask)
    material_gold_split_summary = {
        **material_gold_split["summary"],
        "performance_eval_count_before_calibration_exclusion": eval_count_before_gold_exclusion,
        "performance_eval_count_after_calibration_exclusion": len(eval_rows),
        "calibration_rows_excluded_from_performance_eval": eval_count_before_gold_exclusion - len(eval_rows),
    }
    split_summary = {
        **split_summary,
        "train_count_after_leakage_guard": len(train_rows),
        "material_gold_split": material_gold_split_summary,
    }
    train_basis = _prepare_history_tokens(train_rows or all_rows)
    history_index = _history_candidate_index(train_basis)
    eval_basis = _prepare_history_tokens(eval_rows)
    embedding_context = historical_embedding_backtest_context(train_basis + eval_basis)
    baselines = _historical_group_baselines(train_basis)
    omni_baselines = _omni_shadow_group_baselines(train_basis)
    omni_router_profiles = _omni_router_profiles_from_training(train_basis, omni_baselines)
    material_router_profiles = _material_gold_router_profiles(
        material_calibration_rows,
        account_ids={str(row.get("account_id") or "unknown") for row in all_rows},
    )
    material_taxonomy_router_profiles = _material_gold_router_profiles(
        material_calibration_rows,
        account_ids={str(row.get("account_id") or "unknown") for row in all_rows},
        accuracy_mode="canonical_material_type",
    )
    interaction_thresholds = _interaction_thresholds(train_basis)
    embedding_context["thresholds"] = interaction_thresholds
    account_profiles = _account_ranker_profiles(train_basis, thresholds=interaction_thresholds)
    scored = []
    for row in eval_basis:
        reward = float(row.get("normalized_reward") or row.get("reward_proxy") or 0)
        strategy_scores, component_scores = _historical_strategy_scores(
            row,
            train_basis,
            baselines,
            history_index=history_index,
            thresholds=interaction_thresholds,
            account_profiles=account_profiles,
        )
        v24_row = _v24_reliable_signal_row(row)
        v24_signal_quality = _v24_signal_quality(row, v24_row, component_scores)
        gated_v24_score = _score_v24_from_components(
            component_scores,
            row=row,
            account_profiles=account_profiles,
            config=RESEARCH_RANKER_V24_WEIGHT_CONFIG,
            signal_quality=v24_signal_quality,
        )
        strategy_scores[RESEARCH_RANKER_V24_STRATEGY] = _select_v24_signal_gate_score(
            raw_score=float(strategy_scores.get(RESEARCH_RANKER_V23_STRATEGY) or 0.0),
            gated_score=gated_v24_score,
            raw_components=component_scores,
            gated_components=component_scores,
            signal_quality=v24_signal_quality,
        )
        v25_omni_components = _v25_omni_shadow_components(row, omni_baselines)
        v25_score = _score_v25_shadow(
            float(strategy_scores.get(RESEARCH_RANKER_V24_STRATEGY) or 0.0),
            component_scores,
            v25_omni_components,
            config=RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG,
        )
        strategy_scores[RESEARCH_RANKER_V25_SHADOW_STRATEGY] = v25_score
        v26_pool_components = _v26_omni_pool_components(row, v25_omni_components, omni_router_profiles)
        v26_score = _score_v26_pool(
            float(strategy_scores.get(RESEARCH_RANKER_V24_STRATEGY) or 0.0),
            v25_score,
            component_scores,
            v25_omni_components,
            v26_pool_components,
            config=RESEARCH_RANKER_V26_POOL_WEIGHT_CONFIG,
        )
        strategy_scores[RESEARCH_RANKER_V26_POOL_STRATEGY] = v26_score
        v27_material_components = _v27_material_components(row, v25_omni_components, v26_pool_components)
        v27_base_score = max(float(strategy_scores.get(RESEARCH_RANKER_V24_STRATEGY) or 0.0), float(v26_score or 0.0))
        v27_score = _score_v27_material(
            v27_base_score,
            component_scores,
            v25_omni_components,
            v26_pool_components,
            v27_material_components,
            config=RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG,
        )
        strategy_scores[RESEARCH_RANKER_V27_MATERIAL_STRATEGY] = v27_score
        v28_material_components = _v28_material_components(
            row,
            v27_material_components,
            material_router_profiles,
        )
        v28_score = _score_v28_material(
            float(strategy_scores.get(RESEARCH_RANKER_V24_STRATEGY) or 0.0),
            v27_score,
            v28_material_components,
            config=RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG,
        )
        strategy_scores[RESEARCH_RANKER_V28_MATERIAL_STRATEGY] = v28_score
        v29_material_components = _v29_material_components(
            row,
            v27_material_components,
            material_taxonomy_router_profiles,
        )
        v29_score = _score_v29_material(
            float(strategy_scores.get(RESEARCH_RANKER_V24_STRATEGY) or 0.0),
            v27_score,
            v29_material_components,
            config=RESEARCH_RANKER_V29_TAXONOMY_WEIGHT_CONFIG,
        )
        strategy_scores[RESEARCH_RANKER_V29_TAXONOMY_STRATEGY] = v29_score
        strategy_scores.update(
            _v27_material_ablation_scores(
                v27_base_score,
                component_scores,
                v25_omni_components,
                v26_pool_components,
                v27_material_components,
            )
        )
        v25_ablation_scores = _v25_shadow_ablation_scores(
            row,
            omni_baselines,
            component_scores,
            base_v24_score=float(strategy_scores.get(RESEARCH_RANKER_V24_STRATEGY) or 0.0),
        )
        strategy_scores.update(v25_ablation_scores)
        embedding_scores, embedding_components = historical_embedding_strategy_scores(
            row,
            train_basis,
            embedding_context,
            base_score=float(strategy_scores.get(RESEARCH_RANKER_V24_STRATEGY) or 50.0),
        )
        strategy_scores.update(embedding_scores)
        component_scores = {
            **component_scores,
            **v24_signal_quality,
            **v25_omni_components,
            **v26_pool_components,
            **v27_material_components,
            **v28_material_components,
            **v29_material_components,
            **embedding_components,
            "v24_account_baseline_position": float(component_scores.get("account_baseline_position") or 0.0),
            "v24_high_similarity": float(component_scores.get("high_similarity") or 0.0),
            "v24_low_interaction_risk": float(component_scores.get("low_interaction_risk") or 0.0),
            "v24_prototype_fit": float(component_scores.get("prototype_fit") or 0.0),
        }
        predicted = float(strategy_scores.get(strategy) or strategy_scores["research_ranker_v2"])
        scored.append(
            {
                "training_sample_id": row.get("id") or "",
                "candidate_segment_id": row.get("platform_item_id") or row.get("sample_key") or row.get("id") or "",
                "reward_proxy": float(row.get("reward_proxy") or 0),
                "normalized_reward": reward,
                "label_window": "lifetime/current_visible",
                "sample_source": "historical_capture_samples",
                "views": int(row.get("views") or 0),
                "impressions": 0,
                "uncertainty": 1.0 if len(all_rows) < 300 else 0.35,
                "music_slice_type": row.get("content_category") or row.get("hook_type") or "unknown",
                "duration_seconds": float(row.get("duration_seconds") or 0),
                "final_score": predicted,
                "strategy_scores": strategy_scores,
                "component_scores": component_scores,
                "v24_component_scores": component_scores,
                "rights_risk_score": 0,
                "low_originality_score": 0,
                "account_id": row.get("account_id") or "",
                "title": row.get("title") or "",
                "platform_item_id": row.get("platform_item_id") or "",
                "performance_label": row.get("performance_label") or "",
                "content_category": row.get("content_category") or "",
                "hook_type": row.get("hook_type") or "",
                "slice_structure": row.get("slice_structure") or "",
                "structure_confidence": row.get("structure_confidence") or "",
                "structure_evidence": row.get("structure_evidence") or "",
                "structure_unknown_reason": row.get("structure_unknown_reason") or "",
                "program_name": row.get("program_name") or "",
                "artist_names": row.get("artist_names") or "",
                "song_title": row.get("song_title") or "",
                "original_sound_owner": row.get("original_sound_owner") or "",
                "is_original_sound": bool(row.get("is_original_sound")),
                "entity_signal": row.get("entity_signal") or "",
                "tags": row.get("tags") or "",
                "classification_confidence": row.get("classification_confidence") or "",
                "semantic_unknown_reason": row.get("semantic_unknown_reason") or "",
                "semantic_feature_version": row.get("semantic_feature_version") or "",
                "omni_shadow": row.get("_omni_shadow") or {},
                "omni_router_profile": omni_router_profiles.get(str(row.get("account_id") or "")) or {},
                "material_router_profile": material_router_profiles.get(str(row.get("account_id") or "")) or material_router_profiles.get("__global__") or {},
                "material_taxonomy_router_profile": material_taxonomy_router_profiles.get(str(row.get("account_id") or "")) or material_taxonomy_router_profiles.get("__global__") or {},
                "material_gold_annotation": row.get("_material_gold") or {},
                "dataset_id": row.get("dataset_id") or "",
                "platform_url": row.get("platform_url") or "",
                "research_label_version": row.get("research_label_version") or RESEARCH_LABEL_VERSION,
                "published_at": row.get("published_at") or "",
                "holdout_policy": actual_policy,
            }
        )
    scored = _apply_v23_diversity(scored, config=RESEARCH_RANKER_V23_WEIGHT_CONFIG)
    scored = _apply_v24_diversity(scored, config=RESEARCH_RANKER_V24_WEIGHT_CONFIG)
    scored = _apply_v25_shadow_diversity(scored, config=RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG)
    scored = _apply_v26_pool_diversity(scored, config=RESEARCH_RANKER_V26_POOL_WEIGHT_CONFIG)
    scored = _apply_v27_material_diversity(scored, config=RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG)
    scored = _apply_v28_material_diversity(scored, config=RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG)
    scored = _apply_v29_material_diversity(scored, config=RESEARCH_RANKER_V29_TAXONOMY_WEIGHT_CONFIG)
    if strategy == RESEARCH_RANKER_V23_STRATEGY:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(RESEARCH_RANKER_V23_STRATEGY, row.get("final_score") or 0.0))
    if strategy == RESEARCH_RANKER_V24_STRATEGY:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(RESEARCH_RANKER_V24_STRATEGY, row.get("final_score") or 0.0))
    if strategy == RESEARCH_RANKER_V25_SHADOW_STRATEGY:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(RESEARCH_RANKER_V25_SHADOW_STRATEGY, row.get("final_score") or 0.0))
    if strategy == RESEARCH_RANKER_V26_POOL_STRATEGY:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(RESEARCH_RANKER_V26_POOL_STRATEGY, row.get("final_score") or 0.0))
    if strategy == RESEARCH_RANKER_V27_MATERIAL_STRATEGY:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(RESEARCH_RANKER_V27_MATERIAL_STRATEGY, row.get("final_score") or 0.0))
    if strategy == RESEARCH_RANKER_V28_MATERIAL_STRATEGY:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(RESEARCH_RANKER_V28_MATERIAL_STRATEGY, row.get("final_score") or 0.0))
    if strategy == RESEARCH_RANKER_V29_TAXONOMY_STRATEGY:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(RESEARCH_RANKER_V29_TAXONOMY_STRATEGY, row.get("final_score") or 0.0))
    if strategy in EMBEDDING_RESEARCH_STRATEGIES:
        for row in scored:
            scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
            row["final_score"] = float(scores.get(strategy, row.get("final_score") or 0.0))
    strategy_comparison = {
        name: _strategy_metrics(scored, name, k=k)
        for name in [
            "current_rules",
            "semantic_baseline_v2",
            "research_ranker_v2",
            RESEARCH_RANKER_V21_STRATEGY,
            RESEARCH_RANKER_V22_STRATEGY,
            RESEARCH_RANKER_V23_STRATEGY,
            RESEARCH_RANKER_V24_STRATEGY,
            RESEARCH_RANKER_V25_SHADOW_STRATEGY,
            RESEARCH_RANKER_V26_POOL_STRATEGY,
            RESEARCH_RANKER_V27_MATERIAL_STRATEGY,
            RESEARCH_RANKER_V28_MATERIAL_STRATEGY,
            RESEARCH_RANKER_V29_TAXONOMY_STRATEGY,
            *OMNI_MATERIAL_ABLATION_STRATEGIES,
            TEXT_EMBEDDING_STRATEGY,
            VISUAL_EMBEDDING_STRATEGY,
            TEXT_VISUAL_EMBEDDING_STRATEGY,
            "ranker_without_prototypes",
            "ranker_without_low_risk",
        ]
    }
    per_account = _per_account_metrics(scored, strategy)
    material_full_quality = _material_gold_quality_report(all_rows, scope="full_gold_set")
    material_audit_quality = _material_gold_quality_report(material_audit_rows, scope="audit_holdout")
    promotion_gate = _promotion_gate(strategy_comparison, per_account, strategy=strategy)
    if strategy in {RESEARCH_RANKER_V28_MATERIAL_STRATEGY, RESEARCH_RANKER_V29_TAXONOMY_STRATEGY}:
        ranking_gate_passed = bool(promotion_gate.get("material_calibration_gate_passed"))
        quality_gate_passed = bool(material_audit_quality.get("quality_gate_passed"))
        if strategy == RESEARCH_RANKER_V29_TAXONOMY_STRATEGY:
            quality_gate_passed = bool(material_audit_quality.get("canonical_quality_gate_passed"))
        promotion_gate["ranking_gate_passed"] = ranking_gate_passed
        promotion_gate["gold_audit_quality_gate_passed"] = quality_gate_passed
        promotion_gate["material_calibration_gate_passed"] = ranking_gate_passed and quality_gate_passed
    component_ablation = {
        "ranker_without_prototypes": strategy_comparison["ranker_without_prototypes"],
        "ranker_without_low_risk": strategy_comparison["ranker_without_low_risk"],
        RESEARCH_RANKER_V25_SHADOW_STRATEGY: strategy_comparison[RESEARCH_RANKER_V25_SHADOW_STRATEGY],
        RESEARCH_RANKER_V26_POOL_STRATEGY: strategy_comparison[RESEARCH_RANKER_V26_POOL_STRATEGY],
        RESEARCH_RANKER_V27_MATERIAL_STRATEGY: strategy_comparison[RESEARCH_RANKER_V27_MATERIAL_STRATEGY],
        RESEARCH_RANKER_V28_MATERIAL_STRATEGY: strategy_comparison[RESEARCH_RANKER_V28_MATERIAL_STRATEGY],
        RESEARCH_RANKER_V29_TAXONOMY_STRATEGY: strategy_comparison[RESEARCH_RANKER_V29_TAXONOMY_STRATEGY],
        "v27_without_material_type": strategy_comparison["v27_without_material_type"],
        "v27_material_only": strategy_comparison["v27_material_only"],
        TEXT_EMBEDDING_STRATEGY: strategy_comparison[TEXT_EMBEDDING_STRATEGY],
        VISUAL_EMBEDDING_STRATEGY: strategy_comparison[VISUAL_EMBEDDING_STRATEGY],
        TEXT_VISUAL_EMBEDDING_STRATEGY: strategy_comparison[TEXT_VISUAL_EMBEDDING_STRATEGY],
    }
    embedding_gap = embedding_strategy_gap(strategy_comparison, selected_strategy=strategy if strategy in EMBEDDING_RESEARCH_STRATEGIES else TEXT_VISUAL_EMBEDDING_STRATEGY)
    omni_account_pool_gates = _omni_account_pool_gates(scored)
    return {
        "rows": scored,
        "strategy_comparison": strategy_comparison,
        "per_account_metrics": per_account,
        "component_ablation": component_ablation,
        "promotion_gate": promotion_gate,
        "weight_config": _weight_config_for_strategy(strategy),
        "baseline_gap": _baseline_gap(strategy_comparison, strategy),
        "semantic_gap_analysis": _semantic_gap_analysis(strategy_comparison, strategy),
        "diagnostic_samples": _diagnostic_samples(scored, strategy, k=k),
        "diversity_summary": _diversity_summary(scored, strategy, k=k),
        "leakage_guard_summary": leakage_summary,
        "next_calibration_queue": _next_calibration_queue(scored, strategy, k=k),
        "calibration_summary": _calibration_summary(
            strategy_comparison,
            promotion_gate,
            strategy,
        ),
        "embedding_coverage": embedding_coverage_for_scope(account_id=account_id),
        "embedding_evidence_summary": embedding_backtest_summary(scored),
        "embedding_strategy_gap": embedding_gap,
        "omni_shadow_summary": _omni_shadow_summary(scored, train_basis, eval_basis, strategy=strategy, k=k),
        "omni_shadow_ablation": _omni_shadow_ablation(scored, k=k),
        "omni_shadow_account_metrics": _omni_shadow_account_metrics(scored, k=k),
        "omni_trust_profiles": _omni_trust_profiles(scored),
        "omni_pool_report": _omni_pool_report(scored),
        "omni_pool_gate": _omni_pool_gate(scored),
        "omni_account_pool_gates": omni_account_pool_gates,
        "omni_account_pool_summary": _omni_account_pool_summary(omni_account_pool_gates),
        "omni_material_report": _omni_material_report(scored),
        "omni_material_gate": _omni_material_gate(scored),
        "omni_material_gold_set_queue": _omni_material_gold_set_queue(scored, limit=60),
        "omni_material_calibration": material_full_quality,
        "omni_material_calibration_holdout": material_audit_quality,
        "omni_material_gold_split": material_gold_split_summary,
        "omni_material_router_profiles": _material_router_profile_rows(material_router_profiles),
        "omni_material_taxonomy_router_profiles": _material_router_profile_rows(material_taxonomy_router_profiles),
        "omni_material_v28_report": _omni_material_v28_report(scored),
        "omni_material_v28_gate": _omni_material_v28_gate(scored, material_audit_rows),
        "omni_material_v29_report": _omni_material_v29_report(scored),
        "omni_material_v29_gate": _omni_material_v29_gate(scored, material_audit_rows),
        "holdout_policy_key": actual_policy,
        "holdout_policy_text": _holdout_policy("historical_capture_samples", actual_policy, split_summary=split_summary),
        "label_version": label_version or RESEARCH_LABEL_VERSION,
        "split_summary": split_summary,
    }


def _historical_backtest_rows(account_id: str | None) -> list[dict]:
    return _historical_backtest_dataset(
        account_id,
        strategy=RESEARCH_RANKER_V24_STRATEGY,
        holdout_policy="time",
        label_version=None,
        k=10,
    ).get("rows") or []


def _historical_rows(account_id: str | None, *, label_version: str | None = None) -> list[dict]:
    clauses = [
        "COALESCE(platform_item_id, '') != ''",
        "(COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0)",
    ]
    params: list[Any] = []
    account = (account_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if label_version and label_version.lower() not in {"all", "any"}:
        clauses.append("research_label_version = ?")
        params.append(label_version)
    with connect() as conn:
        return fetch_all(
            conn,
            f"""
            SELECT *
            FROM historical_capture_samples
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, account_id ASC, published_at ASC, platform_item_id ASC, id ASC
            """,
            params,
        )


def _attach_omni_shadow_cache(rows: list[dict], cache_index: dict[str, dict]) -> list[dict]:
    if not cache_index:
        return [dict(row) for row in rows]
    enriched = []
    for row in rows:
        item = dict(row)
        sample_id = str(item.get("id") or "").strip()
        platform_id = str(item.get("platform_item_id") or "").strip()
        omni = cache_index.get(sample_id) or cache_index.get(platform_id)
        if isinstance(omni, dict) and omni:
            omni = refresh_omni_shadow_for_row(omni, item)
            item["_omni_shadow"] = omni
            for field, target in [
                ("content_category", "_omni_content_category"),
                ("hook_type", "_omni_hook_type"),
                ("slice_structure", "_omni_slice_structure"),
            ]:
                value = _omni_shadow_usable_field(omni, field)
                if value:
                    item[target] = value
        enriched.append(item)
    return enriched


def _attach_material_gold_annotations(rows: list[dict], annotation_index: dict[str, dict]) -> list[dict]:
    enriched = []
    for row in rows:
        item = dict(row)
        sample_id = str(item.get("id") or "").strip()
        annotation = annotation_index.get(sample_id)
        if isinstance(annotation, dict) and annotation.get("review_status") == "confirmed":
            item["_material_gold"] = dict(annotation)
        enriched.append(item)
    return enriched


def _omni_shadow_usable_field(omni: dict, field: str) -> str:
    quality = omni.get("semantic_quality") if isinstance(omni.get("semantic_quality"), dict) else {}
    field_quality = quality.get("field_quality") if isinstance(quality.get("field_quality"), dict) else {}
    item = field_quality.get(field) if isinstance(field_quality.get(field), dict) else {}
    if not item.get("usable_for_ranker"):
        return ""
    value = str(item.get("normalized_value") or "").strip()
    return value if _known_feature_value(value) else ""


def _omni_shadow_group_baselines(rows: list[dict]) -> dict[str, Any]:
    rewards = [float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0) for row in rows]
    result: dict[str, Any] = {
        "global": sum(rewards) / max(1, len(rewards)),
        "coverage_count": sum(1 for row in rows if isinstance(row.get("_omni_shadow"), dict) and row.get("_omni_shadow")),
        "total_count": len(rows),
        "fields": {},
    }
    for field in ["_omni_content_category", "_omni_hook_type", "_omni_slice_structure"]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            key = str(row.get(field) or "").strip()
            if not key:
                continue
            grouped[key].append(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0))
        result["fields"][field] = {
            key: {
                "avg": sum(values) / max(1, len(values)),
                "count": len(values),
            }
            for key, values in grouped.items()
            if values
        }
    return result


def _v25_omni_shadow_components(row: dict, baselines: dict[str, Any], *, field_mask: set[str] | None = None) -> dict[str, float]:
    masked = field_mask or set()
    omni = row.get("_omni_shadow") if isinstance(row.get("_omni_shadow"), dict) else {}
    if not omni:
        return {
            "v25_omni_shadow_available": 0.0,
            "v25_omni_shadow_usable_count": 0.0,
            "v25_omni_shadow_coverage": 0.0,
            "v25_omni_shadow_evidence_quality": 0.0,
            "v25_omni_baseline_score": 0.0,
            "v25_omni_agreement_count": 0.0,
            "v25_omni_conflict_count": 0.0,
        }
    usable_fields = [
        ("content_category", "_omni_content_category"),
        ("hook_type", "_omni_hook_type"),
        ("slice_structure", "_omni_slice_structure"),
    ]
    usable_fields = [(field, key) for field, key in usable_fields if field not in masked]
    usable_count = sum(1 for _, key in usable_fields if _known_feature_value(row.get(key)))
    coverage = usable_count / max(1, len(usable_fields))
    confidence_values = []
    quality = omni.get("semantic_quality") if isinstance(omni.get("semantic_quality"), dict) else {}
    field_quality = quality.get("field_quality") if isinstance(quality.get("field_quality"), dict) else {}
    for field, _ in usable_fields:
        item = field_quality.get(field) if isinstance(field_quality.get(field), dict) else {}
        confidence_values.append({"high": 1.0, "medium": 0.68, "low": 0.25}.get(str(item.get("confidence") or "").lower(), 0.45))
    confidence = sum(confidence_values) / max(1, len(confidence_values))
    baseline_score, support = _omni_shadow_baseline_score(row, baselines, field_mask=masked)
    agreement = 0.0
    conflict = 0.0
    for field, key in usable_fields:
        current = _index_key(row.get(field))
        suggested = _index_key(row.get(key))
        if not suggested:
            continue
        if current and current == suggested:
            agreement += 1.0
        elif current:
            conflict += 1.0
    evidence = coverage * confidence * min(1.0, support / 12.0 if support else 0.35)
    return {
        "v25_omni_shadow_available": 1.0,
        "v25_omni_shadow_usable_count": float(usable_count),
        "v25_omni_shadow_coverage": round(coverage, 4),
        "v25_omni_shadow_evidence_quality": round(evidence, 4),
        "v25_omni_baseline_score": round(float(baseline_score or 0.0), 4),
        "v25_omni_baseline_support": float(support),
        "v25_omni_agreement_count": agreement,
        "v25_omni_conflict_count": conflict,
    }


def _omni_shadow_baseline_score(row: dict, baselines: dict[str, Any], *, field_mask: set[str] | None = None) -> tuple[float, int]:
    masked = field_mask or set()
    weights = [
        ("content_category", "_omni_content_category", 0.34),
        ("hook_type", "_omni_hook_type", 0.38),
        ("slice_structure", "_omni_slice_structure", 0.28),
    ]
    total = 0.0
    weighted = 0.0
    support = 0
    field_data = baselines.get("fields") if isinstance(baselines.get("fields"), dict) else {}
    for logical_field, storage_field, weight in weights:
        if logical_field in masked:
            continue
        key = str(row.get(storage_field) or "").strip()
        item = (field_data.get(storage_field) or {}).get(key) if key else None
        if not isinstance(item, dict):
            continue
        count = int(item.get("count") or 0)
        confidence = min(1.0, count / 8.0) if count else 0.0
        effective_weight = weight * max(0.35, confidence)
        weighted += float(item.get("avg") or 0.0) * effective_weight
        total += effective_weight
        support += count
    if total <= 0:
        return 0.0, 0
    global_score = float(baselines.get("global") or 0.0)
    return ((weighted + global_score * max(0.0, 1.0 - total)) / max(1.0, total + max(0.0, 1.0 - total))), support


def _v25_shadow_ablation_scores(
    row: dict,
    omni_baselines: dict[str, Any],
    components: dict[str, float],
    *,
    base_v24_score: float,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for name, mask in [
        ("v25_without_content_category", {"content_category"}),
        ("v25_without_hook_type", {"hook_type"}),
        ("v25_without_slice_structure", {"slice_structure"}),
    ]:
        omni_components = _v25_omni_shadow_components(row, omni_baselines, field_mask=mask)
        scores[name] = _score_v25_shadow(
            base_v24_score,
            components,
            omni_components,
            config=RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG,
        )
    no_pull_config = {
        **RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG,
        "omni_baseline_pull_weight": 0.0,
    }
    full_components = _v25_omni_shadow_components(row, omni_baselines)
    scores["v25_without_baseline_pull"] = _score_v25_shadow(
        base_v24_score,
        components,
        full_components,
        config=no_pull_config,
    )
    scores["v25_conflict_penalty_only"] = _score_v25_shadow_conflict_only(
        base_v24_score,
        full_components,
        config=RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG,
    )
    return scores


def _prepare_history_tokens(rows: list[dict]) -> list[dict]:
    prepared = []
    for row in rows:
        item = dict(row)
        text = _history_text(item)
        tokens = _history_tokens(text)
        item["_history_text"] = text
        item["_history_tokens"] = tokens
        item["_history_token_count"] = len(tokens)
        for field in ["content_category", "hook_type", "slice_structure", "artist_names", "song_title", "original_sound_owner", "entity_signal"]:
            item[f"_norm_{field}"] = _index_key(item.get(field))
        prepared.append(item)
    return prepared


def _masked_history_rows(rows: list[dict], fields: list[str]) -> list[dict]:
    masked = []
    for row in rows:
        item = dict(row)
        for field in fields:
            if field in {"content_category", "hook_type", "slice_structure"}:
                item[field] = "unknown"
            elif field == "is_original_sound":
                item[field] = 0
            else:
                item[field] = ""
        masked.append(item)
    return masked


def _v24_reliable_signal_rows(rows: list[dict]) -> list[dict]:
    return [_v24_reliable_signal_row(row) for row in rows]


def _v24_reliable_signal_row(row: dict) -> dict:
    item = dict(row)
    manual_verified = str(item.get("classification_confidence") or "").strip().lower() == "manual_verified"
    if not manual_verified:
        if _known_feature_value(item.get("hook_type")):
            item["hook_type"] = "unknown"
        if _known_feature_value(item.get("slice_structure")):
            item["slice_structure"] = "unknown"
            item["structure_evidence"] = ""
            item["structure_confidence"] = "low"
            item["structure_unknown_reason"] = "quarantined_by_signal_trust_gate_v2_4"
    if _v24_song_title_untrusted(item):
        item["song_title"] = ""
    for key in list(item.keys()):
        if key.startswith("_history_") or key.startswith("_norm_"):
            item.pop(key, None)
    return item


def _v24_song_title_untrusted(row: dict) -> bool:
    title = str(row.get("song_title") or "").strip()
    if not _known_feature_value(title):
        return False
    owner = str(row.get("original_sound_owner") or "").strip()
    normalized_title = title.lower()
    normalized_owner = owner.lower()
    if _truthy_flag(row.get("is_original_sound")):
        return True
    if "原声" in title or "創作的原聲" in title or "创作的原声" in title:
        return True
    if normalized_owner and (normalized_title == normalized_owner or normalized_owner in normalized_title and len(title) <= len(owner) + 8):
        return True
    return False


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _v24_signal_quality(raw_row: dict, gated_row: dict, components: dict[str, float]) -> dict[str, float]:
    stable_fields = ["content_category", "artist_names", "original_sound_owner", "entity_signal"]
    trusted = sum(1 for field in stable_fields if _known_feature_value(gated_row.get(field)))
    manual_verified = str(raw_row.get("classification_confidence") or "").strip().lower() == "manual_verified"
    if manual_verified:
        trusted += sum(1 for field in ["hook_type", "slice_structure"] if _known_feature_value(gated_row.get(field)))
    quarantined_hook = 1.0 if _known_feature_value(raw_row.get("hook_type")) and not _known_feature_value(gated_row.get("hook_type")) else 0.0
    quarantined_slice = 1.0 if _known_feature_value(raw_row.get("slice_structure")) and not _known_feature_value(gated_row.get("slice_structure")) else 0.0
    quarantined_song = 1.0 if _known_feature_value(raw_row.get("song_title")) and not _known_feature_value(gated_row.get("song_title")) else 0.0
    quarantined = quarantined_hook + quarantined_slice + quarantined_song
    evidence = _evidence_quality_from_components(components)
    trust_score = clamp(32.0 + trusted * 14.0 + (18.0 if manual_verified else 0.0) + evidence * 18.0 - quarantined * 4.0)
    return {
        "v24_signal_trust": round(trust_score, 4),
        "v24_trusted_signal_count": float(trusted),
        "v24_quarantined_signal_count": round(quarantined, 4),
        "v24_quarantined_hook_type": quarantined_hook,
        "v24_quarantined_slice_structure": quarantined_slice,
        "v24_quarantined_song_title": quarantined_song,
        "v24_evidence_quality": float(evidence),
        "v24_manual_verified": 1.0 if manual_verified else 0.0,
    }


def _history_candidate_index(rows: list[dict]) -> dict[str, Any]:
    token_index: dict[str, list[int]] = defaultdict(list)
    field_index: dict[str, dict[str, list[int]]] = {
        field: defaultdict(list)
        for field in [
            "account_id",
            "content_category",
            "hook_type",
            "slice_structure",
            "artist_names",
            "song_title",
            "original_sound_owner",
            "entity_signal",
        ]
    }
    for index, row in enumerate(rows):
        tokens = row.get("_history_tokens")
        if not isinstance(tokens, set):
            continue
        for token in sorted(tokens):
            token_index[token].append(index)
        for field in field_index:
            key = _index_key(row.get(field))
            if key:
                field_index[field][key].append(index)
    return {
        "rows": rows,
        "token_index": token_index,
        "field_index": {field: dict(values) for field, values in field_index.items()},
    }


def _historical_holdout_split(rows: list[dict], holdout_policy: str) -> tuple[list[dict], list[dict], str, dict]:
    policy = str(holdout_policy or "time").strip().lower()
    if policy in {"hash", "hash_holdout"}:
        train, eval_rows = _hash_holdout_split(rows)
        return train, eval_rows, "hash", {"fallback_reason": "", "account_count": len({row.get("account_id") for row in rows})}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("account_id") or "unknown")].append(row)
    train_rows: list[dict] = []
    eval_rows: list[dict] = []
    fallback_accounts = []
    time_accounts = 0
    for account, account_rows in grouped.items():
        timed = [(row, _parse_iso(row.get("published_at"))) for row in account_rows]
        if len(account_rows) >= 5 and all(parsed is not None for _, parsed in timed):
            ordered = [
                row
                for row, _ in sorted(
                    timed,
                    key=lambda item: (
                        item[1] or datetime.min.replace(tzinfo=timezone.utc),
                        _deterministic_row_key(item[0]),
                    ),
                )
            ]
            cutoff = max(1, min(len(ordered) - 1, math.ceil(len(ordered) * 0.8)))
            train_rows.extend(ordered[:cutoff])
            eval_rows.extend(ordered[cutoff:])
            time_accounts += 1
        else:
            train_part, eval_part = _hash_holdout_split(account_rows)
            train_rows.extend(train_part)
            eval_rows.extend(eval_part)
            fallback_accounts.append(account)
    actual_policy = "time" if time_accounts else "hash"
    return (
        train_rows,
        eval_rows,
        actual_policy,
        {
            "account_count": len(grouped),
            "time_split_accounts": time_accounts,
            "hash_fallback_accounts": fallback_accounts,
            "train_count": len(train_rows),
            "eval_count": len(eval_rows),
        },
    )


def _hash_holdout_split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    train_rows = [row for row in rows if _holdout_bucket(row) != 0]
    eval_rows = [row for row in rows if _holdout_bucket(row) == 0]
    if eval_rows:
        return train_rows or rows, eval_rows
    ordered = sorted(rows, key=lambda row: str(row.get("platform_item_id") or row.get("sample_key") or row.get("id") or ""))
    cutoff = max(1, math.ceil(len(ordered) * 0.8))
    return ordered[:cutoff], ordered[cutoff:] or ordered[-1:]


def _apply_leakage_guard(train_rows: list[dict], eval_rows: list[dict]) -> tuple[list[dict], dict]:
    eval_item_ids = {_leakage_platform_key(row) for row in eval_rows if _leakage_platform_key(row)}
    eval_title_keys = {_stable_title_key(row.get("title")) for row in eval_rows if _stable_title_key(row.get("title"))}
    kept: list[dict] = []
    removed_item = 0
    removed_title = 0
    for row in train_rows:
        item_key = _leakage_platform_key(row)
        title_key = _stable_title_key(row.get("title"))
        if item_key and item_key in eval_item_ids:
            removed_item += 1
            continue
        if title_key and title_key in eval_title_keys:
            removed_title += 1
            continue
        kept.append(row)
    if not kept and train_rows:
        kept = train_rows
    return kept, {
        "policy": "remove same platform_item_id and stable title key from training evidence when evaluating holdout rows",
        "train_before": len(train_rows),
        "train_after": len(kept),
        "eval_count": len(eval_rows),
        "removed_same_platform_item_id": removed_item,
        "removed_same_title_key": removed_title,
        "fallback_used": bool(train_rows and not kept),
    }


def _leakage_platform_key(row: dict) -> str:
    account = str(row.get("account_id") or "").strip().lower()
    item_id = str(row.get("platform_item_id") or "").strip().lower()
    return f"{account}:{item_id}" if account and item_id else item_id


def _stable_title_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[@#《》【】\[\]（）()，,。.!！?？:：;；\"'“”‘’、\s]+", "", text)
    text = re.sub(r"\d+", "#", text)
    return text[:80]


def _parse_iso(value: Any) -> datetime | None:
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


def _holdout_bucket(row: dict) -> int:
    key = str(row.get("platform_item_id") or row.get("sample_key") or row.get("id") or "")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 5


def _historical_group_baselines(rows: list[dict]) -> dict:
    global_rewards = [float(row.get("normalized_reward") or row.get("reward_proxy") or 0) for row in rows]
    baselines: dict[str, dict[str, float]] = {"global": {"all": sum(global_rewards) / max(1, len(global_rewards))}}
    for field in ["account_id", "content_category", "hook_type", "slice_structure", "artist_names", "song_title", "original_sound_owner", "entity_signal"]:
        groups: dict[str, list[float]] = {}
        for row in rows:
            key = str(row.get(field) or "")
            if not key:
                continue
            groups.setdefault(key, []).append(float(row.get("normalized_reward") or row.get("reward_proxy") or 0))
        baselines[field] = {key: sum(values) / max(1, len(values)) for key, values in groups.items() if values}
    return baselines


def _account_ranker_profiles(rows: list[dict], *, thresholds: tuple[float, float] | None = None) -> dict[str, dict[str, float]]:
    threshold_values = thresholds or _interaction_thresholds(rows)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        account = str(row.get("account_id") or "").strip()
        if account:
            grouped[account].append(row)
    profiles: dict[str, dict[str, float]] = {}
    for account, items in grouped.items():
        sample_count = len(items)
        if not sample_count:
            continue
        labels = Counter(_interaction_label(row, threshold_values) for row in items)
        unknown_count = sum(
            1
            for row in items
            if any(
                str(row.get(field) or "").strip().lower() in {"", "unknown", "none", "null"}
                for field in ["content_category", "hook_type", "slice_structure"]
            )
        )
        manual_count = sum(1 for row in items if str(row.get("classification_confidence") or "").strip().lower() == "manual_verified")
        rewards = [float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0) for row in items]
        profiles[account] = {
            "sample_count": float(sample_count),
            "high_rate": labels["high"] / sample_count,
            "low_rate": labels["low"] / sample_count,
            "unknown_rate": unknown_count / sample_count,
            "manual_rate": manual_count / sample_count,
            "avg_reward": sum(rewards) / max(1, sample_count),
        }
    return profiles


def _historical_predicted_score(row: dict, baselines: dict) -> float:
    global_score = float((baselines.get("global") or {}).get("all") or 0)
    weighted = 0.0
    total = 0.0
    for field, weight in [("account_id", 0.25), ("content_category", 0.3), ("hook_type", 0.25), ("slice_structure", 0.2)]:
        key = str(row.get(field) or "")
        value = (baselines.get(field) or {}).get(key)
        if value is None:
            continue
        weighted += float(value) * weight
        total += weight
    if total <= 0:
        return round(global_score, 4)
    return round((weighted + global_score * max(0.0, 1.0 - total)) / max(1.0, total + max(0.0, 1.0 - total)), 4)


def _historical_strategy_scores(
    row: dict,
    train_rows: list[dict],
    baselines: dict,
    *,
    history_index: dict[str, Any] | None = None,
    thresholds: tuple[float, float] | None = None,
    account_profiles: dict[str, dict[str, float]] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    current = _historical_predicted_score(row, baselines)
    semantic = _semantic_baseline_v2(row, baselines)
    components = _historical_research_ranker_components(
        row,
        train_rows,
        semantic,
        history_index=history_index,
        thresholds=thresholds,
    )
    ranker = _score_from_components(components, semantic, use_prototypes=True, use_low_risk=True)
    ranker_v21 = _score_v21_from_components(components, config=RESEARCH_RANKER_V21_WEIGHT_CONFIG)
    ranker_v22 = _score_v22_from_components(components, config=RESEARCH_RANKER_V22_WEIGHT_CONFIG)
    ranker_v23 = _score_v23_from_components(
        components,
        row=row,
        account_profiles=account_profiles,
        config=RESEARCH_RANKER_V23_WEIGHT_CONFIG,
    )
    no_prototypes = _score_from_components(components, semantic, use_prototypes=False, use_low_risk=True)
    no_low_risk = _score_from_components(components, semantic, use_prototypes=True, use_low_risk=False)
    return (
        {
            "current_rules": current,
            "semantic_baseline_v2": semantic,
            "research_ranker_v2": ranker,
            RESEARCH_RANKER_V21_STRATEGY: ranker_v21,
            RESEARCH_RANKER_V22_STRATEGY: ranker_v22,
            RESEARCH_RANKER_V23_STRATEGY: ranker_v23,
            "ranker_without_prototypes": no_prototypes,
            "ranker_without_low_risk": no_low_risk,
        },
        components,
    )


def _semantic_baseline_v2(row: dict, baselines: dict) -> float:
    global_score = float((baselines.get("global") or {}).get("all") or 0)
    weighted = 0.0
    total = 0.0
    for field, weight in [
        ("account_id", 0.18),
        ("content_category", 0.25),
        ("hook_type", 0.2),
        ("slice_structure", 0.17),
        ("artist_names", 0.12),
        ("song_title", 0.08),
    ]:
        key = str(row.get(field) or "")
        value = (baselines.get(field) or {}).get(key)
        if value is None:
            continue
        weighted += float(value) * weight
        total += weight
    if total <= 0:
        return round(global_score, 4)
    return round((weighted + global_score * max(0.0, 1.0 - total)) / max(1.0, total + max(0.0, 1.0 - total)), 4)


def _historical_research_ranker_score(
    row: dict,
    train_rows: list[dict],
    semantic_score: float,
    *,
    use_prototypes: bool,
    use_low_risk: bool,
    history_index: dict[str, Any] | None = None,
    thresholds: tuple[float, float] | None = None,
) -> tuple[float, dict[str, float]]:
    components = _historical_research_ranker_components(
        row,
        train_rows,
        semantic_score,
        history_index=history_index,
        thresholds=thresholds,
    )
    return _score_from_components(components, semantic_score, use_prototypes=use_prototypes, use_low_risk=use_low_risk), components


def _historical_research_ranker_components(
    row: dict,
    train_rows: list[dict],
    semantic_score: float,
    *,
    history_index: dict[str, Any] | None = None,
    thresholds: tuple[float, float] | None = None,
) -> dict[str, float]:
    cached_tokens = row.get("_history_tokens")
    target_tokens = cached_tokens if isinstance(cached_tokens, set) else _history_tokens(_history_text(row))
    candidate_rows = _candidate_history_rows(row, target_tokens, train_rows, history_index)
    high_score = 0.0
    low_risk = 0.0
    best_similarity = 0.0
    high_matches: list[tuple[float, float, dict]] = []
    low_matches: list[tuple[float, float, dict]] = []
    threshold_values = thresholds or _interaction_thresholds(train_rows)
    for sample in candidate_rows:
        similarity = _history_similarity(target_tokens, row, sample)
        if similarity <= 0:
            continue
        reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0)
        label = _interaction_label(sample, threshold_values)
        best_similarity = max(best_similarity, similarity)
        if label == "high":
            high_score = max(high_score, similarity * reward)
            high_matches.append((similarity, reward, sample))
        elif label == "low":
            low_risk = max(low_risk, similarity * (100.0 - reward))
            low_matches.append((similarity, reward, sample))
    prototype_fit = _historical_prototype_fit(high_matches)
    semantic_trust = _historical_semantic_trust(row, high_matches + low_matches)
    novelty = clamp((1.0 - min(1.0, best_similarity)) * 42.0 + semantic_trust * 18.0)
    return {
        "high_similarity": round(clamp(high_score), 4),
        "low_interaction_risk": round(clamp(low_risk), 4),
        "account_baseline_position": round(clamp(semantic_score), 4),
        "prototype_fit": round(clamp(prototype_fit), 4),
        "semantic_label_trust": round(clamp(semantic_trust * 100.0), 4),
        "long_tail_novelty": round(clamp(novelty), 4),
        "best_similarity": round(best_similarity, 4),
    }


def _score_from_components(
    components: dict[str, float],
    semantic_score: float,
    *,
    use_prototypes: bool,
    use_low_risk: bool,
) -> float:
    low_risk = float(components.get("low_interaction_risk") or 0) if use_low_risk else 0.0
    prototype_fit = float(components.get("prototype_fit") or 0) if use_prototypes else 0.0
    score = clamp(
        50.0
        + float(components.get("high_similarity") or 0) * 0.34
        - low_risk * 0.16
        + (semantic_score - 50.0) * 0.09
        + prototype_fit * 0.055
        + (float(components.get("semantic_label_trust") or 0) - 50.0) * 0.025
        + max(0.0, float(components.get("long_tail_novelty") or 0) - 35.0) * 0.035
    )
    return round(score, 4)


def _score_v21_from_components(components: dict[str, float], *, config: dict[str, Any] | None = None) -> float:
    weights = dict(RESEARCH_RANKER_V21_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    semantic = float(components.get("account_baseline_position") or 50.0)
    score = (
        semantic * float(weights.get("semantic_weight") or 0.0)
        + float(components.get("high_similarity") or 0.0) * float(weights.get("high_similarity_weight") or 0.0)
        - float(components.get("low_interaction_risk") or 0.0) * float(weights.get("low_risk_weight") or 0.0)
        + float(components.get("prototype_fit") or 0.0) * float(weights.get("prototype_weight") or 0.0)
        + (float(components.get("semantic_label_trust") or 0.0) - 50.0) * float(weights.get("semantic_trust_weight") or 0.0)
        + max(0.0, float(components.get("long_tail_novelty") or 0.0) - 35.0) * float(weights.get("novelty_weight") or 0.0)
        + float(weights.get("bias") or 0.0)
    )
    return round(clamp(score), 4)


def _score_v22_from_components(components: dict[str, float], *, config: dict[str, Any] | None = None) -> float:
    weights = dict(RESEARCH_RANKER_V22_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    semantic = float(components.get("account_baseline_position") or 50.0)
    evidence = _evidence_quality_from_components(components)
    high = float(components.get("high_similarity") or 0.0)
    risk = float(components.get("low_interaction_risk") or 0.0)
    prototype = float(components.get("prototype_fit") or 0.0)
    trust = float(components.get("semantic_label_trust") or 0.0)
    novelty = max(0.0, float(components.get("long_tail_novelty") or 0.0) - 35.0)
    threshold = float(weights.get("evidence_threshold") or 0.35)
    semantic_weight = (
        float(weights.get("semantic_strong_weight") or 0.0)
        if evidence >= threshold
        else float(weights.get("semantic_floor_weight") or 0.0)
    )
    semantic_base = 50.0 + (semantic - 50.0) * semantic_weight
    positive_gate = evidence if evidence >= threshold else evidence * 0.25
    risk_floor = float(weights.get("risk_activation_threshold") or 0.0)
    risk_margin = float(weights.get("risk_high_similarity_margin") or 0.0)
    risk_excess = max(0.0, risk - max(risk_floor, high * risk_margin))
    risk_gate = evidence if evidence >= threshold else evidence * 0.5
    score = (
        semantic_base
        + high * float(weights.get("high_similarity_weight") or 0.0) * positive_gate
        - risk_excess * float(weights.get("low_risk_weight") or 0.0) * risk_gate
        + prototype * float(weights.get("prototype_weight") or 0.0) * positive_gate
        + (trust - 50.0) * float(weights.get("semantic_trust_weight") or 0.0)
        + novelty * float(weights.get("novelty_weight") or 0.0)
        + float(weights.get("bias") or 0.0)
    )
    return round(clamp(score), 4)


def _score_v23_from_components(
    components: dict[str, float],
    *,
    row: dict | None = None,
    account_profiles: dict[str, dict[str, float]] | None = None,
    config: dict[str, Any] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V23_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    base = _score_v22_from_components(components, config=weights)
    profile = (account_profiles or {}).get(str((row or {}).get("account_id") or ""))
    if not profile or float(profile.get("sample_count") or 0.0) < float(weights.get("account_ready_sample_threshold") or 50):
        return base
    confidence = min(1.0, max(0.0, float(profile.get("sample_count") or 0.0) / 500.0))
    high = float(components.get("high_similarity") or 0.0)
    risk = float(components.get("low_interaction_risk") or 0.0)
    trust = float(components.get("semantic_label_trust") or 0.0)
    high_rate = float(profile.get("high_rate") or 0.0)
    low_rate = float(profile.get("low_rate") or 0.0)
    unknown_rate = float(profile.get("unknown_rate") or 0.0)
    adjustment = 0.0
    if high > 55.0:
        adjustment += (high - 55.0) * float(weights.get("account_high_similarity_bonus") or 0.0) * (0.65 + high_rate) * confidence
    if risk > 60.0 and high < 55.0:
        adjustment -= (risk - 60.0) * float(weights.get("account_low_risk_guard") or 0.0) * (0.65 + low_rate) * confidence
    if trust > 60.0 and unknown_rate < 0.35:
        adjustment += (trust - 60.0) * float(weights.get("account_semantic_trust_bonus") or 0.0) * confidence
    unknown_core_count = _unknown_core_semantic_count(row or {})
    if unknown_core_count >= 3:
        adjustment -= (unknown_core_count - 2) * float(weights.get("unknown_core_semantic_penalty") or 0.0) * (1.0 + min(1.0, high / 100.0) * 0.45)
    return round(clamp(base + adjustment), 4)


def _score_v24_from_components(
    components: dict[str, float],
    *,
    row: dict | None = None,
    account_profiles: dict[str, dict[str, float]] | None = None,
    config: dict[str, Any] | None = None,
    signal_quality: dict[str, float] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V24_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    base = _score_v23_from_components(components, row=row, account_profiles=account_profiles, config=weights)
    evidence = _evidence_quality_from_components(components)
    semantic = float(components.get("account_baseline_position") or 50.0)
    if evidence < 0.28:
        floor = clamp(float(weights.get("semantic_fallback_floor") or 0.82), 0.0, 1.0)
        base = semantic * floor + base * (1.0 - floor)
    quality = signal_quality or {}
    trusted = float(quality.get("v24_trusted_signal_count") or 0.0)
    quarantined = float(quality.get("v24_quarantined_signal_count") or 0.0)
    signal_trust = float(quality.get("v24_signal_trust") or 50.0)
    adjustment = (
        trusted * float(weights.get("trusted_signal_bonus") or 0.0)
        + max(0.0, signal_trust - 62.0) * 0.006
        - quarantined * float(weights.get("weak_signal_penalty") or 0.0)
    )
    return round(clamp(base + adjustment), 4)


def _score_v25_shadow(
    v24_score: float,
    components: dict[str, float],
    omni_components: dict[str, float],
    *,
    config: dict[str, Any] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    coverage = float(omni_components.get("v25_omni_shadow_coverage") or 0.0)
    evidence = float(omni_components.get("v25_omni_shadow_evidence_quality") or 0.0)
    if coverage < float(weights.get("omni_min_coverage") or 0.34) or evidence <= 0:
        return round(clamp(v24_score), 4)
    baseline = float(omni_components.get("v25_omni_baseline_score") or 0.0)
    if baseline <= 0:
        return round(clamp(v24_score), 4)
    agreement = float(omni_components.get("v25_omni_agreement_count") or 0.0)
    conflict = float(omni_components.get("v25_omni_conflict_count") or 0.0)
    support = min(1.0, float(omni_components.get("v25_omni_baseline_support") or 0.0) / 24.0)
    low_risk = float(components.get("low_interaction_risk") or 0.0)
    risk_guard = 0.65 if low_risk >= 68.0 and baseline > v24_score else 1.0
    pull = (baseline - v24_score) * float(weights.get("omni_baseline_pull_weight") or 0.0) * evidence * (0.65 + support * 0.35) * risk_guard
    usable_bonus = (
        float(omni_components.get("v25_omni_shadow_usable_count") or 0.0)
        * (
            float(weights.get("omni_content_category_bonus") or 0.0)
            + float(weights.get("omni_hook_bonus") or 0.0)
            + float(weights.get("omni_slice_bonus") or 0.0)
        )
        / 3.0
        * evidence
    )
    agreement_adjustment = (
        agreement * float(weights.get("omni_agreement_bonus") or 0.0)
        - conflict * float(weights.get("omni_conflict_penalty") or 0.0)
    ) * evidence
    return round(clamp(v24_score + pull + usable_bonus + agreement_adjustment), 4)


def _score_v25_shadow_conflict_only(
    v24_score: float,
    omni_components: dict[str, float],
    *,
    config: dict[str, Any] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    evidence = float(omni_components.get("v25_omni_shadow_evidence_quality") or 0.0)
    conflict = float(omni_components.get("v25_omni_conflict_count") or 0.0)
    if evidence <= 0 or conflict <= 0:
        return round(clamp(v24_score), 4)
    penalty = conflict * float(weights.get("omni_conflict_penalty") or 0.0) * evidence
    return round(clamp(v24_score - penalty), 4)


def _omni_router_profiles_from_training(rows: list[dict], omni_baselines: dict[str, Any]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if isinstance(row.get("_omni_shadow"), dict) and row.get("_omni_shadow"):
            grouped[str(row.get("account_id") or "unknown")].append(row)
    profiles: dict[str, dict] = {}
    for account_id, items in grouped.items():
        labels = Counter(str(row.get("performance_label") or "unknown").lower() for row in items)
        components = [_v25_omni_shadow_components(row, omni_baselines) for row in items]
        evidence_values = [float(item.get("v25_omni_shadow_evidence_quality") or 0.0) for item in components]
        conflict_count = sum(1 for item in components if float(item.get("v25_omni_conflict_count") or 0.0) > 0)
        usable_values = [float(item.get("v25_omni_shadow_usable_count") or 0.0) for item in components]
        cached_count = len(items)
        high_rate = labels.get("high", 0) / max(1, cached_count)
        low_rate = labels.get("low", 0) / max(1, cached_count)
        avg_evidence = sum(evidence_values) / max(1, len(evidence_values))
        conflict_rate = conflict_count / max(1, cached_count)
        usable_rate = sum(1 for value in usable_values if value >= 2.0) / max(1, cached_count)
        if cached_count >= 5 and high_rate >= low_rate and avg_evidence >= 0.18 and conflict_rate <= 0.85:
            status = "boost_enabled"
        elif cached_count >= 5 and (low_rate > high_rate + 0.12 or conflict_rate >= 0.9 or avg_evidence < 0.05):
            status = "quarantine"
        else:
            status = "evidence_only"
        profiles[account_id] = {
            "account_id": account_id,
            "router_status": status,
            "cached_train_count": cached_count,
            "label_distribution": dict(labels),
            "high_rate": round(high_rate, 4),
            "low_rate": round(low_rate, 4),
            "avg_evidence_quality": round(avg_evidence, 4),
            "conflict_rate": round(conflict_rate, 4),
            "usable_rate": round(usable_rate, 4),
        }
    return profiles


def _v26_omni_pool_components(row: dict, omni_components: dict[str, float], profiles: dict[str, dict]) -> dict[str, float]:
    profile = profiles.get(str(row.get("account_id") or "")) or {}
    status = str(profile.get("router_status") or "evidence_only")
    status_score = {"boost_enabled": 1.0, "evidence_only": 0.5, "quarantine": 0.0}.get(status, 0.5)
    evidence = float(omni_components.get("v25_omni_shadow_evidence_quality") or 0.0)
    coverage = float(omni_components.get("v25_omni_shadow_coverage") or 0.0)
    pool_eligible = 1.0 if status == "boost_enabled" and evidence > 0 and coverage >= 0.34 else 0.0
    return {
        "v26_omni_router_status_score": round(status_score, 4),
        "v26_omni_pool_eligible": pool_eligible,
        "v26_omni_account_trust": round(float(profile.get("avg_evidence_quality") or 0.0), 4),
        "v26_omni_account_conflict_rate": round(float(profile.get("conflict_rate") or 0.0), 4),
        "v26_omni_pool_evidence": round(evidence * pool_eligible, 4),
    }


def _score_v26_pool(
    v24_score: float,
    v25_score: float,
    components: dict[str, float],
    omni_components: dict[str, float],
    pool_components: dict[str, float],
    *,
    config: dict[str, Any] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V26_POOL_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    evidence = float(omni_components.get("v25_omni_shadow_evidence_quality") or 0.0)
    if evidence <= 0:
        return round(clamp(v24_score), 4)
    status_score = float(pool_components.get("v26_omni_router_status_score") or 0.5)
    delta = float(v25_score) - float(v24_score)
    if status_score >= 0.99:
        adjusted = max(-float(weights.get("max_penalty") or 1.2), min(float(weights.get("max_boost") or 5.0), delta))
        return round(clamp(v24_score + adjusted), 4)
    if status_score <= 0.01:
        if delta < 0:
            adjusted = max(-float(weights.get("quarantine_max_penalty") or 0.5), delta)
            return round(clamp(v24_score + adjusted), 4)
        return round(clamp(v24_score), 4)
    if delta > 0:
        adjusted = min(float(weights.get("evidence_only_max_boost") or 0.0), delta)
        return round(clamp(v24_score + adjusted), 4)
    return round(clamp(v24_score), 4)


def _v27_material_components(row: dict, omni_components: dict[str, float], pool_components: dict[str, float]) -> dict[str, float]:
    suggestions = _omni_shadow_suggestions(row)
    quality = _omni_shadow_field_quality(row)
    material = _known_material_value(suggestions.get("material_type"))
    domain = _known_material_value(suggestions.get("domain_category"))
    program = _known_material_value(suggestions.get("program_context"))
    presentation = _known_material_value(suggestions.get("presentation_style"))
    content_category = _known_material_value(row.get("content_category"))
    material_quality = quality.get("material_type") if isinstance(quality.get("material_type"), dict) else {}
    confidence = _omni_confidence_score(material_quality.get("confidence"))
    material_known = 1.0 if material else 0.0
    domain_known = 1.0 if domain else 0.0
    compatible = 1.0 if material and _material_compatible_with_category(material, content_category) else 0.0
    music_domain_split = 1.0 if domain == "music_variety" and material and material not in {"program_context"} else 0.0
    conflict = 1.0 if material and content_category and not _material_compatible_with_category(material, content_category) else 0.0
    status_score = float(pool_components.get("v26_omni_router_status_score") or 0.5)
    evidence = float(omni_components.get("v25_omni_shadow_evidence_quality") or 0.0)
    material_evidence = material_known * confidence * (0.55 + min(0.45, evidence))
    eligible = 1.0 if material_known and status_score > 0.01 and (compatible or music_domain_split) else 0.0
    return {
        "v27_material_type_available": material_known,
        "v27_domain_category_available": domain_known,
        "v27_program_context_available": 1.0 if program else 0.0,
        "v27_presentation_style_available": 1.0 if presentation else 0.0,
        "v27_material_confidence": round(confidence, 4),
        "v27_material_evidence": round(material_evidence, 4),
        "v27_material_agreement": compatible,
        "v27_material_conflict": conflict,
        "v27_music_domain_split": music_domain_split,
        "v27_material_router_eligible": eligible,
        "v27_material_account_status_score": round(status_score, 4),
    }


def _score_v27_material(
    v24_score: float,
    components: dict[str, float],
    omni_components: dict[str, float],
    pool_components: dict[str, float],
    material_components: dict[str, float],
    *,
    config: dict[str, Any] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    evidence = float(material_components.get("v27_material_evidence") or 0.0)
    if evidence <= 0:
        return round(clamp(v24_score), 4)
    status_score = float(material_components.get("v27_material_account_status_score") or 0.5)
    agreement = float(material_components.get("v27_material_agreement") or 0.0)
    conflict = float(material_components.get("v27_material_conflict") or 0.0)
    split = float(material_components.get("v27_music_domain_split") or 0.0)
    program = float(material_components.get("v27_program_context_available") or 0.0)
    boost = (
        agreement * float(weights.get("material_agreement_bonus") or 0.0)
        + split * float(weights.get("music_domain_split_bonus") or 0.0)
        + program * float(weights.get("program_context_bonus") or 0.0)
    ) * evidence
    penalty = conflict * float(weights.get("material_conflict_penalty") or 0.0) * evidence
    low_risk = float(components.get("low_interaction_risk") or 0.0)
    if low_risk >= 68.0 and boost > 0:
        boost *= float(weights.get("low_risk_boost_multiplier") or 0.35)
    if status_score >= 0.99:
        boost = min(float(weights.get("boost_enabled_max_boost") or 1.25), boost)
    elif status_score <= 0.01:
        boost = 0.0
        penalty = min(float(weights.get("quarantine_max_penalty") or 0.45), penalty)
    else:
        boost = min(float(weights.get("evidence_only_max_boost") or 0.55), boost)
    return round(clamp(v24_score + boost - penalty), 4)


def _v27_material_ablation_scores(
    base_score: float,
    components: dict[str, float],
    omni_components: dict[str, float],
    pool_components: dict[str, float],
    material_components: dict[str, float],
) -> dict[str, float]:
    material_only_components = {
        **material_components,
        "v27_material_conflict": 0.0,
    }
    material_only = _score_v27_material(
        base_score,
        components,
        omni_components,
        pool_components,
        material_only_components,
        config=RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG,
    )
    return {
        "v27_without_material_type": round(clamp(base_score), 4),
        "v27_material_only": material_only,
    }


def _material_gold_from_row(row: dict) -> dict:
    for key in ("_material_gold", "material_gold_annotation", "annotation"):
        value = row.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _material_gold_deduplicated_rows(rows: list[dict]) -> list[dict]:
    representatives: dict[str, dict] = {}
    for row in rows:
        gold = _material_gold_from_row(row)
        if not gold:
            continue
        group_key = _material_gold_group_key(row)
        current = representatives.get(group_key)
        if current is None:
            representatives[group_key] = row
            continue
        current_gold = _material_gold_from_row(current)
        current_key = (
            str(current_gold.get("updated_at") or ""),
            str(current.get("id") or current.get("training_sample_id") or ""),
        )
        candidate_key = (
            str(gold.get("updated_at") or ""),
            str(row.get("id") or row.get("training_sample_id") or ""),
        )
        if candidate_key > current_key:
            representatives[group_key] = row
    return [representatives[key] for key in sorted(representatives)]


def _material_gold_calibration_split(rows: list[dict]) -> dict[str, Any]:
    raw_rows = [row for row in rows if _material_gold_from_row(row)]
    unique_rows = _material_gold_deduplicated_rows(raw_rows)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in unique_rows:
        grouped[str(row.get("account_id") or "unknown")].append(row)

    calibration_rows: list[dict] = []
    audit_rows: list[dict] = []
    account_summary: list[dict] = []
    for account_id in sorted(grouped):
        account_rows = sorted(
            grouped[account_id],
            key=lambda row: hashlib.sha256(
                f"material-gold-v2.8|{_material_gold_group_key(row)}".encode("utf-8")
            ).hexdigest(),
        )
        audit_count = 0
        if len(account_rows) >= 3:
            audit_count = max(1, min(len(account_rows) - 1, int(round(len(account_rows) * 0.30))))
        account_audit = account_rows[:audit_count]
        account_calibration = account_rows[audit_count:]
        audit_rows.extend(account_audit)
        calibration_rows.extend(account_calibration)
        account_summary.append(
            {
                "account_id": account_id,
                "effective_gold_count": len(account_rows),
                "calibration_count": len(account_calibration),
                "audit_count": len(account_audit),
            }
        )

    calibration_groups = {_material_gold_group_key(row) for row in calibration_rows}
    audit_groups = {_material_gold_group_key(row) for row in audit_rows}
    summary = {
        "policy": "deterministic account-stratified 70/30 split; same-account stable-title variants stay in one group",
        "raw_confirmed_count": len(raw_rows),
        "effective_unique_count": len(unique_rows),
        "collapsed_duplicate_count": max(0, len(raw_rows) - len(unique_rows)),
        "calibration_count": len(calibration_rows),
        "audit_count": len(audit_rows),
        "calibration_account_count": len({str(row.get("account_id") or "unknown") for row in calibration_rows}),
        "audit_account_count": len({str(row.get("account_id") or "unknown") for row in audit_rows}),
        "group_overlap_count": len(calibration_groups & audit_groups),
        "performance_label_used_for_split": False,
        "account_summary": account_summary,
    }
    return {
        "calibration_rows": calibration_rows,
        "audit_rows": audit_rows,
        "summary": summary,
    }


def _material_gold_router_profiles(
    rows: list[dict],
    *,
    account_ids: set[str] | None = None,
    accuracy_mode: str = "strict",
) -> dict[str, dict]:
    global_profile = _material_gold_profile(rows, account_id="__global__")
    accuracy_key = "taxonomy_weighted_accuracy" if accuracy_mode == "canonical_material_type" else "weighted_accuracy"
    global_accuracy = float(global_profile.get(accuracy_key) or 0.0)
    global_profile = {
        **global_profile,
        "calibration_accuracy_mode": accuracy_mode,
        "calibrated_accuracy": round(global_accuracy, 4),
    }
    global_profile["router_status"] = _material_router_status(int(global_profile.get("confirmed_count") or 0), global_accuracy)
    global_profile["router_multiplier"] = _material_router_multiplier(global_profile["router_status"], global_accuracy)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("account_id") or "unknown")].append(row)
    for account_id in account_ids or set():
        grouped.setdefault(str(account_id or "unknown"), [])
    profiles = {"__global__": global_profile}
    for account_id, items in grouped.items():
        local = _material_gold_profile(items, account_id=account_id)
        support = int(local.get("confirmed_count") or 0)
        local_accuracy = float(local.get(accuracy_key) or 0.0)
        shrinkage = min(1.0, support / max(1.0, float(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["account_min_gold_samples"])))
        calibrated_accuracy = local_accuracy * shrinkage + global_accuracy * (1.0 - shrinkage)
        profile = {
            **local,
            "calibration_accuracy_mode": accuracy_mode,
            "global_fallback": support < int(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["account_min_gold_samples"]),
            "calibrated_accuracy": round(calibrated_accuracy, 4),
        }
        profile["router_status"] = _material_router_status(
            support=max(support, int(global_profile.get("confirmed_count") or 0)) if profile["global_fallback"] else support,
            accuracy=calibrated_accuracy,
        )
        profile["router_multiplier"] = _material_router_multiplier(profile["router_status"], calibrated_accuracy)
        profiles[account_id] = profile
    return profiles


def _material_gold_profile(rows: list[dict], *, account_id: str) -> dict:
    field_counts: dict[str, dict[str, int]] = {
        field: {"compared": 0, "matched": 0}
        for field in ["domain_category", "material_type", "presentation_style"]
    }
    taxonomy_counts = {"compared": 0, "canonical_matched": 0, "partial_score_total": 0.0}
    taxonomy_relations: Counter[str] = Counter()
    confirmed_count = 0
    for row in _material_gold_deduplicated_rows(rows):
        gold = _material_gold_from_row(row)
        if not gold:
            continue
        suggestions = _omni_shadow_suggestions(row)
        confirmed_count += 1
        for field in field_counts:
            expected = _known_material_value(gold.get(field))
            predicted = _known_material_value(suggestions.get(field))
            if not expected:
                continue
            field_counts[field]["compared"] += 1
            if expected == predicted:
                field_counts[field]["matched"] += 1
            if field == "material_type":
                relation = _material_type_taxonomy_relation(expected, predicted)
                taxonomy_relations[relation] += 1
                if relation == "not_material_form":
                    continue
                taxonomy_counts["compared"] += 1
                if relation in MATERIAL_TAXONOMY_MATCH_RELATIONS:
                    taxonomy_counts["canonical_matched"] += 1
                taxonomy_counts["partial_score_total"] += float(MATERIAL_TYPE_TAXONOMY_SCORES.get(relation) or 0.0)
    accuracies = {
        field: round(values["matched"] / max(1, values["compared"]), 4) if values["compared"] else 0.0
        for field, values in field_counts.items()
    }
    weighted_values = []
    for field, weight in [("material_type", 0.55), ("domain_category", 0.25), ("presentation_style", 0.20)]:
        if field_counts[field]["compared"]:
            weighted_values.append((accuracies[field], weight))
    total_weight = sum(weight for _, weight in weighted_values)
    weighted_accuracy = sum(value * weight for value, weight in weighted_values) / max(0.0001, total_weight) if weighted_values else 0.0
    canonical_material_accuracy = (
        taxonomy_counts["canonical_matched"] / max(1, taxonomy_counts["compared"])
        if taxonomy_counts["compared"]
        else 0.0
    )
    taxonomy_partial_accuracy = (
        taxonomy_counts["partial_score_total"] / max(1, taxonomy_counts["compared"])
        if taxonomy_counts["compared"]
        else 0.0
    )
    taxonomy_weighted_values = []
    for field, weight in [("material_type", 0.55), ("domain_category", 0.25), ("presentation_style", 0.20)]:
        if not field_counts[field]["compared"]:
            continue
        value = canonical_material_accuracy if field == "material_type" else accuracies[field]
        taxonomy_weighted_values.append((value, weight))
    taxonomy_total_weight = sum(weight for _, weight in taxonomy_weighted_values)
    taxonomy_weighted_accuracy = (
        sum(value * weight for value, weight in taxonomy_weighted_values) / max(0.0001, taxonomy_total_weight)
        if taxonomy_weighted_values
        else 0.0
    )
    status = _material_router_status(confirmed_count, weighted_accuracy)
    return {
        "account_id": account_id,
        "confirmed_count": confirmed_count,
        "field_counts": field_counts,
        "field_accuracy": accuracies,
        "weighted_accuracy": round(weighted_accuracy, 4),
        "canonical_material_type_accuracy": round(canonical_material_accuracy, 4),
        "taxonomy_partial_accuracy": round(taxonomy_partial_accuracy, 4),
        "taxonomy_weighted_accuracy": round(taxonomy_weighted_accuracy, 4),
        "taxonomy_relation_counts": dict(taxonomy_relations),
        "calibrated_accuracy": round(weighted_accuracy, 4),
        "router_status": status,
        "router_multiplier": _material_router_multiplier(status, weighted_accuracy),
        "global_fallback": account_id != "__global__",
    }


def _material_router_status(support: int, accuracy: float) -> str:
    if support >= int(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["trusted_min_gold_samples"]) and accuracy >= float(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["trusted_min_accuracy"]):
        return "trusted"
    if support >= 8 and accuracy < float(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["blocked_max_accuracy"]):
        return "blocked"
    return "neutral"


def _material_router_multiplier(status: str, accuracy: float) -> float:
    if accuracy <= 0:
        return 0.0
    if status == "trusted":
        return round(max(0.65, min(1.0, accuracy)), 4)
    if status == "blocked":
        return 0.0
    return round(max(0.08, min(0.28, accuracy * 0.3)), 4)


def _v28_material_components(row: dict, v27_components: dict[str, float], profiles: dict[str, dict]) -> dict[str, float]:
    account_id = str(row.get("account_id") or "unknown")
    profile = profiles.get(account_id) or profiles.get("__global__") or {}
    status = str(profile.get("router_status") or "neutral")
    status_score = {"trusted": 1.0, "neutral": 0.5, "blocked": 0.0}.get(status, 0.5)
    return {
        "v28_material_gold_available": 1.0 if _material_gold_from_row(row) else 0.0,
        "v28_material_router_status_score": status_score,
        "v28_material_router_multiplier": float(profile.get("router_multiplier") or 0.0),
        "v28_material_router_support": float(profile.get("confirmed_count") or 0),
        "v28_material_router_accuracy": float(profile.get("calibrated_accuracy") or profile.get("weighted_accuracy") or 0.0),
        "v28_material_router_global_fallback": 1.0 if profile.get("global_fallback") else 0.0,
        "v28_material_evidence": float(v27_components.get("v27_material_evidence") or 0.0),
    }


def _score_v28_material(
    base_score: float,
    v27_score: float,
    components: dict[str, float],
    *,
    config: dict[str, Any] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    multiplier = max(0.0, min(1.0, float(components.get("v28_material_router_multiplier") or 0.0)))
    status_score = float(components.get("v28_material_router_status_score") or 0.5)
    delta = float(v27_score) - float(base_score)
    if status_score <= 0.01 or multiplier <= 0:
        return round(clamp(base_score), 4)
    if delta >= 0:
        cap = float(weights.get("trusted_max_boost") if status_score >= 0.99 else weights.get("neutral_max_boost") or 0.0)
        adjusted = min(cap, delta * multiplier)
    else:
        cap = float(weights.get("trusted_max_penalty") if status_score >= 0.99 else weights.get("neutral_max_penalty") or 0.0)
        adjusted = max(-cap, delta * multiplier)
    return round(clamp(base_score + adjusted), 4)


def _v29_material_components(row: dict, v27_components: dict[str, float], profiles: dict[str, dict]) -> dict[str, float]:
    account_id = str(row.get("account_id") or "unknown")
    profile = profiles.get(account_id) or profiles.get("__global__") or {}
    status = str(profile.get("router_status") or "neutral")
    status_score = {"trusted": 1.0, "neutral": 0.5, "blocked": 0.0}.get(status, 0.5)
    suggestions = _omni_shadow_suggestions(row)
    raw_material = _known_material_value(suggestions.get("material_type"))
    canonical_material = _canonical_material_type(raw_material)
    return {
        "v29_material_router_status_score": status_score,
        "v29_material_router_multiplier": float(profile.get("router_multiplier") or 0.0),
        "v29_material_router_support": float(profile.get("confirmed_count") or 0),
        "v29_material_router_accuracy": float(profile.get("calibrated_accuracy") or profile.get("taxonomy_weighted_accuracy") or 0.0),
        "v29_material_router_global_fallback": 1.0 if profile.get("global_fallback") else 0.0,
        "v29_material_evidence": float(v27_components.get("v27_material_evidence") or 0.0),
        "v29_material_canonicalized": 1.0 if raw_material and canonical_material != raw_material else 0.0,
        "v29_material_highlight_detail": 1.0 if raw_material == "performance_highlight" else 0.0,
    }


def _score_v29_material(
    base_score: float,
    v27_score: float,
    components: dict[str, float],
    *,
    config: dict[str, Any] | None = None,
) -> float:
    weights = dict(RESEARCH_RANKER_V29_TAXONOMY_WEIGHT_CONFIG)
    if config:
        weights.update(config)
    multiplier = max(0.0, min(1.0, float(components.get("v29_material_router_multiplier") or 0.0)))
    status_score = float(components.get("v29_material_router_status_score") or 0.5)
    delta = float(v27_score) - float(base_score)
    if status_score <= 0.01 or multiplier <= 0:
        return round(clamp(base_score), 4)
    if delta >= 0:
        cap = float(weights.get("trusted_max_boost") if status_score >= 0.99 else weights.get("neutral_max_boost") or 0.0)
        adjusted = min(cap, delta * multiplier)
    else:
        cap = float(weights.get("trusted_max_penalty") if status_score >= 0.99 else weights.get("neutral_max_penalty") or 0.0)
        adjusted = max(-cap, delta * multiplier)
    return round(clamp(base_score + adjusted), 4)


def _omni_shadow_suggestions(row: dict) -> dict:
    omni = row.get("_omni_shadow") if isinstance(row.get("_omni_shadow"), dict) else row.get("omni_shadow") if isinstance(row.get("omni_shadow"), dict) else {}
    suggestions = omni.get("semantic_suggestions") if isinstance(omni.get("semantic_suggestions"), dict) else {}
    return suggestions if isinstance(suggestions, dict) else {}


def _omni_shadow_field_quality(row: dict) -> dict:
    omni = row.get("_omni_shadow") if isinstance(row.get("_omni_shadow"), dict) else row.get("omni_shadow") if isinstance(row.get("omni_shadow"), dict) else {}
    quality = omni.get("semantic_quality") if isinstance(omni.get("semantic_quality"), dict) else {}
    field_quality = quality.get("field_quality") if isinstance(quality.get("field_quality"), dict) else {}
    return field_quality if isinstance(field_quality, dict) else {}


def _known_material_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text and text not in {"unknown", "none", "null", "其他", "其它"} else ""


def _omni_confidence_score(value: Any) -> float:
    return {
        "high": 1.0,
        "medium": 0.72,
        "low": 0.28,
    }.get(str(value or "").strip().lower(), 0.5)


def _material_compatible_with_category(material: str, category: str) -> bool:
    material = _known_material_value(material)
    category = _known_material_value(category)
    if not material or not category:
        return False
    if material == category:
        return True
    compatibility = {
        "music_variety": {
            "performance_clip",
            "performance_highlight",
            "reaction",
            "commentary",
            "judge_comment",
            "compilation",
            "vocal_teaching",
            "program_context",
            "behind_the_scenes",
        },
        "performance_clip": {
            "performance_clip",
            "performance_highlight",
            "program_context",
            "commentary",
            "reaction",
            "vocal_teaching",
            "entertainment_news",
        },
        "performance_highlight": {"performance_clip", "performance_highlight", "commentary", "reaction"},
        "reaction": {"reaction", "commentary", "judge_comment", "vocal_teaching", "performance_clip"},
        "commentary": {"commentary", "reaction", "judge_comment", "vocal_teaching", "performance_clip"},
        "judge_comment": {"judge_comment", "commentary", "reaction", "performance_clip", "vocal_teaching"},
        "compilation": {"compilation", "entertainment_news"},
        "entertainment_news": {
            "entertainment_news",
            "compilation",
            "humor_entertainment",
            "commentary",
            "reaction",
            "performance_clip",
            "program_context",
        },
        "behind_the_scenes": {"behind_the_scenes", "program_context"},
        "drama_film": {"drama_film"},
        "life_emotion": {"life_emotion", "lifestyle"},
        "lifestyle": {"lifestyle", "life_emotion", "vocal_teaching"},
        "creative_ai": {"creative_ai"},
        "commercial": {"commercial"},
    }
    return material in compatibility.get(category, set())


def _select_v24_signal_gate_score(
    *,
    raw_score: float,
    gated_score: float,
    raw_components: dict[str, float],
    gated_components: dict[str, float],
    signal_quality: dict[str, float],
) -> float:
    raw_evidence = _evidence_quality_from_components(raw_components)
    gated_evidence = _evidence_quality_from_components(gated_components)
    quarantined = float(signal_quality.get("v24_quarantined_signal_count") or 0.0)
    trust = float(signal_quality.get("v24_signal_trust") or 0.0)
    if gated_score >= raw_score:
        return round(clamp(gated_score), 4)
    if quarantined <= 0 or gated_evidence >= raw_evidence * 0.88 or trust >= 76.0:
        return round(clamp(max(gated_score, raw_score - quarantined * 0.04)), 4)
    evidence_gap = max(0.0, raw_evidence - gated_evidence - 0.08)
    penalty = min(0.45, quarantined * 0.08 + evidence_gap * 0.35)
    return round(clamp(raw_score - penalty), 4)


def _evidence_quality_from_components(components: dict[str, float]) -> float:
    high = min(1.0, float(components.get("high_similarity") or 0.0) / 100.0)
    risk = min(1.0, float(components.get("low_interaction_risk") or 0.0) / 100.0)
    prototype = min(1.0, float(components.get("prototype_fit") or 0.0) / 100.0)
    trust = min(1.0, max(0.0, float(components.get("semantic_label_trust") or 0.0) / 100.0))
    similarity = min(1.0, max(0.0, float(components.get("best_similarity") or 0.0)))
    return round(max(similarity, high * 0.45 + risk * 0.35 + prototype * 0.2, trust * 0.35), 4)


def _weight_config_for_strategy(strategy: str) -> dict[str, Any]:
    if strategy in EMBEDDING_RESEARCH_STRATEGIES:
        return {
            "name": "qwen_embedding_research_overlay",
            "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
            "strategy": strategy,
            "text_strategy": TEXT_EMBEDDING_STRATEGY,
            "visual_strategy": VISUAL_EMBEDDING_STRATEGY,
            "text_visual_strategy": TEXT_VISUAL_EMBEDDING_STRATEGY,
            "production_status": "research_only",
        }
    if strategy == RESEARCH_RANKER_V26_POOL_STRATEGY:
        return RESEARCH_RANKER_V26_POOL_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V27_MATERIAL_STRATEGY:
        return RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V28_MATERIAL_STRATEGY:
        return RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V29_TAXONOMY_STRATEGY:
        return RESEARCH_RANKER_V29_TAXONOMY_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V25_SHADOW_STRATEGY:
        return RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V24_STRATEGY:
        return RESEARCH_RANKER_V24_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V23_STRATEGY:
        return RESEARCH_RANKER_V23_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V22_STRATEGY:
        return RESEARCH_RANKER_V22_WEIGHT_CONFIG
    if strategy == RESEARCH_RANKER_V21_STRATEGY:
        return RESEARCH_RANKER_V21_WEIGHT_CONFIG
    return {}


def _candidate_history_rows(
    row: dict,
    target_tokens: set[str],
    train_rows: list[dict],
    history_index: dict[str, Any] | None,
    *,
    limit: int = 360,
) -> list[dict]:
    if len(train_rows) <= limit or not history_index or not target_tokens:
        return train_rows
    indexed_rows = history_index.get("rows") if isinstance(history_index.get("rows"), list) else train_rows
    token_index = history_index.get("token_index") if isinstance(history_index.get("token_index"), dict) else {}
    field_index = history_index.get("field_index") if isinstance(history_index.get("field_index"), dict) else {}
    counts: Counter[int] = Counter()
    max_token_postings = max(80, min(420, len(indexed_rows) // 8))
    for token in sorted(target_tokens):
        postings = token_index.get(token, [])
        if len(postings) > max_token_postings:
            continue
        for index in postings:
            counts[int(index)] += 1
    for field in ["account_id", "content_category", "hook_type", "slice_structure", "artist_names", "song_title", "original_sound_owner", "entity_signal"]:
        key = _index_key(row.get(field))
        if not key:
            continue
        bonus = 3 if field in {"account_id", "content_category", "entity_signal"} else 2
        for index in (field_index.get(field) or {}).get(key, []):
            counts[int(index)] += bonus
    if not counts:
        return []
    ranked = sorted(
        counts.items(),
        key=lambda item: (-item[1], _deterministic_row_key(indexed_rows[item[0]])),
    )[: max(1, int(limit))]
    return [indexed_rows[index] for index, _ in ranked if 0 <= index < len(indexed_rows)]


def _index_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _history_text(row: dict) -> str:
    cached = row.get("_history_text")
    if isinstance(cached, str):
        return cached
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "title",
            "tags",
            "artist_names",
            "song_title",
            "original_sound_owner",
            "entity_signal",
            "program_name",
            "content_category",
            "hook_type",
            "slice_structure",
            "structure_evidence",
        ]
    )


def _history_similarity(target_tokens: set[str], row: dict, sample: dict) -> float:
    cached_tokens = sample.get("_history_tokens")
    sample_tokens = cached_tokens if isinstance(cached_tokens, set) else _history_tokens(_history_text(sample))
    if not target_tokens or not sample_tokens:
        token_score = 0.0
    else:
        overlap = len(target_tokens & sample_tokens)
        target_count = int(row.get("_history_token_count") or len(target_tokens))
        sample_count = int(sample.get("_history_token_count") or len(sample_tokens))
        token_score = overlap / max(1, target_count + sample_count - overlap)
    semantic_bonus = 0.0
    for field, bonus in [
        ("content_category", 0.09),
        ("hook_type", 0.08),
        ("slice_structure", 0.07),
        ("artist_names", 0.05),
        ("song_title", 0.04),
        ("entity_signal", 0.045),
        ("original_sound_owner", 0.035),
    ]:
        row_key = row.get(f"_norm_{field}") or _index_key(row.get(field))
        sample_key = sample.get(f"_norm_{field}") or _index_key(sample.get(field))
        if row_key and sample_key and row_key == sample_key:
            semantic_bonus += bonus
    return min(1.0, token_score + semantic_bonus)


def _history_tokens(text: str) -> set[str]:
    cleaned = str(text or "").lower()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", cleaned)
    tokens = set(words)
    chinese_chars = [word for word in words if len(word) == 1 and "\u4e00" <= word <= "\u9fff"]
    tokens.update("".join(chinese_chars[index : index + 2]) for index in range(max(0, len(chinese_chars) - 1)))
    tokens.update("".join(chinese_chars[index : index + 3]) for index in range(max(0, len(chinese_chars) - 2)))
    return {token for token in tokens if token}


def _historical_prototype_fit(high_matches: list[tuple[float, float, dict]]) -> float:
    if not high_matches:
        return 0.0
    groups: dict[str, list[tuple[float, float, dict]]] = defaultdict(list)
    for item in high_matches:
        _, _, sample = item
        key = "|".join(str(sample.get(field) or "") for field in ["content_category", "hook_type", "slice_structure"]) or str(sample.get("title") or "")[:24]
        groups[key].append(item)
    scores = []
    for items in groups.values():
        avg_similarity = sum(item[0] for item in items) / len(items)
        avg_reward = sum(item[1] for item in items) / len(items)
        scores.append(avg_similarity * avg_reward * (0.75 + min(0.25, len(items) * 0.06)))
    return max(scores) if scores else 0.0


def _historical_semantic_trust(row: dict, matches: list[tuple[float, float, dict]]) -> float:
    confidence = {"manual_verified": 1.0, "high": 0.85, "medium": 0.62, "low": 0.36}.get(
        str(row.get("classification_confidence") or "").lower(),
        0.45,
    )
    if not matches:
        return confidence
    values = [confidence]
    for _, _, sample in matches[:5]:
        values.append(
            {"manual_verified": 1.0, "high": 0.85, "medium": 0.62, "low": 0.36}.get(
                str(sample.get("classification_confidence") or "").lower(),
                0.45,
            )
        )
    return sum(values) / len(values)


def _candidate_count(account_id: str | None) -> int:
    query = """
        SELECT COUNT(*) AS count
        FROM candidate_segments c
        JOIN source_videos v ON v.id = c.source_video_id
    """
    params: list[Any] = []
    if account_id:
        query += " WHERE v.account_id = ?"
        params.append(account_id)
    with connect() as conn:
        row = fetch_one(conn, query, params)
    return int((row or {}).get("count") or 0)


def _strategy_metrics(rows: list[dict], strategy: str, *, k: int) -> dict:
    strategy_rows = _rows_for_strategy(rows, strategy)
    ranked_by_score = _rank_rows(strategy_rows)
    ranked_by_reward = _rank_rows(strategy_rows, primary="normalized_reward", fallback="reward_proxy")
    return {
        "strategy": strategy,
        "sample_count": len(strategy_rows),
        "k": max(1, int(k or 10)),
        "ndcg_at_k": _ndcg_at_k(ranked_by_score, k),
        "topk_hit_rate": _topk_hit_rate(ranked_by_score, ranked_by_reward, k),
        "topk_lift_vs_random": _topk_lift_vs_random(ranked_by_score, strategy_rows, k),
        "high_interaction_hit_rate": _high_interaction_hit_rate(ranked_by_score, strategy_rows, k),
        "low_interaction_avoidance_rate": _low_interaction_avoidance_rate(ranked_by_score, strategy_rows, k),
        "calibration_mae": _calibration_mae(strategy_rows),
    }


def _rows_for_strategy(rows: list[dict], strategy: str) -> list[dict]:
    cloned = []
    for row in rows:
        item = dict(row)
        scores = item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {}
        item["final_score"] = float(scores.get(strategy, item.get("final_score") or 0))
        cloned.append(item)
    return cloned


def _rows_with_v21_config(rows: list[dict], config: dict[str, Any]) -> list[dict]:
    cloned = []
    for row in rows:
        item = dict(row)
        scores = dict(item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {})
        components = item.get("component_scores") if isinstance(item.get("component_scores"), dict) else {}
        scores[RESEARCH_RANKER_V21_STRATEGY] = _score_v21_from_components(components, config=config)
        item["strategy_scores"] = scores
        item["final_score"] = scores[RESEARCH_RANKER_V21_STRATEGY]
        cloned.append(item)
    return cloned


def _rows_with_v22_config(rows: list[dict], config: dict[str, Any]) -> list[dict]:
    cloned = []
    for row in rows:
        item = dict(row)
        scores = dict(item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {})
        components = item.get("component_scores") if isinstance(item.get("component_scores"), dict) else {}
        scores[RESEARCH_RANKER_V22_STRATEGY] = _score_v22_from_components(components, config=config)
        item["strategy_scores"] = scores
        item["final_score"] = scores[RESEARCH_RANKER_V22_STRATEGY]
        cloned.append(item)
    return cloned


def _rows_with_v23_config(rows: list[dict], config: dict[str, Any]) -> list[dict]:
    cloned = []
    for row in rows:
        item = dict(row)
        scores = dict(item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {})
        components = item.get("component_scores") if isinstance(item.get("component_scores"), dict) else {}
        scores[RESEARCH_RANKER_V23_STRATEGY] = _score_v23_from_components(components, row=item, config=config)
        item["strategy_scores"] = scores
        item["final_score"] = scores[RESEARCH_RANKER_V23_STRATEGY]
        cloned.append(item)
    tuned = _apply_v23_diversity(cloned, config=config)
    for item in tuned:
        scores = item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {}
        item["final_score"] = float(scores.get(RESEARCH_RANKER_V23_STRATEGY, item.get("final_score") or 0.0))
    return tuned


def _rows_with_v24_config(rows: list[dict], config: dict[str, Any]) -> list[dict]:
    cloned = []
    for row in rows:
        item = dict(row)
        scores = dict(item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {})
        components = item.get("v24_component_scores") if isinstance(item.get("v24_component_scores"), dict) else {}
        if not components:
            components = item.get("component_scores") if isinstance(item.get("component_scores"), dict) else {}
        signal_quality = {
            key: float((item.get("component_scores") or {}).get(key) or 0.0)
            for key in [
                "v24_signal_trust",
                "v24_trusted_signal_count",
                "v24_quarantined_signal_count",
                "v24_evidence_quality",
            ]
        }
        gated_score = _score_v24_from_components(
            components,
            row=item,
            config=config,
            signal_quality=signal_quality,
        )
        raw_components = item.get("component_scores") if isinstance(item.get("component_scores"), dict) else {}
        scores[RESEARCH_RANKER_V24_STRATEGY] = _select_v24_signal_gate_score(
            raw_score=float(scores.get(RESEARCH_RANKER_V23_STRATEGY) or item.get("final_score") or 0.0),
            gated_score=gated_score,
            raw_components=raw_components,
            gated_components=components,
            signal_quality=signal_quality,
        )
        item["strategy_scores"] = scores
        item["final_score"] = scores[RESEARCH_RANKER_V24_STRATEGY]
        cloned.append(item)
    tuned = _apply_v24_diversity(cloned, config=config)
    for item in tuned:
        scores = item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {}
        item["final_score"] = float(scores.get(RESEARCH_RANKER_V24_STRATEGY, item.get("final_score") or 0.0))
    return tuned


def _apply_v23_diversity(rows: list[dict], *, config: dict[str, Any] | None = None) -> list[dict]:
    return _apply_strategy_diversity(
        rows,
        strategy=RESEARCH_RANKER_V23_STRATEGY,
        default_config=RESEARCH_RANKER_V23_WEIGHT_CONFIG,
        component_key="v23_diversity_penalty",
        config=config,
    )


def _apply_v24_diversity(rows: list[dict], *, config: dict[str, Any] | None = None) -> list[dict]:
    return _apply_strategy_diversity(
        rows,
        strategy=RESEARCH_RANKER_V24_STRATEGY,
        default_config=RESEARCH_RANKER_V24_WEIGHT_CONFIG,
        component_key="v24_diversity_penalty",
        config=config,
    )


def _apply_v25_shadow_diversity(rows: list[dict], *, config: dict[str, Any] | None = None) -> list[dict]:
    return _apply_strategy_diversity(
        rows,
        strategy=RESEARCH_RANKER_V25_SHADOW_STRATEGY,
        default_config=RESEARCH_RANKER_V25_SHADOW_WEIGHT_CONFIG,
        component_key="v25_shadow_diversity_penalty",
        config=config,
    )


def _apply_v26_pool_diversity(rows: list[dict], *, config: dict[str, Any] | None = None) -> list[dict]:
    return _apply_strategy_diversity(
        rows,
        strategy=RESEARCH_RANKER_V26_POOL_STRATEGY,
        default_config=RESEARCH_RANKER_V26_POOL_WEIGHT_CONFIG,
        component_key="v26_pool_diversity_penalty",
        config=config,
    )


def _apply_v27_material_diversity(rows: list[dict], *, config: dict[str, Any] | None = None) -> list[dict]:
    return _apply_strategy_diversity(
        rows,
        strategy=RESEARCH_RANKER_V27_MATERIAL_STRATEGY,
        default_config=RESEARCH_RANKER_V27_MATERIAL_WEIGHT_CONFIG,
        component_key="v27_material_diversity_penalty",
        config=config,
    )


def _apply_v28_material_diversity(rows: list[dict], *, config: dict[str, Any] | None = None) -> list[dict]:
    return _apply_strategy_diversity(
        rows,
        strategy=RESEARCH_RANKER_V28_MATERIAL_STRATEGY,
        default_config=RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG,
        component_key="v28_material_diversity_penalty",
        config=config,
    )


def _apply_v29_material_diversity(rows: list[dict], *, config: dict[str, Any] | None = None) -> list[dict]:
    return _apply_strategy_diversity(
        rows,
        strategy=RESEARCH_RANKER_V29_TAXONOMY_STRATEGY,
        default_config=RESEARCH_RANKER_V29_TAXONOMY_WEIGHT_CONFIG,
        component_key="v29_material_diversity_penalty",
        config=config,
    )


def _apply_strategy_diversity(
    rows: list[dict],
    *,
    strategy: str,
    default_config: dict[str, Any],
    component_key: str,
    config: dict[str, Any] | None = None,
) -> list[dict]:
    weights = dict(default_config)
    if config:
        weights.update(config)
    ordered = sorted(
        rows,
        key=lambda row: (
            -float((row.get("strategy_scores") or {}).get(strategy, row.get("final_score") or 0.0)),
            _deterministic_row_key(row),
        ),
    )
    title_seen: Counter[str] = Counter()
    song_seen: Counter[str] = Counter()
    artist_seen: Counter[str] = Counter()
    category_seen: Counter[str] = Counter()
    adjusted_by_id: dict[str, tuple[float, float]] = {}
    for row in ordered:
        scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
        base = float(scores.get(strategy, row.get("final_score") or 0.0))
        title_key = _stable_title_key(row.get("title"))
        song_key = _diversity_key(row.get("song_title"))
        artist_key = _diversity_key(row.get("artist_names"))
        category_key = _diversity_key(row.get("content_category"))
        penalty = 0.0
        if title_key:
            penalty += title_seen[title_key] * float(weights.get("title_diversity_penalty") or 0.0)
        if song_key:
            penalty += min(3, song_seen[song_key]) * float(weights.get("song_diversity_penalty") or 0.0)
        if artist_key:
            penalty += min(3, artist_seen[artist_key]) * float(weights.get("artist_diversity_penalty") or 0.0)
        if category_key:
            penalty += min(4, category_seen[category_key]) * float(weights.get("category_diversity_penalty") or 0.0)
        key = _deterministic_row_key(row)
        adjusted_by_id[key] = (round(clamp(base - penalty), 4), round(penalty, 4))
        if title_key:
            title_seen[title_key] += 1
        if song_key:
            song_seen[song_key] += 1
        if artist_key:
            artist_seen[artist_key] += 1
        if category_key:
            category_seen[category_key] += 1
    adjusted_rows = []
    for row in rows:
        item = dict(row)
        key = _deterministic_row_key(item)
        adjusted, penalty = adjusted_by_id.get(key, (float((item.get("strategy_scores") or {}).get(strategy, item.get("final_score") or 0.0)), 0.0))
        scores = dict(item.get("strategy_scores") if isinstance(item.get("strategy_scores"), dict) else {})
        scores[strategy] = adjusted
        item["strategy_scores"] = scores
        if isinstance(item.get("component_scores"), dict):
            item["component_scores"] = {
                **item["component_scores"],
                component_key: penalty,
            }
        adjusted_rows.append(item)
    return adjusted_rows


def _diversity_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"unknown", "none", "null"}:
        return ""
    parts = [part.strip() for part in re.split(r"[|,，/、\s]+", text) if part.strip()]
    return "|".join(sorted(set(parts)))[:100] if parts else text[:100]


def _unknown_core_semantic_count(row: dict) -> int:
    return sum(
        1
        for field in ["content_category", "hook_type", "slice_structure"]
        if str(row.get(field) or "").strip().lower() in {"", "unknown", "none", "null"}
    )


def _per_account_metrics(rows: list[dict], strategy: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("account_id") or "unknown")].append(row)
    result = []
    for account, items in grouped.items():
        k = max(1, min(10, len(items)))
        current = _strategy_metrics(items, "current_rules", k=k)
        selected = _strategy_metrics(items, strategy, k=k)
        result.append(
            {
                "account_id": account,
                "sample_count": len(items),
                "status": "ready" if len(items) >= 10 else "low_confidence",
                "strategy": strategy,
                "topk_lift_vs_random": selected["topk_lift_vs_random"],
                "high_interaction_hit_rate": selected["high_interaction_hit_rate"],
                "current_rules_high_interaction_hit_rate": current["high_interaction_hit_rate"],
                "current_rules_topk_lift_vs_random": current["topk_lift_vs_random"],
                "improved_vs_current_rules": selected["high_interaction_hit_rate"] > current["high_interaction_hit_rate"]
                or selected["topk_lift_vs_random"] > current["topk_lift_vs_random"],
            }
        )
    result.sort(key=lambda item: (item["status"] == "ready", item["sample_count"], item["topk_lift_vs_random"]), reverse=True)
    return result


def _promotion_gate(
    strategy_comparison: dict,
    per_account_metrics: list[dict],
    *,
    strategy: str = RESEARCH_RANKER_V24_STRATEGY,
) -> dict:
    target = strategy_comparison.get(strategy) or strategy_comparison.get("research_ranker_v2") or {}
    if strategy == "current_rules":
        return {
            "passed": True,
            "status": "production_baseline",
            "strategy": strategy,
            "topk_lift_vs_random": round(float(target.get("topk_lift_vs_random") or 0.0), 4),
            "high_interaction_hit_rate": round(float(target.get("high_interaction_hit_rate") or 0.0), 4),
            "low_interaction_avoidance_rate": round(float(target.get("low_interaction_avoidance_rate") or 0.0), 4),
            "decision": "keep_current_rules_as_production_baseline",
            "automatic_promotion": False,
            "note": "current_rules 是已采用基线；研究策略只有通过冻结门禁并显式变更策略后才能替代它。",
        }
    if strategy in {RESEARCH_RANKER_V28_MATERIAL_STRATEGY, RESEARCH_RANKER_V29_TAXONOMY_STRATEGY}:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        ready_improved = [item for item in per_account_metrics if item.get("status") == "ready" and item.get("improved_vs_current_rules")]
        research_gate = lift_delta >= 0.03 and high_delta >= 0.05 and low_delta >= -0.0001
        taxonomy_mode = strategy == RESEARCH_RANKER_V29_TAXONOMY_STRATEGY
        return {
            "passed": False,
            "material_calibration_gate_passed": research_gate,
            "status": "material_taxonomy_research_only" if taxonomy_mode else "material_calibration_research_only",
            "strategy": strategy,
            "topk_lift_vs_random": round(float(target.get("topk_lift_vs_random") or 0.0), 4),
            "high_interaction_hit_rate": round(float(target.get("high_interaction_hit_rate") or 0.0), 4),
            "low_interaction_avoidance_rate": round(float(target.get("low_interaction_avoidance_rate") or 0.0), 4),
            "required_lift_delta_vs_v2_4": 0.03,
            "required_high_hit_delta_vs_v2_4": 0.05,
            "required_low_avoidance_delta_vs_v2_4": 0.0,
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "improved_ready_account_count": len(ready_improved),
            "decision": "keep_as_material_taxonomy_research" if taxonomy_mode else "keep_as_material_calibration_research",
            "note": (
                "v2.9 只使用 canonical 素材形态可信度；原始 Gold 标签与细粒度准确率保持不变。"
                if taxonomy_mode
                else "v2.8 使用独立 Gold 校准子集汇总可信度，并从性能验证行排除校准样本；审计子集只用于质量门槛。"
            ),
        }
    if strategy == RESEARCH_RANKER_V26_POOL_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        ready_improved = [
            item
            for item in per_account_metrics
            if item.get("status") == "ready" and item.get("improved_vs_current_rules")
        ]
        return {
            "passed": False,
            "pool_gate_passed": lift_delta >= 0.05 and high_delta >= 0.08 and low_delta >= -0.0001,
            "status": "pool_research_only",
            "strategy": strategy,
            "topk_lift_vs_random": round(float(target.get("topk_lift_vs_random") or 0.0), 4),
            "high_interaction_hit_rate": round(float(target.get("high_interaction_hit_rate") or 0.0), 4),
            "low_interaction_avoidance_rate": round(float(target.get("low_interaction_avoidance_rate") or 0.0), 4),
            "required_lift_delta_vs_v2_4": 0.05,
            "required_high_hit_delta_vs_v2_4": 0.08,
            "required_low_avoidance_delta_vs_v2_4": 0.0,
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "improved_ready_account_count": len(ready_improved),
            "decision": "keep_top30_pool_as_research_evidence",
            "note": "v2.6 只作为 Omni Top30 扩池研究门控，不替代 v2.4 Top10 最终排序。",
        }
    if strategy == RESEARCH_RANKER_V27_MATERIAL_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        ready_improved = [
            item
            for item in per_account_metrics
            if item.get("status") == "ready" and item.get("improved_vs_current_rules")
        ]
        research_gate = lift_delta >= 0.03 and high_delta >= 0.05 and low_delta >= -0.0001
        return {
            "passed": False,
            "material_gate_passed": research_gate,
            "status": "material_research_only",
            "strategy": strategy,
            "topk_lift_vs_random": round(float(target.get("topk_lift_vs_random") or 0.0), 4),
            "high_interaction_hit_rate": round(float(target.get("high_interaction_hit_rate") or 0.0), 4),
            "low_interaction_avoidance_rate": round(float(target.get("low_interaction_avoidance_rate") or 0.0), 4),
            "required_lift_delta_vs_v2_4": 0.03,
            "required_high_hit_delta_vs_v2_4": 0.05,
            "required_low_avoidance_delta_vs_v2_4": 0.0,
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "improved_ready_account_count": len(ready_improved),
            "decision": "keep_as_material_shadow_research",
            "note": "v2.7 只验证 Omni material_type 路由，不写人工标签、不替代 v2.4。",
        }
    if strategy == RESEARCH_RANKER_V25_SHADOW_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        ready_improved = [
            item
            for item in per_account_metrics
            if item.get("status") == "ready" and item.get("improved_vs_current_rules")
        ]
        return {
            "passed": False,
            "research_gate_passed": lift_delta >= 0.02 and high_delta >= -0.0001 and low_delta >= -0.0001,
            "status": "research_only",
            "strategy": strategy,
            "topk_lift_vs_random": round(float(target.get("topk_lift_vs_random") or 0.0), 4),
            "high_interaction_hit_rate": round(float(target.get("high_interaction_hit_rate") or 0.0), 4),
            "low_interaction_avoidance_rate": round(float(target.get("low_interaction_avoidance_rate") or 0.0), 4),
            "required_lift_delta_vs_v2_4": 0.02,
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "improved_ready_account_count": len(ready_improved),
            "decision": "keep_as_shadow_research_evidence",
            "note": "v2.5 只读取 Omni shadow 缓存做研究回测，不写人工标签，也不替代 v2.4 生产权重。",
        }
    if strategy in EMBEDDING_RESEARCH_STRATEGIES:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        return {
            "passed": False,
            "research_gate_passed": lift_delta >= 0.02 and low_delta >= -0.0001,
            "status": "research_only",
            "strategy": strategy,
            "topk_lift_vs_random": round(float(target.get("topk_lift_vs_random") or 0.0), 4),
            "high_interaction_hit_rate": round(float(target.get("high_interaction_hit_rate") or 0.0), 4),
            "low_interaction_avoidance_rate": round(float(target.get("low_interaction_avoidance_rate") or 0.0), 4),
            "required_lift_delta_vs_v2_4": 0.02,
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "decision": "keep_as_research_evidence",
            "note": "Qwen embedding 策略只作为研究证据对比，不替代 v2.4 生产权重。",
        }
    ready_improved = [
        item
        for item in per_account_metrics
        if item.get("status") == "ready" and item.get("improved_vs_current_rules")
    ]
    topk_lift = float(target.get("topk_lift_vs_random") or 0.0)
    high_hit = float(target.get("high_interaction_hit_rate") or 0.0)
    low_avoidance = float(target.get("low_interaction_avoidance_rate") or 0.0)
    if strategy in {RESEARCH_RANKER_V22_STRATEGY, RESEARCH_RANKER_V23_STRATEGY, RESEARCH_RANKER_V24_STRATEGY}:
        required_lift = float(RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_topk_lift_vs_random"])
        required_high_hit = float(RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_high_interaction_hit_rate"])
        required_low_avoidance = float(RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_low_interaction_avoidance_rate"])
        required_accounts = int(RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_improved_ready_account_count"])
    elif strategy == RESEARCH_RANKER_V21_STRATEGY:
        required_lift = 1.70
        required_high_hit = 0.70
        required_low_avoidance = 0.0
        required_accounts = 8
    else:
        required_lift = 1.10
        required_high_hit = 0.0
        required_low_avoidance = 0.0
        required_accounts = 3
    baseline_guard = _production_baseline_guard(strategy_comparison, target)
    threshold_gate_passed = (
        topk_lift >= required_lift
        and high_hit >= required_high_hit
        and low_avoidance >= required_low_avoidance
        and len(ready_improved) >= required_accounts
    )
    passed = threshold_gate_passed and bool(baseline_guard.get("passed"))
    return {
        "passed": passed,
        "status": "eligible_for_promotion" if passed else "research_only",
        "strategy": strategy,
        "topk_lift_vs_random": round(topk_lift, 4),
        "high_interaction_hit_rate": round(high_hit, 4),
        "low_interaction_avoidance_rate": round(low_avoidance, 4),
        "required_topk_lift_vs_random": required_lift,
        "required_high_interaction_hit_rate": required_high_hit,
        "required_low_interaction_avoidance_rate": required_low_avoidance,
        "improved_ready_account_count": len(ready_improved),
        "required_improved_ready_account_count": required_accounts,
        "threshold_gate_passed": threshold_gate_passed,
        "baseline_guard": baseline_guard,
        "decision": "eligible_for_explicit_production_promotion" if passed else "keep_as_research_evidence",
        "automatic_promotion": False,
        "note": "门禁通过也不会自动改写生产排序；必须冻结新基准并显式更新 production ranking policy。",
    }


def _production_baseline_guard(strategy_comparison: dict, target: dict) -> dict:
    baselines = {
        name: strategy_comparison.get(name) or {}
        for name in ["current_rules", "semantic_baseline_v2"]
        if isinstance(strategy_comparison.get(name), dict)
    }
    strongest_lift_strategy = max(
        baselines,
        key=lambda name: float((baselines.get(name) or {}).get("topk_lift_vs_random") or 0.0),
        default="missing",
    )
    strongest = {
        metric: max((float(item.get(metric) or 0.0) for item in baselines.values()), default=0.0)
        for metric in [
            "topk_lift_vs_random",
            "ndcg_at_k",
            "high_interaction_hit_rate",
            "low_interaction_avoidance_rate",
        ]
    }
    deltas = {
        metric: round(float(target.get(metric) or 0.0) - baseline, 4)
        for metric, baseline in strongest.items()
    }
    required = {
        "topk_lift_vs_random": float(
            RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_lift_delta_vs_strongest_baseline"]
        ),
        "ndcg_at_k": float(
            RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_ndcg_delta_vs_strongest_baseline"]
        ),
        "high_interaction_hit_rate": float(
            RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_high_hit_delta_vs_strongest_baseline"]
        ),
        "low_interaction_avoidance_rate": float(
            RESEARCH_RANKER_PROMOTION_THRESHOLDS["required_low_avoidance_delta_vs_strongest_baseline"]
        ),
    }
    checks = {metric: deltas[metric] >= threshold for metric, threshold in required.items()}
    return {
        "passed": bool(baselines) and all(checks.values()),
        "baseline_strategies": sorted(baselines),
        "strongest_lift_strategy": strongest_lift_strategy,
        "strongest_metric_values": {key: round(value, 4) for key, value in strongest.items()},
        "target_deltas": deltas,
        "required_deltas": required,
        "checks": checks,
    }


def _baseline_gap(strategy_comparison: dict, strategy: str) -> dict:
    target = strategy_comparison.get(strategy) or {}
    semantic = strategy_comparison.get("semantic_baseline_v2") or {}
    current = strategy_comparison.get("current_rules") or {}
    return {
        "strategy": strategy,
        "lift_vs_semantic_baseline": round(
            float(target.get("topk_lift_vs_random") or 0) - float(semantic.get("topk_lift_vs_random") or 0),
            4,
        ),
        "high_hit_vs_semantic_baseline": round(
            float(target.get("high_interaction_hit_rate") or 0) - float(semantic.get("high_interaction_hit_rate") or 0),
            4,
        ),
        "lift_vs_current_rules": round(
            float(target.get("topk_lift_vs_random") or 0) - float(current.get("topk_lift_vs_random") or 0),
            4,
        ),
        "high_hit_vs_current_rules": round(
            float(target.get("high_interaction_hit_rate") or 0) - float(current.get("high_interaction_hit_rate") or 0),
            4,
        ),
    }


def _calibration_summary(strategy_comparison: dict, promotion_gate: dict, strategy: str) -> dict:
    target = strategy_comparison.get(strategy) or {}
    semantic = strategy_comparison.get("semantic_baseline_v2") or {}
    if strategy in {RESEARCH_RANKER_V28_MATERIAL_STRATEGY, RESEARCH_RANKER_V29_TAXONOMY_STRATEGY}:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        status = "material_taxonomy_research_only" if strategy == RESEARCH_RANKER_V29_TAXONOMY_STRATEGY else "material_calibration_research_only"
        return {
            "strategy": strategy,
            "status": status,
            "semantic_baseline_ahead": float(semantic.get("topk_lift_vs_random") or 0) > float(target.get("topk_lift_vs_random") or 0),
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0),
            "target_high_interaction_hit_rate": float(target.get("high_interaction_hit_rate") or 0),
            "required_lift_delta_vs_v2_4": 0.03,
            "required_high_hit_delta_vs_v2_4": 0.05,
            "lift_delta_vs_v2_4": round(float(target.get("topk_lift_vs_random") or 0) - float(base.get("topk_lift_vs_random") or 0), 4),
            "high_hit_delta_vs_v2_4": round(float(target.get("high_interaction_hit_rate") or 0) - float(base.get("high_interaction_hit_rate") or 0), 4),
            "production_status": status,
        }
    if strategy == RESEARCH_RANKER_V26_POOL_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        return {
            "strategy": strategy,
            "status": "pool_research_only",
            "semantic_baseline_ahead": float(semantic.get("topk_lift_vs_random") or 0) > float(target.get("topk_lift_vs_random") or 0),
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0),
            "target_high_interaction_hit_rate": float(target.get("high_interaction_hit_rate") or 0),
            "required_lift_delta_vs_v2_4": 0.05,
            "required_high_hit_delta_vs_v2_4": 0.08,
            "lift_delta_vs_v2_4": round(float(target.get("topk_lift_vs_random") or 0) - float(base.get("topk_lift_vs_random") or 0), 4),
            "production_status": "pool_research_only",
        }
    if strategy == RESEARCH_RANKER_V27_MATERIAL_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        return {
            "strategy": strategy,
            "status": "material_research_only",
            "semantic_baseline_ahead": float(semantic.get("topk_lift_vs_random") or 0) > float(target.get("topk_lift_vs_random") or 0),
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0),
            "target_high_interaction_hit_rate": float(target.get("high_interaction_hit_rate") or 0),
            "required_lift_delta_vs_v2_4": 0.03,
            "required_high_hit_delta_vs_v2_4": 0.05,
            "lift_delta_vs_v2_4": round(float(target.get("topk_lift_vs_random") or 0) - float(base.get("topk_lift_vs_random") or 0), 4),
            "high_hit_delta_vs_v2_4": round(float(target.get("high_interaction_hit_rate") or 0) - float(base.get("high_interaction_hit_rate") or 0), 4),
            "production_status": "material_research_only",
        }
    if strategy == RESEARCH_RANKER_V25_SHADOW_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        return {
            "strategy": strategy,
            "status": "research_only",
            "semantic_baseline_ahead": float(semantic.get("topk_lift_vs_random") or 0) > float(target.get("topk_lift_vs_random") or 0),
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0),
            "target_high_interaction_hit_rate": float(target.get("high_interaction_hit_rate") or 0),
            "required_lift_delta_vs_v2_4": 0.02,
            "lift_delta_vs_v2_4": round(float(target.get("topk_lift_vs_random") or 0) - float(base.get("topk_lift_vs_random") or 0), 4),
            "production_status": "research_only",
        }
    if strategy in EMBEDDING_RESEARCH_STRATEGIES:
        return {
            "strategy": strategy,
            "status": "research_only",
            "semantic_baseline_ahead": float(semantic.get("topk_lift_vs_random") or 0) > float(target.get("topk_lift_vs_random") or 0),
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0),
            "target_high_interaction_hit_rate": float(target.get("high_interaction_hit_rate") or 0),
            "required_lift_delta_vs_v2_4": 0.02,
            "production_status": "research_only",
        }
    return {
        "strategy": strategy,
        "status": "eligible_for_stronger_weight" if promotion_gate.get("passed") else "research_only",
        "semantic_baseline_ahead": float(semantic.get("topk_lift_vs_random") or 0) > float(target.get("topk_lift_vs_random") or 0),
        "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0),
        "target_high_interaction_hit_rate": float(target.get("high_interaction_hit_rate") or 0),
        "required_topk_lift_vs_random": 1.85 if strategy in {RESEARCH_RANKER_V22_STRATEGY, RESEARCH_RANKER_V23_STRATEGY, RESEARCH_RANKER_V24_STRATEGY} else 1.70 if strategy == RESEARCH_RANKER_V21_STRATEGY else 1.10,
        "required_high_interaction_hit_rate": 0.90 if strategy in {RESEARCH_RANKER_V22_STRATEGY, RESEARCH_RANKER_V23_STRATEGY, RESEARCH_RANKER_V24_STRATEGY} else 0.70 if strategy == RESEARCH_RANKER_V21_STRATEGY else 0.0,
        "required_low_interaction_avoidance_rate": 0.95 if strategy in {RESEARCH_RANKER_V22_STRATEGY, RESEARCH_RANKER_V23_STRATEGY, RESEARCH_RANKER_V24_STRATEGY} else 0.0,
    }


def _semantic_gap_analysis(strategy_comparison: dict, strategy: str) -> dict:
    gap = _baseline_gap(strategy_comparison, strategy)
    target = strategy_comparison.get(strategy) or {}
    semantic = strategy_comparison.get("semantic_baseline_v2") or {}
    if strategy == RESEARCH_RANKER_V26_POOL_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        passed = lift_delta >= 0.05 and high_delta >= 0.08 and low_delta >= -0.0001
        return {
            "strategy": strategy,
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0.0),
            "semantic_topk_lift_vs_random": float(semantic.get("topk_lift_vs_random") or 0.0),
            "lift_gap": round(float(gap.get("lift_vs_semantic_baseline") or 0.0), 4),
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "required_lift_delta_vs_v2_4": 0.05,
            "required_high_hit_delta_vs_v2_4": 0.08,
            "passed": passed,
            "status": "positive_pool_signal" if passed else "pool_research_only",
        }
    if strategy == RESEARCH_RANKER_V27_MATERIAL_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        passed = lift_delta >= 0.03 and high_delta >= 0.05 and low_delta >= -0.0001
        return {
            "strategy": strategy,
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0.0),
            "semantic_topk_lift_vs_random": float(semantic.get("topk_lift_vs_random") or 0.0),
            "lift_gap": round(float(gap.get("lift_vs_semantic_baseline") or 0.0), 4),
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "required_lift_delta_vs_v2_4": 0.03,
            "required_high_hit_delta_vs_v2_4": 0.05,
            "passed": passed,
            "status": "positive_material_signal" if passed else "material_research_only",
        }
    if strategy == RESEARCH_RANKER_V25_SHADOW_STRATEGY:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        return {
            "strategy": strategy,
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0.0),
            "semantic_topk_lift_vs_random": float(semantic.get("topk_lift_vs_random") or 0.0),
            "lift_gap": round(float(gap.get("lift_vs_semantic_baseline") or 0.0), 4),
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "required_lift_delta_vs_v2_4": 0.02,
            "passed": lift_delta >= 0.02,
            "status": "positive_shadow_signal" if lift_delta >= 0.02 else "research_only",
        }
    if strategy in EMBEDDING_RESEARCH_STRATEGIES:
        base = strategy_comparison.get(RESEARCH_RANKER_V24_STRATEGY) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        return {
            "strategy": strategy,
            "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0.0),
            "semantic_topk_lift_vs_random": float(semantic.get("topk_lift_vs_random") or 0.0),
            "lift_gap": round(float(gap.get("lift_vs_semantic_baseline") or 0.0), 4),
            "lift_delta_vs_v2_4": round(lift_delta, 4),
            "required_lift_delta_vs_v2_4": 0.02,
            "passed": lift_delta >= 0.02,
            "status": "positive_research_signal" if lift_delta >= 0.02 else "research_only",
        }
    required_gap = 0.03 if strategy in {RESEARCH_RANKER_V22_STRATEGY, RESEARCH_RANKER_V23_STRATEGY, RESEARCH_RANKER_V24_STRATEGY} else 0.0
    lift_gap = float(gap.get("lift_vs_semantic_baseline") or 0.0)
    return {
        "strategy": strategy,
        "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0.0),
        "semantic_topk_lift_vs_random": float(semantic.get("topk_lift_vs_random") or 0.0),
        "lift_gap": round(lift_gap, 4),
        "required_lift_gap": required_gap,
        "passed": lift_gap >= required_gap,
        "status": "ahead_of_semantic_baseline" if lift_gap >= required_gap else "research_only",
    }


def _semantic_feature_coverage(rows: list[dict]) -> dict[str, dict[str, Any]]:
    fields = [
        "content_category",
        "hook_type",
        "slice_structure",
        "artist_names",
        "song_title",
        "original_sound_owner",
        "entity_signal",
        "structure_confidence",
    ]
    coverage = {}
    total = len(rows)
    for field in fields:
        known = sum(1 for row in rows if _known_feature_value(row.get(field)))
        coverage[field] = {
            "count": known,
            "total": total,
            "rate": round(known / max(1, total), 4),
        }
    return coverage


def _semantic_feature_diagnosis(base_metrics: dict, mask_results: list[dict], rows: list[dict]) -> dict:
    lift = float(base_metrics.get("topk_lift_vs_random") or 0.0)
    strongest = sorted(
        mask_results,
        key=lambda item: float(item.get("lift_delta_vs_full") or 0.0),
    )[:3]
    noisy = sorted(
        [
            item
            for item in mask_results
            if float(item.get("lift_delta_vs_full") or 0.0) > 0
        ],
        key=lambda item: float(item.get("lift_delta_vs_full") or 0.0),
        reverse=True,
    )
    weak_fields = [
        field
        for field, item in _semantic_feature_coverage(rows).items()
        if field in {"hook_type", "slice_structure", "content_category"} and float(item.get("rate") or 0.0) < 0.5
    ]
    return {
        "promotion_gap_to_1_85": round(1.85 - lift, 4),
        "strongest_positive_evidence_fields": [
            {
                "name": item.get("name"),
                "fields": item.get("fields") or [],
                "lift_loss_when_masked": round(abs(float(item.get("lift_delta_vs_full") or 0.0)), 4),
            }
            for item in strongest
            if float(item.get("lift_delta_vs_full") or 0.0) < 0
        ],
        "possibly_noisy_fields": [
            {
                "name": item.get("name"),
                "fields": item.get("fields") or [],
                "lift_gain_when_masked": round(float(item.get("lift_delta_vs_full") or 0.0), 4),
            }
            for item in noisy[:3]
        ],
        "low_coverage_core_fields": weak_fields,
        "recommendation": "prioritize fields that lose lift when masked; quarantine fields that gain lift when masked until manual calibration proves them reliable.",
    }


def _known_feature_value(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and text not in {"unknown", "none", "null", "其他", "其它", "0"})


def _diagnostic_samples(rows: list[dict], strategy: str, *, k: int) -> dict:
    if not rows:
        return {
            "missed_high_interaction": [],
            "low_interaction_false_positive": [],
            "semantic_disagreements": [],
            "diversity_limited_duplicates": [],
        }
    thresholds = _interaction_thresholds(rows)
    ranked = _rows_for_strategy(rows, strategy)
    ranked = _rank_rows(ranked)
    top_ids = {row.get("training_sample_id") for row in ranked[: max(1, int(k or 10))]}
    high_rows = [
        row for row in rows if _interaction_label(row, thresholds) == "high" and row.get("training_sample_id") not in top_ids
    ]
    high_rows = _rank_rows(high_rows, primary="normalized_reward", fallback="reward_proxy")
    low_false = [row for row in ranked[: max(1, int(k or 10))] if _interaction_label(row, thresholds) == "low"]
    disagreement_rows = []
    for row in rows:
        scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
        target = float(scores.get(strategy) or row.get("final_score") or 0.0)
        semantic = float(scores.get("semantic_baseline_v2") or 0.0)
        delta = abs(target - semantic)
        if delta >= 0.35:
            item = dict(row)
            item["_semantic_delta_abs"] = delta
            disagreement_rows.append(item)
    disagreement_rows = _rank_rows(disagreement_rows, primary="_semantic_delta_abs")
    return {
        "missed_high_interaction": [_diagnostic_row(row, strategy) for row in high_rows[:12]],
        "low_interaction_false_positive": [_diagnostic_row(row, strategy) for row in low_false[:12]],
        "semantic_disagreements": [_diagnostic_row(row, strategy) for row in disagreement_rows[:12]],
        "diversity_limited_duplicates": _diversity_diagnostic_rows(ranked, strategy, k=k),
    }


def _diagnostic_row(row: dict, strategy: str) -> dict:
    scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
    components = row.get("component_scores") if isinstance(row.get("component_scores"), dict) else {}
    target = float(scores.get(strategy) or row.get("final_score") or 0.0)
    semantic = float(scores.get("semantic_baseline_v2") or 0.0)
    return {
        "sample_id": row.get("training_sample_id") or "",
        "platform_item_id": row.get("platform_item_id") or "",
        "account_id": row.get("account_id") or "",
        "title": row.get("title") or "",
        "performance_label": row.get("performance_label") or "",
        "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
        "semantic_baseline_score": round(semantic, 4),
        "ranker_strategy_score": round(target, 4),
        "disagreement_score": round(abs(target - semantic), 4),
        "risk_score": round(float(components.get("low_interaction_risk") or 0.0), 4),
        "recommended_fields": _diagnostic_recommended_fields(row),
        "component_scores": components,
        "omni_shadow": _diagnostic_omni_shadow(row),
    }


def _diagnostic_omni_shadow(row: dict) -> dict:
    omni = row.get("omni_shadow") if isinstance(row.get("omni_shadow"), dict) else row.get("_omni_shadow") if isinstance(row.get("_omni_shadow"), dict) else {}
    if not omni:
        return {"available": False}
    suggestions = omni.get("semantic_suggestions") if isinstance(omni.get("semantic_suggestions"), dict) else {}
    quality = omni.get("semantic_quality") if isinstance(omni.get("semantic_quality"), dict) else {}
    return {
        "available": True,
        "normalization_version": quality.get("normalization_version") or omni.get("normalization_version") or "",
        "ranker_usable_fields": quality.get("ranker_usable_fields") if isinstance(quality.get("ranker_usable_fields"), list) else omni.get("ranker_usable_fields") or [],
        "suggested_fields": {
            field: suggestions.get(field)
            for field in [
                "content_category",
                "hook_type",
                "slice_structure",
                "domain_category",
                "material_type",
                "program_context",
                "presentation_style",
                "artist_names",
                "song_title",
            ]
            if suggestions.get(field) not in {None, "", "unknown"}
        },
    }


def _next_calibration_queue(rows: list[dict], strategy: str, *, k: int) -> list[dict]:
    diagnostics = _diagnostic_samples(rows, strategy, k=k)
    queue: list[dict] = []
    seen = set()
    for queue_type, reason in [
        ("missed_high_interaction", "high_interaction_missed_by_ranker"),
        ("low_interaction_false_positive", "low_interaction_false_positive"),
        ("semantic_disagreements", "semantic_ranker_disagreement"),
        ("diversity_limited_duplicates", "near_duplicate_diversity_review"),
    ]:
        for row in diagnostics.get(queue_type) or []:
            sample_id = row.get("sample_id") or row.get("platform_item_id") or row.get("title")
            if not sample_id or sample_id in seen:
                continue
            seen.add(sample_id)
            queue.append(
                {
                    **row,
                    "queue_reason": reason,
                    "queue_type": "risk" if queue_type == "low_interaction_false_positive" else "disagreement",
                    "priority_score": round(
                        min(
                            100.0,
                            float(row.get("normalized_reward") or 0.0) * 0.45
                            + float(row.get("disagreement_score") or 0.0) * 12.0
                            + float(row.get("risk_score") or 0.0) * 0.35,
                        ),
                        2,
                    ),
                }
            )
            if len(queue) >= 30:
                return queue
    return queue


def _diversity_summary(rows: list[dict], strategy: str, *, k: int) -> dict:
    ranked = _rows_for_strategy(rows, strategy)
    ranked = _rank_rows(ranked)
    top = ranked[: max(1, int(k or 10))]
    duplicate_keys = _duplicate_key_counts(top)
    if strategy == RESEARCH_RANKER_V26_POOL_STRATEGY:
        penalty_key = "v26_pool_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V29_TAXONOMY_STRATEGY:
        penalty_key = "v29_material_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V28_MATERIAL_STRATEGY:
        penalty_key = "v28_material_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V27_MATERIAL_STRATEGY:
        penalty_key = "v27_material_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V25_SHADOW_STRATEGY:
        penalty_key = "v25_shadow_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V24_STRATEGY:
        penalty_key = "v24_diversity_penalty"
    else:
        penalty_key = "v23_diversity_penalty"
    penalties = [
        float((row.get("component_scores") or {}).get(penalty_key) or 0.0)
        for row in top
    ]
    return {
        "strategy": strategy,
        "topk": len(top),
        "duplicate_title_groups": duplicate_keys["title"],
        "duplicate_song_groups": duplicate_keys["song"],
        "duplicate_artist_groups": duplicate_keys["artist"],
        "penalized_topk_count": sum(1 for value in penalties if value > 0),
        "max_diversity_penalty": round(max(penalties, default=0.0), 4),
        "policy": "limit near-duplicate title, song, artist, and broad category concentration before promotion-gate evaluation.",
    }


def _omni_cached_rows(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if isinstance(row.get("omni_shadow") or row.get("_omni_shadow"), dict)
        and (row.get("omni_shadow") or row.get("_omni_shadow"))
    ]


def _label_distribution(rows: list[dict]) -> dict[str, int]:
    counts = Counter(str(row.get("performance_label") or "unknown").lower() for row in rows)
    return {key: int(counts.get(key, 0)) for key in ["high", "mid", "low", "unknown"] if counts.get(key, 0)}


def _score_delta(row: dict, target: str, base: str = RESEARCH_RANKER_V24_STRATEGY) -> float:
    scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
    return float(scores.get(target) or 0.0) - float(scores.get(base) or 0.0)


def _omni_pool_report(rows: list[dict]) -> dict:
    cached = _omni_cached_rows(rows)
    result: dict[str, Any] = {
        "mode": "cached_eval_only",
        "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
        "shadow_strategy": RESEARCH_RANKER_V25_SHADOW_STRATEGY,
        "pool_strategy": RESEARCH_RANKER_V26_POOL_STRATEGY,
        "eval_count": len(rows),
        "cached_eval_count": len(cached),
        "cached_eval_rate": round(len(cached) / max(1, len(rows)), 4),
        "topk": {},
    }
    for k in [20, 30, 50]:
        if len(cached) < k:
            continue
        top24 = _rows_for_strategy(cached, RESEARCH_RANKER_V24_STRATEGY)
        top25 = _rows_for_strategy(cached, RESEARCH_RANKER_V25_SHADOW_STRATEGY)
        top26 = _rows_for_strategy(cached, RESEARCH_RANKER_V26_POOL_STRATEGY)
        top24 = _rank_rows(top24)
        top25 = _rank_rows(top25)
        top26 = _rank_rows(top26)
        ids24 = {_row_identity(row) for row in top24[:k]}
        ids25 = {_row_identity(row) for row in top25[:k]}
        ids26 = {_row_identity(row) for row in top26[:k]}
        v24 = _strategy_metrics(cached, RESEARCH_RANKER_V24_STRATEGY, k=k)
        v25 = _strategy_metrics(cached, RESEARCH_RANKER_V25_SHADOW_STRATEGY, k=k)
        v26 = _strategy_metrics(cached, RESEARCH_RANKER_V26_POOL_STRATEGY, k=k)
        result["topk"][str(k)] = {
            "k": k,
            "v2_4": v24,
            "v2_5_shadow": v25,
            "v2_6_pool": v26,
            "v2_5_lift_delta_vs_v2_4": round(float(v25.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0), 4),
            "v2_6_lift_delta_vs_v2_4": round(float(v26.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0), 4),
            "v2_6_high_hit_delta_vs_v2_4": round(float(v26.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0), 4),
            "v2_6_low_avoidance_delta_vs_v2_4": round(float(v26.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0), 4),
            "v2_5_overlap_with_v2_4": len(ids24 & ids25),
            "v2_6_overlap_with_v2_4": len(ids24 & ids26),
            "v2_4_label_distribution": _label_distribution(top24[:k]),
            "v2_5_label_distribution": _label_distribution(top25[:k]),
            "v2_6_label_distribution": _label_distribution(top26[:k]),
            "v2_6_entered_count": len(ids26 - ids24),
            "v2_6_left_count": len(ids24 - ids26),
        }
    return result


def _row_identity(row: dict) -> str:
    return str(row.get("training_sample_id") or row.get("platform_item_id") or row.get("candidate_segment_id") or row.get("id") or "")


def _deterministic_row_key(row: dict) -> str:
    identity = _row_identity(row)
    if identity:
        return identity
    payload = {
        key: row.get(key)
        for key in ["account_id", "dataset_id", "title", "published_at", "song_title", "artist_names"]
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _rank_rows(rows: list[dict], *, primary: str = "final_score", fallback: str | None = None) -> list[dict]:
    def score(row: dict) -> float:
        value = row.get(primary)
        if (value is None or value == "") and fallback:
            value = row.get(fallback)
        return float(value or 0.0)

    return sorted(rows, key=lambda row: (-score(row), _deterministic_row_key(row)))


def _omni_pool_gate(rows: list[dict]) -> dict:
    report = _omni_pool_report(rows)
    top30 = (report.get("topk") or {}).get("30") if isinstance(report.get("topk"), dict) else {}
    lift_delta = float((top30 or {}).get("v2_6_lift_delta_vs_v2_4") or 0.0)
    high_delta = float((top30 or {}).get("v2_6_high_hit_delta_vs_v2_4") or 0.0)
    low_delta = float((top30 or {}).get("v2_6_low_avoidance_delta_vs_v2_4") or 0.0)
    cached_count = int(report.get("cached_eval_count") or 0)
    passed = cached_count >= 50 and lift_delta >= 0.05 and high_delta >= 0.08 and low_delta >= -0.0001
    return {
        "strategy": RESEARCH_RANKER_V26_POOL_STRATEGY,
        "status": "pool_gate_pass" if passed else "pool_research_only",
        "passed": passed,
        "pool_k": 30,
        "cached_eval_count": cached_count,
        "required_cached_eval_count": 50,
        "lift_delta_vs_v2_4": round(lift_delta, 4),
        "high_hit_delta_vs_v2_4": round(high_delta, 4),
        "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
        "required_lift_delta_vs_v2_4": 0.05,
        "required_high_hit_delta_vs_v2_4": 0.08,
        "decision": "enable_top30_pool_research" if passed else "keep_as_pool_research",
        "note": "v2.6 只用于 Omni Top20/30 扩池研究，不替代 v2.4 Top10 最终排序。",
    }


def _omni_material_report(rows: list[dict]) -> dict:
    cached = _omni_cached_rows(rows)
    material_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    presentation_counts: Counter[str] = Counter()
    program_counts: Counter[str] = Counter()
    matrix: Counter[str] = Counter()
    conflict_count = 0
    agreement_count = 0
    for row in cached:
        suggestions = _omni_shadow_suggestions(row)
        material = _known_material_value(suggestions.get("material_type")) or "unknown"
        domain = _known_material_value(suggestions.get("domain_category")) or "unknown"
        style = _known_material_value(suggestions.get("presentation_style")) or "unknown"
        program = str(suggestions.get("program_context") or "unknown").strip() or "unknown"
        category = _known_material_value(row.get("content_category")) or "unknown"
        material_counts[material] += 1
        domain_counts[domain] += 1
        presentation_counts[style] += 1
        if program != "unknown":
            program_counts[program] += 1
        matrix[f"{category}->{material}"] += 1
        if material != "unknown" and category != "unknown":
            if _material_compatible_with_category(material, category):
                agreement_count += 1
            else:
                conflict_count += 1
    result: dict[str, Any] = {
        "mode": "cached_eval_only",
        "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
        "strategy": RESEARCH_RANKER_V27_MATERIAL_STRATEGY,
        "eval_count": len(rows),
        "cached_eval_count": len(cached),
        "cached_eval_rate": round(len(cached) / max(1, len(rows)), 4),
        "domain_distribution": dict(domain_counts.most_common()),
        "material_distribution": dict(material_counts.most_common()),
        "presentation_distribution": dict(presentation_counts.most_common()),
        "program_context_distribution": dict(program_counts.most_common(20)),
        "content_material_matrix_top": dict(matrix.most_common(30)),
        "material_agreement_count": agreement_count,
        "material_conflict_count": conflict_count,
        "material_conflict_rate": round(conflict_count / max(1, conflict_count + agreement_count), 4),
        "annotation_field_guides": omni_annotation_field_guides(_material_annotation_fields()),
        "topk": {},
        "material_buckets": {},
    }
    for k in [20, 30, 50]:
        if len(cached) < k:
            continue
        v24 = _strategy_metrics(cached, RESEARCH_RANKER_V24_STRATEGY, k=k)
        v26 = _strategy_metrics(cached, RESEARCH_RANKER_V26_POOL_STRATEGY, k=k)
        v27 = _strategy_metrics(cached, RESEARCH_RANKER_V27_MATERIAL_STRATEGY, k=k)
        top24 = _rows_for_strategy(cached, RESEARCH_RANKER_V24_STRATEGY)
        top27 = _rows_for_strategy(cached, RESEARCH_RANKER_V27_MATERIAL_STRATEGY)
        top24 = _rank_rows(top24)
        top27 = _rank_rows(top27)
        ids24 = {_row_identity(row) for row in top24[:k]}
        ids27 = {_row_identity(row) for row in top27[:k]}
        entered = [row for row in top27[:k] if _row_identity(row) not in ids24]
        left = [row for row in top24[:k] if _row_identity(row) not in ids27]
        result["topk"][str(k)] = {
            "k": k,
            "v2_4": v24,
            "v2_6_pool": v26,
            "v2_7_material": v27,
            "v2_7_lift_delta_vs_v2_4": round(float(v27.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0), 4),
            "v2_7_high_hit_delta_vs_v2_4": round(float(v27.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0), 4),
            "v2_7_low_avoidance_delta_vs_v2_4": round(float(v27.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0), 4),
            "v2_7_overlap_with_v2_4": len(ids24 & ids27),
            "v2_7_entered_count": len(ids27 - ids24),
            "v2_7_left_count": len(ids24 - ids27),
            "v2_7_entered_samples": [_material_topk_change_summary(row) for row in entered[:8]],
            "v2_7_left_samples": [_material_topk_change_summary(row) for row in left[:8]],
            "v2_7_label_distribution": _label_distribution(top27[:k]),
        }
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in cached:
        material = _known_material_value(_omni_shadow_suggestions(row).get("material_type")) or "unknown"
        grouped[material].append(row)
    for material, items in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True):
        bucket_k = min(10, max(1, len(items)))
        v24 = _strategy_metrics(items, RESEARCH_RANKER_V24_STRATEGY, k=bucket_k)
        v27 = _strategy_metrics(items, RESEARCH_RANKER_V27_MATERIAL_STRATEGY, k=bucket_k)
        result["material_buckets"][material] = {
            "sample_count": len(items),
            "k": bucket_k,
            "label_distribution": _label_distribution(items),
            "v2_4_topk_lift_vs_random": v24.get("topk_lift_vs_random"),
            "v2_7_topk_lift_vs_random": v27.get("topk_lift_vs_random"),
            "lift_delta_vs_v2_4": round(float(v27.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0), 4),
            "high_hit_delta_vs_v2_4": round(float(v27.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0), 4),
            "low_avoidance_delta_vs_v2_4": round(float(v27.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0), 4),
        }
    return result


def _material_topk_change_summary(row: dict) -> dict:
    suggestions = _omni_shadow_suggestions(row)
    scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
    components = row.get("component_scores") if isinstance(row.get("component_scores"), dict) else {}
    return {
        "sample_id": row.get("training_sample_id") or "",
        "platform_item_id": row.get("platform_item_id") or "",
        "account_id": row.get("account_id") or "",
        "title": row.get("title") or "",
        "performance_label": row.get("performance_label") or "",
        "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
        "content_category": row.get("content_category") or "",
        "domain_category": _known_material_value(suggestions.get("domain_category")) or "unknown",
        "material_type": _known_material_value(suggestions.get("material_type")) or "unknown",
        "presentation_style": suggestions.get("presentation_style") or "unknown",
        "v2_4_score": round(float(scores.get(RESEARCH_RANKER_V24_STRATEGY) or 0.0), 4),
        "v2_7_score": round(float(scores.get(RESEARCH_RANKER_V27_MATERIAL_STRATEGY) or 0.0), 4),
        "score_delta_vs_v2_4": round(_score_delta(row, RESEARCH_RANKER_V27_MATERIAL_STRATEGY, RESEARCH_RANKER_V24_STRATEGY), 4),
        "material_conflict": bool(float(components.get("v27_material_conflict") or 0.0)),
        "material_evidence": round(float(components.get("v27_material_evidence") or 0.0), 4),
    }


def _material_annotation_fields() -> list[str]:
    return ["domain_category", "material_type", "program_context", "presentation_style", "material_label_verified"]


def _omni_material_gate(rows: list[dict]) -> dict:
    report = _omni_material_report(rows)
    top30 = (report.get("topk") or {}).get("30") if isinstance(report.get("topk"), dict) else {}
    lift_delta = float((top30 or {}).get("v2_7_lift_delta_vs_v2_4") or 0.0)
    high_delta = float((top30 or {}).get("v2_7_high_hit_delta_vs_v2_4") or 0.0)
    low_delta = float((top30 or {}).get("v2_7_low_avoidance_delta_vs_v2_4") or 0.0)
    cached_count = int(report.get("cached_eval_count") or 0)
    passed = cached_count >= 50 and lift_delta >= 0.03 and high_delta >= 0.05 and low_delta >= -0.0001
    return {
        "strategy": RESEARCH_RANKER_V27_MATERIAL_STRATEGY,
        "status": "material_gate_pass" if passed else "material_research_only",
        "passed": passed,
        "cached_eval_count": cached_count,
        "required_cached_eval_count": 50,
        "lift_delta_vs_v2_4": round(lift_delta, 4),
        "high_hit_delta_vs_v2_4": round(high_delta, 4),
        "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
        "required_lift_delta_vs_v2_4": 0.03,
        "required_high_hit_delta_vs_v2_4": 0.05,
        "required_low_avoidance_delta_vs_v2_4": 0.0,
        "material_conflict_rate": report.get("material_conflict_rate"),
        "decision": "allow_material_shadow_research" if passed else "keep_as_material_shadow_research",
        "note": "v2.7 只验证 domain_category + material_type 路由，不写 manual_verified，不替代 v2.4。",
    }


def _omni_material_gold_set_queue(rows: list[dict], *, limit: int = 60) -> list[dict]:
    candidates: list[dict] = []
    cached_rows = _omni_cached_rows(rows)
    confirmed_groups = {
        _material_gold_group_key(row)
        for row in cached_rows
        if _material_gold_from_row(row).get("review_status") == "confirmed"
    }
    for row in cached_rows:
        gold = _material_gold_from_row(row)
        if (gold and gold.get("review_status") == "confirmed") or _material_gold_group_key(row) in confirmed_groups:
            continue
        suggestions = _omni_shadow_suggestions(row)
        material = _known_material_value(suggestions.get("material_type"))
        domain = _known_material_value(suggestions.get("domain_category"))
        category = _known_material_value(row.get("content_category"))
        components = row.get("component_scores") if isinstance(row.get("component_scores"), dict) else {}
        conflict = float(components.get("v27_material_conflict") or 0.0)
        score_delta = abs(_score_delta(row, RESEARCH_RANKER_V27_MATERIAL_STRATEGY, RESEARCH_RANKER_V24_STRATEGY))
        if not material and not conflict and score_delta < 0.12:
            continue
        priority = (
            float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0) * 0.38
            + conflict * 28.0
            + score_delta * 12.0
            + (8.0 if material in {"performance_clip", "reaction", "commentary", "vocal_teaching", "compilation"} else 0.0)
        )
        recommended_fields = _material_annotation_fields()
        guides = omni_annotation_field_guides(recommended_fields)
        candidates.append(
            {
                "sample_id": row.get("training_sample_id") or row.get("id") or "",
                "platform_item_id": row.get("platform_item_id") or "",
                "account_id": row.get("account_id") or "",
                "dataset_id": row.get("dataset_id") or "",
                "title": row.get("title") or "",
                "platform_url": row.get("platform_url") or "",
                "performance_label": row.get("performance_label") or "",
                "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
                "content_category": row.get("content_category") or "",
                "domain_category": domain or "unknown",
                "material_type": material or "unknown",
                "program_context": suggestions.get("program_context") or "unknown",
                "presentation_style": suggestions.get("presentation_style") or "unknown",
                "material_conflict": bool(conflict),
                "score_delta_vs_v2_4": round(score_delta, 4),
                "recommended_fields": recommended_fields,
                "recommended_field_guides": [guides[field] for field in recommended_fields if field in guides],
                "queue_reason": "material_conflict_review" if conflict else "material_shadow_gold_set",
                "priority_score": round(min(100.0, priority), 2),
                "_priority_raw": priority,
                "writes_labels": False,
                "production_weight": False,
            }
        )
    candidates.sort(
        key=lambda item: (
            float(item.get("_priority_raw") or 0.0),
            bool(item.get("material_conflict")),
            float(item.get("score_delta_vs_v2_4") or 0.0),
            float(item.get("normalized_reward") or 0.0),
            str(item.get("sample_id") or ""),
        ),
        reverse=True,
    )
    group_sizes = Counter(_material_gold_group_key(item) for item in candidates)
    selected: list[dict] = []
    seen_groups: set[str] = set()
    for candidate in candidates:
        group_key = _material_gold_group_key(candidate)
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        item = dict(candidate)
        item.pop("_priority_raw", None)
        group_size = int(group_sizes.get(group_key) or 1)
        item["duplicate_group_size"] = group_size
        item["collapsed_variant_count"] = max(0, group_size - 1)
        selected.append(item)
        if len(selected) >= max(1, int(limit or 60)):
            break
    return selected


def _material_gold_group_key(row: dict) -> str:
    account = str(row.get("account_id") or "").strip().lower()
    title_key = _stable_title_key(row.get("title"))
    if title_key:
        return f"{account}:title:{title_key}"
    sample_id = str(row.get("sample_id") or row.get("training_sample_id") or row.get("id") or row.get("platform_item_id") or "").strip()
    return f"{account}:sample:{sample_id}"


def _material_gold_quality_report(rows: list[dict], *, scope: str = "gold_set") -> dict:
    raw_gold_rows = [row for row in rows if _material_gold_from_row(row)]
    effective_rows = _material_gold_deduplicated_rows(raw_gold_rows)
    compared_rows = []
    field_reports: dict[str, dict[str, Any]] = {}
    fields = ["domain_category", "material_type", "presentation_style"]
    for field in fields:
        compared = 0
        matched = 0
        matrix: Counter[str] = Counter()
        for row in effective_rows:
            gold = _material_gold_from_row(row)
            if not gold:
                continue
            predicted = _known_material_value(_omni_shadow_suggestions(row).get(field)) or "unknown"
            expected = _known_material_value(gold.get(field)) or "unknown"
            matrix[f"{expected}->{predicted}"] += 1
            if expected == "unknown":
                continue
            compared += 1
            if predicted == expected:
                matched += 1
        accuracy = matched / max(1, compared) if compared else 0.0
        field_reports[field] = {
            "compared_count": compared,
            "matched_count": matched,
            "accuracy": round(accuracy, 4),
            "confusion_top": dict(matrix.most_common(20)),
        }
    taxonomy_relations: Counter[str] = Counter()
    taxonomy_compared = 0
    taxonomy_matched = 0
    taxonomy_partial_total = 0.0
    for row in effective_rows:
        gold = _material_gold_from_row(row)
        if not gold:
            continue
        suggestions = _omni_shadow_suggestions(row)
        gold_material = _known_material_value(gold.get("material_type"))
        omni_material = _known_material_value(suggestions.get("material_type"))
        relation = _material_type_taxonomy_relation(gold_material, omni_material) if gold_material else "not_scored"
        if gold_material and relation != "not_material_form":
            taxonomy_relations[relation] += 1
            taxonomy_compared += 1
            if relation in MATERIAL_TAXONOMY_MATCH_RELATIONS:
                taxonomy_matched += 1
            taxonomy_partial_total += float(MATERIAL_TYPE_TAXONOMY_SCORES.get(relation) or 0.0)
        compared_rows.append(
            {
                "sample_id": row.get("id") or row.get("training_sample_id") or "",
                "account_id": row.get("account_id") or "",
                "title": row.get("title") or "",
                "gold_material_type": gold_material or "unknown",
                "omni_material_type": omni_material or "unknown",
                "canonical_gold_material_type": _canonical_material_type(gold_material) or "unknown",
                "canonical_omni_material_type": _canonical_material_type(omni_material) or "unknown",
                "taxonomy_relation": relation,
                "material_match": bool(gold_material and gold_material == omni_material),
                "canonical_material_match": bool(gold_material and relation in MATERIAL_TAXONOMY_MATCH_RELATIONS),
            }
        )
    material_accuracy = float((field_reports.get("material_type") or {}).get("accuracy") or 0.0)
    canonical_accuracy = taxonomy_matched / max(1, taxonomy_compared) if taxonomy_compared else 0.0
    taxonomy_partial_accuracy = taxonomy_partial_total / max(1, taxonomy_compared) if taxonomy_compared else 0.0
    severe_error_count = int(taxonomy_relations.get("mismatch") or 0)
    severe_error_rate = severe_error_count / max(1, taxonomy_compared) if taxonomy_compared else 0.0
    canonical_quality_gate_passed = (
        taxonomy_compared >= int(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["trusted_min_gold_samples"])
        and material_accuracy >= 0.70
        and canonical_accuracy >= 0.75
        and severe_error_rate <= 0.25
    )
    return {
        "mode": "gold_set_quality_audit",
        "scope": scope,
        "confirmed_count": len(raw_gold_rows),
        "effective_unique_count": len(compared_rows),
        "collapsed_duplicate_count": max(0, len(raw_gold_rows) - len(compared_rows)),
        "required_for_router": int(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["trusted_min_gold_samples"]),
        "field_reports": field_reports,
        "material_type_accuracy": round(material_accuracy, 4),
        "canonical_material_type_accuracy": round(canonical_accuracy, 4),
        "taxonomy_partial_accuracy": round(taxonomy_partial_accuracy, 4),
        "taxonomy_relation_counts": dict(taxonomy_relations),
        "severe_error_count": severe_error_count,
        "severe_error_rate": round(severe_error_rate, 4),
        "quality_gate_passed": len(compared_rows) >= int(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["trusted_min_gold_samples"]) and material_accuracy >= 0.75,
        "canonical_quality_gate_passed": canonical_quality_gate_passed,
        "canonical_quality_requirements": {
            "compared_count": int(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["trusted_min_gold_samples"]),
            "strict_accuracy": 0.70,
            "canonical_accuracy": 0.75,
            "max_severe_error_rate": 0.25,
        },
        "disagreement_samples": [item for item in compared_rows if not item["material_match"]][:20],
        "taxonomy_mismatch_samples": [item for item in compared_rows if item["taxonomy_relation"] == "mismatch"][:20],
        "coarse_match_samples": [item for item in compared_rows if item["taxonomy_relation"] == "coarse_match"][:20],
        "writes_main_semantic_labels": False,
        "rewrites_manual_annotations": False,
    }


def _material_router_profile_rows(profiles: dict[str, dict]) -> list[dict]:
    rows = [dict(profile) for profile in profiles.values()]
    rows.sort(
        key=lambda item: (
            item.get("account_id") == "__global__",
            item.get("router_status") == "trusted",
            int(item.get("confirmed_count") or 0),
        ),
        reverse=True,
    )
    return rows


def _omni_material_v28_report(rows: list[dict]) -> dict:
    cached = _omni_cached_rows(rows)
    result: dict[str, Any] = {
        "mode": "cached_eval_only",
        "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
        "strategy": RESEARCH_RANKER_V28_MATERIAL_STRATEGY,
        "cached_eval_count": len(cached),
        "topk": {},
    }
    for k in [20, 30, 50]:
        if len(cached) < k:
            continue
        v24 = _strategy_metrics(cached, RESEARCH_RANKER_V24_STRATEGY, k=k)
        v28 = _strategy_metrics(cached, RESEARCH_RANKER_V28_MATERIAL_STRATEGY, k=k)
        top24 = _rank_rows(_rows_for_strategy(cached, RESEARCH_RANKER_V24_STRATEGY))
        top28 = _rank_rows(_rows_for_strategy(cached, RESEARCH_RANKER_V28_MATERIAL_STRATEGY))
        ids24 = {_row_identity(row) for row in top24[:k]}
        ids28 = {_row_identity(row) for row in top28[:k]}
        entered = [row for row in top28[:k] if _row_identity(row) not in ids24]
        left = [row for row in top24[:k] if _row_identity(row) not in ids28]
        result["topk"][str(k)] = {
            "k": k,
            "v2_4": v24,
            "v2_8_material": v28,
            "v2_8_lift_delta_vs_v2_4": round(float(v28.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0), 4),
            "v2_8_high_hit_delta_vs_v2_4": round(float(v28.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0), 4),
            "v2_8_low_avoidance_delta_vs_v2_4": round(float(v28.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0), 4),
            "v2_8_entered_count": len(ids28 - ids24),
            "v2_8_left_count": len(ids24 - ids28),
            "v2_8_entered_samples": [_material_topk_change_summary_v28(row) for row in entered[:8]],
            "v2_8_left_samples": [_material_topk_change_summary_v28(row) for row in left[:8]],
        }
    return result


def _material_topk_change_summary_v28(row: dict) -> dict:
    item = _material_topk_change_summary(row)
    scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
    components = row.get("component_scores") if isinstance(row.get("component_scores"), dict) else {}
    item.update(
        {
            "v2_8_score": round(float(scores.get(RESEARCH_RANKER_V28_MATERIAL_STRATEGY) or 0.0), 4),
            "v2_8_delta_vs_v2_4": round(_score_delta(row, RESEARCH_RANKER_V28_MATERIAL_STRATEGY, RESEARCH_RANKER_V24_STRATEGY), 4),
            "router_status": {1.0: "trusted", 0.5: "neutral", 0.0: "blocked"}.get(float(components.get("v28_material_router_status_score") or 0.5), "neutral"),
            "router_accuracy": round(float(components.get("v28_material_router_accuracy") or 0.0), 4),
        }
    )
    return item


def _omni_material_v28_gate(rows: list[dict], gold_rows: list[dict]) -> dict:
    report = _omni_material_v28_report(rows)
    quality = _material_gold_quality_report(gold_rows, scope="audit_holdout")
    top20 = (report.get("topk") or {}).get("20") or {}
    top30 = (report.get("topk") or {}).get("30") or {}
    lift_delta = float(top30.get("v2_8_lift_delta_vs_v2_4") or 0.0)
    high_delta = float(top30.get("v2_8_high_hit_delta_vs_v2_4") or 0.0)
    low_delta = float(top30.get("v2_8_low_avoidance_delta_vs_v2_4") or 0.0)
    top20_lift = float(top20.get("v2_8_lift_delta_vs_v2_4") or 0.0)
    passed = bool(quality.get("quality_gate_passed")) and lift_delta >= 0.03 and high_delta >= 0.05 and low_delta >= -0.0001 and top20_lift >= -0.0001
    return {
        "strategy": RESEARCH_RANKER_V28_MATERIAL_STRATEGY,
        "status": "material_calibration_gate_pass" if passed else "material_calibration_research_only",
        "passed": False,
        "research_gate_passed": passed,
        "confirmed_count": quality.get("confirmed_count"),
        "effective_unique_count": quality.get("effective_unique_count"),
        "quality_scope": quality.get("scope"),
        "material_type_accuracy": quality.get("material_type_accuracy"),
        "lift_delta_vs_v2_4": round(lift_delta, 4),
        "high_hit_delta_vs_v2_4": round(high_delta, 4),
        "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
        "top20_lift_delta_vs_v2_4": round(top20_lift, 4),
        "requirements": {
            "confirmed_count": int(RESEARCH_RANKER_V28_MATERIAL_WEIGHT_CONFIG["trusted_min_gold_samples"]),
            "material_type_accuracy": 0.75,
            "lift_delta_vs_v2_4": 0.03,
            "high_hit_delta_vs_v2_4": 0.05,
            "low_avoidance_delta_vs_v2_4": 0.0,
            "top20_lift_delta_vs_v2_4": 0.0,
        },
        "decision": "keep_v2_8_as_research" if not passed else "eligible_for_repeated_time_window_validation",
        "note": "Gold 校准子集只建立 Omni 可信度路由并从性能验证行排除；独立审计子集只用于质量门槛。",
    }


def _omni_material_v29_report(rows: list[dict]) -> dict:
    cached = _omni_cached_rows(rows)
    result: dict[str, Any] = {
        "mode": "cached_eval_only",
        "base_strategy": RESEARCH_RANKER_V24_STRATEGY,
        "strict_router_strategy": RESEARCH_RANKER_V28_MATERIAL_STRATEGY,
        "strategy": RESEARCH_RANKER_V29_TAXONOMY_STRATEGY,
        "cached_eval_count": len(cached),
        "topk": {},
    }
    for k in [20, 30, 50]:
        if len(cached) < k:
            continue
        v24 = _strategy_metrics(cached, RESEARCH_RANKER_V24_STRATEGY, k=k)
        v28 = _strategy_metrics(cached, RESEARCH_RANKER_V28_MATERIAL_STRATEGY, k=k)
        v29 = _strategy_metrics(cached, RESEARCH_RANKER_V29_TAXONOMY_STRATEGY, k=k)
        top24 = _rank_rows(_rows_for_strategy(cached, RESEARCH_RANKER_V24_STRATEGY))
        top29 = _rank_rows(_rows_for_strategy(cached, RESEARCH_RANKER_V29_TAXONOMY_STRATEGY))
        ids24 = {_row_identity(row) for row in top24[:k]}
        ids29 = {_row_identity(row) for row in top29[:k]}
        entered = [row for row in top29[:k] if _row_identity(row) not in ids24]
        left = [row for row in top24[:k] if _row_identity(row) not in ids29]
        result["topk"][str(k)] = {
            "k": k,
            "v2_4": v24,
            "v2_8_strict": v28,
            "v2_9_taxonomy": v29,
            "v2_9_lift_delta_vs_v2_4": round(float(v29.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0), 4),
            "v2_9_high_hit_delta_vs_v2_4": round(float(v29.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0), 4),
            "v2_9_low_avoidance_delta_vs_v2_4": round(float(v29.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0), 4),
            "v2_9_lift_delta_vs_v2_8": round(float(v29.get("topk_lift_vs_random") or 0.0) - float(v28.get("topk_lift_vs_random") or 0.0), 4),
            "v2_9_high_hit_delta_vs_v2_8": round(float(v29.get("high_interaction_hit_rate") or 0.0) - float(v28.get("high_interaction_hit_rate") or 0.0), 4),
            "v2_9_low_avoidance_delta_vs_v2_8": round(float(v29.get("low_interaction_avoidance_rate") or 0.0) - float(v28.get("low_interaction_avoidance_rate") or 0.0), 4),
            "v2_9_entered_count": len(ids29 - ids24),
            "v2_9_left_count": len(ids24 - ids29),
            "v2_9_entered_samples": [_material_topk_change_summary_v29(row) for row in entered[:8]],
            "v2_9_left_samples": [_material_topk_change_summary_v29(row) for row in left[:8]],
        }
    return result


def _material_topk_change_summary_v29(row: dict) -> dict:
    item = _material_topk_change_summary(row)
    scores = row.get("strategy_scores") if isinstance(row.get("strategy_scores"), dict) else {}
    components = row.get("component_scores") if isinstance(row.get("component_scores"), dict) else {}
    item.update(
        {
            "v2_9_score": round(float(scores.get(RESEARCH_RANKER_V29_TAXONOMY_STRATEGY) or 0.0), 4),
            "v2_9_delta_vs_v2_4": round(_score_delta(row, RESEARCH_RANKER_V29_TAXONOMY_STRATEGY, RESEARCH_RANKER_V24_STRATEGY), 4),
            "router_status": {1.0: "trusted", 0.5: "neutral", 0.0: "blocked"}.get(float(components.get("v29_material_router_status_score") or 0.5), "neutral"),
            "router_accuracy": round(float(components.get("v29_material_router_accuracy") or 0.0), 4),
            "canonicalized_material": bool(float(components.get("v29_material_canonicalized") or 0.0)),
        }
    )
    return item


def _omni_material_v29_gate(rows: list[dict], gold_rows: list[dict]) -> dict:
    report = _omni_material_v29_report(rows)
    quality = _material_gold_quality_report(gold_rows, scope="audit_holdout")
    top20 = (report.get("topk") or {}).get("20") or {}
    top30 = (report.get("topk") or {}).get("30") or {}
    lift_delta = float(top30.get("v2_9_lift_delta_vs_v2_4") or 0.0)
    high_delta = float(top30.get("v2_9_high_hit_delta_vs_v2_4") or 0.0)
    low_delta = float(top30.get("v2_9_low_avoidance_delta_vs_v2_4") or 0.0)
    top20_lift = float(top20.get("v2_9_lift_delta_vs_v2_4") or 0.0)
    passed = bool(quality.get("canonical_quality_gate_passed")) and lift_delta >= 0.03 and high_delta >= 0.05 and low_delta >= -0.0001 and top20_lift >= -0.0001
    return {
        "strategy": RESEARCH_RANKER_V29_TAXONOMY_STRATEGY,
        "status": "material_taxonomy_gate_pass" if passed else "material_taxonomy_research_only",
        "passed": False,
        "research_gate_passed": passed,
        "confirmed_count": quality.get("confirmed_count"),
        "effective_unique_count": quality.get("effective_unique_count"),
        "quality_scope": quality.get("scope"),
        "strict_material_type_accuracy": quality.get("material_type_accuracy"),
        "canonical_material_type_accuracy": quality.get("canonical_material_type_accuracy"),
        "taxonomy_partial_accuracy": quality.get("taxonomy_partial_accuracy"),
        "severe_error_rate": quality.get("severe_error_rate"),
        "canonical_quality_gate_passed": quality.get("canonical_quality_gate_passed"),
        "lift_delta_vs_v2_4": round(lift_delta, 4),
        "high_hit_delta_vs_v2_4": round(high_delta, 4),
        "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
        "top20_lift_delta_vs_v2_4": round(top20_lift, 4),
        "requirements": {
            "canonical_material_type_accuracy": 0.75,
            "strict_material_type_accuracy": 0.70,
            "max_severe_error_rate": 0.25,
            "lift_delta_vs_v2_4": 0.03,
            "high_hit_delta_vs_v2_4": 0.05,
            "low_avoidance_delta_vs_v2_4": 0.0,
            "top20_lift_delta_vs_v2_4": 0.0,
        },
        "decision": "keep_v2_9_as_research" if not passed else "eligible_for_repeated_time_window_validation",
        "note": "原始人工标签不改写；performance_highlight 仅在路由侧归入 performance_clip，并继续单独审计细粒度缺失。",
    }


def _omni_account_pool_gates(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in _omni_cached_rows(rows):
        grouped[str(row.get("account_id") or "unknown")].append(row)
    gates: list[dict] = []
    for account_id, items in grouped.items():
        account_k = max(1, min(10, max(3, len(items) // 2)))
        account_k = min(account_k, len(items))
        top24 = _rows_for_strategy(items, RESEARCH_RANKER_V24_STRATEGY)
        top26 = _rows_for_strategy(items, RESEARCH_RANKER_V26_POOL_STRATEGY)
        top24 = _rank_rows(top24)
        top26 = _rank_rows(top26)
        ids24 = {_row_identity(row) for row in top24[:account_k]}
        ids26 = {_row_identity(row) for row in top26[:account_k]}
        v24 = _strategy_metrics(items, RESEARCH_RANKER_V24_STRATEGY, k=account_k)
        v26 = _strategy_metrics(items, RESEARCH_RANKER_V26_POOL_STRATEGY, k=account_k)
        lift_delta = float(v26.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0)
        high_delta = float(v26.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0)
        low_delta = float(v26.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0)
        evidence_values = [
            float((row.get("component_scores") or {}).get("v25_omni_shadow_evidence_quality") or 0.0)
            for row in items
        ]
        pool_evidence_count = sum(1 for row in items if float((row.get("component_scores") or {}).get("v26_omni_pool_evidence") or 0.0) > 0)
        conflict_count = sum(1 for row in items if float((row.get("component_scores") or {}).get("v25_omni_conflict_count") or 0.0) > 0)
        profile = next(
            (
                row.get("omni_router_profile")
                for row in items
                if isinstance(row.get("omni_router_profile"), dict) and row.get("omni_router_profile")
            ),
            {},
        )
        if len(items) < 10:
            status = "low_confidence"
            decision = "collect_more_omni"
            reason = "账号 cached eval 样本不足 10 条，先补 Omni 覆盖再判断是否扩池。"
        elif lift_delta >= 0.05 and high_delta >= 0.05 and low_delta >= -0.0001:
            status = "pool_boost_candidate"
            decision = "allow_pool_boost_research"
            reason = "v2.6 在该账号 cached-only 子集上同时提升 lift 和高互动命中，适合进入扩池候选。"
        elif lift_delta < -0.03 or high_delta < -0.05 or low_delta < -0.02:
            status = "quarantine"
            decision = "quarantine_omni_boost"
            reason = "v2.6 在该账号上降低 lift、高互动命中或低互动避让，暂不允许 Omni boost。"
        else:
            status = "evidence_only"
            decision = "use_as_explanation_only"
            reason = "v2.6 与 v2.4 差异不足以放开扩池，只作为审核解释和校准线索。"
        gates.append(
            {
                "account_id": account_id,
                "status": status,
                "decision": decision,
                "reason": reason,
                "cached_eval_count": len(items),
                "k": account_k,
                "v2_4_topk_lift_vs_random": v24.get("topk_lift_vs_random"),
                "v2_6_topk_lift_vs_random": v26.get("topk_lift_vs_random"),
                "lift_delta_vs_v2_4": round(lift_delta, 4),
                "high_hit_delta_vs_v2_4": round(high_delta, 4),
                "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
                "v2_6_entered_count": len(ids26 - ids24),
                "v2_6_left_count": len(ids24 - ids26),
                "v2_6_overlap_with_v2_4": len(ids24 & ids26),
                "v2_4_label_distribution": _label_distribution(top24[:account_k]),
                "v2_6_label_distribution": _label_distribution(top26[:account_k]),
                "avg_evidence_quality": round(sum(evidence_values) / max(1, len(evidence_values)), 4),
                "pool_evidence_rate": round(pool_evidence_count / max(1, len(items)), 4),
                "conflict_rate": round(conflict_count / max(1, len(items)), 4),
                "router_status": profile.get("router_status") or status,
                "recommended_next_step": _omni_account_pool_next_step(status),
            }
        )
    priority = {
        "pool_boost_candidate": 4,
        "quarantine": 3,
        "evidence_only": 2,
        "low_confidence": 1,
    }
    gates.sort(
        key=lambda item: (
            priority.get(str(item.get("status") or ""), 0),
            abs(float(item.get("lift_delta_vs_v2_4") or 0.0)) + abs(float(item.get("high_hit_delta_vs_v2_4") or 0.0)),
            int(item.get("cached_eval_count") or 0),
        ),
        reverse=True,
    )
    return gates


def _omni_account_pool_next_step(status: str) -> str:
    if status == "pool_boost_candidate":
        return "进入 Top30 扩池候选池，抽样复核进入和退出样本。"
    if status == "quarantine":
        return "优先审计该账号 Omni content_category/slice_structure 冲突样本，暂不使用 boost。"
    if status == "low_confidence":
        return "补齐该账号至少 10 条 cached eval，理想目标 30 条。"
    return "保持解释证据，优先校准分歧样本后再回测。"


def _omni_account_pool_summary(gates: list[dict]) -> dict:
    counts = Counter(str(item.get("status") or "unknown") for item in gates)
    boost = [item for item in gates if item.get("status") == "pool_boost_candidate"]
    quarantine = [item for item in gates if item.get("status") == "quarantine"]
    low_confidence = [item for item in gates if item.get("status") == "low_confidence"]
    return {
        "strategy": RESEARCH_RANKER_V26_POOL_STRATEGY,
        "account_count": len(gates),
        "status_counts": dict(counts),
        "boost_candidate_count": len(boost),
        "quarantine_count": len(quarantine),
        "low_confidence_count": len(low_confidence),
        "top_boost_accounts": [item.get("account_id") for item in boost[:5]],
        "top_quarantine_accounts": [item.get("account_id") for item in quarantine[:5]],
        "coverage_priority_accounts": [item.get("account_id") for item in low_confidence[:5]],
        "recommendation": _omni_account_pool_summary_recommendation(boost, quarantine, low_confidence),
    }


def _omni_account_pool_summary_recommendation(boost: list[dict], quarantine: list[dict], low_confidence: list[dict]) -> str:
    if boost and quarantine:
        return "按账号分流：boost_candidate 只进扩池研究，quarantine 先做语义/Omni 冲突审计。"
    if boost:
        return "已有账号出现稳定扩池增益，下一步抽查 Top30 进入样本并扩大 cached eval。"
    if quarantine:
        return "当前主要问题是账号级负增益，优先校准冲突字段而不是提高全局权重。"
    if low_confidence:
        return "多数账号 Omni 覆盖不足，先补 cached eval 到每账号 10-30 条。"
    return "账号级增益尚不明显，继续作为解释证据并扩大样本。"


def _omni_trust_profiles(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in _omni_cached_rows(rows):
        grouped[str(row.get("account_id") or "unknown")].append(row)
    profiles = []
    for account_id, items in grouped.items():
        account_k = max(1, min(10, len(items)))
        v24 = _strategy_metrics(items, RESEARCH_RANKER_V24_STRATEGY, k=account_k)
        v25 = _strategy_metrics(items, RESEARCH_RANKER_V25_SHADOW_STRATEGY, k=account_k)
        v26 = _strategy_metrics(items, RESEARCH_RANKER_V26_POOL_STRATEGY, k=account_k)
        v27 = _strategy_metrics(items, RESEARCH_RANKER_V27_MATERIAL_STRATEGY, k=account_k)
        evidence_values = [
            float((row.get("component_scores") or {}).get("v25_omni_shadow_evidence_quality") or 0.0)
            for row in items
        ]
        conflict_count = sum(1 for row in items if float((row.get("component_scores") or {}).get("v25_omni_conflict_count") or 0.0) > 0)
        lift_delta = float(v25.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0)
        high_delta = float(v25.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0)
        low_delta = float(v25.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0)
        if len(items) >= 10 and lift_delta >= 0.03 and high_delta >= -0.0001 and low_delta >= -0.0001:
            router_status = "boost_enabled"
        elif len(items) >= 10 and (lift_delta < -0.03 or high_delta < -0.0001 or low_delta < -0.0001):
            router_status = "quarantine"
        else:
            router_status = "evidence_only"
        profiles.append(
            {
                "account_id": account_id,
                "router_status": router_status,
                "cached_eval_count": len(items),
                "status": "ready" if len(items) >= 10 else "low_confidence",
                "label_distribution": _label_distribution(items),
                "avg_evidence_quality": round(sum(evidence_values) / max(1, len(evidence_values)), 4),
                "conflict_rate": round(conflict_count / max(1, len(items)), 4),
                "v2_4_topk_lift_vs_random": v24.get("topk_lift_vs_random"),
                "v2_5_topk_lift_vs_random": v25.get("topk_lift_vs_random"),
                "v2_6_topk_lift_vs_random": v26.get("topk_lift_vs_random"),
                "v2_7_topk_lift_vs_random": v27.get("topk_lift_vs_random"),
                "lift_delta_vs_v2_4": round(lift_delta, 4),
                "v2_7_lift_delta_vs_v2_4": round(float(v27.get("topk_lift_vs_random") or 0.0) - float(v24.get("topk_lift_vs_random") or 0.0), 4),
                "v2_7_high_hit_delta_vs_v2_4": round(float(v27.get("high_interaction_hit_rate") or 0.0) - float(v24.get("high_interaction_hit_rate") or 0.0), 4),
                "v2_7_low_avoidance_delta_vs_v2_4": round(float(v27.get("low_interaction_avoidance_rate") or 0.0) - float(v24.get("low_interaction_avoidance_rate") or 0.0), 4),
                "high_hit_delta_vs_v2_4": round(high_delta, 4),
                "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            }
        )
    profiles.sort(key=lambda item: (item["router_status"] == "boost_enabled", item["status"] == "ready", float(item.get("lift_delta_vs_v2_4") or 0.0)), reverse=True)
    return profiles


def _omni_shadow_summary(rows: list[dict], train_rows: list[dict], eval_rows: list[dict], *, strategy: str, k: int) -> dict:
    ranked = _rows_for_strategy(rows, strategy)
    ranked = _rank_rows(ranked)
    top = ranked[: max(1, int(k or 10))]

    def available_count(items: list[dict]) -> int:
        return sum(
            1
            for row in items
            if isinstance(row.get("_omni_shadow") or row.get("omni_shadow"), dict)
            and (row.get("_omni_shadow") or row.get("omni_shadow"))
        )

    def avg_component(items: list[dict], key: str) -> float:
        values = [
            float((row.get("component_scores") or {}).get(key) or 0.0)
            for row in items
            if isinstance(row.get("component_scores"), dict)
        ]
        return round(sum(values) / max(1, len(values)), 4)

    versions = Counter()
    for row in rows:
        omni = row.get("omni_shadow") if isinstance(row.get("omni_shadow"), dict) else row.get("_omni_shadow") if isinstance(row.get("_omni_shadow"), dict) else {}
        if not omni:
            continue
        semantic_quality = omni.get("semantic_quality") if isinstance(omni.get("semantic_quality"), dict) else {}
        version = str(omni.get("normalization_version") or semantic_quality.get("normalization_version") or "unknown")
        versions[version] += 1
    train_available = available_count(train_rows)
    eval_available = available_count(eval_rows)
    top_available = available_count(top)
    return {
        "strategy": strategy,
        "mode": "shadow_only",
        "train_cache_available_count": train_available,
        "train_count": len(train_rows),
        "train_cache_available_rate": round(train_available / max(1, len(train_rows)), 4),
        "eval_cache_available_count": eval_available,
        "eval_count": len(eval_rows),
        "eval_cache_available_rate": round(eval_available / max(1, len(eval_rows)), 4),
        "topk_cache_available_count": top_available,
        "topk": len(top),
        "topk_cache_available_rate": round(top_available / max(1, len(top)), 4),
        "avg_eval_evidence_quality": avg_component(rows, "v25_omni_shadow_evidence_quality"),
        "avg_topk_evidence_quality": avg_component(top, "v25_omni_shadow_evidence_quality"),
        "normalization_versions": dict(versions),
        "writes_labels": False,
        "production_weight": False,
    }


def _omni_shadow_ablation(rows: list[dict], *, k: int) -> dict:
    baseline = _strategy_metrics(rows, RESEARCH_RANKER_V25_SHADOW_STRATEGY, k=k)
    v24 = _strategy_metrics(rows, RESEARCH_RANKER_V24_STRATEGY, k=k)
    variants = {}
    for name in OMNI_SHADOW_ABLATION_STRATEGIES:
        metrics = _strategy_metrics(rows, name, k=k)
        variants[name] = {
            **metrics,
            "lift_delta_vs_v25": round(
                float(metrics.get("topk_lift_vs_random") or 0.0)
                - float(baseline.get("topk_lift_vs_random") or 0.0),
                4,
            ),
            "lift_delta_vs_v2_4": round(
                float(metrics.get("topk_lift_vs_random") or 0.0)
                - float(v24.get("topk_lift_vs_random") or 0.0),
                4,
            ),
        }
    ranked_loss = sorted(
        variants.values(),
        key=lambda item: float(item.get("lift_delta_vs_v25") or 0.0),
    )
    return {
        "baseline_strategy": RESEARCH_RANKER_V25_SHADOW_STRATEGY,
        "base_v2_4_lift": v24.get("topk_lift_vs_random"),
        "v25_lift": baseline.get("topk_lift_vs_random"),
        "variants": variants,
        "largest_lift_loss": ranked_loss[:3],
        "interpretation": _omni_ablation_interpretation(variants),
    }


def _omni_ablation_interpretation(variants: dict[str, dict]) -> str:
    losses = {
        name: abs(float(item.get("lift_delta_vs_v25") or 0.0))
        for name, item in variants.items()
        if float(item.get("lift_delta_vs_v25") or 0.0) < 0
    }
    if not losses:
        return "No ablation reduced lift; current v2.5 shadow gain may be weak or concentrated in ranking side effects."
    strongest = max(losses.items(), key=lambda item: item[1])[0]
    return f"Strongest observed v2.5 contribution: {strongest}; validate on higher Omni coverage before changing production weights."


def _omni_shadow_account_metrics(rows: list[dict], *, k: int) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("account_id") or "unknown")].append(row)
    result = []
    for account, items in grouped.items():
        account_k = max(1, min(int(k or 10), len(items)))
        v24 = _strategy_metrics(items, RESEARCH_RANKER_V24_STRATEGY, k=account_k)
        v25 = _strategy_metrics(items, RESEARCH_RANKER_V25_SHADOW_STRATEGY, k=account_k)
        available = [
            row
            for row in items
            if isinstance(row.get("omni_shadow"), dict)
            and row.get("omni_shadow")
        ]
        evidence_values = [
            float((row.get("component_scores") or {}).get("v25_omni_shadow_evidence_quality") or 0.0)
            for row in items
            if isinstance(row.get("component_scores"), dict)
        ]
        result.append(
            {
                "account_id": account,
                "sample_count": len(items),
                "status": "ready" if len(items) >= 10 else "low_confidence",
                "omni_cache_available_count": len(available),
                "omni_cache_available_rate": round(len(available) / max(1, len(items)), 4),
                "avg_omni_evidence_quality": round(sum(evidence_values) / max(1, len(evidence_values)), 4),
                "v2_4_topk_lift_vs_random": v24.get("topk_lift_vs_random"),
                "v2_5_topk_lift_vs_random": v25.get("topk_lift_vs_random"),
                "lift_delta_vs_v2_4": round(
                    float(v25.get("topk_lift_vs_random") or 0.0)
                    - float(v24.get("topk_lift_vs_random") or 0.0),
                    4,
                ),
                "v2_4_high_interaction_hit_rate": v24.get("high_interaction_hit_rate"),
                "v2_5_high_interaction_hit_rate": v25.get("high_interaction_hit_rate"),
                "v2_4_low_interaction_avoidance_rate": v24.get("low_interaction_avoidance_rate"),
                "v2_5_low_interaction_avoidance_rate": v25.get("low_interaction_avoidance_rate"),
                "improved_vs_v2_4": float(v25.get("topk_lift_vs_random") or 0.0) > float(v24.get("topk_lift_vs_random") or 0.0),
            }
        )
    result.sort(
        key=lambda item: (
            item["status"] == "ready",
            float(item.get("omni_cache_available_rate") or 0.0),
            float(item.get("lift_delta_vs_v2_4") or 0.0),
            int(item.get("sample_count") or 0),
        ),
        reverse=True,
    )
    return result


def _duplicate_key_counts(rows: list[dict]) -> dict[str, int]:
    counters = {
        "title": Counter(_stable_title_key(row.get("title")) for row in rows if _stable_title_key(row.get("title"))),
        "song": Counter(_diversity_key(row.get("song_title")) for row in rows if _diversity_key(row.get("song_title"))),
        "artist": Counter(_diversity_key(row.get("artist_names")) for row in rows if _diversity_key(row.get("artist_names"))),
    }
    return {key: sum(1 for count in counter.values() if count > 1) for key, counter in counters.items()}


def _diversity_diagnostic_rows(rows: list[dict], strategy: str, *, k: int) -> list[dict]:
    top = rows[: max(10, int(k or 10) * 2)]
    diagnostics = []
    if strategy == RESEARCH_RANKER_V26_POOL_STRATEGY:
        penalty_key = "v26_pool_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V27_MATERIAL_STRATEGY:
        penalty_key = "v27_material_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V25_SHADOW_STRATEGY:
        penalty_key = "v25_shadow_diversity_penalty"
    elif strategy == RESEARCH_RANKER_V24_STRATEGY:
        penalty_key = "v24_diversity_penalty"
    else:
        penalty_key = "v23_diversity_penalty"
    seen_titles: Counter[str] = Counter()
    seen_songs: Counter[str] = Counter()
    for row in top:
        title_key = _stable_title_key(row.get("title"))
        song_key = _diversity_key(row.get("song_title"))
        penalty = float((row.get("component_scores") or {}).get(penalty_key) or 0.0)
        if penalty > 0 or (title_key and seen_titles[title_key]) or (song_key and seen_songs[song_key]):
            item = _diagnostic_row(row, strategy)
            item["diversity_penalty"] = round(penalty, 4)
            item["duplicate_title_seen"] = int(seen_titles[title_key]) if title_key else 0
            item["duplicate_song_seen"] = int(seen_songs[song_key]) if song_key else 0
            diagnostics.append(item)
        if title_key:
            seen_titles[title_key] += 1
        if song_key:
            seen_songs[song_key] += 1
        if len(diagnostics) >= 12:
            break
    return diagnostics


def _diagnostic_recommended_fields(row: dict) -> list[str]:
    fields = []
    components = row.get("component_scores") if isinstance(row.get("component_scores"), dict) else {}
    if float(components.get("v27_material_conflict") or 0.0) > 0:
        fields.extend(["domain_category", "material_type", "program_context", "presentation_style"])
    for field in ["content_category", "hook_type", "slice_structure", "artist_names", "song_title"]:
        value = str(row.get(field) or "").strip().lower()
        if not value or value in {"unknown", "none", "null"}:
            fields.append(field)
    if not fields:
        return ["content_category", "hook_type", "slice_structure"]
    return list(dict.fromkeys(fields))


def _tuning_objective(metrics: dict, promotion_gate: dict) -> float:
    lift = float(metrics.get("topk_lift_vs_random") or 0.0)
    high_hit = float(metrics.get("high_interaction_hit_rate") or 0.0)
    avoidance = float(metrics.get("low_interaction_avoidance_rate") or 0.0)
    account_bonus = min(0.5, float(promotion_gate.get("improved_ready_account_count") or 0) * 0.04)
    threshold_bonus = 0.3 if lift >= 1.70 and high_hit >= 0.70 else 0.0
    return round(lift + high_hit * 0.55 + avoidance * 0.15 + account_bonus + threshold_bonus, 6)


def _ndcg_at_k(rows: list[dict], k: int) -> float:
    actual = [_gain(row) for row in rows[:k]]
    ideal = sorted((_gain(row) for row in rows), reverse=True)[:k]
    dcg = _dcg(actual)
    idcg = _dcg(ideal)
    return round(dcg / idcg, 4) if idcg > 0 else 0.0


def _dcg(gains: list[float]) -> float:
    return sum(((2**gain) - 1) / math.log2(index + 2) for index, gain in enumerate(gains))


def _gain(row: dict) -> float:
    return max(0.0, min(5.0, float(row.get("normalized_reward") or row.get("reward_proxy") or 0) / 20.0))


def _topk_hit_rate(score_rows: list[dict], reward_rows: list[dict], k: int) -> float:
    limit = max(1, min(k, len(score_rows), len(reward_rows)))
    predicted = {row["training_sample_id"] for row in score_rows[:limit]}
    ideal = {row["training_sample_id"] for row in reward_rows[:limit]}
    return round(len(predicted & ideal) / limit, 4)


def _topk_lift_vs_random(score_rows: list[dict], all_rows: list[dict], k: int) -> float:
    if not score_rows or not all_rows:
        return 0.0
    limit = max(1, min(k, len(score_rows)))
    top_avg = sum(float(row.get("normalized_reward") or row.get("reward_proxy") or 0) for row in score_rows[:limit]) / limit
    random_avg = sum(float(row.get("normalized_reward") or row.get("reward_proxy") or 0) for row in all_rows) / max(1, len(all_rows))
    if random_avg <= 0:
        return 0.0
    return round(top_avg / random_avg, 4)


def _high_interaction_hit_rate(score_rows: list[dict], all_rows: list[dict], k: int) -> float:
    limit = max(1, min(k, len(score_rows)))
    thresholds = _interaction_thresholds(all_rows)
    high = sum(1 for row in score_rows[:limit] if _interaction_label(row, thresholds) == "high")
    return round(high / limit, 4)


def _low_interaction_avoidance_rate(score_rows: list[dict], all_rows: list[dict], k: int) -> float:
    limit = max(1, min(k, len(score_rows)))
    thresholds = _interaction_thresholds(all_rows)
    low = sum(1 for row in score_rows[:limit] if _interaction_label(row, thresholds) == "low")
    return round((limit - low) / limit, 4)


def _interaction_label_counts(rows: list[dict]) -> dict[str, int]:
    thresholds = _interaction_thresholds(rows)
    counts = Counter(_interaction_label(row, thresholds) for row in rows)
    return {key: int(counts.get(key, 0)) for key in ["high", "mid", "low"]}


def _interaction_label(row: dict, thresholds: tuple[float, float]) -> str:
    label = str(row.get("performance_label") or "").strip().lower()
    if label in {"high", "mid", "low"}:
        return label
    value = float(row.get("normalized_reward") or row.get("reward_proxy") or 0)
    low_threshold, high_threshold = thresholds
    if value >= high_threshold:
        return "high"
    if value <= low_threshold:
        return "low"
    return "mid"


def _interaction_thresholds(rows: list[dict]) -> tuple[float, float]:
    values = sorted(float(row.get("normalized_reward") or row.get("reward_proxy") or 0) for row in rows)
    if not values:
        return (0.0, 0.0)
    return (_quantile(values, 0.25), _quantile(values, 0.75))


def _quantile(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    position = max(0.0, min(1.0, q)) * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[int(position)]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _holdout_policy(source: str, policy: str = "time", *, split_summary: dict | None = None) -> str:
    if source == "historical_capture_samples":
        if policy == "time":
            summary = split_summary or {}
            return (
                "account-local published_at time split, first 80% as history and last 20% as validation; "
                f"hash fallback accounts={len(summary.get('hash_fallback_accounts') or [])}; no future sample leakage."
            )
        return "sha256(platform_item_id) % 5 == 0 as holdout fallback; no new-slice publication feedback is assumed."
    return "training samples use imported feedback rows; no automatic publication action is assumed."


def _metric_basis(source: str) -> str:
    if source == "historical_capture_samples":
        return "published-video visible engagement proxy from likes/comments/favorites/shares; play/view count is not imputed."
    return "imported feedback reward_proxy/normalized_reward."


def _calibration_mae(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    errors = [
        abs(float(row.get("final_score") or 0) - float(row.get("normalized_reward") or row.get("reward_proxy") or 0))
        for row in rows
    ]
    return round(sum(errors) / len(errors), 4)


def _low_exposure_uncertain_rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    uncertain = [
        row
        for row in rows
        if int(row.get("impressions") or 0) < 300 or float(row.get("uncertainty") or 0) >= 0.5
    ]
    return round(len(uncertain) / len(rows), 4)


def _report_payload(
    account_id: str | None,
    *,
    status: str,
    rows: list[dict],
    k: int,
    metrics: dict[str, Any] | None = None,
    query_extra: dict[str, Any] | None = None,
) -> dict:
    query = {"account_id": account_id or "all", "k": k}
    if query_extra:
        query.update(query_extra)
    selected_strategy = _normalize_strategy(str(query.get("strategy") or RESEARCH_RANKER_V24_STRATEGY))
    empty_gate = _promotion_gate({}, [], strategy=selected_strategy)
    return {
        "contract_version": BACKTEST_VERSION,
        "status": status,
        "account_id": account_id or "all",
        "report_name": "rules_backtest",
        "generated_at": utc_now(),
        "query": query,
        "metrics": metrics or {
            "sample_count": 0,
            "k": k,
            "strategy": selected_strategy,
            "ndcg_at_k": 0.0,
            "topk_hit_rate": 0.0,
            "topk_lift_vs_random": 0.0,
            "high_interaction_hit_rate": 0.0,
            "low_interaction_avoidance_rate": 0.0,
            "calibration_mae": 0.0,
            "closed_loop_rate": 0.0,
            "low_exposure_uncertain_rate": 0.0,
            "sample_source": "training_samples",
            "holdout_policy": _holdout_policy("training_samples"),
            "holdout_policy_key": "training",
            "metric_basis": _metric_basis("training_samples"),
            "label_counts": {"high": 0, "mid": 0, "low": 0},
            "research_label_version": RESEARCH_LABEL_VERSION,
            "research_ranker_version": RESEARCH_RANKER_VERSION,
            "scorer_version": SCORER_VERSION,
            "risk_note": "离线排序研究报告，不代表发布预测、流量预测或播放量预测。",
            "strategy_comparison": {},
            "per_account_metrics": [],
            "component_ablation": {},
            "promotion_gate": empty_gate,
            "weight_config": _weight_config_for_strategy(selected_strategy),
            "baseline_gap": _baseline_gap({}, selected_strategy),
            "semantic_gap_analysis": _semantic_gap_analysis({}, selected_strategy),
            "diagnostic_samples": {},
            "diversity_summary": {},
            "leakage_guard_summary": {},
            "next_calibration_queue": [],
            "calibration_summary": _calibration_summary({}, empty_gate, selected_strategy),
            "embedding_coverage": {},
            "embedding_evidence_summary": {},
            "embedding_strategy_gap": embedding_strategy_gap({}, selected_strategy=TEXT_VISUAL_EMBEDDING_STRATEGY),
            "omni_shadow_summary": {},
            "omni_shadow_ablation": {},
            "omni_shadow_account_metrics": [],
            "omni_trust_profiles": [],
            "omni_pool_report": {},
            "omni_pool_gate": {},
            "omni_account_pool_gates": [],
            "omni_account_pool_summary": {},
            "omni_material_report": {},
            "omni_material_gate": {},
            "omni_material_gold_set_queue": [],
            "omni_material_calibration": {},
            "omni_material_calibration_holdout": {},
            "omni_material_gold_split": {},
            "omni_material_router_profiles": [],
            "omni_material_taxonomy_router_profiles": [],
            "omni_material_v28_report": {},
            "omni_material_v28_gate": {},
            "omni_material_v29_report": {},
            "omni_material_v29_gate": {},
        },
        "top_rows": rows,
    }


def _store_report(report: dict) -> None:
    with connect() as conn:
        insert_row(
            conn,
            "backtest_reports",
            {
                "id": new_id("bt"),
                "account_id": report["account_id"],
                "report_name": report["report_name"],
                "status": report["status"],
                "metrics_json": json.dumps(
                    {
                        "metrics": report.get("metrics") or {},
                        "top_rows": report.get("top_rows") or [],
                    },
                    ensure_ascii=False,
                ),
                "created_at": report["generated_at"],
            },
        )
        conn.commit()
