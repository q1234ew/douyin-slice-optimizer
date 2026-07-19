"""Application-facing status and zero-network smoke helpers for G3."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

from dso.config import ensure_data_dirs
from dso.providers.aliyun_bailian import (
    BAILIAN_PROVIDER_ID,
    BAILIAN_SECRET_ENV,
    DEFAULT_BAILIAN_MODEL,
    AliyunBailianProvider,
    BailianConfigurationError,
    validate_bailian_base_url,
)
from dso.providers.budget import BudgetGuard, BudgetLimits, Money
from dso.providers.admin_config import provider_config_values
from dso.providers.cache import FileResponseCache
from dso.providers.contracts import (
    PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION,
    ProviderDataPermissionRecord,
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderRequest,
    stable_json_sha256,
)
from dso.providers.fake import FakeProvider
from dso.providers.ledger import LEDGER_SCHEMA_VERSION, PublicModelLedger
from dso.providers.policy import (
    DataPermission,
    PublicModelPolicy,
    SecretEnvRef,
    UploadLevel,
)
from dso.providers.registry import ProviderRegistry
from dso.providers.runner import PUBLIC_MODEL_RUNNER_CONTRACT_VERSION, PublicModelRunner


PUBLIC_MODEL_RUNTIME_CONTRACT_VERSION = "public_model_runtime.v2"
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AliyunBailianRuntime:
    provider: AliyunBailianProvider
    runner: PublicModelRunner
    budget_guard: BudgetGuard
    data_permission: ProviderDataPermissionRecord
    allowed_upload_levels: frozenset[UploadLevel]
    batch_id: str


def _runtime_paths() -> tuple[Path, Path]:
    settings = ensure_data_dirs()
    return (
        settings.cache_dir / "public_models",
        settings.db_dir / "public_model_ledger.sqlite3",
    )


def _registry_with_providers(
    bailian_provider: AliyunBailianProvider | None = None,
) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    registry.register(bailian_provider or AliyunBailianProvider())
    return registry


def _registry_with_fake() -> ProviderRegistry:
    """Keep the fake smoke isolated from any network-capable provider."""

    registry = ProviderRegistry()
    registry.register(FakeProvider())
    return registry


def _public_api_flag() -> bool:
    return os.environ.get("DSO_PUBLIC_MODEL_API_ENABLED", "").strip().lower() in _TRUE_VALUES


def _bailian_environment() -> dict[str, object]:
    errors: list[str] = []
    provider_selected = (
        os.environ.get("DSO_PUBLIC_MODEL_PROVIDER", "").strip() == BAILIAN_PROVIDER_ID
    )
    model = os.environ.get("DSO_BAILIAN_MODEL_ID", DEFAULT_BAILIAN_MODEL).strip()
    try:
        model_probe = AliyunBailianProvider(model_id=model)
        fixed_model_selected = True
    except BailianConfigurationError:
        model_probe = AliyunBailianProvider()
        fixed_model_selected = False
        errors.append("DSO_BAILIAN_MODEL_ID must be an allowed fixed snapshot")

    raw_base_url = os.environ.get("DSO_BAILIAN_BASE_URL", "").strip()
    base_url: str | None = None
    if raw_base_url:
        try:
            base_url = validate_bailian_base_url(raw_base_url)
        except BailianConfigurationError:
            errors.append("DSO_BAILIAN_BASE_URL is not a valid cn-beijing workspace URL")
    base_url_configured = base_url is not None
    secret_configured = SecretEnvRef(BAILIAN_SECRET_ENV).is_configured

    budget_names = (
        "DSO_PUBLIC_MODEL_BUDGET_PER_REQUEST_CNY",
        "DSO_PUBLIC_MODEL_BUDGET_PER_BATCH_CNY",
        "DSO_PUBLIC_MODEL_BUDGET_PER_DAY_CNY",
    )
    budget_values: list[Decimal] = []
    for name in budget_names:
        raw = os.environ.get(name, "").strip()
        try:
            value = Decimal(raw)
        except Exception:
            value = Decimal("-1")
        if not value.is_finite() or value <= 0:
            errors.append(f"{name} must be a positive decimal amount")
        budget_values.append(value)
    budget_configured = (
        all(value.is_finite() and value > 0 for value in budget_values)
        and budget_values[0] <= budget_values[1] <= budget_values[2]
    )
    if all(value.is_finite() and value > 0 for value in budget_values) and not budget_configured:
        errors.append("public-model budgets must satisfy per_request <= per_batch <= per_day")
    budget_limits = (
        BudgetLimits(
            per_request=Money(budget_values[0], "CNY"),
            per_batch=Money(budget_values[1], "CNY"),
            per_day=Money(budget_values[2], "CNY"),
        )
        if budget_configured
        else None
    )

    data_allowed = (
        os.environ.get("DSO_BAILIAN_DATA_ALLOWED", "").strip().lower() in _TRUE_VALUES
    )
    authorization_basis = os.environ.get(
        "DSO_BAILIAN_AUTHORIZATION_BASIS", ""
    ).strip()
    redaction_strategy = os.environ.get(
        "DSO_BAILIAN_REDACTION_STRATEGY", ""
    ).strip()
    retention_reference = os.environ.get(
        "DSO_BAILIAN_RETENTION_POLICY_REFERENCE", ""
    ).strip()
    retention_raw = os.environ.get("DSO_BAILIAN_RETENTION_DAYS", "").strip()
    retention_is_policy_only = retention_raw == "provider_minimum_necessary"
    if retention_is_policy_only:
        retention_days = None
        retention_valid = True
    else:
        try:
            retention_days = int(retention_raw)
            retention_valid = retention_days >= 0 and str(retention_days) == retention_raw
        except ValueError:
            retention_days = None
            retention_valid = False

    levels_raw = os.environ.get("DSO_BAILIAN_ALLOWED_UPLOAD_LEVELS", "").strip()
    allowed_upload_levels: frozenset[UploadLevel] = frozenset()
    if levels_raw:
        try:
            parsed_levels = frozenset(
                UploadLevel(item.strip()) for item in levels_raw.split(",") if item.strip()
            )
        except ValueError:
            parsed_levels = frozenset()
            errors.append("DSO_BAILIAN_ALLOWED_UPLOAD_LEVELS contains an unknown level")
        forbidden = parsed_levels - {
            UploadLevel.STRUCTURED_SUMMARY,
            UploadLevel.REPRESENTATIVE_FRAMES,
            UploadLevel.FULL_MEDIA,
        }
        if forbidden:
            errors.append(
                "Bailian only permits structured_summary/representative_frames/full_media"
            )
        else:
            allowed_upload_levels = parsed_levels

    data_permission_configured = all(
        (
            data_allowed,
            bool(authorization_basis),
            bool(redaction_strategy),
            retention_valid,
            bool(retention_reference),
            bool(allowed_upload_levels),
        )
    )
    if data_allowed and not data_permission_configured:
        errors.append(
            "Bailian data permission requires authorization, redaction, retention, and upload levels"
        )
    data_permission = (
        DataPermission(
            may_leave_local=True,
            authorization_basis=authorization_basis,
            allowed_upload_levels=allowed_upload_levels,
            redaction_strategy=redaction_strategy,
            retention_days=retention_days,
            retention_policy_reference=retention_reference,
        )
        if data_permission_configured
        else None
    )

    provider = AliyunBailianProvider(
        model_id=(model if fixed_model_selected else model_probe.descriptor.identity.model_id),
        base_url=base_url,
    )
    return {
        "provider": provider,
        "base_url": base_url,
        "provider_selected": provider_selected,
        "fixed_model_selected": fixed_model_selected,
        "base_url_configured": base_url_configured,
        "secret_configured": secret_configured,
        "budget_configured": budget_configured,
        "data_permission_configured": data_permission_configured,
        "retention_policy_confirmed": data_permission_configured,
        "retention_days_known": retention_days is not None,
        "budget_limits": budget_limits,
        "data_permission": data_permission,
        "allowed_upload_levels": allowed_upload_levels,
        "errors": tuple(dict.fromkeys(errors)),
    }


def public_model_status() -> dict:
    cache_path, ledger_path = _runtime_paths()
    bailian = _bailian_environment()
    registry = _registry_with_providers(bailian["provider"])
    descriptors = []
    for descriptor in registry.descriptors():
        identity = descriptor.identity
        descriptors.append(
            {
                "provider": identity.provider_id,
                "model": identity.model_id,
                "api_version": identity.api_version,
                "prompt_version": identity.prompt_version,
                "lifecycle_status": descriptor.lifecycle_status.value,
                "request_types": list(descriptor.request_types),
                "uses_public_network": descriptor.uses_public_network,
                "description": descriptor.description,
            }
        )

    ledger_call_count = 0
    if ledger_path.exists():
        ledger_call_count = PublicModelLedger(ledger_path).count()
    public_enabled = _public_api_flag()
    network_provider_count = sum(item["uses_public_network"] for item in descriptors)
    gates = {
        "public_api_enabled": public_enabled,
        "network_provider_registered": network_provider_count > 0,
        "provider_selected": bool(bailian["provider_selected"]),
        "fixed_model_selected": bool(bailian["fixed_model_selected"]),
        "workspace_base_url_configured": bool(bailian["base_url_configured"]),
        "secret_configured": bool(bailian["secret_configured"]),
        "budget_configured": bool(bailian["budget_configured"]),
        "data_permission_configured": bool(bailian["data_permission_configured"]),
        "retention_policy_confirmed": bool(bailian["retention_policy_confirmed"]),
    }
    all_ready = all(gates.values())
    return {
        "contract_version": PUBLIC_MODEL_RUNTIME_CONTRACT_VERSION,
        "provider_contract_version": PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION,
        "runner_contract_version": PUBLIC_MODEL_RUNNER_CONTRACT_VERSION,
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "status": "ready_for_shadow" if all_ready else "disabled",
        "lifecycle_status": "research_only",
        "public_api_enabled": public_enabled,
        "network_calls_allowed": all_ready,
        "registered_providers": descriptors,
        "network_provider_count": network_provider_count,
        "gates": gates,
        "configuration_errors": list(bailian["errors"]),
        "retention_days_known": bool(bailian["retention_days_known"]),
        "cache_path": str(cache_path),
        "ledger_path": str(ledger_path),
        "ledger_call_count": ledger_call_count,
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "automatic_publish": False,
        "next_action": (
            "Run a bounded research-only shadow batch and verify usage, latency and ledger cost."
            if all_ready
            else "Keep the adapter disabled until provider selection, workspace URL, secret, "
            "budgets, explicit data permission and a referenced retention policy are configured."
        ),
    }


def provider_admin_status(
    *,
    secure_submission_allowed: bool,
    secure_submission_reason: str,
) -> dict:
    """Return connection configuration metadata without secret material."""

    runtime = public_model_status()
    config = provider_config_values()
    return {
        **config,
        "status": runtime["status"],
        "lifecycle_status": runtime["lifecycle_status"],
        "public_api_enabled": runtime["public_api_enabled"],
        "network_calls_allowed": runtime["network_calls_allowed"],
        "gates": runtime["gates"],
        "configuration_errors": runtime["configuration_errors"],
        "secure_submission_allowed": secure_submission_allowed,
        "secure_submission_reason": secure_submission_reason,
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "automatic_publish": False,
    }


def build_aliyun_bailian_runtime(
    *,
    batch_id: str,
    model_id: str | None = None,
    request_profile: str | None = None,
) -> AliyunBailianRuntime:
    """Build a real runtime only after every environment gate is explicit.

    This function does not invoke the provider. It is safe to use for startup
    validation and injects persisted batch/day spend into the in-memory guard.
    """

    if not str(batch_id).strip():
        raise ValueError("batch_id is required")
    settings = _bailian_environment()
    required = {
        "public_api_enabled": _public_api_flag(),
        "provider_selected": bool(settings["provider_selected"]),
        "fixed_model_selected": bool(settings["fixed_model_selected"]),
        "base_url_configured": bool(settings["base_url_configured"]),
        "secret_configured": bool(settings["secret_configured"]),
        "budget_configured": bool(settings["budget_configured"]),
        "data_permission_configured": bool(settings["data_permission_configured"]),
    }
    missing = [name for name, ready in required.items() if not ready]
    if missing:
        raise RuntimeError(
            "Aliyun Bailian runtime remains fail-closed; missing gates: "
            + ", ".join(missing)
        )

    provider = settings["provider"]
    if model_id is not None or request_profile is not None:
        provider = AliyunBailianProvider(
            model_id=str(
                model_id or provider.descriptor.identity.model_id
            ).strip(),
            request_profile=str(request_profile or "standard").strip(),
            base_url=settings.get("base_url"),
        )
    budget_limits = settings["budget_limits"]
    permission = settings["data_permission"]
    if not isinstance(provider, AliyunBailianProvider):  # pragma: no cover
        raise RuntimeError("invalid Bailian provider configuration")
    if not isinstance(budget_limits, BudgetLimits):  # pragma: no cover
        raise RuntimeError("invalid Bailian budget configuration")
    if not isinstance(permission, DataPermission):  # pragma: no cover
        raise RuntimeError("invalid Bailian data permission configuration")

    cache_path, ledger_path = _runtime_paths()
    ledger = PublicModelLedger(ledger_path)
    today = datetime.now(timezone.utc).date().isoformat()
    initial_batch = ledger.total_spend(currency="CNY", batch_id=batch_id)
    initial_daily = ledger.total_spend(currency="CNY", recorded_date=today)
    budget_guard = BudgetGuard(
        budget_limits,
        batch_id=batch_id,
        initial_batch_spent=initial_batch,
        initial_daily_spent=initial_daily,
    )
    policy = PublicModelPolicy(
        provider=BAILIAN_PROVIDER_ID,
        enabled=True,
        secret=SecretEnvRef(BAILIAN_SECRET_ENV),
        budget_configured=True,
        data_permission=permission,
    )
    registry = ProviderRegistry()
    registry.register(provider)
    runner = PublicModelRunner(
        registry=registry,
        cache=FileResponseCache(cache_path),
        ledger=ledger,
        policy=policy,
        budget_guard=budget_guard,
    )
    permission_record = ProviderDataPermissionRecord(
        allowed_to_leave_local=True,
        authorization_basis=permission.authorization_basis,
        redaction_strategy=permission.redaction_strategy,
        retention_days=permission.retention_days,
        retention_policy_reference=permission.retention_policy_reference,
    )
    return AliyunBailianRuntime(
        provider=provider,
        runner=runner,
        budget_guard=budget_guard,
        data_permission=permission_record,
        allowed_upload_levels=permission.allowed_upload_levels,
        batch_id=batch_id,
    )


def run_fake_provider_smoke(
    *,
    text: str = "G3 provider contract smoke",
    repeat: int = 2,
    batch_id: str | None = None,
) -> dict:
    normalized_text = str(text).strip()
    if not normalized_text:
        raise ValueError("text must not be empty")
    if len(normalized_text) > 4000:
        raise ValueError("text must be at most 4000 characters")
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 5:
        raise ValueError("repeat must be an integer between 1 and 5")

    cache_path, ledger_path = _runtime_paths()
    registry = _registry_with_fake()
    provider = registry.resolve("fake")
    runner = PublicModelRunner(
        registry=registry,
        cache=FileResponseCache(cache_path),
        ledger=PublicModelLedger(ledger_path),
        policy=PublicModelPolicy(),
        budget_guard=None,
    )
    target = provider.descriptor.identity
    content_sha256 = stable_json_sha256({"text": normalized_text})
    request_bytes = len(normalized_text.encode("utf-8"))
    selected_batch_id = str(batch_id or f"fake-smoke-{uuid4().hex[:12]}")
    local_baseline = {
        "status": "local_baseline",
        "label": "unchanged",
        "score": 0.5,
    }
    outcomes = []
    for index in range(repeat):
        request = ProviderRequest(
            request_id=f"{selected_batch_id}-{index + 1}",
            request_type="structured_analysis",
            target=target,
            content_sha256=content_sha256,
            input_size=ProviderInputSize(
                text_characters=len(normalized_text),
                input_tokens=max(1, len(normalized_text) // 4),
                request_bytes=request_bytes,
            ),
            data_permission=ProviderDataPermissionRecord(),
            execution_policy=ProviderExecutionPolicy(
                public_api_enabled=False,
                budget_authorized=False,
                timeout_seconds=5,
                max_retries=0,
            ),
            payload={"summary": normalized_text},
            parameters={"temperature": 0, "mode": "fake_shadow"},
        )
        outcome = runner.execute(
            request,
            estimated_cost=Money(Decimal("0"), "CNY"),
            upload_level=UploadLevel.STRUCTURED_SUMMARY,
            batch_id=selected_batch_id,
            local_baseline=local_baseline,
        )
        outcomes.append(outcome.to_dict())

    network_request_count = sum(int(item["network_request_count"]) for item in outcomes)
    return {
        "contract_version": PUBLIC_MODEL_RUNTIME_CONTRACT_VERSION,
        "status": "simulated",
        "lifecycle_status": "research_only",
        "provider": asdict(provider.descriptor.identity),
        "batch_id": selected_batch_id,
        "repeat": repeat,
        "outcomes": outcomes,
        "cache_hit_count": sum(bool(item["cache_hit"]) for item in outcomes),
        "network_request_count": network_request_count,
        "public_api_call_count": network_request_count,
        "estimated_cost": "0",
        "currency": "CNY",
        "data_left_local": False,
        "public_api_enabled": _public_api_flag(),
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "automatic_publish": False,
        "ledger_call_count": PublicModelLedger(ledger_path).count(),
    }
