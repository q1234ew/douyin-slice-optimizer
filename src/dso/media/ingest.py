from __future__ import annotations

import shutil
from pathlib import Path

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_one, insert_row
from dso.media.ffmpeg import probe_video
from dso.utils import new_id, utc_now


def ingest_video(path: str | Path, account_id: str, title: str) -> dict:
    settings = ensure_data_dirs()
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    metadata = probe_video(source)
    video_id = new_id("video")
    target_dir = settings.media_dir / video_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    shutil.copy2(source, target)
    now = utc_now()
    row = {
        "id": video_id,
        "account_id": account_id,
        "title": title,
        "original_path": str(source),
        "file_path": str(target),
        "duration_seconds": metadata["duration_seconds"],
        "width": metadata["width"],
        "height": metadata["height"],
        "fps": metadata["fps"],
        "audio_streams": metadata["audio_streams"],
        "status": "ingested",
        "transcript_path": None,
        "created_at": now,
        "updated_at": now,
    }
    with connect() as conn:
        insert_row(conn, "source_videos", row)
        conn.commit()
    return row


def get_video(video_id: str) -> dict:
    with connect() as conn:
        row = fetch_one(conn, "SELECT * FROM source_videos WHERE id = ?", [video_id])
    if not row:
        raise KeyError(f"video not found: {video_id}")
    return row


def list_videos() -> list[dict]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM source_videos ORDER BY created_at DESC").fetchall()
        ]
