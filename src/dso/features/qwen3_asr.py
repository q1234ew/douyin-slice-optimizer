from __future__ import annotations

import json
import math
import os
import re
import uuid
import wave
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_QWEN3_ASR_SERVICE_URL = "http://192.168.31.143:8002"
DEFAULT_QWEN3_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"
DEFAULT_QWEN3_ASR_CONTEXT = ""


class Qwen3ASRError(RuntimeError):
    pass


def qwen3_asr_service_url() -> str:
    return os.getenv("DSO_QWEN3_ASR_SERVICE_URL", DEFAULT_QWEN3_ASR_SERVICE_URL).rstrip("/")


def qwen3_asr_model() -> str:
    return os.getenv("DSO_QWEN3_ASR_MODEL", DEFAULT_QWEN3_ASR_MODEL).strip() or DEFAULT_QWEN3_ASR_MODEL


def qwen3_asr_health(*, timeout_seconds: float = 3.0) -> dict:
    try:
        return _json_request("GET", f"{qwen3_asr_service_url()}/health", timeout_seconds=timeout_seconds)
    except Qwen3ASRError as exc:
        return {"status": "unavailable", "error": str(exc)}


def qwen3_asr_ready() -> bool:
    health = qwen3_asr_health()
    model = health.get("model") if isinstance(health.get("model"), dict) else {}
    return health.get("status") == "ready" and bool(model.get("loaded"))


def qwen3_asr_cache_config() -> dict:
    return {
        "service_url": qwen3_asr_service_url(),
        "model": qwen3_asr_model(),
        "language": os.getenv("DSO_QWEN3_ASR_LANGUAGE", "Chinese"),
        "context": os.getenv("DSO_QWEN3_ASR_CONTEXT", DEFAULT_QWEN3_ASR_CONTEXT),
        "chunk_seconds": _float_env("DSO_QWEN3_ASR_CHUNK_SECONDS", 60.0, 20.0, 300.0),
        "overlap_seconds": _float_env("DSO_QWEN3_ASR_OVERLAP_SECONDS", 1.0, 0.0, 5.0),
        "boundary_search_seconds": _float_env("DSO_QWEN3_ASR_BOUNDARY_SEARCH_SECONDS", 5.0, 0.0, 10.0),
        "retry_enabled": _env_truthy("DSO_QWEN3_ASR_RETRY_ENABLED", True),
        "retry_chunk_seconds": _float_env("DSO_QWEN3_ASR_RETRY_CHUNK_SECONDS", 30.0, 10.0, 60.0),
        "retry_min_rms": _float_env("DSO_QWEN3_ASR_RETRY_MIN_RMS", 0.002, 0.0, 0.1),
        "retry_min_text_density": _float_env(
            "DSO_QWEN3_ASR_RETRY_MIN_TEXT_DENSITY",
            0.5,
            0.0,
            5.0,
        ),
        "retry_slow_seconds": _float_env("DSO_QWEN3_ASR_RETRY_SLOW_SECONDS", 12.0, 1.0, 120.0),
        "timestamps": _env_truthy("DSO_QWEN3_ASR_TIMESTAMPS", True),
    }


def transcribe_wav(audio_path: Path, work_dir: Path) -> tuple[list[dict], dict]:
    health = qwen3_asr_health(timeout_seconds=5.0)
    model_status = health.get("model") if isinstance(health.get("model"), dict) else {}
    if not model_status.get("loaded") and _env_truthy("DSO_QWEN3_ASR_REMOTE_LOAD", False):
        _json_request(
            "POST",
            f"{qwen3_asr_service_url()}/load",
            payload={"force": False},
            timeout_seconds=_float_env("DSO_QWEN3_ASR_LOAD_TIMEOUT_SECONDS", 900.0, 30.0, 1800.0),
        )
        health = qwen3_asr_health(timeout_seconds=5.0)
        model_status = health.get("model") if isinstance(health.get("model"), dict) else {}
    if not model_status.get("loaded"):
        raise Qwen3ASRError(
            f"Qwen3-ASR service is not loaded at {qwen3_asr_service_url()}: {health.get('status', 'unknown')}"
        )

    config = qwen3_asr_cache_config()
    chunk_dir = work_dir / "qwen3_asr_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    segments: list[dict] = []
    responses: list[dict] = []
    try:
        for chunk in _wav_chunks(
            audio_path,
            chunk_dir,
            chunk_seconds=float(config["chunk_seconds"]),
            overlap_seconds=float(config["overlap_seconds"]),
            boundary_search_seconds=float(config["boundary_search_seconds"]),
        ):
            response, diagnostics = _transcribe_chunk_with_recovery(chunk, config, chunk_dir)
            responses.append(
                {
                    "chunk_index": chunk["index"],
                    "start": chunk["start"],
                    "end": chunk["end"],
                    "elapsed_seconds": diagnostics.get("total_elapsed_seconds"),
                    "selected_elapsed_seconds": response.get("elapsed_seconds"),
                    "language": response.get("language"),
                    **diagnostics,
                }
            )
            remote_segments = response.get("segments") if isinstance(response.get("segments"), list) else []
            for remote in remote_segments:
                row = _offset_segment(remote, chunk)
                if row:
                    row["index"] = len(segments) + 1
                    segments.append(row)
            chunk["path"].unlink(missing_ok=True)
    finally:
        for path in chunk_dir.glob("*.wav"):
            path.unlink(missing_ok=True)
        try:
            chunk_dir.rmdir()
        except OSError:
            pass
    unresolved_count = sum(1 for response in responses if response.get("quality_status") == "unresolved")
    suspect_count = sum(1 for response in responses if response.get("quality_status") == "suspect")
    recovered_count = sum(1 for response in responses if response.get("quality_status") == "recovered")
    return segments, {
        "model": model_status.get("model_id") or qwen3_asr_model(),
        "aligner": model_status.get("aligner_id") or "",
        "service_url": qwen3_asr_service_url(),
        "chunk_count": len(responses),
        "chunks": responses,
        "quality_status": "degraded" if unresolved_count or suspect_count else "ready",
        "unresolved_chunk_count": unresolved_count,
        "suspect_chunk_count": suspect_count,
        "recovered_chunk_count": recovered_count,
        "config": config,
    }


def _transcribe_chunk_with_recovery(chunk: dict, config: dict, chunk_dir: Path) -> tuple[dict, dict]:
    configured_context = str(config.get("context") or "")
    activity = _wav_activity_metrics(chunk["path"])
    attempts: list[dict] = []

    selected = _upload_audio(
        chunk["path"],
        language=str(config["language"]),
        context=configured_context,
        return_time_stamps=bool(config["timestamps"]),
    )
    selected_context = configured_context
    selected_strategy = "initial"
    attempts.append(_attempt_summary(selected, "initial", configured_context))
    reason = _retry_reason(selected, chunk, selected_context, activity, config)
    recovery_reasons: list[str] = []

    if reason == "context_echo":
        recovery_reasons.append(reason)
        selected = _upload_audio(
            chunk["path"],
            language=str(config["language"]),
            context="",
            return_time_stamps=bool(config["timestamps"]),
        )
        selected_context = ""
        selected_strategy = "no_context_retry"
        attempts.append(_attempt_summary(selected, selected_strategy, selected_context))
        reason = _retry_reason(selected, chunk, selected_context, activity, config)

    retry_enabled = bool(config.get("retry_enabled", True))
    retry_seconds = min(float(config.get("retry_chunk_seconds") or 30.0), float(chunk["duration"]) / 2)
    can_split = retry_enabled and retry_seconds >= 10.0 and float(chunk["duration"]) >= retry_seconds * 1.5
    if reason in {"empty_active_audio", "sparse_active_audio"} and can_split:
        recovery_reasons.append(reason)
        recovered, split_attempts = _retry_chunk_as_smaller_windows(
            chunk,
            config,
            chunk_dir,
            retry_seconds=retry_seconds,
        )
        attempts.extend(split_attempts)
        if _prefer_recovery(selected, recovered, reason):
            selected = recovered
            selected_context = ""
            selected_strategy = "split_retry"
        reason = _retry_reason(selected, chunk, selected_context, activity, config)

    if reason in {"empty_active_audio", "context_echo"}:
        quality_status = "unresolved"
    elif reason == "sparse_active_audio":
        quality_status = "suspect"
    elif selected_strategy != "initial":
        quality_status = "recovered"
    else:
        quality_status = "ready"

    text = _response_text(selected)
    remote_segments = selected.get("segments") if isinstance(selected.get("segments"), list) else []
    total_elapsed = 0.0
    for attempt in attempts:
        try:
            total_elapsed += float(attempt.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            pass
    return selected, {
        "text_chars": len(_compact_text(text)),
        "segment_count": len(remote_segments),
        "text_chars_per_second": round(len(_compact_text(text)) / max(0.001, float(chunk["duration"])), 4),
        "audio_rms": activity["rms"],
        "audio_peak": activity["peak"],
        "quality_status": quality_status,
        "quality_reason": reason,
        "recovery_reasons": list(dict.fromkeys(recovery_reasons)),
        "selected_strategy": selected_strategy,
        "attempt_count": len(attempts),
        "total_elapsed_seconds": round(total_elapsed, 3),
        "attempts": attempts,
    }


def _retry_chunk_as_smaller_windows(
    chunk: dict,
    config: dict,
    chunk_dir: Path,
    *,
    retry_seconds: float,
) -> tuple[dict, list[dict]]:
    retry_dir = chunk_dir / f"retry-{int(chunk['index']):04d}"
    retry_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict] = []
    combined_segments: list[dict] = []
    elapsed = 0.0
    language = ""
    try:
        children = _wav_chunks(
            chunk["path"],
            retry_dir,
            chunk_seconds=retry_seconds,
            overlap_seconds=min(float(config.get("overlap_seconds") or 0.0), retry_seconds / 4),
            boundary_search_seconds=min(float(config.get("boundary_search_seconds") or 0.0), retry_seconds / 4),
        )
        for child in children:
            response = _upload_audio(
                child["path"],
                language=str(config["language"]),
                context="",
                return_time_stamps=bool(config["timestamps"]),
            )
            attempts.append(_attempt_summary(response, f"split_retry_{child['index']}", ""))
            try:
                elapsed += float(response.get("elapsed_seconds") or 0.0)
            except (TypeError, ValueError):
                pass
            language = str(response.get("language") or language)
            remote_segments = response.get("segments") if isinstance(response.get("segments"), list) else []
            for remote in remote_segments:
                row = _offset_segment(remote, child)
                if row:
                    row["index"] = len(combined_segments) + 1
                    combined_segments.append(row)
            child["path"].unlink(missing_ok=True)
    finally:
        for path in retry_dir.glob("*.wav"):
            path.unlink(missing_ok=True)
        try:
            retry_dir.rmdir()
        except OSError:
            pass
    return {
        "status": "ready" if combined_segments else "empty",
        "language": language,
        "text": "".join(str(segment.get("text") or "") for segment in combined_segments),
        "segments": combined_segments,
        "elapsed_seconds": round(elapsed, 3),
    }, attempts


def _attempt_summary(response: dict, strategy: str, context: str) -> dict:
    text = _response_text(response)
    segments = response.get("segments") if isinstance(response.get("segments"), list) else []
    return {
        "strategy": strategy,
        "status": str(response.get("status") or "unknown"),
        "elapsed_seconds": response.get("elapsed_seconds"),
        "text_chars": len(_compact_text(text)),
        "segment_count": len(segments),
        "context_echo": _is_context_echo(text, context),
    }


def _retry_reason(response: dict, chunk: dict, context: str, activity: dict, config: dict) -> str:
    text = _response_text(response)
    if _is_context_echo(text, context):
        return "context_echo"
    active = float(activity.get("rms") or 0.0) >= float(config.get("retry_min_rms") or 0.0)
    if not _compact_text(text):
        return "empty_active_audio" if active else ""
    duration = max(0.001, float(chunk.get("duration") or 0.0))
    density = len(_compact_text(text)) / duration
    retry_seconds = float(config.get("retry_chunk_seconds") or 30.0)
    try:
        elapsed = float(response.get("elapsed_seconds") or 0.0)
    except (TypeError, ValueError):
        elapsed = 0.0
    slow = elapsed >= float(config.get("retry_slow_seconds") or 12.0)
    if (
        active
        and slow
        and duration >= retry_seconds * 1.5
        and density < float(config.get("retry_min_text_density") or 0.0)
    ):
        return "sparse_active_audio"
    return ""


def _prefer_recovery(current: dict, recovered: dict, reason: str) -> bool:
    current_chars = len(_compact_text(_response_text(current)))
    recovered_chars = len(_compact_text(_response_text(recovered)))
    if recovered_chars <= 0:
        return False
    if reason in {"empty_active_audio", "context_echo"}:
        return True
    return recovered_chars >= max(current_chars + 8, math.ceil(current_chars * 1.25))


def _response_text(response: dict) -> str:
    text = str(response.get("text") or "").strip()
    if text:
        return text
    segments = response.get("segments") if isinstance(response.get("segments"), list) else []
    return "".join(str(segment.get("text") or "") for segment in segments).strip()


def _compact_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", text or "").lower()


def _is_context_echo(text: str, context: str) -> bool:
    compact_text = _compact_text(text)
    compact_context = _compact_text(context)
    if len(compact_text) < 8 or len(compact_context) < 8:
        return False
    length_ratio = len(compact_text) / len(compact_context)
    if not 0.65 <= length_ratio <= 1.35:
        return False
    return SequenceMatcher(None, compact_text, compact_context, autojunk=False).ratio() >= 0.88


def _wav_activity_metrics(audio_path: Path) -> dict[str, float]:
    with wave.open(str(audio_path), "rb") as source:
        channels = max(1, source.getnchannels())
        sample_width = source.getsampwidth()
        rate = max(1, source.getframerate())
        payload = source.readframes(source.getnframes())
    frame_width = channels * sample_width
    frame_count = len(payload) // max(1, frame_width)
    stride = max(1, rate // 2000)
    scale = float(1 << max(1, sample_width * 8 - 1))
    squared = 0.0
    peak = 0.0
    count = 0
    for frame_index in range(0, frame_count, stride):
        base = frame_index * frame_width
        for channel in range(channels):
            value = _pcm_value(payload, base + channel * sample_width, sample_width)
            normalized = abs(value) / scale
            squared += normalized * normalized
            peak = max(peak, normalized)
            count += 1
    rms = math.sqrt(squared / count) if count else 0.0
    return {"rms": round(rms, 6), "peak": round(peak, 6)}


def _pcm_value(payload: bytes, offset: int, sample_width: int) -> int:
    sample = payload[offset : offset + sample_width]
    if len(sample) != sample_width:
        return 0
    if sample_width == 1:
        return sample[0] - 128
    return int.from_bytes(sample, byteorder="little", signed=True)


def _wav_chunks(
    audio_path: Path,
    chunk_dir: Path,
    *,
    chunk_seconds: float,
    overlap_seconds: float,
    boundary_search_seconds: float = 0.0,
):
    chunk_dir.mkdir(parents=True, exist_ok=True)
    with wave.open(str(audio_path), "rb") as source:
        params = source.getparams()
        rate = source.getframerate()
        total_frames = source.getnframes()
        if rate <= 0:
            raise Qwen3ASRError("Invalid WAV sample rate")
        chunk_frames = max(1, int(chunk_seconds * rate))
        overlap_frames = min(chunk_frames - 1, max(0, int(overlap_seconds * rate)))
        start_frame = 0
        index = 0
        while start_frame < total_frames:
            remaining_frames = total_frames - start_frame
            if remaining_frames <= chunk_frames:
                end_frame = total_frames
            else:
                target_frame = start_frame + chunk_frames
                end_frame = _low_energy_boundary(
                    source,
                    target_frame=target_frame,
                    earliest_frame=max(
                        start_frame + overlap_frames + 1,
                        target_frame - int(boundary_search_seconds * rate),
                    ),
                    params=params,
                )
                end_frame = max(start_frame + overlap_frames + 1, min(target_frame, end_frame))
            source.setpos(start_frame)
            frames = source.readframes(end_frame - start_frame)
            if not frames:
                break
            frame_count = len(frames) // max(1, params.sampwidth * params.nchannels)
            chunk_path = chunk_dir / f"chunk-{index:04d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setparams(params)
                target.writeframes(frames)
            start = start_frame / rate
            end = (start_frame + frame_count) / rate
            yield {
                "index": index,
                "path": chunk_path,
                "start": round(start, 6),
                "end": round(end, 6),
                "duration": round(frame_count / rate, 6),
                "overlap": round(overlap_frames / rate, 6),
                "is_first": index == 0,
                "is_last": start_frame + frame_count >= total_frames,
            }
            if end_frame >= total_frames:
                break
            start_frame = max(start_frame + 1, end_frame - overlap_frames)
            index += 1


def _low_energy_boundary(source: wave.Wave_read, *, target_frame: int, earliest_frame: int, params) -> int:
    if earliest_frame >= target_frame:
        return target_frame
    channels = max(1, params.nchannels)
    sample_width = params.sampwidth
    rate = max(1, params.framerate)
    source.setpos(earliest_frame)
    payload = source.readframes(target_frame - earliest_frame)
    frame_width = channels * sample_width
    frame_count = len(payload) // max(1, frame_width)
    stride = max(1, rate // 1000)
    energies: list[float] = []
    positions: list[int] = []
    for frame_index in range(0, frame_count, stride):
        base = frame_index * frame_width
        energy = 0.0
        for channel in range(channels):
            energy += abs(_pcm_value(payload, base + channel * sample_width, sample_width))
        energies.append(energy / channels)
        positions.append(earliest_frame + frame_index)
    if not energies:
        return target_frame
    if max(energies) == min(energies):
        return target_frame
    prefix = [0.0]
    for energy in energies:
        prefix.append(prefix[-1] + energy)
    half_window = max(1, int(0.05 * rate / stride))
    best = target_frame
    best_key = (float("inf"), float("inf"))
    for index, position in enumerate(positions):
        left = max(0, index - half_window)
        right = min(len(energies), index + half_window + 1)
        mean_energy = (prefix[right] - prefix[left]) / max(1, right - left)
        key = (mean_energy, abs(target_frame - position))
        if key < best_key:
            best = position
            best_key = key
    return best


def _offset_segment(remote: dict, chunk: dict) -> dict | None:
    text = str(remote.get("text") or "").strip()
    if not text:
        return None
    try:
        local_start = max(0.0, float(remote.get("start") or 0.0))
        local_end = max(local_start + 0.03, float(remote.get("end") or local_start + 0.03))
    except (TypeError, ValueError):
        return None
    overlap = float(chunk.get("overlap") or 0.0)
    midpoint = (local_start + local_end) / 2
    if not chunk.get("is_first") and midpoint < overlap / 2:
        return None
    if not chunk.get("is_last") and midpoint >= float(chunk["duration"]) - overlap / 2:
        return None
    offset = float(chunk["start"])
    return {
        "start": round(offset + local_start, 3),
        "end": round(offset + local_end, 3),
        "text": text,
    }


def _upload_audio(audio_path: Path, *, language: str, context: str, return_time_stamps: bool) -> dict:
    boundary = f"----dso-qwen3-asr-{uuid.uuid4().hex}"
    parts = [
        _multipart_field(boundary, "language", language),
        _multipart_field(boundary, "context", context),
        _multipart_field(boundary, "return_time_stamps", "true" if return_time_stamps else "false"),
        _multipart_file(boundary, "audio", audio_path.name, audio_path.read_bytes(), "audio/wav"),
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    request = Request(
        f"{qwen3_asr_service_url()}/transcribe/file",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
    )
    return _open_json(
        request,
        timeout_seconds=_float_env("DSO_QWEN3_ASR_TIMEOUT_SECONDS", 900.0, 30.0, 3600.0),
    )


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


def _multipart_file(boundary: str, name: str, filename: str, content: bytes, content_type: str) -> bytes:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return header + content + b"\r\n"


def _json_request(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    timeout_seconds: float,
) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    return _open_json(Request(url, data=data, method=method, headers=headers), timeout_seconds=timeout_seconds)


def _open_json(request: Request, *, timeout_seconds: float) -> dict:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise Qwen3ASRError(f"Qwen3-ASR HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise Qwen3ASRError(f"Qwen3-ASR request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise Qwen3ASRError("Qwen3-ASR response is not an object")
    return data


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))
