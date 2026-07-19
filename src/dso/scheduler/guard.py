from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from typing import Iterator

from dso.scheduler.repository import ClaimedJob


_TRUE_VALUES = {"1", "true", "yes", "on"}
_execution_claim: ContextVar[ClaimedJob | None] = ContextVar("dso_model_scheduler_claim", default=None)


class SchedulerLeaseRequired(RuntimeError):
    pass


def scheduler_enforces_gpu_lease() -> bool:
    return str(os.environ.get("DSO_MODEL_SCHEDULER_ENABLED") or "").strip().lower() in _TRUE_VALUES


@contextmanager
def scheduler_execution(claim: ClaimedJob) -> Iterator[None]:
    token = _execution_claim.set(claim)
    try:
        yield
    finally:
        _execution_claim.reset(token)


def require_scheduler_lease(operation: str) -> None:
    if not scheduler_enforces_gpu_lease():
        return
    if _execution_claim.get() is None:
        raise SchedulerLeaseRequired(
            f"model_scheduler_lease_required: {operation} must run through dso model-worker"
        )
