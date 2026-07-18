from __future__ import annotations

import gc
import importlib.metadata
import inspect
import os
import re
import tempfile
import threading
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel


SERVICE_VERSION = "dso-qwen3-asr-service.v1"
DEFAULT_MODEL = "Qwen/Qwen3-ASR-1.7B"
DEFAULT_ALIGNER = "Qwen/Qwen3-ForcedAligner-0.6B"

app = FastAPI(title="DSO Qwen3 ASR Service", version=SERVICE_VERSION)
_model: Any | None = None
_load_error = ""
_loaded_at = 0.0
_inference_lock = threading.Lock()


class LoadRequest(BaseModel):
    force: bool = False


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _model_id() -> str:
    return os.getenv("QWEN3_ASR_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _aligner_id() -> str:
    if not _env_truthy("QWEN3_ASR_TIMESTAMPS", True):
        return ""
    return os.getenv("QWEN3_ASR_ALIGNER", DEFAULT_ALIGNER).strip()


def _device() -> str:
    return os.getenv("QWEN3_ASR_DEVICE", "cuda:0").strip() or "cuda:0"


def _aligner_device() -> str:
    return os.getenv("QWEN3_ASR_ALIGNER_DEVICE", _device()).strip() or _device()


def _dtype() -> torch.dtype:
    configured = os.getenv("QWEN3_ASR_DTYPE", "bfloat16").strip().lower()
    if configured in {"float16", "fp16", "half"}:
        return torch.float16
    if configured in {"float32", "fp32"}:
        return torch.float32
    return torch.bfloat16


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _gpu_status() -> dict[str, Any]:
    if not torch.cuda.is_available():
        return {"available": False}
    free_bytes, total_bytes = torch.cuda.mem_get_info(0)
    return {
        "available": True,
        "name": torch.cuda.get_device_name(0),
        "free_memory_gb": round(free_bytes / 1024**3, 3),
        "total_memory_gb": round(total_bytes / 1024**3, 3),
        "allocated_memory_gb": round(torch.cuda.memory_allocated(0) / 1024**3, 3),
    }


def _require_gpu_headroom() -> None:
    if not _device().startswith("cuda"):
        return
    if not torch.cuda.is_available():
        raise HTTPException(status_code=503, detail="CUDA is unavailable")
    free_bytes, _ = torch.cuda.mem_get_info(0)
    free_gb = free_bytes / 1024**3
    minimum = _float_env("QWEN3_ASR_MIN_FREE_GPU_GB", 10.0, 4.0, 15.0)
    if free_gb < minimum:
        raise HTTPException(
            status_code=409,
            detail={
                "status": "gpu_memory_busy",
                "free_memory_gb": round(free_gb, 3),
                "required_free_memory_gb": minimum,
                "action": "Stop or unload the Omni model before loading Qwen3-ASR.",
            },
        )


def _load_model(force: bool = False) -> dict[str, Any]:
    global _model, _load_error, _loaded_at
    with _inference_lock:
        if _model is not None and not force:
            return {"status": "already_loaded", "model": _model_id(), "aligner": _aligner_id()}
        if _model is not None:
            _unload_model_locked()
        _require_gpu_headroom()
        try:
            from qwen_asr import Qwen3ASRModel

            kwargs: dict[str, Any] = {
                "dtype": _dtype(),
                "device_map": _device(),
                "max_inference_batch_size": _int_env("QWEN3_ASR_MAX_BATCH", 1, 1, 32),
                "max_new_tokens": _int_env("QWEN3_ASR_MAX_NEW_TOKENS", 1024, 128, 8192),
            }
            attention = os.getenv("QWEN3_ASR_ATTN", "sdpa").strip()
            if attention:
                kwargs["attn_implementation"] = attention
            aligner = _aligner_id()
            if aligner:
                kwargs["forced_aligner"] = aligner
                kwargs["forced_aligner_kwargs"] = {
                    "dtype": _dtype(),
                    "device_map": _aligner_device(),
                    "attn_implementation": attention,
                }
            _model = Qwen3ASRModel.from_pretrained(_model_id(), **kwargs)
            _load_error = ""
            _loaded_at = time.time()
        except Exception as exc:
            _model = None
            _load_error = f"{type(exc).__name__}: {exc}"[:1000]
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            raise HTTPException(status_code=500, detail={"status": "load_failed", "error": _load_error}) from exc
    return {"status": "loaded", "model": _model_id(), "aligner": _aligner_id(), "gpu": _gpu_status()}


def _unload_model_locked() -> None:
    global _model, _loaded_at
    _model = None
    _loaded_at = 0.0
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


def _unload_model() -> dict[str, Any]:
    with _inference_lock:
        _unload_model_locked()
    return {"status": "unloaded", "gpu": _gpu_status()}


def _audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else 0.0
    except (wave.Error, OSError):
        return 0.0


def _timestamp_row(value: Any, index: int) -> dict[str, Any] | None:
    if isinstance(value, dict):
        text = str(value.get("text") or "").strip()
        start = value.get("start_time", value.get("start", 0.0))
        end = value.get("end_time", value.get("end", start))
    else:
        text = str(getattr(value, "text", "") or "").strip()
        start = getattr(value, "start_time", getattr(value, "start", 0.0))
        end = getattr(value, "end_time", getattr(value, "end", start))
    try:
        start_value = float(start or 0.0)
        end_value = float(end or start_value)
    except (TypeError, ValueError):
        return None
    if not text:
        return None
    return {
        "index": index,
        "start": round(max(0.0, start_value), 3),
        "end": round(max(start_value + 0.03, end_value), 3),
        "text": text,
    }


def _result_payload(result: Any, duration: float) -> dict[str, Any]:
    text = str(getattr(result, "text", "") or "").strip()
    language = str(getattr(result, "language", "") or "")
    raw_timestamps = getattr(result, "time_stamps", None) or getattr(result, "timestamps", None) or []
    if not isinstance(raw_timestamps, (list, tuple, dict)) and hasattr(raw_timestamps, "items"):
        raw_timestamps = getattr(raw_timestamps, "items")
    if isinstance(raw_timestamps, dict):
        raw_timestamps = raw_timestamps.get("items") or []
    segments: list[dict[str, Any]] = []
    for value in raw_timestamps:
        row = _timestamp_row(value, len(segments) + 1)
        if row:
            segments.append(row)
    if not segments and text:
        segments = [{"index": 1, "start": 0.0, "end": round(max(0.3, duration), 3), "text": text}]
    return {"language": language, "text": text, "segments": segments}


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


@app.on_event("startup")
def _startup() -> None:
    if _env_truthy("QWEN3_ASR_AUTOLOAD", False):
        _load_model()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ready" if _model is not None else "available",
        "service_version": SERVICE_VERSION,
        "model": {
            "loaded": _model is not None,
            "model_id": _model_id(),
            "aligner_id": _aligner_id(),
            "loaded_seconds": round(time.time() - _loaded_at, 3) if _loaded_at else 0.0,
            "last_error": _load_error,
        },
        "runtime": {
            "torch": torch.__version__,
            "cuda": torch.version.cuda or "",
            "qwen_asr": _package_version("qwen-asr"),
            "device": _device(),
            "aligner_device": _aligner_device(),
            "dtype": str(_dtype()).replace("torch.", ""),
            "max_batch": _int_env("QWEN3_ASR_MAX_BATCH", 1, 1, 32),
            "max_new_tokens": _int_env("QWEN3_ASR_MAX_NEW_TOKENS", 1024, 128, 8192),
        },
        "gpu": _gpu_status(),
        "limits": {
            "max_audio_seconds": _float_env("QWEN3_ASR_MAX_AUDIO_SECONDS", 300.0, 10.0, 300.0),
            "min_free_gpu_gb_to_load": _float_env("QWEN3_ASR_MIN_FREE_GPU_GB", 10.0, 4.0, 15.0),
        },
    }


@app.post("/load")
def load_model(payload: LoadRequest = LoadRequest()) -> dict[str, Any]:
    return _load_model(force=payload.force)


@app.post("/unload")
def unload_model() -> dict[str, Any]:
    return _unload_model()


@app.post("/transcribe/file")
def transcribe_file(
    audio: UploadFile = File(...),
    language: str = Form("Chinese"),
    context: str = Form(""),
    return_time_stamps: bool = Form(True),
) -> dict[str, Any]:
    if _model is None:
        raise HTTPException(status_code=503, detail={"status": "model_not_loaded", "action": "POST /load first"})
    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    max_bytes = _int_env("QWEN3_ASR_MAX_UPLOAD_MB", 32, 1, 256) * 1024 * 1024
    payload = audio.file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="Audio upload is too large")
    started = time.time()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="dso-qwen3-asr-", suffix=suffix, delete=False) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        duration = _audio_duration(temp_path)
        maximum = _float_env("QWEN3_ASR_MAX_AUDIO_SECONDS", 300.0, 10.0, 300.0)
        if duration > maximum + 0.5:
            raise HTTPException(
                status_code=413,
                detail={"status": "audio_too_long", "duration_seconds": duration, "max_audio_seconds": maximum},
            )
        with _inference_lock:
            kwargs: dict[str, Any] = {
                "audio": str(temp_path),
                "language": language.strip() or None,
                "context": context.strip(),
                "return_time_stamps": bool(return_time_stamps and _aligner_id()),
            }
            signature = inspect.signature(_model.transcribe)
            kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
            results = _model.transcribe(**kwargs)
        if not results:
            raise HTTPException(status_code=502, detail="ASR returned no result")
        result = _result_payload(results[0], duration)
        text_chars = len(_compact_text(str(result.get("text") or "")))
        segment_count = len(result.get("segments") or [])
        empty_text = text_chars == 0
        context_echo = _is_context_echo(str(result.get("text") or ""), context)
        return {
            "status": "empty" if empty_text else "ready",
            "model": _model_id(),
            "aligner": _aligner_id(),
            "duration_seconds": round(duration, 3),
            "elapsed_seconds": round(time.time() - started, 3),
            "quality": {
                "empty_text": empty_text,
                "context_echo": context_echo,
                "text_chars": text_chars,
                "segment_count": segment_count,
                "text_chars_per_second": round(text_chars / max(0.001, duration), 4),
            },
            **result,
        }
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
