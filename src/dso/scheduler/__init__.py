"""Persistent local-model scheduling primitives.

The scheduler owns execution state only. Product candidates, scores, review
state, and feedback remain in the application database.
"""

from dso.scheduler.contracts import MODEL_JOB_CONTRACT_VERSION, MODEL_SCHEDULER_VERSION
from dso.scheduler.db import init_scheduler_db, scheduler_db_path

__all__ = [
    "MODEL_JOB_CONTRACT_VERSION",
    "MODEL_SCHEDULER_VERSION",
    "init_scheduler_db",
    "scheduler_db_path",
]
