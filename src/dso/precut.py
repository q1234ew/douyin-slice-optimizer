from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence

from dso.db.session import connect, fetch_all, fetch_one, init_db, insert_row
from dso.features.asr import transcribe_video
from dso.features.audio import extract_audio_features
from dso.media.ingest import ingest_video
from dso.scoring.ranking_policy import attach_ranking_policy, production_ranking_contract
from dso.scoring.scorer import score_segment
from dso.segments.generator import describe_candidate_content
from dso.utils import new_id, utc_now
from dso.versions import PRECUT_BATCH_VERSION, STANDARD_CANDIDATE_VERSION


MAX_BATCH_ITEMS = 100
_SCORED_STATUS = "scored"


def create_precut_batch(
    video_paths: Sequence[str | Path],
    *,
    account_id: str = "main",
    title: str = "",
    source_names: Sequence[str] | None = None,
) -> dict:
    """Import already-cut videos as one immutable candidate per source asset."""
    init_db()
    paths = [Path(value).expanduser().resolve() for value in video_paths]
    if not paths:
        raise ValueError("at least one precut video is required")
    if len(paths) > MAX_BATCH_ITEMS:
        raise ValueError(f"a precut batch supports at most {MAX_BATCH_ITEMS} files")
    if source_names is not None and len(source_names) != len(paths):
        raise ValueError("source_names must match video_paths")

    clean_account = account_id.strip() or "main"
    now = utc_now()
    batch_id = new_id("precut")
    batch = {
        "id": batch_id,
        "account_id": clean_account,
        "title": title.strip() or f"{clean_account} / {now[:10]}",
        "status": "ready",
        "item_count": len(paths),
        "created_count": 0,
        "reused_count": 0,
        "failed_count": 0,
        "processed_count": 0,
        "contract_version": PRECUT_BATCH_VERSION,
        "error_summary": "",
        "created_at": now,
        "updated_at": now,
    }
    with connect() as conn:
        insert_row(conn, "precut_import_batches", batch)
        conn.commit()

    created_count = 0
    reused_count = 0
    failed_count = 0
    errors: list[str] = []
    for position, path in enumerate(paths):
        source_name = Path(source_names[position]).name if source_names else path.name
        item_title = Path(source_name).stem.strip() or f"短片 {position + 1}"
        item = {
            "id": new_id("precut_item"),
            "batch_id": batch_id,
            "position": position,
            "source_name": source_name,
            "title": item_title,
            "content_hash": "",
            "size_bytes": 0,
            "source_video_id": None,
            "candidate_segment_id": None,
            "ingest_disposition": "failed",
            "status": "failed",
            "error": "",
            "processing_notes_json": "[]",
            "created_at": now,
            "updated_at": now,
        }
        try:
            if not path.is_file():
                raise FileNotFoundError(path)
            content_hash = _file_sha256(path)
            item["content_hash"] = content_hash
            item["size_bytes"] = path.stat().st_size
            existing = _existing_precut_source(clean_account, content_hash)
            if existing:
                video = existing
                disposition = "reused"
                reused_count += 1
            else:
                video = ingest_video(
                    path,
                    account_id=clean_account,
                    title=item_title,
                    input_mode="precut",
                    content_hash=content_hash,
                    import_batch_id=batch_id,
                )
                disposition = "created"
                created_count += 1
            candidate = _ensure_precut_candidate(video, batch_id=batch_id, content_hash=content_hash)
            has_score = _candidate_has_score(candidate["id"])
            item.update(
                {
                    "source_video_id": video["id"],
                    "candidate_segment_id": candidate["id"],
                    "ingest_disposition": disposition,
                    "status": _SCORED_STATUS if has_score else ("reused" if disposition == "reused" else "ready"),
                }
            )
        except Exception as exc:
            failed_count += 1
            item["error"] = _error_text(exc)
            errors.append(f"{source_name}: {item['error']}")
        with connect() as conn:
            insert_row(conn, "precut_import_items", item)
            conn.commit()

    with connect() as conn:
        processed_count = _scored_item_count(conn, batch_id)
        if processed_count == len(paths):
            status = "completed"
        elif failed_count == len(paths):
            status = "failed"
        elif processed_count and processed_count + failed_count == len(paths):
            status = "partial_failed"
        else:
            status = "partial_failed" if failed_count else "ready"
        conn.execute(
            """
            UPDATE precut_import_batches
            SET status = ?, created_count = ?, reused_count = ?, failed_count = ?,
                processed_count = ?, error_summary = ?, updated_at = ?
            WHERE id = ?
            """,
            [
                status,
                created_count,
                reused_count,
                failed_count,
                processed_count,
                " | ".join(errors[:5]),
                utc_now(),
                batch_id,
            ],
        )
        conn.commit()
    return get_precut_batch(batch_id)


def queue_precut_batch(batch_id: str) -> dict:
    init_db()
    with connect() as conn:
        batch = fetch_one(conn, "SELECT * FROM precut_import_batches WHERE id = ?", [batch_id])
        if not batch:
            raise KeyError(f"precut batch not found: {batch_id}")
        if batch["status"] != "processing":
            conn.execute(
                "UPDATE precut_import_batches SET status = 'queued', updated_at = ? WHERE id = ?",
                [utc_now(), batch_id],
            )
            conn.commit()
    return get_precut_batch(batch_id)


def process_precut_batch(
    batch_id: str,
    *,
    force: bool = False,
    asr_profile: str = "fast",
) -> dict:
    """Extract shared features and score all usable items in a batch."""
    init_db()
    with connect() as conn:
        batch = fetch_one(conn, "SELECT * FROM precut_import_batches WHERE id = ?", [batch_id])
        if not batch:
            raise KeyError(f"precut batch not found: {batch_id}")
        if batch["status"] == "processing":
            result = get_precut_batch(batch_id)
            result["already_running"] = True
            return result
        conn.execute(
            "UPDATE precut_import_batches SET status = 'processing', error_summary = '', updated_at = ? WHERE id = ?",
            [utc_now(), batch_id],
        )
        conn.commit()

    try:
        with connect() as conn:
            items = fetch_all(
                conn,
                "SELECT * FROM precut_import_items WHERE batch_id = ? ORDER BY position",
                [batch_id],
            )
        processed_candidates: dict[str, tuple[str, str, list[dict] | None]] = {}
        for item in items:
            candidate_id = str(item.get("candidate_segment_id") or "")
            video_id = str(item.get("source_video_id") or "")
            if not candidate_id or not video_id:
                continue
            if candidate_id in processed_candidates:
                prior_status, prior_error, prior_notes = processed_candidates[candidate_id]
                _update_item(
                    item["id"],
                    status=prior_status,
                    error=prior_error,
                    notes=prior_notes,
                )
                continue
            if not force and _candidate_has_score(candidate_id):
                _update_item(item["id"], status=_SCORED_STATUS, error="")
                processed_candidates[candidate_id] = (_SCORED_STATUS, "", None)
                continue
            _update_item(item["id"], status="processing", error="")
            try:
                notes = _extract_and_score(video_id, candidate_id, force=force, asr_profile=asr_profile)
                _update_item(item["id"], status=_SCORED_STATUS, error="", notes=notes)
                processed_candidates[candidate_id] = (_SCORED_STATUS, "", notes)
            except Exception as exc:
                error = _error_text(exc)
                _update_item(item["id"], status="failed", error=error)
                processed_candidates[candidate_id] = ("failed", error, None)
    finally:
        _sync_batch_summary(batch_id)
    return get_precut_batch(batch_id)


def list_precut_batches(*, limit: int = 20) -> dict:
    init_db()
    safe_limit = max(1, min(int(limit), 100))
    with connect() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM precut_import_batches ORDER BY created_at DESC LIMIT ?",
            [safe_limit],
        )
    for row in rows:
        row["progress"] = _batch_progress(row)
    return {
        "contract_version": PRECUT_BATCH_VERSION,
        "count": len(rows),
        "batches": rows,
    }


def get_precut_batch(batch_id: str) -> dict:
    init_db()
    with connect() as conn:
        batch = fetch_one(conn, "SELECT * FROM precut_import_batches WHERE id = ?", [batch_id])
        if not batch:
            raise KeyError(f"precut batch not found: {batch_id}")
        items = fetch_all(
            conn,
            """
            SELECT
              i.*,
              v.account_id,
              v.duration_seconds AS source_duration_seconds,
              v.width,
              v.height,
              v.audio_streams,
              v.input_mode,
              c.start_time,
              c.end_time,
              c.duration_seconds,
              c.summary,
              c.transcript,
              c.music_slice_type,
              c.emotion_type,
              c.short_video_structure,
              c.candidate_origin,
              c.boundary_locked,
              c.boundary_strategy,
              c.boundary_confidence,
              c.candidate_contract_version,
              c.status AS candidate_status,
              s.final_score,
              s.ranker_score,
              s.ranker_version,
              s.hybrid_score,
              s.hybrid_rank,
              s.hybrid_ranker_version,
              s.score_explanation,
              s.title_suggestions,
              s.cover_suggestion,
              s.risk_notes,
              s.learning_signals_json
            FROM precut_import_items i
            LEFT JOIN source_videos v ON v.id = i.source_video_id
            LEFT JOIN candidate_segments c ON c.id = i.candidate_segment_id
            LEFT JOIN slice_scores s ON s.candidate_segment_id = i.candidate_segment_id
            WHERE i.batch_id = ?
            ORDER BY i.position
            """,
            [batch_id],
        )

    for item in items:
        item["processing_notes"] = _json_value(item.pop("processing_notes_json", "[]"), [])
        item["title_suggestions"] = _json_value(item.get("title_suggestions"), [])
        item["risk_notes"] = _json_value(item.get("risk_notes"), [])
        item["learning_signals"] = _json_value(item.pop("learning_signals_json", "{}"), {})
        item.update(attach_ranking_policy(item))
        item["boundary_invariant"] = bool(
            item.get("candidate_origin") == "precut"
            and int(item.get("boundary_locked") or 0) == 1
            and float(item.get("start_time") or 0) == 0.0
            and abs(float(item.get("end_time") or 0) - float(item.get("source_duration_seconds") or 0)) <= 0.001
        )

    observed_processed = sum(1 for item in items if item.get("status") == _SCORED_STATUS)
    observed_failed = sum(1 for item in items if item.get("status") == "failed")
    batch["processed_count"] = observed_processed
    batch["failed_count"] = observed_failed

    rankings = [item for item in items if item.get("effective_score") is not None]
    rankings.sort(
        key=lambda item: (
            float(item.get("effective_score") or 0),
            float(item.get("final_score") or 0),
            float(item.get("research_score") or 0),
            -int(item.get("position") or 0),
        ),
        reverse=True,
    )
    for rank, item in enumerate(rankings, start=1):
        item["batch_rank"] = rank
    batch["progress"] = _batch_progress(batch)
    return {
        "contract_version": PRECUT_BATCH_VERSION,
        "candidate_contract_version": STANDARD_CANDIDATE_VERSION,
        "ranking_policy": production_ranking_contract(),
        "batch_id": batch_id,
        "status": batch["status"],
        "batch": batch,
        "items": items,
        "rankings": rankings,
        "summary": {
            "item_count": int(batch.get("item_count") or 0),
            "created_count": int(batch.get("created_count") or 0),
            "reused_count": int(batch.get("reused_count") or 0),
            "failed_count": int(batch.get("failed_count") or 0),
            "processed_count": int(batch.get("processed_count") or 0),
            "ranked_count": len(rankings),
            "boundary_locked_count": sum(1 for item in items if item.get("boundary_invariant")),
        },
    }


def _ensure_precut_candidate(video: dict, *, batch_id: str, content_hash: str) -> dict:
    with connect() as conn:
        existing = fetch_one(
            conn,
            "SELECT * FROM candidate_segments WHERE source_video_id = ? AND candidate_origin = 'precut' LIMIT 1",
            [video["id"]],
        )
        if existing:
            return existing

        duration = round(float(video.get("duration_seconds") or 0), 3)
        if duration <= 0:
            raise ValueError("video duration must be greater than zero")
        semantics = describe_candidate_content(str(video.get("title") or ""), 0.0, duration)
        row = {
            "id": new_id("seg"),
            "source_video_id": video["id"],
            "performance_id": None,
            "start_time": 0.0,
            "end_time": duration,
            "duration_seconds": duration,
            "transcript": "",
            **semantics,
            "cover_time": round(min(duration * 0.45, 15.0), 3),
            "status": "candidate",
            "generation_signals_json": json.dumps(
                {
                    "generation_source": "precut_asset",
                    "input_mode": "precut",
                    "boundary_locked": True,
                    "source_content_hash": content_hash,
                },
                ensure_ascii=False,
            ),
            "boundary_strategy": "source_asset_full_duration",
            "boundary_confidence": 1.0,
            "candidate_origin": "precut",
            "boundary_locked": 1,
            "source_content_hash": content_hash,
            "import_batch_id": batch_id,
            "candidate_contract_version": STANDARD_CANDIDATE_VERSION,
            "created_at": utc_now(),
        }
        insert_row(conn, "candidate_segments", row)
        conn.commit()
        return row


def _extract_and_score(
    video_id: str,
    candidate_id: str,
    *,
    force: bool,
    asr_profile: str,
) -> list[dict]:
    with connect() as conn:
        video = fetch_one(conn, "SELECT * FROM source_videos WHERE id = ?", [video_id])
        candidate = fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [candidate_id])
    if not video or not candidate:
        raise KeyError(f"precut candidate source is missing: {candidate_id}")

    notes: list[dict] = []
    transcript_text = ""
    energy = 0.0
    if int(video.get("audio_streams") or 0) > 0:
        try:
            transcript = transcribe_video(
                video_id,
                asr_profile=asr_profile,
                force=force,
            )
            if transcript.get("source") == "placeholder":
                notes.append({"stage": "asr", "status": "fallback", "reason": "placeholder_transcript"})
            else:
                transcript_text = " ".join(
                    str(segment.get("text") or "").strip()
                    for segment in transcript.get("segments") or []
                    if str(segment.get("text") or "").strip()
                ).strip()
        except Exception as exc:
            notes.append({"stage": "asr", "status": "fallback", "reason": _error_text(exc)})
        try:
            audio = extract_audio_features(video_id)
            energies = [float(frame.get("energy") or 0) for frame in audio.get("frames") or []]
            energy = sum(energies) / len(energies) if energies else 0.0
        except Exception as exc:
            notes.append({"stage": "audio", "status": "fallback", "reason": _error_text(exc)})
    else:
        notes.append({"stage": "audio", "status": "missing", "reason": "no_audio_stream"})

    semantic_text = " ".join(part for part in [str(video.get("title") or ""), transcript_text] if part).strip()
    semantics = describe_candidate_content(
        semantic_text,
        energy,
        float(candidate.get("duration_seconds") or 0),
    )
    with connect() as conn:
        conn.execute(
            """
            UPDATE candidate_segments
            SET transcript = ?, summary = ?, primary_topic = ?, song_section_type = ?,
                music_slice_type = ?, emotion_type = ?, short_video_structure = ?,
                musical_moment = ?, program_context = ?, comment_trigger = ?
            WHERE id = ?
            """,
            [
                transcript_text,
                semantics["summary"],
                semantics["primary_topic"],
                semantics["song_section_type"],
                semantics["music_slice_type"],
                semantics["emotion_type"],
                semantics["short_video_structure"],
                semantics["musical_moment"],
                semantics["program_context"],
                semantics["comment_trigger"],
                candidate_id,
            ],
        )
        conn.commit()
    score_segment(candidate_id)
    with connect() as conn:
        conn.execute(
            "UPDATE source_videos SET status = 'scored', updated_at = ? WHERE id = ?",
            [utc_now(), video_id],
        )
        conn.commit()
    return notes


def _existing_precut_source(account_id: str, content_hash: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT * FROM source_videos
            WHERE account_id = ? AND input_mode = 'precut' AND content_hash = ?
            LIMIT 1
            """,
            [account_id, content_hash],
        )


def _candidate_has_score(candidate_id: str) -> bool:
    with connect() as conn:
        return bool(
            fetch_one(
                conn,
                "SELECT id FROM slice_scores WHERE candidate_segment_id = ?",
                [candidate_id],
            )
        )


def _update_item(
    item_id: str,
    *,
    status: str,
    error: str,
    notes: list[dict] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE precut_import_items
            SET status = ?, error = ?, processing_notes_json = COALESCE(?, processing_notes_json), updated_at = ?
            WHERE id = ?
            """,
            [status, error, json.dumps(notes, ensure_ascii=False) if notes is not None else None, utc_now(), item_id],
        )
        conn.commit()


def _sync_batch_summary(batch_id: str) -> None:
    with connect() as conn:
        rows = fetch_all(
            conn,
            "SELECT status, error FROM precut_import_items WHERE batch_id = ?",
            [batch_id],
        )
        count = len(rows)
        processed = sum(1 for row in rows if row["status"] == _SCORED_STATUS)
        failed = sum(1 for row in rows if row["status"] == "failed")
        if count and processed == count:
            status = "completed"
        elif processed and processed + failed == count:
            status = "partial_failed"
        elif failed == count:
            status = "failed"
        else:
            status = "ready"
        errors = [str(row.get("error") or "") for row in rows if row.get("error")]
        conn.execute(
            """
            UPDATE precut_import_batches
            SET status = ?, processed_count = ?, failed_count = ?, error_summary = ?, updated_at = ?
            WHERE id = ?
            """,
            [status, processed, failed, " | ".join(errors[:5]), utc_now(), batch_id],
        )
        conn.commit()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scored_item_count(conn, batch_id: str) -> int:
    row = fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM precut_import_items WHERE batch_id = ? AND status = ?",
        [batch_id, _SCORED_STATUS],
    )
    return int((row or {}).get("count") or 0)


def _batch_progress(batch: dict) -> dict:
    total = int(batch.get("item_count") or 0)
    processed = int(batch.get("processed_count") or 0)
    failed = int(batch.get("failed_count") or 0)
    settled = min(total, processed + failed)
    return {
        "settled_count": settled,
        "total_count": total,
        "ratio": round(settled / total, 4) if total else 0.0,
    }


def _json_value(value: object, default: object) -> object:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
