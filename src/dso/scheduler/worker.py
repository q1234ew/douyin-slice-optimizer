"""Lease-aware worker that separates inference staging from durable commit."""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Protocol

from dso.config import ensure_data_dirs
from dso.scheduler.contracts import (
    OMNI_RERANK_JOB_KIND,
    QWEN3_ASR_JOB_KIND,
    TEXT_EMBEDDING_JOB_KIND,
    VISUAL_EMBEDDING_JOB_KIND,
)
from dso.scheduler.guard import scheduler_execution
from dso.scheduler.repository import ClaimedJob, LeaseLost, ModelJobRepository, PreparationClaim
from dso.scheduler.resource_agent import RuntimeActivationError, RuntimeManager
from dso.utils import read_json, write_json


class RetryableJobError(RuntimeError):
    """Adapter failure that may be retried within the item's fixed attempt cap."""

    def __init__(self, error_code: str, message: str, *, retry_delay_seconds: float = 5.0) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_delay_seconds = retry_delay_seconds


class PermanentJobError(RuntimeError):
    """Adapter failure that should proceed directly to fallback/finalization."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class JobAdapter(Protocol):
    def execute(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class ModelWorker:
    """Execute one leased item at a time without allowing stale commits.

    Results are written to a content-addressed staging artifact before database
    completion. If the process dies after inference, a later owner can recover
    the staged result; if the lease is lost, this worker must not commit it.
    """

    repository: ModelJobRepository
    worker_id: str
    resource_id: str = "gpu:0"
    lease_ttl_seconds: float = 180.0
    adapters: dict[str, JobAdapter] | None = None
    runtime_manager: RuntimeManager | None = None

    def __post_init__(self) -> None:
        if self.adapters is None:
            from dso.scheduler.asr import Qwen3ASRJobAdapter
            from dso.scheduler.embedding import QwenEmbeddingJobAdapter
            from dso.scheduler.omni import OmniRerankJobAdapter

            embedding = QwenEmbeddingJobAdapter()
            self.adapters = {
                OMNI_RERANK_JOB_KIND: OmniRerankJobAdapter(),
                QWEN3_ASR_JOB_KIND: Qwen3ASRJobAdapter(),
                TEXT_EMBEDDING_JOB_KIND: embedding,
                VISUAL_EMBEDDING_JOB_KIND: embedding,
            }
        if self.runtime_manager is None:
            self.runtime_manager = RuntimeManager()

    def run_once(self) -> dict[str, Any] | None:
        """Prepare available inputs, execute one claim, and finalize or reschedule it."""

        self._prepare_available()
        claim = self.repository.claim_next(
            worker_id=self.worker_id,
            resource_id=self.resource_id,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        if claim is None:
            return None
        adapter = (self.adapters or {}).get(str(claim.job["job_kind"]))
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(claim, heartbeat_stop, heartbeat_lost),
            name=f"dso-model-heartbeat-{claim.attempt_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            if adapter is None:
                return self._terminal_failure(
                    claim,
                    None,
                    error_code="schema_invalid",
                    error_summary=f"no adapter for job kind {claim.job['job_kind']}",
                )
            with scheduler_execution(claim):
                result, recovered_staged = self._recover_staged(claim)
                if not recovered_staged:
                    assert self.runtime_manager is not None
                    runtime = self.runtime_manager.ensure_profile(claim)
                    self.repository.record_runtime_ready(
                        claim,
                        actual_model_id=str(runtime.get("actual_model_id") or claim.job.get("model_id") or ""),
                        load_ms=int(runtime.get("load_ms") or 0),
                        warm_hit=bool(runtime.get("warm_hit", False)),
                    )
                    started = time.monotonic()
                    result = adapter.execute(claim.job, claim.item)
                else:
                    started = time.monotonic()
            assert isinstance(result, dict)
            inference_ms = int((time.monotonic() - started) * 1000)
            if heartbeat_lost.is_set() or not self.repository.lease_valid(claim):
                raise LeaseLost(f"lease lost while executing {claim.job['id']}")
            artifact_path = (
                Path(str(claim.item.get("result_artifact_path")))
                if recovered_staged
                else self._stage_path(claim)
            )
            if not recovered_staged:
                # Stage before touching terminal DB state so crash recovery can
                # reuse completed inference instead of charging GPU time twice.
                write_json(
                    artifact_path,
                    {
                        "contract_version": "model_staged_result.v1",
                        "job_id": claim.job["id"],
                        "item_id": claim.item["id"],
                        "attempt_id": claim.attempt_id,
                        "input_hash": claim.item["input_hash"],
                        "result": result,
                    },
                )
            self.repository.stage_result(
                claim,
                artifact_path,
                inference_ms=inference_ms,
                cache_hit=bool(recovered_staged or result.get("cache_hit_count")),
            )
            if self.repository.cancel_requested(claim.job["id"]):
                return self.repository.finish_cancelled(claim)
            if heartbeat_lost.is_set() or not self.repository.lease_valid(claim):
                raise LeaseLost(f"lease lost before commit for {claim.job['id']}")
            return self._commit_success(claim, adapter, result)
        except RetryableJobError as exc:
            if int(claim.item.get("attempt_count") or 0) < int(claim.item.get("max_attempts") or 1):
                return self.repository.fail_attempt(
                    claim,
                    error_code=exc.error_code,
                    error_summary=str(exc),
                    retryable=True,
                    retry_delay_seconds=exc.retry_delay_seconds,
                )
            return self._terminal_failure(claim, adapter, error_code=exc.error_code, error_summary=str(exc))
        except PermanentJobError as exc:
            return self._terminal_failure(claim, adapter, error_code=exc.error_code, error_summary=str(exc))
        except RuntimeActivationError as exc:
            if int(claim.item.get("attempt_count") or 0) < int(claim.item.get("max_attempts") or 1):
                return self.repository.fail_attempt(
                    claim,
                    error_code=exc.error_code,
                    error_summary=str(exc),
                    retryable=True,
                    retry_delay_seconds=exc.retry_delay_seconds,
                )
            return self._terminal_failure(claim, adapter, error_code=exc.error_code, error_summary=str(exc))
        except LeaseLost:
            # A newer owner may already hold the resource. Never mutate its lease
            # or commit this stale attempt.
            return self.repository.get(claim.job["id"])
        except Exception as exc:
            code = _classify_error(exc)
            retryable = code in {"model_unavailable", "resource_unavailable", "inference_timeout", "gpu_oom"}
            try:
                if retryable and int(claim.item.get("attempt_count") or 0) < int(claim.item.get("max_attempts") or 1):
                    return self.repository.fail_attempt(
                        claim,
                        error_code=code,
                        error_summary=str(exc),
                        retryable=True,
                        retry_delay_seconds=20.0 if code == "gpu_oom" else 5.0,
                    )
                return self._terminal_failure(claim, adapter, error_code=code, error_summary=str(exc))
            except LeaseLost:
                return self.repository.get(claim.job["id"])
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2.0)

    def run_forever(self, *, poll_seconds: float = 1.0, max_jobs: int | None = None) -> dict[str, Any]:
        processed = 0
        while max_jobs is None or processed < max_jobs:
            result = self.run_once()
            if result is None:
                time.sleep(max(0.05, float(poll_seconds)))
                continue
            processed += 1
        return {"status": "stopped", "worker_id": self.worker_id, "processed_jobs": processed}

    def _recover_staged(self, claim: ClaimedJob) -> tuple[dict[str, Any] | None, bool]:
        """Reuse a staged result only when job, item, and input hash still match."""

        staged = str(claim.item.get("result_artifact_path") or "")
        if staged:
            payload = read_json(Path(staged), default={}) or {}
            if (
                payload.get("contract_version") == "model_staged_result.v1"
                and payload.get("job_id") == claim.job["id"]
                and payload.get("item_id") == claim.item["id"]
                and payload.get("input_hash") == claim.item["input_hash"]
                and isinstance(payload.get("result"), dict)
            ):
                return dict(payload["result"]), True
        return None, False

    def _commit_success(self, claim: ClaimedJob, adapter: JobAdapter, result: dict[str, Any]) -> dict[str, Any]:
        """Commit one item and finalize the job only after all siblings terminate."""

        commit_started = time.monotonic()
        commit_item = getattr(adapter, "commit_item", None)
        item_summary = commit_item(claim.job, claim.item, result) if callable(commit_item) else _compact_item_summary(result)
        pending = self.repository.pending_item_count(claim.job["id"], excluding_item_id=claim.item["id"])
        if pending:
            return self.repository.complete_item(
                claim,
                item_result_summary=item_summary,
                item_status="succeeded",
                commit_ms=int((time.monotonic() - commit_started) * 1000),
            )
        results = self.repository.item_results(claim.job["id"])
        final_summary = self._finalize(adapter, claim.job, results)
        has_failed = any(item.get("item_status") == "failed" for item in results)
        final_status = "degraded" if has_failed or final_summary.get("status") != "ready" else "succeeded"
        return self.repository.complete_item(
            claim,
            item_result_summary=item_summary,
            item_status="succeeded",
            job_result_summary=final_summary,
            final_status=final_status,
            commit_ms=int((time.monotonic() - commit_started) * 1000),
        )

    def _terminal_failure(
        self,
        claim: ClaimedJob,
        adapter: JobAdapter | None,
        *,
        error_code: str,
        error_summary: str,
    ) -> dict[str, Any]:
        """Persist a bounded failure artifact so fallback remains auditable."""

        artifact_path = self._stage_path(claim)
        failure = {"status": "failed", "error_code": error_code, "error_summary": str(error_summary)[:500]}
        write_json(
            artifact_path,
            {
                "contract_version": "model_staged_result.v1",
                "job_id": claim.job["id"],
                "item_id": claim.item["id"],
                "attempt_id": claim.attempt_id,
                "input_hash": claim.item["input_hash"],
                "result": failure,
            },
        )
        self.repository.stage_result(claim, artifact_path, inference_ms=0, cache_hit=False)
        if self.repository.cancel_requested(claim.job["id"]):
            return self.repository.finish_cancelled(claim)
        pending = self.repository.pending_item_count(claim.job["id"], excluding_item_id=claim.item["id"])
        final_summary = None
        final_status = None
        if not pending:
            results = self.repository.item_results(claim.job["id"])
            final_summary = self._finalize(adapter, claim.job, results) if adapter is not None else failure
            final_status = "degraded" if claim.job.get("fallback") else "failed"
        return self.repository.complete_item(
            claim,
            item_result_summary=failure,
            item_status="failed",
            job_result_summary=final_summary,
            final_status=final_status,
            error_code=error_code,
            error_summary=error_summary,
        )

    def _finalize(self, adapter: JobAdapter | None, job: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
        if adapter is None:
            return {"status": "failed", "items": len(results)}
        finalize = getattr(adapter, "finalize", None)
        if callable(finalize):
            return finalize(job, results)
        commit = getattr(adapter, "commit", None)
        if callable(commit):
            payload = (results[0].get("result") or {}) if len(results) == 1 else {"items": results}
            return commit(job, payload)
        succeeded = sum(1 for item in results if item.get("item_status") == "succeeded")
        return {"status": "ready" if succeeded == len(results) else "degraded", "completed_items": succeeded, "total_items": len(results)}

    def _heartbeat_loop(self, claim: ClaimedJob, stop: threading.Event, lost: threading.Event) -> None:
        interval = max(1.0, min(30.0, self.lease_ttl_seconds / 3.0))
        while not stop.wait(interval):
            try:
                if not self.repository.heartbeat(claim, lease_ttl_seconds=self.lease_ttl_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    def _prepare_available(self) -> None:
        worker_count = _int_env("DSO_MODEL_PREP_WORKERS", 2, 1, 8)
        claims = []
        for index in range(worker_count):
            claim = self.repository.claim_preparation(worker_id=f"{self.worker_id}-prep-{index + 1}")
            if claim is None:
                break
            claims.append(claim)
        if not claims:
            return
        if len(claims) == 1:
            self._prepare_claim(claims[0])
            return
        with ThreadPoolExecutor(max_workers=len(claims), thread_name_prefix="dso-model-prep") as pool:
            list(pool.map(self._prepare_claim, claims))

    def _prepare_claim(self, claim: PreparationClaim) -> None:
        adapter = (self.adapters or {}).get(str(claim.job.get("job_kind") or ""))
        try:
            prepare = getattr(adapter, "prepare", None) if adapter is not None else None
            prepared = prepare(claim.job, claim.item) if callable(prepare) else {"status": "ready"}
            artifact_path = self._prepared_path(claim)
            write_json(
                artifact_path,
                {
                    "contract_version": "model_prepared_item.v1",
                    "job_id": claim.job["id"],
                    "item_id": claim.item["id"],
                    "input_hash": claim.item["input_hash"],
                    "prepared": prepared if isinstance(prepared, dict) else {"status": "ready"},
                },
            )
            self.repository.complete_preparation(claim, artifact_path=artifact_path)
        except Exception as exc:
            self.repository.fail_preparation(
                claim,
                error_code=_classify_error(exc),
                error_summary=str(exc),
            )

    def _stage_path(self, claim: ClaimedJob) -> Path:
        root = ensure_data_dirs().cache_dir / "model_scheduler" / "results" / claim.job["id"]
        return root / claim.item["id"] / f"{claim.attempt_id}.json"

    def _prepared_path(self, claim: PreparationClaim) -> Path:
        root = ensure_data_dirs().cache_dir / "model_scheduler" / "prepared" / claim.job["id"]
        return root / f"{claim.item['id']}.json"


def _compact_item_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in ("status", "segment_id", "window_role", "entity_id", "modality", "vector_dim", "cache_hit")
        if key in result
    }


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def default_worker_id() -> str:
    configured = str(os.environ.get("DSO_MODEL_WORKER_ID") or "").strip()
    return configured or f"{socket.gethostname()}-{os.getpid()}"


def _classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "input_changed" in text:
        return "input_changed"
    if "no such file" in text or "not found" in text:
        return "input_missing"
    if "timeout" in text or "timed out" in text:
        return "inference_timeout"
    if "out of memory" in text or "cuda oom" in text:
        return "gpu_oom"
    if "model" in text and ("unavailable" in text or "not loaded" in text):
        return "model_unavailable"
    return "internal_error"
