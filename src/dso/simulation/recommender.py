from __future__ import annotations

import json
from collections import Counter

from dso.db.session import connect, fetch_all, fetch_one
from dso.feedback.reward import duration_bucket
from dso.utils import clamp


STAGE_LABELS = {
    "cold_start": "冷启动人群匹配",
    "first_pool": "首轮留存测试",
    "expansion": "扩量互动潜力",
    "long_tail": "长尾持续消费",
    "risk": "风险/质量重排",
    "diversity": "队列多样性",
}


def simulate_video(video_id: str, top_k: int = 10) -> dict:
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT c.*, v.account_id, v.title AS video_title,
                   s.short_video_hook_score, s.musical_moment_score, s.narrative_context_score,
                   s.chorus_climax_score, s.lyric_resonance_score, s.performer_stage_score,
                   s.audience_reaction_score, s.comment_trigger_score, s.song_recognition_score,
                   s.novelty_arrangement_score, s.history_match_score, s.production_quality_score,
                   s.rights_risk_score, s.low_originality_score, s.final_score,
                   s.score_explanation, s.title_suggestions, s.cover_suggestion, s.risk_notes
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            JOIN slice_scores s ON s.candidate_segment_id = c.id
            WHERE c.source_video_id = ?
            ORDER BY s.final_score DESC
            LIMIT ?
            """,
            [video_id, max(top_k * 3, top_k)],
        )
        simulations = _simulate_rows(conn, rows)

    simulations = simulations[:top_k]
    return {
        "video_id": video_id,
        "count": len(simulations),
        "summary": _summary(simulations),
        "simulations": simulations,
    }


def simulate_segment(segment_id: str) -> dict:
    with connect() as conn:
        row = fetch_one(
            conn,
            """
            SELECT c.*, v.account_id, v.title AS video_title,
                   s.short_video_hook_score, s.musical_moment_score, s.narrative_context_score,
                   s.chorus_climax_score, s.lyric_resonance_score, s.performer_stage_score,
                   s.audience_reaction_score, s.comment_trigger_score, s.song_recognition_score,
                   s.novelty_arrangement_score, s.history_match_score, s.production_quality_score,
                   s.rights_risk_score, s.low_originality_score, s.final_score,
                   s.score_explanation, s.title_suggestions, s.cover_suggestion, s.risk_notes
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            JOIN slice_scores s ON s.candidate_segment_id = c.id
            WHERE c.id = ?
            """,
            [segment_id],
        )
        if not row:
            raise KeyError(f"segment not found or not scored: {segment_id}")
        return _simulate_rows(conn, [row])[0]


def _simulate_rows(conn, rows: list[dict]) -> list[dict]:
    simulated: list[dict] = []
    seen_types: Counter[str] = Counter()
    seen_structures: Counter[str] = Counter()
    for index, row in enumerate(rows, 1):
        type_key = row.get("music_slice_type") or "unknown"
        structure_key = row.get("short_video_structure") or "unknown"
        diversity_score = clamp(100 - seen_types[type_key] * 9 - seen_structures[structure_key] * 7)
        seen_types[type_key] += 1
        seen_structures[structure_key] += 1
        simulated.append(_simulate_row(conn, row, index, diversity_score))
    simulated.sort(key=lambda item: (item["simulated_score"], item["base_rank"] * -0.01), reverse=True)
    for index, item in enumerate(simulated, 1):
        item["simulation_rank"] = index
    return simulated


def _simulate_row(conn, row: dict, base_rank: int, diversity_score: float) -> dict:
    audience_clusters = _audience_clusters(row)
    history_prior, history_count = _history_prior(conn, row)
    topic_clarity = _topic_clarity(row)
    audience_breadth = clamp(48 + len(audience_clusters) * 8 + (8 if _has_any(row, ["经典", "青春", "梦想", "高音"]) else 0))
    risk_score = _risk_rerank_score(row)
    duration_score = _duration_score(float(row.get("duration_seconds") or 0))

    cold_start = clamp(
        0.28 * _num(row, "final_score")
        + 0.24 * topic_clarity
        + 0.14 * _num(row, "song_recognition_score")
        + 0.14 * _num(row, "novelty_arrangement_score")
        + 0.12 * audience_breadth
        + 0.08 * history_prior
        - 0.08 * _num(row, "low_originality_score")
    )
    first_pool = clamp(
        0.36 * _num(row, "short_video_hook_score")
        + 0.21 * _num(row, "musical_moment_score")
        + 0.13 * _num(row, "chorus_climax_score")
        + 0.11 * _num(row, "narrative_context_score")
        + 0.10 * _num(row, "production_quality_score")
        + 0.09 * duration_score
        - 0.10 * _num(row, "low_originality_score")
    )
    expansion = clamp(
        0.23 * first_pool
        + 0.22 * _num(row, "comment_trigger_score")
        + 0.17 * _num(row, "audience_reaction_score")
        + 0.13 * _num(row, "lyric_resonance_score")
        + 0.10 * _num(row, "novelty_arrangement_score")
        + 0.10 * _num(row, "final_score")
        + 0.05 * _num(row, "song_recognition_score")
        - 0.09 * _num(row, "low_originality_score")
    )
    long_tail = clamp(
        0.22 * _num(row, "lyric_resonance_score")
        + 0.20 * _num(row, "song_recognition_score")
        + 0.17 * _num(row, "narrative_context_score")
        + 0.15 * history_prior
        + 0.12 * _num(row, "production_quality_score")
        + 0.10 * _num(row, "final_score")
        + 0.04 * topic_clarity
        - 0.07 * _num(row, "low_originality_score")
    )

    stages = {
        "cold_start": cold_start,
        "first_pool": first_pool,
        "expansion": expansion,
        "long_tail": long_tail,
        "risk": risk_score,
        "diversity": diversity_score,
    }
    simulated_score = round(
        clamp(
            0.18 * cold_start
            + 0.26 * first_pool
            + 0.20 * expansion
            + 0.14 * long_tail
            + 0.16 * risk_score
            + 0.06 * diversity_score
        ),
        2,
    )
    bottleneck_key = min(stages, key=stages.get)
    title = _first_title(row)
    return {
        "segment_id": row["id"],
        "source_video_id": row["source_video_id"],
        "video_title": row.get("video_title") or "",
        "base_rank": base_rank,
        "simulation_rank": base_rank,
        "title": title,
        "time_range": {
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "duration_seconds": row["duration_seconds"],
        },
        "music_slice_type": row.get("music_slice_type"),
        "emotion_type": row.get("emotion_type"),
        "short_video_structure": row.get("short_video_structure"),
        "musical_moment": row.get("musical_moment"),
        "final_score": round(_num(row, "final_score"), 2),
        "simulated_score": simulated_score,
        "predicted_stage": _predicted_stage(simulated_score, stages),
        "bottleneck": {
            "key": bottleneck_key,
            "label": STAGE_LABELS[bottleneck_key],
            "score": round(stages[bottleneck_key], 2),
            "reason": _stage_reason(bottleneck_key, row, history_count),
        },
        "audience_clusters": audience_clusters,
        "history_prior_score": round(history_prior, 2),
        "history_sample_count": history_count,
        "stage_flow": [
            _stage_item("cold_start", cold_start, row, history_count),
            _stage_item("first_pool", first_pool, row, history_count),
            _stage_item("expansion", expansion, row, history_count),
            _stage_item("long_tail", long_tail, row, history_count),
            _stage_item("risk", risk_score, row, history_count),
            _stage_item("diversity", diversity_score, row, history_count),
        ],
        "actions": _actions(stages, row, history_count),
    }


def _summary(simulations: list[dict]) -> dict:
    if not simulations:
        return {
            "avg_score": 0,
            "high_potential_count": 0,
            "top_bottleneck": "暂无",
            "top_stage": "暂无",
        }
    avg = sum(item["simulated_score"] for item in simulations) / len(simulations)
    bottlenecks = Counter(item["bottleneck"]["label"] for item in simulations)
    stages = Counter(item["predicted_stage"] for item in simulations)
    return {
        "avg_score": round(avg, 2),
        "high_potential_count": sum(1 for item in simulations if item["simulated_score"] >= 72),
        "top_bottleneck": bottlenecks.most_common(1)[0][0],
        "top_stage": stages.most_common(1)[0][0],
    }


def _audience_clusters(row: dict) -> list[str]:
    text = _text(row)
    clusters: list[str] = []
    if _contains_any(text, ["首秀", "第一次", "紧张", "壓力", "压力"]):
        clusters.append("歌手成长线受众")
    if _contains_any(text, ["赛制", "排名", "结果", "晋级", "淘汰", "悬念"]):
        clusters.append("节目追更/赛制讨论人群")
    if _contains_any(text, ["导师", "评委", "老师", "点评", "专业判断"]):
        clusters.append("导师点评/专业分析受众")
    if _contains_any(text, ["高音", "长音", "副歌", "和声", "合唱", "改编", "solo"]):
        clusters.append("音乐技术/舞台表现受众")
    if _contains_any(text, ["梦想", "遗憾", "青春", "经典", "回忆", "共鸣", "歌词"]):
        clusters.append("情绪共鸣/经典歌曲受众")
    if _contains_any(text, ["全场", "现场", "观众", "欢呼", "泪目", "炸场"]):
        clusters.append("现场氛围/名场面受众")
    if not clusters:
        clusters.append("音乐综艺泛受众")
    return clusters[:4]


def _history_prior(conn, row: dict) -> tuple[float, int]:
    bucket = duration_bucket(row.get("duration_seconds"))
    result = fetch_one(
        conn,
        """
        SELECT AVG(CASE WHEN ts.normalized_reward > 0 THEN ts.normalized_reward ELSE ts.reward_proxy END) AS reward,
               COUNT(*) AS count
        FROM training_samples ts
        JOIN candidate_segments c ON c.id = ts.candidate_segment_id
        JOIN source_videos v ON v.id = c.source_video_id
        WHERE v.account_id = ?
          AND c.music_slice_type = ?
          AND CASE
            WHEN c.duration_seconds < 25 THEN 'short'
            WHEN c.duration_seconds < 45 THEN 'medium'
            WHEN c.duration_seconds < 90 THEN 'long'
            ELSE 'extra_long'
          END = ?
        """,
        [row.get("account_id") or "main", row.get("music_slice_type") or "", bucket],
    )
    count = int(result["count"] or 0) if result else 0
    if count <= 0:
        return float(row.get("history_match_score") or 50.0), 0
    return clamp(float(result["reward"] or 50.0)), count


def _topic_clarity(row: dict) -> float:
    value = 46.0
    slice_type = str(row.get("music_slice_type") or "")
    structure = str(row.get("short_video_structure") or "")
    context = str(row.get("program_context") or "")
    if slice_type and slice_type not in {"节目叙事型", "铺垫到高潮型"}:
        value += 18
    if "->" in structure and "铺垫信息 -> 舞台表现 -> 结果/反应" not in structure:
        value += 14
    if context and "需人工确认" not in context:
        value += 10
    if _contains_any(_text(row), ["导师", "赛制", "首秀", "高音", "共鸣", "改编", "炸场"]):
        value += 8
    return clamp(value)


def _risk_rerank_score(row: dict) -> float:
    duration = float(row.get("duration_seconds") or 0)
    duration_penalty = 0
    if duration < 12 or duration > 90:
        duration_penalty = 24
    elif duration < 18 or duration > 75:
        duration_penalty = 10
    return clamp(
        100
        - 0.70 * _num(row, "rights_risk_score")
        - 0.62 * _num(row, "low_originality_score")
        - duration_penalty
    )


def _duration_score(duration: float) -> float:
    if 22 <= duration <= 48:
        return 92
    if 18 <= duration <= 65:
        return 78
    if 12 <= duration <= 80:
        return 62
    return 38


def _predicted_stage(score: float, stages: dict[str, float]) -> str:
    if stages["risk"] < 60:
        return "风险重排受限"
    if stages["first_pool"] < 58:
        return "首轮留存偏弱"
    if stages["cold_start"] < 60:
        return "冷启动匹配偏窄"
    if stages["expansion"] < 66:
        return "小流量池可测"
    if stages["long_tail"] < 64:
        return "短期扩量候选"
    if score >= 74:
        return "高潜扩量候选"
    return "稳定测试候选"


def _stage_item(key: str, score: float, row: dict, history_count: int) -> dict:
    return {
        "key": key,
        "label": STAGE_LABELS[key],
        "score": round(score, 2),
        "status": "ok" if score >= 72 else "warn" if score >= 58 else "risk",
        "reason": _stage_reason(key, row, history_count),
    }


def _stage_reason(key: str, row: dict, history_count: int) -> str:
    if key == "cold_start":
        return "依据切片类型、主题清晰度、歌曲识别度和账号历史相似样本估算首批受众匹配。"
    if key == "first_pool":
        return "依据开头 hook、音乐爆点、时长适配和上下文完整度估算首轮留存。"
    if key == "expansion":
        return "依据评论触发、现场反应、情绪共鸣和互动潜力估算是否值得继续扩量。"
    if key == "long_tail":
        return "依据歌词共鸣、歌曲识别、节目上下文和历史样本估算长尾消费。"
    if key == "risk":
        return "依据授权风险、低原创/负反馈风险和时长异常做质量重排模拟。"
    if history_count:
        return f"同账号同类型同长度已有 {history_count} 条样本，可用于多样性和历史校准。"
    return "按当前 Top 队列中相同类型/结构的重复度做去重重排模拟。"


def _actions(stages: dict[str, float], row: dict, history_count: int) -> list[str]:
    actions: list[str] = []
    if stages["first_pool"] < 66:
        actions.append("强化前 3 秒：把导师评价、结果悬念或歌手状态前置。")
    if stages["expansion"] < 66:
        actions.append("补一个可讨论点：胜负判断、改编争议、现场反应或歌词共鸣。")
    if stages["long_tail"] < 64:
        actions.append("标题和封面突出歌曲记忆点或人物故事，提升非粉丝理解成本。")
    if stages["risk"] < 72:
        actions.append("缩短或重剪片段，减少纯歌曲/低上下文比例。")
    if stages["diversity"] < 74:
        actions.append("同类片段较多，发布队列中建议错开结构和标题角度。")
    if history_count <= 0:
        actions.append("当前缺少同类型历史表现样本，建议小流量测试后回填 CSV。")
    return actions[:4] or ["该切片可进入正常候选池，优先做标题/封面小版本实验。"]


def _first_title(row: dict) -> str:
    raw = row.get("title_suggestions") or ""
    try:
        titles = json.loads(raw) if isinstance(raw, str) else raw
        if titles:
            return str(titles[0])
    except Exception:
        pass
    return str(row.get("summary") or row.get("music_slice_type") or "候选切片")


def _num(row: dict, key: str) -> float:
    return float(row.get(key) or 0.0)


def _text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
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


def _has_any(row: dict, words: list[str]) -> bool:
    return _contains_any(_text(row), words)


def _contains_any(text: str, words: list[str]) -> bool:
    lower = str(text or "").lower()
    return any(word.lower() in lower for word in words)
