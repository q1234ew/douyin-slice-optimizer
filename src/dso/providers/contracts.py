from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping


PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION = "public_model_provider.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProviderLifecycleStatus(StrEnum):
    WATCH = "watch"
    VALIDATE = "validate"
    RESEARCH_ONLY = "research_only"
    SHADOW = "shadow"
    CANDIDATE = "candidate"
    ADOPTED = "adopted"
    REJECTED = "rejected"


class ProviderCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    RATE_LIMITED = "rate_limited"
    FALLBACK_LOCAL = "fallback_local"


class ProviderDecisionStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    SHADOW_ONLY = "shadow_only"
    FALLBACK_LOCAL = "fallback_local"
    PRODUCTION_ADOPTED = "production_adopted"


@dataclass(frozen=True, slots=True)
class ProviderModelRef:
    provider_id: str
    model_id: str
    api_version: str
    prompt_version: str

    def __post_init__(self) -> None:
        _require_text(self.provider_id, "provider_id")
        _require_text(self.model_id, "model_id")
        _require_text(self.api_version, "api_version")
        _require_text(self.prompt_version, "prompt_version")


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    identity: ProviderModelRef
    lifecycle_status: ProviderLifecycleStatus = ProviderLifecycleStatus.RESEARCH_ONLY
    request_types: tuple[str, ...] = ()
    uses_public_network: bool = True
    description: str = ""
    contract_version: str = PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_contract(self.contract_version)
        if not isinstance(self.identity, ProviderModelRef):
            raise TypeError("identity must be ProviderModelRef")
        if not isinstance(self.lifecycle_status, ProviderLifecycleStatus):
            raise TypeError("lifecycle_status must be ProviderLifecycleStatus")
        if not self.request_types:
            raise ValueError("request_types must contain at least one capability")
        for request_type in self.request_types:
            _require_text(request_type, "request_types item")


@dataclass(frozen=True, slots=True)
class ProviderInputSize:
    video_seconds: float = 0.0
    audio_seconds: float = 0.0
    frame_count: int = 0
    image_count: int = 0
    text_characters: int = 0
    input_tokens: int = 0
    request_bytes: int = 0

    def __post_init__(self) -> None:
        for name in ("video_seconds", "audio_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a finite non-negative number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name in (
            "frame_count",
            "image_count",
            "text_characters",
            "input_tokens",
            "request_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ProviderDataPermissionRecord:
    """Immutable audit snapshot; authorization decisions live in policy.DataPermission."""

    allowed_to_leave_local: bool = False
    authorization_basis: str = "local_only"
    redaction_strategy: str = "not_applicable"
    retention_days: int | None = 0

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_to_leave_local, bool):
            raise TypeError("allowed_to_leave_local must be bool")
        _require_text(self.authorization_basis, "authorization_basis")
        _require_text(self.redaction_strategy, "redaction_strategy")
        if self.retention_days is not None and (
            isinstance(self.retention_days, bool)
            or not isinstance(self.retention_days, int)
            or self.retention_days < 0
        ):
            raise ValueError("retention_days must be a non-negative integer or None")
        if self.allowed_to_leave_local:
            if self.authorization_basis == "local_only":
                raise ValueError("external data use requires an explicit authorization_basis")
            if self.retention_days is None:
                raise ValueError("external data use requires a known retention_days policy")


@dataclass(frozen=True, slots=True)
class ProviderExecutionPolicy:
    public_api_enabled: bool = False
    budget_authorized: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.public_api_enabled, bool):
            raise TypeError("public_api_enabled must be bool")
        if not isinstance(self.budget_authorized, bool):
            raise TypeError("budget_authorized must be bool")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a finite positive number")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    request_id: str
    request_type: str
    target: ProviderModelRef
    content_sha256: str
    input_size: ProviderInputSize
    data_permission: ProviderDataPermissionRecord = field(
        default_factory=ProviderDataPermissionRecord
    )
    execution_policy: ProviderExecutionPolicy = field(default_factory=ProviderExecutionPolicy)
    payload: Mapping[str, Any] = field(default_factory=dict)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    contract_version: str = PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_contract(self.contract_version)
        _require_text(self.request_id, "request_id")
        _require_text(self.request_type, "request_type")
        if not isinstance(self.target, ProviderModelRef):
            raise TypeError("target must be ProviderModelRef")
        if not isinstance(self.input_size, ProviderInputSize):
            raise TypeError("input_size must be ProviderInputSize")
        if not isinstance(self.data_permission, ProviderDataPermissionRecord):
            raise TypeError("data_permission must be ProviderDataPermissionRecord")
        if not isinstance(self.execution_policy, ProviderExecutionPolicy):
            raise TypeError("execution_policy must be ProviderExecutionPolicy")
        if not _SHA256_RE.fullmatch(self.content_sha256):
            raise ValueError("content_sha256 must be a lowercase SHA-256 hex digest")
        _validate_json_object(self.payload, "payload")
        _validate_json_object(self.parameters, "parameters")

    @property
    def cache_key(self) -> str:
        value = {
            "contract_version": self.contract_version,
            "provider_id": self.target.provider_id,
            "model_id": self.target.model_id,
            "api_version": self.target.api_version,
            "prompt_version": self.target.prompt_version,
            "request_type": self.request_type,
            "content_sha256": self.content_sha256,
            "parameters": self.parameters,
        }
        return stable_json_sha256(value)


@dataclass(frozen=True, slots=True)
class ProviderCallMetrics:
    input_size: ProviderInputSize
    output_tokens: int = 0
    latency_ms: float = 0.0
    retry_count: int = 0
    rate_limit_count: int = 0
    request_count: int = 1
    network_request_count: int = 0
    cache_hit: bool = False
    estimated_cost: Decimal = Decimal("0")
    cost_currency: str = "CNY"
    error_code: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.input_size, ProviderInputSize):
            raise TypeError("input_size must be ProviderInputSize")
        for name in (
            "output_tokens",
            "retry_count",
            "rate_limit_count",
            "request_count",
            "network_request_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, (int, float)):
            raise TypeError("latency_ms must be a finite non-negative number")
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        if not isinstance(self.cache_hit, bool):
            raise TypeError("cache_hit must be bool")
        if not isinstance(self.estimated_cost, Decimal):
            raise TypeError("estimated_cost must be Decimal")
        if not self.estimated_cost.is_finite() or self.estimated_cost < 0:
            raise ValueError("estimated_cost must be finite and non-negative")
        currency = self.cost_currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("cost_currency must be a three-letter currency code")
        object.__setattr__(self, "cost_currency", currency)


@dataclass(frozen=True, slots=True)
class ProviderDecisionEvidence:
    local_baseline: Mapping[str, Any] = field(default_factory=dict)
    api_result: Mapping[str, Any] = field(default_factory=dict)
    fusion_result: Mapping[str, Any] = field(default_factory=dict)
    decision_status: ProviderDecisionStatus = ProviderDecisionStatus.SHADOW_ONLY
    final_adoption_reason: str = "not promoted; shadow evidence only"

    def __post_init__(self) -> None:
        if not isinstance(self.decision_status, ProviderDecisionStatus):
            raise TypeError("decision_status must be ProviderDecisionStatus")
        _validate_json_object(self.local_baseline, "local_baseline")
        _validate_json_object(self.api_result, "api_result")
        _validate_json_object(self.fusion_result, "fusion_result")
        _require_text(self.final_adoption_reason, "final_adoption_reason")


@dataclass(frozen=True, slots=True)
class ProviderResult:
    request_id: str
    request_type: str
    target: ProviderModelRef
    status: ProviderCallStatus
    output: Mapping[str, Any]
    metrics: ProviderCallMetrics
    data_permission: ProviderDataPermissionRecord
    lifecycle_status: ProviderLifecycleStatus = ProviderLifecycleStatus.RESEARCH_ONLY
    decision: ProviderDecisionEvidence = field(default_factory=ProviderDecisionEvidence)
    contract_version: str = PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_contract(self.contract_version)
        _require_text(self.request_id, "request_id")
        _require_text(self.request_type, "request_type")
        if not isinstance(self.target, ProviderModelRef):
            raise TypeError("target must be ProviderModelRef")
        if not isinstance(self.status, ProviderCallStatus):
            raise TypeError("status must be ProviderCallStatus")
        if not isinstance(self.metrics, ProviderCallMetrics):
            raise TypeError("metrics must be ProviderCallMetrics")
        if not isinstance(self.data_permission, ProviderDataPermissionRecord):
            raise TypeError("data_permission must be ProviderDataPermissionRecord")
        if not isinstance(self.lifecycle_status, ProviderLifecycleStatus):
            raise TypeError("lifecycle_status must be ProviderLifecycleStatus")
        if not isinstance(self.decision, ProviderDecisionEvidence):
            raise TypeError("decision must be ProviderDecisionEvidence")
        _validate_json_object(self.output, "output")


def stable_json_sha256(value: Any) -> str:
    _validate_json_value(value, "value")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_contract(contract_version: str) -> None:
    if contract_version != PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION:
        raise ValueError(
            f"unsupported contract_version {contract_version!r}; "
            f"expected {PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION!r}"
        )


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_json_object(value: Mapping[str, Any], name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    _validate_json_value(value, name)


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            _validate_json_value(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains a non-JSON value of type {type(value).__name__}")
