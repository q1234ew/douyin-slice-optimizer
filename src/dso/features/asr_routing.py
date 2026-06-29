from __future__ import annotations

import os
import re
from typing import Any

from dso.features.asr_profile import ASR_PROFILE_MODELS, normalize_asr_profile, resolve_asr_model_size
from dso.versions import ASR_MODEL_ROUTING_VERSION


AUTO_PROFILE_ALIASES = {"auto", "route", "routed", "smart", "strategy"}
ASR_QUALITY_ISSUE_KEYS = {
    "asr_repetition_noise",
    "transcript_ad_reads",
    "whisper_cpp_base_no_vad",
}

COMPETITION_TERMS = [
    "排名",
    "名次",
    "晋级",
    "淘汰",
    "揭晓",
    "竞演",
    "赛制",
    "补位",
    "听审",
    "投票",
    "胜负",
    "结果",
]
KNOWN_PERSON_TERMS = [
    "陈楚生",
    "张韶涵",
    "范玮琪",
    "范范",
    "歌手",
    "导师",
    "评委",
    "主持人",
    "听审",
    "合唱官",
]
NARRATIVE_TERMS = [
    "第一次",
    "首次",
    "代表作",
    "妈妈",
    "梦想",
    "一路",
    "坚持",
    "压力",
    "离开舞台",
    "故事",
    "回忆",
    "选择",
    "突破",
]
SPONSOR_TERMS = [
    "合作伙伴",
    "超级合作伙伴",
    "提醒您",
    "销量第一",
    "怕上火",
    "扫码",
    "直播间",
    "王老吉",
    "白雀羚",
    "动感地带",
    "芒果卡",
    "酸酸乳",
    "vivo",
    "VIP",
]
ENGLISH_CONTEXT_TERMS = [
    "english",
    "singer",
    "song",
    "lyrics",
    "stage",
    "rankings",
    "creative",
    "fresh",
    "grace",
]

REASON_LABELS = {
    "manual_profile": "手动指定 ASR profile",
    "default_full_video_fast": "全片默认使用 fast/base 批量转写",
    "missing_transcript": "尚无 transcript，先完成基础 ASR",
    "base_quality_risk": "base 结果命中质量风险，建议重跑 quality/small",
    "asr_quality_risk": "质量哨兵命中 ASR 风险，建议候选级 verify",
    "long_chinese_narrative": "中文长叙事段，专名和上下文误识别风险更高",
    "person_name_dense": "人名/角色密集，适合高规格模型复核",
    "competition_context": "赛制/排名/晋级口播，关键词准确性影响切片判断",
    "sponsor_or_ad_context": "品牌/导流口播命中，导出前需复核字幕边界",
    "weak_context": "候选上下文不足，需人工或高规格 ASR 辅助确认",
    "english_music_context": "英文歌手/歌名/英文介绍场景，优先保留 quality/small 结果",
    "default_candidate_quality": "候选默认保持 quality/small 作为发布前字幕基线",
    "keep_current": "当前 ASR 信号稳定，保留现有结果",
}


def is_auto_asr_profile(profile: str | None) -> bool:
    return bool(profile and profile.strip().lower() in AUTO_PROFILE_ALIASES)


def route_video_asr(
    video: dict[str, Any] | None = None,
    *,
    transcript_summary: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
    requested_profile: str | None = None,
    model_size: str | None = None,
) -> dict[str, Any]:
    manual_profile = _manual_profile(requested_profile)
    if manual_profile:
        return _route(
            scope="video",
            decision="manual_profile",
            profile=manual_profile,
            model_size=model_size,
            reason_keys=["manual_profile"],
            signals={"requested_profile": requested_profile or ""},
            candidate_only=False,
        )

    summary = transcript_summary or {}
    issue_keys = _issue_keys(issues)
    source = str(summary.get("source") or "missing")
    current_profile = _current_profile(summary)
    current_model = _current_model(summary)
    quality_risk = bool(issue_keys & ASR_QUALITY_ISSUE_KEYS)
    missing = source == "missing" or not summary.get("path") or int(summary.get("segment_count") or 0) <= 0

    if missing:
        return _route(
            scope="video",
            decision="transcribe_full_video",
            profile="fast",
            model_size=model_size,
            reason_keys=["missing_transcript"],
            signals={"source": source, "status": (video or {}).get("status") or ""},
            candidate_only=False,
        )

    if quality_risk and (current_profile == "fast" or current_model in {"", "base"}):
        return _route(
            scope="video",
            decision="rerun_full_video_quality",
            profile="quality",
            model_size=model_size,
            reason_keys=["base_quality_risk"],
            signals={
                "source": source,
                "current_profile": current_profile,
                "current_model": current_model,
                "issue_keys": sorted(issue_keys),
            },
            candidate_only=False,
        )

    if quality_risk:
        return _route(
            scope="video",
            decision="verify_candidates",
            profile="verify",
            model_size=model_size,
            reason_keys=["asr_quality_risk"],
            signals={
                "source": source,
                "current_profile": current_profile,
                "current_model": current_model,
                "issue_keys": sorted(issue_keys),
            },
            candidate_only=True,
        )

    return _route(
        scope="video",
        decision="keep_current",
        profile=current_profile or "quality",
        model_size=model_size or current_model or None,
        reason_keys=["keep_current"],
        signals={"source": source, "current_profile": current_profile, "current_model": current_model},
        candidate_only=False,
    )


def route_candidate_asr(
    segment: dict[str, Any],
    *,
    transcript_summary: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
    requested_profile: str | None = None,
    model_size: str | None = None,
) -> dict[str, Any]:
    manual_profile = _manual_profile(requested_profile)
    signals = classify_candidate_asr_signals(segment, transcript_summary=transcript_summary, issues=issues)
    if manual_profile:
        return _route(
            scope="candidate",
            decision="manual_profile",
            profile=manual_profile,
            model_size=model_size,
            reason_keys=["manual_profile"],
            signals=signals | {"requested_profile": requested_profile or ""},
            candidate_only=True,
            preserve_quality_result=manual_profile == "verify",
        )

    if signals["english_music_context"]:
        return _route(
            scope="candidate",
            decision="keep_quality_for_english",
            profile="quality",
            model_size=model_size,
            reason_keys=["english_music_context"],
            signals=signals,
            candidate_only=True,
            preserve_quality_result=True,
        )

    reason_keys = [
        key
        for key in [
            "asr_quality_risk",
            "long_chinese_narrative",
            "person_name_dense",
            "competition_context",
            "sponsor_or_ad_context",
            "weak_context",
        ]
        if signals[key]
    ]
    if reason_keys:
        return _route(
            scope="candidate",
            decision="verify_candidate",
            profile="verify",
            model_size=model_size,
            reason_keys=reason_keys,
            signals=signals,
            candidate_only=True,
            preserve_quality_result=True,
        )

    return _route(
        scope="candidate",
        decision="keep_quality",
        profile="quality",
        model_size=model_size,
        reason_keys=["default_candidate_quality"],
        signals=signals,
        candidate_only=True,
        preserve_quality_result=True,
    )


def classify_candidate_asr_signals(
    segment: dict[str, Any],
    *,
    transcript_summary: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = _candidate_text(segment)
    compact = re.sub(r"\s+", "", text)
    cjk_count = sum(1 for char in compact if "\u4e00" <= char <= "\u9fff")
    ascii_words = re.findall(r"[A-Za-z][A-Za-z0-9'_-]*", text)
    english_word_count = len(ascii_words)
    duration = _safe_float(segment.get("duration_seconds"), 0.0)
    issue_keys = _issue_keys(issues)
    sponsor_hits = _term_hits(text, SPONSOR_TERMS)
    competition_hits = _term_hits(text, COMPETITION_TERMS)
    person_hits = _term_hits(text, KNOWN_PERSON_TERMS)
    narrative_hits = _term_hits(text, NARRATIVE_TERMS)
    english_terms = _term_hits(text.lower(), ENGLISH_CONTEXT_TERMS)
    english_ratio = english_word_count / max(1, english_word_count + cjk_count / 2)

    english_music_context = (
        english_word_count >= 3
        and (english_ratio >= 0.22 or cjk_count <= 42 or bool(english_terms))
    )
    long_chinese_narrative = cjk_count >= 90 or (duration >= 42 and cjk_count >= 45 and len(narrative_hits) >= 1)
    person_name_dense = len(person_hits) >= 3 or (len(person_hits) >= 2 and cjk_count >= 50)
    competition_context = len(competition_hits) >= 2
    weak_context = _has_weak_context(segment)
    asr_quality_risk = bool(issue_keys & ASR_QUALITY_ISSUE_KEYS)

    return {
        "text_chars": len(compact),
        "duration_seconds": round(duration, 3),
        "cjk_count": cjk_count,
        "english_word_count": english_word_count,
        "english_ratio": round(english_ratio, 3),
        "issue_keys": sorted(issue_keys),
        "current_profile": _current_profile(transcript_summary or {}),
        "current_model": _current_model(transcript_summary or {}),
        "sponsor_hits": sponsor_hits[:6],
        "competition_hits": competition_hits[:6],
        "person_hits": person_hits[:6],
        "narrative_hits": narrative_hits[:6],
        "english_terms": english_terms[:6],
        "asr_quality_risk": asr_quality_risk,
        "long_chinese_narrative": long_chinese_narrative,
        "person_name_dense": person_name_dense,
        "competition_context": competition_context,
        "sponsor_or_ad_context": len(sponsor_hits) >= 2,
        "weak_context": weak_context,
        "english_music_context": english_music_context,
    }


def asr_routing_plan() -> dict[str, Any]:
    return {
        "contract_version": ASR_MODEL_ROUTING_VERSION,
        "auto_profile_aliases": sorted(AUTO_PROFILE_ALIASES),
        "default_full_video_profile": "fast",
        "default_candidate_profile": "quality",
        "verify_profile": "verify",
        "preserve_quality_for_english": True,
        "rules": [
            {
                "key": "base_quality_risk",
                "scope": "video",
                "trigger": "base/full-video transcript 命中 ASR 重复、广告口播或未启用 VAD",
                "profile": "quality",
                "model": ASR_PROFILE_MODELS["quality"],
                "action": "重跑全片 quality，不覆盖人工已修正文本",
            },
            {
                "key": "candidate_verify",
                "scope": "candidate",
                "trigger": "中文长叙事、人名密集、赛制口播、ASR 质量风险或广告口播边界风险",
                "profile": "verify",
                "model": ASR_PROFILE_MODELS["verify"],
                "action": "只对 Top 候选做二次转写对比",
            },
            {
                "key": "english_quality_preserve",
                "scope": "candidate",
                "trigger": "英文歌手、英文歌名、英文介绍或英文歌词场景",
                "profile": "quality",
                "model": ASR_PROFILE_MODELS["quality"],
                "action": "保留 small 结果作为候选，不用 verify 自动覆盖",
            },
        ],
    }


def _route(
    *,
    scope: str,
    decision: str,
    profile: str,
    model_size: str | None,
    reason_keys: list[str],
    signals: dict[str, Any],
    candidate_only: bool,
    preserve_quality_result: bool = False,
) -> dict[str, Any]:
    resolved_profile = normalize_asr_profile(profile)
    resolved_model = model_size or resolve_asr_model_size(profile=resolved_profile)
    return {
        "contract_version": ASR_MODEL_ROUTING_VERSION,
        "enabled": os.getenv("DSO_ASR_ROUTING", "1").strip().lower() not in {"0", "false", "no", "off"},
        "scope": scope,
        "decision": decision,
        "recommended_profile": resolved_profile,
        "recommended_model": resolved_model,
        "reason_keys": reason_keys,
        "reasons": [REASON_LABELS.get(key, key) for key in reason_keys],
        "candidate_only": candidate_only,
        "preserve_quality_result": preserve_quality_result,
        "signals": signals,
    }


def _manual_profile(requested_profile: str | None) -> str | None:
    if not requested_profile or is_auto_asr_profile(requested_profile):
        return None
    return normalize_asr_profile(requested_profile)


def _issue_keys(issues: list[dict[str, Any]] | None) -> set[str]:
    return {str(issue.get("key") or "") for issue in issues or [] if issue.get("key")}


def _current_profile(summary: dict[str, Any]) -> str:
    profile = str(summary.get("profile") or "").strip()
    if profile:
        return normalize_asr_profile(profile)
    model = _current_model(summary)
    for profile_name, profile_model in ASR_PROFILE_MODELS.items():
        if model == profile_model:
            return profile_name
    return ""


def _current_model(summary: dict[str, Any]) -> str:
    return str(summary.get("model_size") or summary.get("whisper_cpp_model_name") or "").strip()


def _candidate_text(segment: dict[str, Any]) -> str:
    fields = [
        "transcript",
        "summary",
        "music_slice_type",
        "short_video_structure",
        "musical_moment",
        "program_context",
        "comment_trigger",
    ]
    return " ".join(str(segment.get(field) or "") for field in fields).strip()


def _term_hits(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    hits = []
    for term in terms:
        needle = term.lower()
        if needle and needle in lowered:
            hits.append(term)
    return hits


def _has_weak_context(segment: dict[str, Any]) -> bool:
    text = " ".join(
        str(segment.get(field) or "")
        for field in ["program_context", "short_video_structure", "comment_trigger", "summary"]
    )
    weak_terms = ["缺少", "不足", "不明确", "仅音乐", "纯音乐", "audio only", "audio-only"]
    return any(term in text.lower() for term in weak_terms)


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
