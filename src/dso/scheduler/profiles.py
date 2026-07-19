from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from dso.features.qwen3_asr import qwen3_asr_health, qwen3_asr_model, qwen3_asr_service_url
from dso.learning.qwen_embeddings import QWEN_EMBEDDING_MODEL, QwenEmbeddingClient
from dso.learning.qwen_omni import QWEN_OMNI_MODEL, QwenOmniClient, qwen_omni_status
from dso.scheduler.contracts import OMNI_PROFILE_ID, QWEN3_ASR_PROFILE_ID, QWEN_EMBEDDING_PROFILE_ID


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    profile_id: str
    model_id: str
    service_url: str
    capability: str
    health: Callable[[], dict[str, Any]]
    is_ready: Callable[[dict[str, Any]], bool]
    actual_model_id: Callable[[dict[str, Any]], str]


def runtime_profiles() -> dict[str, RuntimeProfile]:
    omni_client = QwenOmniClient()
    embedding_client = QwenEmbeddingClient()
    return {
        OMNI_PROFILE_ID: RuntimeProfile(
            profile_id=OMNI_PROFILE_ID,
            model_id=QWEN_OMNI_MODEL,
            service_url=omni_client.service_url,
            capability="omni_candidate_window",
            health=lambda: qwen_omni_status(client=omni_client),
            is_ready=lambda value: value.get("status") == "ready" and bool(value.get("loaded_omni")),
            actual_model_id=lambda value: str(value.get("loaded_model") or value.get("model") or ""),
        ),
        QWEN3_ASR_PROFILE_ID: RuntimeProfile(
            profile_id=QWEN3_ASR_PROFILE_ID,
            model_id=qwen3_asr_model(),
            service_url=qwen3_asr_service_url(),
            capability="qwen3_asr_chunk",
            health=lambda: qwen3_asr_health(timeout_seconds=5.0),
            is_ready=_asr_ready,
            actual_model_id=_asr_model_id,
        ),
        QWEN_EMBEDDING_PROFILE_ID: RuntimeProfile(
            profile_id=QWEN_EMBEDDING_PROFILE_ID,
            model_id=QWEN_EMBEDDING_MODEL,
            service_url=embedding_client.service_url,
            capability="qwen_embedding",
            health=embedding_client.health,
            is_ready=lambda value: value.get("status") == "ready" and bool(value.get("model_loaded")) and _model_identity_matches(str(value.get("model_id") or ""), QWEN_EMBEDDING_MODEL),
            actual_model_id=lambda value: str(value.get("model_id") or ""),
        ),
    }


def runtime_profile(profile_id: str) -> RuntimeProfile | None:
    return runtime_profiles().get(str(profile_id or ""))


def _asr_ready(value: dict[str, Any]) -> bool:
    model = value.get("model") if isinstance(value.get("model"), dict) else {}
    return value.get("status") == "ready" and bool(model.get("loaded")) and _model_identity_matches(str(model.get("model_id") or qwen3_asr_model()), qwen3_asr_model())


def _asr_model_id(value: dict[str, Any]) -> str:
    model = value.get("model") if isinstance(value.get("model"), dict) else {}
    return str(model.get("model_id") or "")


def _model_identity_matches(actual: str, expected: str) -> bool:
    normalized_actual = str(actual or "").rstrip("/").lower()
    normalized_expected = str(expected or "").rstrip("/").lower()
    if not normalized_actual or not normalized_expected:
        return False
    return normalized_actual == normalized_expected or normalized_actual.endswith(f"/{normalized_expected.rsplit('/', 1)[-1]}")
