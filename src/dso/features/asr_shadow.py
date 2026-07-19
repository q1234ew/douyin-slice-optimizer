from __future__ import annotations

import hashlib
from pathlib import Path

from dso.config import ensure_data_dirs
from dso.features.asr import POSTPROCESS_VERSION, transcribe_audio_file
from dso.features.asr_routing import qwen3_asr_shadow_policy
from dso.features.qwen3_asr import qwen3_asr_cache_config, qwen3_asr_health
from dso.media.ffmpeg import extract_audio
from dso.media.ingest import get_video
from dso.utils import read_json, utc_now, write_json
from dso.versions import QWEN3_ASR_SHADOW_VERSION


def qwen3_asr_shadow_status(video_id: str) -> dict:
    video = get_video(video_id)
    policy = qwen3_asr_shadow_policy(video)
    paths = _shadow_paths(video_id)
    status = read_json(paths["status"], default={}) or {}
    transcript = read_json(paths["transcript"], default={}) or {}
    baseline = _baseline_summary(video)
    health = qwen3_asr_health(timeout_seconds=3.0)
    model = health.get("model") if isinstance(health.get("model"), dict) else {}
    if transcript.get("segments"):
        effective_status = str(status.get("status") or "ready")
    elif not policy["eligible"]:
        effective_status = str(policy["status"])
    elif model.get("loaded"):
        effective_status = "ready_to_run"
    else:
        effective_status = "waiting_model_switch"
    return {
        "contract_version": QWEN3_ASR_SHADOW_VERSION,
        "video_id": video_id,
        "status": effective_status,
        "policy": policy,
        "baseline": baseline,
        "artifact": {
            "available": bool(transcript.get("segments")),
            "source": transcript.get("source") or "",
            "segment_count": len(transcript.get("segments") or []),
            "quality_status": (transcript.get("metadata") or {}).get("quality_status") or "",
            "created_at": transcript.get("created_at") or "",
            "cache_hit": bool(status.get("cache_hit")),
        },
        "service": {
            "status": health.get("status") or "unavailable",
            "model_loaded": bool(model.get("loaded")),
            "model_id": model.get("model_id") or "",
            "error": health.get("error") or model.get("last_error") or "",
        },
        "last_run": status,
    }


def run_qwen3_asr_shadow(video_id: str, *, force: bool = False) -> dict:
    video = get_video(video_id)
    policy = qwen3_asr_shadow_policy(video)
    paths = _shadow_paths(video_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    baseline = _baseline_summary(video)
    if not policy["eligible"]:
        result = _run_status(video_id, str(policy["status"]), policy, baseline, cache_hit=False)
        write_json(paths["status"], result)
        return result

    audio_path = paths["audio"]
    if not audio_path.is_file():
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        extract_audio(Path(video["file_path"]), audio_path)
    cache_key = {
        "contract_version": QWEN3_ASR_SHADOW_VERSION,
        "audio_sha256": _file_sha256(audio_path),
        "qwen3_asr": qwen3_asr_cache_config(),
        "postprocess_version": POSTPROCESS_VERSION,
    }
    existing = read_json(paths["transcript"], default={}) or {}
    if not force and (existing.get("metadata") or {}).get("cache_key") == cache_key and existing.get("segments"):
        result = _run_status(
            video_id,
            "ready",
            policy,
            baseline,
            cache_hit=True,
            transcript=existing,
            artifact_path=paths["transcript"],
        )
        write_json(paths["status"], result)
        return result

    health = qwen3_asr_health(timeout_seconds=5.0)
    model = health.get("model") if isinstance(health.get("model"), dict) else {}
    if not model.get("loaded"):
        result = _run_status(
            video_id,
            "waiting_model_switch",
            policy,
            baseline,
            cache_hit=False,
            error=str(health.get("error") or model.get("last_error") or "Qwen3-ASR model is not loaded"),
        )
        result["action"] = "Activate the Qwen3-ASR runtime, then rerun this Shadow job. The Whisper baseline is unchanged."
        write_json(paths["status"], result)
        return result

    result = transcribe_audio_file(
        audio_path,
        paths["root"],
        model_size="Qwen/Qwen3-ASR-1.7B",
        asr_profile="quality",
        backend="qwen3_asr",
        routing_context={
            "contract_version": QWEN3_ASR_SHADOW_VERSION,
            "scope": "full_program_shadow",
            "decision": "qwen3_asr_preferred_shadow",
            "candidate_only": False,
            "preserve_quality_result": True,
            "shadow": policy,
        },
    )
    segments = result.get("segments") or []
    qwen_run = read_json(paths["root"] / "qwen3_asr_last_run.json", default={}) or {}
    quality_status = str(qwen_run.get("status") or "ready")
    if not segments:
        failed = _run_status(
            video_id,
            "failed",
            policy,
            baseline,
            cache_hit=False,
            error=f"Qwen3-ASR returned no usable segments ({result.get('source') or 'unknown'}).",
        )
        write_json(paths["status"], failed)
        return failed

    transcript = {
        "contract_version": QWEN3_ASR_SHADOW_VERSION,
        "video_id": video_id,
        "role": "shadow",
        "source": result.get("source") or "qwen3_asr",
        "segments": segments,
        "metadata": {
            **(result.get("metadata") or {}),
            "cache_key": cache_key,
            "quality_status": quality_status,
            "active_transcript_preserved": True,
            "auto_promote": False,
            "baseline_source": baseline.get("source") or "missing",
        },
        "created_at": utc_now(),
    }
    write_json(paths["transcript"], transcript)
    final_status = "degraded" if quality_status != "ready" else "ready"
    completed = _run_status(
        video_id,
        final_status,
        policy,
        baseline,
        cache_hit=False,
        transcript=transcript,
        artifact_path=paths["transcript"],
    )
    write_json(paths["status"], completed)
    return completed


def _shadow_paths(video_id: str) -> dict[str, Path]:
    transcript_root = ensure_data_dirs().cache_dir / video_id / "transcript"
    root = transcript_root / "shadow" / "qwen3_asr"
    return {
        "root": root,
        "audio": transcript_root / "audio.wav",
        "transcript": root / "transcript.json",
        "status": root / "status.json",
    }


def _baseline_summary(video: dict) -> dict:
    path = Path(str(video.get("transcript_path") or "")) if video.get("transcript_path") else None
    payload = read_json(path, default={}) if path and path.is_file() else {}
    return {
        "available": bool((payload or {}).get("segments")),
        "source": (payload or {}).get("source") or "missing",
        "segment_count": len((payload or {}).get("segments") or []),
        "preserved": True,
    }


def _run_status(
    video_id: str,
    status: str,
    policy: dict,
    baseline: dict,
    *,
    cache_hit: bool,
    transcript: dict | None = None,
    artifact_path: Path | None = None,
    error: str = "",
) -> dict:
    transcript = transcript or {}
    return {
        "contract_version": QWEN3_ASR_SHADOW_VERSION,
        "video_id": video_id,
        "status": status,
        "role": "shadow",
        "preferred_backend": "qwen3_asr",
        "fallback_backend": "whisper_cpp",
        "cache_hit": cache_hit,
        "policy": policy,
        "baseline": baseline,
        "artifact_path": str(artifact_path) if artifact_path else "",
        "source": transcript.get("source") or "",
        "segment_count": len(transcript.get("segments") or []),
        "quality_status": (transcript.get("metadata") or {}).get("quality_status") or "",
        "auto_promote": False,
        "active_transcript_preserved": True,
        "error": error,
        "updated_at": utc_now(),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
