from __future__ import annotations

from typing import Any

from dso.versions import PRODUCTION_RANKING_POLICY_VERSION, RESEARCH_RANKER_VERSION, SCORER_VERSION


PRODUCTION_RANKING_SCOPE = "production"
RESEARCH_RANKING_SCOPE = "research"
PRODUCTION_RANKING_STRATEGY = "current_rules"
PRODUCTION_SCORE_FIELD = "final_score"

RESEARCH_RANKER_PROMOTION_THRESHOLDS = {
    "required_topk_lift_vs_random": 1.85,
    "required_high_interaction_hit_rate": 0.90,
    "required_low_interaction_avoidance_rate": 0.95,
    "required_improved_ready_account_count": 10,
    "required_lift_delta_vs_strongest_baseline": 0.03,
    "required_ndcg_delta_vs_strongest_baseline": 0.0,
    "required_high_hit_delta_vs_strongest_baseline": 0.0,
    "required_low_avoidance_delta_vs_strongest_baseline": 0.0,
}


def normalize_ranking_scope(value: str | None) -> str:
    scope = str(value or PRODUCTION_RANKING_SCOPE).strip().lower()
    if scope not in {PRODUCTION_RANKING_SCOPE, RESEARCH_RANKING_SCOPE}:
        raise ValueError("ranking_scope must be production or research")
    return scope


def attach_ranking_policy(row: dict[str, Any], *, ranking_scope: str | None = None) -> dict[str, Any]:
    item = dict(row)
    scope = normalize_ranking_scope(ranking_scope)
    production_score = _optional_score(item.get(PRODUCTION_SCORE_FIELD))
    research_score, research_strategy = _research_score(item)
    selected_score = production_score if scope == PRODUCTION_RANKING_SCOPE else research_score
    item.update(
        {
            "ranking_scope": scope,
            "effective_score": selected_score,
            "production_score": production_score,
            "production_ranking_strategy": PRODUCTION_RANKING_STRATEGY,
            "production_ranking_version": PRODUCTION_RANKING_POLICY_VERSION,
            "production_score_field": PRODUCTION_SCORE_FIELD,
            "production_status": "adopted_baseline",
            "research_score": research_score,
            "research_ranking_strategy": research_strategy,
            "research_ranker_version": str(item.get("ranker_version") or RESEARCH_RANKER_VERSION),
            "research_promotion_status": "research_only",
            "research_score_delta_vs_production": (
                round(research_score - production_score, 4)
                if research_score is not None and production_score is not None
                else None
            ),
            "ranking_policy_reason": (
                "默认排序使用已采用的 current_rules；历史证据和多模态分仅供研究对照。"
                if scope == PRODUCTION_RANKING_SCOPE
                else "显式研究视图使用未晋级分数，不改变默认排序、审核或导出。"
            ),
        }
    )
    return item


def ranking_sort_key(row: dict[str, Any], *, ranking_scope: str | None = None) -> tuple[float, float, str]:
    item = attach_ranking_policy(row, ranking_scope=ranking_scope)
    return (
        float(item.get("effective_score") or 0.0),
        float(item.get("production_score") or 0.0),
        str(item.get("id") or item.get("candidate_segment_id") or ""),
    )


def production_ranking_contract() -> dict[str, Any]:
    return {
        "contract_version": PRODUCTION_RANKING_POLICY_VERSION,
        "default_scope": PRODUCTION_RANKING_SCOPE,
        "default_strategy": PRODUCTION_RANKING_STRATEGY,
        "default_score_field": PRODUCTION_SCORE_FIELD,
        "default_scorer_version": SCORER_VERSION,
        "research_ranker_version": RESEARCH_RANKER_VERSION,
        "research_status": "research_only",
        "automatic_promotion": False,
        "promotion_thresholds": dict(RESEARCH_RANKER_PROMOTION_THRESHOLDS),
    }


def _research_score(row: dict[str, Any]) -> tuple[float | None, str]:
    hybrid = _nonzero_score(row.get("hybrid_score"))
    if hybrid is not None:
        return hybrid, str(row.get("hybrid_ranker_version") or "hybrid_research")
    ranker = _nonzero_score(row.get("ranker_score"))
    if ranker is not None:
        return ranker, str(row.get("ranker_version") or RESEARCH_RANKER_VERSION)
    return _optional_score(row.get(PRODUCTION_SCORE_FIELD)), PRODUCTION_RANKING_STRATEGY


def _score(value: Any) -> float:
    try:
        return round(float(value or 0.0), 4)
    except (TypeError, ValueError):
        return 0.0


def _optional_score(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _nonzero_score(value: Any) -> float | None:
    parsed = _score(value)
    return parsed if parsed != 0.0 else None
