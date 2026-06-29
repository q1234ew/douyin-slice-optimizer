from __future__ import annotations

import os

ASR_PROFILE_MODELS = {
    "fast": "base",
    "quality": "small",
    "verify": "large-v3-turbo-q5_0",
}

ASR_COMPARE_MODELS = ["base", "small"]

_PROFILE_ALIASES = {
    "default": "fast",
    "base": "fast",
    "quick": "fast",
    "prod": "fast",
    "production": "fast",
    "small": "quality",
    "high": "quality",
    "hq": "quality",
    "accurate": "quality",
    "verify": "verify",
    "verification": "verify",
    "premium": "verify",
    "large": "verify",
    "large-v3": "verify",
    "large-v3-turbo": "verify",
    "large-v3-turbo-q5_0": "verify",
}

_COMPARE_ALIASES = {
    "both": "compare",
    "benchmark": "compare",
    "bench": "compare",
    "ab": "compare",
    "a/b": "compare",
}


def normalize_asr_profile(profile: str | None = None, *, allow_compare: bool = False) -> str:
    value = (profile or os.getenv("DSO_ASR_PROFILE", "fast")).strip().lower() or "fast"
    value = _PROFILE_ALIASES.get(value, _COMPARE_ALIASES.get(value, value))
    if value == "compare" and allow_compare:
        return value
    if value in ASR_PROFILE_MODELS:
        return value
    if value == "compare":
        return "quality"
    return "fast"


def resolve_asr_model_size(model_size: str | None = None, *, profile: str | None = None) -> str:
    explicit = model_size or os.getenv("DSO_WHISPER_MODEL")
    if explicit:
        return explicit.strip() or "base"
    return ASR_PROFILE_MODELS[normalize_asr_profile(profile)]


def resolve_asr_model_list(models: str | None = None, *, profile: str | None = None) -> list[str]:
    if models:
        resolved = [item.strip() for item in models.split(",") if item.strip()]
        if resolved:
            return resolved
    profile_name = normalize_asr_profile(profile, allow_compare=True)
    if profile_name == "compare":
        return list(ASR_COMPARE_MODELS)
    return [ASR_PROFILE_MODELS[profile_name]]
