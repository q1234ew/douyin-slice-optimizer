from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.utils import new_id, read_json, utc_now, write_json
from dso.versions import (
    ARTIFACT_MANIFEST_VERSION,
    QUALITY_GATE_VERSION,
    SCORER_VERSION,
    SEGMENTER_VERSION,
    component_versions,
)


def record_artifact(
    video_id: str,
    *,
    step: str,
    artifact_type: str,
    artifact_path: str | Path = "",
    version: str = "",
    status: str = "ready",
    summary: dict[str, Any] | None = None,
    error: str = "",
) -> dict:
    now = utc_now()
    row = {
        "id": new_id("artifact"),
        "video_id": video_id,
        "step": step,
        "artifact_type": artifact_type,
        "artifact_path": str(artifact_path or ""),
        "version": version,
        "status": status,
        "summary_json": json.dumps(summary or {}, ensure_ascii=False, sort_keys=True),
        "error": error,
        "created_at": now,
        "updated_at": now,
    }
    with connect() as conn:
        insert_row(conn, "pipeline_artifacts", row)
        conn.commit()
    return row


def write_artifact_json(
    video_id: str,
    *,
    step: str,
    filename: str,
    data: dict,
    artifact_type: str,
    version: str = "",
    summary: dict[str, Any] | None = None,
) -> dict:
    settings = ensure_data_dirs()
    path = settings.cache_dir / video_id / step / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    return record_artifact(
        video_id,
        step=step,
        artifact_type=artifact_type,
        artifact_path=path,
        version=version,
        status="ready",
        summary=summary,
    )


def video_manifest(video_id: str) -> dict:
    settings = ensure_data_dirs()
    with connect() as conn:
        video = fetch_one(conn, "SELECT * FROM source_videos WHERE id = ?", [video_id])
        if not video:
            raise KeyError(f"video not found: {video_id}")
        candidate_count = int(
            fetch_one(conn, "SELECT COUNT(*) AS count FROM candidate_segments WHERE source_video_id = ?", [video_id])[
                "count"
            ]
            or 0
        )
        scored_count = int(
            fetch_one(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM candidate_segments c
                JOIN slice_scores s ON s.candidate_segment_id = c.id
                WHERE c.source_video_id = ?
                """,
                [video_id],
            )["count"]
            or 0
        )
        exports = fetch_all(
            conn,
            """
            SELECT v.*
            FROM slice_variants v
            JOIN candidate_segments c ON c.id = v.candidate_segment_id
            WHERE c.source_video_id = ? AND v.export_path IS NOT NULL AND v.export_path != ''
            ORDER BY v.created_at DESC
            """,
            [video_id],
        )
        verifications = fetch_all(
            conn,
            "SELECT * FROM asr_verifications WHERE source_video_id = ? ORDER BY created_at DESC LIMIT 5",
            [video_id],
        )
        recorded = fetch_all(
            conn,
            "SELECT * FROM pipeline_artifacts WHERE video_id = ? ORDER BY updated_at DESC",
            [video_id],
        )

    latest = _latest_by_step(recorded)
    steps = [
        _transcript_step(video),
        _audio_step(settings.cache_dir / video_id / "audio" / "audio.wav", latest),
        _count_step("candidates", candidate_count, SEGMENTER_VERSION, latest),
        _count_step("scores", scored_count, SCORER_VERSION, latest),
        _quality_step(scored_count, latest),
        _exports_step(exports, latest),
        _verify_step(verifications, latest),
    ]
    ready = sum(1 for step in steps if step["status"] in {"ready", "partial"})
    return {
        "contract_version": ARTIFACT_MANIFEST_VERSION,
        "video_id": video_id,
        "generated_at": utc_now(),
        "component_versions": component_versions(),
        "status": "ready" if ready == len(steps) else ("partial" if ready else "empty"),
        "ready_steps": ready,
        "total_steps": len(steps),
        "completion_ratio": round(ready / len(steps), 3) if steps else 0,
        "next_action": _next_action(steps),
        "steps": steps,
        "recorded_artifacts": [_decode_summary(row) for row in recorded[:20]],
    }


def _latest_by_step(rows: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        latest.setdefault(row["step"], _decode_summary(row))
    return latest


def _decode_summary(row: dict) -> dict:
    row = dict(row)
    try:
        row["summary"] = json.loads(row.get("summary_json") or "{}")
    except Exception:
        row["summary"] = {}
    return row


def _transcript_step(video: dict) -> dict:
    path = video.get("transcript_path") or ""
    data = read_json(Path(path), default={}) if path else {}
    metadata = data.get("metadata") if isinstance(data, dict) else {}
    return {
        "step": "transcript",
        "label": "ASR transcript",
        "status": "ready" if path and Path(path).exists() else "missing",
        "artifact_path": path,
        "version": str((metadata or {}).get("postprocess_version") or ""),
        "summary": {
            "source": data.get("source") if isinstance(data, dict) else "",
            "segments": len(data.get("segments") or []) if isinstance(data, dict) else 0,
        },
    }


def _audio_step(path: Path, latest: dict[str, dict]) -> dict:
    recorded = latest.get("audio")
    return {
        "step": "audio",
        "label": "Audio features",
        "status": "ready" if path.exists() or recorded else "missing",
        "artifact_path": str(path if path.exists() else (recorded or {}).get("artifact_path", "")),
        "version": (recorded or {}).get("version", ""),
        "summary": (recorded or {}).get("summary", {}),
    }


def _count_step(step: str, count: int, version: str, latest: dict[str, dict]) -> dict:
    recorded = latest.get(step) or {}
    return {
        "step": step,
        "label": {"candidates": "Candidate segments", "scores": "Slice scores"}.get(step, step),
        "status": "ready" if count else "missing",
        "artifact_path": recorded.get("artifact_path", ""),
        "version": version,
        "summary": {**(recorded.get("summary") or {}), "count": count},
    }


def _quality_step(scored_count: int, latest: dict[str, dict]) -> dict:
    recorded = latest.get("quality") or {}
    if recorded:
        status = recorded.get("status") or "ready"
    elif scored_count:
        status = "available_on_request"
    else:
        status = "missing"
    return {
        "step": "quality",
        "label": "Quality gate",
        "status": status,
        "artifact_path": recorded.get("artifact_path", ""),
        "version": QUALITY_GATE_VERSION,
        "summary": recorded.get("summary") or {},
    }


def _exports_step(exports: list[dict], latest: dict[str, dict]) -> dict:
    recorded = latest.get("exports") or {}
    return {
        "step": "exports",
        "label": "Export previews",
        "status": "ready" if exports else "missing",
        "artifact_path": (exports[0].get("export_path") if exports else recorded.get("artifact_path", "")) or "",
        "version": recorded.get("version", ""),
        "summary": {**(recorded.get("summary") or {}), "count": len(exports)},
    }


def _verify_step(verifications: list[dict], latest: dict[str, dict]) -> dict:
    recorded = latest.get("asr_verify") or {}
    return {
        "step": "asr_verify",
        "label": "Top candidate ASR verify",
        "status": "ready" if verifications else "optional",
        "artifact_path": (verifications[0].get("artifact_path") if verifications else recorded.get("artifact_path", "")) or "",
        "version": recorded.get("version", ""),
        "summary": {
            **(recorded.get("summary") or {}),
            "count": len(verifications),
            "latest_difference_score": verifications[0].get("difference_score") if verifications else None,
        },
    }


def _next_action(steps: list[dict]) -> dict:
    for step in steps:
        if step["status"] == "missing":
            return {
                "step": step["step"],
                "label": step["label"],
                "action": {
                    "transcript": "extract",
                    "audio": "extract",
                    "candidates": "generate_candidates",
                    "scores": "score_candidates",
                    "quality": "refresh_quality",
                    "exports": "export_preview",
                }.get(step["step"], "review"),
            }
    return {"step": "feedback", "label": "Import metrics", "action": "import_metrics"}
