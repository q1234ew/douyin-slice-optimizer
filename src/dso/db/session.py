from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping, Any

from dso.config import ensure_data_dirs, get_settings


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    settings = ensure_data_dirs()
    conn = sqlite3.connect(str(db_path or settings.db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path: Path | None = None) -> Path:
    settings = ensure_data_dirs()
    path = db_path or settings.db_path
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return path


def dict_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def fetch_one(conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    return dict_row(conn.execute(query, tuple(params)).fetchone())


def fetch_all(conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def insert_row(conn: sqlite3.Connection, table: str, data: Mapping[str, Any]) -> str:
    keys = list(data.keys())
    placeholders = ", ".join("?" for _ in keys)
    columns = ", ".join(keys)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(data[key] for key in keys),
    )
    return str(data["id"])


def _migrate(conn: sqlite3.Connection) -> None:
    """Keep existing local SQLite databases compatible with the latest schema."""
    _add_columns(
        conn,
        "slice_variants",
        {
            "hypothesis": "TEXT NOT NULL DEFAULT ''",
            "changed_variable": "TEXT NOT NULL DEFAULT ''",
            "publish_window": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'draft'",
            "updated_at": "TEXT",
        },
    )
    _add_columns(
        conn,
        "publishing_experiments",
        {
            "changed_variable": "TEXT NOT NULL DEFAULT ''",
            "publish_window": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'planned'",
            "updated_at": "TEXT",
        },
    )
    _add_columns(
        conn,
        "performance_metrics",
        {
            "window_name": "TEXT NOT NULL DEFAULT 'final'",
            "hours_since_publish": "REAL NOT NULL DEFAULT 0",
            "rewatch_rate": "REAL NOT NULL DEFAULT 0",
            "comment_quality_score": "REAL NOT NULL DEFAULT 0",
            "reward_proxy": "REAL NOT NULL DEFAULT 0",
            "normalized_reward": "REAL NOT NULL DEFAULT 0",
            "uncertainty": "REAL NOT NULL DEFAULT 1",
            "sample_source": "TEXT NOT NULL DEFAULT 'csv'",
            "platform_item_id": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        conn,
        "metric_snapshots",
        {
            "sample_source": "TEXT NOT NULL DEFAULT 'csv'",
            "platform_item_id": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        conn,
        "platform_video_mappings",
        {
            "platform_url": "TEXT NOT NULL DEFAULT ''",
            "platform_title": "TEXT NOT NULL DEFAULT ''",
            "published_at": "TEXT NOT NULL DEFAULT ''",
            "last_metrics_at": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        conn,
        "historical_capture_samples",
        {
            "reward_proxy": "REAL NOT NULL DEFAULT 0",
            "normalized_reward": "REAL NOT NULL DEFAULT 0",
            "performance_label": "TEXT NOT NULL DEFAULT ''",
            "label_rank": "INTEGER NOT NULL DEFAULT 0",
            "label_percentile": "REAL NOT NULL DEFAULT 0",
            "label_reason": "TEXT NOT NULL DEFAULT ''",
            "quality_grade": "TEXT NOT NULL DEFAULT ''",
            "quality_score": "REAL NOT NULL DEFAULT 0",
            "source_run_id": "TEXT NOT NULL DEFAULT ''",
            "feature_version": "TEXT NOT NULL DEFAULT ''",
            "duration_seconds": "REAL NOT NULL DEFAULT 0",
            "media_type": "TEXT NOT NULL DEFAULT ''",
            "commercial_intent": "TEXT NOT NULL DEFAULT ''",
            "rights_risk": "TEXT NOT NULL DEFAULT ''",
            "classification_confidence": "TEXT NOT NULL DEFAULT ''",
            "semantic_unknown_reason": "TEXT NOT NULL DEFAULT ''",
            "semantic_feature_version": "TEXT NOT NULL DEFAULT ''",
            "research_label_version": "TEXT NOT NULL DEFAULT ''",
            "structure_confidence": "TEXT NOT NULL DEFAULT ''",
            "structure_evidence": "TEXT NOT NULL DEFAULT ''",
            "structure_unknown_reason": "TEXT NOT NULL DEFAULT ''",
            "original_sound_owner": "TEXT NOT NULL DEFAULT ''",
            "is_original_sound": "INTEGER NOT NULL DEFAULT 0",
            "entity_signal": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        conn,
        "source_videos",
        {
            "input_mode": "TEXT NOT NULL DEFAULT 'program'",
            "content_hash": "TEXT NOT NULL DEFAULT ''",
            "import_batch_id": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        conn,
        "candidate_segments",
        {
            "generation_signals_json": "TEXT NOT NULL DEFAULT '{}'",
            "boundary_strategy": "TEXT NOT NULL DEFAULT ''",
            "boundary_confidence": "REAL NOT NULL DEFAULT 0",
            "candidate_origin": "TEXT NOT NULL DEFAULT 'generated'",
            "boundary_locked": "INTEGER NOT NULL DEFAULT 0",
            "source_content_hash": "TEXT NOT NULL DEFAULT ''",
            "import_batch_id": "TEXT NOT NULL DEFAULT ''",
            "candidate_contract_version": "TEXT NOT NULL DEFAULT 'standard_candidate.v1'",
        },
    )
    _add_columns(
        conn,
        "slice_scores",
        {
            "ranker_score": "REAL NOT NULL DEFAULT 0",
            "ranker_version": "TEXT NOT NULL DEFAULT ''",
            "learning_signals_json": "TEXT NOT NULL DEFAULT '{}'",
            "omni_score": "REAL NOT NULL DEFAULT 0",
            "omni_confidence": "REAL NOT NULL DEFAULT 0",
            "omni_status": "TEXT NOT NULL DEFAULT 'not_run'",
            "omni_analysis_json": "TEXT NOT NULL DEFAULT '{}'",
            "hybrid_score": "REAL NOT NULL DEFAULT 0",
            "hybrid_rank": "INTEGER NOT NULL DEFAULT 0",
            "hybrid_ranker_version": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _ensure_historical_capture_item_unique(conn)
    _ensure_prototype_bank_dataset_schema(conn)
    _ensure_embedding_records_schema(conn)
    _ensure_material_gold_annotations_schema(conn)
    _ensure_material_window_annotations_schema(conn)
    _ensure_precut_batch_schema(conn)


def _add_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_prototype_bank_dataset_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'prototype_bank_items'"
    ).fetchone()
    table_sql = str(row["sql"] if row else "")
    if "dataset_id" in table_sql and "UNIQUE(account_id, dataset_id, prototype_key, source, version)" in table_sql:
        return
    conn.execute("ALTER TABLE prototype_bank_items RENAME TO prototype_bank_items_old")
    conn.execute(
        """
        CREATE TABLE prototype_bank_items (
          id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL DEFAULT 'main',
          dataset_id TEXT NOT NULL DEFAULT 'default',
          dataset_name TEXT NOT NULL DEFAULT '',
          prototype_key TEXT NOT NULL,
          prototype_name TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'external',
          sample_count INTEGER NOT NULL DEFAULT 0,
          median_views REAL NOT NULL DEFAULT 0,
          p75_views REAL NOT NULL DEFAULT 0,
          max_views REAL NOT NULL DEFAULT 0,
          avg_score REAL NOT NULL DEFAULT 0,
          confidence REAL NOT NULL DEFAULT 0,
          keywords_json TEXT NOT NULL DEFAULT '[]',
          examples_json TEXT NOT NULL DEFAULT '[]',
          parameters_json TEXT NOT NULL DEFAULT '{}',
          vector_path TEXT NOT NULL DEFAULT '',
          version TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          UNIQUE(account_id, dataset_id, prototype_key, source, version)
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO prototype_bank_items (
          id, account_id, dataset_id, dataset_name, prototype_key, prototype_name, source,
          sample_count, median_views, p75_views, max_views, avg_score, confidence,
          keywords_json, examples_json, parameters_json, vector_path, version, updated_at
        )
        SELECT
          id, account_id, 'default', '', prototype_key, prototype_name, source,
          sample_count, median_views, p75_views, max_views, avg_score, confidence,
          keywords_json, examples_json, parameters_json, vector_path, version, updated_at
        FROM prototype_bank_items_old
        """
    )
    conn.execute("DROP TABLE prototype_bank_items_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prototype_bank_account ON prototype_bank_items(account_id, dataset_id, source, updated_at DESC)")


def _ensure_historical_capture_item_unique(conn: sqlite3.Connection) -> None:
    _dedupe_historical_capture_item_rows(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_historical_capture_account_platform_item
        ON historical_capture_samples(account_id, platform, platform_item_id)
        WHERE platform_item_id != ''
        """
    )


def _ensure_embedding_records_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_records (
          id TEXT PRIMARY KEY,
          entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          account_id TEXT NOT NULL DEFAULT '',
          dataset_id TEXT NOT NULL DEFAULT '',
          platform_item_id TEXT NOT NULL DEFAULT '',
          modality TEXT NOT NULL,
          model_name TEXT NOT NULL,
          model_version TEXT NOT NULL DEFAULT '',
          vector_path TEXT NOT NULL DEFAULT '',
          vector_dim INTEGER NOT NULL DEFAULT 0,
          source_hash TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'ready',
          error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_embedding_records_entity ON embedding_records(entity_type, entity_id, modality, model_name, source_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_embedding_records_scope ON embedding_records(entity_type, account_id, dataset_id, modality, status)"
    )


def _ensure_precut_batch_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS precut_import_batches (
          id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL DEFAULT 'main',
          title TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'ready',
          item_count INTEGER NOT NULL DEFAULT 0,
          created_count INTEGER NOT NULL DEFAULT 0,
          reused_count INTEGER NOT NULL DEFAULT 0,
          failed_count INTEGER NOT NULL DEFAULT 0,
          processed_count INTEGER NOT NULL DEFAULT 0,
          contract_version TEXT NOT NULL DEFAULT 'precut_batch.v1',
          error_summary TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS precut_import_items (
          id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL,
          position INTEGER NOT NULL DEFAULT 0,
          source_name TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          content_hash TEXT NOT NULL DEFAULT '',
          size_bytes INTEGER NOT NULL DEFAULT 0,
          source_video_id TEXT,
          candidate_segment_id TEXT,
          ingest_disposition TEXT NOT NULL DEFAULT 'created',
          status TEXT NOT NULL DEFAULT 'ready',
          error TEXT NOT NULL DEFAULT '',
          processing_notes_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(batch_id, position),
          FOREIGN KEY(batch_id) REFERENCES precut_import_batches(id) ON DELETE CASCADE,
          FOREIGN KEY(source_video_id) REFERENCES source_videos(id) ON DELETE SET NULL,
          FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_precut_batches_created
          ON precut_import_batches(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_precut_items_batch
          ON precut_import_items(batch_id, position);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_source_videos_precut_account_hash
          ON source_videos(account_id, content_hash)
          WHERE input_mode = 'precut' AND content_hash != '';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_precut_source
          ON candidate_segments(source_video_id)
          WHERE candidate_origin = 'precut';

        CREATE TRIGGER IF NOT EXISTS prevent_locked_candidate_boundary_update
        BEFORE UPDATE OF start_time, end_time, duration_seconds ON candidate_segments
        WHEN OLD.boundary_locked = 1 AND (
          ABS(NEW.start_time - OLD.start_time) > 0.000001 OR
          ABS(NEW.end_time - OLD.end_time) > 0.000001 OR
          ABS(NEW.duration_seconds - OLD.duration_seconds) > 0.000001
        )
        BEGIN
          SELECT RAISE(ABORT, 'locked candidate boundary is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_locked_candidate_unlock
        BEFORE UPDATE OF boundary_locked ON candidate_segments
        WHEN OLD.boundary_locked = 1 AND NEW.boundary_locked != 1
        BEGIN
          SELECT RAISE(ABORT, 'locked candidate boundary cannot be unlocked');
        END;
        """
    )
    _add_columns(
        conn,
        "precut_import_items",
        {"ingest_disposition": "TEXT NOT NULL DEFAULT 'created'"},
    )


def _ensure_material_gold_annotations_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS material_gold_annotations (
          id TEXT PRIMARY KEY,
          sample_id TEXT NOT NULL UNIQUE,
          account_id TEXT NOT NULL DEFAULT '',
          dataset_id TEXT NOT NULL DEFAULT '',
          domain_category TEXT NOT NULL DEFAULT 'unknown',
          material_type TEXT NOT NULL DEFAULT 'unknown',
          program_context TEXT NOT NULL DEFAULT 'unknown',
          presentation_style TEXT NOT NULL DEFAULT 'unknown',
          review_status TEXT NOT NULL DEFAULT 'confirmed',
          operator TEXT NOT NULL DEFAULT 'local',
          review_note TEXT NOT NULL DEFAULT '',
          model_snapshot_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(sample_id) REFERENCES historical_capture_samples(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_material_gold_scope ON material_gold_annotations(account_id, dataset_id, review_status, updated_at DESC)"
    )


def _ensure_material_window_annotations_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS material_window_annotations (
          id TEXT PRIMARY KEY,
          window_id TEXT NOT NULL UNIQUE,
          sample_id TEXT NOT NULL,
          account_id TEXT NOT NULL DEFAULT '',
          dataset_id TEXT NOT NULL DEFAULT '',
          start_seconds REAL NOT NULL DEFAULT 0,
          end_seconds REAL NOT NULL DEFAULT 0,
          scene_form TEXT NOT NULL DEFAULT 'unknown',
          program_context_mode TEXT NOT NULL DEFAULT 'unknown',
          selection_quality TEXT NOT NULL DEFAULT 'uncertain',
          review_status TEXT NOT NULL DEFAULT 'confirmed',
          operator TEXT NOT NULL DEFAULT 'local',
          review_note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(sample_id) REFERENCES historical_capture_samples(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_material_window_scope ON material_window_annotations(account_id, dataset_id, review_status, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_material_window_sample ON material_window_annotations(sample_id, start_seconds)"
    )


def _dedupe_historical_capture_item_rows(conn: sqlite3.Connection) -> None:
    groups = conn.execute(
        """
        SELECT account_id, platform, platform_item_id, COUNT(*) AS count
        FROM historical_capture_samples
        WHERE platform_item_id != ''
        GROUP BY account_id, platform, platform_item_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in groups:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM historical_capture_samples
                WHERE account_id = ? AND platform = ? AND platform_item_id = ?
                """,
                [group["account_id"], group["platform"], group["platform_item_id"]],
            ).fetchall()
        ]
        if not rows:
            continue
        keep = max(rows, key=_historical_capture_preference_rank)
        remove_ids = [row["id"] for row in rows if row.get("id") != keep.get("id")]
        if remove_ids:
            conn.executemany("DELETE FROM historical_capture_samples WHERE id = ?", [(row_id,) for row_id in remove_ids])


def _historical_capture_preference_rank(row: Mapping[str, Any]) -> tuple:
    return (
        _historical_metric_completeness(row),
        _historical_source_kind_rank(row.get("source_kind")),
        _historical_dataset_date_rank(row.get("dataset_id")),
        _historical_num(row.get("views")),
        _historical_float(row.get("reward_proxy")),
        str(row.get("updated_at") or ""),
    )


def _historical_metric_completeness(row: Mapping[str, Any]) -> int:
    return sum(
        1
        for key in ["views", "likes", "comments", "favorites", "shares", "follows"]
        if _historical_float(row.get(key)) > 0
    )


def _historical_source_kind_rank(value: Any) -> int:
    kind = str(value or "").strip()
    if kind == "douyin_clean_json":
        return 30
    if kind.startswith("douyin"):
        return 20
    if kind == "metric_db":
        return 10
    if kind.startswith("capture"):
        return 5
    return 0


def _historical_dataset_date_rank(value: Any) -> int:
    import re

    matches = re.findall(r"(20\d{6})", str(value or ""))
    return int(matches[-1]) if matches else 0


def _historical_num(value: Any) -> int:
    return int(_historical_float(value))


def _historical_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_videos (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  title TEXT NOT NULL,
  original_path TEXT NOT NULL,
  file_path TEXT NOT NULL,
  duration_seconds REAL NOT NULL DEFAULT 0,
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  fps REAL NOT NULL DEFAULT 0,
  audio_streams INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ingested',
  transcript_path TEXT,
  input_mode TEXT NOT NULL DEFAULT 'program',
  content_hash TEXT NOT NULL DEFAULT '',
  import_batch_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS songs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  original_artist TEXT,
  composer TEXT,
  lyricist TEXT,
  is_original_for_program INTEGER NOT NULL DEFAULT 0,
  recognition_level TEXT NOT NULL DEFAULT 'unknown',
  rights_status TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performances (
  id TEXT PRIMARY KEY,
  source_video_id TEXT NOT NULL,
  song_id TEXT,
  performer_name TEXT,
  episode TEXT,
  start_time REAL NOT NULL DEFAULT 0,
  end_time REAL NOT NULL DEFAULT 0,
  stage_type TEXT,
  arrangement_notes TEXT,
  rights_status TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_video_id) REFERENCES source_videos(id) ON DELETE CASCADE,
  FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS music_segments (
  id TEXT PRIMARY KEY,
  performance_id TEXT,
  source_video_id TEXT NOT NULL,
  start_time REAL NOT NULL,
  end_time REAL NOT NULL,
  section_type TEXT NOT NULL,
  energy_level REAL NOT NULL DEFAULT 0,
  vocal_intensity REAL NOT NULL DEFAULT 0,
  chorus_probability REAL NOT NULL DEFAULT 0,
  climax_probability REAL NOT NULL DEFAULT 0,
  lyric_text TEXT,
  emotion_label TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(performance_id) REFERENCES performances(id) ON DELETE SET NULL,
  FOREIGN KEY(source_video_id) REFERENCES source_videos(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS candidate_segments (
  id TEXT PRIMARY KEY,
  source_video_id TEXT NOT NULL,
  performance_id TEXT,
  start_time REAL NOT NULL,
  end_time REAL NOT NULL,
  duration_seconds REAL NOT NULL,
  transcript TEXT,
  summary TEXT,
  primary_topic TEXT,
  song_section_type TEXT,
  music_slice_type TEXT,
  emotion_type TEXT,
  short_video_structure TEXT,
  musical_moment TEXT,
  program_context TEXT,
  comment_trigger TEXT,
  cover_time REAL,
  status TEXT NOT NULL DEFAULT 'candidate',
  generation_signals_json TEXT NOT NULL DEFAULT '{}',
  boundary_strategy TEXT NOT NULL DEFAULT '',
  boundary_confidence REAL NOT NULL DEFAULT 0,
  candidate_origin TEXT NOT NULL DEFAULT 'generated',
  boundary_locked INTEGER NOT NULL DEFAULT 0,
  source_content_hash TEXT NOT NULL DEFAULT '',
  import_batch_id TEXT NOT NULL DEFAULT '',
  candidate_contract_version TEXT NOT NULL DEFAULT 'standard_candidate.v1',
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_video_id) REFERENCES source_videos(id) ON DELETE CASCADE,
  FOREIGN KEY(performance_id) REFERENCES performances(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS precut_import_batches (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready',
  item_count INTEGER NOT NULL DEFAULT 0,
  created_count INTEGER NOT NULL DEFAULT 0,
  reused_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  processed_count INTEGER NOT NULL DEFAULT 0,
  contract_version TEXT NOT NULL DEFAULT 'precut_batch.v1',
  error_summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precut_import_items (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  source_name TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  source_video_id TEXT,
  candidate_segment_id TEXT,
  ingest_disposition TEXT NOT NULL DEFAULT 'created',
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT NOT NULL DEFAULT '',
  processing_notes_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(batch_id, position),
  FOREIGN KEY(batch_id) REFERENCES precut_import_batches(id) ON DELETE CASCADE,
  FOREIGN KEY(source_video_id) REFERENCES source_videos(id) ON DELETE SET NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS clip_embeddings (
  id TEXT PRIMARY KEY,
  candidate_segment_id TEXT NOT NULL,
  embedding_type TEXT NOT NULL,
  model_name TEXT NOT NULL,
  vector_path TEXT NOT NULL,
  vector_dim INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS embedding_records (
  id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  account_id TEXT NOT NULL DEFAULT '',
  dataset_id TEXT NOT NULL DEFAULT '',
  platform_item_id TEXT NOT NULL DEFAULT '',
  modality TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_version TEXT NOT NULL DEFAULT '',
  vector_path TEXT NOT NULL DEFAULT '',
  vector_dim INTEGER NOT NULL DEFAULT 0,
  source_hash TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history_matches (
  id TEXT PRIMARY KEY,
  candidate_segment_id TEXT NOT NULL,
  matched_segment_id TEXT,
  training_sample_id TEXT,
  match_type TEXT NOT NULL DEFAULT 'neutral',
  similarity REAL NOT NULL DEFAULT 0,
  reward_proxy REAL NOT NULL DEFAULT 0,
  normalized_reward REAL NOT NULL DEFAULT 0,
  sample_source TEXT NOT NULL DEFAULT '',
  model_name TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE CASCADE,
  FOREIGN KEY(matched_segment_id) REFERENCES candidate_segments(id) ON DELETE SET NULL,
  FOREIGN KEY(training_sample_id) REFERENCES training_samples(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS slice_scores (
  id TEXT PRIMARY KEY,
  candidate_segment_id TEXT NOT NULL UNIQUE,
  short_video_hook_score REAL NOT NULL,
  musical_moment_score REAL NOT NULL,
  narrative_context_score REAL NOT NULL,
  chorus_climax_score REAL NOT NULL,
  lyric_resonance_score REAL NOT NULL,
  performer_stage_score REAL NOT NULL,
  audience_reaction_score REAL NOT NULL,
  comment_trigger_score REAL NOT NULL,
  song_recognition_score REAL NOT NULL,
  novelty_arrangement_score REAL NOT NULL,
  history_match_score REAL NOT NULL,
  production_quality_score REAL NOT NULL,
  rights_risk_score REAL NOT NULL,
  low_originality_score REAL NOT NULL,
  final_score REAL NOT NULL,
  ranker_score REAL NOT NULL DEFAULT 0,
  ranker_version TEXT NOT NULL DEFAULT '',
  learning_signals_json TEXT NOT NULL DEFAULT '{}',
  omni_score REAL NOT NULL DEFAULT 0,
  omni_confidence REAL NOT NULL DEFAULT 0,
  omni_status TEXT NOT NULL DEFAULT 'not_run',
  omni_analysis_json TEXT NOT NULL DEFAULT '{}',
  hybrid_score REAL NOT NULL DEFAULT 0,
  hybrid_rank INTEGER NOT NULL DEFAULT 0,
  hybrid_ranker_version TEXT NOT NULL DEFAULT '',
  score_explanation TEXT NOT NULL,
  title_suggestions TEXT NOT NULL,
  cover_suggestion TEXT NOT NULL,
  risk_notes TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rights_clearance (
  id TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  program_rights_status TEXT NOT NULL DEFAULT 'unknown',
  song_rights_status TEXT NOT NULL DEFAULT 'unknown',
  performance_rights_status TEXT NOT NULL DEFAULT 'unknown',
  artist_portrait_status TEXT NOT NULL DEFAULT 'unknown',
  platform_license_scope TEXT NOT NULL DEFAULT '',
  allowed_clip_duration REAL,
  allowed_publish_accounts TEXT NOT NULL DEFAULT '',
  allowed_publish_platforms TEXT NOT NULL DEFAULT '',
  expiration_date TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(asset_type, asset_id)
);

CREATE TABLE IF NOT EXISTS slice_variants (
  id TEXT PRIMARY KEY,
  candidate_segment_id TEXT NOT NULL,
  title TEXT NOT NULL,
  cover_time REAL,
  subtitle_style TEXT NOT NULL DEFAULT 'lyrics_and_dialogue',
  export_path TEXT,
  variant_notes TEXT,
  hypothesis TEXT NOT NULL DEFAULT '',
  changed_variable TEXT NOT NULL DEFAULT '',
  publish_window TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  predicted_score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS publishing_experiments (
  id TEXT PRIMARY KEY,
  slice_variant_id TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT 'douyin',
  published_at TEXT,
  title_used TEXT,
  hashtags_used TEXT,
  experiment_group TEXT,
  hypothesis TEXT,
  changed_variable TEXT NOT NULL DEFAULT '',
  publish_window TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'planned',
  created_at TEXT NOT NULL,
  updated_at TEXT,
  FOREIGN KEY(slice_variant_id) REFERENCES slice_variants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS candidate_review_events (
  id TEXT PRIMARY KEY,
  candidate_segment_id TEXT NOT NULL,
  previous_status TEXT NOT NULL DEFAULT '',
  review_status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS change_events (
  id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  source_video_id TEXT,
  candidate_segment_id TEXT,
  change_type TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT 'local',
  diff_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_artifacts (
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL,
  step TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  artifact_path TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready',
  summary_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(video_id) REFERENCES source_videos(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS asr_verifications (
  id TEXT PRIMARY KEY,
  candidate_segment_id TEXT NOT NULL,
  source_video_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  model_name TEXT NOT NULL,
  backend TEXT NOT NULL,
  baseline_text TEXT NOT NULL DEFAULT '',
  verified_text TEXT NOT NULL DEFAULT '',
  baseline_path TEXT NOT NULL DEFAULT '',
  verified_path TEXT NOT NULL DEFAULT '',
  artifact_path TEXT NOT NULL DEFAULT '',
  difference_score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  created_at TEXT NOT NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE CASCADE,
  FOREIGN KEY(source_video_id) REFERENCES source_videos(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS platform_accounts (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  platform TEXT NOT NULL DEFAULT 'douyin',
  platform_account_id TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  auth_status TEXT NOT NULL DEFAULT 'not_connected',
  scopes TEXT NOT NULL DEFAULT '',
  token_status TEXT NOT NULL DEFAULT 'not_stored',
  token_expires_at TEXT NOT NULL DEFAULT '',
  last_synced_at TEXT NOT NULL DEFAULT '',
  sync_cursor TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform, account_id)
);

CREATE TABLE IF NOT EXISTS platform_oauth_sessions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  platform TEXT NOT NULL DEFAULT 'douyin',
  state TEXT NOT NULL UNIQUE,
  auth_url TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '',
  redirect_uri TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'waiting_scan',
  code TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS platform_video_mappings (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  platform TEXT NOT NULL DEFAULT 'douyin',
  platform_item_id TEXT NOT NULL,
  candidate_segment_id TEXT,
  slice_variant_id TEXT,
  experiment_id TEXT,
  platform_url TEXT NOT NULL DEFAULT '',
  platform_title TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  sync_status TEXT NOT NULL DEFAULT 'linked',
  last_synced_at TEXT,
  last_metrics_at TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform, platform_item_id),
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE SET NULL,
  FOREIGN KEY(slice_variant_id) REFERENCES slice_variants(id) ON DELETE SET NULL,
  FOREIGN KEY(experiment_id) REFERENCES publishing_experiments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS platform_sync_runs (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  platform TEXT NOT NULL DEFAULT 'douyin',
  source TEXT NOT NULL DEFAULT 'mock',
  sync_mode TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL DEFAULT 'pending',
  requested_windows TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  pulled_items INTEGER NOT NULL DEFAULT 0,
  mapped_items INTEGER NOT NULL DEFAULT 0,
  imported_metrics INTEGER NOT NULL DEFAULT 0,
  linked_rows INTEGER NOT NULL DEFAULT 0,
  unlinked_rows INTEGER NOT NULL DEFAULT 0,
  training_samples INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS performance_metrics (
  id TEXT PRIMARY KEY,
  experiment_id TEXT,
  slice_variant_id TEXT,
  candidate_segment_id TEXT,
  window_name TEXT NOT NULL DEFAULT 'final',
  collected_at TEXT NOT NULL,
  hours_since_publish REAL NOT NULL DEFAULT 0,
  views INTEGER NOT NULL DEFAULT 0,
  impressions INTEGER NOT NULL DEFAULT 0,
  avg_watch_seconds REAL NOT NULL DEFAULT 0,
  avg_watch_ratio REAL NOT NULL DEFAULT 0,
  five_second_retention REAL NOT NULL DEFAULT 0,
  completion_rate REAL NOT NULL DEFAULT 0,
  rewatch_rate REAL NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  favorites INTEGER NOT NULL DEFAULT 0,
  shares INTEGER NOT NULL DEFAULT 0,
  follows INTEGER NOT NULL DEFAULT 0,
  negative_feedback INTEGER NOT NULL DEFAULT 0,
  comment_quality_score REAL NOT NULL DEFAULT 0,
  reward_proxy REAL NOT NULL DEFAULT 0,
  normalized_reward REAL NOT NULL DEFAULT 0,
  uncertainty REAL NOT NULL DEFAULT 1,
  sample_source TEXT NOT NULL DEFAULT 'csv',
  platform_item_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(experiment_id) REFERENCES publishing_experiments(id) ON DELETE SET NULL,
  FOREIGN KEY(slice_variant_id) REFERENCES slice_variants(id) ON DELETE SET NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
  id TEXT PRIMARY KEY,
  performance_metric_id TEXT,
  experiment_id TEXT,
  slice_variant_id TEXT,
  candidate_segment_id TEXT,
  window_name TEXT NOT NULL DEFAULT 'final',
  collected_at TEXT NOT NULL,
  hours_since_publish REAL NOT NULL DEFAULT 0,
  views INTEGER NOT NULL DEFAULT 0,
  impressions INTEGER NOT NULL DEFAULT 0,
  avg_watch_seconds REAL NOT NULL DEFAULT 0,
  avg_watch_ratio REAL NOT NULL DEFAULT 0,
  five_second_retention REAL NOT NULL DEFAULT 0,
  completion_rate REAL NOT NULL DEFAULT 0,
  rewatch_rate REAL NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  favorites INTEGER NOT NULL DEFAULT 0,
  shares INTEGER NOT NULL DEFAULT 0,
  follows INTEGER NOT NULL DEFAULT 0,
  negative_feedback INTEGER NOT NULL DEFAULT 0,
  comment_quality_score REAL NOT NULL DEFAULT 0,
  reward_proxy REAL NOT NULL DEFAULT 0,
  normalized_reward REAL NOT NULL DEFAULT 0,
  uncertainty REAL NOT NULL DEFAULT 1,
  sample_source TEXT NOT NULL DEFAULT 'csv',
  platform_item_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(performance_metric_id) REFERENCES performance_metrics(id) ON DELETE CASCADE,
  FOREIGN KEY(experiment_id) REFERENCES publishing_experiments(id) ON DELETE SET NULL,
  FOREIGN KEY(slice_variant_id) REFERENCES slice_variants(id) ON DELETE SET NULL,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS training_samples (
  id TEXT PRIMARY KEY,
  metric_snapshot_id TEXT NOT NULL UNIQUE,
  candidate_segment_id TEXT,
  slice_variant_id TEXT,
  experiment_id TEXT,
  sample_source TEXT NOT NULL DEFAULT 'csv',
  feature_version TEXT NOT NULL DEFAULT 'v1.rules',
  label_window TEXT NOT NULL DEFAULT 'final',
  reward_proxy REAL NOT NULL DEFAULT 0,
  normalized_reward REAL NOT NULL DEFAULT 0,
  account_baseline_snapshot TEXT NOT NULL DEFAULT '{}',
  rights_policy_status TEXT NOT NULL DEFAULT 'unknown',
  train_split TEXT NOT NULL DEFAULT 'train',
  created_at TEXT NOT NULL,
  FOREIGN KEY(metric_snapshot_id) REFERENCES metric_snapshots(id) ON DELETE CASCADE,
  FOREIGN KEY(candidate_segment_id) REFERENCES candidate_segments(id) ON DELETE SET NULL,
  FOREIGN KEY(slice_variant_id) REFERENCES slice_variants(id) ON DELETE SET NULL,
  FOREIGN KEY(experiment_id) REFERENCES publishing_experiments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS account_baselines (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT 'unknown',
  duration_bucket TEXT NOT NULL DEFAULT 'unknown',
  publish_hour INTEGER NOT NULL DEFAULT -1,
  metric_name TEXT NOT NULL,
  median_value REAL NOT NULL DEFAULT 0,
  p75_value REAL NOT NULL DEFAULT 0,
  p90_value REAL NOT NULL DEFAULT 0,
  sample_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, content_type, duration_bucket, publish_hour, metric_name)
);

CREATE TABLE IF NOT EXISTS interest_clock_suggestions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  content_type TEXT NOT NULL DEFAULT 'unknown',
  duration_bucket TEXT NOT NULL DEFAULT 'unknown',
  publish_hour INTEGER NOT NULL DEFAULT -1,
  suggested_score REAL NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0,
  sample_count INTEGER NOT NULL DEFAULT 0,
  version TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, content_type, duration_bucket, publish_hour, version)
);

CREATE TABLE IF NOT EXISTS backtest_reports (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'all',
  report_name TEXT NOT NULL DEFAULT 'rules_backtest',
  status TEXT NOT NULL DEFAULT 'ready',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS historical_capture_samples (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  dataset_id TEXT NOT NULL DEFAULT 'default',
  dataset_name TEXT NOT NULL DEFAULT '',
  program_key TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  source_kind TEXT NOT NULL DEFAULT 'capture_xlsx',
  platform TEXT NOT NULL DEFAULT 'douyin',
  platform_item_id TEXT NOT NULL DEFAULT '',
  sample_key TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  platform_url TEXT NOT NULL DEFAULT '',
  views INTEGER NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  favorites INTEGER NOT NULL DEFAULT 0,
  shares INTEGER NOT NULL DEFAULT 0,
  follows INTEGER NOT NULL DEFAULT 0,
  reward_proxy REAL NOT NULL DEFAULT 0,
  normalized_reward REAL NOT NULL DEFAULT 0,
  performance_label TEXT NOT NULL DEFAULT '',
  label_rank INTEGER NOT NULL DEFAULT 0,
  label_percentile REAL NOT NULL DEFAULT 0,
  label_reason TEXT NOT NULL DEFAULT '',
  quality_grade TEXT NOT NULL DEFAULT '',
  quality_score REAL NOT NULL DEFAULT 0,
  source_run_id TEXT NOT NULL DEFAULT '',
  feature_version TEXT NOT NULL DEFAULT '',
  duration_seconds REAL NOT NULL DEFAULT 0,
  media_type TEXT NOT NULL DEFAULT '',
  content_category TEXT NOT NULL DEFAULT '',
  hook_type TEXT NOT NULL DEFAULT '',
  slice_structure TEXT NOT NULL DEFAULT '',
  program_name TEXT NOT NULL DEFAULT '',
  artist_names TEXT NOT NULL DEFAULT '',
  song_title TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  commercial_intent TEXT NOT NULL DEFAULT '',
  rights_risk TEXT NOT NULL DEFAULT '',
  classification_confidence TEXT NOT NULL DEFAULT '',
  semantic_unknown_reason TEXT NOT NULL DEFAULT '',
  semantic_feature_version TEXT NOT NULL DEFAULT '',
  structure_confidence TEXT NOT NULL DEFAULT '',
  structure_evidence TEXT NOT NULL DEFAULT '',
  structure_unknown_reason TEXT NOT NULL DEFAULT '',
  original_sound_owner TEXT NOT NULL DEFAULT '',
  is_original_sound INTEGER NOT NULL DEFAULT 0,
  entity_signal TEXT NOT NULL DEFAULT '',
  research_label_version TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  collected_at TEXT NOT NULL DEFAULT '',
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, dataset_id, sample_key)
);

CREATE TABLE IF NOT EXISTS material_gold_annotations (
  id TEXT PRIMARY KEY,
  sample_id TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL DEFAULT '',
  dataset_id TEXT NOT NULL DEFAULT '',
  domain_category TEXT NOT NULL DEFAULT 'unknown',
  material_type TEXT NOT NULL DEFAULT 'unknown',
  program_context TEXT NOT NULL DEFAULT 'unknown',
  presentation_style TEXT NOT NULL DEFAULT 'unknown',
  review_status TEXT NOT NULL DEFAULT 'confirmed',
  operator TEXT NOT NULL DEFAULT 'local',
  review_note TEXT NOT NULL DEFAULT '',
  model_snapshot_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(sample_id) REFERENCES historical_capture_samples(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS material_window_annotations (
  id TEXT PRIMARY KEY,
  window_id TEXT NOT NULL UNIQUE,
  sample_id TEXT NOT NULL,
  account_id TEXT NOT NULL DEFAULT '',
  dataset_id TEXT NOT NULL DEFAULT '',
  start_seconds REAL NOT NULL DEFAULT 0,
  end_seconds REAL NOT NULL DEFAULT 0,
  scene_form TEXT NOT NULL DEFAULT 'unknown',
  program_context_mode TEXT NOT NULL DEFAULT 'unknown',
  selection_quality TEXT NOT NULL DEFAULT 'uncertain',
  review_status TEXT NOT NULL DEFAULT 'confirmed',
  operator TEXT NOT NULL DEFAULT 'local',
  review_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(sample_id) REFERENCES historical_capture_samples(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS prototype_bank_items (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT 'main',
  dataset_id TEXT NOT NULL DEFAULT 'default',
  dataset_name TEXT NOT NULL DEFAULT '',
  prototype_key TEXT NOT NULL,
  prototype_name TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'external',
  sample_count INTEGER NOT NULL DEFAULT 0,
  median_views REAL NOT NULL DEFAULT 0,
  p75_views REAL NOT NULL DEFAULT 0,
  max_views REAL NOT NULL DEFAULT 0,
  avg_score REAL NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0,
  keywords_json TEXT NOT NULL DEFAULT '[]',
  examples_json TEXT NOT NULL DEFAULT '[]',
  parameters_json TEXT NOT NULL DEFAULT '{}',
  vector_path TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, dataset_id, prototype_key, source, version)
);

CREATE INDEX IF NOT EXISTS idx_candidate_segments_video ON candidate_segments(source_video_id);
CREATE INDEX IF NOT EXISTS idx_clip_embeddings_candidate ON clip_embeddings(candidate_segment_id, embedding_type, model_name);
CREATE INDEX IF NOT EXISTS idx_embedding_records_entity ON embedding_records(entity_type, entity_id, modality, model_name, source_hash);
CREATE INDEX IF NOT EXISTS idx_embedding_records_scope ON embedding_records(entity_type, account_id, dataset_id, modality, status);
CREATE INDEX IF NOT EXISTS idx_slice_scores_final ON slice_scores(final_score DESC);
CREATE INDEX IF NOT EXISTS idx_rights_asset ON rights_clearance(asset_type, asset_id);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_candidate ON metric_snapshots(candidate_segment_id);
CREATE INDEX IF NOT EXISTS idx_training_samples_candidate ON training_samples(candidate_segment_id);
CREATE INDEX IF NOT EXISTS idx_account_baselines_key ON account_baselines(account_id, content_type, duration_bucket, publish_hour);
CREATE INDEX IF NOT EXISTS idx_review_events_candidate ON candidate_review_events(candidate_segment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_events_candidate ON change_events(candidate_segment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_artifacts_video ON pipeline_artifacts(video_id, step, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_asr_verifications_candidate ON asr_verifications(candidate_segment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_video_mappings_item ON platform_video_mappings(platform, platform_item_id);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_account ON platform_accounts(platform, account_id);
CREATE INDEX IF NOT EXISTS idx_platform_oauth_sessions_state ON platform_oauth_sessions(platform, state);
CREATE INDEX IF NOT EXISTS idx_platform_sync_runs_account ON platform_sync_runs(platform, account_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_matches_candidate ON history_matches(candidate_segment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interest_clock_key ON interest_clock_suggestions(account_id, content_type, duration_bucket, publish_hour);
CREATE INDEX IF NOT EXISTS idx_backtest_reports_account ON backtest_reports(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_historical_capture_dataset ON historical_capture_samples(account_id, dataset_id, views DESC);
CREATE INDEX IF NOT EXISTS idx_material_gold_scope ON material_gold_annotations(account_id, dataset_id, review_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_material_window_scope ON material_window_annotations(account_id, dataset_id, review_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_material_window_sample ON material_window_annotations(sample_id, start_seconds);
CREATE INDEX IF NOT EXISTS idx_historical_capture_item ON historical_capture_samples(platform, platform_item_id);
CREATE INDEX IF NOT EXISTS idx_prototype_bank_account ON prototype_bank_items(account_id, dataset_id, source, updated_at DESC);
"""
