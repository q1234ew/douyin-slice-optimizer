"""Independent SQLite audit ledger for public-model calls.

The schema is intentionally fixed-width rather than accepting arbitrary JSON.
That prevents API keys, request bodies, raw media, and prompt text from being
accidentally persisted through a generic metadata field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
from typing import Iterator
from uuid import uuid4

from .budget import Money
from .contracts import ProviderAttemptMetrics


LEDGER_SCHEMA_VERSION = "public_model_ledger.v2"
_STATUSES = {
    "reserved",
    "success",
    "error",
    "timeout",
    "rate_limited",
    "budget_rejected",
    "policy_rejected",
    "cache_hit",
    "fallback",
}
_BILLING_STATUSES = {"not_billable", "usage_estimated", "billed", "unknown"}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|secret|password|"
        r"prompt|prompt_text|system_prompt)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r'(?i)"(prompt|prompt_text|system_prompt)"\s*:\s*"(?:[^"\\]|\\.)*"'),
)


def _non_negative_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _non_negative_decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must not be a float")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return parsed


def sanitize_error_summary(value: str | None) -> str | None:
    """Best-effort defense for safe, short operational error summaries."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("error_summary must be a string or None")
    sanitized = value.replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized[:500]


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One logical provider call with bounded, body-free governance metadata."""

    provider: str
    model: str
    api_version: str
    prompt_version: str
    request_type: str
    status: str
    estimated_cost: Money
    preflight_reserved_cost: Money | None = None
    usage_estimated_cost: Money | None = None
    billed_cost: Money | None = None
    billing_status: str = "not_billable"
    pricing_version: str = ""
    provider_request_id: str = ""
    call_id: str = ""
    batch_id: str = ""
    content_hash: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    video_seconds: Decimal = Decimal("0")
    audio_seconds: Decimal = Decimal("0")
    frame_count: int = 0
    image_count: int = 0
    text_chars: int = 0
    latency_ms: int = 0
    retry_count: int = 0
    request_count: int = 1
    network_request_count: int = 0
    rate_limit_count: int = 0
    provider_cached_input_tokens: int = 0
    cache_hit: bool = False
    upload_level: str = ""
    data_allowed: bool = False
    authorization_basis: str = ""
    redaction_strategy: str = ""
    retention_days: int = 0
    retention_days_known: bool = False
    retention_policy_reference: str = ""
    error_code: str | None = None
    error_summary: str | None = None
    recorded_at: str = ""

    def __post_init__(self) -> None:
        for name in ("provider", "model", "api_version", "prompt_version", "request_type"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported public-model ledger status: {self.status}")
        if self.billing_status not in _BILLING_STATUSES:
            raise ValueError(f"unsupported billing_status: {self.billing_status}")
        for name in (
            "input_tokens",
            "output_tokens",
            "request_bytes",
            "response_bytes",
            "frame_count",
            "image_count",
            "text_chars",
            "latency_ms",
            "retry_count",
            "request_count",
            "network_request_count",
            "rate_limit_count",
            "provider_cached_input_tokens",
            "retention_days",
        ):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))
        object.__setattr__(
            self, "video_seconds", _non_negative_decimal(self.video_seconds, "video_seconds")
        )
        object.__setattr__(
            self, "audio_seconds", _non_negative_decimal(self.audio_seconds, "audio_seconds")
        )
        object.__setattr__(self, "call_id", self.call_id or uuid4().hex)
        usage_estimated_cost = self.usage_estimated_cost or self.estimated_cost
        for name, money in (
            ("preflight_reserved_cost", self.preflight_reserved_cost),
            ("usage_estimated_cost", usage_estimated_cost),
            ("billed_cost", self.billed_cost),
        ):
            if money is not None and money.currency != self.estimated_cost.currency:
                raise ValueError(f"{name} currency must match estimated_cost currency")
        if not isinstance(self.retention_days_known, bool):
            raise TypeError("retention_days_known must be bool")
        for name in (
            "pricing_version",
            "provider_request_id",
            "retention_policy_reference",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if "\x00" in value or "\r" in value or "\n" in value:
                raise ValueError(f"{name} must not contain control-line characters")
        object.__setattr__(self, "usage_estimated_cost", usage_estimated_cost)
        object.__setattr__(
            self,
            "recorded_at",
            self.recorded_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        object.__setattr__(self, "error_summary", sanitize_error_summary(self.error_summary))


@dataclass(frozen=True, slots=True)
class LedgerAttemptEntry:
    """One physical network attempt belonging to a logical ledger call."""

    attempt_number: int
    status_code: int
    latency_ms: int
    response_bytes: int
    input_tokens: int
    output_tokens: int
    provider_cached_input_tokens: int
    estimated_cost: Money
    billing_status: str
    provider_request_id: str = ""
    error_code: str = ""

    @classmethod
    def from_provider_metrics(cls, value: ProviderAttemptMetrics) -> "LedgerAttemptEntry":
        return cls(
            attempt_number=value.attempt_number,
            status_code=value.status_code,
            latency_ms=max(0, int(round(value.latency_ms))),
            response_bytes=value.response_bytes,
            input_tokens=value.input_tokens,
            output_tokens=value.output_tokens,
            provider_cached_input_tokens=value.provider_cached_input_tokens,
            estimated_cost=Money(value.estimated_cost, value.cost_currency),
            billing_status=value.billing_status.value,
            provider_request_id=value.provider_request_id,
            error_code=value.error_code,
        )

    def __post_init__(self) -> None:
        for name in (
            "attempt_number",
            "status_code",
            "latency_ms",
            "response_bytes",
            "input_tokens",
            "output_tokens",
            "provider_cached_input_tokens",
        ):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))
        if self.attempt_number < 1:
            raise ValueError("attempt_number must start at 1")
        if self.billing_status not in _BILLING_STATUSES:
            raise ValueError(f"unsupported billing_status: {self.billing_status}")
        for name in ("provider_request_id", "error_code"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if "\x00" in value or "\r" in value or "\n" in value:
                raise ValueError(f"{name} must not contain control-line characters")


class PublicModelLedger:
    """Standalone ledger that never imports or mutates the application DB."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        """Create or add compatible columns without rewriting historical calls."""

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS public_model_ledger_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS public_model_calls (
                    call_id TEXT PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    request_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    request_bytes INTEGER NOT NULL,
                    response_bytes INTEGER NOT NULL,
                    video_seconds TEXT NOT NULL,
                    audio_seconds TEXT NOT NULL,
                    frame_count INTEGER NOT NULL,
                    image_count INTEGER NOT NULL,
                    text_chars INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 1,
                    network_request_count INTEGER NOT NULL DEFAULT 0,
                    rate_limit_count INTEGER NOT NULL DEFAULT 0,
                    provider_cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_hit INTEGER NOT NULL,
                    estimated_cost TEXT NOT NULL,
                    preflight_reserved_cost TEXT NOT NULL DEFAULT '0',
                    usage_estimated_cost TEXT NOT NULL DEFAULT '0',
                    billed_cost TEXT,
                    currency TEXT NOT NULL,
                    billing_status TEXT NOT NULL DEFAULT 'not_billable',
                    pricing_version TEXT NOT NULL DEFAULT '',
                    provider_request_id TEXT NOT NULL DEFAULT '',
                    upload_level TEXT NOT NULL DEFAULT '',
                    data_allowed INTEGER NOT NULL DEFAULT 0,
                    authorization_basis TEXT NOT NULL DEFAULT '',
                    redaction_strategy TEXT NOT NULL DEFAULT '',
                    retention_days INTEGER NOT NULL DEFAULT 0,
                    retention_days_known INTEGER NOT NULL DEFAULT 0,
                    retention_policy_reference TEXT NOT NULL DEFAULT '',
                    error_code TEXT,
                    error_summary TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_public_model_calls_recorded_at
                ON public_model_calls(recorded_at);
                CREATE INDEX IF NOT EXISTS idx_public_model_calls_batch
                ON public_model_calls(batch_id, recorded_at);

                CREATE TABLE IF NOT EXISTS public_model_attempts (
                    call_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status_code INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    response_bytes INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    provider_cached_input_tokens INTEGER NOT NULL,
                    estimated_cost TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    billing_status TEXT NOT NULL,
                    provider_request_id TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (call_id, attempt_number),
                    FOREIGN KEY (call_id) REFERENCES public_model_calls(call_id)
                );

                CREATE INDEX IF NOT EXISTS idx_public_model_attempts_provider_request
                ON public_model_attempts(provider_request_id);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO public_model_ledger_meta(key, value) VALUES (?, ?)",
                ("schema_version", LEDGER_SCHEMA_VERSION),
            )
            _add_columns(
                connection,
                "public_model_calls",
                {
                    "upload_level": "TEXT NOT NULL DEFAULT ''",
                    "data_allowed": "INTEGER NOT NULL DEFAULT 0",
                    "authorization_basis": "TEXT NOT NULL DEFAULT ''",
                    "redaction_strategy": "TEXT NOT NULL DEFAULT ''",
                    "retention_days": "INTEGER NOT NULL DEFAULT 0",
                    "request_count": "INTEGER NOT NULL DEFAULT 1",
                    "network_request_count": "INTEGER NOT NULL DEFAULT 0",
                    "rate_limit_count": "INTEGER NOT NULL DEFAULT 0",
                    "provider_cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
                    "preflight_reserved_cost": "TEXT NOT NULL DEFAULT '0'",
                    "usage_estimated_cost": "TEXT NOT NULL DEFAULT '0'",
                    "billed_cost": "TEXT",
                    "billing_status": "TEXT NOT NULL DEFAULT 'not_billable'",
                    "pricing_version": "TEXT NOT NULL DEFAULT ''",
                    "provider_request_id": "TEXT NOT NULL DEFAULT ''",
                    "retention_days_known": "INTEGER NOT NULL DEFAULT 0",
                    "retention_policy_reference": "TEXT NOT NULL DEFAULT ''",
                },
            )
            connection.execute(
                """
                UPDATE public_model_calls
                SET usage_estimated_cost = estimated_cost
                WHERE usage_estimated_cost = '0' AND estimated_cost != '0'
                """
            )

    def record(
        self,
        entry: LedgerEntry,
        *,
        attempts: tuple[LedgerAttemptEntry, ...] = (),
    ) -> str:
        """Atomically persist a call and its ordered physical attempts."""

        if tuple(item.attempt_number for item in attempts) != tuple(
            range(1, len(attempts) + 1)
        ):
            raise ValueError("ledger attempts must be sequential starting at 1")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO public_model_calls (
                    call_id, recorded_at, batch_id, content_hash, provider, model,
                    api_version, prompt_version, request_type, status, input_tokens,
                    output_tokens, request_bytes, response_bytes, video_seconds,
                    audio_seconds, frame_count, image_count, text_chars, latency_ms,
                    retry_count, cache_hit, estimated_cost, currency, error_code,
                    error_summary, upload_level, data_allowed, authorization_basis,
                    redaction_strategy, retention_days, request_count,
                    network_request_count, rate_limit_count,
                    provider_cached_input_tokens, preflight_reserved_cost,
                    usage_estimated_cost, billed_cost, billing_status,
                    pricing_version, provider_request_id, retention_days_known,
                    retention_policy_reference
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    entry.call_id,
                    entry.recorded_at,
                    entry.batch_id,
                    entry.content_hash,
                    entry.provider,
                    entry.model,
                    entry.api_version,
                    entry.prompt_version,
                    entry.request_type,
                    entry.status,
                    entry.input_tokens,
                    entry.output_tokens,
                    entry.request_bytes,
                    entry.response_bytes,
                    str(entry.video_seconds),
                    str(entry.audio_seconds),
                    entry.frame_count,
                    entry.image_count,
                    entry.text_chars,
                    entry.latency_ms,
                    entry.retry_count,
                    int(entry.cache_hit),
                    str(entry.estimated_cost.amount),
                    entry.estimated_cost.currency,
                    entry.error_code,
                    entry.error_summary,
                    entry.upload_level,
                    int(entry.data_allowed),
                    entry.authorization_basis,
                    entry.redaction_strategy,
                    entry.retention_days,
                    entry.request_count,
                    entry.network_request_count,
                    entry.rate_limit_count,
                    entry.provider_cached_input_tokens,
                    str(
                        entry.preflight_reserved_cost.amount
                        if entry.preflight_reserved_cost is not None
                        else Decimal("0")
                    ),
                    str(entry.usage_estimated_cost.amount),
                    (
                        str(entry.billed_cost.amount)
                        if entry.billed_cost is not None
                        else None
                    ),
                    entry.billing_status,
                    entry.pricing_version,
                    entry.provider_request_id,
                    int(entry.retention_days_known),
                    entry.retention_policy_reference,
                ),
            )
            connection.executemany(
                """
                INSERT INTO public_model_attempts (
                    call_id, attempt_number, status_code, latency_ms,
                    response_bytes, input_tokens, output_tokens,
                    provider_cached_input_tokens, estimated_cost, currency,
                    billing_status, provider_request_id, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        entry.call_id,
                        attempt.attempt_number,
                        attempt.status_code,
                        attempt.latency_ms,
                        attempt.response_bytes,
                        attempt.input_tokens,
                        attempt.output_tokens,
                        attempt.provider_cached_input_tokens,
                        str(attempt.estimated_cost.amount),
                        attempt.estimated_cost.currency,
                        attempt.billing_status,
                        attempt.provider_request_id,
                        attempt.error_code,
                    )
                    for attempt in attempts
                ],
            )
        return entry.call_id

    def get(self, call_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM public_model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
        return self._row(row) if row is not None else None

    def iter_entries(self, *, batch_id: str | None = None) -> Iterator[dict[str, object]]:
        """Yield logical calls in deterministic audit order."""

        query = "SELECT * FROM public_model_calls"
        parameters: tuple[object, ...] = ()
        if batch_id is not None:
            query += " WHERE batch_id = ?"
            parameters = (batch_id,)
        query += " ORDER BY recorded_at, call_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        for row in rows:
            yield self._row(row)

    def iter_attempts(self, *, call_id: str | None = None) -> Iterator[dict[str, object]]:
        """Yield per-attempt metrics without joining in request content."""

        query = "SELECT * FROM public_model_attempts"
        parameters: tuple[object, ...] = ()
        if call_id is not None:
            query += " WHERE call_id = ?"
            parameters = (call_id,)
        query += " ORDER BY call_id, attempt_number"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        for row in rows:
            result = dict(row)
            result["estimated_cost"] = Decimal(str(result["estimated_cost"]))
            yield result

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["cache_hit"] = bool(result["cache_hit"])
        result["data_allowed"] = bool(result["data_allowed"])
        result["retention_days_known"] = bool(result["retention_days_known"])
        result["estimated_cost"] = Decimal(str(result["estimated_cost"]))
        result["preflight_reserved_cost"] = Decimal(
            str(result["preflight_reserved_cost"])
        )
        result["usage_estimated_cost"] = Decimal(str(result["usage_estimated_cost"]))
        result["billed_cost"] = (
            Decimal(str(result["billed_cost"]))
            if result["billed_cost"] is not None
            else None
        )
        result["video_seconds"] = Decimal(str(result["video_seconds"]))
        result["audio_seconds"] = Decimal(str(result["audio_seconds"]))
        return result

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM public_model_calls").fetchone()
        return int(row[0] if row else 0)

    def total_spend(
        self,
        *,
        currency: str,
        batch_id: str | None = None,
        recorded_date: str | None = None,
    ) -> Money:
        """Return conservative spend for budget bootstrap after process restart.

        Billed cost wins, unknown billing consumes its full preflight reservation,
        and otherwise the usage estimate is used. This prevents a restart from
        resetting spend merely because the provider bill is delayed.
        """

        normalized_currency = currency.strip().upper()
        clauses = ["currency = ?"]
        parameters: list[object] = [normalized_currency]
        if batch_id is not None:
            clauses.append("batch_id = ?")
            parameters.append(batch_id)
        if recorded_date is not None:
            clauses.append("substr(recorded_at, 1, 10) = ?")
            parameters.append(recorded_date)
        query = (
            "SELECT billing_status, estimated_cost, usage_estimated_cost, billed_cost, "
            "preflight_reserved_cost FROM public_model_calls WHERE "
            + " AND ".join(clauses)
        )
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        amounts = []
        for row in rows:
            billing_status = str(row[0])
            if row[3] is not None:
                amounts.append(Decimal(str(row[3])))
            elif billing_status == "unknown":
                amounts.append(Decimal(str(row[4])))
            else:
                amounts.append(Decimal(str(row[2] or row[1])))
        amount = sum(amounts, Decimal("0"))
        return Money(amount, normalized_currency)


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
