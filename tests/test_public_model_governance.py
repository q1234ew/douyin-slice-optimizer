from __future__ import annotations

from datetime import date
from decimal import Decimal
import sqlite3

import pytest

from dso.providers.budget import (
    BudgetExceeded,
    BudgetGuard,
    BudgetLimits,
    CurrencyMismatch,
    Money,
)
from dso.providers.cache import FileResponseCache, UnsafeCacheData, build_cache_key
from dso.providers.ledger import LedgerEntry, PublicModelLedger
from dso.providers.policy import (
    DataPermission,
    PolicyDenied,
    PublicModelPolicy,
    SecretEnvRef,
    UploadLevel,
)


def _permission(*levels: UploadLevel) -> DataPermission:
    return DataPermission(
        may_leave_local=True,
        authorization_basis="owned media; public-model processing approved for this task",
        allowed_upload_levels=frozenset(levels),
        redaction_strategy="remove account identifiers before upload",
        retention_days=0,
        retention_policy_reference="test-contract://no-retention.v1",
    )


def _limits(
    per_request: str = "1.00", per_batch: str = "2.00", per_day: str = "3.00"
) -> BudgetLimits:
    return BudgetLimits(
        per_request=Money(Decimal(per_request), "CNY"),
        per_batch=Money(Decimal(per_batch), "CNY"),
        per_day=Money(Decimal(per_day), "CNY"),
    )


def test_public_models_are_fail_closed_by_default() -> None:
    policy = PublicModelPolicy()

    decision = policy.decision(UploadLevel.STRUCTURED_SUMMARY)

    assert decision.allowed is False
    assert decision.code == "public_models_disabled"
    with pytest.raises(PolicyDenied, match="public_models_disabled"):
        policy.authorize(UploadLevel.STRUCTURED_SUMMARY)


def test_policy_requires_env_secret_budget_permission_and_exact_upload_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "do-not-persist-this-secret"
    monkeypatch.setenv("DSO_TEST_PROVIDER_KEY", secret_value)
    secret = SecretEnvRef("DSO_TEST_PROVIDER_KEY")
    policy = PublicModelPolicy(
        provider="fake-provider",
        enabled=True,
        secret=secret,
        budget_configured=True,
        data_permission=_permission(UploadLevel.STRUCTURED_SUMMARY),
    )

    allowed = policy.authorize(UploadLevel.STRUCTURED_SUMMARY)
    denied = policy.decision(UploadLevel.FULL_MEDIA)

    assert allowed.allowed is True
    assert denied.code == "upload_level_not_permitted"
    assert secret.resolve() == secret_value
    assert secret_value not in repr(secret)
    assert secret_value not in repr(policy)


def test_policy_rejects_implicit_data_permission_and_missing_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="authorization_basis"):
        DataPermission(
            may_leave_local=True,
            allowed_upload_levels=frozenset({UploadLevel.STRUCTURED_SUMMARY}),
            redaction_strategy="none required",
        )
    with pytest.raises(ValueError, match="cannot be allowed"):
        DataPermission(
            allowed_upload_levels=frozenset({UploadLevel.STRUCTURED_SUMMARY})
        )

    monkeypatch.setenv("DSO_TEST_PROVIDER_KEY", "configured")
    no_budget = PublicModelPolicy(
        provider="fake-provider",
        enabled=True,
        secret=SecretEnvRef("DSO_TEST_PROVIDER_KEY"),
        data_permission=_permission(UploadLevel.STRUCTURED_SUMMARY),
    )
    assert no_budget.decision(UploadLevel.STRUCTURED_SUMMARY).code == "budget_not_configured"


def test_budget_uses_decimal_and_requires_one_currency() -> None:
    with pytest.raises(TypeError, match="floats are forbidden"):
        Money(0.1, "CNY")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="same currency"):
        BudgetLimits(
            per_request=Money(Decimal("1"), "CNY"),
            per_batch=Money(Decimal("2"), "USD"),
            per_day=Money(Decimal("3"), "CNY"),
        )

    guard = BudgetGuard(_limits())
    with pytest.raises(CurrencyMismatch, match="budget currency is CNY"):
        guard.reserve(Money(Decimal("0.1"), "USD"))


def test_budget_enforces_request_batch_and_day_before_network_call() -> None:
    guard = BudgetGuard(_limits(per_request="1", per_batch="1.5", per_day="2"))

    with pytest.raises(BudgetExceeded) as request_error:
        guard.reserve(Money(Decimal("1.01"), "CNY"))
    assert request_error.value.scope == "per_request"
    assert guard.snapshot().daily_spent.amount == Decimal("0")

    first = guard.reserve(Money(Decimal("1.00"), "CNY"))
    guard.settle(first, Money(Decimal("1.00"), "CNY"))
    second = guard.reserve(Money(Decimal("0.50"), "CNY"))
    guard.settle(second, Money(Decimal("0.50"), "CNY"))
    with pytest.raises(BudgetExceeded) as batch_error:
        guard.reserve(Money(Decimal("0.01"), "CNY"))
    assert batch_error.value.scope == "per_batch"

    guard.begin_batch("batch-2")
    third = guard.reserve(Money(Decimal("0.50"), "CNY"))
    guard.settle(third, Money(Decimal("0.50"), "CNY"))
    with pytest.raises(BudgetExceeded) as day_error:
        guard.reserve(Money(Decimal("0.01"), "CNY"))
    assert day_error.value.scope == "per_day"
    assert guard.snapshot().daily_remaining.amount == Decimal("0.00")


def test_budget_can_resume_persisted_batch_and_daily_spend() -> None:
    guard = BudgetGuard(
        _limits(per_request="1", per_batch="2", per_day="3"),
        batch_id="resumed",
        initial_batch_spent=Money(Decimal("1.75"), "CNY"),
        initial_daily_spent=Money(Decimal("2.75"), "CNY"),
    )

    guard.reserve(Money(Decimal("0.25"), "CNY"))
    with pytest.raises(BudgetExceeded) as error:
        guard.reserve(Money(Decimal("0.01"), "CNY"))
    assert error.value.scope == "per_batch"


def test_budget_settlement_releases_unused_reservation_and_accounts_actual_overrun() -> None:
    guard = BudgetGuard(_limits(per_request="1", per_batch="2", per_day="3"))
    reservation = guard.reserve(Money(Decimal("0.80"), "CNY"))

    settlement = guard.settle(reservation, Money(Decimal("0.25"), "CNY"))

    assert settlement.released.amount == Decimal("0.55")
    assert guard.snapshot().batch_spent.amount == Decimal("0.25")
    assert guard.snapshot().active_reservation_count == 0

    released = guard.reserve(Money(Decimal("0.50"), "CNY"))
    guard.release(released)
    assert guard.snapshot().batch_spent.amount == Decimal("0.25")

    overrun = guard.reserve(Money(Decimal("0.10"), "CNY"))
    with pytest.raises(BudgetExceeded) as error:
        guard.settle(overrun, Money(Decimal("0.20"), "CNY"))
    assert error.value.scope == "reservation"
    assert guard.snapshot().batch_spent.amount == Decimal("0.45")


def test_budget_rejects_cross_batch_settlement_without_losing_reservation() -> None:
    guard = BudgetGuard(
        _limits(per_request="1", per_batch="2", per_day="3"),
        batch_id="batch-a",
    )
    reservation = guard.reserve(Money(Decimal("0.50"), "CNY"))

    # Defensive corruption simulation: public APIs cannot normally switch a
    # batch while reservations are active, but settlement must still fail safe.
    guard._batch_id = "batch-b"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="different active batch"):
        guard.settle(reservation, Money(Decimal("0.25"), "CNY"))

    assert guard.snapshot().active_reservation_count == 1
    guard._batch_id = "batch-a"  # type: ignore[attr-defined]
    guard.release(reservation)
    assert guard.snapshot().active_reservation_count == 0


def test_budget_refresh_merges_persisted_totals_conservatively() -> None:
    guard = BudgetGuard(
        _limits(per_request="1", per_batch="2", per_day="3"),
        batch_id="shared-batch",
        initial_batch_spent=Money(Decimal("0.40"), "CNY"),
        initial_daily_spent=Money(Decimal("0.60"), "CNY"),
    )

    guard.refresh_persisted_spend(
        batch_id="shared-batch",
        batch_spent=Money(Decimal("0.75"), "CNY"),
        daily_spent=Money(Decimal("1.25"), "CNY"),
    )
    guard.refresh_persisted_spend(
        batch_id="shared-batch",
        batch_spent=Money(Decimal("0.10"), "CNY"),
        daily_spent=Money(Decimal("0.10"), "CNY"),
    )

    snapshot = guard.snapshot()
    assert snapshot.batch_spent.amount == Decimal("0.75")
    assert snapshot.daily_spent.amount == Decimal("1.25")


def test_daily_budget_rolls_over_but_batch_and_daily_are_reset() -> None:
    days = [date(2026, 7, 18)]
    guard = BudgetGuard(_limits(), today=lambda: days[0])
    guard.reserve(Money(Decimal("0.75"), "CNY"))

    days[0] = date(2026, 7, 19)
    snapshot = guard.snapshot()

    assert snapshot.day == date(2026, 7, 19)
    assert snapshot.batch_spent.amount == Decimal("0")
    assert snapshot.daily_spent.amount == Decimal("0")


def test_cache_key_is_deterministic_and_covers_all_contract_fields() -> None:
    base = dict(
        content_hash="sha256:content",
        provider="fake-provider",
        model="fake-model",
        api_version="2026-07-18",
        prompt_version="material-review.v1",
    )
    first = build_cache_key(**base, parameters={"temperature": Decimal("0.1"), "top_p": 1})
    reordered = build_cache_key(
        **base, parameters={"top_p": 1, "temperature": Decimal("0.1")}
    )

    assert first == reordered
    for changed in (
        {**base, "content_hash": "sha256:other"},
        {**base, "provider": "other-provider"},
        {**base, "model": "other-model"},
        {**base, "api_version": "v2"},
        {**base, "prompt_version": "material-review.v2"},
    ):
        assert build_cache_key(**changed, parameters={"temperature": Decimal("0.1"), "top_p": 1}) != first


def test_cache_writes_atomically_and_refuses_secrets_prompt_or_binary_media(tmp_path) -> None:
    cache = FileResponseCache(tmp_path / "cache")
    key = build_cache_key(
        content_hash="sha256:content",
        provider="fake-provider",
        model="fake-model",
        api_version="v1",
        prompt_version="prompt.v1",
        parameters={"temperature": 0},
    )

    path = cache.put(key, {"schema_version": "result.v1", "score": 0.7})

    assert cache.get(key) == {"schema_version": "result.v1", "score": 0.7}
    assert path.exists()
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(UnsafeCacheData, match="api_key"):
        cache.put(key, {"api_key": "secret"})
    with pytest.raises(UnsafeCacheData, match="prompt"):
        build_cache_key(
            content_hash="sha256:content",
            provider="fake-provider",
            model="fake-model",
            api_version="v1",
            prompt_version="prompt.v1",
            parameters={"prompt": "prompt body must never be a parameter"},
        )
    with pytest.raises(UnsafeCacheData, match="binary media"):
        cache.put(key, {"media": b"raw-video"})


def test_ledger_records_only_safe_fixed_fields_and_preserves_decimal_cost(tmp_path) -> None:
    db_path = tmp_path / "public-model-ledger.sqlite3"
    ledger = PublicModelLedger(db_path)
    secret = "secret-value-that-must-not-be-stored"
    entry = LedgerEntry(
        call_id="call-1",
        batch_id="batch-1",
        content_hash="sha256:content",
        provider="fake-provider",
        model="fake-model",
        api_version="v1",
        prompt_version="review.v1",
        request_type="structured_summary",
        status="error",
        input_tokens=101,
        output_tokens=7,
        request_bytes=2048,
        response_bytes=128,
        video_seconds=Decimal("3.25"),
        audio_seconds=Decimal("2.50"),
        frame_count=3,
        image_count=3,
        text_chars=99,
        latency_ms=321,
        retry_count=1,
        request_count=2,
        network_request_count=2,
        rate_limit_count=1,
        cache_hit=False,
        upload_level="structured_summary",
        data_allowed=True,
        authorization_basis="owned_media",
        redaction_strategy="remove_account_identifiers",
        retention_days=0,
        retention_days_known=True,
        retention_policy_reference="contract://owned-media.v1",
        preflight_reserved_cost=Money(Decimal("0.020000"), "CNY"),
        usage_estimated_cost=Money(Decimal("0.012300"), "CNY"),
        billing_status="usage_estimated",
        pricing_version="price-20260718",
        provider_request_id="req-safe-1",
        provider_cached_input_tokens=11,
        estimated_cost=Money(Decimal("0.012300"), "CNY"),
        error_code="provider_error",
        error_summary=(
            f"Authorization: Bearer {secret}; prompt=private-user-prompt; safe timeout summary"
        ),
    )

    ledger.record(entry)
    row = ledger.get("call-1")

    assert row is not None
    assert row["input_tokens"] == 101
    assert row["request_bytes"] == 2048
    assert row["cache_hit"] is False
    assert row["estimated_cost"] == Decimal("0.012300")
    assert row["currency"] == "CNY"
    assert row["upload_level"] == "structured_summary"
    assert row["data_allowed"] is True
    assert row["authorization_basis"] == "owned_media"
    assert row["request_count"] == 2
    assert row["network_request_count"] == 2
    assert row["rate_limit_count"] == 1
    assert row["preflight_reserved_cost"] == Decimal("0.020000")
    assert row["usage_estimated_cost"] == Decimal("0.012300")
    assert row["billing_status"] == "usage_estimated"
    assert row["provider_request_id"] == "req-safe-1"
    assert row["provider_cached_input_tokens"] == 11
    assert row["retention_days_known"] is True
    assert secret not in str(row["error_summary"])
    assert "private-user-prompt" not in str(row["error_summary"])
    assert secret.encode() not in db_path.read_bytes()
    assert b"private-user-prompt" not in db_path.read_bytes()
    assert ledger.count() == 1
    assert ledger.total_spend(currency="CNY", batch_id="batch-1").amount == Decimal(
        "0.012300"
    )

    with sqlite3.connect(db_path) as connection:
        column_names = {
            row[1] for row in connection.execute("PRAGMA table_info(public_model_calls)")
        }
    assert not {
        "api_key",
        "secret",
        "raw_media",
        "media_bytes",
        "prompt",
        "prompt_text",
        "request_body",
    } & column_names


def test_ledger_rejects_invalid_status_and_negative_usage(tmp_path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        LedgerEntry(
            provider="fake",
            model="fake",
            api_version="v1",
            prompt_version="p1",
            request_type="text",
            status="anything-goes",
            estimated_cost=Money(Decimal("0"), "CNY"),
        )
    with pytest.raises(ValueError, match="input_tokens"):
        LedgerEntry(
            provider="fake",
            model="fake",
            api_version="v1",
            prompt_version="p1",
            request_type="text",
            status="success",
            input_tokens=-1,
            estimated_cost=Money(Decimal("0"), "CNY"),
        )

    ledger = PublicModelLedger(tmp_path / "ledger.sqlite3")
    assert list(ledger.iter_entries()) == []
