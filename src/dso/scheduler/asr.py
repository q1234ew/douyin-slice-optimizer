from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.features.asr import POSTPROCESS_VERSION, commit_scheduled_qwen3_asr
from dso.features.qwen3_asr import (
    prepare_qwen3_asr_chunks,
    qwen3_asr_cache_config,
    qwen3_asr_model,
    transcribe_prepared_qwen3_asr_chunk,
)
from dso.media.ffmpeg import extract_audio
from dso.media.ingest import get_video
from dso.scheduler.contracts import (
    MODEL_SCHEDULER_VERSION,
    PRIORITY_DEFAULTS,
    QWEN3_ASR_JOB_KIND,
    QWEN3_ASR_PROFILE_ID,
    JobItemSpec,
    ModelJobSpec,
    stable_json_hash,
)
from dso.scheduler.repository import ModelJobRepository
from dso.scheduler.media import register_prepared_media
from dso.utils import read_json, utc_now
from dso.versions import QWEN3_ASR_VERSION


class Qwen3ASRJobAdapter:
    def prepare(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        request = dict(item.get("request") or {})
        chunk = request.get("chunk") if isinstance(request.get("chunk"), dict) else {}
        path = Path(str(chunk.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if str(chunk.get("sha256") or "") != _file_sha256(path):
            raise RuntimeError("input_changed: ASR chunk changed before preparation")
        return {"status": "ready", "chunk_index": int(chunk.get("index") or 0), "prepared_media": chunk.get("prepared_media") or {}}

    def execute(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        request = dict(item.get("request") or {})
        summary = dict(job.get("request_summary") or {})
        work_dir = ensure_data_dirs().cache_dir / "model_scheduler" / "asr_recovery" / str(job["id"]) / str(item["id"])
        work_dir.mkdir(parents=True, exist_ok=True)
        return transcribe_prepared_qwen3_asr_chunk(
            dict(request.get("chunk") or {}),
            config=dict(summary.get("config") or {}),
            work_dir=work_dir,
        )

    def commit_item(self, job: dict[str, Any], item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
        return {
            "status": str(result.get("status") or "failed"),
            "chunk_index": int(result.get("chunk_index") or 0),
            "segment_count": len(result.get("segments") or []),
            "quality_status": str(diagnostics.get("quality_status") or ""),
        }

    def finalize(self, job: dict[str, Any], item_results: list[dict[str, Any]]) -> dict[str, Any]:
        summary = dict(job.get("request_summary") or {})
        return commit_scheduled_qwen3_asr(
            str(job["subject_id"]),
            Path(str(summary.get("audio_path") or "")),
            item_results,
            expected_audio_sha256=str(summary.get("audio_sha256") or ""),
            config=dict(summary.get("config") or {}),
            role=str(summary.get("role") or "primary"),
        )


def submit_qwen3_asr_job(
    video_id: str,
    *,
    force: bool = False,
    role: str = "primary",
    repository: ModelJobRepository | None = None,
) -> dict[str, Any]:
    if role not in {"primary", "shadow"}:
        raise ValueError("ASR role must be primary or shadow")
    video = get_video(video_id)
    settings = ensure_data_dirs()
    transcript_dir = settings.cache_dir / video_id / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    audio_path = transcript_dir / "audio.wav"
    if force or not audio_path.is_file():
        extract_audio(Path(str(video["file_path"])), audio_path)
    audio_sha = _file_sha256(audio_path)
    config = qwen3_asr_cache_config()
    config_hash = stable_json_hash({"config": config, "postprocess_version": POSTPROCESS_VERSION})
    chunk_dir = settings.cache_dir / "model_scheduler" / "asr_chunks" / audio_sha[:20] / config_hash[:16]
    chunks = prepare_qwen3_asr_chunks(audio_path, chunk_dir, config=config)
    for chunk in chunks:
        chunk["prepared_media"] = register_prepared_media(
            source_content_key=audio_sha,
            profile="qwen3_asr_pcm16k_mono_chunk.v1",
            artifacts=[Path(str(chunk["path"]))],
            start_seconds=float(chunk.get("start") or 0.0),
            duration_seconds=float(chunk.get("duration") or 0.0),
            metadata={"chunk_index": int(chunk.get("index") or 0)},
        )
    if not chunks:
        return {
            "contract_version": MODEL_SCHEDULER_VERSION,
            "status": "empty",
            "baseline": _asr_baseline(transcript_dir),
            "model_job": None,
            "reason": "audio_has_no_chunks",
        }
    input_hash = stable_json_hash(
        {
            "video_id": video_id,
            "audio_sha256": audio_sha,
            "config_hash": config_hash,
            "chunk_hashes": [chunk["sha256"] for chunk in chunks],
            "role": role,
        }
    )
    parameters_hash = stable_json_hash({"force": bool(force), "role": role, **({"force_nonce": utc_now()} if force else {})})
    dedupe_key = stable_json_hash(
        {
            "job_kind": QWEN3_ASR_JOB_KIND,
            "input_hash": input_hash,
            "profile": QWEN3_ASR_PROFILE_ID,
            "version": QWEN3_ASR_VERSION,
            "parameters_hash": parameters_hash,
        }
    )
    deadline_seconds = _int_env("DSO_MODEL_ASR_DEADLINE_SECONDS", 7200, 300, 43200)
    spec = ModelJobSpec(
        job_kind=QWEN3_ASR_JOB_KIND,
        subject_type="source_video",
        subject_id=video_id,
        account_id=str(video.get("account_id") or "main"),
        resource_class=str(os.environ.get("DSO_MODEL_RESOURCE_ID") or "gpu:0"),
        model_profile_id=QWEN3_ASR_PROFILE_ID,
        model_id=qwen3_asr_model(),
        model_version=QWEN3_ASR_VERSION,
        prompt_version="qwen3_asr_empty_context.v1",
        priority_class="product_batch",
        base_priority=PRIORITY_DEFAULTS["product_batch"],
        input_hash=input_hash,
        parameters_hash=parameters_hash,
        dedupe_key=dedupe_key,
        request_summary={
            "video_id": video_id,
            "role": role,
            "audio_path": str(audio_path),
            "audio_sha256": audio_sha,
            "config": config,
            "chunk_count": len(chunks),
            "duration_seconds": float(video.get("duration_seconds") or 0.0),
        },
        fallback_ref=_asr_baseline(transcript_dir),
        items=tuple(
            JobItemSpec(
                item_kind="qwen3_asr_chunk",
                item_role=f"chunk_{int(chunk['index']):04d}",
                input_hash=stable_json_hash({"audio_sha256": audio_sha, "chunk_sha256": chunk["sha256"], "config_hash": config_hash}),
                request={"chunk": chunk},
                estimated_units=float(chunk.get("duration") or 1.0),
                max_attempts=3,
            )
            for chunk in chunks
        ),
        max_attempts=3,
        deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)).isoformat(),
    )
    enqueued = (repository or ModelJobRepository()).enqueue(spec)
    return {
        "contract_version": MODEL_SCHEDULER_VERSION,
        "status": "cached" if enqueued.cache_hit else "accepted",
        "baseline": _asr_baseline(transcript_dir),
        "model_job": {**enqueued.job, "deduplicated": enqueued.deduplicated, "cache_hit": enqueued.cache_hit},
    }


def _asr_baseline(transcript_dir: Path) -> dict[str, Any]:
    transcript = read_json(transcript_dir / "transcript.json", default={}) or {}
    return {
        "status": "ready" if transcript.get("segments") else "missing",
        "source": str(transcript.get("source") or "existing_or_whisper"),
        "segment_count": len(transcript.get("segments") or []),
        "preserved": True,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))
