from __future__ import annotations

import json
from typing import Any

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.utils import new_id, utc_now
from dso.versions import PLATFORM_SYNC_VERSION


FIELD_ALIASES = {
    "platform_item_id": ["platform_item_id", "item_id", "video_id", "aweme_id", "视频ID文本", "视频ID"],
    "platform_title": ["platform_title", "item_title", "video_title", "desc", "title", "标题", "normalized_title"],
    "platform_url": ["platform_url", "share_url", "video_url", "url", "视频URL"],
    "published_at": ["published_at", "publish_time", "create_time"],
    "views": ["views", "play_count", "view_count", "计数数值", "可见计数", "visible_count_number", "best_visible_count_number"],
    "impressions": ["impressions", "show_count", "impression_count", "exposure_count"],
    "avg_watch_seconds": ["avg_watch_seconds", "avg_play_duration", "average_play_time"],
    "avg_watch_ratio": ["avg_watch_ratio", "avg_play_ratio", "average_play_ratio"],
    "five_second_retention": ["five_second_retention", "five_s_retention", "retention_5s"],
    "completion_rate": ["completion_rate", "play_finish_rate", "finish_rate"],
    "rewatch_rate": ["rewatch_rate", "replay_rate"],
    "likes": ["likes", "like_count", "digg_count"],
    "comments": ["comments", "comment_count"],
    "favorites": ["favorites", "collect_count", "favorite_count"],
    "shares": ["shares", "share_count"],
    "follows": ["follows", "follow_count"],
    "negative_feedback": ["negative_feedback", "dislike_count", "not_interested_count"],
    "comment_quality_score": ["comment_quality_score", "comment_quality"],
}


def create_platform_mapping(payload: dict[str, Any]) -> dict:
    platform = _text(payload.get("platform")) or "douyin"
    platform_item_id = _clean_item_id(_first(payload, FIELD_ALIASES["platform_item_id"]))
    if not platform_item_id:
        raise ValueError("platform_item_id is required")
    with connect() as conn:
        experiment_id = _existing_id(conn, "publishing_experiments", _text(payload.get("experiment_id")))
        slice_variant_id = _existing_id(conn, "slice_variants", _text(payload.get("slice_variant_id")))
        candidate_segment_id = _existing_id(conn, "candidate_segments", _text(payload.get("candidate_segment_id")))
        if experiment_id and not slice_variant_id:
            row = fetch_one(conn, "SELECT slice_variant_id FROM publishing_experiments WHERE id = ?", [experiment_id])
            slice_variant_id = row["slice_variant_id"] if row else None
        if slice_variant_id and not candidate_segment_id:
            row = fetch_one(conn, "SELECT candidate_segment_id FROM slice_variants WHERE id = ?", [slice_variant_id])
            candidate_segment_id = row["candidate_segment_id"] if row else None
        now = utc_now()
        existing = fetch_one(
            conn,
            "SELECT * FROM platform_video_mappings WHERE platform = ? AND platform_item_id = ?",
            [platform, platform_item_id],
        )
        data = {
            "account_id": _text(payload.get("account_id")) or "main",
            "platform": platform,
            "platform_item_id": platform_item_id,
            "candidate_segment_id": candidate_segment_id,
            "slice_variant_id": slice_variant_id,
            "experiment_id": experiment_id,
            "platform_url": _text(_first(payload, FIELD_ALIASES["platform_url"])),
            "platform_title": _text(_first(payload, FIELD_ALIASES["platform_title"])),
            "published_at": _text(_first(payload, FIELD_ALIASES["published_at"])),
            "sync_status": _text(payload.get("sync_status")) or "linked",
            "last_synced_at": _text(payload.get("last_synced_at")),
            "last_metrics_at": _text(payload.get("last_metrics_at")),
            "notes": _text(payload.get("notes")),
            "updated_at": now,
        }
        if existing:
            data = {key: value for key, value in data.items() if value not in (None, "") or key in {"sync_status", "updated_at"}}
            assignments = ", ".join(f"{key} = ?" for key in data)
            conn.execute(
                f"UPDATE platform_video_mappings SET {assignments} WHERE id = ?",
                [*data.values(), existing["id"]],
            )
            mapping_id = existing["id"]
        else:
            data = {"id": new_id("pmap"), **data, "created_at": now}
            insert_row(conn, "platform_video_mappings", data)
            mapping_id = data["id"]
        conn.commit()
        return fetch_one(conn, "SELECT * FROM platform_video_mappings WHERE id = ?", [mapping_id])


def list_platform_mappings(
    account_id: str | None = None,
    platform: str | None = None,
    candidate_segment_id: str | None = None,
    slice_variant_id: str | None = None,
    experiment_id: str | None = None,
) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if candidate_segment_id:
        clauses.append("candidate_segment_id = ?")
        params.append(candidate_segment_id)
    if slice_variant_id:
        clauses.append("slice_variant_id = ?")
        params.append(slice_variant_id)
    if experiment_id:
        clauses.append("experiment_id = ?")
        params.append(experiment_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        return fetch_all(
            conn,
            f"SELECT * FROM platform_video_mappings{where} ORDER BY updated_at DESC",
            params,
        )


def resolve_platform_mapping(conn, raw: dict) -> dict[str, str | None]:
    item_id = _clean_item_id(_first(raw, FIELD_ALIASES["platform_item_id"]))
    if not item_id:
        return {"experiment_id": None, "slice_variant_id": None, "candidate_segment_id": None}
    platform = _text(raw.get("platform")) or "douyin"
    row = fetch_one(
        conn,
        "SELECT * FROM platform_video_mappings WHERE platform = ? AND platform_item_id = ?",
        [platform, item_id],
    )
    if not row:
        return {"experiment_id": None, "slice_variant_id": None, "candidate_segment_id": None}
    return {
        "experiment_id": row.get("experiment_id"),
        "slice_variant_id": row.get("slice_variant_id"),
        "candidate_segment_id": row.get("candidate_segment_id"),
    }


def upsert_platform_account(payload: dict[str, Any]) -> dict:
    platform = _text(payload.get("platform")) or "douyin"
    account_id = _text(payload.get("account_id")) or "main"
    now = utc_now()
    data = {
        "account_id": account_id,
        "platform": platform,
        "platform_account_id": _text(payload.get("platform_account_id") or payload.get("open_id") or payload.get("union_id")),
        "display_name": _text(payload.get("display_name") or payload.get("nickname")),
        "auth_status": _text(payload.get("auth_status")) or "mock_ready",
        "scopes": _json_text(payload.get("scopes")),
        "token_status": _text(payload.get("token_status")) or "not_stored",
        "token_expires_at": _text(payload.get("token_expires_at") or payload.get("expires_at")),
        "last_synced_at": _text(payload.get("last_synced_at")),
        "sync_cursor": _text(payload.get("sync_cursor")),
        "notes": _text(payload.get("notes")),
        "updated_at": now,
    }
    with connect() as conn:
        existing = fetch_one(
            conn,
            "SELECT id FROM platform_accounts WHERE platform = ? AND account_id = ?",
            [platform, account_id],
        )
        if existing:
            data = {key: value for key, value in data.items() if value not in (None, "") or key in {"auth_status", "updated_at"}}
            assignments = ", ".join(f"{key} = ?" for key in data)
            conn.execute(f"UPDATE platform_accounts SET {assignments} WHERE id = ?", [*data.values(), existing["id"]])
            account_row_id = existing["id"]
        else:
            data = {"id": new_id("pacc"), **data, "created_at": now}
            insert_row(conn, "platform_accounts", data)
            account_row_id = data["id"]
        conn.commit()
        return fetch_one(conn, "SELECT * FROM platform_accounts WHERE id = ?", [account_row_id])


def list_platform_accounts(account_id: str | None = None, platform: str | None = None) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        return fetch_all(conn, f"SELECT * FROM platform_accounts{where} ORDER BY updated_at DESC", params)


def update_platform_sync_state(account_id: str, platform: str, *, synced_at: str, cursor: str = "") -> None:
    with connect() as conn:
        account = fetch_one(conn, "SELECT id FROM platform_accounts WHERE platform = ? AND account_id = ?", [platform, account_id])
        if account:
            conn.execute(
                "UPDATE platform_accounts SET last_synced_at = ?, sync_cursor = COALESCE(NULLIF(?, ''), sync_cursor), updated_at = ? WHERE id = ?",
                [synced_at, cursor, utc_now(), account["id"]],
            )
        conn.execute(
            "UPDATE platform_video_mappings SET last_synced_at = ?, updated_at = ? WHERE platform = ? AND account_id = ?",
            [synced_at, utc_now(), platform, account_id],
        )
        conn.commit()


def record_platform_sync_run(payload: dict[str, Any]) -> dict:
    now = utc_now()
    row = {
        "id": new_id("psync"),
        "account_id": _text(payload.get("account_id")) or "main",
        "platform": _text(payload.get("platform")) or "douyin",
        "source": _text(payload.get("source")) or "mock",
        "sync_mode": _text(payload.get("sync_mode")) or "manual",
        "status": _text(payload.get("status")) or "completed",
        "requested_windows": _json_text(payload.get("requested_windows")),
        "started_at": _text(payload.get("started_at")) or now,
        "finished_at": _text(payload.get("finished_at")) or now,
        "pulled_items": _int(payload.get("pulled_items")),
        "mapped_items": _int(payload.get("mapped_items")),
        "imported_metrics": _int(payload.get("imported_metrics")),
        "linked_rows": _int(payload.get("linked_rows")),
        "unlinked_rows": _int(payload.get("unlinked_rows")),
        "training_samples": _int(payload.get("training_samples")),
        "error": _text(payload.get("error")),
        "summary_json": json.dumps(payload.get("summary") or {}, ensure_ascii=False),
    }
    with connect() as conn:
        insert_row(conn, "platform_sync_runs", row)
        conn.commit()
        return fetch_one(conn, "SELECT * FROM platform_sync_runs WHERE id = ?", [row["id"]])


def list_platform_sync_runs(account_id: str | None = None, platform: str | None = None, limit: int = 20) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit or 20)))
    with connect() as conn:
        return fetch_all(conn, f"SELECT * FROM platform_sync_runs{where} ORDER BY started_at DESC LIMIT ?", params)


def map_platform_metric_row(raw: dict[str, Any], *, sample_source: str = "mock") -> dict:
    mapped = {
        "sample_source": sample_source,
        "platform": _text(raw.get("platform")) or "douyin",
    }
    for target, aliases in FIELD_ALIASES.items():
        value = _first(raw, aliases)
        if value not in (None, ""):
            if target == "platform_item_id":
                value = _clean_item_id(value)
            mapped[target] = value
    for key in ["window_name", "label_window", "hours_since_publish", "collected_at", "candidate_segment_id", "slice_variant_id", "experiment_id"]:
        if raw.get(key) not in (None, ""):
            mapped[key] = raw.get(key)
    return mapped


def platform_metric_contract() -> dict:
    return {
        "contract_version": PLATFORM_SYNC_VERSION,
        "platform": "douyin",
        "sample_sources": ["csv", "api", "mock"],
        "file_formats": ["csv", "xlsx", "json"],
        "field_aliases": FIELD_ALIASES,
        "window_names": ["6h", "24h", "72h", "7d", "30d", "final"],
        "mapping_keys": ["platform_item_id", "candidate_segment_id", "slice_variant_id", "experiment_id"],
        "auth_policy": "Only local read-only account status is stored by default; real access/refresh tokens are not persisted in this workspace.",
        "sync_policy": "Mock/API rows are mapped locally first; the default sync client never makes real platform requests.",
    }


def _existing_id(conn, table: str, value: str | None) -> str | None:
    if not value:
        return None
    row = fetch_one(conn, f"SELECT id FROM {table} WHERE id = ?", [value])
    return value if row else None


def _first(raw: dict, aliases: list[str]) -> Any:
    for key in aliases:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _clean_item_id(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    digits = "".join(char for char in text if char.isdigit())
    return digits if len(digits) >= 10 else text


def _int(value: Any) -> int:
    text = str(value or "0").strip().replace(",", "")
    multiplier = 1.0
    if text.endswith("万"):
        multiplier = 10000.0
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100000000.0
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def _json_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
