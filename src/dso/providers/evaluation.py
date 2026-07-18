"""Vendor-neutral shadow evaluation for optional public-model providers.

This module only compares provider observations with an existing local baseline.
It does not expose a production weight or make promotion decisions.  Missing
quality and severe-error labels remain missing instead of being coerced to zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Iterable


SHADOW_EVALUATION_CONTRACT_VERSION = "public_model_shadow_evaluation.v1"
SHADOW_EVALUATION_STATUS = "research_only"

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def _finite_optional(value: float | None, *, field_name: str) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite when provided")
    return normalized


def _non_negative_optional(value: float | None, *, field_name: str) -> float | None:
    normalized = _finite_optional(value, field_name=field_name)
    if normalized is not None and normalized < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return normalized


def _decimal_cost(value: Decimal | int | float | str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("cost must be a finite non-negative number") from exc
    if not normalized.is_finite() or normalized < 0:
        raise ValueError("cost must be a finite non-negative number")
    return normalized


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """One candidate's local-baseline versus public-provider shadow result.

    ``success`` describes whether a usable provider response was returned.
    Successful responses without both quality values are deliberately excluded
    from comparative coverage.  Severe-error rates are evaluated only when both
    explicit severe-error labels are present.
    """

    candidate_id: str
    success: bool
    baseline_quality: float | None = None
    provider_quality: float | None = None
    baseline_severe_error: bool | None = None
    provider_severe_error: bool | None = None
    latency_ms: float | None = None
    cache_hit: bool = False
    cost: Decimal | int | float | str = Decimal("0")
    currency: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        candidate_id = str(self.candidate_id).strip()
        if not candidate_id:
            raise ValueError("candidate_id must not be empty")

        baseline_quality = _finite_optional(
            self.baseline_quality,
            field_name="baseline_quality",
        )
        provider_quality = _finite_optional(
            self.provider_quality,
            field_name="provider_quality",
        )
        latency_ms = _non_negative_optional(self.latency_ms, field_name="latency_ms")
        cost = _decimal_cost(self.cost)

        currency = None
        if self.currency is not None:
            currency = str(self.currency).strip().upper()
            if not _CURRENCY_PATTERN.fullmatch(currency):
                raise ValueError("currency must be a three-letter ISO-style code")
        if cost > 0 and currency is None:
            raise ValueError("currency is required when cost is greater than zero")

        failure_reason = None
        if self.failure_reason is not None:
            failure_reason = str(self.failure_reason).strip() or None

        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "baseline_quality", baseline_quality)
        object.__setattr__(self, "provider_quality", provider_quality)
        object.__setattr__(self, "latency_ms", latency_ms)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "failure_reason", failure_reason)


@dataclass(frozen=True, slots=True)
class ShadowEvaluationReport:
    """JSON-friendly aggregate metrics for a shadow-only provider comparison."""

    contract_version: str
    status: str
    production_weight_changed: bool
    sample_count: int
    successful_count: int
    failed_count: int
    evaluated_count: int
    coverage_rate: float
    win_count: int
    tie_count: int
    loss_count: int
    baseline_severe_error_count: int
    provider_severe_error_count: int
    severe_error_evaluated_count: int
    baseline_severe_error_rate: float | None
    provider_severe_error_rate: float | None
    average_quality_delta: float | None
    latency_sample_count: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    failure_rate: float
    cache_hit_count: int
    cache_hit_rate: float
    total_cost: float
    effective_candidate_cost: float | None
    currency: str | None

    def to_dict(self) -> dict[str, object]:
        """Return a serialization-ready representation of this report."""

        return asdict(self)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _optional_rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    """Return an R-7/NumPy-style linearly interpolated percentile."""

    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def aggregate_shadow_observations(
    observations: Iterable[ShadowObservation],
    *,
    tie_tolerance: float = 1e-9,
) -> ShadowEvaluationReport:
    """Aggregate provider shadow results without changing production behavior.

    Costs from successful and failed requests are both included.  Cost per
    effective candidate uses the same successfully compared population as the
    coverage and win/tie/loss metrics.  Declared currencies must be homogeneous;
    this function never performs an implicit foreign-exchange conversion.
    """

    tie_tolerance = float(tie_tolerance)
    if not math.isfinite(tie_tolerance) or tie_tolerance < 0:
        raise ValueError("tie_tolerance must be a finite non-negative number")

    rows = list(observations)
    if not all(isinstance(row, ShadowObservation) for row in rows):
        raise TypeError("observations must contain only ShadowObservation values")

    currencies = {row.currency for row in rows if row.currency is not None}
    if len(currencies) > 1:
        joined = ", ".join(sorted(currencies))
        raise ValueError(f"mixed currencies are not supported: {joined}")
    currency = next(iter(currencies), None)

    successful = [row for row in rows if row.success]
    evaluated = [
        row
        for row in successful
        if row.baseline_quality is not None and row.provider_quality is not None
    ]
    severe_error_evaluated = [
        row
        for row in successful
        if row.baseline_severe_error is not None and row.provider_severe_error is not None
    ]

    deltas = [
        float(row.provider_quality) - float(row.baseline_quality)
        for row in evaluated
    ]
    win_count = sum(delta > tie_tolerance for delta in deltas)
    loss_count = sum(delta < -tie_tolerance for delta in deltas)
    tie_count = len(deltas) - win_count - loss_count

    baseline_severe_error_count = sum(
        row.baseline_severe_error is True for row in severe_error_evaluated
    )
    provider_severe_error_count = sum(
        row.provider_severe_error is True for row in severe_error_evaluated
    )
    latencies = [float(row.latency_ms) for row in rows if row.latency_ms is not None]
    total_cost_decimal = sum((row.cost for row in rows), Decimal("0"))

    sample_count = len(rows)
    evaluated_count = len(evaluated)
    failed_count = sample_count - len(successful)

    return ShadowEvaluationReport(
        contract_version=SHADOW_EVALUATION_CONTRACT_VERSION,
        status=SHADOW_EVALUATION_STATUS,
        production_weight_changed=False,
        sample_count=sample_count,
        successful_count=len(successful),
        failed_count=failed_count,
        evaluated_count=evaluated_count,
        coverage_rate=_rate(evaluated_count, sample_count),
        win_count=win_count,
        tie_count=tie_count,
        loss_count=loss_count,
        baseline_severe_error_count=baseline_severe_error_count,
        provider_severe_error_count=provider_severe_error_count,
        severe_error_evaluated_count=len(severe_error_evaluated),
        baseline_severe_error_rate=_optional_rate(
            baseline_severe_error_count,
            len(severe_error_evaluated),
        ),
        provider_severe_error_rate=_optional_rate(
            provider_severe_error_count,
            len(severe_error_evaluated),
        ),
        average_quality_delta=(sum(deltas) / len(deltas)) if deltas else None,
        latency_sample_count=len(latencies),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        failure_rate=_rate(failed_count, sample_count),
        cache_hit_count=sum(row.cache_hit for row in rows),
        cache_hit_rate=_rate(sum(row.cache_hit for row in rows), sample_count),
        total_cost=float(total_cost_decimal),
        effective_candidate_cost=(
            float(total_cost_decimal / evaluated_count) if evaluated_count else None
        ),
        currency=currency,
    )

