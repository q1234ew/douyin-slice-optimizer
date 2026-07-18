"""Application-facing status and zero-network smoke helpers for G3."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

from dso.config import ensure_data_dirs
from dso.providers.budget import Money
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
from dso.providers.policy import PublicModelPolicy, UploadLevel
from dso.providers.registry import ProviderRegistry
from dso.providers.runner import PUBLIC_MODEL_RUNNER_CONTRACT_VERSION, PublicModelRunner


PUBLIC_MODEL_RUNTIME_CONTRACT_VERSION = "public_model_runtime.v1"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _runtime_paths() -> tuple[Path, Path]:
    settings = ensure_data_dirs()
    return (
        settings.cache_dir / "public_models",
        settings.db_dir / "public_model_ledger.sqlite3",
    )


def _registry_with_fake() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    return registry


def _public_api_flag() -> bool:
    return os.environ.get("DSO_PUBLIC_MODEL_API_ENABLED", "").strip().lower() in _TRUE_VALUES


def public_model_status() -> dict:
    cache_path, ledger_path = _runtime_paths()
    registry = _registry_with_fake()
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
        "provider_selected": False,
        "secret_configured": False,
        "budget_configured": False,
        "data_permission_configured": False,
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
        "cache_path": str(cache_path),
        "ledger_path": str(ledger_path),
        "ledger_call_count": ledger_call_count,
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "automatic_publish": False,
        "next_action": (
            "Choose a real provider, verify official API/pricing, then explicitly configure "
            "secret, budgets and data permission before any network adapter is enabled."
        ),
    }


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
