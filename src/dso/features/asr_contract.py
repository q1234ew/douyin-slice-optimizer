from __future__ import annotations

from pathlib import Path

from dso.config import ensure_data_dirs
from dso.features.asr import active_asr_backend
from dso.features.asr_profile import ASR_PROFILE_MODELS
from dso.features.asr_routing import asr_routing_plan
from dso.features.whisper_cpp import (
    whisper_cpp_model,
    whisper_cpp_vad_enabled,
    whisper_cpp_vad_model,
)
from dso.utils import read_json
from dso.versions import ASR_PROFILE_PLAN_VERSION


PROFILE_PURPOSES = {
    "fast": "批量召回：快速生成全片 transcript 和候选切片",
    "quality": "发布前复核：对全片或重点节目做更稳的转写",
    "verify": "Top 候选验证：仅对人工选中的高价值候选做二次转写对比",
}


def asr_profile_plan() -> dict:
    benchmark = _last_benchmark()
    profiles = []
    for name, model in ASR_PROFILE_MODELS.items():
        model_path = whisper_cpp_model(model)
        profiles.append(
            {
                "profile": name,
                "model": model,
                "purpose": PROFILE_PURPOSES.get(name, ""),
                "active_backend": active_asr_backend(model_size=model, asr_profile=name),
                "whisper_cpp_model_path": model_path or "",
                "model_exists": bool(model_path and Path(model_path).exists()),
                "vad_enabled": whisper_cpp_vad_enabled(),
                "vad_model": whisper_cpp_vad_model() or "",
                "recent_benchmark": benchmark.get(model),
            }
        )
    return {
        "contract_version": ASR_PROFILE_PLAN_VERSION,
        "default_profile": "fast",
        "manual_verify_profile": "verify",
        "routing_strategy": asr_routing_plan(),
        "profiles": profiles,
        "profiles_by_name": {item["profile"]: item for item in profiles},
    }


def _last_benchmark() -> dict[str, dict]:
    settings = ensure_data_dirs()
    candidates = [
        settings.root / "output/asr-bench/last_benchmark.json",
        settings.root / "outputs/asr-bench/last_benchmark.json",
    ]
    for path in candidates:
        data = read_json(path, default={}) or {}
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            continue
        return {
            str(item.get("model") or ""): {
                "rtf": item.get("rtf"),
                "wall_seconds": item.get("wall_seconds"),
                "audio_seconds": item.get("audio_seconds"),
                "segments": item.get("segments"),
                "source": item.get("source"),
                "transcript_path": item.get("transcript_path") or "",
            }
            for item in results
            if item.get("model")
        }
    return {}
