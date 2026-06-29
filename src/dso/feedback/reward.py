from __future__ import annotations

from datetime import datetime
from math import sqrt
from statistics import median
from typing import Iterable

from dso.utils import clamp


REWARD_COMPONENTS = [
    "play_conversion_rate",
    "play_conversion_score",
    "five_second_retention",
    "avg_watch_ratio",
    "completion_rate",
    "rewatch_rate",
    "engagement_rate",
    "engagement_score",
    "comment_quality_rate",
    "favorite_rate",
    "share_rate",
    "follow_rate",
    "follow_score",
    "related_content_continue_watch",
    "negative_feedback_rate",
    "negative_feedback_score",
    "rights_or_policy_risk",
]


def compute_reward_proxy(metrics: dict, *, rights_risk_score: float = 0.0) -> tuple[float, dict[str, float]]:
    views = max(1, int(metrics.get("views") or 0))
    impressions = int(metrics.get("impressions") or 0)
    rates = feedback_signal_rates(metrics)
    play_conversion_score = _scaled_ratio(rates["play_conversion_rate"], good_rate=0.35)
    if impressions <= 0 and views > 0:
        play_conversion_score = 0.5
    five_second = _ratio(metrics.get("five_second_retention"))
    avg_watch = _ratio(metrics.get("avg_watch_ratio"))
    completion = _ratio(metrics.get("completion_rate"))
    rewatch = _ratio(metrics.get("rewatch_rate"))
    engagement_score = _scaled_ratio(rates["engagement_rate"], good_rate=0.08)
    comment_quality = _comment_quality(metrics, views)
    favorite_rate = _scaled_rate(metrics.get("favorites"), views, good_rate=0.02)
    share_rate = _scaled_rate(metrics.get("shares"), views, good_rate=0.02)
    follow_score = _scaled_ratio(rates["follow_rate"], good_rate=0.01)
    negative_score = _scaled_ratio(rates["negative_feedback_rate"], good_rate=0.02)
    rights_risk = clamp(float(rights_risk_score or 0.0)) / 100.0

    components = {
        "play_conversion_rate": rates["play_conversion_rate"],
        "play_conversion_score": play_conversion_score,
        "five_second_retention": five_second,
        "avg_watch_ratio": avg_watch,
        "completion_rate": completion,
        "rewatch_rate": rewatch,
        "engagement_rate": rates["engagement_rate"],
        "engagement_score": engagement_score,
        "comment_quality_rate": comment_quality,
        "favorite_rate": favorite_rate,
        "share_rate": share_rate,
        "follow_rate": rates["follow_rate"],
        "follow_score": follow_score,
        "related_content_continue_watch": 0.0,
        "negative_feedback_rate": rates["negative_feedback_rate"],
        "negative_feedback_score": negative_score,
        "rights_or_policy_risk": rights_risk,
    }
    raw = (
        0.12 * play_conversion_score
        + 0.22 * five_second
        + 0.20 * avg_watch
        + 0.16 * completion
        + 0.08 * rewatch
        + 0.12 * engagement_score
        + 0.05 * follow_score
        + 0.05 * comment_quality
        + 0.05 * components["related_content_continue_watch"]
        - 0.20 * negative_score
        - 0.25 * rights_risk
    )
    return round(clamp(raw * 100), 4), components


def feedback_signal_rates(metrics: dict) -> dict[str, float]:
    views = max(0, _count(metrics.get("views")))
    impressions = max(0, _count(metrics.get("impressions")))
    view_denominator = max(1, views)
    interactions = sum(
        _count(metrics.get(field))
        for field in ("likes", "comments", "favorites", "shares")
    )
    play_conversion = (views / impressions) if impressions > 0 else 0.0
    return {
        "play_conversion_rate": round(clamp(play_conversion, 0, 1), 4),
        "five_second_retention": round(_ratio(metrics.get("five_second_retention")), 4),
        "completion_rate": round(_ratio(metrics.get("completion_rate")), 4),
        "avg_watch_ratio": round(_ratio(metrics.get("avg_watch_ratio")), 4),
        "engagement_rate": round(clamp(interactions / view_denominator, 0, 1), 4),
        "follow_rate": round(clamp(_count(metrics.get("follows")) / view_denominator, 0, 1), 4),
        "negative_feedback_rate": round(clamp(_count(metrics.get("negative_feedback")) / view_denominator, 0, 1), 4),
    }


def duration_bucket(duration_seconds: float | int | None) -> str:
    duration = float(duration_seconds or 0)
    if duration <= 0:
        return "unknown"
    if duration < 25:
        return "short"
    if duration < 45:
        return "medium"
    if duration < 90:
        return "long"
    return "extra_long"


def publish_hour(*values: str | None) -> int:
    for value in values:
        parsed = parse_datetime(value)
        if parsed:
            return parsed.hour
    return -1


def publish_time_bucket(*values: str | None) -> str:
    hour = publish_hour(*values)
    if hour < 0:
        return "unknown"
    if hour < 6:
        return "late_night_00_05"
    if hour < 12:
        return "morning_06_11"
    if hour < 18:
        return "afternoon_12_17"
    return "evening_18_23"


def infer_hook_type(row: dict) -> str:
    # The current model has no dedicated hook_type column, so we degrade to the
    # first structure leg and existing copy fields until generation stores it.
    structure = str(row.get("short_video_structure") or "")
    lead = structure.split("->", 1)[0].strip()
    if lead:
        inferred = _classify_hook_text(lead)
        if inferred != "unknown":
            return inferred
    text = " ".join(
        str(row.get(field) or "")
        for field in ("comment_trigger", "summary", "program_context", "transcript", "short_video_structure")
    )
    return _classify_hook_text(text)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def percentile(values: Iterable[float], q: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def baseline_stats(values: Iterable[float]) -> tuple[float, float, float, int]:
    data = [float(value) for value in values]
    if not data:
        return 0.0, 0.0, 0.0, 0
    return round(median(data), 4), round(percentile(data, 0.75), 4), round(percentile(data, 0.90), 4), len(data)


def normalize_against_baseline(value: float, *, median_value: float, p75_value: float, sample_count: int, impressions: int) -> tuple[float, float]:
    if sample_count < 3 or median_value <= 0:
        exposure_uncertainty = 0.35 if impressions >= 300 else 0.65
        return round(clamp(value), 4), exposure_uncertainty
    spread = max(5.0, p75_value - median_value)
    normalized = 50.0 + ((float(value) - median_value) / spread) * 25.0
    exposure_bonus = 0.35 if impressions < 300 else 0.0
    sample_uncertainty = min(0.6, 1.0 / sqrt(max(1, sample_count)))
    uncertainty = clamp(sample_uncertainty + exposure_bonus, 0, 1)
    return round(clamp(normalized), 4), round(uncertainty, 4)


def _ratio(value: object) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if number > 1:
        number = number / 100.0
    return clamp(number, 0, 1)


def _scaled_rate(value: object, views: int, *, good_rate: float) -> float:
    try:
        count = float(value or 0)
    except (TypeError, ValueError):
        count = 0.0
    return clamp((count / max(1, views)) / good_rate, 0, 1)


def _scaled_ratio(value: object, *, good_rate: float) -> float:
    return clamp(_ratio(value) / good_rate, 0, 1)


def _comment_quality(metrics: dict, views: int) -> float:
    explicit = metrics.get("comment_quality_score")
    if explicit not in (None, ""):
        return _ratio(explicit)
    return _scaled_rate(metrics.get("comments"), views, good_rate=0.02)


def _count(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _classify_hook_text(text: str) -> str:
    if not text:
        return "unknown"
    if any(token in text for token in ("节目上下文", "导师", "赛制", "点评", "节目")):
        return "program_context_hook"
    if any(token in text for token in ("副歌", "高音", "爆点", "强节奏", "climax", "高潮")):
        return "music_climax_hook"
    if any(token in text for token in ("现场反应", "观众", "欢呼", "哭", "泪", "惊艳")):
        return "reaction_hook"
    if any(token in text for token in ("讨论", "是否", "为什么", "?", "？", "评论")):
        return "comment_trigger_hook"
    if any(token in text for token in ("改编", "突破", "第一次", "反转", "悬念")):
        return "story_turn_hook"
    return "unknown"
