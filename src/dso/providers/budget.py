"""Decimal-based, currency-safe public API budget enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Callable
from uuid import uuid4


def _decimal(value: Decimal | int | str, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be Decimal, int, or decimal string; floats are forbidden")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal amount") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _currency(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError("currency must be a three-letter code such as CNY")
    return normalized


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True, slots=True)
class Money:
    """Exact non-negative money value; floats are deliberately rejected."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount = _decimal(self.amount, field_name="amount")
        if amount < 0:
            raise ValueError("amount must be non-negative")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", _currency(self.currency))


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Nested hard limits sharing one currency: request <= batch <= day by policy."""

    per_request: Money
    per_batch: Money
    per_day: Money

    def __post_init__(self) -> None:
        currencies = {self.per_request.currency, self.per_batch.currency, self.per_day.currency}
        if len(currencies) != 1:
            raise ValueError("all budget limits must use the same currency")

    @property
    def currency(self) -> str:
        return self.per_request.currency


class BudgetExceeded(RuntimeError):
    """Identifies the limit scope that rejected a reservation or settlement."""

    def __init__(
        self,
        scope: str,
        *,
        requested: Money,
        spent: Money,
        limit: Money,
    ) -> None:
        self.scope = scope
        self.requested = requested
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"{scope} budget exhausted: spent={spent.amount} requested={requested.amount} "
            f"limit={limit.amount} {limit.currency}"
        )


class CurrencyMismatch(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Worst-case cost counted before a network request is allowed to start."""

    reservation_id: str
    amount: Money
    batch_id: str
    day: date


@dataclass(frozen=True, slots=True)
class BudgetSettlement:
    """Final accounting that replaces exactly one active reservation."""

    reservation_id: str
    reserved: Money
    actual: Money
    released: Money
    batch_id: str
    day: date


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Read-only view used for status reporting; it does not reserve funds."""

    batch_id: str
    day: date
    batch_spent: Money
    daily_spent: Money
    batch_remaining: Money
    daily_remaining: Money
    active_reservation_count: int


class BudgetGuard:
    """Thread-safe preflight reservation guard.

    ``reserve`` must run before the provider network call.  Reservations count
    immediately so concurrent requests cannot collectively overspend.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        batch_id: str = "default",
        today: Callable[[], date] = _utc_today,
        initial_batch_spent: Money | None = None,
        initial_daily_spent: Money | None = None,
    ) -> None:
        self.limits = limits
        self._batch_id = batch_id
        self._today = today
        self._day = today()
        for amount in (initial_batch_spent, initial_daily_spent):
            if amount is not None and amount.currency != limits.currency:
                raise CurrencyMismatch(
                    f"budget currency is {limits.currency}, got {amount.currency}"
                )
        self._batch_spent = (
            initial_batch_spent.amount if initial_batch_spent is not None else Decimal("0")
        )
        self._daily_spent = (
            initial_daily_spent.amount if initial_daily_spent is not None else Decimal("0")
        )
        self._reservations: dict[str, BudgetReservation] = {}
        self._lock = Lock()

    def _roll_day(self) -> None:
        current = self._today()
        if current != self._day:
            self._day = current
            self._daily_spent = Decimal("0")
            self._batch_spent = Decimal("0")

    def _money(self, amount: Decimal) -> Money:
        return Money(amount, self.limits.currency)

    def _check_currency(self, money: Money) -> None:
        if money.currency != self.limits.currency:
            raise CurrencyMismatch(
                f"budget currency is {self.limits.currency}, got {money.currency}"
            )

    def reserve(self, estimated_cost: Money) -> BudgetReservation:
        """Atomically reserve cost or reject without making an API call."""

        self._check_currency(estimated_cost)
        with self._lock:
            self._roll_day()
            checks = (
                ("per_request", Decimal("0"), self.limits.per_request),
                ("per_batch", self._batch_spent, self.limits.per_batch),
                ("per_day", self._daily_spent, self.limits.per_day),
            )
            for scope, spent, limit in checks:
                if spent + estimated_cost.amount > limit.amount:
                    raise BudgetExceeded(
                        scope,
                        requested=estimated_cost,
                        spent=self._money(spent),
                        limit=limit,
                    )
            self._batch_spent += estimated_cost.amount
            self._daily_spent += estimated_cost.amount
            reservation = BudgetReservation(
                reservation_id=uuid4().hex,
                amount=estimated_cost,
                batch_id=self._batch_id,
                day=self._day,
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def settle(
        self,
        reservation: BudgetReservation,
        actual_cost: Money,
    ) -> BudgetSettlement:
        """Replace a preflight reservation with a usage-based cost.

        The actual cost is accounted even when it exceeds the reservation.  In
        that case this method raises ``BudgetExceeded`` after updating the
        counters, so callers can fail closed without allowing later requests to
        spend against stale optimistic totals.
        """

        self._check_currency(actual_cost)
        with self._lock:
            self._roll_day()
            active = self._reservations.get(reservation.reservation_id)
            if active != reservation:
                raise ValueError("budget reservation is unknown, stale, or already settled")
            if reservation.batch_id != self._batch_id:
                raise ValueError("budget reservation belongs to a different active batch")
            self._reservations.pop(reservation.reservation_id)

            if reservation.day == self._day:
                self._daily_spent = max(
                    Decimal("0"), self._daily_spent - reservation.amount.amount
                )
                if reservation.batch_id == self._batch_id:
                    self._batch_spent = max(
                        Decimal("0"), self._batch_spent - reservation.amount.amount
                    )

            self._daily_spent += actual_cost.amount
            if reservation.batch_id == self._batch_id:
                self._batch_spent += actual_cost.amount

            settlement = BudgetSettlement(
                reservation_id=reservation.reservation_id,
                reserved=reservation.amount,
                actual=actual_cost,
                released=self._money(
                    max(Decimal("0"), reservation.amount.amount - actual_cost.amount)
                ),
                batch_id=reservation.batch_id,
                day=reservation.day,
            )

            checks = (
                ("reservation", Decimal("0"), reservation.amount),
                ("per_request", Decimal("0"), self.limits.per_request),
                ("per_batch", Decimal("0"), self.limits.per_batch),
                ("per_day", Decimal("0"), self.limits.per_day),
            )
            actuals = (
                actual_cost.amount,
                actual_cost.amount,
                self._batch_spent,
                self._daily_spent,
            )
            for (scope, spent, limit), actual in zip(checks, actuals, strict=True):
                if actual > limit.amount:
                    raise BudgetExceeded(
                        scope,
                        requested=self._money(actual),
                        spent=self._money(spent),
                        limit=limit,
                    )
            return settlement

    def release(self, reservation: BudgetReservation) -> BudgetSettlement:
        """Release a reservation only when no billable network attempt occurred."""

        with self._lock:
            self._roll_day()
            active = self._reservations.pop(reservation.reservation_id, None)
            if active != reservation:
                raise ValueError("budget reservation is unknown, stale, or already settled")
            if reservation.day == self._day:
                self._daily_spent = max(
                    Decimal("0"), self._daily_spent - reservation.amount.amount
                )
                if reservation.batch_id == self._batch_id:
                    self._batch_spent = max(
                        Decimal("0"), self._batch_spent - reservation.amount.amount
                    )
            return BudgetSettlement(
                reservation_id=reservation.reservation_id,
                reserved=reservation.amount,
                actual=self._money(Decimal("0")),
                released=reservation.amount,
                batch_id=reservation.batch_id,
                day=reservation.day,
            )

    def settle_unknown(self, reservation: BudgetReservation) -> BudgetSettlement:
        """Conservatively charge the full reservation when billing is unknown."""

        return self.settle(reservation, reservation.amount)

    def begin_batch(self, batch_id: str) -> None:
        """Start a new batch only after every prior reservation is finalized."""

        if not batch_id.strip():
            raise ValueError("batch_id is required")
        with self._lock:
            self._roll_day()
            if self._reservations:
                raise RuntimeError("cannot begin a new batch with active budget reservations")
            self._batch_id = batch_id
            self._batch_spent = Decimal("0")

    def refresh_persisted_spend(
        self,
        *,
        batch_id: str,
        batch_spent: Money,
        daily_spent: Money,
    ) -> None:
        """Merge ledger totals before a cross-process serialized reservation.

        The caller must hold the shared ledger execution lock.  ``max`` keeps
        this guard conservative if a prior local settlement could not yet be
        reconstructed from the ledger; persisted totals can raise, but never
        lower, an in-memory counter.
        """

        self._check_currency(batch_spent)
        self._check_currency(daily_spent)
        if not batch_id.strip():
            raise ValueError("batch_id is required")
        with self._lock:
            self._roll_day()
            if self._reservations:
                raise RuntimeError("cannot refresh persisted spend with active reservations")
            if batch_id != self._batch_id:
                raise ValueError("persisted spend batch does not match the active budget batch")
            self._batch_spent = max(self._batch_spent, batch_spent.amount)
            self._daily_spent = max(self._daily_spent, daily_spent.amount)

    def snapshot(self) -> BudgetSnapshot:
        """Return counters under the same lock used by reserve and settle."""

        with self._lock:
            self._roll_day()
            return BudgetSnapshot(
                batch_id=self._batch_id,
                day=self._day,
                batch_spent=self._money(self._batch_spent),
                daily_spent=self._money(self._daily_spent),
                batch_remaining=self._money(
                    max(Decimal("0"), self.limits.per_batch.amount - self._batch_spent)
                ),
                daily_remaining=self._money(
                    max(Decimal("0"), self.limits.per_day.amount - self._daily_spent)
                ),
                active_reservation_count=len(self._reservations),
            )
