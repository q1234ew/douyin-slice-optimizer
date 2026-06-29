from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dso.artifacts import record_artifact
from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.features.asr import write_srt
from dso.media.ffmpeg import export_vertical_clip, extract_frame
from dso.media.ingest import get_video
from dso.quality.insights import quality_insights
from dso.review import insert_change_event
from dso.scoring.rights import rights_mode, rights_risk_for_segment
from dso.utils import new_id, read_json, utc_now
from dso.versions import VARIANT_EXPERIMENT_VERSION, component_versions


def create_variant(segment_id: str, title: str | None = None, **payload: Any) -> dict:
    with connect() as conn:
        segment = fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [segment_id])
        score = fetch_one(conn, "SELECT * FROM slice_scores WHERE candidate_segment_id = ?", [segment_id])
    if not segment:
        raise KeyError(f"segment not found: {segment_id}")
    title = title or _first_title(score) or "音乐综艺高潜短视频切片"
    row = {
        "id": new_id("variant"),
        "candidate_segment_id": segment_id,
        "title": title,
        "cover_time": _float_or_none(payload.get("cover_time"), default=segment.get("cover_time")),
        "subtitle_style": _text(payload.get("subtitle_style")) or "lyrics_and_dialogue",
        "export_path": None,
        "variant_notes": _text(payload.get("variant_notes")) or "默认版本：9:16 智能适配，歌词 + 剧情字幕",
        "hypothesis": _text(payload.get("hypothesis")),
        "changed_variable": _text(payload.get("changed_variable")),
        "publish_window": _text(payload.get("publish_window")),
        "status": _text(payload.get("status")) or "draft",
        "predicted_score": float(score["final_score"]) if score else 0,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    with connect() as conn:
        insert_row(conn, "slice_variants", row)
        insert_change_event(
            conn,
            entity_type="slice_variant",
            entity_id=row["id"],
            change_type="variant_created",
            before={},
            after={
                "title": row["title"],
                "hypothesis": row["hypothesis"],
                "changed_variable": row["changed_variable"],
                "publish_window": row["publish_window"],
            },
            reason=_text(payload.get("reason")),
            operator=_text(payload.get("operator")) or "local",
            source_video_id=segment["source_video_id"],
            candidate_segment_id=segment_id,
        )
        conn.commit()
    return {**row, "contract_version": VARIANT_EXPERIMENT_VERSION}


def update_variant(variant_id: str, payload: dict[str, Any]) -> dict:
    editable = {
        "title",
        "cover_time",
        "subtitle_style",
        "variant_notes",
        "hypothesis",
        "changed_variable",
        "publish_window",
        "status",
    }
    with connect() as conn:
        current = fetch_one(
            conn,
            """
            SELECT v.*, c.source_video_id
            FROM slice_variants v
            JOIN candidate_segments c ON c.id = v.candidate_segment_id
            WHERE v.id = ?
            """,
            [variant_id],
        )
        if not current:
            raise KeyError(f"variant not found: {variant_id}")
        updates: dict[str, Any] = {}
        for key in editable:
            if key not in payload:
                continue
            if key == "cover_time":
                updates[key] = _float_or_none(payload.get(key), default=current.get(key))
            else:
                updates[key] = _text(payload.get(key))
        updates["updated_at"] = utc_now()
        insert_change_event(
            conn,
            entity_type="slice_variant",
            entity_id=variant_id,
            change_type="variant_updated",
            before={key: current.get(key) for key in updates},
            after=updates,
            reason=_text(payload.get("reason")),
            operator=_text(payload.get("operator")) or "local",
            source_video_id=current["source_video_id"],
            candidate_segment_id=current["candidate_segment_id"],
        )
        _update_row(conn, "slice_variants", variant_id, updates)
        conn.commit()
        row = fetch_one(conn, "SELECT * FROM slice_variants WHERE id = ?", [variant_id])
    return {**row, "contract_version": VARIANT_EXPERIMENT_VERSION}


def create_experiment(variant_id: str, payload: dict[str, Any] | None = None) -> dict:
    payload = payload or {}
    with connect() as conn:
        variant = fetch_one(conn, "SELECT * FROM slice_variants WHERE id = ?", [variant_id])
        if not variant:
            raise KeyError(f"variant not found: {variant_id}")
        now = utc_now()
        row = {
            "id": new_id("exp"),
            "slice_variant_id": variant_id,
            "platform": _text(payload.get("platform")) or "douyin",
            "published_at": _text(payload.get("published_at")),
            "title_used": _text(payload.get("title_used")) or variant["title"],
            "hashtags_used": _text(payload.get("hashtags_used")),
            "experiment_group": _text(payload.get("experiment_group")) or "manual",
            "hypothesis": _text(payload.get("hypothesis")) or variant.get("hypothesis") or "",
            "changed_variable": _text(payload.get("changed_variable")) or variant.get("changed_variable") or "",
            "publish_window": _text(payload.get("publish_window")) or variant.get("publish_window") or "",
            "status": _text(payload.get("status")) or "planned",
            "created_at": now,
            "updated_at": now,
        }
        insert_row(conn, "publishing_experiments", row)
        conn.commit()
    return {**row, "contract_version": VARIANT_EXPERIMENT_VERSION}


def export_segment(segment_id: str, variant_id: str | None = None, *, force: bool = False) -> dict:
    settings = ensure_data_dirs()
    with connect() as conn:
        segment = fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [segment_id])
        if not segment:
            raise KeyError(f"segment not found: {segment_id}")
        if variant_id:
            variant = fetch_one(conn, "SELECT * FROM slice_variants WHERE id = ?", [variant_id])
        else:
            variant = fetch_one(
                conn,
                "SELECT * FROM slice_variants WHERE candidate_segment_id = ? ORDER BY created_at LIMIT 1",
                [segment_id],
            )
    if not variant:
        variant = create_variant(segment_id)

    preflight = export_preflight(segment_id, variant_id=variant["id"])
    if not preflight["can_export"] and not force:
        raise PermissionError(preflight["summary"])

    video = get_video(segment["source_video_id"])
    video_path = Path(video["file_path"])
    export_dir = settings.exports_dir / segment["source_video_id"]
    export_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = export_dir / f"{segment_id}.srt"
    output_path = export_dir / f"{variant['id']}.mp4"
    cover_path = export_dir / f"{variant['id']}_cover.jpg"

    transcript_segments = _overlapping_transcript(video, segment["start_time"], segment["end_time"])
    if not transcript_segments:
        transcript_segments = [
            {
                "start": float(segment["start_time"]),
                "end": float(segment["end_time"]),
                "text": segment.get("transcript") or variant["title"],
            }
        ]
    write_srt(transcript_segments, subtitle_path, offset=float(segment["start_time"]))
    export_vertical_clip(
        video_path,
        output_path,
        float(segment["start_time"]),
        float(segment["end_time"]),
        subtitle_path,
    )
    extract_frame(video_path, cover_path, float(segment.get("cover_time") or segment["start_time"]))

    with connect() as conn:
        conn.execute(
            "UPDATE slice_variants SET export_path = ?, status = ?, updated_at = ? WHERE id = ?",
            [str(output_path), "exported", utc_now(), variant["id"]],
        )
        conn.commit()
    record_artifact(
        segment["source_video_id"],
        step="exports",
        artifact_type="export_preview",
        artifact_path=output_path,
        status="ready",
        summary={"segment_id": segment_id, "variant_id": variant["id"], "subtitle_path": str(subtitle_path)},
    )
    return {
        "segment_id": segment_id,
        "variant_id": variant["id"],
        "export_path": str(output_path),
        "subtitle_path": str(subtitle_path),
        "cover_path": str(cover_path),
        "rights_risk": preflight["rights_risk"],
        "rights_mode": rights_mode(),
        "rights_notes": preflight["rights_notes"],
        "export_preflight": preflight,
        "quality_gate": preflight.get("quality_gate"),
        "quality_warnings": preflight.get("warnings", []),
        "component_versions": component_versions(),
    }


def export_preflight(segment_id: str, variant_id: str | None = None) -> dict:
    with connect() as conn:
        segment = fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [segment_id])
        score = fetch_one(conn, "SELECT * FROM slice_scores WHERE candidate_segment_id = ?", [segment_id])
        variant = fetch_one(conn, "SELECT * FROM slice_variants WHERE id = ?", [variant_id]) if variant_id else None
    if not segment:
        raise KeyError(f"segment not found: {segment_id}")
    rights_risk, rights_notes, rights_allowed = rights_risk_for_segment(segment)
    reasons: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    status = "allow"

    def add(reason_status: str, key: str, label: str, detail: str) -> None:
        nonlocal status
        item = {"key": key, "label": label, "detail": detail}
        if reason_status == "block":
            reasons.append(item)
            status = "block"
        elif status != "block":
            warnings.append(item)
            status = "review"

    candidate_status = str(segment.get("status") or "candidate")
    if candidate_status in {"blocked", "rejected"}:
        add("block", "manual_blocked", "人工已暂缓", "候选被标记为 blocked/rejected，需解除后才能导出。")
    if not rights_allowed or rights_risk >= 80:
        add("block", "rights_block", "授权风险阻断", "；".join(rights_notes) or f"rights_risk={rights_risk:.0f}")
    low_originality = float((score or {}).get("low_originality_score") or 0)
    if low_originality >= 80:
        add("block", "low_originality_block", "低原创风险阻断", f"low_originality_score={low_originality:.0f}")
    if not score:
        add("review", "missing_score", "候选尚未评分", "建议先运行评分再导出预览。")
    elif low_originality >= 45:
        add("review", "low_originality_review", "低原创风险复核", f"low_originality_score={low_originality:.0f}")
    if candidate_status in {"corrected", "review", "needs_review"}:
        add("review", "manual_review_required", "人工复核未通过", f"candidate.status={candidate_status}")

    quality_gate = None
    try:
        quality_gate = (quality_insights(segment["source_video_id"], top_k=30).get("gate") or {})
        if quality_gate.get("status") == "block":
            add("review", "quality_gate_block", "节目质量 Gate 阻断", quality_gate.get("summary") or "质量 Gate 要求暂缓。")
        elif quality_gate.get("status") == "review":
            add("review", "quality_gate_review", "节目质量 Gate 复核", quality_gate.get("summary") or "质量 Gate 要求复核。")
    except Exception as exc:
        add("review", "quality_gate_unavailable", "质量 Gate 暂不可用", str(exc))

    label = {"allow": "可导出", "review": "可导出但需复核", "block": "暂缓导出"}[status]
    summary = reasons[0]["detail"] if reasons else (warnings[0]["detail"] if warnings else "导出前检查通过。")
    return {
        "status": status,
        "label": label,
        "can_export": status != "block",
        "force_required": status == "block",
        "summary": summary,
        "segment_id": segment_id,
        "variant_id": variant_id or (variant or {}).get("id") or "",
        "candidate_status": candidate_status,
        "rights_risk": rights_risk,
        "rights_notes": rights_notes,
        "reasons": reasons,
        "warnings": warnings,
        "quality_gate": quality_gate,
        "component_versions": component_versions(),
    }


def list_variants(segment_id: str) -> list[dict]:
    with connect() as conn:
        return fetch_all(
            conn,
            "SELECT * FROM slice_variants WHERE candidate_segment_id = ? ORDER BY created_at DESC",
            [segment_id],
        )


def list_experiments(variant_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM publishing_experiments"
    params: list[Any] = []
    if variant_id:
        query += " WHERE slice_variant_id = ?"
        params.append(variant_id)
    query += " ORDER BY created_at DESC"
    with connect() as conn:
        return fetch_all(conn, query, params)


def _first_title(score: dict | None) -> str | None:
    if not score:
        return None
    try:
        titles = json.loads(score["title_suggestions"])
    except Exception:
        return None
    return titles[0] if titles else None


def _update_row(conn, table: str, row_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [*updates.values(), row_id])


def _float_or_none(value: Any, *, default: Any = None) -> float | None:
    if value is None or value == "":
        value = default
    if value is None or value == "":
        return None
    return round(float(value), 3)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _overlapping_transcript(video: dict, start: float, end: float) -> list[dict]:
    path = video.get("transcript_path")
    if not path:
        return []
    data = read_json(Path(path), default={}) or {}
    segments = []
    for seg in data.get("segments") or []:
        seg_start = float(seg["start"])
        seg_end = float(seg["end"])
        if seg_end >= start and seg_start <= end:
            clipped_start = max(seg_start, start)
            clipped_end = min(seg_end, end)
            if clipped_end > clipped_start:
                clipped = dict(seg)
                clipped["start"] = clipped_start
                clipped["end"] = clipped_end
                segments.append(clipped)
    return segments
