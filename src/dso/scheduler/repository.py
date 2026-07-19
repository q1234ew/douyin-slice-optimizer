"""Transactional model-job queue with single-resource leases and fencing.

All state transitions that grant or consume a GPU lease happen under SQLite
``BEGIN IMMEDIATE`` transactions. A monotonically increasing fencing token
prevents an expired worker from committing over a newer resource owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from dso.scheduler.contracts import (
    ACTIVE_JOB_STATUSES,
    MODEL_JOB_CONTRACT_VERSION,
    RETRYABLE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobItemSpec,
    ModelJobSpec,
    safe_error_code,
    safe_error_summary,
    stable_json_hash,
)
from dso.scheduler.db import init_scheduler_db, scheduler_connect, scheduler_db_path
from dso.utils import new_id, utc_now


class JobNotFound(KeyError):
    pass


class InvalidJobTransition(ValueError):
    pass


class LeaseUnavailable(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job: dict[str, Any]
    deduplicated: bool
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Worker claim carrying the ownership tuple required for every mutation."""

    job: dict[str, Any]
    item: dict[str, Any]
    attempt_id: str
    worker_id: str
    resource_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class PreparationClaim:
    job: dict[str, Any]
    item: dict[str, Any]
    worker_id: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return decoded


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class ModelJobRepository:
    """Persist queue state, events, attempts, staged results, and GPU ownership."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path or scheduler_db_path())
        init_scheduler_db(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return scheduler_connect(self.db_path)

    def enqueue(self, spec: ModelJobSpec) -> EnqueueResult:
        """Atomically deduplicate active/cache-valid work or create a complete job."""

        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                f"SELECT * FROM model_jobs WHERE dedupe_key = ? AND status IN ({','.join('?' for _ in ACTIVE_JOB_STATUSES)}) ORDER BY created_at DESC LIMIT 1",
                [spec.dedupe_key, *sorted(ACTIVE_JOB_STATUSES)],
            ).fetchone()
            if active is not None:
                connection.commit()
                return EnqueueResult(self._public_job(connection, active), deduplicated=True, cache_hit=False)

            cached = connection.execute(
                "SELECT * FROM model_jobs WHERE dedupe_key = ? AND status = 'succeeded' ORDER BY finished_at DESC LIMIT 1",
                [spec.dedupe_key],
            ).fetchone()
            if cached is not None and self._cached_job_valid(cached):
                connection.commit()
                return EnqueueResult(self._public_job(connection, cached), deduplicated=True, cache_hit=True)

            job_id = new_id("model_job")
            connection.execute(
                """
                INSERT INTO model_jobs (
                  id, contract_version, parent_job_id, retry_of_job_id, job_kind,
                  subject_type, subject_id, account_id, resource_class,
                  model_profile_id, model_id, model_version, prompt_version,
                  priority_class, base_priority, status, input_hash, parameters_hash,
                  dedupe_key, fallback_ref_json, request_summary_json, total_items,
                  max_attempts, not_before_at, deadline_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued',
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    job_id,
                    MODEL_JOB_CONTRACT_VERSION,
                    spec.parent_job_id,
                    spec.retry_of_job_id,
                    spec.job_kind,
                    spec.subject_type,
                    spec.subject_id,
                    spec.account_id,
                    spec.resource_class,
                    spec.model_profile_id,
                    spec.model_id,
                    spec.model_version,
                    spec.prompt_version,
                    spec.priority_class,
                    int(spec.base_priority),
                    spec.input_hash,
                    spec.parameters_hash,
                    spec.dedupe_key,
                    _json(spec.fallback_ref),
                    _json(spec.request_summary),
                    len(spec.items),
                    int(spec.max_attempts),
                    spec.not_before_at,
                    spec.deadline_at,
                    now,
                    now,
                ],
            )
            for index, item in enumerate(spec.items):
                connection.execute(
                    """
                    INSERT INTO model_job_items (
                      id, job_id, item_index, item_kind, item_role, status,
                      input_hash, request_json, estimated_units, max_attempts,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        new_id("model_item"),
                        job_id,
                        index,
                        item.item_kind,
                        item.item_role,
                        item.input_hash,
                        _json(item.request),
                        float(item.estimated_units),
                        int(item.max_attempts),
                        now,
                        now,
                    ],
                )
            self._event(connection, job_id, "job_enqueued", "", "queued", "accepted", {"items": len(spec.items)})
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [job_id]).fetchone()
            connection.commit()
            assert row is not None
            return EnqueueResult(self._public_job(connection, row), deduplicated=False, cache_hit=False)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [job_id]).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            return self._public_job(connection, row)

    def get_internal(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [job_id]).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            return self._internal_job(connection, row)

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        selected_limit = max(1, min(200, int(limit or 50)))
        query = "SELECT * FROM model_jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(selected_limit)
        with self._connect() as connection:
            return [self._public_job(connection, row) for row in connection.execute(query, params).fetchall()]

    def events(self, job_id: str, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        self.get(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_job_events WHERE job_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                [job_id, max(0, int(after or 0)), max(1, min(500, int(limit or 200)))],
            ).fetchall()
        return [
            {
                "event_id": int(row["id"]),
                "job_id": row["job_id"],
                "event_type": row["event_type"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "reason_code": row["reason_code"],
                "summary": _decode(row["summary_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def cancel(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [job_id]).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            current = str(row["status"])
            if current in TERMINAL_JOB_STATUSES:
                connection.commit()
                return self._public_job(connection, row)
            target = "cancel_requested" if current == "running" else "cancelled"
            finished_at = None if target == "cancel_requested" else now
            connection.execute(
                "UPDATE model_jobs SET status = ?, cancel_requested = 1, updated_at = ?, finished_at = COALESCE(?, finished_at) WHERE id = ?",
                [target, now, finished_at, job_id],
            )
            if target == "cancelled":
                connection.execute(
                    "UPDATE model_job_items SET status = 'cancelled', error_code = 'cancelled_by_user', updated_at = ?, finished_at = ? WHERE job_id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled')",
                    [now, now, job_id],
                )
            self._event(connection, job_id, "cancel_requested", current, target, "cancelled_by_user", {})
            updated = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [job_id]).fetchone()
            connection.commit()
            assert updated is not None
            return self._public_job(connection, updated)

    def retry(self, job_id: str) -> EnqueueResult:
        source = self.get_internal(job_id)
        if source["status"] not in RETRYABLE_JOB_STATUSES:
            raise InvalidJobTransition("retry is allowed only for failed, degraded, or cancelled_partial jobs")
        retry_nonce = new_id("retry")
        items = tuple(
            JobItemSpec(
                item_kind=item["item_kind"],
                item_role=item["item_role"],
                input_hash=item["input_hash"],
                request=item["request"],
                estimated_units=float(item["estimated_units"]),
                max_attempts=int(item["max_attempts"]),
            )
            for item in source["items"]
        )
        spec = ModelJobSpec(
            job_kind=source["job_kind"],
            subject_type=source["subject_type"],
            subject_id=source["subject_id"],
            account_id=source["account_id"],
            resource_class=source["resource_class"],
            model_profile_id=source["model_profile_id"],
            model_id=source["model_id"],
            model_version=source["model_version"],
            prompt_version=source["prompt_version"],
            priority_class=source["priority_class"],
            base_priority=int(source["base_priority"]),
            input_hash=source["input_hash"],
            parameters_hash=source["parameters_hash"],
            dedupe_key=stable_json_hash({"retry_of": job_id, "nonce": retry_nonce}),
            request_summary=source["request_summary"],
            fallback_ref=source["fallback"],
            items=items,
            max_attempts=int(source["max_attempts"]),
            deadline_at=source.get("deadline_at"),
            retry_of_job_id=job_id,
        )
        return self.enqueue(spec)

    def claim_preparation(self, *, worker_id: str) -> PreparationClaim | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                """
                SELECT i.* FROM model_job_items i
                JOIN model_jobs j ON j.id = i.job_id
                WHERE i.status = 'queued' AND j.status IN ('queued', 'preparing', 'ready')
                  AND j.cancel_requested = 0
                  AND (j.not_before_at IS NULL OR j.not_before_at <= ?)
                ORDER BY j.base_priority DESC, j.created_at ASC, i.item_index ASC
                LIMIT 1
                """,
                [now],
            ).fetchone()
            if item is None:
                connection.commit()
                return None
            job = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [item["job_id"]]).fetchone()
            assert job is not None
            connection.execute(
                "UPDATE model_job_items SET status = 'preparing', updated_at = ? WHERE id = ? AND status = 'queued'",
                [now, item["id"]],
            )
            connection.execute(
                "UPDATE model_jobs SET status = 'preparing', updated_at = ? WHERE id = ? AND status IN ('queued', 'ready', 'preparing')",
                [now, job["id"]],
            )
            self._event(connection, job["id"], "item_preparing", job["status"], "preparing", "cpu_io_preparation", {"worker_id": worker_id}, item_id=item["id"])
            updated_item = connection.execute("SELECT * FROM model_job_items WHERE id = ?", [item["id"]]).fetchone()
            updated_job = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [job["id"]]).fetchone()
            connection.commit()
            assert updated_item is not None and updated_job is not None
            return PreparationClaim(
                job=self._internal_job(connection, updated_job, include_items=False),
                item=self._internal_item(updated_item),
                worker_id=worker_id,
            )

    def complete_preparation(self, claim: PreparationClaim, *, artifact_path: Path | None = None) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE model_job_items SET status = 'ready', prepared_artifact_path = ?, updated_at = ? WHERE id = ? AND status = 'preparing'",
                [str(artifact_path or ""), now, claim.item["id"]],
            )
            if cursor.rowcount != 1:
                raise InvalidJobTransition("preparation claim is no longer active")
            connection.execute(
                "UPDATE model_jobs SET status = 'ready', updated_at = ? WHERE id = ? AND cancel_requested = 0",
                [now, claim.job["id"]],
            )
            self._event(connection, claim.job["id"], "item_prepared", "preparing", "ready", "cpu_io_ready", {}, item_id=claim.item["id"])
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [claim.job["id"]]).fetchone()
            connection.commit()
            assert row is not None
            return self._public_job(connection, row)

    def fail_preparation(self, claim: PreparationClaim, *, error_code: str, error_summary: str) -> dict[str, Any]:
        now = utc_now()
        code = safe_error_code(error_code)
        summary = safe_error_summary(error_summary)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE model_job_items SET status = 'failed', error_code = ?, error_summary = ?, updated_at = ?, finished_at = ? WHERE id = ? AND status = 'preparing'",
                [code, summary, now, now, claim.item["id"]],
            )
            pending = connection.execute(
                """
                SELECT COUNT(*) AS count,
                       SUM(CASE WHEN status IN ('ready', 'retry_wait', 'waiting_resource') THEN 1 ELSE 0 END) AS dispatchable,
                       SUM(CASE WHEN status = 'preparing' THEN 1 ELSE 0 END) AS preparing
                FROM model_job_items WHERE job_id = ? AND status IN ('queued', 'ready', 'retry_wait', 'waiting_resource', 'preparing', 'running')
                """,
                [claim.job["id"]],
            ).fetchone()
            failed = connection.execute(
                "SELECT COUNT(*) AS count FROM model_job_items WHERE job_id = ? AND status = 'failed'",
                [claim.job["id"]],
            ).fetchone()
            if int(pending["count"] or 0):
                target = "ready" if int(pending["dispatchable"] or 0) else ("preparing" if int(pending["preparing"] or 0) else "queued")
            else:
                target = "degraded"
            connection.execute(
                "UPDATE model_jobs SET status = ?, failed_items = ?, result_summary_json = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                [target, int(failed["count"] or 0), _json({"status": target, "error_code": code, "error_summary": summary}), now, now if target == "degraded" else None, claim.job["id"]],
            )
            self._event(connection, claim.job["id"], "preparation_failed", "preparing", target, code, {"error_summary": summary}, item_id=claim.item["id"])
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [claim.job["id"]]).fetchone()
            connection.commit()
            assert row is not None
            return self._public_job(connection, row)

    def claim_next(
        self,
        *,
        worker_id: str,
        resource_id: str = "gpu:0",
        lease_ttl_seconds: float = 180.0,
    ) -> ClaimedJob | None:
        """Select work and acquire the resource lease in one transaction.

        Stale leases are recovered first. The incremented fencing token becomes
        part of the claim and must match on heartbeat, staging, completion, or
        failure, so an old worker cannot mutate a newly reassigned GPU job.
        """

        now_dt = _now()
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=max(5.0, float(lease_ttl_seconds)))).isoformat()
        attempt_id = new_id("model_attempt")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_stale_locked(connection, now_dt)
            lease = connection.execute("SELECT * FROM gpu_resource_leases WHERE resource_id = ?", [resource_id]).fetchone()
            if lease is not None and lease["status"] == "active" and (_parse_time(lease["expires_at"]) or now_dt) > now_dt:
                connection.commit()
                return None

            rows = connection.execute(
                """
                SELECT * FROM model_jobs
                WHERE resource_class = ?
                  AND status IN ('ready', 'retry_wait', 'waiting_resource')
                  AND cancel_requested = 0
                  AND (not_before_at IS NULL OR not_before_at <= ?)
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY base_priority DESC, created_at ASC, id ASC
                LIMIT 100
                """,
                [resource_id, now, now],
            ).fetchall()
            runnable = [row for row in rows if not row["deadline_at"] or (_parse_time(row["deadline_at"]) or now_dt) > now_dt]
            expired = [row for row in rows if row["deadline_at"] and (_parse_time(row["deadline_at"]) or now_dt) <= now_dt]
            for row in expired:
                connection.execute(
                    "UPDATE model_jobs SET status = 'expired', updated_at = ?, finished_at = ? WHERE id = ?",
                    [now, now, row["id"]],
                )
                self._event(connection, row["id"], "job_expired", row["status"], "expired", "deadline_expired", {})
            if not runnable:
                connection.commit()
                return None
            selected, selected_reason, dispatch_summary = self._select_dispatch(connection, runnable, now_dt, resource_id)
            item = connection.execute(
                """
                SELECT * FROM model_job_items
                WHERE job_id = ? AND status IN ('ready', 'retry_wait', 'waiting_resource')
                ORDER BY item_index ASC LIMIT 1
                """,
                [selected["id"]],
            ).fetchone()
            if item is None:
                connection.execute(
                    "UPDATE model_jobs SET status = 'failed', failed_items = total_items, updated_at = ?, finished_at = ? WHERE id = ?",
                    [now, now, selected["id"]],
                )
                self._event(connection, selected["id"], "job_failed", selected["status"], "failed", "schema_invalid", {})
                connection.commit()
                return None

            old_token = int(lease["fencing_token"] or 0) if lease is not None else 0
            token = old_token + 1
            connection.execute(
                """
                INSERT INTO gpu_resource_leases (
                  resource_id, worker_id, job_id, attempt_id, model_profile_id,
                  fencing_token, acquired_at, heartbeat_at, expires_at, released_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active')
                ON CONFLICT(resource_id) DO UPDATE SET
                  worker_id = excluded.worker_id,
                  job_id = excluded.job_id,
                  attempt_id = excluded.attempt_id,
                  model_profile_id = excluded.model_profile_id,
                  fencing_token = excluded.fencing_token,
                  acquired_at = excluded.acquired_at,
                  heartbeat_at = excluded.heartbeat_at,
                  expires_at = excluded.expires_at,
                  released_at = NULL,
                  status = 'active'
                """,
                [resource_id, worker_id, selected["id"], attempt_id, selected["model_profile_id"], token, now, now, expires_at],
            )
            queue_wait_ms = max(0, int((now_dt - (_parse_time(selected["created_at"]) or now_dt)).total_seconds() * 1000))
            connection.execute(
                """
                UPDATE model_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ?
                """,
                [now, now, selected["id"]],
            )
            connection.execute(
                "UPDATE model_job_items SET status = 'running', attempt_count = attempt_count + 1, started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                [now, now, item["id"]],
            )
            connection.execute(
                """
                INSERT INTO model_job_attempts (
                  id, job_id, item_id, worker_id, resource_id, fencing_token,
                  attempt_kind, model_profile_id, status, started_at, queue_wait_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 'inference', ?, 'running', ?, ?)
                """,
                [attempt_id, selected["id"], item["id"], worker_id, resource_id, token, selected["model_profile_id"], now, queue_wait_ms],
            )
            self._event(
                connection,
                selected["id"],
                "job_dispatched",
                selected["status"],
                "running",
                "priority_affinity",
                {
                    "worker_id": worker_id,
                    "resource_id": resource_id,
                    "fencing_token": token,
                    "selected_reason": selected_reason,
                    **dispatch_summary,
                },
                item_id=item["id"],
                attempt_id=attempt_id,
            )
            job_row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [selected["id"]]).fetchone()
            item_row = connection.execute("SELECT * FROM model_job_items WHERE id = ?", [item["id"]]).fetchone()
            connection.commit()
            assert job_row is not None and item_row is not None
            return ClaimedJob(
                job=self._internal_job(connection, job_row, include_items=False),
                item=self._internal_item(item_row),
                attempt_id=attempt_id,
                worker_id=worker_id,
                resource_id=resource_id,
                fencing_token=token,
            )

    def heartbeat(self, claim: ClaimedJob, *, lease_ttl_seconds: float = 180.0) -> bool:
        now_dt = _now()
        expires = (now_dt + timedelta(seconds=max(5.0, float(lease_ttl_seconds)))).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE gpu_resource_leases SET heartbeat_at = ?, expires_at = ?
                WHERE resource_id = ? AND worker_id = ? AND attempt_id = ?
                  AND fencing_token = ? AND status = 'active'
                """,
                [now_dt.isoformat(), expires, claim.resource_id, claim.worker_id, claim.attempt_id, claim.fencing_token],
            )
            return cursor.rowcount == 1

    def lease_valid(self, claim: ClaimedJob) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM gpu_resource_leases WHERE resource_id = ?",
                [claim.resource_id],
            ).fetchone()
        return bool(
            row
            and row["status"] == "active"
            and row["worker_id"] == claim.worker_id
            and row["attempt_id"] == claim.attempt_id
            and int(row["fencing_token"]) == claim.fencing_token
            and (_parse_time(row["expires_at"]) or _now()) > _now()
        )

    def stage_result(self, claim: ClaimedJob, artifact_path: Path, *, inference_ms: int, cache_hit: bool = False) -> None:
        """Attach a durable result artifact while the claim still owns the lease."""

        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, claim)
            connection.execute(
                "UPDATE model_job_attempts SET status = 'staged', staged_artifact_path = ?, inference_ms = ?, cache_hit = ? WHERE id = ?",
                [str(artifact_path), max(0, int(inference_ms)), int(bool(cache_hit)), claim.attempt_id],
            )
            connection.execute(
                "UPDATE model_job_items SET result_artifact_path = ?, updated_at = ? WHERE id = ?",
                [str(artifact_path), now, claim.item["id"]],
            )
            self._event(connection, claim.job["id"], "result_staged", "running", "running", "", {"cache_hit": bool(cache_hit)}, item_id=claim.item["id"], attempt_id=claim.attempt_id)
            connection.commit()

    def complete_item(
        self,
        claim: ClaimedJob,
        *,
        item_result_summary: dict[str, Any],
        item_status: str = "succeeded",
        job_result_summary: dict[str, Any] | None = None,
        final_status: str | None = None,
        commit_ms: int = 0,
        error_code: str = "",
        error_summary: str = "",
    ) -> dict[str, Any]:
        """Commit item/job state and release its lease as one transaction.

        A job cannot become terminal while sibling items are pending. Staged
        artifacts remain internal; only compact result summaries enter public
        job state and events.
        """

        if item_status not in {"succeeded", "failed"}:
            raise ValueError("item status must be succeeded or failed")
        if final_status is not None and final_status not in {"succeeded", "degraded", "failed"}:
            raise ValueError("invalid final job status")
        now = utc_now()
        code = safe_error_code(error_code) if error_code else ""
        summary = safe_error_summary(error_summary) if error_summary else ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, claim)
            artifact_row = connection.execute("SELECT result_artifact_path FROM model_job_items WHERE id = ?", [claim.item["id"]]).fetchone()
            result_artifact_path = str(artifact_row["result_artifact_path"] or "") if artifact_row else ""
            attempt_status = "succeeded" if item_status == "succeeded" else "failed"
            connection.execute(
                "UPDATE model_job_attempts SET status = ?, finished_at = ?, commit_ms = ?, output_units_json = ?, error_code = ?, safe_error_summary = ? WHERE id = ?",
                [attempt_status, now, max(0, int(commit_ms)), _json({"status": item_status}), code, summary, claim.attempt_id],
            )
            connection.execute(
                "UPDATE model_job_items SET status = ?, result_summary_json = ?, result_artifact_path = ?, error_code = ?, error_summary = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                [item_status, _json(item_result_summary), result_artifact_path, code, summary, now, now, claim.item["id"]],
            )
            counts = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN status IN ('queued', 'ready', 'retry_wait', 'waiting_resource', 'preparing', 'running') THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN status IN ('ready', 'retry_wait', 'waiting_resource') THEN 1 ELSE 0 END) AS dispatchable,
                  SUM(CASE WHEN status = 'preparing' THEN 1 ELSE 0 END) AS preparing
                FROM model_job_items WHERE job_id = ?
                """,
                [claim.job["id"]],
            ).fetchone()
            completed_items = int(counts["completed"] or 0) if counts else 0
            failed_items = int(counts["failed"] or 0) if counts else 0
            pending_items = int(counts["pending"] or 0) if counts else 0
            if pending_items > 0 and final_status is not None:
                raise InvalidJobTransition("cannot finalize a job with pending items")
            if pending_items > 0:
                if int(counts["dispatchable"] or 0):
                    next_status = "ready"
                elif int(counts["preparing"] or 0):
                    next_status = "preparing"
                else:
                    next_status = "queued"
                finished_at = None
                result_payload = _decode(claim.job.get("result_summary_json"), {})
            else:
                next_status = final_status or ("degraded" if failed_items else "succeeded")
                finished_at = now
                result_payload = job_result_summary or item_result_summary
            connection.execute(
                """
                UPDATE model_jobs SET status = ?, result_summary_json = ?, result_artifact_path = ?,
                  completed_items = ?, failed_items = ?, next_attempt_at = NULL, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                [next_status, _json(result_payload), result_artifact_path if pending_items == 0 else "", completed_items, failed_items, now, finished_at, claim.job["id"]],
            )
            self._release_lease(connection, claim, now)
            self._runtime_state(connection, claim, status=next_status, inference_ms=self._attempt_inference_ms(connection, claim.attempt_id), error_code=code, error_summary=summary)
            event_type = "job_completed" if pending_items == 0 else "item_completed"
            self._event(connection, claim.job["id"], event_type, "running", next_status, code, item_result_summary, item_id=claim.item["id"], attempt_id=claim.attempt_id)
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [claim.job["id"]]).fetchone()
            connection.commit()
            assert row is not None
            return self._public_job(connection, row)

    def complete(
        self,
        claim: ClaimedJob,
        *,
        result_summary: dict[str, Any],
        status: str = "succeeded",
        commit_ms: int = 0,
    ) -> dict[str, Any]:
        """Backward-compatible single-item completion used by older callers."""

        return self.complete_item(
            claim,
            item_result_summary=result_summary,
            item_status="succeeded" if status == "succeeded" else "failed",
            job_result_summary=result_summary,
            final_status=status,
            commit_ms=commit_ms,
        )

    def item_results(self, job_id: str) -> list[dict[str, Any]]:
        """Load staged item results in deterministic item order.

        Artifact paths are internal-only and never included in the public job
        response. Missing or invalid artifacts become explicit failed items.
        """

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_job_items WHERE job_id = ? ORDER BY item_index",
                [job_id],
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            artifact_path = Path(str(row["result_artifact_path"] or ""))
            payload: dict[str, Any] = {}
            if artifact_path.is_file():
                try:
                    decoded = json.loads(artifact_path.read_text(encoding="utf-8"))
                    payload = decoded if isinstance(decoded, dict) else {}
                except (OSError, ValueError, json.JSONDecodeError):
                    payload = {}
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            results.append(
                {
                    "item_id": row["id"],
                    "item_index": int(row["item_index"]),
                    "item_kind": row["item_kind"],
                    "item_role": row["item_role"],
                    "item_status": row["status"],
                    "input_hash": row["input_hash"],
                    "result": result,
                    "error_code": row["error_code"],
                    "error_summary": row["error_summary"],
                }
            )
        return results

    def pending_item_count(self, job_id: str, *, excluding_item_id: str = "") -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM model_job_items
                WHERE job_id = ? AND id <> ?
                  AND status IN ('queued', 'ready', 'retry_wait', 'waiting_resource', 'preparing', 'running')
                """,
                [job_id, excluding_item_id],
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def fail_attempt(
        self,
        claim: ClaimedJob,
        *,
        error_code: str,
        error_summary: str,
        retryable: bool,
        retry_delay_seconds: float = 5.0,
        degrade_on_exhaustion: bool = True,
    ) -> dict[str, Any]:
        """Fail the owned attempt and choose retry, remaining work, or degradation."""

        now_dt = _now()
        now = now_dt.isoformat()
        code = safe_error_code(error_code)
        summary = safe_error_summary(error_summary)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, claim)
            job = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [claim.job["id"]]).fetchone()
            assert job is not None
            item = connection.execute("SELECT * FROM model_job_items WHERE id = ?", [claim.item["id"]]).fetchone()
            assert item is not None
            can_retry = bool(retryable and int(item["attempt_count"]) < int(item["max_attempts"]))
            if can_retry:
                target = "retry_wait"
                next_attempt = (now_dt + timedelta(seconds=max(0.0, float(retry_delay_seconds)))).isoformat()
                finished_at = None
            else:
                other_pending = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM model_job_items
                    WHERE job_id = ? AND id <> ?
                      AND status IN ('queued', 'ready', 'retry_wait', 'waiting_resource', 'preparing', 'running')
                    """,
                    [claim.job["id"], claim.item["id"]],
                ).fetchone()
                target = "queued" if int(other_pending["count"] or 0) else ("degraded" if degrade_on_exhaustion else "failed")
                next_attempt = None
                finished_at = None if target == "queued" else now
            connection.execute(
                "UPDATE model_job_attempts SET status = 'failed', error_code = ?, safe_error_summary = ?, finished_at = ? WHERE id = ?",
                [code, summary, now, claim.attempt_id],
            )
            item_target = "retry_wait" if can_retry else "failed"
            connection.execute(
                "UPDATE model_job_items SET status = ?, error_code = ?, error_summary = ?, not_before_at = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                [item_target, code, summary, next_attempt, now, finished_at, claim.item["id"]],
            )
            counts = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM model_job_items WHERE job_id = ?
                """,
                [claim.job["id"]],
            ).fetchone()
            result_summary = {
                "status": target,
                "error_code": code,
                "error_summary": summary,
                "fallback": claim.job.get("fallback") or {},
            }
            connection.execute(
                "UPDATE model_jobs SET status = ?, next_attempt_at = ?, completed_items = ?, failed_items = ?, result_summary_json = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                [target, next_attempt, int(counts["completed"] or 0), int(counts["failed"] or 0), _json(result_summary), now, finished_at, claim.job["id"]],
            )
            self._release_lease(connection, claim, now)
            self._runtime_state(connection, claim, status=target, error_code=code, error_summary=summary)
            self._event(connection, claim.job["id"], "attempt_failed", "running", target, code, {"retryable": can_retry, "error_summary": summary}, item_id=claim.item["id"], attempt_id=claim.attempt_id)
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [claim.job["id"]]).fetchone()
            connection.commit()
            assert row is not None
            return self._public_job(connection, row)

    def finish_cancelled(self, claim: ClaimedJob) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, claim)
            connection.execute(
                "UPDATE model_job_attempts SET status = 'cancelled', error_code = 'cancelled_by_user', finished_at = ? WHERE id = ?",
                [now, claim.attempt_id],
            )
            connection.execute(
                "UPDATE model_job_items SET status = 'cancelled', error_code = 'cancelled_by_user', updated_at = ?, finished_at = ? WHERE id = ?",
                [now, now, claim.item["id"]],
            )
            connection.execute(
                "UPDATE model_job_items SET status = 'cancelled', error_code = 'cancelled_by_user', updated_at = ?, finished_at = ? WHERE job_id = ? AND status IN ('queued', 'ready', 'retry_wait', 'waiting_resource', 'preparing')",
                [now, now, claim.job["id"]],
            )
            counts = connection.execute(
                "SELECT SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS completed FROM model_job_items WHERE job_id = ?",
                [claim.job["id"]],
            ).fetchone()
            completed = int(counts["completed"] or 0) if counts else 0
            target = "cancelled_partial" if completed else "cancelled"
            connection.execute(
                "UPDATE model_jobs SET status = ?, completed_items = ?, failed_items = 0, updated_at = ?, finished_at = ? WHERE id = ?",
                [target, completed, now, now, claim.job["id"]],
            )
            self._release_lease(connection, claim, now)
            self._event(connection, claim.job["id"], "job_cancelled", "cancel_requested", target, "cancelled_by_user", {}, item_id=claim.item["id"], attempt_id=claim.attempt_id)
            row = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [claim.job["id"]]).fetchone()
            connection.commit()
            assert row is not None
            return self._public_job(connection, row)

    def cancel_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT cancel_requested FROM model_jobs WHERE id = ?", [job_id]).fetchone()
        return bool(row and row["cancel_requested"])

    def recover_stale(self) -> dict[str, int]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovered = self._recover_stale_locked(connection, _now())
            connection.commit()
        return {"recovered_leases": recovered}

    def status(self, *, enabled: bool) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {row["status"]: int(row["count"]) for row in connection.execute("SELECT status, COUNT(*) AS count FROM model_jobs GROUP BY status")}
            priority_counts = {
                row["priority_class"]: int(row["count"])
                for row in connection.execute(
                    "SELECT priority_class, COUNT(*) AS count FROM model_jobs WHERE status IN ('queued', 'ready', 'waiting_resource', 'retry_wait') GROUP BY priority_class"
                )
            }
            active = connection.execute("SELECT * FROM gpu_resource_leases WHERE status = 'active' ORDER BY resource_id").fetchall()
            recent = connection.execute(
                """
                SELECT COUNT(*) AS attempts,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits,
                       AVG(queue_wait_ms) AS avg_queue_wait_ms,
                       AVG(inference_ms) AS avg_inference_ms
                FROM model_job_attempts
                WHERE started_at >= ?
                """,
                [(_now() - timedelta(hours=1)).isoformat()],
            ).fetchone()
            runtimes = connection.execute("SELECT * FROM model_runtime_states ORDER BY resource_id").fetchall()
        return {
            "contract_version": "model_scheduler_status.v1",
            "scheduler_version": "model_scheduler.v1",
            "enabled": bool(enabled),
            "status": "ready" if enabled else "disabled",
            "db_path": str(self.db_path),
            "jobs": counts,
            "queued_by_priority": priority_counts,
            "active_leases": [self._public_lease(row) for row in active],
            "runtime_states": [dict(row) for row in runtimes],
            "last_hour": {
                "attempts": int(recent["attempts"] or 0) if recent else 0,
                "succeeded": int(recent["succeeded"] or 0) if recent else 0,
                "cache_hits": int(recent["cache_hits"] or 0) if recent else 0,
                "avg_queue_wait_ms": round(float(recent["avg_queue_wait_ms"] or 0), 2) if recent else 0,
                "avg_inference_ms": round(float(recent["avg_inference_ms"] or 0), 2) if recent else 0,
            },
        }

    def resources(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            leases = {row["resource_id"]: row for row in connection.execute("SELECT * FROM gpu_resource_leases").fetchall()}
            states = {row["resource_id"]: row for row in connection.execute("SELECT * FROM model_runtime_states").fetchall()}
        resource_ids = sorted({"gpu:0", *leases.keys(), *states.keys()})
        return [
            {
                "resource_id": resource_id,
                "lease": self._public_lease(leases[resource_id]) if resource_id in leases else None,
                "runtime": dict(states[resource_id]) if resource_id in states else None,
            }
            for resource_id in resource_ids
        ]

    def record_runtime_ready(
        self,
        claim: ClaimedJob,
        *,
        actual_model_id: str,
        load_ms: int,
        warm_hit: bool,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE model_job_attempts SET model_load_ms = ? WHERE id = ?",
                [max(0, int(load_ms)), claim.attempt_id],
            )
            connection.execute(
                """
                INSERT INTO model_runtime_states (
                  resource_id, model_profile_id, desired_model_id, actual_model_id,
                  status, worker_id, active_job_id, last_error_code,
                  last_error_summary, last_load_ms, last_health_at, updated_at
                ) VALUES (?, ?, ?, ?, 'ready', ?, ?, '', '', ?, ?, ?)
                ON CONFLICT(resource_id) DO UPDATE SET
                  model_profile_id = excluded.model_profile_id,
                  desired_model_id = excluded.desired_model_id,
                  actual_model_id = excluded.actual_model_id,
                  status = excluded.status,
                  worker_id = excluded.worker_id,
                  active_job_id = excluded.active_job_id,
                  last_error_code = '',
                  last_error_summary = '',
                  last_load_ms = excluded.last_load_ms,
                  last_health_at = excluded.last_health_at,
                  updated_at = excluded.updated_at
                """,
                [claim.resource_id, claim.job["model_profile_id"], claim.job["model_id"], actual_model_id, claim.worker_id, claim.job["id"], max(0, int(load_ms)), now, now],
            )
            self._event(
                connection,
                claim.job["id"],
                "runtime_ready",
                "running",
                "running",
                "warm_hit" if warm_hit else "profile_activated",
                {"profile_id": claim.job["model_profile_id"], "load_ms": max(0, int(load_ms)), "warm_hit": bool(warm_hit)},
                item_id=claim.item["id"],
                attempt_id=claim.attempt_id,
            )

    def _cached_job_valid(self, row: sqlite3.Row) -> bool:
        artifact = str(row["result_artifact_path"] or "")
        return bool(_decode(row["result_summary_json"], {})) and (not artifact or Path(artifact).is_file())

    def _dispatch_key(self, row: sqlite3.Row, now: datetime) -> tuple[int, float, str]:
        priority = int(row["base_priority"])
        waited = max(0.0, (now - (_parse_time(row["created_at"]) or now)).total_seconds())
        priority_class = str(row["priority_class"])
        if priority_class == "product_batch":
            priority += min(59, int(waited // 300))
        elif priority_class == "maintenance":
            priority += min(99, int(waited // 1800) * 10)
        deadline = _parse_time(row["deadline_at"])
        deadline_score = 0.0 if deadline is None else -max(0.0, (deadline - now).total_seconds())
        return priority, deadline_score, str(row["id"])

    def _select_dispatch(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        now: datetime,
        resource_id: str,
    ) -> tuple[sqlite3.Row, str, dict[str, Any]]:
        """Balance effective priority, deadlines, fairness, and warm-model affinity."""

        scored = [(row, self._dispatch_key(row, now)[0]) for row in rows]
        highest_priority = max(score for _, score in scored)
        tier = [row for row, score in scored if score == highest_priority]
        urgent = [
            row
            for row in tier
            if _parse_time(row["deadline_at"])
            and ((_parse_time(row["deadline_at"]) or now) - now).total_seconds() <= 60
        ]
        reason = "effective_priority"
        if urgent:
            tier = urgent
            reason = "interactive_deadline"

        runtime = connection.execute(
            "SELECT model_profile_id FROM model_runtime_states WHERE resource_id = ?",
            [resource_id],
        ).fetchone()
        resident_profile = str(runtime["model_profile_id"] or "") if runtime else ""
        recent = connection.execute(
            "SELECT job_id, model_profile_id FROM model_job_attempts WHERE resource_id = ? ORDER BY started_at DESC, id DESC LIMIT 64",
            [resource_id],
        ).fetchall()
        last_job_id = str(recent[0]["job_id"] or "") if recent else ""
        last_profile = str(recent[0]["model_profile_id"] or "") if recent else resident_profile
        parent_burst = 0
        profile_burst = 0
        for attempt in recent:
            if str(attempt["job_id"] or "") == last_job_id and parent_burst == profile_burst:
                parent_burst += 1
            if str(attempt["model_profile_id"] or "") == last_profile:
                profile_burst += 1
            else:
                break
        max_parent_burst = _int_env("DSO_MODEL_MAX_PARENT_BURST", 4, 1, 32)
        max_profile_burst = _int_env("DSO_MODEL_MAX_CONSECUTIVE_ITEMS", 32, 1, 512)
        if parent_burst >= max_parent_burst and len(tier) > 1:
            alternatives = [row for row in tier if str(row["id"]) != last_job_id]
            if alternatives:
                tier = alternatives
                reason = "parent_fair_rotation"

        if resident_profile and profile_burst < max_profile_burst:
            warm = [row for row in tier if str(row["model_profile_id"] or "") == resident_profile]
            if warm:
                tier = warm
                if reason == "effective_priority":
                    reason = "resident_model_affinity"
        elif profile_burst >= max_profile_burst:
            alternatives = [row for row in tier if str(row["model_profile_id"] or "") != last_profile]
            if alternatives:
                tier = alternatives
                reason = "profile_residency_rotation"

        last_dispatch: dict[str, str] = {}
        if tier:
            placeholders = ",".join("?" for _ in tier)
            for row in connection.execute(
                f"SELECT job_id, MAX(started_at) AS last_at FROM model_job_attempts WHERE job_id IN ({placeholders}) GROUP BY job_id",
                [row["id"] for row in tier],
            ).fetchall():
                last_dispatch[str(row["job_id"])] = str(row["last_at"] or "")
        selected = min(
            tier,
            key=lambda row: (
                last_dispatch.get(str(row["id"]), ""),
                str(row["created_at"]),
                str(row["id"]),
            ),
        )
        return selected, reason, {
            "candidate_job_count": len(rows),
            "effective_priority": highest_priority,
            "resident_profile_id": resident_profile,
            "parent_burst_before_dispatch": parent_burst,
            "profile_burst_before_dispatch": profile_burst,
        }

    def _recover_stale_locked(self, connection: sqlite3.Connection, now_dt: datetime) -> int:
        """Expire dead leases and make unfinished work safely dispatchable again.

        Callers must already hold the write transaction. A staged attempt keeps
        its artifact and returns to retry so a new owner can validate and commit
        it without repeating successful inference.
        """

        rows = connection.execute("SELECT * FROM gpu_resource_leases WHERE status = 'active'").fetchall()
        recovered = 0
        now = now_dt.isoformat()
        for lease in rows:
            if (_parse_time(lease["expires_at"]) or now_dt) > now_dt:
                continue
            recovered += 1
            connection.execute(
                "UPDATE gpu_resource_leases SET status = 'expired', released_at = ? WHERE resource_id = ?",
                [now, lease["resource_id"]],
            )
            attempt = connection.execute("SELECT * FROM model_job_attempts WHERE id = ?", [lease["attempt_id"]]).fetchone()
            if attempt is not None and attempt["status"] in {"running", "staged"}:
                connection.execute(
                    "UPDATE model_job_attempts SET status = ?, error_code = 'lease_lost', safe_error_summary = 'worker lease expired', finished_at = ? WHERE id = ?",
                    ["staged" if attempt["status"] == "staged" else "failed", now, attempt["id"]],
                )
            job = connection.execute("SELECT * FROM model_jobs WHERE id = ?", [lease["job_id"]]).fetchone()
            if job is None or job["status"] not in {"running", "cancel_requested"}:
                continue
            item = connection.execute("SELECT * FROM model_job_items WHERE id = ?", [attempt["item_id"]]).fetchone() if attempt else None
            if job["status"] == "cancel_requested":
                completed = connection.execute(
                    "SELECT COUNT(*) AS count FROM model_job_items WHERE job_id = ? AND status = 'succeeded'",
                    [job["id"]],
                ).fetchone()
                target = "cancelled_partial" if int(completed["count"] or 0) else "cancelled"
                item_target = "cancelled"
                next_attempt = None
                finished_at = now
            elif attempt is not None and attempt["status"] == "staged":
                target = "retry_wait"
                item_target = "retry_wait"
                next_attempt = now
                finished_at = None
            elif item is not None and int(item["attempt_count"]) < int(item["max_attempts"]):
                target = "retry_wait"
                item_target = "retry_wait"
                next_attempt = now
                finished_at = None
            else:
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM model_job_items WHERE job_id = ? AND id <> ? AND status IN ('queued', 'ready', 'retry_wait', 'waiting_resource', 'preparing', 'running')",
                    [job["id"], item["id"] if item else ""],
                ).fetchone()
                target = "queued" if int(pending["count"] or 0) else "degraded"
                item_target = "failed"
                next_attempt = None
                finished_at = None if target == "queued" else now
            connection.execute(
                "UPDATE model_jobs SET status = ?, next_attempt_at = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                [target, next_attempt, now, finished_at, job["id"]],
            )
            if item is not None:
                connection.execute(
                    "UPDATE model_job_items SET status = ?, error_code = 'lease_lost', error_summary = 'worker lease expired', updated_at = ?, finished_at = ? WHERE id = ?",
                    [item_target, now, finished_at, item["id"]],
                )
            self._event(connection, job["id"], "lease_expired", job["status"], target, "lease_lost", {"resource_id": lease["resource_id"]})
        return recovered

    def _require_lease(self, connection: sqlite3.Connection, claim: ClaimedJob) -> sqlite3.Row:
        """Reject mutations from expired, replaced, or differently fenced workers."""

        row = connection.execute("SELECT * FROM gpu_resource_leases WHERE resource_id = ?", [claim.resource_id]).fetchone()
        valid = bool(
            row
            and row["status"] == "active"
            and row["worker_id"] == claim.worker_id
            and row["attempt_id"] == claim.attempt_id
            and int(row["fencing_token"]) == int(claim.fencing_token)
            and (_parse_time(row["expires_at"]) or _now()) > _now()
        )
        if not valid:
            raise LeaseLost(f"lease lost for {claim.resource_id}")
        assert row is not None
        return row

    def _release_lease(self, connection: sqlite3.Connection, claim: ClaimedJob, now: str) -> None:
        self._require_lease(connection, claim)
        connection.execute(
            "UPDATE gpu_resource_leases SET status = 'released', released_at = ?, heartbeat_at = ? WHERE resource_id = ? AND attempt_id = ? AND fencing_token = ?",
            [now, now, claim.resource_id, claim.attempt_id, claim.fencing_token],
        )

    def _runtime_state(
        self,
        connection: sqlite3.Connection,
        claim: ClaimedJob,
        *,
        status: str,
        inference_ms: int = 0,
        error_code: str = "",
        error_summary: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO model_runtime_states (
              resource_id, model_profile_id, desired_model_id, actual_model_id,
              status, worker_id, active_job_id, last_error_code,
              last_error_summary, last_inference_ms, last_health_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_id) DO UPDATE SET
              model_profile_id = excluded.model_profile_id,
              desired_model_id = excluded.desired_model_id,
              actual_model_id = excluded.actual_model_id,
              status = excluded.status,
              worker_id = excluded.worker_id,
              active_job_id = excluded.active_job_id,
              last_error_code = excluded.last_error_code,
              last_error_summary = excluded.last_error_summary,
              last_inference_ms = excluded.last_inference_ms,
              last_health_at = excluded.last_health_at,
              updated_at = excluded.updated_at
            """,
            [
                claim.resource_id,
                claim.job["model_profile_id"],
                claim.job["model_id"],
                "" if error_code in {"model_unavailable", "model_identity_mismatch", "resource_unavailable"} else claim.job["model_id"],
                status,
                claim.worker_id,
                claim.job["id"] if status == "running" else "",
                error_code,
                safe_error_summary(error_summary),
                max(0, int(inference_ms)),
                utc_now(),
                utc_now(),
            ],
        )

    def _attempt_inference_ms(self, connection: sqlite3.Connection, attempt_id: str) -> int:
        row = connection.execute("SELECT inference_ms FROM model_job_attempts WHERE id = ?", [attempt_id]).fetchone()
        return int(row["inference_ms"] or 0) if row else 0

    def _event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        from_status: str,
        to_status: str,
        reason_code: str,
        summary: dict[str, Any],
        *,
        item_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO model_job_events (
              job_id, item_id, attempt_id, event_type, from_status,
              to_status, reason_code, summary_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [job_id, item_id, attempt_id, event_type, from_status, to_status, reason_code, _json(summary), utc_now()],
        )

    def _public_job(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        items = connection.execute(
            "SELECT id, item_index, item_kind, item_role, status, attempt_count, max_attempts, error_code, error_summary, started_at, finished_at FROM model_job_items WHERE job_id = ? ORDER BY item_index",
            [row["id"]],
        ).fetchall()
        return {
            "contract_version": row["contract_version"],
            "job_id": row["id"],
            "job_kind": row["job_kind"],
            "subject": {
                "entity_type": row["subject_type"],
                "entity_id": row["subject_id"],
                "account_id": row["account_id"],
            },
            "resource_class": row["resource_class"],
            "model_ref": {
                "provider": "local",
                "profile_id": row["model_profile_id"],
                "model_id": row["model_id"],
                "model_version": row["model_version"],
                "prompt_version": row["prompt_version"],
            },
            "priority_class": row["priority_class"],
            "status": row["status"],
            "progress": {
                "total_items": int(row["total_items"]),
                "completed_items": int(row["completed_items"]),
                "failed_items": int(row["failed_items"]),
            },
            "fallback": _decode(row["fallback_ref_json"], {}),
            "result_summary": _decode(row["result_summary_json"], {}),
            "attempt_count": int(row["attempt_count"]),
            "max_attempts": int(row["max_attempts"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "retry_of_job_id": row["retry_of_job_id"],
            "items": [dict(item) for item in items],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def _internal_job(self, connection: sqlite3.Connection, row: sqlite3.Row, *, include_items: bool = True) -> dict[str, Any]:
        result = dict(row)
        result["fallback"] = _decode(row["fallback_ref_json"], {})
        result["request_summary"] = _decode(row["request_summary_json"], {})
        result["result_summary"] = _decode(row["result_summary_json"], {})
        if include_items:
            result["items"] = [
                self._internal_item(item)
                for item in connection.execute("SELECT * FROM model_job_items WHERE job_id = ? ORDER BY item_index", [row["id"]]).fetchall()
            ]
        return result

    def _internal_item(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["request"] = _decode(row["request_json"], {})
        result["result_summary"] = _decode(row["result_summary_json"], {})
        return result

    def _public_lease(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "resource_id": row["resource_id"],
            "worker_id": row["worker_id"],
            "job_id": row["job_id"],
            "model_profile_id": row["model_profile_id"],
            "fencing_token": int(row["fencing_token"]),
            "status": row["status"],
            "acquired_at": row["acquired_at"],
            "heartbeat_at": row["heartbeat_at"],
            "expires_at": row["expires_at"],
            "released_at": row["released_at"],
        }
