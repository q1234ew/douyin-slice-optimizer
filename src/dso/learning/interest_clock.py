from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from dso.db.session import connect, fetch_all, insert_row
from dso.feedback.reward import duration_bucket, parse_datetime, publish_hour
from dso.utils import new_id, utc_now
from dso.versions import INTEREST_CLOCK_VERSION


def build_interest_clock(account_id: str = "main") -> dict:
    rows = _sample_rows(account_id)
    source = "training_samples" if any((row.get("sample_source") or "") != "historical_capture_samples" for row in rows) else "historical_capture_samples"
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("music_slice_type") or "unknown", duration_bucket(row.get("duration_seconds")))
        groups.setdefault(key, []).append(row)
    suggestions = []
    for (content_type, bucket), group_rows in groups.items():
        for hour in range(24):
            score, confidence, sample_count = _smooth_hour(group_rows, hour)
            suggestions.append(
                {
                    "account_id": account_id,
                    "content_type": content_type,
                    "duration_bucket": bucket,
                    "publish_hour": hour,
                    "suggested_score": score,
                    "confidence": confidence,
                    "sample_count": sample_count,
                }
            )
    _store_suggestions(account_id, suggestions)
    ranked = sorted(suggestions, key=lambda row: (row["suggested_score"], row["confidence"], row["sample_count"]), reverse=True)
    status = "insufficient_history"
    if ranked:
        if source == "historical_capture_samples":
            status = "ready" if len(rows) >= 300 else ("low_confidence" if len(rows) >= 50 else "insufficient_history")
        else:
            status = "ready" if len(rows) >= 3 else "low_confidence"
    return {
        "contract_version": INTEREST_CLOCK_VERSION,
        "status": status,
        "account_id": account_id,
        "generated_at": utc_now(),
        "sample_source": source,
        "sample_count": len(rows),
        "group_count": len(groups),
        "suggestions": ranked[:24],
        "top_windows": ranked[:5],
    }


def recommend_publish_hours(
    account_id: str = "main",
    *,
    content_type: str | None = None,
    duration_seconds: float | None = None,
    limit: int = 5,
) -> dict:
    bucket = duration_bucket(duration_seconds) if duration_seconds else None
    clauses = ["account_id = ?"]
    params: list[Any] = [account_id]
    if content_type:
        clauses.append("content_type = ?")
        params.append(content_type)
    if bucket:
        clauses.append("duration_bucket = ?")
        params.append(bucket)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT * FROM interest_clock_suggestions
            WHERE {' AND '.join(clauses)}
            ORDER BY suggested_score DESC, confidence DESC, sample_count DESC
            LIMIT ?
            """,
            [*params, max(1, int(limit or 5))],
        )
    if not rows:
        build_interest_clock(account_id)
        with connect() as conn:
            rows = fetch_all(
                conn,
                f"""
                SELECT * FROM interest_clock_suggestions
                WHERE {' AND '.join(clauses)}
                ORDER BY suggested_score DESC, confidence DESC, sample_count DESC
                LIMIT ?
                """,
                [*params, max(1, int(limit or 5))],
            )
    status = "insufficient_history"
    if rows:
        status = "ready" if max(int(row.get("sample_count") or 0) for row in rows) >= 3 else "low_confidence"
    return {
        "contract_version": INTEREST_CLOCK_VERSION,
        "status": status,
        "account_id": account_id,
        "query": {"content_type": content_type or "all", "duration_bucket": bucket or "all", "limit": limit},
        "recommendations": rows,
    }


def _sample_rows(account_id: str) -> list[dict]:
    account = (account_id or "").strip()
    account_filter = None if not account or account.lower() == "all" else account
    params: list[Any] = []
    account_clause = ""
    if account_filter:
        account_clause = "AND v.account_id = ?"
        params.append(account_filter)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT ts.*, c.music_slice_type, c.duration_seconds, ms.collected_at, ms.hours_since_publish, e.published_at
            FROM training_samples ts
            JOIN candidate_segments c ON c.id = ts.candidate_segment_id
            JOIN source_videos v ON v.id = c.source_video_id
            JOIN metric_snapshots ms ON ms.id = ts.metric_snapshot_id
            LEFT JOIN publishing_experiments e ON e.id = ts.experiment_id
            WHERE 1 = 1
              {account_clause}
              AND ts.sample_source != 'mock'
            """,
            params,
        )
    return rows or _historical_sample_rows(account_filter)


def _historical_sample_rows(account_id: str | None) -> list[dict]:
    clauses = [
        "COALESCE(platform_item_id, '') != ''",
        "(COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0)",
    ]
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT id, reward_proxy, normalized_reward, content_category AS music_slice_type,
                   duration_seconds, collected_at, published_at, account_id
            FROM historical_capture_samples
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
            """,
            params,
        )
    for row in rows:
        row["sample_source"] = "historical_capture_samples"
        row["hours_since_publish"] = 0
    return rows


def _smooth_hour(rows: list[dict[str, Any]], hour: int) -> tuple[float, float, int]:
    weighted_sum = 0.0
    total_weight = 0.0
    nearby_count = 0
    for row in rows:
        sample_hour = _publish_hour_from_snapshot(row)
        if sample_hour < 0:
            continue
        distance = _hour_distance(hour, sample_hour)
        weight = math.exp(-((distance * distance) / (2 * 3.0 * 3.0)))
        weighted_sum += float(row.get("normalized_reward") or row.get("reward_proxy") or 0) * weight
        total_weight += weight
        if distance <= 3:
            nearby_count += 1
    if total_weight <= 0:
        return 0.0, 0.0, 0
    score = round(weighted_sum / total_weight, 4)
    confidence = round(min(1.0, math.sqrt(max(1, nearby_count)) / 4.0), 4)
    return score, confidence, nearby_count


def _store_suggestions(account_id: str, suggestions: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM interest_clock_suggestions WHERE account_id = ? AND version = ?", [account_id, INTEREST_CLOCK_VERSION])
        for item in suggestions:
            insert_row(
                conn,
                "interest_clock_suggestions",
                {
                    "id": new_id("clock"),
                    **item,
                    "version": INTEREST_CLOCK_VERSION,
                    "updated_at": utc_now(),
                },
            )
        conn.commit()


def _hour_distance(left: int, right: int) -> int:
    distance = abs(int(left) - int(right))
    return min(distance, 24 - distance)


def _publish_hour_from_snapshot(row: dict[str, Any]) -> int:
    direct = publish_hour(row.get("published_at"))
    if direct >= 0:
        return direct
    collected = parse_datetime(row.get("collected_at"))
    if not collected:
        return -1
    try:
        hours = float(row.get("hours_since_publish") or 0)
    except (TypeError, ValueError):
        hours = 0
    if hours > 0:
        return (collected - timedelta(hours=hours)).hour
    return collected.hour
