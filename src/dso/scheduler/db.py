from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from dso.config import ensure_data_dirs
from dso.scheduler.contracts import MODEL_SCHEDULER_VERSION


def scheduler_db_path() -> Path:
    settings = ensure_data_dirs()
    configured = str(os.environ.get("DSO_MODEL_SCHEDULER_DB_PATH") or "").strip()
    if not configured:
        return settings.db_dir / "model_scheduler.sqlite3"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else settings.root / path


def scheduler_connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or scheduler_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def init_scheduler_db(db_path: Path | None = None) -> Path:
    path = db_path or scheduler_db_path()
    with scheduler_connect(path) as connection:
        connection.executescript(SCHEDULER_SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO model_scheduler_meta(key, value) VALUES ('schema_version', ?)",
            [MODEL_SCHEDULER_VERSION],
        )
    return path


SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_scheduler_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_jobs (
  id TEXT PRIMARY KEY,
  contract_version TEXT NOT NULL,
  parent_job_id TEXT,
  retry_of_job_id TEXT,
  job_kind TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  account_id TEXT NOT NULL DEFAULT '',
  resource_class TEXT NOT NULL,
  model_profile_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  model_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT '',
  priority_class TEXT NOT NULL,
  base_priority INTEGER NOT NULL,
  status TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  parameters_hash TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  fallback_ref_json TEXT NOT NULL DEFAULT '{}',
  request_summary_json TEXT NOT NULL DEFAULT '{}',
  result_summary_json TEXT NOT NULL DEFAULT '{}',
  result_artifact_path TEXT NOT NULL DEFAULT '',
  total_items INTEGER NOT NULL DEFAULT 0,
  completed_items INTEGER NOT NULL DEFAULT 0,
  failed_items INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  not_before_at TEXT,
  deadline_at TEXT,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(parent_job_id) REFERENCES model_jobs(id),
  FOREIGN KEY(retry_of_job_id) REFERENCES model_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_model_jobs_dispatch
ON model_jobs(status, resource_class, base_priority DESC, not_before_at, created_at);
CREATE INDEX IF NOT EXISTS idx_model_jobs_subject
ON model_jobs(subject_type, subject_id, job_kind, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_jobs_active_dedupe
ON model_jobs(dedupe_key)
WHERE status IN (
  'queued', 'preparing', 'ready', 'waiting_resource',
  'running', 'retry_wait', 'cancel_requested'
);

CREATE TABLE IF NOT EXISTS model_job_items (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  item_index INTEGER NOT NULL,
  item_kind TEXT NOT NULL,
  item_role TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  request_json TEXT NOT NULL DEFAULT '{}',
  input_artifact_path TEXT NOT NULL DEFAULT '',
  prepared_artifact_path TEXT NOT NULL DEFAULT '',
  estimated_units REAL NOT NULL DEFAULT 1,
  actual_units REAL NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  result_artifact_path TEXT NOT NULL DEFAULT '',
  result_summary_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT NOT NULL DEFAULT '',
  error_summary TEXT NOT NULL DEFAULT '',
  not_before_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_id, item_index),
  FOREIGN KEY(job_id) REFERENCES model_jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_model_job_items_dispatch
ON model_job_items(status, job_id, item_index);

CREATE TABLE IF NOT EXISTS model_job_attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  attempt_kind TEXT NOT NULL,
  model_profile_id TEXT NOT NULL,
  batch_id TEXT NOT NULL DEFAULT '',
  batch_size INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  queue_wait_ms INTEGER NOT NULL DEFAULT 0,
  prepare_ms INTEGER NOT NULL DEFAULT 0,
  model_load_ms INTEGER NOT NULL DEFAULT 0,
  upload_ms INTEGER NOT NULL DEFAULT 0,
  inference_ms INTEGER NOT NULL DEFAULT 0,
  commit_ms INTEGER NOT NULL DEFAULT 0,
  input_units_json TEXT NOT NULL DEFAULT '{}',
  output_units_json TEXT NOT NULL DEFAULT '{}',
  gpu_metrics_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT NOT NULL DEFAULT '',
  safe_error_summary TEXT NOT NULL DEFAULT '',
  staged_artifact_path TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(job_id) REFERENCES model_jobs(id) ON DELETE CASCADE,
  FOREIGN KEY(item_id) REFERENCES model_job_items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_model_job_attempts_job
ON model_job_attempts(job_id, started_at DESC);

CREATE TABLE IF NOT EXISTS gpu_resource_leases (
  resource_id TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  model_profile_id TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_runtime_states (
  resource_id TEXT PRIMARY KEY,
  model_profile_id TEXT NOT NULL DEFAULT '',
  desired_model_id TEXT NOT NULL DEFAULT '',
  actual_model_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unknown',
  worker_id TEXT NOT NULL DEFAULT '',
  active_job_id TEXT NOT NULL DEFAULT '',
  last_error_code TEXT NOT NULL DEFAULT '',
  last_error_summary TEXT NOT NULL DEFAULT '',
  last_load_ms INTEGER NOT NULL DEFAULT 0,
  last_inference_ms INTEGER NOT NULL DEFAULT 0,
  last_health_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  item_id TEXT,
  attempt_id TEXT,
  event_type TEXT NOT NULL,
  from_status TEXT NOT NULL DEFAULT '',
  to_status TEXT NOT NULL DEFAULT '',
  reason_code TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES model_jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_model_job_events_job
ON model_job_events(job_id, id);
"""
