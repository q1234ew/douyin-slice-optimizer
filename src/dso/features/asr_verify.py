from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from dso.artifacts import record_artifact
from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.features.asr import transcribe_audio_file
from dso.features.asr_profile import resolve_asr_model_size
from dso.features.asr_routing import route_candidate_asr
from dso.media.ffmpeg import require_binary
from dso.media.ingest import get_video
from dso.utils import new_id, read_json, run_cmd, utc_now, write_json
from dso.versions import ASR_VERIFY_VERSION


def verify_candidate_asr(
    segment_id: str,
    *,
    asr_profile: str | None = "verify",
    model_size: str | None = None,
    backend: str | None = None,
    force: bool = False,
) -> dict:
    with connect() as conn:
        segment = fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [segment_id])
    if not segment:
        raise KeyError(f"segment not found: {segment_id}")

    routing = route_candidate_asr(segment, requested_profile=asr_profile or "verify", model_size=model_size)
    profile = routing["recommended_profile"]
    model = resolve_asr_model_size(model_size, profile=profile)
    routing = {**routing, "recommended_model": model}
    video = get_video(segment["source_video_id"])
    settings = ensure_data_dirs()
    verify_dir = settings.cache_dir / segment["source_video_id"] / "asr-verify" / segment_id
    verify_dir.mkdir(parents=True, exist_ok=True)
    audio_path = verify_dir / "segment.wav"
    verified_path = verify_dir / f"{profile}_{model}.json"
    artifact_path = verify_dir / "comparison.json"

    if force or not verified_path.exists():
        _extract_segment_audio(
            Path(video["file_path"]),
            audio_path,
            float(segment["start_time"]),
            float(segment["end_time"]),
        )
        result = transcribe_audio_file(
            audio_path,
            verify_dir,
            model_size=model,
            asr_profile=profile,
            backend=backend,
            routing_context=routing,
        )
        write_json(
            verified_path,
            {
                "candidate_segment_id": segment_id,
                "source_video_id": segment["source_video_id"],
                "profile": profile,
                "model_name": model,
                "source": result["source"],
                "segments": result["segments"],
                "metadata": result.get("metadata") or {},
                "routing": routing,
                "created_at": utc_now(),
            },
        )

    verified = read_json(verified_path, default={}) or {}
    verified_segments = list(verified.get("segments") or [])
    baseline_segments = _baseline_segments(video, segment)
    baseline_text = _join_text(baseline_segments) or str(segment.get("transcript") or "")
    verified_text = _join_text(verified_segments)
    difference_score = round(1 - SequenceMatcher(None, baseline_text, verified_text).ratio(), 4) if baseline_text or verified_text else 0
    status = "ready" if verified_segments else "empty"
    comparison = {
        "contract_version": ASR_VERIFY_VERSION,
        "status": status,
        "candidate_segment_id": segment_id,
        "source_video_id": segment["source_video_id"],
        "time_range": {
            "start_time": float(segment["start_time"]),
            "end_time": float(segment["end_time"]),
            "duration_seconds": float(segment["duration_seconds"]),
        },
        "profile": profile,
        "model_name": model,
        "backend": backend or "auto",
        "routing": verified.get("routing") or routing,
        "baseline": {
            "text": baseline_text,
            "segments": baseline_segments,
            "path": video.get("transcript_path") or "",
        },
        "verified": {
            "text": verified_text,
            "segments": verified_segments,
            "path": str(verified_path),
            "source": verified.get("source") or "",
            "metadata": verified.get("metadata") or {},
        },
        "difference_score": difference_score,
        "recommendation": _recommendation(difference_score, verified_text),
        "created_at": utc_now(),
    }
    write_json(artifact_path, comparison)
    row = {
        "id": new_id("asrver"),
        "candidate_segment_id": segment_id,
        "source_video_id": segment["source_video_id"],
        "profile": profile,
        "model_name": model,
        "backend": backend or "auto",
        "baseline_text": baseline_text,
        "verified_text": verified_text,
        "baseline_path": video.get("transcript_path") or "",
        "verified_path": str(verified_path),
        "artifact_path": str(artifact_path),
        "difference_score": difference_score,
        "status": status,
        "created_at": utc_now(),
    }
    with connect() as conn:
        insert_row(conn, "asr_verifications", row)
        conn.commit()
    record_artifact(
        segment["source_video_id"],
        step="asr_verify",
        artifact_type="asr_comparison",
        artifact_path=artifact_path,
        version=ASR_VERIFY_VERSION,
        status=status,
        summary={
            "candidate_segment_id": segment_id,
            "profile": profile,
            "model_name": model,
            "difference_score": difference_score,
        },
    )
    return {**comparison, "record": row}


def list_asr_verifications(segment_id: str, limit: int = 5) -> dict:
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT *
            FROM asr_verifications
            WHERE candidate_segment_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [segment_id, limit],
        )
    return {
        "contract_version": ASR_VERIFY_VERSION,
        "segment_id": segment_id,
        "count": len(rows),
        "verifications": rows,
    }


def latest_asr_verification(segment_id: str) -> dict | None:
    rows = list_asr_verifications(segment_id, limit=1)["verifications"]
    return rows[0] if rows else None


def _extract_segment_audio(video_path: Path, wav_path: Path, start_time: float, end_time: float) -> Path:
    require_binary("ffmpeg")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, end_time - start_time)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_time:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
    )
    return wav_path


def _baseline_segments(video: dict, segment: dict) -> list[dict]:
    path = video.get("transcript_path")
    if not path:
        return []
    data = read_json(Path(path), default={}) or {}
    rows = []
    start = float(segment["start_time"])
    end = float(segment["end_time"])
    for item in data.get("segments") or []:
        item_start = float(item.get("start") or 0)
        item_end = float(item.get("end") or item_start)
        if item_end < start or item_start > end:
            continue
        rows.append(
            {
                "start": max(start, item_start),
                "end": min(end, item_end),
                "text": str(item.get("text") or "").strip(),
            }
        )
    return rows


def _join_text(rows: list[dict]) -> str:
    return " ".join(str(row.get("text") or "").strip() for row in rows if row.get("text")).strip()


def _recommendation(difference_score: float, verified_text: str) -> str:
    if not verified_text:
        return "verify profile 未得到有效字幕，保留原 transcript，并检查模型/音频路径。"
    if difference_score >= 0.35:
        return "二次转写与原字幕差异较大，建议人工并排复核后再导出。"
    if difference_score >= 0.12:
        return "二次转写存在中等差异，导出前抽查关键人名、歌名和广告口播。"
    return "二次转写与原字幕基本一致，可作为发布前字幕可信度佐证。"
