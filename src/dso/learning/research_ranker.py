from __future__ import annotations

from collections import defaultdict
import json
import re
from typing import Any

from dso.db.session import connect, fetch_all
from dso.utils import clamp
from dso.versions import PROTOTYPE_BANK_VERSION, RESEARCH_RANKER_VERSION


def research_learning_signals(segment: dict) -> dict:
    account_id = str(segment.get("account_id") or "").strip()
    samples, scope, fallback_reason = _research_samples(account_id)
    if not samples:
        return _empty_signals(scope=scope, fallback_reason="no_research_samples")

    target_text = _research_text(segment)
    target_tokens = _research_tokens(target_text)
    matches = []
    for sample in samples:
        similarity = _research_similarity(target_tokens, _sample_research_text(sample), segment, sample)
        if similarity <= 0:
            continue
        reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0.0)
        label = str(sample.get("performance_label") or "").strip().lower() or _label_from_reward(reward)
        matches.append(
            {
                "historical_sample_id": sample.get("id") or "",
                "platform_item_id": sample.get("platform_item_id") or "",
                "account_id": sample.get("account_id") or "",
                "title": sample.get("title") or "",
                "similarity": round(similarity, 4),
                "reward_proxy": round(float(sample.get("reward_proxy") or 0), 4),
                "normalized_reward": round(reward, 4),
                "performance_label": label,
                "match_type": label if label in {"high", "low"} else "neutral",
                "content_category": sample.get("content_category") or "",
                "hook_type": sample.get("hook_type") or "",
                "slice_structure": sample.get("slice_structure") or "",
                "program_name": sample.get("program_name") or "",
                "artist_names": sample.get("artist_names") or "",
                "song_title": sample.get("song_title") or "",
                "tags": sample.get("tags") or "",
                "classification_confidence": sample.get("classification_confidence") or "",
                "research_label_version": sample.get("research_label_version") or "",
            }
        )
    matches.sort(key=lambda row: (row["similarity"], row["normalized_reward"]), reverse=True)
    selected = matches[:8]
    high = [row for row in matches if row["match_type"] == "high"][:8]
    low = [row for row in matches if row["match_type"] == "low"][:8]
    similar_high = max((row["similarity"] * row["normalized_reward"] for row in high), default=0.0)
    similar_low = max((row["similarity"] * (100.0 - row["normalized_reward"]) for row in low), default=0.0)
    best_similarity = max((row["similarity"] for row in selected), default=0.0)
    account_position = _account_baseline_position(samples, selected, scope)
    prototype_hits = _prototype_hits(account_id, target_tokens, high[:3])
    low_risks = _low_interaction_risks(low[:6])
    confidence = _research_confidence(len(samples), selected, scope, prototype_hits)
    semantic_confidence = _semantic_confidence(selected)
    component_scores = _component_scores(
        similar_high=similar_high,
        similar_low=similar_low,
        best_similarity=best_similarity,
        account_position=account_position,
        prototype_hits=prototype_hits,
        low_risks=low_risks,
        semantic_confidence=semantic_confidence,
    )
    history_score = _history_score(component_scores)
    status = "ready" if len(samples) >= 50 and selected else "low_confidence"
    if not selected:
        status = "insufficient_match"
    reason = _ranker_reason(component_scores, high, low, prototype_hits)
    advice = _ranker_advice(component_scores, confidence, status)
    baseline_score = float(component_scores.get("account_baseline_position") or 50.0)
    signals = {
        "research_ranker_version": RESEARCH_RANKER_VERSION,
        "history_match_score": round(history_score, 2),
        "semantic_baseline_score": round(baseline_score, 4),
        "ranker_baseline_delta": round(history_score - baseline_score, 4),
        "history_source": "published_research_samples",
        "status": status,
        "match_scope": scope,
        "fallback_reason": fallback_reason,
        "sample_count": len(samples),
        "matched_count": len(selected),
        "similar_high_perf_score": round(similar_high, 4),
        "similar_low_perf_risk": round(similar_low, 4),
        "similar_high_samples": high[:3],
        "similar_low_samples": low[:3],
        "matched_high_samples": high[:3],
        "matched_low_samples": low[:3],
        "account_baseline_position": account_position,
        "prototype_hits": prototype_hits,
        "prototype_summary": _prototype_summary(prototype_hits),
        "low_interaction_risk_library": low_risks,
        "risk_summary": _risk_summary(low_risks),
        "component_scores": component_scores,
        "evidence_quality": {
            "score": round(confidence, 4),
            "label": _confidence_label(confidence),
            "status": status,
            "scope": scope,
            "sample_count": len(samples),
            "matched_count": len(selected),
            "semantic_confidence": round(semantic_confidence, 4),
        },
        "ranker_reason": reason,
        "ranker_advice": advice,
        "semantic_gap_reason": _semantic_gap_reason(component_scores, history_score),
        "confidence": round(confidence, 4),
        "confidence_label": _confidence_label(confidence),
        "evidence_label": "历史研究先验",
        "metric_basis": "visible engagement proxy; not publication feedback for this candidate",
        "matches": selected,
    }
    return signals


def _empty_signals(*, scope: str, fallback_reason: str) -> dict:
    return {
        "research_ranker_version": RESEARCH_RANKER_VERSION,
        "history_match_score": 50.0,
        "history_source": "published_research_samples",
        "status": "insufficient_history",
        "match_scope": scope,
        "fallback_reason": fallback_reason,
        "sample_count": 0,
        "matched_count": 0,
        "similar_high_perf_score": 0.0,
        "similar_low_perf_risk": 0.0,
        "similar_high_samples": [],
        "similar_low_samples": [],
        "matched_high_samples": [],
        "matched_low_samples": [],
        "account_baseline_position": _empty_account_baseline_position(0, scope),
        "semantic_baseline_score": 50.0,
        "ranker_baseline_delta": 0.0,
        "prototype_hits": [],
        "prototype_summary": "无可用历史研究原型",
        "low_interaction_risk_library": [],
        "risk_summary": "无可用低互动风险样本",
        "component_scores": _zero_component_scores(),
        "evidence_quality": {
            "score": 0.0,
            "label": "low",
            "status": "insufficient_history",
            "scope": scope,
            "sample_count": 0,
            "matched_count": 0,
            "semantic_confidence": 0.0,
        },
        "ranker_reason": "历史研究样本不足，仅保留规则分。",
        "ranker_advice": {
            "action": "low_evidence_hold",
            "label": "证据不足",
            "reason": "历史研究样本不足，仅作为低置信参考。",
        },
        "semantic_gap_reason": {
            "baseline_score": 50.0,
            "ranker_score": 50.0,
            "delta": 0.0,
            "reason": "历史研究样本不足，无法形成相对语义基线差异。",
        },
        "confidence": 0.0,
        "confidence_label": "low",
        "evidence_label": "历史研究先验不足",
        "metric_basis": "visible engagement proxy; not publication feedback for this candidate",
        "matches": [],
    }


def _research_samples(account_id: str) -> tuple[list[dict], str, str]:
    account = account_id.strip()
    with connect() as conn:
        if account and account.lower() not in {"all", "main"}:
            rows = _fetch_research_samples(conn, account)
            if len(rows) >= 50:
                return rows, "account", ""
            if rows:
                fallback = _fetch_research_samples(conn, None)
                return fallback or rows, "global_fallback", "target_account_below_50_samples"
        if account == "main":
            rows = _fetch_research_samples(conn, account)
            if len(rows) >= 50:
                return rows, "account", ""
        rows = _fetch_research_samples(conn, None)
    if rows:
        return rows, "global", "target_account_missing_or_below_threshold"
    return [], "none", "no_published_research_samples"


def _fetch_research_samples(conn, account_id: str | None) -> list[dict]:
    clauses = [
        "COALESCE(platform_item_id, '') != ''",
        "(COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0)",
    ]
    params: list[object] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    return fetch_all(
        conn,
        f"""
        SELECT id, account_id, platform_item_id, title, reward_proxy, normalized_reward,
               performance_label, content_category, hook_type, slice_structure,
               program_name, artist_names, song_title, tags, classification_confidence,
               research_label_version
        FROM historical_capture_samples
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC
        """,
        params,
    )


def _research_text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "transcript",
            "summary",
            "music_slice_type",
            "emotion_type",
            "short_video_structure",
            "musical_moment",
            "program_context",
            "comment_trigger",
            "video_title",
        ]
    )


def _sample_research_text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "title",
            "tags",
            "artist_names",
            "song_title",
            "program_name",
            "content_category",
            "hook_type",
            "slice_structure",
        ]
    )


def _research_similarity(target_tokens: set[str], sample_text: str, segment: dict, sample: dict) -> float:
    sample_tokens = _research_tokens(sample_text)
    if not target_tokens or not sample_tokens:
        token_score = 0.0
    else:
        token_score = len(target_tokens & sample_tokens) / max(1, len(target_tokens | sample_tokens))
    semantic_bonus = 0.0
    if _text_equal(segment.get("music_slice_type"), sample.get("content_category")):
        semantic_bonus += 0.12
    if _text_equal(segment.get("short_video_structure"), sample.get("slice_structure")):
        semantic_bonus += 0.08
    if str(sample.get("hook_type") or "") and str(sample.get("hook_type")) in _research_text(segment):
        semantic_bonus += 0.08
    if str(sample.get("song_title") or "") and str(sample.get("song_title")) in _research_text(segment):
        semantic_bonus += 0.05
    return min(1.0, token_score + semantic_bonus)


def _research_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", cleaned)
    tokens = set(words)
    chinese_chars = [word for word in words if len(word) == 1 and "\u4e00" <= word <= "\u9fff"]
    tokens.update("".join(chinese_chars[index : index + 2]) for index in range(max(0, len(chinese_chars) - 1)))
    tokens.update("".join(chinese_chars[index : index + 3]) for index in range(max(0, len(chinese_chars) - 2)))
    return {token for token in tokens if token}


def _component_scores(
    *,
    similar_high: float,
    similar_low: float,
    best_similarity: float,
    account_position: dict,
    prototype_hits: list[dict],
    low_risks: list[dict],
    semantic_confidence: float,
) -> dict:
    prototype_score = max((float(row.get("fit_score") or 0) for row in prototype_hits), default=0.0)
    low_risk_score = max((float(row.get("risk_score") or 0) for row in low_risks), default=similar_low)
    baseline_percentile = account_position.get("percentile")
    baseline_score = 50.0 if baseline_percentile is None else float(baseline_percentile) * 100.0
    novelty = clamp((1.0 - min(1.0, best_similarity)) * 42.0 + semantic_confidence * 20.0)
    return {
        "high_similarity": round(clamp(similar_high), 4),
        "low_interaction_risk": round(clamp(low_risk_score), 4),
        "account_baseline_position": round(clamp(baseline_score), 4),
        "prototype_fit": round(clamp(prototype_score), 4),
        "semantic_label_trust": round(clamp(semantic_confidence * 100.0), 4),
        "long_tail_novelty": round(clamp(novelty), 4),
        "best_similarity": round(best_similarity, 4),
    }


def _zero_component_scores() -> dict:
    return {
        "high_similarity": 0.0,
        "low_interaction_risk": 0.0,
        "account_baseline_position": 50.0,
        "prototype_fit": 0.0,
        "semantic_label_trust": 0.0,
        "long_tail_novelty": 0.0,
        "best_similarity": 0.0,
    }


def _history_score(component_scores: dict) -> float:
    return clamp(
        50.0
        + float(component_scores.get("high_similarity") or 0) * 0.34
        - float(component_scores.get("low_interaction_risk") or 0) * 0.16
        + (float(component_scores.get("account_baseline_position") or 50) - 50.0) * 0.06
        + float(component_scores.get("prototype_fit") or 0) * 0.055
        + (float(component_scores.get("semantic_label_trust") or 0) - 50.0) * 0.025
        + max(0.0, float(component_scores.get("long_tail_novelty") or 0) - 35.0) * 0.035
    )


def _prototype_hits(account_id: str, target_tokens: set[str], high: list[dict]) -> list[dict]:
    hits = _stored_prototype_hits(account_id, target_tokens)
    hits.extend(_derived_prototype_hits(high))
    deduped: dict[str, dict] = {}
    for hit in hits:
        key = str(hit.get("prototype_key") or hit.get("prototype_name") or "")
        if not key:
            continue
        previous = deduped.get(key)
        if previous is None or float(hit.get("fit_score") or 0) > float(previous.get("fit_score") or 0):
            deduped[key] = hit
    ranked = list(deduped.values())
    ranked.sort(key=lambda row: (float(row.get("fit_score") or 0), float(row.get("confidence") or 0)), reverse=True)
    return ranked[:3]


def _stored_prototype_hits(account_id: str, target_tokens: set[str]) -> list[dict]:
    if not target_tokens:
        return []
    accounts = []
    for value in [account_id, "main", "all"]:
        normalized = str(value or "").strip()
        if normalized and normalized not in accounts:
            accounts.append(normalized)
    if not accounts:
        accounts = ["main", "all"]
    placeholders = ", ".join("?" for _ in accounts)
    params: list[object] = [*accounts, PROTOTYPE_BANK_VERSION]
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT account_id, dataset_id, dataset_name, prototype_key, prototype_name, source,
                   sample_count, avg_score, confidence, keywords_json, examples_json,
                   parameters_json, version, updated_at
            FROM prototype_bank_items
            WHERE account_id IN ({placeholders}) AND version = ?
            ORDER BY updated_at DESC
            LIMIT 120
            """,
            params,
        )
    hits = []
    for row in rows:
        keywords = _json_field(row.get("keywords_json"), [])
        examples = _json_field(row.get("examples_json"), [])
        parameters = _json_field(row.get("parameters_json"), {})
        prototype_text = " ".join(
            [
                str(row.get("prototype_name") or ""),
                " ".join(str(item) for item in keywords if item),
                json.dumps(parameters, ensure_ascii=False) if parameters else "",
            ]
        )
        similarity = _prototype_text_similarity(target_tokens, prototype_text)
        if similarity <= 0:
            continue
        prototype_score = float(row.get("avg_score") or 0.0)
        confidence = float(row.get("confidence") or 0.0)
        fit_score = similarity * max(45.0, prototype_score) * (0.65 + 0.35 * confidence)
        hits.append(
            {
                "prototype_key": row.get("prototype_key") or "",
                "prototype_name": row.get("prototype_name") or "",
                "source": f"prototype_bank:{row.get('source') or 'external'}",
                "account_id": row.get("account_id") or "",
                "dataset_id": row.get("dataset_id") or "default",
                "similarity": round(similarity, 4),
                "prototype_score": round(prototype_score, 4),
                "fit_score": round(fit_score, 4),
                "confidence": round(confidence, 4),
                "sample_count": int(row.get("sample_count") or 0),
                "keywords": keywords[:8] if isinstance(keywords, list) else [],
                "examples": _prototype_payload_examples(examples),
                "parameters": parameters if isinstance(parameters, dict) else {},
            }
        )
    return hits


def _derived_prototype_hits(high: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in high:
        groups[_semantic_group_key(row)].append(row)
    hits = []
    for key, rows in groups.items():
        avg_similarity = sum(float(row.get("similarity") or 0) for row in rows) / len(rows)
        avg_reward = sum(_sample_reward(row) for row in rows) / len(rows)
        fit_score = avg_similarity * avg_reward * (0.75 + min(0.25, len(rows) * 0.06))
        confidence = clamp(avg_similarity * 0.55 + (avg_reward / 100.0) * 0.25 + min(1.0, len(rows) / 3.0) * 0.2, 0.0, 1.0)
        first = rows[0]
        hits.append(
            {
                "prototype_key": f"research_high:{key}",
                "prototype_name": _derived_prototype_name(first),
                "source": "matched_high_samples",
                "account_id": first.get("account_id") or "",
                "dataset_id": "",
                "similarity": round(avg_similarity, 4),
                "prototype_score": round(avg_reward, 4),
                "fit_score": round(fit_score, 4),
                "confidence": round(confidence, 4),
                "sample_count": len(rows),
                "keywords": _keywords_from_rows(rows),
                "examples": _sample_examples(rows),
                "parameters": {
                    "content_category": first.get("content_category") or "",
                    "hook_type": first.get("hook_type") or "",
                    "slice_structure": first.get("slice_structure") or "",
                    "basis": "high_interaction_research_matches",
                },
            }
        )
    return hits


def _low_interaction_risks(low: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in low:
        groups[_semantic_group_key(row)].append(row)
    risks = []
    for key, rows in groups.items():
        avg_similarity = sum(float(row.get("similarity") or 0) for row in rows) / len(rows)
        avg_reward = sum(_sample_reward(row) for row in rows) / len(rows)
        risk_score = avg_similarity * (100.0 - avg_reward) * (0.8 + min(0.2, len(rows) * 0.05))
        confidence = clamp(avg_similarity * 0.6 + (1.0 - avg_reward / 100.0) * 0.25 + min(1.0, len(rows) / 3.0) * 0.15, 0.0, 1.0)
        first = rows[0]
        risks.append(
            {
                "risk_key": f"research_low:{key}",
                "risk_name": _derived_risk_name(first),
                "source": "matched_low_samples",
                "risk_score": round(risk_score, 4),
                "similarity": round(avg_similarity, 4),
                "confidence": round(confidence, 4),
                "sample_count": len(rows),
                "avg_reward_proxy": round(avg_reward, 4),
                "content_category": first.get("content_category") or "",
                "hook_type": first.get("hook_type") or "",
                "keywords": _keywords_from_rows(rows),
                "examples": _sample_examples(rows),
            }
        )
    risks.sort(key=lambda row: (float(row.get("risk_score") or 0), float(row.get("confidence") or 0)), reverse=True)
    return risks[:3]


def _semantic_group_key(row: dict) -> str:
    parts = [_compact_key_part(row.get("content_category")), _compact_key_part(row.get("hook_type")), _compact_key_part(row.get("slice_structure"))]
    key = "|".join(part for part in parts if part)
    if key:
        return key
    title_tokens = sorted(_research_tokens(row.get("title") or ""))[:4]
    return "title:" + "_".join(title_tokens)


def _compact_key_part(value: object) -> str:
    text = re.sub(r"\s+", "_", str(value or "").strip().lower())
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", text).strip("_")
    return text[:48]


def _derived_prototype_name(row: dict) -> str:
    category = str(row.get("content_category") or "").strip()
    hook = str(row.get("hook_type") or "").strip()
    if category and hook:
        return f"{category} / {hook} 高互动原型"
    if category:
        return f"{category} 高互动原型"
    if hook:
        return f"{hook} 高互动原型"
    return "相似高互动样本原型"


def _derived_risk_name(row: dict) -> str:
    category = str(row.get("content_category") or "").strip()
    hook = str(row.get("hook_type") or "").strip()
    if category and hook:
        return f"{category} / {hook} 低互动风险"
    if category:
        return f"{category} 低互动风险"
    if hook:
        return f"{hook} 低互动风险"
    return "相似低互动样本风险"


def _account_baseline_position(samples: list[dict], selected: list[dict], scope: str) -> dict:
    rewards = sorted(_sample_reward(row) for row in samples if _sample_reward(row) > 0)
    if not rewards:
        return _empty_account_baseline_position(len(samples), scope)
    if selected:
        weighted = [(_sample_reward(row), max(0.05, float(row.get("similarity") or 0.0))) for row in selected if _sample_reward(row) > 0]
        predicted = sum(value * weight for value, weight in weighted) / sum(weight for _, weight in weighted) if weighted else sum(rewards) / len(rewards)
    else:
        predicted = sum(rewards) / len(rewards)
    percentile = sum(1 for value in rewards if value <= predicted) / len(rewards)
    label = _baseline_position_label(percentile)
    return {
        "status": "ready" if selected else "insufficient_match",
        "scope": scope,
        "basis": "normalized_reward_percentile_within_research_samples",
        "sample_count": len(samples),
        "predicted_reward_proxy": round(predicted, 4),
        "percentile": round(percentile, 4),
        "position_label": label,
        "position_text": _baseline_position_text(label),
        "mean_reward": round(sum(rewards) / len(rewards), 4),
        "p50_reward": round(_quantile(rewards, 0.5), 4),
        "p75_reward": round(_quantile(rewards, 0.75), 4),
        "p90_reward": round(_quantile(rewards, 0.9), 4),
    }


def _empty_account_baseline_position(sample_count: int, scope: str) -> dict:
    return {
        "status": "insufficient_history" if not sample_count else "insufficient_match",
        "scope": scope,
        "basis": "normalized_reward_percentile_within_research_samples",
        "sample_count": sample_count,
        "predicted_reward_proxy": 0.0,
        "percentile": None,
        "position_label": "unknown",
        "position_text": "历史研究样本不足",
        "mean_reward": 0.0,
        "p50_reward": 0.0,
        "p75_reward": 0.0,
        "p90_reward": 0.0,
    }


def _baseline_position_label(percentile: float) -> str:
    if percentile >= 0.9:
        return "top_decile"
    if percentile >= 0.75:
        return "top_quartile"
    if percentile >= 0.5:
        return "above_median"
    if percentile >= 0.25:
        return "below_median"
    return "low_quartile"


def _baseline_position_text(label: str) -> str:
    return {
        "top_decile": "研究样本前 10%",
        "top_quartile": "研究样本前 25%",
        "above_median": "高于账号中位",
        "below_median": "低于账号中位",
        "low_quartile": "低位风险区",
    }.get(label, "历史研究样本不足")


def _research_confidence(sample_count: int, selected: list[dict], scope: str, prototype_hits: list[dict]) -> float:
    best_similarity = max((float(row.get("similarity") or 0) for row in selected), default=0.0)
    coverage = clamp(sample_count / 50.0, 0.0, 1.0)
    match_density = clamp(len(selected) / 5.0, 0.0, 1.0)
    scope_factor = {"account": 1.0, "global_fallback": 0.72, "global": 0.62, "none": 0.0}.get(scope, 0.35)
    prototype_factor = max((float(row.get("confidence") or 0) for row in prototype_hits), default=0.0)
    return clamp(
        coverage * 0.25 + best_similarity * 0.35 + match_density * 0.15 + scope_factor * 0.15 + prototype_factor * 0.10,
        0.0,
        1.0,
    )


def _semantic_confidence(selected: list[dict]) -> float:
    if not selected:
        return 0.0
    values = []
    for row in selected:
        values.append({"manual_verified": 1.0, "high": 0.85, "medium": 0.62, "low": 0.36}.get(str(row.get("classification_confidence") or "").lower(), 0.45))
    return sum(values) / len(values)


def _confidence_label(value: float) -> str:
    if value >= 0.7:
        return "high"
    if value >= 0.4:
        return "medium"
    return "low"


def _ranker_reason(component_scores: dict, high: list[dict], low: list[dict], prototype_hits: list[dict]) -> str:
    parts = []
    if high:
        parts.append(f"命中 {len(high)} 条高互动相似样本")
    if low:
        parts.append(f"命中 {len(low)} 条低互动风险样本")
    if prototype_hits:
        parts.append("命中高互动原型")
    if not parts:
        parts.append("历史相似证据不足")
    baseline = float(component_scores.get("account_baseline_position") or 50.0)
    high_score = float(component_scores.get("high_similarity") or 0.0)
    risk = float(component_scores.get("low_interaction_risk") or 0.0)
    if high_score >= risk and high_score >= 35:
        parts.append("相对语义基线：高互动相似证据提供上调理由")
    elif risk >= 35:
        parts.append("相对语义基线：低互动风险证据提供下调理由")
    else:
        parts.append(f"相对语义基线：证据较弱，主要跟随账号/语义基线 {round(baseline, 1)}")
    parts.append(f"组件分 high={component_scores.get('high_similarity')}, risk={component_scores.get('low_interaction_risk')}")
    return "；".join(parts)


def _semantic_gap_reason(component_scores: dict, history_score: float) -> dict:
    baseline = float(component_scores.get("account_baseline_position") or 50.0)
    delta = float(history_score) - baseline
    if abs(delta) < 1.5:
        reason = "证据强度不足或互相抵消，v2.2 基本回退到语义基线。"
    elif delta > 0:
        reason = "高互动相似、原型命中或语义可信度强于低互动风险，因此相对语义基线上调。"
    else:
        reason = "低互动风险或证据质量不足，因此相对语义基线下调。"
    return {
        "baseline_score": round(baseline, 4),
        "ranker_score": round(float(history_score), 4),
        "delta": round(delta, 4),
        "reason": reason,
    }


def _ranker_advice(component_scores: dict, confidence: float, status: str) -> dict:
    high = float(component_scores.get("high_similarity") or 0.0)
    risk = float(component_scores.get("low_interaction_risk") or 0.0)
    baseline = float(component_scores.get("account_baseline_position") or 50.0)
    trust = float(component_scores.get("semantic_label_trust") or 0.0)
    if status in {"insufficient_history", "insufficient_match"} or confidence < 0.28:
        return {
            "action": "low_evidence_hold",
            "label": "证据不足",
            "reason": "历史相似证据不足，建议保留规则分和人工判断。",
        }
    if risk >= 45 and risk >= high * 0.65:
        return {
            "action": "low_interaction_risk_review",
            "label": "低互动风险复核",
            "reason": "命中低互动风险样本，需要人工确认上下文和包装是否充分差异化。",
        }
    if baseline >= 62 and high >= 45 and trust >= 55:
        return {
            "action": "recommend_export_preview",
            "label": "建议导出预览",
            "reason": "账号基线、高互动相似和语义可信度同时支撑，可进入导出预览复核。",
        }
    return {
        "action": "needs_context_review",
        "label": "上下文复核",
        "reason": "历史证据有一定支撑，但需要确认节目上下文、标题封面和低互动风险。",
    }


def _prototype_summary(prototype_hits: list[dict]) -> str:
    if not prototype_hits:
        return "未命中高互动原型"
    best = prototype_hits[0]
    return (
        f"{best.get('prototype_name') or '高互动原型'} / "
        f"fit {float(best.get('fit_score') or 0):.1f} / "
        f"confidence {float(best.get('confidence') or 0):.2f}"
    )


def _risk_summary(risks: list[dict]) -> str:
    if not risks:
        return "未命中低互动风险库"
    best = risks[0]
    return (
        f"{best.get('risk_name') or '低互动风险'} / "
        f"risk {float(best.get('risk_score') or 0):.1f} / "
        f"confidence {float(best.get('confidence') or 0):.2f}"
    )


def _keywords_from_rows(rows: list[dict]) -> list[str]:
    keywords: list[str] = []
    for row in rows:
        for field in ["content_category", "hook_type", "artist_names", "song_title"]:
            value = str(row.get(field) or "").strip()
            if value:
                keywords.append(value)
        tags = re.split(r"[|,，、\s]+", str(row.get("tags") or ""))
        keywords.extend(tag.strip() for tag in tags if tag.strip())
    seen = set()
    unique = []
    for keyword in keywords:
        if keyword not in seen:
            seen.add(keyword)
            unique.append(keyword)
    return unique[:8]


def _sample_examples(rows: list[dict]) -> list[dict]:
    examples = []
    for row in rows[:3]:
        examples.append(
            {
                "historical_sample_id": row.get("historical_sample_id") or "",
                "platform_item_id": row.get("platform_item_id") or "",
                "title": row.get("title") or "",
                "normalized_reward": row.get("normalized_reward") or 0,
                "similarity": row.get("similarity") or 0,
                "performance_label": row.get("performance_label") or "",
            }
        )
    return examples


def _prototype_payload_examples(examples: object) -> list[dict]:
    if not isinstance(examples, list):
        return []
    normalized = []
    for item in examples[:3]:
        if isinstance(item, dict):
            normalized.append(
                {
                    "title": item.get("title") or item.get("platform_title") or item.get("name") or "",
                    "platform_item_id": item.get("platform_item_id") or item.get("sample_id") or "",
                    "score": item.get("score") or item.get("reward_proxy") or item.get("normalized_reward") or 0,
                }
            )
        else:
            normalized.append({"title": str(item), "platform_item_id": "", "score": 0})
    return normalized


def _prototype_text_similarity(target_tokens: set[str], text: str) -> float:
    prototype_tokens = _research_tokens(text)
    if not target_tokens or not prototype_tokens:
        return 0.0
    overlap = len(target_tokens & prototype_tokens)
    return min(1.0, overlap / max(1, min(len(target_tokens), len(prototype_tokens))))


def _json_field(value: object, default: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def _sample_reward(row: dict) -> float:
    return float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0)


def _label_from_reward(value: float) -> str:
    if value >= 70:
        return "high"
    if value <= 30:
        return "low"
    return "mid"


def _text_equal(left: object, right: object) -> bool:
    return bool(left and right and str(left).strip().lower() == str(right).strip().lower())


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    position = (len(values) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return float(values[lower]) * (1 - fraction) + float(values[upper]) * fraction
