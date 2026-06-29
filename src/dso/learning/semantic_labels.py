from __future__ import annotations

import re
from typing import Any


SEMANTIC_ENUMS: dict[str, dict[str, str]] = {
    "content_category": {
        "unknown": "未知",
        "music_variety": "音乐综艺",
        "performance_clip": "舞台片段",
        "performance_highlight": "舞台高光",
        "judge_comment": "评委点评",
        "reaction": "反应/共鸣",
        "commentary": "解读评论",
        "behind_the_scenes": "幕后花絮",
        "behind_scene": "幕后花絮",
        "entertainment_news": "娱乐资讯",
        "humor_entertainment": "幽默娱乐",
        "sports_entertainment": "体育娱乐",
        "creative_ai": "AI 创作",
        "lifestyle": "生活方式",
        "drama_film": "影视剧情",
        "life_emotion": "生活情感",
        "compilation": "合集盘点",
        "commercial": "商业带货",
    },
    "hook_type": {
        "unknown": "未知",
        "high_note": "高音/爆点",
        "music_burst": "音乐爆发",
        "reaction": "反应触发",
        "celebrity_pairing": "艺人组合",
        "emotional_story": "情绪故事",
        "chorus": "合唱共鸣",
        "judge_comment": "点评钩子",
        "funny": "幽默反差",
        "remix_creation": "二创改编",
        "topical_hook": "热点话题",
        "expert_comment": "专业解读",
        "daily_moment": "日常瞬间",
        "ecommerce": "商业转化",
    },
    "slice_structure": {
        "unknown": "未知",
        "pure_highlight": "纯高光",
        "reaction_first": "反应先行",
        "setup_to_payoff": "铺垫到爆点",
        "chorus_first": "副歌先行",
        "quote_first": "金句先行",
        "climax_first": "高潮先行",
        "context_first": "上下文先行",
        "linear": "线性叙事",
    },
}


ALIASES: dict[str, dict[str, str]] = {
    "content_category": {
        "behind-scenes": "behind_the_scenes",
        "behind_scenes": "behind_the_scenes",
        "stage_clip": "performance_clip",
        "stage_highlight": "performance_highlight",
        "music_show": "music_variety",
        "music": "music_variety",
        "other": "unknown",
        "none": "unknown",
        "null": "unknown",
        "其他": "unknown",
        "其它": "unknown",
    },
    "hook_type": {
        "highnote": "high_note",
        "music_blast": "music_burst",
        "topic": "topical_hook",
        "other": "unknown",
        "none": "unknown",
        "null": "unknown",
        "其他": "unknown",
        "其它": "unknown",
    },
    "slice_structure": {
        "highlight": "pure_highlight",
        "setup-payoff": "setup_to_payoff",
        "setup_payoff": "setup_to_payoff",
        "other": "unknown",
        "none": "unknown",
        "null": "unknown",
        "其他": "unknown",
        "其它": "unknown",
    },
}


def semantic_label_catalog() -> dict[str, Any]:
    return {
        field: [{"value": value, "label": label} for value, label in values.items()]
        for field, values in SEMANTIC_ENUMS.items()
    }


def normalize_semantic_field(field: str, value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    if field not in SEMANTIC_ENUMS:
        return raw, ""
    if not raw:
        return "unknown", "missing"
    key = _slug(raw)
    mapped = (ALIASES.get(field) or {}).get(key, key)
    if mapped in SEMANTIC_ENUMS[field]:
        return mapped, "missing_or_unknown" if mapped == "unknown" and key != "unknown" else ""
    if raw in SEMANTIC_ENUMS[field]:
        return raw, ""
    return "unknown", f"unmapped:{raw[:40]}"


def normalize_semantic_labels(values: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    reasons = []
    for field in SEMANTIC_ENUMS:
        value, reason = normalize_semantic_field(field, values.get(field))
        normalized[field] = value
        if reason:
            reasons.append(f"{field}:{reason}")
    normalized["semantic_unknown_reason"] = "|".join(reasons)
    return normalized


def semantic_unknown_reason(values: dict[str, Any]) -> str:
    return str(normalize_semantic_labels(values).get("semantic_unknown_reason") or "")


def semantic_label_display(field: str, value: Any) -> str:
    key, _ = normalize_semantic_field(field, value)
    return (SEMANTIC_ENUMS.get(field) or {}).get(key, key)


def _slug(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "_", text.lower()).strip("_")
