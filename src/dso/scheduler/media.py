from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.utils import read_json, utc_now, write_json


MEDIA_WINDOW_CONTRACT_VERSION = "media_window.v1"


def register_prepared_media(
    *,
    source_content_key: str,
    profile: str,
    artifacts: list[Path],
    start_seconds: float | None = None,
    duration_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register reusable CPU/IO preparation under a content-addressed manifest."""

    existing = [Path(path) for path in artifacts if Path(path).is_file()]
    if not existing:
        raise FileNotFoundError("prepared media has no readable artifacts")
    artifact_facts = [
        {
            "path": str(path),
            "size": int(path.stat().st_size),
            "sha256": _file_sha256(path),
        }
        for path in existing
    ]
    key_payload = {
        "contract_version": MEDIA_WINDOW_CONTRACT_VERSION,
        "source_content_key": str(source_content_key),
        "profile": str(profile),
        "start_seconds": None if start_seconds is None else round(float(start_seconds), 3),
        "duration_seconds": None if duration_seconds is None else round(float(duration_seconds), 3),
        "artifacts": [{"size": item["size"], "sha256": item["sha256"]} for item in artifact_facts],
    }
    digest = hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path = ensure_data_dirs().cache_dir / "model_scheduler" / "media_windows" / digest[:2] / digest / "manifest.json"
    cached = read_json(manifest_path, default={}) or {}
    cache_hit = cached.get("content_hash") == digest and all(Path(str(item.get("path") or "")).is_file() for item in cached.get("artifacts") or [])
    if not cache_hit:
        write_json(
            manifest_path,
            {
                **key_payload,
                "content_hash": digest,
                "artifacts": artifact_facts,
                "metadata": metadata or {},
                "created_at": utc_now(),
            },
        )
    return {
        "contract_version": MEDIA_WINDOW_CONTRACT_VERSION,
        "content_hash": digest,
        "profile": profile,
        "manifest_path": str(manifest_path),
        "artifact_count": len(artifact_facts),
        "cache_hit": cache_hit,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
