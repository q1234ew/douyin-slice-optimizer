from __future__ import annotations

import json
from sqlite3 import Connection
from typing import Any

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.utils import new_id, utc_now
from dso.versions import CHANGE_LOG_VERSION, REVIEW_CONTRACT_VERSION


REVIEW_STATUSES = {"candidate", "review", "approved", "blocked", "exported"}
REVIEW_ALIASES = {
    "needs_review": "review",
    "need_review": "review",
    "corrected": "review",
    "ready": "approved",
    "rejected": "blocked",
}


def normalize_review_status(value: Any) -> str:
    status = str(value or "review").strip().lower()
    status = REVIEW_ALIASES.get(status, status)
    if status not in REVIEW_STATUSES:
        raise ValueError(f"review status must be one of {sorted(REVIEW_STATUSES)}")
    return status


def mark_candidate_review(
    segment_id: str,
    status: str,
    *,
    reason: str = "",
    operator: str = "local",
) -> dict:
    review_status = normalize_review_status(status)
    with connect() as conn:
        current = fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [segment_id])
        if not current:
            raise KeyError(f"segment not found: {segment_id}")
        previous = str(current.get("status") or "candidate")
        now = utc_now()
        clean_reason = _text(reason)
        clean_operator = _text(operator) or "local"
        latest = fetch_one(
            conn,
            """
            SELECT *
            FROM candidate_review_events
            WHERE candidate_segment_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [segment_id],
        )
        if previous == review_status and latest and str(latest.get("review_status") or "") == review_status and _text(latest.get("reason")) == clean_reason:
            return {
                "contract_version": REVIEW_CONTRACT_VERSION,
                "status": "unchanged",
                "segment_id": segment_id,
                "review_status": review_status,
                "previous_status": previous,
                "reason": clean_reason,
                "operator": clean_operator,
                "event": latest,
            }
        conn.execute("UPDATE candidate_segments SET status = ? WHERE id = ?", [review_status, segment_id])
        event = {
            "id": new_id("review"),
            "candidate_segment_id": segment_id,
            "previous_status": previous,
            "review_status": review_status,
            "reason": clean_reason,
            "operator": clean_operator,
            "created_at": now,
        }
        insert_row(conn, "candidate_review_events", event)
        insert_change_event(
            conn,
            entity_type="candidate_segment",
            entity_id=segment_id,
            change_type="review_status",
            before={"status": previous},
            after={"status": review_status},
            reason=reason,
            operator=operator,
            source_video_id=current["source_video_id"],
            candidate_segment_id=segment_id,
        )
        conn.commit()
    return {
        "contract_version": REVIEW_CONTRACT_VERSION,
        "status": "updated",
        "segment_id": segment_id,
        "review_status": review_status,
        "previous_status": previous,
        "reason": _text(reason),
        "operator": _text(operator) or "local",
        "event": event,
    }


def list_review_events(segment_id: str, limit: int = 20) -> dict:
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT *
            FROM candidate_review_events
            WHERE candidate_segment_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [segment_id, limit],
        )
    return {
        "contract_version": REVIEW_CONTRACT_VERSION,
        "segment_id": segment_id,
        "count": len(rows),
        "events": rows,
    }


def insert_change_event(
    conn: Connection,
    *,
    entity_type: str,
    entity_id: str,
    change_type: str,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str = "",
    operator: str = "local",
    source_video_id: str | None = None,
    candidate_segment_id: str | None = None,
) -> dict | None:
    diff = _diff(before, after)
    if not diff:
        return None
    row = {
        "id": new_id("change"),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_video_id": source_video_id,
        "candidate_segment_id": candidate_segment_id,
        "change_type": change_type,
        "reason": _text(reason),
        "operator": _text(operator) or "local",
        "diff_json": json.dumps(diff, ensure_ascii=False, sort_keys=True),
        "created_at": utc_now(),
    }
    insert_row(conn, "change_events", row)
    return row


def list_change_events(
    *,
    segment_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 30,
) -> dict:
    clauses = []
    params: list[Any] = []
    if segment_id:
        clauses.append("candidate_segment_id = ?")
        params.append(segment_id)
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT *
            FROM change_events
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        )
    for row in rows:
        try:
            row["diff"] = json.loads(row.get("diff_json") or "{}")
        except Exception:
            row["diff"] = {}
    return {
        "contract_version": CHANGE_LOG_VERSION,
        "count": len(rows),
        "changes": rows,
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if _canonical(old) == _canonical(new):
            continue
        result[key] = {"before": old, "after": new}
    return result


def _canonical(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    return "" if value is None else str(value)


def _text(value: Any) -> str:
    return str(value or "").strip()
