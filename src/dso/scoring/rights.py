from __future__ import annotations

import os

from dso.db.session import connect, fetch_one, insert_row
from dso.utils import new_id, utc_now


CLEARED_STATUSES = {"cleared", "approved", "licensed", "authorized", "ok", "yes", "授权", "已授权"}
SAMPLE_MODES = {"trusted_sample", "sample", "disabled", "off", "ignore"}


def rights_mode() -> str:
    return os.getenv("DSO_RIGHTS_MODE", "trusted_sample").strip().lower() or "trusted_sample"


def set_rights(
    asset_type: str,
    asset_id: str,
    *,
    program: str,
    song: str,
    performance: str,
    artist: str,
    platforms: str = "douyin",
    duration: float | None = None,
    accounts: str = "",
    expiration_date: str | None = None,
    notes: str | None = None,
) -> dict:
    now = utc_now()
    row = {
        "id": new_id("rights"),
        "asset_type": asset_type,
        "asset_id": asset_id,
        "program_rights_status": program,
        "song_rights_status": song,
        "performance_rights_status": performance,
        "artist_portrait_status": artist,
        "platform_license_scope": platforms,
        "allowed_clip_duration": duration,
        "allowed_publish_accounts": accounts,
        "allowed_publish_platforms": platforms,
        "expiration_date": expiration_date,
        "notes": notes,
        "updated_at": now,
    }
    with connect() as conn:
        existing = fetch_one(
            conn,
            "SELECT id FROM rights_clearance WHERE asset_type = ? AND asset_id = ?",
            [asset_type, asset_id],
        )
        if existing:
            row["id"] = existing["id"]
            conn.execute(
                """
                UPDATE rights_clearance
                SET program_rights_status = ?, song_rights_status = ?,
                    performance_rights_status = ?, artist_portrait_status = ?,
                    platform_license_scope = ?, allowed_clip_duration = ?,
                    allowed_publish_accounts = ?, allowed_publish_platforms = ?,
                    expiration_date = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                [
                    program,
                    song,
                    performance,
                    artist,
                    platforms,
                    duration,
                    accounts,
                    platforms,
                    expiration_date,
                    notes,
                    now,
                    row["id"],
                ],
            )
        else:
            insert_row(conn, "rights_clearance", row)
        conn.commit()
    return row


def get_rights(asset_type: str, asset_id: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            "SELECT * FROM rights_clearance WHERE asset_type = ? AND asset_id = ?",
            [asset_type, asset_id],
        )


def rights_risk_for_segment(segment: dict) -> tuple[float, list[str], bool]:
    if rights_mode() in SAMPLE_MODES:
        return 0.0, ["合格 sample 数据：暂不做版权/授权拦截，片段可参与评分与导出"], True

    rights = get_rights("candidate_segment", segment["id"]) or get_rights(
        "source_video", segment["source_video_id"]
    )
    notes: list[str] = []
    if not rights:
        return 100.0, ["缺少授权记录，片段只允许分析，不允许导出"], False

    statuses = [
        rights["program_rights_status"],
        rights["song_rights_status"],
        rights["performance_rights_status"],
        rights["artist_portrait_status"],
    ]
    missing = [status for status in statuses if str(status).strip().lower() not in CLEARED_STATUSES]
    risk = 0.0
    if missing:
        risk += 70.0
        notes.append("节目/歌曲/表演者/肖像授权存在未通过状态")
    allowed_duration = rights.get("allowed_clip_duration")
    if allowed_duration is not None and segment["duration_seconds"] > float(allowed_duration):
        risk += 40.0
        notes.append(f"片段时长 {segment['duration_seconds']:.1f}s 超过授权允许 {float(allowed_duration):.1f}s")
    platforms = str(rights.get("allowed_publish_platforms") or rights.get("platform_license_scope") or "")
    if platforms and "douyin" not in platforms.lower() and "抖音" not in platforms:
        risk += 35.0
        notes.append("授权平台范围未包含抖音")
    cleared = risk < 50.0
    if not notes:
        notes.append("授权记录通过，可导出")
    return min(100.0, risk), notes, cleared
