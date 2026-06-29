from __future__ import annotations

from collections import defaultdict
import json
import re
from pathlib import Path

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.research_ranker import research_learning_signals
from dso.scoring.rights import rights_risk_for_segment
from dso.utils import clamp, new_id, utc_now
from dso.versions import PROTOTYPE_BANK_VERSION, RESEARCH_RANKER_VERSION, SCORER_VERSION


HOOK_CUES = ["没想到", "第一次", "最后", "直接", "竟然", "为什么", "导师", "评委", "淘汰", "晋级"]
SUSPENSE_CUES = ["淘汰", "晋级", "待定", "排名", "票数", "结果", "选择", "PK", "pk", "赛制", "悬念", "争议"]
STORY_CUES = ["故事", "经历", "一路", "小时候", "家人", "妈妈", "父亲", "写给", "压力", "梦想", "坚持", "原创"]
MUSIC_CUES = ["副歌", "高音", "转调", "升key", "和声", "rap", "RAP", "爆发", "高潮", "长音", "哭腔", "solo", "drop"]
REACTION_CUES = ["全场", "观众", "现场", "掌声", "欢呼", "尖叫", "起立", "站起来", "泪目", "哭了", "反应"]
EMOTION_CUES = ["遗憾", "错过", "梦想", "突破", "温柔", "眼泪", "坚持", "释怀", "哽咽"]
FIRST_STAGE_CUES = ["第一次", "首次", "首個", "首个", "緊張", "紧张", "壓力", "压力"]


def score_video(video_id: str) -> list[dict]:
    with connect() as conn:
        segments = fetch_all(conn, "SELECT * FROM candidate_segments WHERE source_video_id = ?", [video_id])
    return [score_segment(segment["id"]) for segment in segments]


def score_segment(segment_id: str) -> dict:
    with connect() as conn:
        segment = fetch_one(
            conn,
            """
            SELECT c.*, v.account_id, v.title AS video_title
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            WHERE c.id = ?
            """,
            [segment_id],
        )
    if not segment:
        raise KeyError(f"segment not found: {segment_id}")

    rights_risk, risk_notes, export_allowed = rights_risk_for_segment(segment)
    scores = _score_parts(segment)
    learning_signals = research_learning_signals(segment)
    scores["history_match_score"] = float(learning_signals.get("history_match_score") or 50.0)
    scores["rights_risk_score"] = rights_risk
    scores["low_originality_score"] = _low_originality(segment)
    final = _final_score(scores)
    ranker_score = round(clamp(final + (scores["history_match_score"] - 50.0) * 0.08), 2)
    title_suggestions = sanitize_title_suggestions(_title_suggestions(segment))
    cover_suggestion = _cover_suggestion(segment)
    explanation = _explanation(segment, scores, final, export_allowed, risk_notes)
    row = {
        "id": new_id("score"),
        "candidate_segment_id": segment_id,
        **scores,
        "final_score": final,
        "ranker_score": ranker_score,
        "ranker_version": RESEARCH_RANKER_VERSION,
        "learning_signals_json": json.dumps(learning_signals, ensure_ascii=False),
        "score_explanation": explanation,
        "title_suggestions": json.dumps(title_suggestions, ensure_ascii=False),
        "cover_suggestion": cover_suggestion,
        "risk_notes": json.dumps(risk_notes, ensure_ascii=False),
        "created_at": utc_now(),
    }
    with connect() as conn:
        conn.execute("DELETE FROM slice_scores WHERE candidate_segment_id = ?", [segment_id])
        insert_row(conn, "slice_scores", row)
        conn.commit()
    row["scorer_version"] = SCORER_VERSION
    row["ranker_version"] = RESEARCH_RANKER_VERSION
    row["learning_signals"] = learning_signals
    return row


def suggestions(video_id: str, top_k: int = 10) -> list[dict]:
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT c.*, s.final_score, s.score_explanation, s.title_suggestions,
                   s.cover_suggestion, s.risk_notes, s.rights_risk_score,
                   s.low_originality_score, s.ranker_score, s.ranker_version,
                   s.learning_signals_json
            FROM candidate_segments c
            JOIN slice_scores s ON s.candidate_segment_id = c.id
            WHERE c.source_video_id = ?
            ORDER BY COALESCE(NULLIF(s.ranker_score, 0), s.final_score) DESC, s.final_score DESC
            LIMIT ?
            """,
            [video_id, top_k],
        )
    for row in rows:
        row["title_suggestions"] = sanitize_title_suggestions(json.loads(row["title_suggestions"]))
        row["risk_notes"] = json.loads(row["risk_notes"])
        row["learning_signals"] = _json_field(row.pop("learning_signals_json", "{}"), {})
        row["scorer_version"] = SCORER_VERSION
    return rows


def _score_parts(segment: dict) -> dict[str, float]:
    transcript = segment.get("transcript") or ""
    text = " ".join(
        str(segment.get(key) or "")
        for key in [
            "transcript",
            "summary",
            "short_video_structure",
            "musical_moment",
            "program_context",
            "comment_trigger",
            "music_slice_type",
            "emotion_type",
        ]
    )
    structure = segment.get("short_video_structure") or ""
    moment = segment.get("musical_moment") or ""
    context = segment.get("program_context") or ""
    trigger = segment.get("comment_trigger") or ""
    duration = float(segment["duration_seconds"])
    early_text = transcript[:36]
    has_context = _has_context(text, structure, context)
    has_climax = _has_music_climax(text, moment)
    has_reaction = _contains_any(text, REACTION_CUES) or "现场反应" in structure
    has_suspense = _contains_any(text, SUSPENSE_CUES)
    has_story = _contains_any(text, STORY_CUES)
    has_emotion = _contains_any(text, EMOTION_CUES)
    has_early_hook = _contains_any(early_text, HOOK_CUES) or _contains_any(early_text, SUSPENSE_CUES)
    pure_audio = _is_sparse_audio_only(segment)
    duration_fit = _duration_fit(duration)
    first_five_proxy = _first_five_retention_proxy(
        has_early_hook,
        has_context,
        has_climax,
        has_reaction,
        has_suspense,
        has_story,
        pure_audio,
        duration_fit,
    )
    context_completeness = _context_completeness_proxy(
        has_context, has_climax, has_reaction, has_suspense, has_story, text, context
    )
    interaction_proxy = _interaction_proxy(trigger, has_reaction, has_suspense, has_story, has_emotion, has_climax)
    negative_feedback_risk = _negative_feedback_risk(segment, text, duration)

    return {
        "short_video_hook_score": first_five_proxy,
        "musical_moment_score": clamp(46 + (30 if has_climax else 7) + (8 if has_context and has_climax else 0)),
        "narrative_context_score": context_completeness,
        "chorus_climax_score": clamp(44 + (34 if has_climax else 0) + (5 if has_reaction else 0)),
        "lyric_resonance_score": clamp(44 + (24 if has_emotion else 5) + (8 if has_story else 0) + min(10, len(text) / 28)),
        "performer_stage_score": clamp(55 + (10 if "舞台" in text or "表演" in text else 0) + (8 if has_climax else 0)),
        "audience_reaction_score": clamp(44 + (28 if has_reaction else 0) + (8 if "导师" in text or "评委" in text else 0)),
        "comment_trigger_score": interaction_proxy,
        "song_recognition_score": clamp(56 + (7 if "副歌" in text else 0) - (8 if pure_audio else 0)),
        "novelty_arrangement_score": clamp(46 + (18 if "改编" in text else 0) + (10 if "突破" in text else 0)),
        "history_match_score": 50.0,
        "production_quality_score": clamp(62 + duration_fit + (6 if has_context and has_climax else 0) - negative_feedback_risk * 0.22),
    }


def _research_learning_signals(segment: dict) -> dict:
    account_id = str(segment.get("account_id") or "").strip()
    samples, scope, fallback_reason = _research_samples(account_id)
    if not samples:
        return {
            "history_match_score": 50.0,
            "history_source": "published_research_samples",
            "status": "insufficient_history",
            "match_scope": "none",
            "fallback_reason": "no_research_samples",
            "sample_count": 0,
            "matched_count": 0,
            "similar_high_perf_score": 0.0,
            "similar_low_perf_risk": 0.0,
            "similar_high_samples": [],
            "similar_low_samples": [],
            "account_baseline_position": _empty_account_baseline_position(0, scope),
            "prototype_hits": [],
            "prototype_summary": "无可用历史研究原型",
            "low_interaction_risk_library": [],
            "risk_summary": "无可用低互动风险样本",
            "confidence": 0.0,
            "confidence_label": "low",
            "evidence_label": "历史研究先验不足",
            "matches": [],
        }
    target_text = _research_text(segment)
    target_tokens = _research_tokens(target_text)
    matches = []
    for sample in samples:
        sample_text = _sample_research_text(sample)
        similarity = _research_similarity(target_tokens, sample_text, segment, sample)
        if similarity <= 0:
            continue
        reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0)
        label = sample.get("performance_label") or _label_from_reward(reward)
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
            }
        )
    matches.sort(key=lambda row: (row["similarity"], row["normalized_reward"]), reverse=True)
    selected = matches[:8]
    high = [row for row in matches if row["match_type"] == "high"][:8]
    low = [row for row in matches if row["match_type"] == "low"][:8]
    similar_high = max((row["similarity"] * row["normalized_reward"] for row in high), default=0.0)
    similar_low = max((row["similarity"] * (100.0 - row["normalized_reward"]) for row in low), default=0.0)
    best_similarity = max((row["similarity"] for row in selected), default=0.0)
    similar_high_samples = high[:3]
    similar_low_samples = low[:3]
    account_position = _account_baseline_position(samples, selected, scope)
    prototype_hits = _research_prototype_hits(segment, high, samples, account_id, target_tokens)
    low_risks = _low_interaction_risk_library(low)
    confidence = _research_confidence(len(samples), selected, scope, prototype_hits)
    prototype_boost = max((float(row.get("fit_score") or 0) for row in prototype_hits), default=0.0) * 0.055
    risk_penalty = max((float(row.get("risk_score") or 0) for row in low_risks), default=0.0) * 0.06
    history_score = clamp(
        50.0
        + similar_high * 0.42
        - similar_low * 0.28
        + max(0.0, best_similarity - 0.25) * 12
        + prototype_boost
        - risk_penalty
    )
    status = "ready" if len(samples) >= 50 and selected else "low_confidence"
    if not selected:
        status = "insufficient_match"
    return {
        "history_match_score": round(history_score, 2),
        "history_source": "published_research_samples",
        "status": status,
        "match_scope": scope,
        "fallback_reason": fallback_reason,
        "sample_count": len(samples),
        "matched_count": len(selected),
        "similar_high_perf_score": round(similar_high, 4),
        "similar_low_perf_risk": round(similar_low, 4),
        "similar_high_samples": similar_high_samples,
        "similar_low_samples": similar_low_samples,
        "account_baseline_position": account_position,
        "prototype_hits": prototype_hits,
        "prototype_summary": _prototype_summary(prototype_hits),
        "low_interaction_risk_library": low_risks,
        "risk_summary": _risk_summary(low_risks),
        "confidence": round(confidence, 4),
        "confidence_label": _confidence_label(confidence),
        "evidence_label": "历史研究先验",
        "metric_basis": "visible engagement proxy; not publication feedback for this candidate",
        "matches": selected,
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


def _account_baseline_position(samples: list[dict], selected: list[dict], scope: str) -> dict:
    rewards = sorted(_sample_reward(row) for row in samples if _sample_reward(row) > 0)
    if not rewards:
        return _empty_account_baseline_position(len(samples), scope)
    if selected:
        weighted = [
            (_sample_reward(row), max(0.05, float(row.get("similarity") or 0.0)))
            for row in selected
            if _sample_reward(row) > 0
        ]
        if weighted:
            predicted = sum(value * weight for value, weight in weighted) / sum(weight for _, weight in weighted)
        else:
            predicted = sum(rewards) / len(rewards)
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


def _sample_reward(row: dict) -> float:
    return float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0)


def _research_prototype_hits(
    segment: dict,
    selected: list[dict],
    samples: list[dict],
    account_id: str,
    target_tokens: set[str],
) -> list[dict]:
    del samples
    hits = _stored_prototype_hits(account_id, target_tokens)
    hits.extend(_derived_prototype_hits(selected))
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
                " ".join(_example_texts(examples)),
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


def _derived_prototype_hits(selected: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in selected:
        if row.get("match_type") != "high":
            continue
        groups[_semantic_group_key(row)].append(row)
    hits = []
    for key, rows in groups.items():
        if not rows:
            continue
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
                "keywords": _prototype_keywords_from_rows(rows),
                "examples": _prototype_examples(rows),
                "parameters": {
                    "content_category": first.get("content_category") or "",
                    "hook_type": first.get("hook_type") or "",
                    "slice_structure": first.get("slice_structure") or "",
                    "basis": "high_interaction_research_matches",
                },
            }
        )
    return hits


def _low_interaction_risk_library(selected: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in selected:
        if row.get("match_type") != "low":
            continue
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
                "keywords": _prototype_keywords_from_rows(rows),
                "examples": _prototype_examples(rows),
            }
        )
    risks.sort(key=lambda row: (float(row.get("risk_score") or 0), float(row.get("confidence") or 0)), reverse=True)
    return risks[:3]


def _semantic_group_key(row: dict) -> str:
    parts = [
        _compact_key_part(row.get("content_category")),
        _compact_key_part(row.get("hook_type")),
        _compact_key_part(row.get("slice_structure")),
    ]
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


def _prototype_keywords_from_rows(rows: list[dict]) -> list[str]:
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


def _prototype_examples(rows: list[dict]) -> list[dict]:
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


def _example_texts(examples: object) -> list[str]:
    if not isinstance(examples, list):
        return []
    texts = []
    for item in examples[:8]:
        if isinstance(item, dict):
            texts.append(
                " ".join(
                    str(item.get(key) or "")
                    for key in ["title", "platform_title", "name", "prototype_name", "keywords"]
                )
            )
        else:
            texts.append(str(item))
    return texts


def _prototype_text_similarity(target_tokens: set[str], text: str) -> float:
    prototype_tokens = _research_tokens(text)
    if not target_tokens or not prototype_tokens:
        return 0.0
    overlap = len(target_tokens & prototype_tokens)
    return min(1.0, overlap / max(1, min(len(target_tokens), len(prototype_tokens))))


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


def _research_confidence(sample_count: int, selected: list[dict], scope: str, prototype_hits: list[dict]) -> float:
    best_similarity = max((float(row.get("similarity") or 0) for row in selected), default=0.0)
    coverage = clamp(sample_count / 50.0, 0.0, 1.0)
    match_density = clamp(len(selected) / 5.0, 0.0, 1.0)
    scope_factor = {"account": 1.0, "global_fallback": 0.72, "global": 0.62, "none": 0.0}.get(scope, 0.35)
    prototype_factor = max((float(row.get("confidence") or 0) for row in prototype_hits), default=0.0)
    return clamp(
        coverage * 0.25
        + best_similarity * 0.35
        + match_density * 0.15
        + scope_factor * 0.15
        + prototype_factor * 0.10,
        0.0,
        1.0,
    )


def _confidence_label(value: float) -> str:
    if value >= 0.7:
        return "high"
    if value >= 0.4:
        return "medium"
    return "low"


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
               program_name, artist_names, song_title, tags
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
        for key in ["title", "tags", "artist_names", "song_title", "program_name", "content_category", "hook_type", "slice_structure"]
    )


def _research_similarity(target_tokens: set[str], sample_text: str, segment: dict, sample: dict) -> float:
    sample_tokens = _research_tokens(sample_text)
    if not target_tokens or not sample_tokens:
        token_score = 0.0
    else:
        token_score = len(target_tokens & sample_tokens) / max(1, len(target_tokens | sample_tokens))
    semantic_bonus = 0.0
    if _text_equal(segment.get("music_slice_type"), sample.get("content_category")):
        semantic_bonus += 0.1
    if _text_equal(segment.get("short_video_structure"), sample.get("slice_structure")):
        semantic_bonus += 0.08
    if str(sample.get("hook_type") or "") and str(sample.get("hook_type")) in _research_text(segment):
        semantic_bonus += 0.08
    return min(1.0, token_score + semantic_bonus)


def _research_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", cleaned)
    tokens = set(words)
    chinese_chars = [word for word in words if len(word) == 1 and "\u4e00" <= word <= "\u9fff"]
    tokens.update("".join(chinese_chars[index : index + 2]) for index in range(max(0, len(chinese_chars) - 1)))
    tokens.update("".join(chinese_chars[index : index + 3]) for index in range(max(0, len(chinese_chars) - 2)))
    return {token for token in tokens if token}


def _label_from_reward(value: float) -> str:
    if value >= 70:
        return "high"
    if value <= 30:
        return "low"
    return "mid"


def _text_equal(left: object, right: object) -> bool:
    return bool(left and right and str(left).strip().lower() == str(right).strip().lower())


def _json_field(value: object, default: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def _low_originality(segment: dict) -> float:
    text = " ".join(
        str(segment.get(key) or "")
        for key in ["transcript", "summary", "short_video_structure", "program_context", "music_slice_type"]
    )
    duration = float(segment["duration_seconds"])
    risk = 10.0 + _negative_feedback_risk(segment, text, duration) * 0.65
    if _is_sparse_audio_only(segment):
        risk += 18
    if not _has_context(text, segment.get("short_video_structure") or "", segment.get("program_context") or ""):
        risk += 12
    if "节目上下文需人工确认" in text:
        risk += 8
    if "综艺叙事爆点闭环型" in text or "节目叙事到音乐爆点型" in text:
        risk -= 8
    return round(clamp(risk), 2)


def _final_score(scores: dict[str, float]) -> float:
    value = (
        0.16 * scores["short_video_hook_score"]
        + 0.14 * scores["musical_moment_score"]
        + 0.12 * scores["narrative_context_score"]
        + 0.09 * scores["chorus_climax_score"]
        + 0.07 * scores["lyric_resonance_score"]
        + 0.06 * scores["performer_stage_score"]
        + 0.08 * scores["audience_reaction_score"]
        + 0.11 * scores["comment_trigger_score"]
        + 0.04 * scores["song_recognition_score"]
        + 0.06 * scores["novelty_arrangement_score"]
        + 0.04 * scores["production_quality_score"]
        + 0.03 * scores["history_match_score"]
        - 0.20 * scores["rights_risk_score"]
        - 0.12 * scores["low_originality_score"]
    )
    return round(clamp(value), 2)


def _title_suggestions(segment: dict) -> list[str]:
    emotion = segment.get("emotion_type") or "舞台"
    slice_type = segment.get("music_slice_type")
    transcript = _clean_transcript(segment.get("transcript") or "")
    fragment = _display_fragment(transcript)
    focus = _focus_label(segment, transcript)
    duration = max(1, round(float(segment.get("duration_seconds") or 0)))
    titles: list[str] = []

    if fragment:
        if _contains_any(transcript, FIRST_STAGE_CUES):
            titles.extend(
                [
                    f"{fragment}，这段紧张感先立住了",
                    f"第一次登台的压力，都藏在这 {duration} 秒里",
                ]
            )
        elif _contains_any(transcript, SUSPENSE_CUES):
            titles.extend(
                [
                    f"{fragment}，这段可能影响后面的结果",
                    f"先看完这 {duration} 秒，再判断这个结果",
                ]
            )
        elif _contains_any(transcript, STORY_CUES):
            titles.extend(
                [
                    f"{fragment}，后面的舞台就有了情绪底色",
                    f"知道这句铺垫，再听后面完全不一样",
                ]
            )
        elif _contains_any(transcript, MUSIC_CUES):
            titles.extend(
                [
                    f"{fragment}，音乐爆点从这里开始",
                    f"这 {duration} 秒最值得剪的是这个音乐记忆点",
                ]
            )
        else:
            titles.extend(
                [
                    f"{fragment}，这一段的看点很集中",
                    f"这 {duration} 秒抓住的是「{focus}」",
                ]
            )

    if slice_type == "综艺叙事爆点闭环型":
        titles.extend([
            "导师刚说完，副歌一进现场反应变了",
            "这段从悬念到爆点，最后全场给了答案",
            "前面不是废话，是为了这一秒爆发",
        ])
    if slice_type == "赛制悬念到音乐爆点型":
        titles.extend([
            "晋级悬念前，这一段表现太关键了",
            "结果先别急，看完这个爆点再判断",
            "这句唱完，赛制压力全写在现场反应里",
        ])
    if slice_type == "歌手故事到音乐爆点型":
        titles.extend([
            "知道他的故事后，这个高音完全不一样",
            "不是单纯炫技，是把经历唱出来了",
            "前面的故事，都落在这一句副歌里",
        ])
    if segment.get("music_slice_type") == "节目叙事到音乐爆点型":
        titles.extend([
            "导师这句话之后，副歌一出来就变了",
            "前面都在铺垫，真正的爆点在后面",
            "这段改编一出来，现场反应就有了变化",
        ])
    if emotion == "遗憾":
        titles.extend([
            "这句歌词一出来，遗憾感直接拉满",
            "他不是在唱高音，是在唱错过",
            "这段最戳人的其实是这一句",
        ])
    titles.extend([
        "这段舞台真正抓人的地方在第 12 秒",
        "听到这里才懂这个改编的用意",
        "这不是纯高音，是短短几十秒的情绪推进",
    ])
    return sanitize_title_suggestions(titles)[:3]


def sanitize_title_suggestions(titles: list[str]) -> list[str]:
    sanitized: list[str] = []
    for title in titles:
        text = str(title or "").strip()
        if not text:
            continue
        text = _remove_title_meta_prompt(text)
        if text:
            sanitized.append(text)
    return _unique_titles(sanitized)


def _remove_title_meta_prompt(title: str) -> str:
    title = re.sub(r"[，,、；;：:]?这段为什么值得单独切出来", "", title).strip()
    title = re.sub(r"[，,、；;：:]?为什么值得单独切出来", "", title).strip()
    title = re.sub(r"[，,、；;：:]?这段为什么值得切出来", "", title).strip()
    if title in {"这段", "这个片段"}:
        return ""
    return title


def _cover_suggestion(segment: dict) -> str:
    cover_time = segment.get("cover_time")
    if cover_time is None:
        return "优先选择歌手强表情、导师反应或舞台高潮帧"
    return f"优先选择 {float(cover_time):.1f}s 附近的歌手强表情、导师反应或舞台高潮帧"


def _explanation(
    segment: dict,
    scores: dict[str, float],
    final: float,
    export_allowed: bool,
    risk_notes: list[str],
) -> str:
    if risk_notes:
        policy_text = risk_notes[0]
    else:
        policy_text = "样本合规，可导出" if export_allowed else "样本未通过策略检查，仅可分析"
    transcript = _clean_transcript(segment.get("transcript") or "")
    fragment = _display_fragment(transcript)
    focus = _focus_label(segment, transcript)
    reason = _selection_reason(segment, transcript)
    evidence = f"片段看点：{focus}"
    if fragment:
        evidence += f"，原句「{fragment}」"
    evidence += f"。选择原因：{reason}。"
    return (
        f"总分 {final}。{evidence}推荐代理：开头 hook/首5秒留存 {scores['short_video_hook_score']:.0f}，"
        f"音乐爆点 {scores['musical_moment_score']:.0f}，"
        f"上下文完整度 {scores['narrative_context_score']:.0f}，"
        f"互动评论触发 {scores['comment_trigger_score']:.0f}，"
        f"低原创/负反馈风险 {scores['low_originality_score']:.0f}（越低越好）。"
        f"结构为「{segment.get('short_video_structure')}」，"
        f"音乐点为「{segment.get('musical_moment')}」，"
        f"评论触发点为「{segment.get('comment_trigger')}」。{policy_text}。"
        f"评分版本：{SCORER_VERSION}。"
    )


def _first_five_retention_proxy(
    has_early_hook: bool,
    has_context: bool,
    has_climax: bool,
    has_reaction: bool,
    has_suspense: bool,
    has_story: bool,
    pure_audio: bool,
    duration_fit: float,
) -> float:
    value = 44.0
    value += 22 if has_early_hook else 0
    value += 12 if has_context else 0
    value += 9 if has_climax else 0
    value += 7 if has_reaction else 0
    value += 8 if has_suspense or has_story else 0
    value += duration_fit
    value -= 16 if pure_audio else 0
    return clamp(value)


def _context_completeness_proxy(
    has_context: bool,
    has_climax: bool,
    has_reaction: bool,
    has_suspense: bool,
    has_story: bool,
    text: str,
    context: str,
) -> float:
    value = 38.0
    value += 22 if has_context else 0
    value += 14 if has_context and has_climax else 0
    value += 12 if has_context and has_climax and has_reaction else 0
    value += 8 if has_suspense else 0
    value += 8 if has_story else 0
    value += min(10, len(text) / 30)
    value -= 12 if "需人工确认" in context else 0
    return clamp(value)


def _interaction_proxy(
    trigger: str,
    has_reaction: bool,
    has_suspense: bool,
    has_story: bool,
    has_emotion: bool,
    has_climax: bool,
) -> float:
    value = 42.0
    value += 16 if trigger else 0
    value += 14 if has_suspense else 0
    value += 10 if has_reaction else 0
    value += 8 if has_story else 0
    value += 8 if has_emotion else 0
    value += 6 if has_climax else 0
    return clamp(value)


def _negative_feedback_risk(segment: dict, text: str, duration: float) -> float:
    risk = 8.0
    if duration < 12 or duration > 90:
        risk += 28
    elif duration < 18 or duration > 65:
        risk += 10
    if _is_sparse_audio_only(segment):
        risk += 26
    if "节目上下文需人工确认" in text:
        risk += 14
    if "直入听觉爆点型" in text and not _contains_any(text, REACTION_CUES):
        risk += 10
    if _contains_any(text, ["标题党", "争吵", "失误", "跑调"]):
        risk += 8
    return clamp(risk)


def _duration_fit(duration: float) -> float:
    if 22 <= duration <= 48:
        return 10.0
    if 18 <= duration <= 65:
        return 6.0
    if 12 <= duration <= 80:
        return 0.0
    return -12.0


def _has_context(text: str, structure: str, context: str) -> bool:
    return (
        "上下文" in structure
        or "节目" in context
        or _contains_any(text, HOOK_CUES)
        or _contains_any(text, SUSPENSE_CUES)
        or _contains_any(text, STORY_CUES)
    )


def _has_music_climax(text: str, moment: str) -> bool:
    return "爆点" in moment or _contains_any(text, MUSIC_CUES)


def _is_sparse_audio_only(segment: dict) -> bool:
    transcript = (segment.get("transcript") or "").strip()
    return (
        not transcript
        or transcript == "音乐/舞台高能候选片段"
        or ("未转写片段" in transcript and segment.get("music_slice_type") == "直入听觉爆点型")
    )


def _contains_any(text: str, words: list[str]) -> bool:
    lower = text.lower()
    return any(word.lower() in lower for word in words)


def _clean_transcript(text: str) -> str:
    text = " ".join(str(text or "").split())
    if text in {"音乐/舞台高能候选片段"} or "未转写片段" in text:
        return ""
    return text


def _display_fragment(text: str, *, max_chars: int = 22) -> str:
    phrases = [
        phrase.strip()
        for phrase in re.split(r"[\s，。！？、,.!?]+", text or "")
        if phrase.strip()
    ]
    if not phrases:
        return ""
    cues = FIRST_STAGE_CUES + HOOK_CUES + SUSPENSE_CUES + STORY_CUES + MUSIC_CUES + REACTION_CUES
    for cue in cues:
        for phrase in phrases:
            if cue.lower() in phrase.lower():
                return phrase[:max_chars]
    for phrase in phrases:
        if len(phrase) >= 4:
            return phrase[:max_chars]
    return phrases[0][:max_chars]


def _focus_label(segment: dict, transcript: str) -> str:
    text = " ".join(
        str(segment.get(key) or "")
        for key in ["transcript", "program_context", "comment_trigger", "musical_moment"]
    )
    if _contains_any(transcript, FIRST_STAGE_CUES):
        return "首次登台/紧张感"
    if _contains_any(text, ["导师", "评委", "点评", "老师"]):
        return "导师评价/节目判断"
    if _contains_any(text, SUSPENSE_CUES):
        return "赛制悬念/结果预期"
    if _contains_any(text, STORY_CUES):
        return "歌手故事/人物铺垫"
    if _contains_any(text, MUSIC_CUES) or "爆点" in text:
        return "音乐爆点/改编记忆点"
    if _contains_any(text, REACTION_CUES):
        return "现场反应/名场面"
    return str(segment.get("music_slice_type") or segment.get("emotion_type") or "舞台表现")


def _selection_reason(segment: dict, transcript: str) -> str:
    if _contains_any(transcript, FIRST_STAGE_CUES):
        return "开头有人物状态，能先建立代入感，再承接舞台表现"
    if _contains_any(transcript, SUSPENSE_CUES):
        return "片段包含结果预期或赛制压力，天然容易触发评论判断"
    if _contains_any(transcript, STORY_CUES):
        return "前置信息能解释后续音乐情绪，避免变成纯歌曲截取"
    if _contains_any(transcript, MUSIC_CUES):
        return "字幕里出现明确音乐记忆点，适合和舞台画面一起剪成短视频"
    if "节目上下文需人工确认" in str(segment.get("program_context") or ""):
        return "当前主要依赖音频/画面高能点，建议人工确认前后文"
    return "片段具备节目上下文和舞台表现，可作为短视频候选进一步人工审核"


def _unique_titles(titles: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for title in titles:
        normalized = re.sub(r"\s+", "", title).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(title)
    return unique
