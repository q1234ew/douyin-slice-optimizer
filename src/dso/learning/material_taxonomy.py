from __future__ import annotations

from typing import Any


MATERIAL_FORM_LABELS_ZH = {
    "unknown": "未知",
    "performance_clip": "舞台/演唱片段",
    "reaction": "反应/Reaction",
    "vocal_teaching": "声乐教学",
    "commentary": "解读评论",
    "compilation": "合集盘点",
    "entertainment_news": "娱乐资讯",
    "behind_the_scenes": "幕后花絮",
    "humor_entertainment": "幽默娱乐",
    "drama_film": "影视剧情",
    "life_emotion": "生活情感",
    "lifestyle": "生活方式",
    "creative_ai": "AI 创作",
    "commercial": "商业带货",
}
MATERIAL_FORM_TYPES = tuple(MATERIAL_FORM_LABELS_ZH)
MATERIAL_NON_FORM_TYPES = {"program_context"}
MATERIAL_TYPE_CANONICAL_MAP = {
    "performance_highlight": "performance_clip",
    "judge_comment": "commentary",
    "program_context": "unknown",
}
MATERIAL_TYPE_TAXONOMY_SCORES = {
    "exact": 1.0,
    "specific_match": 0.9,
    "coarse_match": 0.75,
    "canonical_match": 0.75,
    "mismatch": 0.0,
    "not_material_form": 0.0,
    "not_scored": 0.0,
}
MATERIAL_TAXONOMY_MATCH_RELATIONS = {
    "exact",
    "specific_match",
    "coarse_match",
    "canonical_match",
}


def known_material_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text and text not in {"unknown", "none", "null", "其他", "其它"} else ""


def canonical_material_type(value: Any) -> str:
    material = known_material_value(value)
    canonical = MATERIAL_TYPE_CANONICAL_MAP.get(material, material)
    return canonical if canonical in MATERIAL_FORM_TYPES and canonical != "unknown" else ""


def material_taxonomy_derivation(
    material_type: Any,
    *,
    program_context: Any = None,
) -> dict[str, Any]:
    raw_material = known_material_value(material_type)
    canonical = canonical_material_type(raw_material)
    context = str(program_context or "").strip()
    if raw_material == "program_context" and not context:
        context = "unknown"
    highlight_signal = "highlight" if raw_material == "performance_highlight" else "unknown"
    detail_signal = "judge_comment" if raw_material == "judge_comment" else highlight_signal
    if raw_material in MATERIAL_NON_FORM_TYPES:
        reason = "program_context_is_context_not_material_form"
    elif raw_material == "performance_highlight":
        reason = "performance_highlight_is_performance_clip_with_highlight_detail"
    elif raw_material == "judge_comment":
        reason = "judge_comment_is_commentary_with_judge_detail"
    elif canonical:
        reason = "stable_material_form"
    else:
        reason = "unknown_material_form"
    return {
        "raw_material_type": raw_material or "unknown",
        "canonical_material_type": canonical or "unknown",
        "highlight_signal": highlight_signal,
        "detail_signal": detail_signal,
        "program_context": context or "unknown",
        "is_material_form": bool(canonical),
        "derivation_reason": reason,
        "rewrites_source_label": False,
    }


def material_type_taxonomy_relation(expected: Any, predicted: Any) -> str:
    expected_value = known_material_value(expected)
    predicted_value = known_material_value(predicted)
    if not expected_value:
        return "not_scored"
    if expected_value in MATERIAL_NON_FORM_TYPES:
        return "not_material_form"
    if not predicted_value or predicted_value in MATERIAL_NON_FORM_TYPES:
        return "mismatch"
    if expected_value == predicted_value:
        return "exact"
    expected_canonical = canonical_material_type(expected_value)
    predicted_canonical = canonical_material_type(predicted_value)
    if not expected_canonical or expected_canonical != predicted_canonical:
        return "mismatch"
    if expected_value != expected_canonical and predicted_value == predicted_canonical:
        return "coarse_match"
    if expected_value == expected_canonical and predicted_value != predicted_canonical:
        return "specific_match"
    return "canonical_match"


def material_form_options() -> list[dict[str, str]]:
    return [{"value": value, "label_zh": label} for value, label in MATERIAL_FORM_LABELS_ZH.items()]
