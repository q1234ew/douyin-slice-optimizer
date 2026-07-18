"""Decimal-based, currency-safe public API budget enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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


@dataclass(frozen=True, slots=True)
class Money:
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
    reservation_id: str
    amount: Money
    batch_id: str
    day: date


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    batch_id: str
    day: date
    batch_spent: Money
    daily_spent: Money
    batch_remaining: Money
    daily_remaining: Money


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
        today: Callable[[], date] = date.today,
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
            return BudgetReservation(
                reservation_id=uuid4().hex,
                amount=estimated_cost,
                batch_id=self._batch_id,
                day=self._day,
            )

    def begin_batch(self, batch_id: str) -> None:
        if not batch_id.strip():
            raise ValueError("batch_id is required")
        with self._lock:
            self._roll_day()
            self._batch_id = batch_id
            self._batch_spent = Decimal("0")

    def snapshot(self) -> BudgetSnapshot:
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
            )
