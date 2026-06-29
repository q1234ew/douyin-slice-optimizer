from __future__ import annotations

import importlib.util
import os
import platform
from pathlib import Path
import shutil
import subprocess

from dso.config import ensure_data_dirs
from dso.features.asr import active_asr_backend, asr_prompt
from dso.features.asr_contract import asr_profile_plan
from dso.features.asr_profile import ASR_PROFILE_MODELS, normalize_asr_profile, resolve_asr_model_size
from dso.features.whisper_cpp import (
    whisper_cpp_binary,
    whisper_cpp_language,
    whisper_cpp_model,
    whisper_cpp_model_name,
    whisper_cpp_vad_enabled,
    whisper_cpp_vad_model,
)
from dso.scoring.rights import rights_mode


def runtime_diagnostics() -> dict:
    settings = ensure_data_dirs()
    ffmpeg = _binary_status("ffmpeg")
    ffprobe = _binary_status("ffprobe")
    faster_whisper = importlib.util.find_spec("faster_whisper") is not None
    profile = normalize_asr_profile()
    model_size = resolve_asr_model_size(profile=profile)
    backend = os.getenv("DSO_ASR_BACKEND", "auto")
    device = os.getenv("DSO_WHISPER_DEVICE", "auto")
    compute_type = os.getenv("DSO_WHISPER_COMPUTE_TYPE", "int8")
    whisper_cpp = _whisper_cpp_status()
    asr_ready = faster_whisper or whisper_cpp["ready"]
    asr_status = "ready" if asr_ready else "fallback_placeholder"
    active_backend = active_asr_backend(model_size=model_size)
    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "asr": {
            "status": asr_status,
            "backend": backend,
            "profile": profile,
            "profiles": ASR_PROFILE_MODELS,
            "profile_plan": asr_profile_plan(),
            "active_backend": active_backend,
            "fallback_order": _fallback_order(backend, active_backend),
            "cache_enabled": True,
            "faster_whisper_installed": faster_whisper,
            "default_model": model_size,
            "device": device,
            "compute_type": compute_type,
            "cpu_threads": os.getenv("DSO_WHISPER_CPU_THREADS"),
            "num_workers": os.getenv("DSO_WHISPER_NUM_WORKERS"),
            "acceleration": _acceleration_status(),
            "whisper_cpp": whisper_cpp,
            "prompt": asr_prompt(),
            "sidecar_srt_supported": True,
            "setup_command": "python3 -m dso.cli setup-asr --profile fast",
            "quality_setup_command": "python3 -m dso.cli setup-asr --profile quality",
            "quality_extract_command": "python3 -m dso.cli extract <video_id> --asr-profile quality --asr-backend whisper_cpp --force-asr",
            "benchmark_command": "python3 -m dso.cli bench-asr <audio_or_video> --backend whisper_cpp --profile compare",
            "install_command": 'python3 -m pip install -e ".[asr]"',
            "note": (
                _asr_note(faster_whisper, whisper_cpp)
            ),
        },
        "rights_mode": rights_mode(),
        "paths": {
            "data_dir": str(settings.data_dir),
            "media_dir": str(settings.media_dir),
            "exports_dir": str(settings.exports_dir),
            "cache_dir": str(settings.cache_dir),
            "db_path": str(settings.db_path),
        },
    }


def _asr_note(faster_whisper: bool, whisper_cpp: dict) -> str:
    if whisper_cpp.get("ready"):
        return "whisper.cpp 已配置；Apple Silicon 可通过 Metal/Core ML 后端加速"
    if faster_whisper:
        return "faster-whisper 可用，当前作为真实 ASR CPU 兜底"
    return "当前会使用占位 transcript；可安装 faster-whisper、配置 whisper.cpp，或提供同名 .srt 字幕"


def _whisper_cpp_status() -> dict:
    binary = whisper_cpp_binary()
    model = whisper_cpp_model()
    model_exists = bool(model and Path(model).exists())
    return {
        "ready": bool(binary and model_exists),
        "binary": binary,
        "binary_env": os.getenv("DSO_WHISPER_CPP_BIN"),
        "model": model,
        "model_name": whisper_cpp_model_name(),
        "model_env": os.getenv("DSO_WHISPER_CPP_MODEL"),
        "model_exists": model_exists,
        "language": whisper_cpp_language(),
        "vad_enabled": whisper_cpp_vad_enabled(),
        "vad_model": whisper_cpp_vad_model(),
        "vad_env": os.getenv("DSO_WHISPER_CPP_VAD"),
        "vad_model_env": os.getenv("DSO_WHISPER_CPP_VAD_MODEL"),
        "extra_args": os.getenv("DSO_WHISPER_CPP_EXTRA_ARGS"),
    }


def _fallback_order(backend: str, active_backend: str) -> list[str]:
    requested = backend.strip().lower() or "auto"
    if requested == "auto":
        return ["sidecar_srt", "whisper_cpp", "faster_whisper", "placeholder"]
    if active_backend.startswith("unknown"):
        return ["placeholder"]
    return ["sidecar_srt", active_backend, "placeholder"]


def _acceleration_status() -> dict:
    status = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "ctranslate2_installed": False,
        "cpu_compute_types": [],
        "cuda_compute_types": [],
        "cuda_error": None,
    }
    try:
        import ctranslate2  # type: ignore
    except Exception as exc:
        status["cuda_error"] = str(exc)
        return status

    status["ctranslate2_installed"] = True
    for device in ["cpu", "cuda"]:
        try:
            status[f"{device}_compute_types"] = sorted(ctranslate2.get_supported_compute_types(device))
        except Exception as exc:
            status[f"{device}_error"] = str(exc)
    return status


def _binary_status(name: str) -> dict:
    path = shutil.which(name)
    if not path:
        return {"available": False, "path": None, "version": None}
    version = None
    try:
        result = subprocess.run(
            [path, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        version = (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else None
    except Exception:
        version = None
    return {"available": True, "path": path, "version": version}
