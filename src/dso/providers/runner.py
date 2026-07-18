"""Fail-closed orchestration for optional public-model providers.

The runner keeps provider evidence in shadow mode.  It never changes production
weights, writes manual Gold, or replaces the supplied local baseline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import json
from typing import Any, Mapping

from dso.providers.budget import BudgetExceeded, BudgetGuard, Money
from dso.providers.cache import FileResponseCache
from dso.providers.contracts import ProviderCallStatus, ProviderRequest
from dso.providers.ledger import LedgerEntry, PublicModelLedger
from dso.providers.policy import PolicyDenied, PublicModelPolicy, UploadLevel
from dso.providers.registry import ProviderRegistry


PUBLIC_MODEL_RUNNER_CONTRACT_VERSION = "public_model_runner.v1"


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    contract_version: str
    status: str
    request_id: str
    provider: str
    model: str
    provider_output: Mapping[str, Any]
    local_baseline: Mapping[str, Any]
    final_output: Mapping[str, Any]
    final_adoption_reason: str
    cache_hit: bool
    network_request_count: int
    estimated_cost: str
    currency: str
    ledger_call_id: str
    policy_code: str
    production_weight_changed: bool = False
    writes_manual_gold: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PublicModelRunner:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        cache: FileResponseCache,
        ledger: PublicModelLedger,
        policy: PublicModelPolicy | None = None,
        budget_guard: BudgetGuard | None = None,
    ) -> None:
        self.registry = registry
        self.cache = cache
        self.ledger = ledger
        self.policy = policy
        self.budget_guard = budget_guard

    def execute(
        self,
        request: ProviderRequest,
        *,
        estimated_cost: Money,
        upload_level: UploadLevel,
        batch_id: str,
        local_baseline: Mapping[str, Any],
    ) -> RunnerOutcome:
        provider = self.registry.resolve(request.target.provider_id)
        descriptor = provider.descriptor
        if descriptor.identity != request.target:
            return self._fallback(
                request,
                estimated_cost=Money(Decimal("0"), estimated_cost.currency),
                upload_level=upload_level,
                batch_id=batch_id,
                local_baseline=local_baseline,
                ledger_status="policy_rejected",
                policy_code="provider_target_mismatch",
                error_summary="registered provider identity does not match request target",
            )

        if descriptor.uses_public_network:
            gate_error = self._authorize_public_request(request, upload_level)
            if gate_error is not None:
                code, summary = gate_error
                return self._fallback(
                    request,
                    estimated_cost=Money(Decimal("0"), estimated_cost.currency),
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="policy_rejected",
                    policy_code=code,
                    error_summary=summary,
                )
            if self.budget_guard is None:
                return self._fallback(
                    request,
                    estimated_cost=Money(Decimal("0"), estimated_cost.currency),
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="budget_rejected",
                    policy_code="budget_guard_not_configured",
                    error_summary="public provider requires a configured budget guard",
                )
            try:
                self.budget_guard.reserve(estimated_cost)
            except BudgetExceeded as exc:
                return self._fallback(
                    request,
                    estimated_cost=Money(Decimal("0"), estimated_cost.currency),
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="budget_rejected",
                    policy_code=f"budget_{exc.scope}_exhausted",
                    error_summary=str(exc),
                )
        elif estimated_cost.amount != 0:
            raise ValueError("network-free providers must declare zero estimated cost")

        cached = self.cache.get(request.cache_key)
        if cached is not None:
            output = cached.get("output")
            if isinstance(output, dict):
                call_id = self._record(
                    request,
                    batch_id=batch_id,
                    upload_level=upload_level,
                    status="cache_hit",
                    cost=Money(Decimal("0"), estimated_cost.currency),
                    output=output,
                    cache_hit=True,
                    network_request_count=0,
                )
                return RunnerOutcome(
                    contract_version=PUBLIC_MODEL_RUNNER_CONTRACT_VERSION,
                    status="shadow_cached",
                    request_id=request.request_id,
                    provider=request.target.provider_id,
                    model=request.target.model_id,
                    provider_output=output,
                    local_baseline=dict(local_baseline),
                    final_output=dict(local_baseline),
                    final_adoption_reason="cached provider evidence remains shadow-only",
                    cache_hit=True,
                    network_request_count=0,
                    estimated_cost="0",
                    currency=estimated_cost.currency,
                    ledger_call_id=call_id,
                    policy_code="allowed",
                )

        result = None
        try:
            result = provider.invoke(request)
            self._validate_result(request, result)
            if result.status != ProviderCallStatus.SUCCEEDED:
                return self._fallback(
                    request,
                    estimated_cost=Money(
                        result.metrics.estimated_cost,
                        result.metrics.cost_currency,
                    ),
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="fallback",
                    policy_code=f"provider_{result.status.value}",
                    error_summary=result.metrics.error_message or result.metrics.error_code,
                    network_request_count=result.metrics.network_request_count,
                    latency_ms=result.metrics.latency_ms,
                    retry_count=result.metrics.retry_count,
                    request_count=result.metrics.request_count,
                    rate_limit_count=result.metrics.rate_limit_count,
                )
            output = dict(result.output)
            self.cache.put(
                request.cache_key,
                {
                    "contract_version": result.contract_version,
                    "provider": request.target.provider_id,
                    "model": request.target.model_id,
                    "output": output,
                },
            )
            actual_cost = Money(
                result.metrics.estimated_cost,
                result.metrics.cost_currency,
            )
            call_id = self._record(
                request,
                batch_id=batch_id,
                upload_level=upload_level,
                status="success",
                cost=actual_cost,
                output=output,
                cache_hit=result.metrics.cache_hit,
                network_request_count=result.metrics.network_request_count,
                latency_ms=result.metrics.latency_ms,
                output_tokens=result.metrics.output_tokens,
                retry_count=result.metrics.retry_count,
                request_count=result.metrics.request_count,
                rate_limit_count=result.metrics.rate_limit_count,
            )
            return RunnerOutcome(
                contract_version=PUBLIC_MODEL_RUNNER_CONTRACT_VERSION,
                status="shadow_succeeded",
                request_id=request.request_id,
                provider=request.target.provider_id,
                model=request.target.model_id,
                provider_output=output,
                local_baseline=dict(local_baseline),
                final_output=dict(local_baseline),
                final_adoption_reason=result.decision.final_adoption_reason,
                cache_hit=result.metrics.cache_hit,
                network_request_count=result.metrics.network_request_count,
                estimated_cost=str(actual_cost.amount),
                currency=actual_cost.currency,
                ledger_call_id=call_id,
                policy_code="allowed",
            )
        except Exception as exc:
            failure_cost = Money(Decimal("0"), estimated_cost.currency)
            failure_network_requests = 0
            failure_latency_ms = 0.0
            failure_retry_count = 0
            failure_request_count = 1
            failure_rate_limit_count = 0
            if result is not None:
                failure_cost = Money(
                    result.metrics.estimated_cost,
                    result.metrics.cost_currency,
                )
                failure_network_requests = result.metrics.network_request_count
                failure_latency_ms = result.metrics.latency_ms
                failure_retry_count = result.metrics.retry_count
                failure_request_count = result.metrics.request_count
                failure_rate_limit_count = result.metrics.rate_limit_count
            return self._fallback(
                request,
                estimated_cost=failure_cost,
                upload_level=upload_level,
                batch_id=batch_id,
                local_baseline=local_baseline,
                ledger_status="error",
                policy_code="provider_error",
                error_summary=str(exc),
                network_request_count=failure_network_requests,
                latency_ms=failure_latency_ms,
                retry_count=failure_retry_count,
                request_count=failure_request_count,
                rate_limit_count=failure_rate_limit_count,
            )

    def _authorize_public_request(
        self,
        request: ProviderRequest,
        upload_level: UploadLevel,
    ) -> tuple[str, str] | None:
        if not request.execution_policy.public_api_enabled:
            return "request_public_api_disabled", "request execution policy disables public API"
        if not request.execution_policy.budget_authorized:
            return "request_budget_not_authorized", "request execution policy lacks budget authorization"
        if not request.data_permission.allowed_to_leave_local:
            return "request_data_export_not_permitted", "request audit record disallows data export"
        if self.policy is None:
            return "public_policy_not_configured", "public provider requires an explicit policy"
        try:
            self.policy.authorize(upload_level)
        except PolicyDenied as exc:
            return exc.code, str(exc)
        if self.policy.provider != request.target.provider_id:
            return "policy_provider_mismatch", "public policy provider does not match request target"
        return None

    @staticmethod
    def _validate_result(request: ProviderRequest, result: Any) -> None:
        if result.request_id != request.request_id:
            raise ValueError("provider result request_id mismatch")
        if result.request_type != request.request_type:
            raise ValueError("provider result request_type mismatch")
        if result.target != request.target:
            raise ValueError("provider result target mismatch")

    def _fallback(
        self,
        request: ProviderRequest,
        *,
        estimated_cost: Money,
        upload_level: UploadLevel,
        batch_id: str,
        local_baseline: Mapping[str, Any],
        ledger_status: str,
        policy_code: str,
        error_summary: str,
        network_request_count: int = 0,
        latency_ms: float = 0,
        retry_count: int = 0,
        request_count: int = 1,
        rate_limit_count: int = 0,
    ) -> RunnerOutcome:
        call_id = self._record(
            request,
            batch_id=batch_id,
            upload_level=upload_level,
            status=ledger_status,
            cost=estimated_cost,
            output={},
            cache_hit=False,
            network_request_count=network_request_count,
            latency_ms=latency_ms,
            retry_count=retry_count,
            request_count=request_count,
            rate_limit_count=rate_limit_count,
            error_code=policy_code,
            error_summary=error_summary,
        )
        return RunnerOutcome(
            contract_version=PUBLIC_MODEL_RUNNER_CONTRACT_VERSION,
            status="fallback_local",
            request_id=request.request_id,
            provider=request.target.provider_id,
            model=request.target.model_id,
            provider_output={},
            local_baseline=dict(local_baseline),
            final_output=dict(local_baseline),
            final_adoption_reason=f"provider unavailable or denied ({policy_code}); local baseline retained",
            cache_hit=False,
            network_request_count=network_request_count,
            estimated_cost=str(estimated_cost.amount),
            currency=estimated_cost.currency,
            ledger_call_id=call_id,
            policy_code=policy_code,
        )

    def _record(
        self,
        request: ProviderRequest,
        *,
        batch_id: str,
        upload_level: UploadLevel,
        status: str,
        cost: Money,
        output: Mapping[str, Any],
        cache_hit: bool,
        network_request_count: int,
        latency_ms: float = 0,
        output_tokens: int = 0,
        retry_count: int = 0,
        request_count: int = 1,
        rate_limit_count: int = 0,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> str:
        response_bytes = len(
            json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        permission = request.data_permission
        entry = LedgerEntry(
            batch_id=batch_id,
            content_hash=request.content_sha256,
            provider=request.target.provider_id,
            model=request.target.model_id,
            api_version=request.target.api_version,
            prompt_version=request.target.prompt_version,
            request_type=request.request_type,
            status=status,
            input_tokens=request.input_size.input_tokens,
            output_tokens=output_tokens,
            request_bytes=request.input_size.request_bytes,
            response_bytes=response_bytes,
            video_seconds=Decimal(str(request.input_size.video_seconds)),
            audio_seconds=Decimal(str(request.input_size.audio_seconds)),
            frame_count=request.input_size.frame_count,
            image_count=request.input_size.image_count,
            text_chars=request.input_size.text_characters,
            latency_ms=max(0, int(round(latency_ms))),
            retry_count=retry_count,
            request_count=request_count,
            network_request_count=network_request_count,
            rate_limit_count=rate_limit_count,
            cache_hit=cache_hit,
            estimated_cost=cost,
            upload_level=upload_level.value,
            data_allowed=permission.allowed_to_leave_local,
            authorization_basis=permission.authorization_basis,
            redaction_strategy=permission.redaction_strategy,
            retention_days=permission.retention_days or 0,
            error_code=error_code,
            error_summary=error_summary,
        )
        return self.ledger.record(entry)
