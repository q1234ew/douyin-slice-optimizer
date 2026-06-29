from __future__ import annotations

import json
import math
import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.utils import clamp, new_id, utc_now
from dso.versions import BACKTEST_VERSION, RESEARCH_LABEL_VERSION, RESEARCH_RANKER_VERSION, SCORER_VERSION


BACKTEST_STRATEGIES = {
    "current_rules",
    "semantic_baseline_v2",
    "research_ranker_v2",
    "research_ranker_v2_1",
    "research_ranker_v2_2",
    "ranker_without_prototypes",
    "ranker_without_low_risk",
}
RESEARCH_RANKER_V21_STRATEGY = "research_ranker_v2_1"
RESEARCH_RANKER_V22_STRATEGY = "research_ranker_v2_2"
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
    "semantic_floor_weight": 1.0,
    "semantic_strong_weight": 1.0,
    "high_similarity_weight": 0.0,
    "low_risk_weight": 0.1,
    "prototype_weight": 0.0,
    "semantic_trust_weight": 0.0,
    "novelty_weight": 0.0,
    "evidence_threshold": 0.35,
    "bias": 0.0,
}
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


def backtest_rule_ranker(
    account_id: str | None = None,
    *,
    k: int = 10,
    strategy: str = RESEARCH_RANKER_V22_STRATEGY,
    holdout_policy: str = "time",
    label_version: str | None = None,
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
    ranked_by_score = sorted(rows, key=lambda row: float(row.get("final_score") or 0), reverse=True)
    ranked_by_reward = sorted(rows, key=lambda row: float(row.get("normalized_reward") or row.get("reward_proxy") or 0), reverse=True)
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
        "leakage_guard_summary": historical_dataset.get("leakage_guard_summary") or {},
        "next_calibration_queue": historical_dataset.get("next_calibration_queue") or [],
        "calibration_summary": historical_dataset.get("calibration_summary")
        or _calibration_summary(strategy_comparison, promotion_gate, selected_strategy),
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


def list_backtest_reports(account_id: str | None = None, limit: int = 10) -> dict:
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
        row["metrics"] = payload.get("metrics") or {}
        row["top_rows"] = payload.get("top_rows") or []
        row["contract_version"] = BACKTEST_VERSION
        row["generated_at"] = row.get("created_at")
    return {
        "contract_version": BACKTEST_VERSION,
        "account_id": account_id or "all",
        "count": len(rows),
        "reports": rows,
    }


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
        strategy=RESEARCH_RANKER_V22_STRATEGY,
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
            "strategy": RESEARCH_RANKER_V22_STRATEGY,
            "trials": [],
            "best": None,
            "generated_at": utc_now(),
        }
    trials = []
    for config in RANKER_TUNING_CANDIDATES_V22[: max(1, int(max_trials or 1))]:
        trial_rows = _rows_with_v22_config(rows, config)
        metrics = _strategy_metrics(trial_rows, RESEARCH_RANKER_V22_STRATEGY, k=k)
        per_account = _per_account_metrics(trial_rows, RESEARCH_RANKER_V22_STRATEGY)
        gate = _promotion_gate(
            {RESEARCH_RANKER_V22_STRATEGY: metrics},
            per_account,
            strategy=RESEARCH_RANKER_V22_STRATEGY,
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
        strategy_comparison[RESEARCH_RANKER_V22_STRATEGY] = best["metrics"]
    return {
        "contract_version": BACKTEST_VERSION,
        "status": "ready" if best else "empty",
        "account_id": account_id or "all",
        "strategy": RESEARCH_RANKER_V22_STRATEGY,
        "holdout_policy": dataset.get("holdout_policy_key") or holdout_policy,
        "sample_count": len(rows),
        "k": k,
        "trials": trials,
        "best": best,
        "best_weight_config": (best or {}).get("weight_config") if best else None,
        "strategy_comparison": strategy_comparison,
        "per_account_metrics": (best or {}).get("per_account_metrics") if best else [],
        "promotion_gate": (best or {}).get("promotion_gate") if best else _promotion_gate({}, [], strategy=RESEARCH_RANKER_V22_STRATEGY),
        "baseline_gap": _baseline_gap(strategy_comparison, RESEARCH_RANKER_V22_STRATEGY),
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
    value = str(strategy or RESEARCH_RANKER_V22_STRATEGY).strip()
    return value if value in BACKTEST_STRATEGIES else RESEARCH_RANKER_V22_STRATEGY


def _historical_backtest_dataset(
    account_id: str | None,
    *,
    strategy: str,
    holdout_policy: str,
    label_version: str | None,
    k: int = 10,
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
    train_rows, eval_rows, actual_policy, split_summary = _historical_holdout_split(all_rows, holdout_policy)
    if not eval_rows:
        eval_rows = all_rows
    train_rows, leakage_summary = _apply_leakage_guard(train_rows or all_rows, eval_rows)
    split_summary = {**split_summary, "train_count_after_leakage_guard": len(train_rows)}
    train_basis = _prepare_history_tokens(train_rows or all_rows)
    history_index = _history_candidate_index(train_basis)
    eval_basis = _prepare_history_tokens(eval_rows)
    baselines = _historical_group_baselines(train_basis)
    scored = []
    for row in eval_basis:
        reward = float(row.get("normalized_reward") or row.get("reward_proxy") or 0)
        strategy_scores, component_scores = _historical_strategy_scores(row, train_basis, baselines, history_index=history_index)
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
                "rights_risk_score": 0,
                "low_originality_score": 0,
                "account_id": row.get("account_id") or "",
                "title": row.get("title") or "",
                "platform_item_id": row.get("platform_item_id") or "",
                "performance_label": row.get("performance_label") or "",
                "content_category": row.get("content_category") or "",
                "hook_type": row.get("hook_type") or "",
                "slice_structure": row.get("slice_structure") or "",
                "program_name": row.get("program_name") or "",
                "artist_names": row.get("artist_names") or "",
                "song_title": row.get("song_title") or "",
                "tags": row.get("tags") or "",
                "classification_confidence": row.get("classification_confidence") or "",
                "semantic_unknown_reason": row.get("semantic_unknown_reason") or "",
                "semantic_feature_version": row.get("semantic_feature_version") or "",
                "research_label_version": row.get("research_label_version") or RESEARCH_LABEL_VERSION,
                "published_at": row.get("published_at") or "",
                "holdout_policy": actual_policy,
            }
        )
    strategy_comparison = {
        name: _strategy_metrics(scored, name, k=k)
        for name in [
            "current_rules",
            "semantic_baseline_v2",
            "research_ranker_v2",
            RESEARCH_RANKER_V21_STRATEGY,
            RESEARCH_RANKER_V22_STRATEGY,
            "ranker_without_prototypes",
            "ranker_without_low_risk",
        ]
    }
    per_account = _per_account_metrics(scored, strategy)
    component_ablation = {
        "ranker_without_prototypes": strategy_comparison["ranker_without_prototypes"],
        "ranker_without_low_risk": strategy_comparison["ranker_without_low_risk"],
    }
    return {
        "rows": scored,
        "strategy_comparison": strategy_comparison,
        "per_account_metrics": per_account,
        "component_ablation": component_ablation,
        "promotion_gate": _promotion_gate(strategy_comparison, per_account, strategy=strategy),
        "weight_config": _weight_config_for_strategy(strategy),
        "baseline_gap": _baseline_gap(strategy_comparison, strategy),
        "semantic_gap_analysis": _semantic_gap_analysis(strategy_comparison, strategy),
        "diagnostic_samples": _diagnostic_samples(scored, strategy, k=k),
        "leakage_guard_summary": leakage_summary,
        "next_calibration_queue": _next_calibration_queue(scored, strategy, k=k),
        "calibration_summary": _calibration_summary(
            strategy_comparison,
            _promotion_gate(strategy_comparison, per_account, strategy=strategy),
            strategy,
        ),
        "holdout_policy_key": actual_policy,
        "holdout_policy_text": _holdout_policy("historical_capture_samples", actual_policy, split_summary=split_summary),
        "label_version": label_version or RESEARCH_LABEL_VERSION,
        "split_summary": split_summary,
    }


def _historical_backtest_rows(account_id: str | None) -> list[dict]:
    return _historical_backtest_dataset(
        account_id,
        strategy=RESEARCH_RANKER_V22_STRATEGY,
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
            ORDER BY updated_at DESC
            """,
            params,
        )


def _prepare_history_tokens(rows: list[dict]) -> list[dict]:
    prepared = []
    for row in rows:
        item = dict(row)
        text = _history_text(item)
        item["_history_text"] = text
        item["_history_tokens"] = _history_tokens(text)
        prepared.append(item)
    return prepared


def _history_candidate_index(rows: list[dict]) -> dict[str, Any]:
    token_index: dict[str, list[int]] = defaultdict(list)
    field_index: dict[str, dict[str, list[int]]] = {
        field: defaultdict(list)
        for field in ["account_id", "content_category", "hook_type", "slice_structure", "artist_names", "song_title"]
    }
    for index, row in enumerate(rows):
        tokens = row.get("_history_tokens")
        if not isinstance(tokens, set):
            continue
        for token in tokens:
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
            ordered = [row for row, _ in sorted(timed, key=lambda item: item[1] or datetime.min.replace(tzinfo=timezone.utc))]
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
    for field in ["account_id", "content_category", "hook_type", "slice_structure", "artist_names", "song_title"]:
        groups: dict[str, list[float]] = {}
        for row in rows:
            key = str(row.get(field) or "")
            if not key:
                continue
            groups.setdefault(key, []).append(float(row.get("normalized_reward") or row.get("reward_proxy") or 0))
        baselines[field] = {key: sum(values) / max(1, len(values)) for key, values in groups.items() if values}
    return baselines


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
) -> tuple[dict[str, float], dict[str, float]]:
    current = _historical_predicted_score(row, baselines)
    semantic = _semantic_baseline_v2(row, baselines)
    components = _historical_research_ranker_components(row, train_rows, semantic, history_index=history_index)
    ranker = _score_from_components(components, semantic, use_prototypes=True, use_low_risk=True)
    ranker_v21 = _score_v21_from_components(components, config=RESEARCH_RANKER_V21_WEIGHT_CONFIG)
    ranker_v22 = _score_v22_from_components(components, config=RESEARCH_RANKER_V22_WEIGHT_CONFIG)
    no_prototypes = _score_from_components(components, semantic, use_prototypes=False, use_low_risk=True)
    no_low_risk = _score_from_components(components, semantic, use_prototypes=True, use_low_risk=False)
    return (
        {
            "current_rules": current,
            "semantic_baseline_v2": semantic,
            "research_ranker_v2": ranker,
            RESEARCH_RANKER_V21_STRATEGY: ranker_v21,
            RESEARCH_RANKER_V22_STRATEGY: ranker_v22,
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
) -> tuple[float, dict[str, float]]:
    components = _historical_research_ranker_components(row, train_rows, semantic_score, history_index=history_index)
    return _score_from_components(components, semantic_score, use_prototypes=use_prototypes, use_low_risk=use_low_risk), components


def _historical_research_ranker_components(
    row: dict,
    train_rows: list[dict],
    semantic_score: float,
    *,
    history_index: dict[str, Any] | None = None,
) -> dict[str, float]:
    cached_tokens = row.get("_history_tokens")
    target_tokens = cached_tokens if isinstance(cached_tokens, set) else _history_tokens(_history_text(row))
    candidate_rows = _candidate_history_rows(row, target_tokens, train_rows, history_index)
    high_score = 0.0
    low_risk = 0.0
    best_similarity = 0.0
    high_matches: list[tuple[float, float, dict]] = []
    low_matches: list[tuple[float, float, dict]] = []
    thresholds = _interaction_thresholds(train_rows)
    for sample in candidate_rows:
        similarity = _history_similarity(target_tokens, row, sample)
        if similarity <= 0:
            continue
        reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0)
        label = _interaction_label(sample, thresholds)
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
    positive_gate = evidence if evidence >= threshold else evidence * 0.25
    risk_gate = 1.0 if risk > 0.0 else 0.0
    score = (
        semantic
        + high * float(weights.get("high_similarity_weight") or 0.0) * positive_gate
        - risk * float(weights.get("low_risk_weight") or 0.0) * risk_gate
        + prototype * float(weights.get("prototype_weight") or 0.0) * positive_gate
        + (trust - 50.0) * float(weights.get("semantic_trust_weight") or 0.0)
        + novelty * float(weights.get("novelty_weight") or 0.0)
        + float(weights.get("bias") or 0.0)
    )
    return round(clamp(score), 4)


def _evidence_quality_from_components(components: dict[str, float]) -> float:
    high = min(1.0, float(components.get("high_similarity") or 0.0) / 100.0)
    risk = min(1.0, float(components.get("low_interaction_risk") or 0.0) / 100.0)
    prototype = min(1.0, float(components.get("prototype_fit") or 0.0) / 100.0)
    trust = min(1.0, max(0.0, float(components.get("semantic_label_trust") or 0.0) / 100.0))
    similarity = min(1.0, max(0.0, float(components.get("best_similarity") or 0.0)))
    return round(max(similarity, high * 0.45 + risk * 0.35 + prototype * 0.2, trust * 0.35), 4)


def _weight_config_for_strategy(strategy: str) -> dict[str, Any]:
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
    limit: int = 520,
) -> list[dict]:
    if len(train_rows) <= limit or not history_index or not target_tokens:
        return train_rows
    indexed_rows = history_index.get("rows") if isinstance(history_index.get("rows"), list) else train_rows
    token_index = history_index.get("token_index") if isinstance(history_index.get("token_index"), dict) else {}
    field_index = history_index.get("field_index") if isinstance(history_index.get("field_index"), dict) else {}
    counts: Counter[int] = Counter()
    for token in target_tokens:
        for index in token_index.get(token, []):
            counts[int(index)] += 1
    for field in ["account_id", "content_category", "hook_type", "slice_structure", "artist_names", "song_title"]:
        key = _index_key(row.get(field))
        if not key:
            continue
        bonus = 3 if field in {"account_id", "content_category"} else 2
        for index in (field_index.get(field) or {}).get(key, []):
            counts[int(index)] += bonus
    if not counts:
        return []
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)[: max(1, int(limit))]
    return [indexed_rows[index] for index, _ in ranked if 0 <= index < len(indexed_rows)]


def _index_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _history_text(row: dict) -> str:
    cached = row.get("_history_text")
    if isinstance(cached, str):
        return cached
    return " ".join(
        str(row.get(key) or "")
        for key in ["title", "tags", "artist_names", "song_title", "program_name", "content_category", "hook_type", "slice_structure"]
    )


def _history_similarity(target_tokens: set[str], row: dict, sample: dict) -> float:
    cached_tokens = sample.get("_history_tokens")
    sample_tokens = cached_tokens if isinstance(cached_tokens, set) else _history_tokens(_history_text(sample))
    if not target_tokens or not sample_tokens:
        token_score = 0.0
    else:
        token_score = len(target_tokens & sample_tokens) / max(1, len(target_tokens | sample_tokens))
    semantic_bonus = 0.0
    for field, bonus in [("content_category", 0.09), ("hook_type", 0.08), ("slice_structure", 0.07), ("artist_names", 0.05), ("song_title", 0.04)]:
        if row.get(field) and sample.get(field) and str(row.get(field)).strip().lower() == str(sample.get(field)).strip().lower():
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
    ranked_by_score = sorted(strategy_rows, key=lambda row: float(row.get("final_score") or 0), reverse=True)
    ranked_by_reward = sorted(strategy_rows, key=lambda row: float(row.get("normalized_reward") or row.get("reward_proxy") or 0), reverse=True)
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
    strategy: str = RESEARCH_RANKER_V22_STRATEGY,
) -> dict:
    target = strategy_comparison.get(strategy) or strategy_comparison.get("research_ranker_v2") or {}
    ready_improved = [
        item
        for item in per_account_metrics
        if item.get("status") == "ready" and item.get("improved_vs_current_rules")
    ]
    topk_lift = float(target.get("topk_lift_vs_random") or 0.0)
    high_hit = float(target.get("high_interaction_hit_rate") or 0.0)
    low_avoidance = float(target.get("low_interaction_avoidance_rate") or 0.0)
    if strategy == RESEARCH_RANKER_V22_STRATEGY:
        required_lift = 1.85
        required_high_hit = 0.90
        required_low_avoidance = 0.95
        required_accounts = 10
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
    passed = (
        topk_lift >= required_lift
        and high_hit >= required_high_hit
        and low_avoidance >= required_low_avoidance
        and len(ready_improved) >= required_accounts
    )
    return {
        "passed": passed,
        "status": "pass" if passed else "research_only",
        "strategy": strategy,
        "topk_lift_vs_random": round(topk_lift, 4),
        "high_interaction_hit_rate": round(high_hit, 4),
        "low_interaction_avoidance_rate": round(low_avoidance, 4),
        "required_topk_lift_vs_random": required_lift,
        "required_high_interaction_hit_rate": required_high_hit,
        "required_low_interaction_avoidance_rate": required_low_avoidance,
        "improved_ready_account_count": len(ready_improved),
        "required_improved_ready_account_count": required_accounts,
        "decision": "eligible_for_stronger_weight" if passed else "keep_as_research_evidence",
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
    return {
        "strategy": strategy,
        "status": "eligible_for_stronger_weight" if promotion_gate.get("passed") else "research_only",
        "semantic_baseline_ahead": float(semantic.get("topk_lift_vs_random") or 0) > float(target.get("topk_lift_vs_random") or 0),
        "target_topk_lift_vs_random": float(target.get("topk_lift_vs_random") or 0),
        "target_high_interaction_hit_rate": float(target.get("high_interaction_hit_rate") or 0),
        "required_topk_lift_vs_random": 1.85 if strategy == RESEARCH_RANKER_V22_STRATEGY else 1.70 if strategy == RESEARCH_RANKER_V21_STRATEGY else 1.10,
        "required_high_interaction_hit_rate": 0.90 if strategy == RESEARCH_RANKER_V22_STRATEGY else 0.70 if strategy == RESEARCH_RANKER_V21_STRATEGY else 0.0,
        "required_low_interaction_avoidance_rate": 0.95 if strategy == RESEARCH_RANKER_V22_STRATEGY else 0.0,
    }


def _semantic_gap_analysis(strategy_comparison: dict, strategy: str) -> dict:
    gap = _baseline_gap(strategy_comparison, strategy)
    target = strategy_comparison.get(strategy) or {}
    semantic = strategy_comparison.get("semantic_baseline_v2") or {}
    required_gap = 0.03 if strategy == RESEARCH_RANKER_V22_STRATEGY else 0.0
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


def _diagnostic_samples(rows: list[dict], strategy: str, *, k: int) -> dict:
    if not rows:
        return {
            "missed_high_interaction": [],
            "low_interaction_false_positive": [],
            "semantic_disagreements": [],
        }
    thresholds = _interaction_thresholds(rows)
    ranked = _rows_for_strategy(rows, strategy)
    ranked.sort(key=lambda row: float(row.get("final_score") or 0.0), reverse=True)
    top_ids = {row.get("training_sample_id") for row in ranked[: max(1, int(k or 10))]}
    high_rows = [
        row for row in rows if _interaction_label(row, thresholds) == "high" and row.get("training_sample_id") not in top_ids
    ]
    high_rows.sort(key=lambda row: float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), reverse=True)
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
    disagreement_rows.sort(key=lambda row: float(row.get("_semantic_delta_abs") or 0.0), reverse=True)
    return {
        "missed_high_interaction": [_diagnostic_row(row, strategy) for row in high_rows[:12]],
        "low_interaction_false_positive": [_diagnostic_row(row, strategy) for row in low_false[:12]],
        "semantic_disagreements": [_diagnostic_row(row, strategy) for row in disagreement_rows[:12]],
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
    }


def _next_calibration_queue(rows: list[dict], strategy: str, *, k: int) -> list[dict]:
    diagnostics = _diagnostic_samples(rows, strategy, k=k)
    queue: list[dict] = []
    seen = set()
    for queue_type, reason in [
        ("missed_high_interaction", "high_interaction_missed_by_ranker"),
        ("low_interaction_false_positive", "low_interaction_false_positive"),
        ("semantic_disagreements", "semantic_ranker_disagreement"),
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


def _diagnostic_recommended_fields(row: dict) -> list[str]:
    fields = []
    for field in ["content_category", "hook_type", "slice_structure", "artist_names", "song_title"]:
        value = str(row.get(field) or "").strip().lower()
        if not value or value in {"unknown", "none", "null"}:
            fields.append(field)
    return fields or ["content_category", "hook_type", "slice_structure"]


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
            "strategy": RESEARCH_RANKER_V22_STRATEGY,
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
            "promotion_gate": _promotion_gate({}, [], strategy=RESEARCH_RANKER_V22_STRATEGY),
            "weight_config": RESEARCH_RANKER_V22_WEIGHT_CONFIG,
            "baseline_gap": _baseline_gap({}, RESEARCH_RANKER_V22_STRATEGY),
            "semantic_gap_analysis": _semantic_gap_analysis({}, RESEARCH_RANKER_V22_STRATEGY),
            "diagnostic_samples": {},
            "leakage_guard_summary": {},
            "next_calibration_queue": [],
            "calibration_summary": _calibration_summary({}, _promotion_gate({}, [], strategy=RESEARCH_RANKER_V22_STRATEGY), RESEARCH_RANKER_V22_STRATEGY),
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
