from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from statistics import mean

from dso.db.session import connect, fetch_all, fetch_one
from dso.features.asr_contract import asr_profile_plan
from dso.features.asr_routing import route_candidate_asr, route_video_asr
from dso.scoring.rights import rights_mode
from dso.simulation.recommender import simulate_video
from dso.utils import clamp, read_json, utc_now
from dso.versions import QUALITY_GATE_VERSION, QUALITY_INSIGHTS_VERSION, component_versions


SPONSOR_TERMS = [
    "合作伙伴",
    "超级合作伙伴",
    "提醒您",
    "销量第一",
    "怕上火",
    "VIP",
    "vivo",
    "王老吉",
    "白雀羚",
    "欧丽薇兰",
    "欧利威兰",
    "动感地带",
    "芒果卡",
    "盲国卡",
    "酸酸乳",
    "QQ音乐",
    "网易云",
    "扫码",
    "直播间",
]

REVIEW_RIGHTS_RISK_SCORE = 50
BLOCK_RIGHTS_RISK_SCORE = 80
REVIEW_LOW_ORIGINALITY_SCORE = 45
BLOCK_LOW_ORIGINALITY_SCORE = 80


def quality_insights(video_id: str, top_k: int = 30) -> dict:
    top_k = max(1, int(top_k or 30))
    with connect() as conn:
        video = fetch_one(conn, "SELECT * FROM source_videos WHERE id = ?", [video_id])
        if not video:
            raise KeyError(f"video not found: {video_id}")
        counts = _counts(conn, video_id)
        rows = fetch_all(
            conn,
            """
            SELECT c.*, s.final_score, s.rights_risk_score, s.low_originality_score,
                   s.short_video_hook_score, s.musical_moment_score, s.narrative_context_score,
                   s.comment_trigger_score, s.score_explanation, s.title_suggestions
            FROM candidate_segments c
            LEFT JOIN slice_scores s ON s.candidate_segment_id = c.id
            WHERE c.source_video_id = ?
            ORDER BY COALESCE(s.final_score, 0) DESC, c.start_time
            LIMIT ?
            """,
            [video_id, top_k],
        )

    transcript = _load_transcript(video)
    transcript_summary = _transcript_summary(video, transcript)
    queue_summary = _queue_summary(rows, counts)
    issues = _issues(transcript_summary, queue_summary)
    asr_routing = _asr_routing(video, transcript_summary, rows, issues)
    health_score = _health_score(transcript_summary, queue_summary, issues)
    health = {
        "score": health_score,
        "level": _health_level(health_score),
        "top_issue": issues[0]["label"] if issues else "暂无明显质量风险",
    }
    watchlist = _watchlist(rows)
    gate = _quality_gate(health, transcript_summary, queue_summary, issues)
    return {
        "contract_version": QUALITY_INSIGHTS_VERSION,
        "component_versions": component_versions(),
        "video_id": video_id,
        "video_title": video.get("title") or "",
        "generated_at": utc_now(),
        "query": {"top_k": top_k, "simulation_top_k": min(top_k, 10)},
        "health": health,
        "gate": gate,
        "transcript": transcript_summary,
        "asr_profiles": asr_profile_plan(),
        "asr_routing": asr_routing,
        "queue": queue_summary,
        "issues": issues,
        "actions": _actions(transcript_summary, queue_summary, issues),
        "watchlist": watchlist,
        "simulation": _simulation_linkage(video_id, rows, issues, health_score, top_k),
    }


def _counts(conn, video_id: str) -> dict:
    candidate_count = fetch_one(
        conn, "SELECT COUNT(*) AS count FROM candidate_segments WHERE source_video_id = ?", [video_id]
    )["count"]
    scored_count = fetch_one(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM candidate_segments c
        JOIN slice_scores s ON s.candidate_segment_id = c.id
        WHERE c.source_video_id = ?
        """,
        [video_id],
    )["count"]
    exports_count = fetch_one(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM candidate_segments c
        JOIN slice_variants v ON v.candidate_segment_id = c.id
        WHERE c.source_video_id = ? AND v.export_path IS NOT NULL AND v.export_path != ''
        """,
        [video_id],
    )["count"]
    return {
        "candidate_count": int(candidate_count or 0),
        "scored_count": int(scored_count or 0),
        "exports_count": int(exports_count or 0),
    }


def _load_transcript(video: dict) -> dict:
    path = video.get("transcript_path")
    if not path:
        return {}
    return read_json(Path(path), default={}) or {}


def _transcript_summary(video: dict, transcript: dict) -> dict:
    segments = list(transcript.get("segments") or [])
    metadata = transcript.get("metadata") or {}
    source = str(transcript.get("source") or "missing")
    repetition_rows = [_segment_signal(row) for row in segments if _has_repetition_noise(str(row.get("text") or ""))]
    ad_rows = [_segment_signal(row) for row in segments if _sponsor_hits(str(row.get("text") or ""))]
    segment_count_raw = int(metadata.get("segment_count_raw") or len(segments))
    segment_count_processed = int(metadata.get("segment_count_processed") or len(segments))
    cache_key = metadata.get("cache_key") if isinstance(metadata.get("cache_key"), dict) else {}
    whisper_cpp = cache_key.get("whisper_cpp") if isinstance(cache_key.get("whisper_cpp"), dict) else {}
    return {
        "source": source,
        "backend": str(metadata.get("backend") or source.split(":", 1)[0]),
        "status": video.get("status") or "",
        "path": video.get("transcript_path") or "",
        "segment_count": len(segments),
        "segment_count_raw": segment_count_raw,
        "segment_count_processed": segment_count_processed,
        "compression_ratio": round(segment_count_processed / segment_count_raw, 3) if segment_count_raw else 0,
        "postprocess_version": metadata.get("postprocess_version") or "",
        "profile": str(metadata.get("profile") or cache_key.get("profile") or ""),
        "model_size": str(metadata.get("model_size") or whisper_cpp.get("model_name") or ""),
        "routing": metadata.get("routing") if isinstance(metadata.get("routing"), dict) else {},
        "whisper_cpp_model_name": whisper_cpp.get("model_name") or "",
        "whisper_cpp_vad_enabled": bool(whisper_cpp.get("vad_enabled")),
        "whisper_cpp_vad_model": whisper_cpp.get("vad_model") or "",
        "whisper_cpp_extra_args": whisper_cpp.get("extra_args"),
        "repetition_noise_count": len(repetition_rows),
        "ad_read_count": len(ad_rows),
        "sample_repetition": repetition_rows[:3],
        "sample_ad_reads": ad_rows[:3],
    }


def _queue_summary(rows: list[dict], counts: dict) -> dict:
    if not rows:
        return {
            **counts,
            "top_k": 0,
            "avg_score": 0,
            "avg_duration_seconds": 0,
            "sponsor_risk_count": 0,
            "weak_context_count": 0,
            "audio_only_count": 0,
            "duration_outlier_count": 0,
            "closed_loop_count": 0,
            "unscored_top_count": 0,
            "rights_risk_count": 0,
            "blocking_rights_risk_count": 0,
            "max_rights_risk_score": 0,
            "low_originality_risk_count": 0,
            "blocking_low_originality_count": 0,
            "max_low_originality_score": 0,
            "type_mix": {},
        }
    scored = [float(row.get("final_score") or 0) for row in rows]
    durations = [float(row.get("duration_seconds") or 0) for row in rows]
    type_mix = Counter(str(row.get("music_slice_type") or "unknown") for row in rows)
    rights_scores = [_score_value(row, "rights_risk_score") for row in rows]
    originality_scores = [_score_value(row, "low_originality_score") for row in rows]
    return {
        **counts,
        "top_k": len(rows),
        "avg_score": round(mean(scored), 2),
        "avg_duration_seconds": round(mean(durations), 2),
        "sponsor_risk_count": sum(1 for row in rows if _candidate_sponsor_risk(row)),
        "weak_context_count": sum(1 for row in rows if _weak_context(row)),
        "audio_only_count": sum(1 for row in rows if _audio_only(row)),
        "duration_outlier_count": sum(1 for row in rows if _duration_outlier(row)),
        "closed_loop_count": sum(1 for row in rows if _closed_loop(row)),
        "unscored_top_count": sum(1 for row in rows if row.get("final_score") is None),
        "rights_risk_count": sum(1 for score in rights_scores if score >= REVIEW_RIGHTS_RISK_SCORE),
        "blocking_rights_risk_count": sum(1 for score in rights_scores if score >= BLOCK_RIGHTS_RISK_SCORE),
        "max_rights_risk_score": round(max(rights_scores or [0]), 2),
        "low_originality_risk_count": sum(
            1 for score in originality_scores if score >= REVIEW_LOW_ORIGINALITY_SCORE
        ),
        "blocking_low_originality_count": sum(
            1 for score in originality_scores if score >= BLOCK_LOW_ORIGINALITY_SCORE
        ),
        "max_low_originality_score": round(max(originality_scores or [0]), 2),
        "type_mix": dict(type_mix.most_common(6)),
    }


def _issues(transcript: dict, queue: dict) -> list[dict]:
    issues: list[dict] = []
    if transcript["source"] == "missing" or not transcript["path"] or transcript["segment_count"] <= 0:
        issues.append(
            {
                "key": "missing_transcript",
                "label": "尚未完成 ASR 转写",
                "severity": "warn",
                "count": 1,
                "evidence": transcript["status"] or "missing transcript",
                "recommendation": "先完成节目提取和 ASR，再生成候选与评分。",
            }
        )
    if transcript["repetition_noise_count"]:
        recommendation = (
            "当前已启用 VAD，建议提高 whisper.cpp 模型规格，或继续收紧重复文本过滤后再刷新候选。"
            if transcript.get("whisper_cpp_vad_enabled")
            else "启用 VAD/静音过滤，或提高 whisper.cpp 模型规格后再刷新候选。"
        )
        issues.append(
            {
                "key": "asr_repetition_noise",
                "label": "ASR 幻觉/重复文本",
                "severity": "risk",
                "count": transcript["repetition_noise_count"],
                "evidence": _evidence(transcript["sample_repetition"]),
                "recommendation": recommendation,
            }
        )
    if transcript["source"].startswith("whisper_cpp") and not transcript.get("whisper_cpp_vad_enabled"):
        issues.append(
            {
                "key": "whisper_cpp_base_no_vad",
                "label": "whisper.cpp 未启用 VAD",
                "severity": "warn",
                "count": 1,
                "evidence": transcript["source"],
                "recommendation": "保留其高速路径，但发布前用重复文本、广告词和闭环结构指标做二次筛选。",
            }
        )
    if transcript["ad_read_count"] and not queue["sponsor_risk_count"]:
        issues.append(
            {
                "key": "transcript_ad_reads",
                "label": "ASR 含品牌/广告口播",
                "severity": "warn",
                "count": transcript["ad_read_count"],
                "evidence": _evidence(transcript["sample_ad_reads"]),
                "recommendation": "Top 队列暂未命中品牌口播，但导出前仍需抽查字幕和上下文，避免广告词被剪入成片。",
            }
        )
    if queue["candidate_count"] <= 0:
        issues.append(
            {
                "key": "missing_candidates",
                "label": "尚未生成候选切片",
                "severity": "warn",
                "count": 1,
                "evidence": "candidate_segments = 0",
                "recommendation": "完成 ASR 后生成候选切片，再进入评分和推荐模拟。",
            }
        )
    elif queue["scored_count"] <= 0:
        issues.append(
            {
                "key": "missing_scores",
                "label": "候选尚未评分",
                "severity": "warn",
                "count": queue["candidate_count"],
                "evidence": f"{queue['candidate_count']} 条候选未进入评分表",
                "recommendation": "运行评分后再判断导出优先级和推荐链路瓶颈。",
            }
        )
    if queue["sponsor_risk_count"]:
        issues.append(
            {
                "key": "sponsor_risk",
                "label": "Top 队列含品牌/导流口播",
                "severity": "warn",
                "count": queue["sponsor_risk_count"],
                "evidence": f"Top{queue['top_k']} 中 {queue['sponsor_risk_count']} 条命中广告/导流词",
                "recommendation": "品牌口播只保留为上下文，独立切片需降权或人工复核。",
            }
        )
    if queue["closed_loop_count"] <= max(1, round(queue["top_k"] * 0.12)) and queue["top_k"]:
        issues.append(
            {
                "key": "weak_closed_loop",
                "label": "闭环候选偏少",
                "severity": "warn",
                "count": queue["closed_loop_count"],
                "evidence": f"Top{queue['top_k']} 中闭环结构 {queue['closed_loop_count']} 条",
                "recommendation": "优先补齐节目上下文 -> 音乐爆点 -> 现场反应的候选生成窗口。",
            }
        )
    if queue["weak_context_count"]:
        issues.append(
            {
                "key": "weak_context",
                "label": "部分候选节目上下文不足",
                "severity": "info",
                "count": queue["weak_context_count"],
                "evidence": f"{queue['weak_context_count']} 条需要人工确认上下文",
                "recommendation": "候选详情中优先展示前后字幕，辅助运营判断是否可独立发布。",
            }
        )
    severity_order = {"risk": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda item: (severity_order.get(item["severity"], 9), -int(item["count"])))
    return issues


def _health_score(transcript: dict, queue: dict, issues: list[dict]) -> int:
    score = 100
    if transcript["source"] == "missing" or transcript["segment_count"] <= 0:
        score -= 25
    score -= min(18, transcript["repetition_noise_count"] * 2)
    if queue["sponsor_risk_count"]:
        score -= min(16, transcript["ad_read_count"] * 2)
    else:
        score -= min(6, round(transcript["ad_read_count"] * 0.25))
    if queue["candidate_count"] <= 0:
        score -= 20
    elif queue["scored_count"] <= 0:
        score -= 16
    score -= min(18, queue["sponsor_risk_count"] * 5)
    score -= min(12, queue["weak_context_count"] * 3)
    score -= min(14, queue["duration_outlier_count"] * 3)
    if queue["candidate_count"] and queue["scored_count"] < queue["candidate_count"]:
        score -= 12
    if queue["top_k"] and queue["closed_loop_count"] <= max(1, round(queue["top_k"] * 0.12)):
        score -= 10
    if any(issue["severity"] == "risk" for issue in issues):
        score -= 4
        score = min(score, 79)
    return int(clamp(score))


def _health_level(score: int) -> str:
    if score >= 82:
        return "good"
    if score >= 58:
        return "warn"
    return "risk"


def _actions(transcript: dict, queue: dict, issues: list[dict]) -> list[str]:
    actions: list[str] = []
    keys = {issue["key"] for issue in issues}
    if "missing_transcript" in keys:
        actions.append("先完成节目提取和 ASR 转写，再判断候选质量。")
    if "missing_candidates" in keys:
        actions.append("为当前节目生成候选切片，并优先检查是否形成节目上下文、音乐爆点和现场反应闭环。")
    if "missing_scores" in keys:
        actions.append("运行评分，生成 Top 队列后再进入推荐模拟和导出预览。")
    if "asr_repetition_noise" in keys:
        if transcript.get("whisper_cpp_vad_enabled"):
            actions.append("先不要直接批量导出：当前 ASR 已启用 VAD 但仍有重复幻觉，建议提高模型规格或继续过滤重复段。")
        else:
            actions.append("先不要直接批量导出：当前 ASR 存在重复幻觉文本，建议启用 VAD 或过滤重复段后重建候选。")
    if "sponsor_risk" in keys:
        actions.append("扩展广告/导流词表，把动感地带、芒果卡、酸酸乳、扫码、直播间等错别字也纳入降权。")
    if "transcript_ad_reads" in keys:
        actions.append("Top 候选未直接命中广告口播，但导出前仍建议抽查字幕重叠段和前后上下文。")
    if "weak_closed_loop" in keys:
        actions.append("下一轮候选生成优先拉长上下文窗口，增加现场反应和导师评价闭环片段。")
    if queue["exports_count"] <= 0 and queue["scored_count"] > 0:
        actions.append("先挑 3 条高分且低风险候选导出预览，建立人工审核样本。")
    if not actions:
        actions.append("质量信号稳定，可进入标题/封面 A/B 和发布反馈导入循环。")
    return actions[:5]


def _asr_routing(video: dict, transcript: dict, rows: list[dict], issues: list[dict]) -> dict:
    video_route = route_video_asr(video, transcript_summary=transcript, issues=issues)
    candidate_routes = []
    for row in rows[:10]:
        route = route_candidate_asr(row, transcript_summary=transcript, issues=issues, requested_profile="auto")
        if route["decision"] == "keep_quality" and route["reason_keys"] == ["default_candidate_quality"]:
            continue
        candidate_routes.append(
            {
                "segment_id": row.get("id") or "",
                "time_range": {
                    "start_time": row.get("start_time"),
                    "end_time": row.get("end_time"),
                    "duration_seconds": row.get("duration_seconds"),
                },
                "final_score": round(float(row.get("final_score") or 0), 2),
                "decision": route["decision"],
                "recommended_profile": route["recommended_profile"],
                "recommended_model": route["recommended_model"],
                "reason_keys": route["reason_keys"],
                "reasons": route["reasons"],
                "preserve_quality_result": route["preserve_quality_result"],
                "evidence": _clip(str(row.get("transcript") or row.get("summary") or ""), 96),
                "signals": {
                    "duration_seconds": route["signals"].get("duration_seconds"),
                    "english_word_count": route["signals"].get("english_word_count"),
                    "cjk_count": route["signals"].get("cjk_count"),
                    "sponsor_hits": route["signals"].get("sponsor_hits", []),
                    "competition_hits": route["signals"].get("competition_hits", []),
                    "person_hits": route["signals"].get("person_hits", []),
                },
            }
        )

    verify_queue = [item for item in candidate_routes if item["decision"] == "verify_candidate"]
    english_preserve_queue = [
        item for item in candidate_routes if item["decision"] == "keep_quality_for_english"
    ]
    if video_route["decision"] == "rerun_full_video_quality":
        next_action = "rerun_full_video_quality"
    elif verify_queue:
        next_action = "verify_top_candidates"
    elif english_preserve_queue:
        next_action = "preserve_quality_for_english"
    else:
        next_action = "keep_current"

    return {
        "contract_version": video_route["contract_version"],
        "enabled": video_route["enabled"],
        "next_action": next_action,
        "video": video_route,
        "candidate_count": len(candidate_routes),
        "verify_count": len(verify_queue),
        "english_preserve_count": len(english_preserve_queue),
        "candidates": candidate_routes,
        "verify_queue": verify_queue[:5],
        "english_preserve_queue": english_preserve_queue[:5],
    }


def _quality_gate(health: dict, transcript: dict, queue: dict, issues: list[dict]) -> dict:
    issue_by_key = {issue["key"]: issue for issue in issues}
    reasons: list[dict] = []
    blocking_issue_keys: list[str] = []
    review_issue_keys: list[str] = []

    def add_reason(
        key: str,
        label: str,
        severity: str,
        evidence: str,
        action: str,
        *,
        blocking: bool = False,
    ) -> None:
        if any(reason["key"] == key for reason in reasons):
            return
        reasons.append(
            {
                "key": key,
                "label": label,
                "severity": severity,
                "evidence": evidence,
                "action": action,
            }
        )
        if blocking:
            blocking_issue_keys.append(key)
        else:
            review_issue_keys.append(key)

    def add_issue_reason(key: str, *, blocking: bool = False, severity: str | None = None) -> None:
        issue = issue_by_key.get(key)
        if not issue:
            return
        add_reason(
            key,
            issue["label"],
            severity or issue["severity"],
            issue.get("evidence") or "",
            issue.get("recommendation") or "",
            blocking=blocking,
        )

    add_issue_reason("missing_transcript", blocking=True, severity="risk")
    add_issue_reason("missing_candidates", blocking=True, severity="risk")
    add_issue_reason("missing_scores", blocking=True, severity="risk")

    if int(queue.get("candidate_count") or 0) > int(queue.get("scored_count") or 0) > 0:
        missing = int(queue.get("candidate_count") or 0) - int(queue.get("scored_count") or 0)
        add_reason(
            "partial_missing_scores",
            "部分候选尚未评分",
            "warn",
            f"{missing} 条候选缺少评分结果",
            "补齐评分后再按 Top 队列做导出优先级和推荐模拟判断。",
        )

    if int(queue.get("blocking_rights_risk_count") or 0):
        add_reason(
            "rights_risk_block",
            "Top 队列存在高授权风险",
            "risk",
            f"最高授权风险 {float(queue.get('max_rights_risk_score') or 0):.0f}",
            "先补齐授权记录或改选低授权风险候选，再进入发布前审核。",
            blocking=True,
        )
    elif int(queue.get("rights_risk_count") or 0):
        add_reason(
            "rights_risk_review",
            "Top 队列存在授权复核风险",
            "warn",
            f"{int(queue.get('rights_risk_count') or 0)} 条候选授权风险 >= {REVIEW_RIGHTS_RISK_SCORE}",
            "导出前逐条确认节目、歌曲、表演和肖像授权状态。",
        )

    if int(queue.get("blocking_low_originality_count") or 0):
        add_reason(
            "low_originality_block",
            "Top 队列存在严重低原创风险",
            "risk",
            f"最高低原创风险 {float(queue.get('max_low_originality_score') or 0):.0f}",
            "先替换为有节目上下文、现场反应或明确二创包装的候选。",
            blocking=True,
        )
    elif int(queue.get("low_originality_risk_count") or 0):
        add_reason(
            "low_originality_review",
            "Top 队列存在低原创风险",
            "warn",
            f"{int(queue.get('low_originality_risk_count') or 0)} 条候选低原创风险 >= {REVIEW_LOW_ORIGINALITY_SCORE}",
            "优先复核是否只是纯音乐/纯舞台截取，并补充标题、字幕和上下文包装。",
        )

    for key in [
        "asr_repetition_noise",
        "whisper_cpp_base_no_vad",
        "transcript_ad_reads",
        "sponsor_risk",
    ]:
        add_issue_reason(key)
    if health.get("level") != "good":
        for key in ["weak_closed_loop", "weak_context"]:
            add_issue_reason(key)

    if health.get("level") == "risk" and not any(reason["key"] == "health_risk" for reason in reasons):
        add_reason(
            "health_risk",
            "质量健康分处于高风险区间",
            "risk",
            f"health.score = {int(health.get('score') or 0)}",
            "先处理排名最高的质量问题，再刷新质量哨兵。",
        )
    elif health.get("level") == "warn" and not reasons:
        add_reason(
            "health_review",
            "质量健康分建议复核",
            "warn",
            f"health.score = {int(health.get('score') or 0)}",
            "抽查字幕、候选上下文和 Top 队列风险后再发布。",
        )

    status = "block" if blocking_issue_keys else ("review" if reasons else "allow")
    severity = _gate_severity(status, reasons)
    label = {
        "allow": "可进入发布前审核",
        "review": "需要人工复核",
        "block": "暂缓导出决策",
    }[status]

    return {
        "version": QUALITY_GATE_VERSION,
        "enforcement": "read_only",
        "status": status,
        "label": label,
        "summary": _gate_summary(status, health, queue, reasons),
        "primary_action": _gate_primary_action(status, reasons),
        "allowed_actions": _gate_allowed_actions(status),
        "blocked_actions": [],
        "severity": severity,
        "reasons": reasons[:8],
        "actions": _gate_actions(status, reasons),
        "blocking_issue_keys": blocking_issue_keys,
        "review_issue_keys": review_issue_keys,
        "signals": {
            "rights_mode": rights_mode(),
            "health_score": int(health.get("score") or 0),
            "health_level": health.get("level") or "",
            "candidate_count": int(queue.get("candidate_count") or 0),
            "scored_count": int(queue.get("scored_count") or 0),
            "transcript_segment_count": int(transcript.get("segment_count") or 0),
            "repetition_noise_count": int(transcript.get("repetition_noise_count") or 0),
            "ad_read_count": int(transcript.get("ad_read_count") or 0),
            "sponsor_risk_count": int(queue.get("sponsor_risk_count") or 0),
            "max_rights_risk_score": float(queue.get("max_rights_risk_score") or 0),
            "max_low_originality_score": float(queue.get("max_low_originality_score") or 0),
        },
    }


def _gate_summary(status: str, health: dict, queue: dict, reasons: list[dict]) -> str:
    scored = int(queue.get("scored_count") or 0)
    candidates = int(queue.get("candidate_count") or 0)
    if status == "allow":
        return f"当前 {scored} 条候选已进入评分，质量信号稳定，可选择高分候选导出 9:16 预览并进入人工终审。"
    if reasons:
        top = reasons[0]
        prefix = "暂缓导出决策" if status == "block" else "导出前需要人工复核"
        return f"{prefix}：{top.get('label') or '存在质量风险'}。本轮 Gate 为只读提示，不会自动阻断导出。"
    return (
        f"当前候选 {candidates} 条、已评分 {scored} 条，质量健康分 "
        f"{int(health.get('score') or 0)}，建议人工抽查后再导出。"
    )


def _gate_primary_action(status: str, reasons: list[dict]) -> dict:
    keys = {str(reason.get("key") or "") for reason in reasons}
    if status == "allow":
        return {
            "kind": "export_preview",
            "label": "导出预览",
            "description": "选择高分候选生成 9:16 MP4，进入标题、封面和人工终审。",
        }
    if "missing_transcript" in keys or "asr_repetition_noise" in keys or "whisper_cpp_base_no_vad" in keys:
        return {
            "kind": "rerun_asr",
            "label": "先处理 ASR",
            "description": "完成或重跑 ASR 后刷新候选、评分和质量 Gate。",
        }
    if "missing_candidates" in keys:
        return {
            "kind": "generate_candidates",
            "label": "先生成候选",
            "description": "完成候选切片生成后再进入评分和导出判断。",
        }
    if "missing_scores" in keys or "partial_missing_scores" in keys:
        return {
            "kind": "score_candidates",
            "label": "先运行评分",
            "description": "补齐评分后再按 Top 队列判断导出优先级。",
        }
    if status == "block":
        return {
            "kind": "open_review_queue",
            "label": "处理阻断项",
            "description": "优先处理授权、低原创或高风险质量原因，再刷新 Gate。",
        }
    return {
        "kind": "open_review_queue",
        "label": "打开复核队列",
        "description": "逐条检查字幕、上下文、授权和广告口播，通过后再导出预览。",
    }


def _gate_allowed_actions(status: str) -> list[str]:
    base = ["view_candidates", "view_quality_report"]
    if status == "allow":
        return [*base, "export_preview", "copy_metadata"]
    if status == "review":
        return [*base, "open_review_queue", "export_preview"]
    return [*base, "open_review_queue", "export_preview"]


def _gate_severity(status: str, reasons: list[dict]) -> str:
    if status == "block":
        return "risk"
    if any(reason.get("severity") == "risk" for reason in reasons):
        return "risk"
    if status == "review":
        return "warn"
    return "ok"


def _gate_actions(status: str, reasons: list[dict]) -> list[str]:
    if status == "allow":
        return ["质量 Gate 为放行提示，可继续进入标题/封面 A/B、导出预览和人工终审。"]
    actions: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        action = str(reason.get("action") or "").strip()
        if not action or action in seen:
            continue
        seen.add(action)
        actions.append(action)
    if status == "block":
        actions.append("处理阻断提示后刷新质量哨兵；当前 Gate 只提供只读决策提示，不会自动阻止导出。")
    else:
        actions.append("人工复核通过后再进入导出预览和推荐模拟联动判断。")
    return actions[:5]


def _watchlist(rows: list[dict]) -> list[dict]:
    items: list[dict] = []
    for row in rows:
        flags = _candidate_flags(row)
        if not flags:
            continue
        items.append(
            {
                "segment_id": row["id"],
                "time_range": {
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "duration_seconds": row["duration_seconds"],
                },
                "final_score": round(float(row.get("final_score") or 0), 2),
                "music_slice_type": row.get("music_slice_type") or "",
                "flags": flags,
                "evidence": _clip(str(row.get("transcript") or row.get("summary") or ""), 96),
            }
        )
    return items[:8]


def _simulation_linkage(video_id: str, rows: list[dict], issues: list[dict], health_score: int, top_k: int) -> dict:
    limit = min(max(1, int(top_k or 10)), 10)
    report = simulate_video(video_id, top_k=limit)
    simulations = list(report.get("simulations") or [])
    if not simulations:
        return {
            "available": False,
            "summary": {
                "decision_count": 0,
                "ready_to_export_count": 0,
                "review_count": 0,
                "optimization_count": 0,
                "wait_for_asr_count": 0,
                "quality_blocked_high_potential_count": 0,
                **(report.get("summary") or {}),
            },
            "decisions": [],
            "actions": ["先完成候选评分，再把质量哨兵和推荐模拟合并判断。"],
        }

    row_by_id = {row["id"]: row for row in rows}
    issue_keys = {issue.get("key") for issue in issues}
    global_asr_risk = bool(issue_keys & {"missing_transcript", "asr_repetition_noise"})
    decisions = [
        _simulation_decision(item, row_by_id.get(item.get("segment_id"), {}), global_asr_risk, health_score)
        for item in simulations
    ]
    decision_counts = Counter(item["decision"] for item in decisions)
    quality_blocked_high_potential = sum(
        1
        for item in decisions
        if item["simulated_score"] >= 72 and item["decision"] in {"review", "wait_for_asr"}
    )
    return {
        "available": True,
        "summary": {
            **(report.get("summary") or {}),
            "decision_count": len(decisions),
            "ready_to_export_count": decision_counts.get("export_preview", 0),
            "review_count": decision_counts.get("review", 0),
            "optimization_count": decision_counts.get("optimize_packaging", 0),
            "wait_for_asr_count": decision_counts.get("wait_for_asr", 0),
            "quality_blocked_high_potential_count": quality_blocked_high_potential,
        },
        "decisions": decisions,
        "actions": _simulation_actions(decision_counts, quality_blocked_high_potential),
    }


def _simulation_decision(item: dict, row: dict, global_asr_risk: bool, health_score: int) -> dict:
    score = float(item.get("simulated_score") or 0)
    flags = _candidate_flags(row) if row else []
    closed_loop = _closed_loop(row) if row else False
    bottleneck = item.get("bottleneck") if isinstance(item.get("bottleneck"), dict) else {}

    if global_asr_risk and score < 64:
        decision = {
            "decision": "wait_for_asr",
            "label": "等待 ASR 重跑",
            "severity": "risk",
            "reason": "推荐模拟偏弱且 ASR 风险未消除，当前分数可能被字幕噪声拖偏。",
            "action": "先用质量 profile 或 VAD 路径重跑 ASR，再刷新候选和模拟。",
        }
    elif score >= 72 and (flags or global_asr_risk or health_score < 82):
        decision = {
            "decision": "review",
            "label": "高潜但需复核",
            "severity": "warn",
            "reason": "推荐模拟分较高，但质量哨兵仍命中风险信号。",
            "action": "先打开候选详情人工复核字幕、上下文和风险提示，通过后再导出。",
        }
    elif score >= 72:
        decision = {
            "decision": "export_preview",
            "label": "优先导出预览",
            "severity": "ok",
            "reason": "推荐模拟分较高，且当前质量风险较低。",
            "action": "优先导出 9:16 预览，进入标题/封面 A/B 和人工终审。",
        }
    elif score < 64 and closed_loop:
        decision = {
            "decision": "optimize_packaging",
            "label": "包装二次优化",
            "severity": "info",
            "reason": "结构闭环完整，但推荐模拟分偏低，问题更可能在标题、封面或开头 hook。",
            "action": "保留片段，先强化前 3 秒、标题和封面表达再复测。",
        }
    else:
        decision = {
            "decision": "small_pool_test",
            "label": "小流量测试",
            "severity": "neutral",
            "reason": "质量与模拟信号未形成强结论，适合进入普通候选池观察。",
            "action": "放入小流量测试队列，等待表现数据回流校准。",
        }

    return {
        "segment_id": item.get("segment_id") or "",
        "title": item.get("title") or "",
        "simulated_score": round(score, 2),
        "final_score": round(float(item.get("final_score") or 0), 2),
        "predicted_stage": item.get("predicted_stage") or "",
        "bottleneck": {
            "key": bottleneck.get("key") or "",
            "label": bottleneck.get("label") or "",
            "score": round(float(bottleneck.get("score") or 0), 2),
        },
        "quality_flags": flags,
        **decision,
    }


def _simulation_actions(decision_counts: Counter[str], quality_blocked_high_potential: int) -> list[str]:
    actions: list[str] = []
    ready = decision_counts.get("export_preview", 0)
    review = decision_counts.get("review", 0)
    optimize = decision_counts.get("optimize_packaging", 0)
    wait_for_asr = decision_counts.get("wait_for_asr", 0)
    if ready:
        actions.append(f"优先导出 {ready} 条高潜且质量稳定的候选，作为人工终审样本。")
    if quality_blocked_high_potential:
        actions.append(f"{quality_blocked_high_potential} 条模拟高潜仍被质量风险拦下，先复核字幕、广告口播和上下文。")
    elif review:
        actions.append(f"{review} 条候选需要人工复核后再判断是否导出。")
    if wait_for_asr:
        actions.append(f"{wait_for_asr} 条候选建议等待 ASR 重跑后再评估推荐潜力。")
    if optimize:
        actions.append(f"{optimize} 条闭环片段适合先做标题、封面和前 3 秒包装优化。")
    return actions[:4] or ["推荐模拟和质量哨兵信号一致，可按普通候选池节奏推进。"]


def _candidate_flags(row: dict) -> list[str]:
    flags: list[str] = []
    if _candidate_sponsor_risk(row):
        flags.append("品牌/导流口播")
    if _weak_context(row):
        flags.append("上下文需确认")
    if _audio_only(row):
        flags.append("纯音频候选")
    if _duration_outlier(row):
        flags.append("时长异常")
    if float(row.get("low_originality_score") or 0) >= 45:
        flags.append("低原创风险")
    if float(row.get("rights_risk_score") or 0) >= 50:
        flags.append("授权风险")
    return flags


def _candidate_sponsor_risk(row: dict) -> bool:
    text = _row_text(row)
    return len(_sponsor_hits(text)) >= 1 or "广告口播" in text or "品牌" in text and "口播" in text


def _weak_context(row: dict) -> bool:
    text = _row_text(row)
    context = str(row.get("program_context") or "")
    return "需人工确认" in context or ("直入听觉爆点" in text and "节目上下文" not in text)


def _audio_only(row: dict) -> bool:
    text = str(row.get("transcript") or row.get("summary") or "")
    return "音乐/舞台高能候选片段" in text


def _duration_outlier(row: dict) -> bool:
    duration = float(row.get("duration_seconds") or 0)
    return duration < 12 or duration > 90


def _closed_loop(row: dict) -> bool:
    text = _row_text(row)
    return (
        ("节目上下文" in text or "导师" in text or "赛制" in text or "歌手故事" in text)
        and ("音乐爆点" in text or "歌曲爆点" in text or "舞台表现" in text)
        and ("现场反应" in text or "结果" in text or "评论触发" in text)
    )


def _row_text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "transcript",
            "summary",
            "program_context",
            "comment_trigger",
            "short_video_structure",
            "musical_moment",
            "music_slice_type",
        ]
    )


def _score_value(row: dict, key: str) -> float:
    if row.get(key) is None:
        return 0.0
    return float(row.get(key) or 0)


def _segment_signal(row: dict) -> dict:
    return {
        "index": row.get("index"),
        "start": row.get("start"),
        "end": row.get("end"),
        "text": _clip(str(row.get("text") or ""), 120),
    }


def _sponsor_hits(text: str) -> list[str]:
    lower = str(text or "").lower()
    return [term for term in SPONSOR_TERMS if term.lower() in lower]


def _has_repetition_noise(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?()（）]+", "", text or "")
    if len(compact) < 24:
        return False
    if re.search(r"(.{1,6})\1{5,}", compact):
        return True
    if _has_repeated_english_words(text):
        return True
    if _looks_like_english_prose_or_lyrics(text):
        return False
    counts = Counter(compact)
    unique_ratio = len(counts) / max(1, len(compact))
    max_char_ratio = max(counts.values()) / max(1, len(compact))
    return len(compact) >= 60 and (unique_ratio <= 0.18 or max_char_ratio >= 0.38)


def _has_repeated_english_words(text: str) -> bool:
    words = [word.lower() for word in re.findall(r"[A-Za-z][A-Za-z']*", str(text or ""))]
    if len(words) < 8:
        return False
    if max((words.count(word) for word in set(words)), default=0) >= 8:
        return True
    pairs = list(zip(words, words[1:]))
    return max((pairs.count(pair) for pair in set(pairs)), default=0) >= 5


def _looks_like_english_prose_or_lyrics(text: str) -> bool:
    value = str(text or "")
    letters = re.findall(r"[A-Za-z]", value)
    words = re.findall(r"[A-Za-z][A-Za-z']*", value)
    cjk = re.findall(r"[\u4e00-\u9fff]", value)
    if len(letters) < 40 or len(words) < 8:
        return False
    return len(letters) / max(1, len(value)) >= 0.45 and len(cjk) < 8


def _evidence(rows: list[dict]) -> str:
    if not rows:
        return ""
    first = rows[0]
    return f"{first.get('start', '-')}-{first.get('end', '-')}s: {first.get('text', '')}"


def _clip(text: str, length: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= length else value[: length - 1] + "..."
