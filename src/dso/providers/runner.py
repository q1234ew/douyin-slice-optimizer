"""Fail-closed orchestration for optional public-model providers.

The runner keeps provider evidence in shadow mode. It never changes production
weights, writes manual Gold, or replaces the supplied local baseline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping

from dso.providers.budget import (
    BudgetExceeded,
    BudgetGuard,
    BudgetReservation,
    Money,
)
from dso.providers.cache import FileResponseCache
from dso.providers.contracts import (
    ProviderBillingStatus,
    ProviderCallMetrics,
    ProviderCallStatus,
    ProviderRequest,
)
from dso.providers.ledger import LedgerAttemptEntry, LedgerEntry, PublicModelLedger
from dso.providers.policy import PolicyDenied, PublicModelPolicy, UploadLevel
from dso.providers.registry import ProviderRegistry


PUBLIC_MODEL_RUNNER_CONTRACT_VERSION = "public_model_runner.v2"


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    """Auditable shadow outcome; ``final_output`` always remains the local baseline."""

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
    preflight_reserved_cost: str
    usage_estimated_cost: str
    currency: str
    billing_status: str
    ledger_call_id: str
    policy_code: str
    production_weight_changed: bool = False
    writes_manual_gold: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PublicModelRunner:
    """Enforce policy, cache, budget, provider, and ledger boundaries in one place.

    Business code must use this runner instead of invoking a network provider
    directly. That keeps public-model evidence shadow-only and guarantees that
    every allowed, denied, cached, or failed request receives a ledger record.
    """

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
        """Run one shadow request while preserving the supplied local decision.

        Ordering is security-sensitive: validate identity and export permission,
        consult the local cache, reserve worst-case budget, invoke the provider,
        settle actual/unknown cost, then persist the audit record. Reordering
        these steps can leak data or permit concurrent budget overspend.
        """

        provider = self.registry.resolve(request.target.provider_id)
        descriptor = provider.descriptor
        zero = Money(Decimal("0"), estimated_cost.currency)
        if descriptor.identity != request.target:
            return self._fallback(
                request,
                preflight_reserved_cost=zero,
                usage_estimated_cost=zero,
                effective_cost=zero,
                billing_status=ProviderBillingStatus.NOT_BILLABLE,
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
                    preflight_reserved_cost=zero,
                    usage_estimated_cost=zero,
                    effective_cost=zero,
                    billing_status=ProviderBillingStatus.NOT_BILLABLE,
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="policy_rejected",
                    policy_code=code,
                    error_summary=summary,
                )
        elif estimated_cost.amount != 0:
            raise ValueError("network-free providers must declare zero estimated cost")

        # Policy and data-export permission are checked before cache access, but
        # a valid local response cache hit must never reserve paid API budget.
        cached = self.cache.get(request.cache_key)
        if cached is not None:
            output = cached.get("output")
            if isinstance(output, dict):
                call_id = self._record(
                    request,
                    batch_id=batch_id,
                    upload_level=upload_level,
                    status="cache_hit",
                    effective_cost=zero,
                    preflight_reserved_cost=zero,
                    usage_estimated_cost=zero,
                    billing_status=ProviderBillingStatus.NOT_BILLABLE,
                    cache_hit=True,
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
                    preflight_reserved_cost="0",
                    usage_estimated_cost="0",
                    currency=estimated_cost.currency,
                    billing_status=ProviderBillingStatus.NOT_BILLABLE.value,
                    ledger_call_id=call_id,
                    policy_code="allowed",
                )

        reservation: BudgetReservation | None = None
        if descriptor.uses_public_network:
            if self.budget_guard is None:
                return self._fallback(
                    request,
                    preflight_reserved_cost=zero,
                    usage_estimated_cost=zero,
                    effective_cost=zero,
                    billing_status=ProviderBillingStatus.NOT_BILLABLE,
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="budget_rejected",
                    policy_code="budget_guard_not_configured",
                    error_summary="public provider requires a configured budget guard",
                )
            try:
                reservation = self.budget_guard.reserve(estimated_cost)
            except BudgetExceeded as exc:
                return self._fallback(
                    request,
                    preflight_reserved_cost=zero,
                    usage_estimated_cost=zero,
                    effective_cost=zero,
                    billing_status=ProviderBillingStatus.NOT_BILLABLE,
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="budget_rejected",
                    policy_code=f"budget_{exc.scope}_exhausted",
                    error_summary=str(exc),
                )
            if reservation.batch_id != batch_id:
                self.budget_guard.release(reservation)
                return self._fallback(
                    request,
                    preflight_reserved_cost=zero,
                    usage_estimated_cost=zero,
                    effective_cost=zero,
                    billing_status=ProviderBillingStatus.NOT_BILLABLE,
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="budget_rejected",
                    policy_code="budget_batch_mismatch",
                    error_summary="budget guard batch does not match runner batch",
                )

        result = None
        # A reservation has exactly one terminal action: release, usage-based
        # settlement, or conservative unknown settlement.
        reservation_finalized = False
        effective_cost = zero
        usage_cost = zero
        billing_status = ProviderBillingStatus.NOT_BILLABLE
        try:
            result = provider.invoke(request)
            self._validate_result(request, result)
            effective_cost, usage_cost, budget_error = self._finalize_budget(
                reservation,
                result.metrics,
            )
            reservation_finalized = True
            billing_status = result.metrics.billing_status
            if budget_error is not None:
                return self._fallback(
                    request,
                    preflight_reserved_cost=(reservation.amount if reservation else zero),
                    usage_estimated_cost=usage_cost,
                    effective_cost=effective_cost,
                    billing_status=billing_status,
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status="budget_rejected",
                    policy_code=f"budget_{budget_error.scope}_actual_exceeded",
                    error_summary=str(budget_error),
                    metrics=result.metrics,
                )

            if result.status != ProviderCallStatus.SUCCEEDED:
                ledger_status = (
                    "rate_limited"
                    if result.status == ProviderCallStatus.RATE_LIMITED
                    else "fallback"
                )
                return self._fallback(
                    request,
                    preflight_reserved_cost=(reservation.amount if reservation else zero),
                    usage_estimated_cost=usage_cost,
                    effective_cost=effective_cost,
                    billing_status=billing_status,
                    upload_level=upload_level,
                    batch_id=batch_id,
                    local_baseline=local_baseline,
                    ledger_status=ledger_status,
                    policy_code=f"provider_{result.status.value}",
                    error_code=result.metrics.error_code,
                    error_summary=result.metrics.error_message or result.metrics.error_code,
                    metrics=result.metrics,
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
            call_id = self._record(
                request,
                batch_id=batch_id,
                upload_level=upload_level,
                status="success",
                effective_cost=effective_cost,
                preflight_reserved_cost=(reservation.amount if reservation else zero),
                usage_estimated_cost=usage_cost,
                billing_status=billing_status,
                cache_hit=result.metrics.cache_hit,
                metrics=result.metrics,
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
                estimated_cost=str(effective_cost.amount),
                preflight_reserved_cost=str(
                    reservation.amount.amount if reservation else Decimal("0")
                ),
                usage_estimated_cost=str(usage_cost.amount),
                currency=effective_cost.currency,
                billing_status=billing_status.value,
                ledger_call_id=call_id,
                policy_code="allowed",
            )
        except Exception as exc:
            metrics = result.metrics if result is not None else None
            if reservation is not None and not reservation_finalized:
                # Once a network-capable provider may have started, absence of
                # usage is not evidence of zero cost. Keep the full reservation.
                if metrics is None:
                    self.budget_guard.settle_unknown(reservation)  # type: ignore[union-attr]
                    effective_cost = reservation.amount
                    billing_status = ProviderBillingStatus.UNKNOWN
                else:
                    try:
                        effective_cost, usage_cost, _ = self._finalize_budget(
                            reservation,
                            metrics,
                        )
                        billing_status = metrics.billing_status
                    except Exception:
                        try:
                            self.budget_guard.settle_unknown(reservation)  # type: ignore[union-attr]
                        except Exception:
                            pass
                        effective_cost = reservation.amount
                        billing_status = ProviderBillingStatus.UNKNOWN
            return self._fallback(
                request,
                preflight_reserved_cost=(reservation.amount if reservation else zero),
                usage_estimated_cost=usage_cost,
                effective_cost=effective_cost,
                billing_status=billing_status,
                upload_level=upload_level,
                batch_id=batch_id,
                local_baseline=local_baseline,
                ledger_status="error",
                policy_code="provider_error",
                error_summary=str(exc),
                metrics=metrics,
            )

    def _finalize_budget(
        self,
        reservation: BudgetReservation | None,
        metrics: ProviderCallMetrics,
    ) -> tuple[Money, Money, BudgetExceeded | None]:
        """Convert provider billing evidence into the only valid reservation outcome."""

        usage_cost = Money(metrics.estimated_cost, metrics.cost_currency)
        if reservation is None:
            return usage_cost, usage_cost, None
        if self.budget_guard is None:  # pragma: no cover - guarded by execute
            raise RuntimeError("budget guard disappeared after reservation")

        if metrics.billing_status == ProviderBillingStatus.NOT_BILLABLE:
            self.budget_guard.release(reservation)
            zero = Money(Decimal("0"), reservation.amount.currency)
            return zero, usage_cost, None
        if metrics.billing_status == ProviderBillingStatus.UNKNOWN:
            self.budget_guard.settle_unknown(reservation)
            return reservation.amount, usage_cost, None
        try:
            self.budget_guard.settle(reservation, usage_cost)
        except BudgetExceeded as exc:
            return usage_cost, usage_cost, exc
        return usage_cost, usage_cost, None

    def _authorize_public_request(
        self,
        request: ProviderRequest,
        upload_level: UploadLevel,
    ) -> tuple[str, str] | None:
        """Require request-time permission to exactly match configured policy.

        The immutable request snapshot prevents a queued request from being
        executed after authorization, redaction, or retention policy changes.
        """

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

        configured = self.policy.data_permission
        audited = request.data_permission
        permission_fields_match = (
            configured.authorization_basis == audited.authorization_basis
            and configured.redaction_strategy == audited.redaction_strategy
            and configured.retention_days == audited.retention_days
            and configured.retention_policy_reference
            == audited.retention_policy_reference
        )
        if not permission_fields_match:
            return (
                "request_data_permission_mismatch",
                "request data permission audit snapshot does not match configured policy",
            )
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
        preflight_reserved_cost: Money,
        usage_estimated_cost: Money,
        effective_cost: Money,
        billing_status: ProviderBillingStatus,
        upload_level: UploadLevel,
        batch_id: str,
        local_baseline: Mapping[str, Any],
        ledger_status: str,
        policy_code: str,
        error_summary: str,
        error_code: str | None = None,
        metrics: ProviderCallMetrics | None = None,
    ) -> RunnerOutcome:
        """Record failure/denial and return the local baseline without adoption."""

        call_id = self._record(
            request,
            batch_id=batch_id,
            upload_level=upload_level,
            status=ledger_status,
            effective_cost=effective_cost,
            preflight_reserved_cost=preflight_reserved_cost,
            usage_estimated_cost=usage_estimated_cost,
            billing_status=billing_status,
            cache_hit=False,
            metrics=metrics,
            error_code=error_code or policy_code,
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
            final_adoption_reason=(
                f"provider unavailable or denied ({policy_code}); local baseline retained"
            ),
            cache_hit=False,
            network_request_count=(metrics.network_request_count if metrics else 0),
            estimated_cost=str(effective_cost.amount),
            preflight_reserved_cost=str(preflight_reserved_cost.amount),
            usage_estimated_cost=str(usage_estimated_cost.amount),
            currency=effective_cost.currency,
            billing_status=billing_status.value,
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
        effective_cost: Money,
        preflight_reserved_cost: Money,
        usage_estimated_cost: Money,
        billing_status: ProviderBillingStatus,
        cache_hit: bool,
        metrics: ProviderCallMetrics | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> str:
        """Persist bounded operational metadata, never request or response bodies."""

        permission = request.data_permission
        size = metrics.input_size if metrics is not None else request.input_size
        entry = LedgerEntry(
            batch_id=batch_id,
            content_hash=request.content_sha256,
            provider=request.target.provider_id,
            model=request.target.model_id,
            api_version=request.target.api_version,
            prompt_version=request.target.prompt_version,
            request_type=request.request_type,
            status=status,
            input_tokens=size.input_tokens,
            output_tokens=(metrics.output_tokens if metrics else 0),
            request_bytes=size.request_bytes,
            response_bytes=(metrics.response_bytes if metrics else 0),
            video_seconds=Decimal(str(size.video_seconds)),
            audio_seconds=Decimal(str(size.audio_seconds)),
            frame_count=size.frame_count,
            image_count=size.image_count,
            text_chars=size.text_characters,
            latency_ms=max(0, int(round(metrics.latency_ms if metrics else 0))),
            retry_count=(metrics.retry_count if metrics else 0),
            request_count=(metrics.request_count if metrics else 1),
            network_request_count=(metrics.network_request_count if metrics else 0),
            rate_limit_count=(metrics.rate_limit_count if metrics else 0),
            provider_cached_input_tokens=(
                metrics.provider_cached_input_tokens if metrics else 0
            ),
            cache_hit=cache_hit,
            estimated_cost=effective_cost,
            preflight_reserved_cost=preflight_reserved_cost,
            usage_estimated_cost=usage_estimated_cost,
            billing_status=billing_status.value,
            pricing_version=(metrics.pricing_version if metrics else ""),
            provider_request_id=(metrics.provider_request_id if metrics else ""),
            upload_level=upload_level.value,
            data_allowed=permission.allowed_to_leave_local,
            authorization_basis=permission.authorization_basis,
            redaction_strategy=permission.redaction_strategy,
            retention_days=permission.retention_days or 0,
            retention_days_known=permission.retention_days is not None,
            retention_policy_reference=permission.retention_policy_reference,
            error_code=error_code or (metrics.error_code if metrics else None),
            error_summary=error_summary,
        )
        attempts = tuple(
            LedgerAttemptEntry.from_provider_metrics(item)
            for item in (metrics.attempts if metrics else ())
        )
        return self.ledger.record(entry, attempts=attempts)
