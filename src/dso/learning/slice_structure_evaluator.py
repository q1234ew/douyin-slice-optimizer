from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from dso.db.session import connect, fetch_all
from dso.learning.semantic_labels import normalize_semantic_field, semantic_label_display
from dso.utils import clamp, utc_now
from dso.versions import DOUYIN_HISTORY_VERSION, SLICE_STRUCTURE_EVALUATOR_VERSION


STRUCTURE_RULES: dict[str, dict[str, Any]] = {
    "climax_first": {
        "label": "高潮先行",
        "positive": ["一开口", "刚开口", "刚唱", "第一句", "上来就", "开口跪", "开头"],
        "context": ["高音", "爆发", "炸", "燃", "惊艳", "封神", "开大"],
        "weight": 28,
    },
    "chorus_first": {
        "label": "副歌先行",
        "positive": ["副歌", "合唱", "大合唱", "全场合唱", "一起唱"],
        "context": ["开口", "开场", "直接", "一上来"],
        "weight": 22,
    },
    "reaction_first": {
        "label": "反应先行",
        "positive": ["reaction", "反应", "全场", "观众", "导师表情", "现场沸腾", "泪目", "尖叫", "掌声"],
        "context": [],
        "weight": 22,
    },
    "quote_first": {
        "label": "金句先行",
        "positive": ["这句话", "一句话", "金句", "名句", "说出", "喊话", "评价太准"],
        "context": [],
        "weight": 21,
    },
    "setup_to_payoff": {
        "label": "铺垫到爆点",
        "positive": ["没想到", "直到", "最后", "反转", "铺垫", "导师", "点评", "晋级", "淘汰", "结果"],
        "context": ["爆发", "高音", "封神", "泪目", "炸场"],
        "weight": 20,
    },
    "context_first": {
        "label": "上下文先行",
        "positive": ["第一次", "原因", "为什么", "如何", "故事", "背景", "解析", "复盘", "教程", "幕后"],
        "context": [],
        "weight": 18,
    },
    "pure_highlight": {
        "label": "纯高光",
        "positive": ["清唱", "无伴奏", "爆发", "直击", "听不够", "高音", "直拍", "名场面", "舞台燃炸"],
        "context": [],
        "weight": 18,
    },
    "linear": {
        "label": "线性叙事",
        "positive": ["vlog", "日常", "记录", "过程", "合集", "盘点", "完整版"],
        "context": [],
        "weight": 14,
    },
}


def evaluate_slice_structure(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 0,
    min_confidence: float = 0.0,
) -> dict:
    rows = _fetch_rows(account_id=account_id, dataset_id=dataset_id, limit=limit)
    evaluations = [evaluate_slice_structure_row(row) for row in rows]
    confidence_floor = max(0.0, float(min_confidence or 0.0))
    if confidence_floor:
        evaluations = [row for row in evaluations if float(row.get("confidence_score") or 0.0) >= confidence_floor]
    structure_distribution = _structure_distribution(evaluations)
    issues = _issue_summary(evaluations)
    queue = _review_queue(evaluations)
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "evaluator_version": SLICE_STRUCTURE_EVALUATOR_VERSION,
        "status": "ready" if rows else "empty",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "sample_count": len(rows),
        "evaluated_count": len(evaluations),
        "coverage": _coverage_summary(evaluations),
        "structure_distribution": structure_distribution,
        "issues": issues,
        "review_queue": queue,
        "recommendations": _recommendations(evaluations, issues),
        "generated_at": utc_now(),
    }


def evaluate_slice_structure_row(row: dict[str, Any]) -> dict:
    current, current_reason = normalize_semantic_field("slice_structure", row.get("slice_structure"))
    text = _row_text(row)
    candidates = _candidate_scores(text)
    best = candidates[0] if candidates else _unknown_candidate("no_structure_signal")
    confidence_score = float(best["confidence_score"])
    confidence_label = _confidence_label(confidence_score)
    current_known = _known(current)
    suggested = str(best["slice_structure"] or "unknown")
    agreement = current_known and current == suggested
    manual_verified = str(row.get("classification_confidence") or "").strip().lower() == "manual_verified"
    status = _evaluation_status(
        current=current,
        suggested=suggested,
        confidence_score=confidence_score,
        agreement=agreement,
        manual_verified=manual_verified,
    )
    priority = _priority_score(row, current=current, suggested=suggested, status=status, confidence_score=confidence_score)
    evidence = best.get("evidence") or []
    return {
        "sample_id": row.get("id") or "",
        "platform_item_id": row.get("platform_item_id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "default",
        "title": row.get("title") or "",
        "performance_label": row.get("performance_label") or "",
        "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
        "current_structure": current,
        "current_structure_label": semantic_label_display("slice_structure", current),
        "current_unknown_reason": current_reason or str(row.get("structure_unknown_reason") or ""),
        "suggested_structure": suggested,
        "suggested_structure_label": semantic_label_display("slice_structure", suggested),
        "confidence_score": round(confidence_score, 4),
        "confidence_label": confidence_label,
        "agreement": bool(agreement),
        "manual_verified": manual_verified,
        "status": status,
        "recommended_action": _recommended_action(status),
        "evidence": evidence,
        "reason": _reason_text(current, suggested, status, evidence),
        "candidate_scores": candidates[:4],
        "priority_score": round(priority, 4),
    }


def _candidate_scores(text: str) -> list[dict]:
    scores = []
    lower = text.lower()
    for structure, rule in STRUCTURE_RULES.items():
        positive_hits = _keyword_hits(text, lower, rule["positive"])
        context_hits = _keyword_hits(text, lower, rule["context"])
        if not positive_hits:
            continue
        base = float(rule["weight"])
        score = base + min(24.0, len(positive_hits) * 8.0) + min(14.0, len(context_hits) * 7.0)
        if structure in {"climax_first", "chorus_first"} and _early_hit(text, positive_hits):
            score += 10.0
        if structure == "setup_to_payoff" and context_hits:
            score += 8.0
        evidence = [*positive_hits[:3], *context_hits[:2]]
        scores.append(
            {
                "slice_structure": structure,
                "label": semantic_label_display("slice_structure", structure),
                "confidence_score": round(clamp(score, 0.0, 100.0), 4),
                "evidence": evidence,
            }
        )
    if not scores:
        return [_unknown_candidate("no_structure_keyword_evidence")]
    scores.sort(key=lambda item: float(item.get("confidence_score") or 0.0), reverse=True)
    if len(scores) > 1:
        margin = float(scores[0]["confidence_score"]) - float(scores[1]["confidence_score"])
        scores[0]["margin_to_second"] = round(margin, 4)
        if margin < 8.0:
            scores[0]["confidence_score"] = round(max(0.0, float(scores[0]["confidence_score"]) - (8.0 - margin)), 4)
    else:
        scores[0]["margin_to_second"] = 100.0
    return scores


def _unknown_candidate(reason: str) -> dict:
    return {
        "slice_structure": "unknown",
        "label": "未知",
        "confidence_score": 0.0,
        "evidence": [],
        "unknown_reason": reason,
        "margin_to_second": 0.0,
    }


def _keyword_hits(text: str, lower: str, keywords: list[str]) -> list[str]:
    hits = []
    for keyword in keywords:
        target = lower if re.search(r"[A-Za-z]", keyword) else text
        probe = keyword.lower() if re.search(r"[A-Za-z]", keyword) else keyword
        if probe in target and keyword not in hits:
            hits.append(keyword)
    return hits


def _early_hit(text: str, hits: list[str]) -> bool:
    first = text[:48]
    return any(hit in first for hit in hits)


def _evaluation_status(
    *,
    current: str,
    suggested: str,
    confidence_score: float,
    agreement: bool,
    manual_verified: bool,
) -> str:
    if manual_verified and _known(current):
        return "manual_verified"
    if not _known(suggested):
        return "unknown"
    if agreement and confidence_score >= 28:
        return "trusted"
    if not _known(current) and confidence_score >= 32:
        return "suggested_update"
    if _known(current) and suggested != current and confidence_score >= 28:
        return "conflict_review"
    if confidence_score < 42:
        return "low_confidence"
    return "needs_review"


def _priority_score(row: dict, *, current: str, suggested: str, status: str, confidence_score: float) -> float:
    reward = float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0)
    label = str(row.get("performance_label") or "").strip().lower()
    impact = 18.0 if label == "high" else 10.0 if label == "low" else 4.0
    status_weight = {
        "conflict_review": 32.0,
        "suggested_update": 26.0,
        "needs_review": 18.0,
        "low_confidence": 12.0,
        "unknown": 10.0,
        "trusted": 2.0,
        "manual_verified": 0.0,
    }.get(status, 8.0)
    unknown_bonus = 12.0 if not _known(current) and _known(suggested) else 0.0
    conflict_bonus = 14.0 if _known(current) and _known(suggested) and current != suggested else 0.0
    return clamp(status_weight + unknown_bonus + conflict_bonus + impact + reward * 0.12 + confidence_score * 0.18)


def _recommended_action(status: str) -> str:
    return {
        "manual_verified": "keep_manual_label",
        "trusted": "keep_structure",
        "suggested_update": "review_and_save_suggestion",
        "conflict_review": "manual_conflict_review",
        "needs_review": "manual_structure_review",
        "low_confidence": "collect_more_context",
        "unknown": "keep_unknown_until_context",
    }.get(status, "manual_structure_review")


def _reason_text(current: str, suggested: str, status: str, evidence: list[str]) -> str:
    evidence_text = "、".join(evidence[:4]) if evidence else "无稳定结构证据"
    if status == "trusted":
        return f"当前结构与评估器一致，证据：{evidence_text}"
    if status == "suggested_update":
        return f"当前结构缺失，评估器建议 {semantic_label_display('slice_structure', suggested)}，证据：{evidence_text}"
    if status == "conflict_review":
        return (
            f"当前结构 {semantic_label_display('slice_structure', current)} 与评估器建议 "
            f"{semantic_label_display('slice_structure', suggested)} 不一致，证据：{evidence_text}"
        )
    if status == "manual_verified":
        return "人工确认标签优先，评估器仅作为旁路参考。"
    return f"结构证据不足或冲突较弱，证据：{evidence_text}"


def _coverage_summary(evaluations: list[dict]) -> dict[str, Any]:
    total = len(evaluations)
    current_known = sum(1 for row in evaluations if _known(row.get("current_structure")))
    suggested_known = sum(1 for row in evaluations if _known(row.get("suggested_structure")))
    trusted = sum(1 for row in evaluations if row.get("status") in {"trusted", "manual_verified"})
    conflicts = sum(1 for row in evaluations if row.get("status") == "conflict_review")
    high_conf = sum(1 for row in evaluations if float(row.get("confidence_score") or 0.0) >= 52.0)
    agreement_base = [row for row in evaluations if _known(row.get("current_structure")) and _known(row.get("suggested_structure"))]
    agreement = sum(1 for row in agreement_base if row.get("agreement"))
    return {
        "total": total,
        "current_known_count": current_known,
        "current_known_rate": round(current_known / max(1, total), 4),
        "evaluator_known_count": suggested_known,
        "evaluator_known_rate": round(suggested_known / max(1, total), 4),
        "trusted_count": trusted,
        "trusted_rate": round(trusted / max(1, total), 4),
        "high_confidence_count": high_conf,
        "high_confidence_rate": round(high_conf / max(1, total), 4),
        "agreement_rate": round(agreement / max(1, len(agreement_base)), 4),
        "conflict_count": conflicts,
        "conflict_rate": round(conflicts / max(1, total), 4),
    }


def _structure_distribution(evaluations: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in evaluations:
        grouped[str(row.get("suggested_structure") or "unknown")].append(row)
    result = []
    for structure, rows in grouped.items():
        rewards = [float(row.get("normalized_reward") or 0.0) for row in rows]
        result.append(
            {
                "slice_structure": structure,
                "label": semantic_label_display("slice_structure", structure),
                "count": len(rows),
                "avg_reward": round(sum(rewards) / max(1, len(rewards)), 4),
                "high_count": sum(1 for row in rows if str(row.get("performance_label") or "").lower() == "high"),
                "trusted_count": sum(1 for row in rows if row.get("status") in {"trusted", "manual_verified"}),
                "avg_confidence": round(
                    sum(float(row.get("confidence_score") or 0.0) for row in rows) / max(1, len(rows)),
                    4,
                ),
            }
        )
    result.sort(key=lambda row: (int(row["count"]), float(row["avg_reward"])), reverse=True)
    return result


def _issue_summary(evaluations: list[dict]) -> list[dict]:
    counts = Counter(str(row.get("status") or "unknown") for row in evaluations)
    total = len(evaluations)
    labels = {
        "conflict_review": "当前结构与评估器高置信建议冲突",
        "suggested_update": "当前 unknown 但评估器可给出建议",
        "low_confidence": "结构证据弱，需要更多上下文",
        "unknown": "评估器也无法判断结构",
        "trusted": "当前结构可信",
        "manual_verified": "人工确认结构",
        "needs_review": "中置信结构建议需人工复核",
    }
    return [
        {
            "type": key,
            "label": labels.get(key, key),
            "count": int(value),
            "rate": round(value / max(1, total), 4),
        }
        for key, value in counts.most_common()
    ]


def _review_queue(evaluations: list[dict], *, limit: int = 30) -> list[dict]:
    candidates = [
        row
        for row in evaluations
        if row.get("status") in {"conflict_review", "suggested_update", "needs_review", "low_confidence", "unknown"}
    ]
    candidates.sort(key=lambda row: float(row.get("priority_score") or 0.0), reverse=True)
    return candidates[:limit]


def _recommendations(evaluations: list[dict], issues: list[dict]) -> list[str]:
    coverage = _coverage_summary(evaluations)
    recs = []
    if float(coverage.get("conflict_rate") or 0.0) > 0.08:
        recs.append("优先人工复核 conflict_review 样本，避免把结构字段重新放入排序强权重。")
    if float(coverage.get("evaluator_known_rate") or 0.0) < 0.5:
        recs.append("结构评估器可判定覆盖不足，需补充标题/字幕/OCR 上下文后再做排序实验。")
    if float(coverage.get("agreement_rate") or 0.0) >= 0.75 and float(coverage.get("high_confidence_rate") or 0.0) >= 0.35:
        recs.append("结构标签已接近可实验状态，可在下一轮回测中单独测试 slice_structure gated weight。")
    if not recs:
        recs.append("保持 slice_structure 为诊断/校准字段，先积累人工确认样本和冲突样本。")
    if any(item.get("type") == "suggested_update" and int(item.get("count") or 0) > 0 for item in issues):
        recs.append("将 suggested_update 队列并入语义校准工作台，用户保存后才标记 manual_verified。")
    return recs


def _fetch_rows(account_id: str | None, dataset_id: str | None, limit: int) -> list[dict]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    account = str(account_id or "").strip()
    dataset = str(dataset_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if dataset and dataset.lower() != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset)
    query = f"""
        SELECT id, account_id, dataset_id, platform_item_id, title, tags, reward_proxy,
               normalized_reward, performance_label, content_category, hook_type,
               slice_structure, structure_confidence, structure_evidence,
               structure_unknown_reason, program_name, artist_names, song_title,
               original_sound_owner, entity_signal, classification_confidence,
               raw_json, updated_at
        FROM historical_capture_samples
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(normalized_reward, reward_proxy, 0) DESC, updated_at DESC
    """
    if int(limit or 0) > 0:
        query += " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        return fetch_all(conn, query, params)


def _row_text(row: dict) -> str:
    raw = _json_field(row.get("raw_json"), {})
    classification = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    return " ".join(
        str(value or "")
        for value in [
            row.get("title"),
            row.get("tags"),
            row.get("program_name"),
            row.get("content_category"),
            row.get("hook_type"),
            row.get("artist_names"),
            row.get("song_title"),
            row.get("original_sound_owner"),
            row.get("entity_signal"),
            row.get("structure_evidence"),
            classification.get("structure_evidence"),
        ]
    )


def _json_field(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def _known(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and text not in {"unknown", "none", "null", "其他", "其它", "0"})


def _confidence_label(score: float) -> str:
    if score >= 52:
        return "high"
    if score >= 30:
        return "medium"
    if score > 0:
        return "low"
    return "unknown"


def _normalize_dataset_id(dataset_id: str | None) -> str:
    return str(dataset_id or "").strip() or "default"
