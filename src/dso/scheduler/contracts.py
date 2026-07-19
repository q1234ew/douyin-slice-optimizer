from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping


MODEL_JOB_CONTRACT_VERSION = "model_job.v1"
MODEL_SCHEDULER_VERSION = "model_scheduler.v1"
OMNI_RERANK_JOB_KIND = "omni_candidate_rerank"
OMNI_PROFILE_ID = "qwen2_5_omni_7b_gptq_int4.local_v1"
QWEN3_ASR_JOB_KIND = "qwen3_asr_program"
QWEN3_ASR_PROFILE_ID = "qwen3_asr_1_7b_forced_aligner.local_v1"
TEXT_EMBEDDING_JOB_KIND = "text_embedding_build"
VISUAL_EMBEDDING_JOB_KIND = "visual_embedding_build"
QWEN_EMBEDDING_PROFILE_ID = "qwen3_vl_embedding_2b.local_v1"

ACTIVE_JOB_STATUSES = frozenset(
    {
        "queued",
        "preparing",
        "ready",
        "waiting_resource",
        "running",
        "retry_wait",
        "cancel_requested",
    }
)
TERMINAL_JOB_STATUSES = frozenset(
    {"succeeded", "degraded", "failed", "cancelled", "cancelled_partial", "expired"}
)
RETRYABLE_JOB_STATUSES = frozenset({"failed", "degraded", "cancelled_partial"})

PRIORITY_DEFAULTS = {
    "interactive_verify": 320,
    "interactive_product": 300,
    "product_batch": 220,
    "maintenance": 110,
    "research": 40,
}

SAFE_ERROR_CODES = frozenset(
    {
        "input_missing",
        "input_changed",
        "model_unavailable",
        "model_identity_mismatch",
        "resource_unavailable",
        "lease_lost",
        "deadline_expired",
        "inference_timeout",
        "gpu_oom",
        "schema_invalid",
        "result_commit_failed",
        "cancelled_by_user",
        "budget_or_policy_denied",
        "internal_error",
    }
)


def stable_json_hash(value: Mapping[str, Any] | list[Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def safe_error_code(value: str | None) -> str:
    normalized = str(value or "internal_error").strip().lower()
    return normalized if normalized in SAFE_ERROR_CODES else "internal_error"


def safe_error_summary(value: object, *, limit: int = 500) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    lowered = text.lower()
    for marker in ("authorization", "api_key", "apikey", "access_token", "secret", "password", "prompt"):
        if marker in lowered:
            return "sensitive error detail was redacted"
    return text[:limit]


@dataclass(frozen=True, slots=True)
class JobItemSpec:
    item_kind: str
    item_role: str
    input_hash: str
    request: Mapping[str, Any] = field(default_factory=dict)
    estimated_units: float = 1.0
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class ModelJobSpec:
    job_kind: str
    subject_type: str
    subject_id: str
    account_id: str
    resource_class: str
    model_profile_id: str
    model_id: str
    model_version: str
    prompt_version: str
    priority_class: str
    base_priority: int
    input_hash: str
    parameters_hash: str
    dedupe_key: str
    request_summary: Mapping[str, Any]
    fallback_ref: Mapping[str, Any]
    items: tuple[JobItemSpec, ...]
    max_attempts: int = 3
    deadline_at: str | None = None
    not_before_at: str | None = None
    parent_job_id: str | None = None
    retry_of_job_id: str | None = None

    def __post_init__(self) -> None:
        if not self.job_kind or not self.subject_type or not self.subject_id:
            raise ValueError("job_kind and subject are required")
        if not self.items:
            raise ValueError("model job requires at least one item")
        if self.priority_class not in PRIORITY_DEFAULTS:
            raise ValueError(f"unsupported priority_class: {self.priority_class}")
        if not 1 <= int(self.max_attempts) <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
