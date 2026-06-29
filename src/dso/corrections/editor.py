from __future__ import annotations

from typing import Any

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.review import insert_change_event
from dso.scoring.scorer import score_segment
from dso.utils import new_id, utc_now


PERFORMANCE_TEXT_FIELDS = ["performer_name", "episode", "stage_type", "arrangement_notes", "rights_status"]
CANDIDATE_TEXT_FIELDS = [
    "transcript",
    "summary",
    "primary_topic",
    "song_section_type",
    "music_slice_type",
    "emotion_type",
    "short_video_structure",
    "musical_moment",
    "program_context",
    "comment_trigger",
    "status",
]


def list_performances(video_id: str) -> list[dict]:
    with connect() as conn:
        if not fetch_one(conn, "SELECT id FROM source_videos WHERE id = ?", [video_id]):
            raise KeyError(f"video not found: {video_id}")
        return fetch_all(conn, _PERFORMANCE_SELECT + " WHERE p.source_video_id = ? ORDER BY p.start_time, p.created_at", [video_id])


def create_performance(video_id: str, payload: dict[str, Any]) -> dict:
    with connect() as conn:
        video = fetch_one(conn, "SELECT * FROM source_videos WHERE id = ?", [video_id])
        if not video:
            raise KeyError(f"video not found: {video_id}")
        start = _time_value(payload.get("start_time"), default=0)
        end = _time_value(payload.get("end_time"), default=0)
        _validate_time_range(start, end, float(video.get("duration_seconds") or 0))
        song_id = _song_id(conn, payload)
        now = utc_now()
        row = {
            "id": new_id("perf"),
            "source_video_id": video_id,
            "song_id": song_id,
            "performer_name": _text(payload.get("performer_name")),
            "episode": _text(payload.get("episode")),
            "start_time": start,
            "end_time": end,
            "stage_type": _text(payload.get("stage_type")),
            "arrangement_notes": _text(payload.get("arrangement_notes")),
            "rights_status": _text(payload.get("rights_status")) or "sample",
            "created_at": now,
        }
        insert_row(conn, "performances", row)
        conn.commit()
        return _get_performance(conn, row["id"])


def update_performance(performance_id: str, payload: dict[str, Any]) -> dict:
    with connect() as conn:
        current = fetch_one(conn, "SELECT * FROM performances WHERE id = ?", [performance_id])
        if not current:
            raise KeyError(f"performance not found: {performance_id}")
        video = fetch_one(conn, "SELECT * FROM source_videos WHERE id = ?", [current["source_video_id"]])
        if not video:
            raise KeyError(f"video not found: {current['source_video_id']}")
        start = _time_value(payload.get("start_time"), default=current["start_time"])
        end = _time_value(payload.get("end_time"), default=current["end_time"])
        _validate_time_range(start, end, float(video.get("duration_seconds") or 0))
        updates: dict[str, Any] = {
            "song_id": _song_id(conn, payload, default=current.get("song_id")),
            "start_time": start,
            "end_time": end,
        }
        for field in PERFORMANCE_TEXT_FIELDS:
            if field in payload:
                updates[field] = _text(payload.get(field))
        if not updates.get("rights_status"):
            updates["rights_status"] = current.get("rights_status") or "sample"
        reason = _text(payload.get("reason") or payload.get("review_reason"))
        operator = _text(payload.get("operator")) or "local"
        insert_change_event(
            conn,
            entity_type="performance",
            entity_id=performance_id,
            change_type="manual_correction",
            before={key: current.get(key) for key in updates},
            after=updates,
            reason=reason or "",
            operator=operator,
            source_video_id=current["source_video_id"],
        )
        _update_row(conn, "performances", performance_id, updates)
        conn.commit()
        return _get_performance(conn, performance_id)


def delete_performance(performance_id: str) -> dict:
    with connect() as conn:
        current = fetch_one(conn, "SELECT * FROM performances WHERE id = ?", [performance_id])
        if not current:
            raise KeyError(f"performance not found: {performance_id}")
        conn.execute("DELETE FROM performances WHERE id = ?", [performance_id])
        conn.commit()
        return {"deleted": True, "performance_id": performance_id}


def update_candidate_segment(segment_id: str, payload: dict[str, Any]) -> dict:
    with connect() as conn:
        current = fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [segment_id])
        if not current:
            raise KeyError(f"segment not found: {segment_id}")
        video = fetch_one(conn, "SELECT * FROM source_videos WHERE id = ?", [current["source_video_id"]])
        if not video:
            raise KeyError(f"video not found: {current['source_video_id']}")
        start = _time_value(payload.get("start_time"), default=current["start_time"])
        end = _time_value(payload.get("end_time"), default=current["end_time"])
        duration_limit = float(video.get("duration_seconds") or 0)
        _validate_time_range(start, end, duration_limit)
        duration = round(end - start, 3)
        cover_time = _time_value(payload.get("cover_time"), default=current.get("cover_time"))
        if cover_time is None or cover_time < start or cover_time > end:
            cover_time = round(start + duration * 0.45, 3)
        updates: dict[str, Any] = {
            "start_time": start,
            "end_time": end,
            "duration_seconds": duration,
            "cover_time": cover_time,
        }
        if "performance_id" in payload:
            updates["performance_id"] = _valid_performance_id(conn, current["source_video_id"], payload.get("performance_id"))
        for field in CANDIDATE_TEXT_FIELDS:
            if field in payload:
                updates[field] = _text(payload.get(field))
        if "status" not in updates or not updates["status"]:
            updates["status"] = "corrected"
        reason = _text(payload.get("reason") or payload.get("review_reason"))
        operator = _text(payload.get("operator")) or "local"
        insert_change_event(
            conn,
            entity_type="candidate_segment",
            entity_id=segment_id,
            change_type="manual_correction",
            before={key: current.get(key) for key in updates},
            after=updates,
            reason=reason or "",
            operator=operator,
            source_video_id=current["source_video_id"],
            candidate_segment_id=segment_id,
        )
        _update_row(conn, "candidate_segments", segment_id, updates)
        conn.commit()

    score_segment(segment_id)
    with connect() as conn:
        row = fetch_one(
            conn,
            """
            SELECT c.*, s.final_score, s.score_explanation, s.title_suggestions,
                   s.cover_suggestion, s.risk_notes, s.rights_risk_score
            FROM candidate_segments c
            LEFT JOIN slice_scores s ON s.candidate_segment_id = c.id
            WHERE c.id = ?
            """,
            [segment_id],
        )
    if not row:
        raise KeyError(f"segment not found after update: {segment_id}")
    return row


def _song_id(conn, payload: dict[str, Any], default: str | None = None) -> str | None:
    if "song_id" in payload and _text(payload.get("song_id")):
        song = fetch_one(conn, "SELECT id FROM songs WHERE id = ?", [_text(payload.get("song_id"))])
        if not song:
            raise ValueError("song_id does not exist")
        return song["id"]
    if "song_title" in payload:
        title = _text(payload.get("song_title"))
        if not title:
            return None
        existing = fetch_one(conn, "SELECT * FROM songs WHERE lower(title) = lower(?) LIMIT 1", [title])
        artist = _text(payload.get("original_artist") or payload.get("performer_name"))
        if existing:
            if artist and not existing.get("original_artist"):
                conn.execute("UPDATE songs SET original_artist = ? WHERE id = ?", [artist, existing["id"]])
            return existing["id"]
        row = {
            "id": new_id("song"),
            "title": title,
            "original_artist": artist,
            "composer": _text(payload.get("composer")),
            "lyricist": _text(payload.get("lyricist")),
            "is_original_for_program": 1 if payload.get("is_original_for_program") else 0,
            "recognition_level": _text(payload.get("recognition_level")) or "unknown",
            "rights_status": _text(payload.get("song_rights_status")) or "sample",
            "created_at": utc_now(),
        }
        insert_row(conn, "songs", row)
        return row["id"]
    return default


def _valid_performance_id(conn, video_id: str, value: Any) -> str | None:
    performance_id = _text(value)
    if not performance_id:
        return None
    row = fetch_one(conn, "SELECT id FROM performances WHERE id = ? AND source_video_id = ?", [performance_id, video_id])
    if not row:
        raise ValueError("performance_id must belong to the same source video")
    return row["id"]


def _get_performance(conn, performance_id: str) -> dict:
    row = fetch_one(conn, _PERFORMANCE_SELECT + " WHERE p.id = ?", [performance_id])
    if not row:
        raise KeyError(f"performance not found: {performance_id}")
    return row


def _update_row(conn, table: str, row_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [*updates.values(), row_id])


def _validate_time_range(start: float, end: float, duration_limit: float) -> None:
    if start < 0:
        raise ValueError("start_time must be >= 0")
    if end <= start:
        raise ValueError("end_time must be greater than start_time")
    if duration_limit > 0 and end > duration_limit + 0.25:
        raise ValueError("end_time exceeds source video duration")


def _time_value(value: Any, *, default: Any = None) -> float | None:
    if value is None or value == "":
        if default is None or default == "":
            return None
        value = default
    try:
        return round(float(value), 3)
    except (TypeError, ValueError) as exc:
        raise ValueError("time fields must be numeric seconds") from exc


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_PERFORMANCE_SELECT = """
SELECT p.*, s.title AS song_title, s.original_artist, s.recognition_level
FROM performances p
LEFT JOIN songs s ON s.id = p.song_id
"""
