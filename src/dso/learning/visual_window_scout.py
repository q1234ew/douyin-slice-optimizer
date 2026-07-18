from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.multimodal_validation import _build_asset_index
from dso.learning.qwen_embeddings import (
    QWEN_EMBEDDING_DIM,
    QWEN_EMBEDDING_MODEL,
    QwenEmbeddingClient,
    cosine_similarity,
    embedding_service_ready,
    quarantine_invalid_qwen_embedding_records,
)
from dso.media.ffmpeg import extract_frame, probe_video, require_binary
from dso.review import insert_change_event
from dso.utils import new_id, read_json, utc_now, write_json


VISUAL_WINDOW_SCOUT_VERSION = "material_visual_window_scout.v1.1"
VISUAL_WINDOW_BUILD_MANIFEST_VERSION = "material_visual_window_build_manifest.v1"
VISUAL_WINDOW_EXPERIMENT_VERSION = "material_visual_window_experiment.v1.1"
VISUAL_WINDOW_ENTITY_TYPE = "material_window"
VISUAL_WINDOW_MODALITY = "visual"
SCENE_FORMS = (
    "stage_performance",
    "rehearsal",
    "backstage_interview",
    "vocal_teaching",
    "compilation",
    "news_document",
    "unknown",
    "mixed",
)
PROGRAM_CONTEXT_MODES = ("present", "absent", "unknown")
SELECTION_QUALITIES = ("target", "useful", "irrelevant", "uncertain")
VISUAL_DOMINANT_FORMS = {"stage_performance", "rehearsal", "backstage_interview"}
TEXT_HEAVY_FORMS = {"compilation", "news_document"}
DEFAULT_FRAME_CACHE_LIMIT_BYTES = 512 * 1024 * 1024
DEFAULT_D11B_BATCH_SIZE = 10
MIN_D11B_EVALUATED_SAMPLES = 30
MIN_D11B_ACCOUNT_COUNT = 5
MIN_D11B_SCENE_FORM_COUNT = 3
BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")


def visual_window_scout_status(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 60,
    summary_only: bool = False,
    build_id: str | None = None,
) -> dict:
    latest = load_visual_window_build(build_id) if build_id else _latest_report()
    readiness = _cached_media_readiness(latest, account_id=account_id, dataset_id=dataset_id) if summary_only else None
    if readiness is None:
        readiness = material_visual_media_readiness(
            account_id=account_id,
            dataset_id=dataset_id,
            limit=limit,
            requires_audio=False,
        )
    annotations = material_window_annotation_index(confirmed_only=False)
    confirmed = [item for item in annotations.values() if item.get("review_status") == "confirmed"]
    review_queue = material_window_review_queue(limit=12, report=latest)
    batch_progress = _batch_review_progress(latest, annotations=annotations)
    effective_status = _effective_visual_window_status(latest, batch_progress=batch_progress)
    prototypes = _window_prototypes()
    return {
        "contract_version": VISUAL_WINDOW_SCOUT_VERSION,
        "status": effective_status or ("ready_for_scan" if readiness.get("eligible_count") else "empty"),
        "mode": "visual_first_text_assisted_window_scout",
        "media_readiness": readiness,
        "annotation_summary": {
            "confirmed_count": len(confirmed),
            "reopened_count": sum(1 for item in annotations.values() if item.get("review_status") == "reopened"),
            "scene_form_counts": dict(Counter(str(item.get("scene_form") or "unknown") for item in confirmed)),
        },
        "prototype_summary": {
            "prototype_count": len(prototypes),
            "scene_forms": sorted(prototypes),
            "sample_counts": {key: int(value.get("sample_count") or 0) for key, value in prototypes.items()},
        },
        "latest_build": _build_summary(latest),
        "batch_progress": batch_progress,
        "build_history": _visual_window_build_history(limit=6),
        "review_queue": review_queue,
        "scene_form_options": _option_contract(SCENE_FORMS),
        "program_context_options": _option_contract(PROGRAM_CONTEXT_MODES),
        "selection_quality_options": _option_contract(SELECTION_QUALITIES),
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _cached_media_readiness(
    report: dict,
    *,
    account_id: str | None,
    dataset_id: str | None,
) -> dict | None:
    query = report.get("query") if isinstance(report.get("query"), dict) else {}
    requested_account = account_id or "all"
    requested_dataset = dataset_id or "all"
    if str(query.get("account_id") or "all") != requested_account:
        return None
    if str(query.get("dataset_id") or "all") != requested_dataset:
        return None
    summary = report.get("media_readiness") if isinstance(report.get("media_readiness"), dict) else {}
    if not summary:
        return None
    eligible_count = int(summary.get("eligible_count") or 0)
    visual_ready_count = int(summary.get("visual_ready_count") or 0)
    return {
        "status": "ready" if eligible_count else "empty",
        "route": summary.get("route") or "visual_audio_optional",
        "requires_audio": False,
        "confirmed_gold_count": int(summary.get("confirmed_gold_count") or 0),
        "eligible_count": eligible_count,
        "eligible_rate": float(summary.get("eligible_rate") or 0),
        "visual_ready_count": visual_ready_count,
        "audio_ready_count": int(summary.get("audio_ready_count") or 0),
        "audio_optional_eligible_count": visual_ready_count,
        "exclusion_reason_counts": {},
        "samples": [],
        "source": "latest_build_summary",
    }


def material_visual_media_readiness(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 100,
    requires_audio: bool = False,
    duration_tolerance: float = 0.15,
) -> dict:
    clauses = ["g.review_status = 'confirmed'"]
    params: list[Any] = []
    if account_id:
        clauses.append("h.account_id = ?")
        params.append(account_id)
    if dataset_id:
        clauses.append("h.dataset_id = ?")
        params.append(dataset_id)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT h.id AS sample_id, h.account_id, h.dataset_id, h.platform_item_id,
                   h.platform_url, h.title, h.duration_seconds AS expected_duration_seconds,
                   g.material_type AS gold_material_type, g.program_context,
                   g.presentation_style
            FROM historical_capture_samples h
            JOIN material_gold_annotations g ON g.sample_id = h.id
            WHERE {' AND '.join(clauses)}
            ORDER BY g.updated_at DESC, h.id
            """,
            params,
        )
    assets = _build_asset_index()
    audited = [
        _visual_media_contract(
            row,
            assets.get(str(row.get("platform_item_id") or "")) or {},
            requires_audio=requires_audio,
            duration_tolerance=max(0.0, float(duration_tolerance)),
        )
        for row in rows
    ]
    eligible = [item for item in audited if item.get("eligible")]
    cap = max(0, int(limit or 0))
    returned = eligible[:cap] if cap else eligible
    return {
        "status": "ready" if eligible else "empty",
        "route": "audio_required" if requires_audio else "visual_audio_optional",
        "requires_audio": bool(requires_audio),
        "confirmed_gold_count": len(audited),
        "eligible_count": len(eligible),
        "eligible_rate": round(len(eligible) / max(1, len(audited)), 4),
        "visual_ready_count": sum(1 for item in audited if item.get("visual_ready")),
        "audio_ready_count": sum(1 for item in audited if item.get("audio_ready")),
        "audio_optional_eligible_count": sum(1 for item in audited if item.get("visual_ready")),
        "exclusion_reason_counts": dict(Counter(reason for item in audited for reason in item.get("exclusion_reasons") or [])),
        "samples": returned,
    }


def build_visual_window_scout(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    sample_ids: list[str] | None = None,
    limit: int = 5,
    window_seconds: float = 15.0,
    stride_seconds: float = 5.0,
    max_windows_per_sample: int = 3,
    force: bool = False,
    load_model: bool = False,
    scan_scenes: bool = True,
    frame_cache_limit_bytes: int = DEFAULT_FRAME_CACHE_LIMIT_BYTES,
    batch_mode: str = "next",
    exclude_reviewed: bool = True,
    resume_pending: bool = True,
    client: QwenEmbeddingClient | None = None,
) -> dict:
    mode = str(batch_mode or "next").strip().lower()
    if mode not in {"next", "explicit"}:
        raise ValueError("batch_mode must be next or explicit")
    requested = {str(value).strip() for value in (sample_ids or []) if str(value).strip()}
    if requested:
        mode = "explicit"
    if mode == "next" and resume_pending:
        latest = _latest_report()
        pending = material_window_review_queue(limit=1, report=latest)
        if int(pending.get("pending_count") or 0) > 0:
            resumed = dict(latest)
            resumed["status"] = "review_in_progress"
            resumed["resumed_pending_batch"] = True
            resumed["review_queue"] = pending
            return resumed

    quarantined_invalid = quarantine_invalid_qwen_embedding_records()
    window_seconds = max(5.0, float(window_seconds or 15.0))
    stride_seconds = max(1.0, float(stride_seconds or 5.0))
    max_windows = max(3, min(60, int(max_windows_per_sample or 3)))
    readiness = material_visual_media_readiness(
        account_id=account_id,
        dataset_id=dataset_id,
        limit=0,
        requires_audio=False,
    )
    all_eligible = list(readiness.get("samples") or [])
    reviewed_sample_ids = _reviewed_window_sample_ids() if exclude_reviewed else set()
    eligible, selection_summary = _select_visual_window_batch(
        all_eligible,
        requested_sample_ids=requested,
        reviewed_sample_ids=reviewed_sample_ids,
        limit=max(1, int(limit or 5)),
        batch_mode=mode,
    )
    if not eligible:
        return {
            "contract_version": VISUAL_WINDOW_SCOUT_VERSION,
            "status": "all_eligible_samples_reviewed" if all_eligible and exclude_reviewed else "empty",
            "mode": "visual_first_text_assisted_window_scout",
            "query": {
                "account_id": account_id or "all",
                "dataset_id": dataset_id or "all",
                "sample_ids": sorted(requested),
                "limit": int(limit or 5),
                "batch_mode": mode,
                "exclude_reviewed": bool(exclude_reviewed),
            },
            "selection_summary": selection_summary,
            "sample_count": 0,
            "candidate_count": 0,
            "embedding_ready_count": 0,
            "writes_main_semantic_labels": False,
            "rewrites_existing_gold": False,
            "calls_omni": False,
            "production_weight": False,
            "generated_at": utc_now(),
        }
    client = client or QwenEmbeddingClient(timeout_seconds=3.0)
    service_status = client.health()
    if load_model and not _service_ready(service_status):
        loaded = client.load()
        if _service_ready(loaded):
            service_status = loaded
    prototypes = _window_prototypes()
    text_evidence = _material_text_evidence_index()
    annotations = material_window_annotation_index(confirmed_only=False)
    cache_root = _cache_root()
    before_bytes = _directory_size(cache_root / "frames")
    sample_reports = []
    counts = Counter()
    errors = []
    for sample in eligible:
        try:
            sample_id = str(sample.get("sample_id") or "")
            sample_prototypes = _window_prototypes(exclude_sample_ids={sample_id})
            result = _build_sample_windows(
                sample,
                window_seconds=window_seconds,
                stride_seconds=stride_seconds,
                max_windows=max_windows,
                force=force,
                scan_scenes=scan_scenes,
                frame_cache_limit_bytes=max(1, int(frame_cache_limit_bytes or DEFAULT_FRAME_CACHE_LIMIT_BYTES)),
                service_status=service_status,
                client=client,
                prototypes=sample_prototypes,
                text_evidence=text_evidence.get(str(sample.get("sample_id") or "")) or [],
                annotation_index=annotations,
            )
            sample_reports.append(result)
            counts.update(result.get("build_counts") or {})
        except Exception as exc:
            errors.append({"sample_id": sample.get("sample_id"), "error": str(exc)})
    after_bytes = _directory_size(cache_root / "frames")
    candidate_count = sum(int(item.get("candidate_count") or 0) for item in sample_reports)
    embedding_ready = sum(int(item.get("embedding_ready_count") or 0) for item in sample_reports)
    embedding_coverage = round(embedding_ready / max(1, candidate_count), 4)
    prototype_ready = bool(prototypes)
    if sample_reports and embedding_coverage < 0.90:
        status = "needs_embedding_retry" if _service_ready(service_status) else "frames_ready_service_unavailable"
    elif embedding_ready and prototype_ready:
        status = "ready_for_window_gold_review"
    elif sample_reports and not _service_ready(service_status):
        status = "frames_ready_service_unavailable"
    elif sample_reports and not prototype_ready:
        status = "needs_window_gold"
    else:
        status = "empty"
    build_id = new_id("d11b")
    report = {
        "contract_version": VISUAL_WINDOW_SCOUT_VERSION,
        "build_id": build_id,
        "lifecycle": "frozen",
        "status": status,
        "mode": "visual_first_text_assisted_window_scout",
        "query": {
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "sample_ids": sorted(requested),
            "limit": int(limit or 5),
            "window_seconds": window_seconds,
            "stride_seconds": stride_seconds,
            "max_windows_per_sample": max_windows,
            "force": bool(force),
            "scan_scenes": bool(scan_scenes),
            "requires_audio": False,
            "batch_mode": mode,
            "exclude_reviewed": bool(exclude_reviewed),
            "resume_pending": bool(resume_pending),
        },
        "selection_summary": selection_summary,
        "service_status": service_status,
        "media_readiness": {key: readiness.get(key) for key in [
            "route", "confirmed_gold_count", "eligible_count", "eligible_rate", "visual_ready_count", "audio_ready_count"
        ]},
        "prototype_summary": {
            "prototype_count": len(prototypes),
            "scene_forms": sorted(prototypes),
            "source": "confirmed_window_gold_only",
            "evaluation_policy": "leave_one_sample_out",
        },
        "sample_count": len(sample_reports),
        "candidate_count": candidate_count,
        "embedding_ready_count": embedding_ready,
        "embedding_coverage": embedding_coverage,
        "embedding_gate": {
            "passed": candidate_count > 0 and embedding_coverage >= 0.90,
            "minimum_coverage": 0.90,
            "ready_count": embedding_ready,
            "candidate_count": candidate_count,
            "failed_count": max(0, candidate_count - embedding_ready),
        },
        "quarantined_invalid_embedding_count": quarantined_invalid,
        "build_counts": dict(counts),
        "persistent_frame_bytes_added": max(0, after_bytes - before_bytes),
        "frame_cache_bytes": after_bytes,
        "frame_cache_limit_bytes": max(1, int(frame_cache_limit_bytes or DEFAULT_FRAME_CACHE_LIMIT_BYTES)),
        "samples": sample_reports,
        "omni_top2_queue": [item for sample in sample_reports for item in sample.get("omni_top2_queue") or []],
        "errors": errors[:12],
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "calls_omni": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }
    manifest_summary = _persist_visual_window_build(report)
    report["manifest_summary"] = manifest_summary
    write_json(_latest_report_path(), report)
    return report


def material_window_review_queue(*, limit: int = 12, report: dict | None = None) -> dict:
    report = report or _latest_report()
    annotations = material_window_annotation_index(confirmed_only=False)
    rows = []
    for sample in report.get("samples") or []:
        for window in sample.get("review_windows") or []:
            window_id = str(window.get("window_id") or "")
            annotation = annotations.get(window_id)
            if annotation and annotation.get("review_status") == "confirmed":
                continue
            rows.append(
                {
                    "sample_id": sample.get("sample_id"),
                    "account_id": sample.get("account_id"),
                    "title": sample.get("title"),
                    "platform_url": sample.get("platform_url"),
                    "gold_material_type": sample.get("gold_material_type"),
                    "audio_source": sample.get("audio_source"),
                    **window,
                    "annotation": annotation,
                }
            )
    rows.sort(key=lambda item: (-len(item.get("selected_by") or []), -float(item.get("fusion_score") or 0.0), str(item.get("sample_id") or "")))
    cap = max(1, min(100, int(limit or 12)))
    return {
        "status": "ready" if rows else ("complete" if report.get("samples") else "not_started"),
        "count": min(cap, len(rows)),
        "pending_count": len(rows),
        "samples": rows[:cap],
    }


def material_window_annotation_index(*, confirmed_only: bool = False) -> dict[str, dict]:
    clauses = ["1 = 1"]
    if confirmed_only:
        clauses.append("review_status = 'confirmed'")
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"SELECT * FROM material_window_annotations WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC",
        )
    return {str(row.get("window_id") or ""): _window_annotation_contract(row) for row in rows if row.get("window_id")}


def _reviewed_window_sample_ids() -> set[str]:
    return {
        str(annotation.get("sample_id") or "")
        for annotation in material_window_annotation_index(confirmed_only=True).values()
        if str(annotation.get("sample_id") or "")
    }


def _select_visual_window_batch(
    eligible: list[dict],
    *,
    requested_sample_ids: set[str] | None,
    reviewed_sample_ids: set[str] | None,
    limit: int,
    batch_mode: str,
) -> tuple[list[dict], dict]:
    requested = {str(value) for value in (requested_sample_ids or set()) if str(value)}
    reviewed = {str(value) for value in (reviewed_sample_ids or set()) if str(value)}
    source = [
        item
        for item in eligible
        if not requested or str(item.get("sample_id") or "") in requested
    ]
    candidates = [item for item in source if str(item.get("sample_id") or "") not in reviewed]
    reviewed_rows = [item for item in eligible if str(item.get("sample_id") or "") in reviewed]
    reviewed_material_counts = Counter(str(item.get("gold_material_type") or "unknown") for item in reviewed_rows)
    reviewed_account_counts = Counter(str(item.get("account_id") or "unknown") for item in reviewed_rows)
    selected_material_counts: Counter[str] = Counter()
    selected_account_counts: Counter[str] = Counter()
    remaining = list(candidates)
    selected: list[dict] = []
    cap = max(1, int(limit or DEFAULT_D11B_BATCH_SIZE))
    while remaining and len(selected) < cap:
        item = min(
            remaining,
            key=lambda row: (
                reviewed_material_counts[str(row.get("gold_material_type") or "unknown")]
                + selected_material_counts[str(row.get("gold_material_type") or "unknown")],
                reviewed_account_counts[str(row.get("account_id") or "unknown")]
                + selected_account_counts[str(row.get("account_id") or "unknown")],
                _stable_batch_order_key(row),
            ),
        )
        remaining.remove(item)
        selected.append(item)
        selected_material_counts[str(item.get("gold_material_type") or "unknown")] += 1
        selected_account_counts[str(item.get("account_id") or "unknown")] += 1
    return selected, {
        "policy": "least_reviewed_material_then_account_stable_order",
        "batch_mode": batch_mode,
        "source_eligible_count": len(eligible),
        "requested_count": len(requested),
        "candidate_count_before_review_exclusion": len(source),
        "excluded_reviewed_sample_count": len(source) - len(candidates),
        "available_after_exclusion_count": len(candidates),
        "selected_count": len(selected),
        "selected_sample_ids": [str(item.get("sample_id") or "") for item in selected],
        "selected_material_type_counts": dict(selected_material_counts),
        "selected_account_counts": dict(selected_account_counts),
        "reviewed_sample_count": len(reviewed),
    }


def _stable_batch_order_key(row: dict) -> str:
    value = "|".join(
        [
            str(row.get("gold_material_type") or "unknown"),
            str(row.get("account_id") or "unknown"),
            str(row.get("sample_id") or ""),
        ]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def update_material_window_annotation(sample_id: str, payload: dict[str, Any]) -> dict:
    sample_key = str(sample_id or "").strip()
    start = round(float(payload.get("start_seconds") or 0.0), 3)
    end = round(float(payload.get("end_seconds") or 0.0), 3)
    if not sample_key:
        raise ValueError("sample_id is required")
    if end <= start:
        raise ValueError("end_seconds must be greater than start_seconds")
    scene_form = str(payload.get("scene_form") or "unknown").strip().lower()
    program_context_mode = str(payload.get("program_context_mode") or "unknown").strip().lower()
    selection_quality = str(payload.get("selection_quality") or "uncertain").strip().lower()
    if scene_form not in SCENE_FORMS:
        raise ValueError(f"scene_form must be one of: {', '.join(SCENE_FORMS)}")
    if program_context_mode not in PROGRAM_CONTEXT_MODES:
        raise ValueError(f"program_context_mode must be one of: {', '.join(PROGRAM_CONTEXT_MODES)}")
    if selection_quality not in SELECTION_QUALITIES:
        raise ValueError(f"selection_quality must be one of: {', '.join(SELECTION_QUALITIES)}")
    operator = str(payload.get("operator") or "workbench").strip() or "workbench"
    note = str(payload.get("review_note") or payload.get("reason") or "manual material window confirmation").strip()
    window_id = _window_entity_id(sample_key, start, end)
    now = utc_now()
    with connect() as conn:
        sample = fetch_one(conn, "SELECT id, account_id, dataset_id FROM historical_capture_samples WHERE id = ?", [sample_key])
        if not sample:
            raise KeyError(f"historical sample not found: {sample_key}")
        existing = fetch_one(conn, "SELECT * FROM material_window_annotations WHERE window_id = ?", [window_id])
        before = _window_annotation_contract(existing or {}) if existing else {}
        row = {
            "id": existing.get("id") if existing else new_id("matwin"),
            "window_id": window_id,
            "sample_id": sample_key,
            "account_id": sample.get("account_id") or "",
            "dataset_id": sample.get("dataset_id") or "",
            "start_seconds": start,
            "end_seconds": end,
            "scene_form": scene_form,
            "program_context_mode": program_context_mode,
            "selection_quality": selection_quality,
            "review_status": "confirmed",
            "operator": operator,
            "review_note": note,
            "created_at": existing.get("created_at") if existing else now,
            "updated_at": now,
        }
        if existing:
            conn.execute(
                """
                UPDATE material_window_annotations
                SET scene_form = ?, program_context_mode = ?, selection_quality = ?, review_status = 'confirmed',
                    operator = ?, review_note = ?, updated_at = ?
                WHERE window_id = ?
                """,
                [scene_form, program_context_mode, selection_quality, operator, note, now, window_id],
            )
        else:
            insert_row(conn, "material_window_annotations", row)
        after = _window_annotation_contract(row)
        insert_change_event(
            conn,
            entity_type="material_window_annotation",
            entity_id=window_id,
            change_type="material_window_confirmed",
            before=before,
            after=after,
            reason=note,
            operator=operator,
        )
        conn.commit()
    return {
        "contract_version": VISUAL_WINDOW_SCOUT_VERSION,
        "status": "confirmed",
        "sample_id": sample_key,
        "window_id": window_id,
        "annotation": after,
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
    }


def _resolve_visual_window_experiment_source(
    *,
    report: dict | None,
    build_id: str | None,
    build_ids: list[str] | None,
    scope: str,
) -> tuple[dict, list[str], str]:
    normalized_scope = str(scope or "cumulative").strip().lower()
    if normalized_scope not in {"cumulative", "single"}:
        raise ValueError("scope must be cumulative or single")
    if report is not None:
        inline_id = str(report.get("build_id") or "").strip()
        return report, ([inline_id] if inline_id else []), "inline_report"

    requested_ids = []
    for value in build_ids or []:
        normalized = _normalize_build_id(value)
        if normalized not in requested_ids:
            requested_ids.append(normalized)
    if requested_ids:
        source_ids = requested_ids
        evaluation_scope = "explicit_frozen_builds"
    else:
        latest = _latest_report()
        target_id = _normalize_build_id(build_id) if build_id else str(latest.get("build_id") or "").strip()
        if not target_id:
            return latest, [], "legacy_latest_report"
        source_ids = [target_id] if normalized_scope == "single" else _visual_window_build_ids_through(target_id)
        evaluation_scope = "single_frozen_build" if normalized_scope == "single" else "cumulative_frozen_builds"

    reports = [load_visual_window_build(value) for value in source_ids]
    if len(reports) == 1:
        source = dict(reports[0])
        source["build_ids"] = list(source_ids)
        source["source_build_count"] = 1
    else:
        source = _combine_visual_window_builds(reports, build_ids=source_ids)
    return source, source_ids, evaluation_scope


def _combine_visual_window_builds(reports: list[dict], *, build_ids: list[str]) -> dict:
    samples_by_id: dict[str, dict] = {}
    for report in reports:
        for sample in report.get("samples") or []:
            sample_id = str(sample.get("sample_id") or "")
            if sample_id:
                samples_by_id[sample_id] = sample
    samples = list(samples_by_id.values())
    candidate_count = sum(int(sample.get("candidate_count") or len(sample.get("candidates") or [])) for sample in samples)
    embedding_ready_count = sum(
        int(
            sample.get("embedding_ready_count")
            or sum(
                1
                for candidate in sample.get("candidates") or []
                if candidate.get("embedding_status") in {"created", "reused"}
            )
        )
        for sample in samples
    )
    latest = dict(reports[-1]) if reports else {}
    return {
        **latest,
        "build_id": build_ids[-1] if build_ids else latest.get("build_id"),
        "build_ids": list(build_ids),
        "source_build_count": len(build_ids),
        "sample_count": len(samples),
        "candidate_count": candidate_count,
        "embedding_ready_count": embedding_ready_count,
        "embedding_coverage": round(embedding_ready_count / max(1, candidate_count), 4),
        "samples": samples,
        "omni_top2_queue": [
            item
            for sample in samples
            for item in sample.get("omni_top2_queue") or []
        ],
    }


def run_visual_window_experiment(
    *,
    report: dict | None = None,
    build_id: str | None = None,
    build_ids: list[str] | None = None,
    scope: str = "cumulative",
    persist: bool = True,
) -> dict:
    source_report, source_build_ids, evaluation_scope = _resolve_visual_window_experiment_source(
        report=report,
        build_id=build_id,
        build_ids=build_ids,
        scope=scope,
    )
    source_build_id = source_build_ids[-1] if source_build_ids else str(source_report.get("build_id") or build_id or "")
    annotations = material_window_annotation_index(confirmed_only=True)
    strategies = ("fixed", "text", "visual", "fusion")
    strategy_rows: dict[str, list[dict]] = {key: [] for key in strategies}
    samples = list(source_report.get("samples") or [])
    for sample in samples:
        candidate_index = {str(item.get("window_id") or ""): item for item in sample.get("candidates") or []}
        for strategy in strategies:
            selected_ids = [
                str(value)
                for value in (sample.get("strategy_windows") or {}).get(strategy) or []
                if str(value)
            ]
            strategy_rows[strategy].append(
                _evaluate_strategy_sample(
                    sample,
                    strategy=strategy,
                    selected_ids=selected_ids,
                    candidate_index=candidate_index,
                    annotations=annotations,
                )
            )

    comparison = {
        key: _strategy_metrics(rows, total_samples=len(samples))
        for key, rows in strategy_rows.items()
    }
    paired_comparison = {
        "visual_vs_fixed": _paired_strategy_metrics("visual", strategy_rows["visual"], "fixed", strategy_rows["fixed"]),
        "fusion_vs_fixed": _paired_strategy_metrics("fusion", strategy_rows["fusion"], "fixed", strategy_rows["fixed"]),
        "fusion_vs_text": _paired_strategy_metrics("fusion", strategy_rows["fusion"], "text", strategy_rows["text"]),
    }
    gold_summary = _experiment_gold_summary(source_report, annotations=annotations)
    leakage_guard = _prototype_leakage_guard(source_report)
    manifest_verification = _build_manifests_verification(source_build_ids)
    embedding_summary = _experiment_embedding_summary(source_report)
    fusion = comparison["fusion"]
    fusion_vs_fixed = paired_comparison["fusion_vs_fixed"]
    fusion_vs_text = paired_comparison["fusion_vs_text"]
    fusion_recall = fusion.get("recall_at_2")
    severe_rate = fusion.get("severe_miss_rate")
    fixed_delta = fusion_vs_fixed.get("recall_delta") if fusion_vs_fixed.get("status") == "ready" else None
    text_delta = fusion_vs_text.get("recall_delta") if fusion_vs_text.get("status") == "ready" else None
    evaluated = int(fusion.get("evaluable_sample_count") or 0)
    paired_fixed_count = int(fusion_vs_fixed.get("paired_sample_count") or 0)
    paired_text_count = int(fusion_vs_text.get("paired_sample_count") or 0)
    text_comparison_required = paired_text_count >= 10
    text_gate_passed = not text_comparison_required or (text_delta is not None and text_delta >= 0.15)
    passed = all(
        [
            evaluated >= MIN_D11B_EVALUATED_SAMPLES,
            paired_fixed_count >= MIN_D11B_EVALUATED_SAMPLES,
            fusion_recall is not None and fusion_recall >= 0.70,
            fixed_delta is not None and fixed_delta >= 0.10,
            severe_rate is not None and severe_rate <= 0.10,
            float(fusion.get("decision_coverage") or 0.0) >= 0.75,
            int(gold_summary.get("account_count") or 0) >= MIN_D11B_ACCOUNT_COUNT,
            int(gold_summary.get("positive_scene_form_count") or 0) >= MIN_D11B_SCENE_FORM_COUNT,
            int(leakage_guard.get("violation_count") or 0) == 0,
            bool(manifest_verification.get("passed")),
            bool(embedding_summary.get("passed")),
            text_gate_passed,
        ]
    )
    if passed:
        status = "eligible_for_omni_top2_validation"
    elif source_build_ids and not embedding_summary.get("passed"):
        status = "needs_embedding_retry"
    elif evaluated < 3 or int(gold_summary.get("pending_sample_count") or 0) > 0:
        status = "needs_window_gold"
    else:
        status = "research_only"
    result = {
        "contract_version": VISUAL_WINDOW_EXPERIMENT_VERSION,
        "experiment_id": new_id("d11bexp"),
        "build_id": source_build_id,
        "build_ids": source_build_ids,
        "source_build_count": len(source_build_ids),
        "evaluation_scope": evaluation_scope,
        "status": status,
        "mode": "paired_fixed_vs_text_vs_visual_vs_fusion",
        "strategy_comparison": comparison,
        "paired_comparison": paired_comparison,
        "evaluation_summary": gold_summary,
        "leakage_guard_summary": leakage_guard,
        "build_manifest_verification": manifest_verification,
        "embedding_coverage_summary": embedding_summary,
        "promotion_gate": {
            "passed": passed,
            "status": status,
            "requirements": {
                "minimum_evaluated_samples": MIN_D11B_EVALUATED_SAMPLES,
                "minimum_paired_fixed_samples": MIN_D11B_EVALUATED_SAMPLES,
                "minimum_account_count": MIN_D11B_ACCOUNT_COUNT,
                "minimum_positive_scene_form_count": MIN_D11B_SCENE_FORM_COUNT,
                "minimum_decision_coverage": 0.75,
                "fusion_recall_at_2": 0.70,
                "delta_vs_fixed": 0.10,
                "delta_vs_text": 0.15,
                "delta_vs_text_policy": "required only when at least 10 paired text samples exist",
                "maximum_severe_miss_rate": 0.10,
                "maximum_prototype_leakage_violations": 0,
                "requires_verified_build_manifest": True,
                "minimum_embedding_coverage": 0.90,
            },
            "observed": {
                "evaluated_samples": evaluated,
                "paired_fixed_samples": paired_fixed_count,
                "paired_text_samples": paired_text_count,
                "fusion_recall_at_2": fusion_recall,
                "fusion_decision_coverage": fusion.get("decision_coverage"),
                "fusion_unknown_abstention_rate": fusion.get("unknown_abstention_rate"),
                "delta_vs_fixed": fixed_delta,
                "delta_vs_text": text_delta,
                "text_comparison_required": text_comparison_required,
                "text_comparison_status": "ready" if text_delta is not None else "not_applicable",
                "severe_miss_rate": severe_rate,
                "account_count": gold_summary.get("account_count"),
                "positive_scene_form_count": gold_summary.get("positive_scene_form_count"),
                "prototype_leakage_violation_count": leakage_guard.get("violation_count"),
                "build_manifest_verified": manifest_verification.get("passed"),
                "embedding_coverage": embedding_summary.get("coverage"),
                "embedding_coverage_passed": embedding_summary.get("passed"),
            },
        },
        "omni_top2_queue": source_report.get("omni_top2_queue") or [],
        "calls_omni": False,
        "writes_main_semantic_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }
    if persist and source_build_id:
        result["report_summary"] = _persist_visual_window_experiment(result)
    return result


def visual_window_frame_path(sample_id: str, window_key: str, frame_name: str) -> Path:
    safe_sample = _safe_name(sample_id)
    safe_window = _safe_name(window_key)
    safe_frame = Path(frame_name).name
    if safe_frame not in {"frame_1.jpg", "frame_2.jpg", "frame_3.jpg"}:
        raise FileNotFoundError(frame_name)
    path = (_cache_root() / "frames" / safe_sample / safe_window / safe_frame).resolve()
    root = (_cache_root() / "frames").resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(frame_name)
    return path


def _build_sample_windows(
    sample: dict,
    *,
    window_seconds: float,
    stride_seconds: float,
    max_windows: int,
    force: bool,
    scan_scenes: bool,
    frame_cache_limit_bytes: int,
    service_status: dict,
    client: QwenEmbeddingClient,
    prototypes: dict[str, dict],
    text_evidence: list[dict],
    annotation_index: dict[str, dict],
) -> dict:
    sample_id = str(sample.get("sample_id") or "")
    video_path = Path(str(sample.get("video_path") or ""))
    duration = float(sample.get("actual_duration_seconds") or 0.0)
    scene_times = _scene_change_times(video_path) if scan_scenes else []
    starts = _candidate_starts(
        duration,
        window_seconds=window_seconds,
        stride_seconds=stride_seconds,
        scene_times=scene_times,
        limit=max_windows,
    )
    fixed_targets = _fixed_starts(duration, window_seconds)
    counts = Counter()
    candidates = []
    for start in starts:
        end = min(duration, start + window_seconds)
        window_id = _window_entity_id(sample_id, start, end)
        window_key = _window_key(start, end)
        if _directory_size(_cache_root() / "frames") >= frame_cache_limit_bytes:
            raise RuntimeError("visual_window_frame_cache_limit_reached")
        frames, created_count = _window_frames(video_path, sample_id=sample_id, window_key=window_key, start=start, end=end)
        counts["frames_created"] += created_count
        vector, vector_status = _window_embedding(
            sample,
            window_id=window_id,
            frame_paths=frames,
            force=force,
            service_status=service_status,
            client=client,
        )
        counts[vector_status] += 1
        prototype_scores = _prototype_scores(vector, prototypes)
        predicted_scene_form = max(prototype_scores, key=prototype_scores.get) if prototype_scores else "unknown"
        raw_similarity = float(prototype_scores.get(predicted_scene_form) or 0.0)
        visual_similarity = max(0.0, min(1.0, (raw_similarity - 0.45) / 0.55)) if raw_similarity else 0.0
        scene_count = sum(1 for value in scene_times if start <= value < end)
        scene_score = min(1.0, scene_count / 5.0)
        visual_score = visual_similarity * 0.85 + scene_score * 0.15 if vector and prototypes else scene_score * 0.20
        text_score, text_available, text_reason = _text_score_for_window(start, end, text_evidence)
        fusion_score, weights = dynamic_window_fusion(
            visual_score=visual_score,
            text_score=text_score,
            visual_available=bool(vector and prototypes),
            text_available=text_available,
            predicted_scene_form=predicted_scene_form,
        )
        frame_urls = [
            f"/learning/visual-window-scout/frames/{sample_id}/{window_key}/{path.name}"
            for path in frames
        ]
        candidates.append(
            {
                "window_id": window_id,
                "window_key": window_key,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "frame_urls": frame_urls,
                "embedding_status": vector_status,
                "predicted_scene_form": predicted_scene_form,
                "prototype_scores": {key: round(value, 4) for key, value in sorted(prototype_scores.items())},
                "visual_similarity": round(raw_similarity, 4),
                "visual_score": round(visual_score, 4),
                "text_score": round(text_score, 4),
                "text_available": text_available,
                "text_reason": text_reason,
                "scene_change_count": scene_count,
                "scene_change_score": round(scene_score, 4),
                "fusion_score": round(fusion_score, 4),
                "fusion_weights": weights,
                "annotation": annotation_index.get(window_id),
                "is_fixed_window": any(abs(start - target) <= 0.51 for target in fixed_targets),
            }
        )
    fixed = _nearest_window_ids(candidates, fixed_targets, limit=3)
    text_selected = _ranked_window_ids(candidates, "text_score", limit=2, require=lambda item: bool(item.get("text_available")))
    visual_selected = _ranked_window_ids(candidates, "visual_score", limit=2, require=lambda item: bool(item.get("embedding_status") in {"created", "reused"}))
    fusion_selected = _ranked_window_ids(candidates, "fusion_score", limit=2)
    if not fusion_selected:
        fusion_selected = _ranked_window_ids(candidates, "scene_change_score", limit=2)
    strategy_windows = {
        "fixed": fixed[:2],
        "text": text_selected,
        "visual": visual_selected,
        "fusion": fusion_selected,
    }
    selected_by: dict[str, list[str]] = defaultdict(list)
    for strategy, window_ids in strategy_windows.items():
        for window_id in window_ids:
            selected_by[window_id].append(strategy)
    candidate_index = {str(item.get("window_id") or ""): item for item in candidates}
    review_windows = []
    for window_id, strategies in selected_by.items():
        item = dict(candidate_index.get(window_id) or {})
        item["selected_by"] = strategies
        review_windows.append(item)
    review_windows.sort(key=lambda item: (-len(item.get("selected_by") or []), -float(item.get("fusion_score") or 0.0)))
    omni_ids = fusion_selected or visual_selected or fixed[:2]
    return {
        "sample_id": sample_id,
        "account_id": sample.get("account_id"),
        "dataset_id": sample.get("dataset_id"),
        "platform_item_id": sample.get("platform_item_id"),
        "platform_url": sample.get("platform_url"),
        "title": sample.get("title"),
        "gold_material_type": sample.get("gold_material_type"),
        "video_path": str(video_path),
        "duration_seconds": round(duration, 3),
        "audio_source": sample.get("audio_source"),
        "prototype_summary": {
            "policy": "leave_one_sample_out",
            "excluded_sample_id": sample_id,
            "prototype_count": len(prototypes),
            "scene_forms": sorted(prototypes),
            "source_sample_ids": sorted(
                {
                    str(source_sample_id)
                    for prototype in prototypes.values()
                    for source_sample_id in prototype.get("sample_ids") or []
                    if str(source_sample_id)
                }
            ),
        },
        "scene_change_count": len(scene_times),
        "candidate_count": len(candidates),
        "embedding_ready_count": sum(1 for item in candidates if item.get("embedding_status") in {"created", "reused"}),
        "build_counts": dict(counts),
        "strategy_windows": strategy_windows,
        "review_windows": review_windows,
        "candidates": sorted(candidates, key=lambda item: (-float(item.get("fusion_score") or 0.0), float(item.get("start_seconds") or 0.0))),
        "omni_top2_queue": [
            {
                "sample_id": sample_id,
                "window_id": window_id,
                "video_path": str(video_path),
                "start_seconds": candidate_index[window_id].get("start_seconds"),
                "end_seconds": candidate_index[window_id].get("end_seconds"),
                "reason": "visual_fusion_top2",
            }
            for window_id in omni_ids
            if window_id in candidate_index
        ],
    }


def dynamic_window_fusion(
    *,
    visual_score: float,
    text_score: float,
    visual_available: bool,
    text_available: bool,
    predicted_scene_form: str = "unknown",
) -> tuple[float, dict[str, float]]:
    if not visual_available and not text_available:
        return 0.0, {"visual": 0.0, "text": 0.0}
    if visual_available and not text_available:
        return max(0.0, min(1.0, visual_score)), {"visual": 1.0, "text": 0.0}
    if text_available and not visual_available:
        return max(0.0, min(1.0, text_score)), {"visual": 0.0, "text": 1.0}
    if predicted_scene_form in VISUAL_DOMINANT_FORMS:
        visual_weight = 0.75
    elif predicted_scene_form in TEXT_HEAVY_FORMS:
        visual_weight = 0.45
    else:
        visual_weight = 0.60
    text_weight = 1.0 - visual_weight
    score = max(0.0, min(1.0, visual_score * visual_weight + text_score * text_weight))
    return score, {"visual": round(visual_weight, 4), "text": round(text_weight, 4)}


def _visual_media_contract(row: dict, assets: dict, *, requires_audio: bool, duration_tolerance: float) -> dict:
    video_path = _first_existing(assets.get("video") or [])
    external_audio_path = _first_existing(assets.get("audio") or [])
    video_probe = _safe_probe(video_path)
    audio_probe = _safe_probe(external_audio_path)
    expected = float(row.get("expected_duration_seconds") or 0.0)
    actual = float(video_probe.get("duration_seconds") or 0.0)
    ratio = actual / expected if actual > 0 and expected > 0 else 0.0
    embedded_audio = int(video_probe.get("audio_streams") or 0) > 0
    external_audio = bool(external_audio_path and int(audio_probe.get("audio_streams") or 0) > 0)
    visual_reasons = []
    if not video_path:
        visual_reasons.append("video_missing")
    if video_path and video_probe.get("error"):
        visual_reasons.append("probe_error")
    if video_path and not video_probe.get("error") and actual <= 0:
        visual_reasons.append("duration_missing")
    if expected > 0 and actual > 0 and abs(ratio - 1.0) > duration_tolerance:
        visual_reasons.append("duration_mismatch")
    visual_ready = not visual_reasons
    audio_ready = embedded_audio or external_audio
    reasons = list(visual_reasons)
    if requires_audio and not audio_ready:
        reasons.append("audio_missing")
    return {
        **row,
        "video_path": str(video_path) if video_path else "",
        "external_audio_path": str(external_audio_path) if external_audio_path else "",
        "actual_duration_seconds": round(actual, 3),
        "duration_ratio": round(ratio, 4),
        "visual_ready": visual_ready,
        "audio_ready": audio_ready,
        "audio_source": "embedded_audio" if embedded_audio else ("external_audio" if external_audio else "missing_audio"),
        "eligible": visual_ready and (audio_ready or not requires_audio),
        "exclusion_reasons": reasons,
    }


def _candidate_starts(
    duration: float,
    *,
    window_seconds: float,
    stride_seconds: float,
    scene_times: list[float],
    limit: int,
) -> list[float]:
    max_start = max(0.0, duration - window_seconds)
    all_regular = []
    value = 0.0
    while value <= max_start + 0.001:
        all_regular.append(round(min(max_start, value), 3))
        value += stride_seconds
    if max_start and (not all_regular or abs(all_regular[-1] - max_start) > 0.5):
        all_regular.append(round(max_start, 3))
    fixed = _fixed_starts(duration, window_seconds)
    scene_starts = [round(max(0.0, min(max_start, value - window_seconds / 3.0)), 3) for value in scene_times]
    reserved = sorted(set([*fixed, *_even_sample(scene_starts, min(max(0, limit // 2), len(scene_starts)))]))
    remaining = max(0, limit - len(reserved))
    regular = _even_sample(all_regular, remaining)
    combined = sorted(set(round(value, 3) for value in [*reserved, *regular]))
    if len(combined) > limit:
        combined = _even_sample(combined, limit)
    return combined


def _fixed_starts(duration: float, window_seconds: float) -> list[float]:
    max_start = max(0.0, duration - window_seconds)
    return [round(value, 3) for value in [0.0, min(max_start, duration * 0.45), min(max_start, duration * 0.78)]]


def _scene_change_times(video_path: Path, threshold: float = 0.35) -> list[float]:
    ffmpeg = require_binary("ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video_path),
            "-vf",
            f"select=gt(scene\\,{threshold:.2f}),showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return sorted(set(round(float(value), 3) for value in re.findall(r"pts_time:([0-9.]+)", result.stderr or "")))


def _window_frames(video_path: Path, *, sample_id: str, window_key: str, start: float, end: float) -> tuple[list[Path], int]:
    root = _cache_root() / "frames" / _safe_name(sample_id) / _safe_name(window_key)
    times = [min(end - 0.1, start + 0.5), (start + end) / 2.0, max(start + 0.5, end - 0.5)]
    paths = []
    created = 0
    for index, timestamp in enumerate(times, start=1):
        target = root / f"frame_{index}.jpg"
        if not target.is_file():
            extract_frame(video_path, target, max(0.0, timestamp))
            created += 1
        if target.is_file():
            paths.append(target)
    return paths, created


def _window_embedding(
    sample: dict,
    *,
    window_id: str,
    frame_paths: list[Path],
    force: bool,
    service_status: dict,
    client: QwenEmbeddingClient,
) -> tuple[list[float], str]:
    source_key = "|".join(f"{path}:{path.stat().st_size}:{int(path.stat().st_mtime)}" for path in frame_paths if path.is_file())
    source_hash = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    cached = _ready_embedding_record(window_id, source_hash)
    if cached and not force:
        vector = _load_vector(cached)
        if vector:
            return vector, "reused"
    if not _service_ready(service_status):
        return [], "service_unavailable"
    if not frame_paths:
        return [], "frames_missing"
    try:
        vector = client.embed_video_frames(frame_paths[:3])
    except Exception as exc:
        _record_embedding_failure(sample, window_id=window_id, source_hash=source_hash, error=str(exc))
        return [], "failed"
    if not vector:
        return [], "failed"
    vector_path = _cache_root() / "vectors" / f"{window_id}_{source_hash[:16]}.json"
    write_json(
        vector_path,
        {
            "contract_version": VISUAL_WINDOW_SCOUT_VERSION,
            "entity_type": VISUAL_WINDOW_ENTITY_TYPE,
            "entity_id": window_id,
            "modality": VISUAL_WINDOW_MODALITY,
            "model_name": QWEN_EMBEDDING_MODEL,
            "source_hash": source_hash,
            "vector_dim": len(vector),
            "vector": vector,
            "created_at": utc_now(),
        },
    )
    _store_embedding_record(sample, window_id=window_id, source_hash=source_hash, vector_path=vector_path, vector_dim=len(vector), status="ready", error="")
    return vector, "created"


def _window_prototypes(*, exclude_sample_ids: set[str] | None = None) -> dict[str, dict]:
    annotations = material_window_annotation_index(confirmed_only=True)
    excluded = {str(value) for value in (exclude_sample_ids or set()) if str(value)}
    positive_annotations = {
        window_id: annotation
        for window_id, annotation in annotations.items()
        if str(annotation.get("selection_quality") or "") in {"target", "useful"}
        and str(annotation.get("sample_id") or "") not in excluded
    }
    if not positive_annotations:
        return {}
    placeholders = ", ".join("?" for _ in positive_annotations)
    with connect() as conn:
        records = fetch_all(
            conn,
            f"""
            SELECT * FROM embedding_records
            WHERE entity_type = ? AND entity_id IN ({placeholders}) AND modality = ?
              AND model_name = ? AND status = 'ready' AND vector_dim = ?
            ORDER BY updated_at DESC
            """,
            [
                VISUAL_WINDOW_ENTITY_TYPE,
                *positive_annotations,
                VISUAL_WINDOW_MODALITY,
                QWEN_EMBEDDING_MODEL,
                QWEN_EMBEDDING_DIM,
            ],
        )
    latest_records: dict[str, dict] = {}
    for record in records:
        latest_records.setdefault(str(record.get("entity_id") or ""), record)
    groups: dict[str, list[list[float]]] = defaultdict(list)
    examples: dict[str, list[str]] = defaultdict(list)
    sample_ids: dict[str, set[str]] = defaultdict(set)
    for window_id, annotation in positive_annotations.items():
        scene_form = str(annotation.get("scene_form") or "unknown")
        if scene_form in {"unknown", "mixed"}:
            continue
        vector = _load_vector(latest_records.get(window_id) or {})
        if vector:
            groups[scene_form].append(vector)
            examples[scene_form].append(window_id)
            sample_ids[scene_form].add(str(annotation.get("sample_id") or ""))
    result = {}
    for scene_form, vectors in groups.items():
        mean = _mean_vectors(vectors)
        if mean:
            result[scene_form] = {
                "vector": mean,
                "sample_count": len(vectors),
                "sample_ids": sorted(value for value in sample_ids[scene_form] if value),
                "examples": examples[scene_form][:6],
            }
    return result


def _prototype_scores(vector: list[float], prototypes: dict[str, dict]) -> dict[str, float]:
    if not vector:
        return {}
    return {
        scene_form: cosine_similarity(vector, prototype.get("vector") or [])
        for scene_form, prototype in prototypes.items()
        if prototype.get("vector")
    }


def _text_score_for_window(start: float, end: float, evidence_rows: list[dict]) -> tuple[float, bool, str]:
    scores = []
    reasons = []
    for row in evidence_rows:
        row_start = float(row.get("start_seconds") or 0.0)
        row_end = float(row.get("end_seconds") or row_start)
        overlap = max(0.0, min(end, row_end) - max(start, row_start))
        if overlap <= 0:
            continue
        score = float(row.get("text_score") or 0.0) * min(1.0, overlap / max(0.1, row_end - row_start))
        scores.append(score)
        if row.get("reason"):
            reasons.append(str(row.get("reason")))
    return (max(scores), True, " / ".join(reasons[:2])) if scores else (0.0, False, "")


def _material_text_evidence_index() -> dict[str, list[dict]]:
    root = ensure_data_dirs().cache_dir / "material_evidence" / "d10b"
    latest: dict[str, tuple[float, Path]] = {}
    if not root.is_dir():
        return {}
    for path in root.glob("*.json"):
        try:
            payload = read_json(path)
        except Exception:
            continue
        sample_id = str(payload.get("sample_id") or "")
        if sample_id and (sample_id not in latest or path.stat().st_mtime > latest[sample_id][0]):
            latest[sample_id] = (path.stat().st_mtime, path)
    result: dict[str, list[dict]] = {}
    for sample_id, (_, path) in latest.items():
        payload = read_json(path)
        pair = payload.get("pair_definition") or {}
        left_cues = [str(value).lower() for value in pair.get("left_cues") or []]
        right_cues = [str(value).lower() for value in pair.get("right_cues") or []]
        rows = []
        for window in payload.get("windows") or []:
            text = " ".join(
                [
                    str((window.get("asr") or {}).get("text") or ""),
                    " ".join(str(value) for value in (window.get("ocr") or {}).get("lines") or []),
                ]
            ).lower()
            hits = [cue for cue in [*left_cues, *right_cues] if cue and cue in text]
            rows.append(
                {
                    "start_seconds": window.get("start_seconds"),
                    "end_seconds": window.get("end_seconds"),
                    "text_score": min(1.0, len(set(hits)) / 2.0),
                    "reason": " / ".join(hits[:4]),
                }
            )
        result[sample_id] = rows
    return result


def _evaluate_strategy_sample(
    sample: dict,
    *,
    strategy: str,
    selected_ids: list[str],
    candidate_index: dict[str, dict],
    annotations: dict[str, dict],
) -> dict:
    selected_annotations = [annotations.get(window_id) for window_id in selected_ids]
    available = bool(selected_ids)
    reviewed = available and all(
        annotation and annotation.get("review_status") == "confirmed"
        for annotation in selected_annotations
    )
    qualities = [str(annotation.get("selection_quality") or "uncertain") for annotation in selected_annotations if annotation]
    hit: bool | None = None
    severe_miss: bool | None = None
    abstained = False
    if reviewed and any(value in {"target", "useful"} for value in qualities):
        hit = True
        severe_miss = False
    elif reviewed and qualities and all(value == "irrelevant" for value in qualities):
        hit = False
        severe_miss = True
    elif reviewed:
        abstained = True
    status = "ready" if hit is not None else (
        "abstained" if abstained else ("needs_review" if available else "not_available")
    )
    return {
        "sample_id": sample.get("sample_id"),
        "account_id": sample.get("account_id"),
        "strategy": strategy,
        "selected_window_ids": selected_ids,
        "available": available,
        "reviewed": reviewed,
        "evaluable": hit is not None,
        "abstained": abstained,
        "hit": hit,
        "severe_miss": severe_miss,
        "status": status,
        "selection_qualities": qualities,
        "selected_windows": [candidate_index.get(window_id) or {"window_id": window_id} for window_id in selected_ids],
    }


def _strategy_metrics(rows: list[dict], *, total_samples: int) -> dict:
    available = [row for row in rows if row.get("available")]
    reviewed = [row for row in available if row.get("reviewed")]
    evaluable = [row for row in reviewed if row.get("evaluable")]
    abstained = [row for row in reviewed if row.get("abstained")]
    recall = (
        round(sum(1 for row in evaluable if row.get("hit") is True) / len(evaluable), 4)
        if evaluable
        else None
    )
    severe_rate = (
        round(sum(1 for row in evaluable if row.get("severe_miss") is True) / len(evaluable), 4)
        if evaluable
        else None
    )
    if not available:
        status = "not_available"
    elif not reviewed:
        status = "needs_review"
    elif not evaluable:
        status = "abstained"
    else:
        status = "ready"
    return {
        "status": status,
        "selected_sample_count": len(available),
        "reviewed_sample_count": len(reviewed),
        "evaluable_sample_count": len(evaluable),
        "evaluated_sample_count": len(evaluable),
        "abstained_sample_count": len(abstained),
        "availability_coverage": round(len(available) / max(1, total_samples), 4),
        "review_coverage": round(len(reviewed) / max(1, total_samples), 4),
        "decision_coverage": round(len(evaluable) / max(1, total_samples), 4),
        "coverage": round(len(evaluable) / max(1, total_samples), 4),
        "unknown_abstention_rate": round(len(abstained) / max(1, len(reviewed)), 4),
        "recall_at_2": recall,
        "severe_miss_rate": severe_rate,
    }


def _paired_strategy_metrics(
    primary_name: str,
    primary_rows: list[dict],
    baseline_name: str,
    baseline_rows: list[dict],
) -> dict:
    primary = {
        str(row.get("sample_id") or ""): row
        for row in primary_rows
        if row.get("evaluable") and str(row.get("sample_id") or "")
    }
    baseline = {
        str(row.get("sample_id") or ""): row
        for row in baseline_rows
        if row.get("evaluable") and str(row.get("sample_id") or "")
    }
    sample_ids = sorted(set(primary) & set(baseline))
    if not sample_ids:
        return {
            "status": "not_comparable",
            "primary_strategy": primary_name,
            "baseline_strategy": baseline_name,
            "paired_sample_count": 0,
            "primary_recall_at_2": None,
            "baseline_recall_at_2": None,
            "recall_delta": None,
            "primary_severe_miss_rate": None,
            "baseline_severe_miss_rate": None,
            "severe_miss_delta": None,
            "sample_ids": [],
        }
    primary_recall = sum(1 for sample_id in sample_ids if primary[sample_id].get("hit") is True) / len(sample_ids)
    baseline_recall = sum(1 for sample_id in sample_ids if baseline[sample_id].get("hit") is True) / len(sample_ids)
    primary_severe = sum(1 for sample_id in sample_ids if primary[sample_id].get("severe_miss") is True) / len(sample_ids)
    baseline_severe = sum(1 for sample_id in sample_ids if baseline[sample_id].get("severe_miss") is True) / len(sample_ids)
    return {
        "status": "ready",
        "primary_strategy": primary_name,
        "baseline_strategy": baseline_name,
        "paired_sample_count": len(sample_ids),
        "primary_recall_at_2": round(primary_recall, 4),
        "baseline_recall_at_2": round(baseline_recall, 4),
        "recall_delta": round(primary_recall - baseline_recall, 4),
        "primary_severe_miss_rate": round(primary_severe, 4),
        "baseline_severe_miss_rate": round(baseline_severe, 4),
        "severe_miss_delta": round(primary_severe - baseline_severe, 4),
        "sample_ids": sample_ids,
    }


def _experiment_gold_summary(report: dict, *, annotations: dict[str, dict]) -> dict:
    samples = list(report.get("samples") or [])
    review_window_ids = {
        str(window.get("window_id") or "")
        for sample in samples
        for window in sample.get("review_windows") or []
        if str(window.get("window_id") or "")
    }
    selected_annotations = [annotations[window_id] for window_id in review_window_ids if window_id in annotations]
    positive = [item for item in selected_annotations if item.get("selection_quality") in {"target", "useful"}]
    progress = _batch_review_progress(report, annotations=annotations)
    return {
        **progress,
        "account_count": len({str(sample.get("account_id") or "") for sample in samples if str(sample.get("account_id") or "")}),
        "review_window_count": len(review_window_ids),
        "confirmed_annotation_count": len(selected_annotations),
        "decisive_annotation_count": sum(1 for item in selected_annotations if item.get("selection_quality") != "uncertain"),
        "uncertain_annotation_count": sum(1 for item in selected_annotations if item.get("selection_quality") == "uncertain"),
        "positive_annotation_count": len(positive),
        "positive_scene_form_count": len({str(item.get("scene_form") or "") for item in positive if str(item.get("scene_form") or "") not in {"", "unknown", "mixed"}}),
        "positive_scene_form_counts": dict(Counter(str(item.get("scene_form") or "unknown") for item in positive)),
    }


def _prototype_leakage_guard(report: dict) -> dict:
    violations = []
    for sample in report.get("samples") or []:
        sample_id = str(sample.get("sample_id") or "")
        prototype_summary = sample.get("prototype_summary") or {}
        source_ids = {str(value) for value in prototype_summary.get("source_sample_ids") or [] if str(value)}
        if sample_id and sample_id in source_ids:
            violations.append(sample_id)
    return {
        "policy": "leave_one_sample_out prototype sources",
        "checked_sample_count": len(report.get("samples") or []),
        "violation_count": len(violations),
        "violation_sample_ids": violations,
        "passed": not violations,
    }


def _build_manifest_verification(build_id: str) -> dict:
    if not build_id:
        return {"passed": False, "status": "legacy_build_without_manifest"}
    try:
        manifest = load_visual_window_build_manifest(build_id)
    except (FileNotFoundError, ValueError):
        return {"passed": False, "status": "manifest_missing", "build_id": build_id}
    verification = manifest.get("verification") or {}
    return {"build_id": build_id, "status": "verified" if verification.get("passed") else "drift_detected", **verification}


def _build_manifests_verification(build_ids: list[str]) -> dict:
    if not build_ids:
        return {
            "passed": False,
            "status": "legacy_build_without_manifest",
            "build_count": 0,
            "builds": [],
        }
    builds = [_build_manifest_verification(value) for value in build_ids]
    passed = all(bool(item.get("passed")) for item in builds)
    return {
        "passed": passed,
        "status": "verified" if passed else "drift_detected",
        "build_count": len(builds),
        "build_ids": list(build_ids),
        "builds": builds,
    }


def _experiment_embedding_summary(report: dict) -> dict:
    samples = list(report.get("samples") or [])
    candidate_count = int(
        report.get("candidate_count")
        or sum(int(sample.get("candidate_count") or len(sample.get("candidates") or [])) for sample in samples)
    )
    ready_count = int(
        report.get("embedding_ready_count")
        or sum(
            int(
                sample.get("embedding_ready_count")
                or sum(
                    1
                    for candidate in sample.get("candidates") or []
                    if candidate.get("embedding_status") in {"created", "reused"}
                )
            )
            for sample in samples
        )
    )
    coverage = ready_count / max(1, candidate_count) if candidate_count else 0.0
    return {
        "passed": candidate_count > 0 and coverage >= 0.90,
        "minimum_coverage": 0.90,
        "candidate_count": candidate_count,
        "ready_count": ready_count,
        "failed_or_missing_count": max(0, candidate_count - ready_count),
        "coverage": round(coverage, 4),
        "vector_policy": f"{QWEN_EMBEDDING_MODEL} {QWEN_EMBEDDING_DIM}-dim model responses only",
    }


def _persist_visual_window_experiment(report: dict) -> dict:
    experiment_id = str(report.get("experiment_id") or "")
    build_id = str(report.get("build_id") or "")
    path = _experiments_root() / f"{_safe_name(experiment_id)}.json"
    if path.exists():
        raise FileExistsError(f"visual window experiment already exists: {experiment_id}")
    write_json(path, report)
    latest_path = _cache_root() / "latest_experiment.json"
    write_json(latest_path, report)
    return {
        "experiment_id": experiment_id,
        "build_id": build_id,
        "report_path": str(path),
        "report_sha256": _file_sha256(path),
    }


def _window_annotation_contract(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in [
            "id", "window_id", "sample_id", "account_id", "dataset_id", "start_seconds", "end_seconds",
            "scene_form", "program_context_mode", "selection_quality", "review_status", "operator", "review_note",
            "created_at", "updated_at",
        ]
    }


def _ready_embedding_record(window_id: str, source_hash: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT * FROM embedding_records
            WHERE entity_type = ? AND entity_id = ? AND modality = ? AND model_name = ?
              AND source_hash = ? AND status = 'ready' AND vector_dim = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            [
                VISUAL_WINDOW_ENTITY_TYPE,
                window_id,
                VISUAL_WINDOW_MODALITY,
                QWEN_EMBEDDING_MODEL,
                source_hash,
                QWEN_EMBEDDING_DIM,
            ],
        )


def _store_embedding_record(
    sample: dict,
    *,
    window_id: str,
    source_hash: str,
    vector_path: Path | str,
    vector_dim: int,
    status: str,
    error: str,
) -> None:
    now = utc_now()
    with connect() as conn:
        insert_row(
            conn,
            "embedding_records",
            {
                "id": new_id("emb"),
                "entity_type": VISUAL_WINDOW_ENTITY_TYPE,
                "entity_id": window_id,
                "account_id": sample.get("account_id") or "",
                "dataset_id": sample.get("dataset_id") or "",
                "platform_item_id": sample.get("platform_item_id") or "",
                "modality": VISUAL_WINDOW_MODALITY,
                "model_name": QWEN_EMBEDDING_MODEL,
                "model_version": VISUAL_WINDOW_SCOUT_VERSION,
                "vector_path": str(vector_path),
                "vector_dim": int(vector_dim or 0),
                "source_hash": source_hash,
                "status": status,
                "error": error,
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.commit()


def _record_embedding_failure(sample: dict, *, window_id: str, source_hash: str, error: str) -> None:
    _store_embedding_record(
        sample,
        window_id=window_id,
        source_hash=source_hash,
        vector_path="",
        vector_dim=0,
        status="failed",
        error=error,
    )


def _load_vector(record: dict) -> list[float]:
    path = Path(str(record.get("vector_path") or ""))
    if not path.is_file():
        return []
    try:
        payload = read_json(path)
        vector = [float(value) for value in payload.get("vector") or []]
        return vector if len(vector) == QWEN_EMBEDDING_DIM else []
    except Exception:
        return []


def _nearest_window_ids(candidates: list[dict], targets: list[float], *, limit: int) -> list[str]:
    result = []
    for target in targets:
        if not candidates:
            continue
        nearest = min(candidates, key=lambda item: abs(float(item.get("start_seconds") or 0.0) - target))
        window_id = str(nearest.get("window_id") or "")
        if window_id and window_id not in result:
            result.append(window_id)
        if len(result) >= limit:
            break
    return result


def _ranked_window_ids(candidates: list[dict], score_key: str, *, limit: int, require=None) -> list[str]:
    rows = [item for item in candidates if not require or require(item)]
    rows.sort(key=lambda item: (-float(item.get(score_key) or 0.0), float(item.get("start_seconds") or 0.0)))
    selected = []
    for item in rows:
        start = float(item.get("start_seconds") or 0.0)
        end = float(item.get("end_seconds") or start)
        if any(max(0.0, min(end, chosen_end) - max(start, chosen_start)) > 0 for chosen_start, chosen_end, _ in selected):
            continue
        selected.append((start, end, str(item.get("window_id") or "")))
        if len(selected) >= limit:
            break
    return [window_id for _, _, window_id in selected if window_id]


def _window_entity_id(sample_id: str, start: float, end: float) -> str:
    digest = hashlib.sha256(f"{sample_id}:{start:.3f}:{end:.3f}".encode("utf-8")).hexdigest()[:20]
    return f"mwin_{digest}"


def _window_key(start: float, end: float) -> str:
    return f"s{int(round(start * 1000)):09d}_e{int(round(end * 1000)):09d}"


def _persist_visual_window_build(report: dict) -> dict:
    build_id = _normalize_build_id(str(report.get("build_id") or ""))
    report_path = _build_report_path(build_id)
    manifest_path = _build_manifest_path(build_id)
    if report_path.exists() or manifest_path.exists():
        raise FileExistsError(f"visual window build already exists: {build_id}")
    write_json(report_path, report)
    report_sha256 = _file_sha256(report_path)
    samples = []
    for sample in report.get("samples") or []:
        samples.append(
            {
                "sample_id": sample.get("sample_id"),
                "account_id": sample.get("account_id"),
                "gold_material_type": sample.get("gold_material_type"),
                "review_window_ids": [
                    str(item.get("window_id") or "")
                    for item in sample.get("review_windows") or []
                    if str(item.get("window_id") or "")
                ],
                "strategy_windows": sample.get("strategy_windows") or {},
                "prototype_summary": sample.get("prototype_summary") or {},
            }
        )
    manifest = {
        "contract_version": VISUAL_WINDOW_BUILD_MANIFEST_VERSION,
        "build_id": build_id,
        "lifecycle": "frozen",
        "created_at": report.get("generated_at") or utc_now(),
        "purpose": "V1 Beta-D-11B immutable visual-window selection batch",
        "source": {
            "git_commit": _git_value("rev-parse", "HEAD"),
            "git_branch": _git_value("branch", "--show-current"),
            "worktree_dirty": bool(_git_value("status", "--porcelain")),
            "visual_window_scout_sha256": _file_sha256(Path(__file__)),
        },
        "query": report.get("query") or {},
        "selection_summary": report.get("selection_summary") or {},
        "prototype_policy": "leave_one_sample_out",
        "sample_count": len(samples),
        "samples": samples,
        "report": {
            "path": str(report_path),
            "sha256": report_sha256,
        },
        "immutability_policy": "Never overwrite this build or manifest; create a new build_id for every scan.",
    }
    manifest["manifest_sha256"] = _canonical_digest(manifest)
    write_json(manifest_path, manifest)
    return {
        "build_id": build_id,
        "lifecycle": "frozen",
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest["manifest_sha256"],
        "report_path": str(report_path),
        "report_sha256": report_sha256,
    }


def load_visual_window_build(build_id: str) -> dict:
    value = _normalize_build_id(build_id)
    report_path = _build_report_path(value)
    if not report_path.is_file():
        raise FileNotFoundError(f"visual window build not found: {value}")
    report = read_json(report_path)
    manifest_path = _build_manifest_path(value)
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        report["manifest_summary"] = {
            "build_id": value,
            "lifecycle": manifest.get("lifecycle"),
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "report_path": str(report_path),
            "report_sha256": (manifest.get("report") or {}).get("sha256"),
        }
    return report


def load_visual_window_build_manifest(build_id: str) -> dict:
    value = _normalize_build_id(build_id)
    path = _build_manifest_path(value)
    if not path.is_file():
        raise FileNotFoundError(f"visual window build manifest not found: {value}")
    manifest = read_json(path)
    expected = str(manifest.get("manifest_sha256") or "")
    actual_payload = dict(manifest)
    actual_payload.pop("manifest_sha256", None)
    actual = _canonical_digest(actual_payload)
    report_path = _build_report_path(value)
    report_sha256 = _file_sha256(report_path) if report_path.is_file() else ""
    expected_report_sha256 = str((manifest.get("report") or {}).get("sha256") or "")
    return {
        **manifest,
        "verification": {
            "passed": bool(expected) and expected == actual and bool(expected_report_sha256) and expected_report_sha256 == report_sha256,
            "manifest_content_matches": bool(expected) and expected == actual,
            "report_content_matches": bool(expected_report_sha256) and expected_report_sha256 == report_sha256,
        },
    }


def _visual_window_build_history(*, limit: int) -> list[dict]:
    root = _build_manifests_root()
    if not root.is_dir():
        return []
    rows = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[: max(1, limit)]:
        try:
            payload = read_json(path)
        except Exception:
            continue
        rows.append(
            {
                "build_id": payload.get("build_id"),
                "lifecycle": payload.get("lifecycle"),
                "created_at": payload.get("created_at"),
                "sample_count": payload.get("sample_count"),
                "selected_material_type_counts": (payload.get("selection_summary") or {}).get("selected_material_type_counts") or {},
                "manifest_sha256": payload.get("manifest_sha256"),
            }
        )
    return rows


def _visual_window_build_ids_through(target_build_id: str) -> list[str]:
    target = _normalize_build_id(target_build_id)
    root = _build_manifests_root()
    ordered: list[tuple[str, str]] = []
    if root.is_dir():
        for path in root.glob("*.json"):
            try:
                payload = read_json(path)
                build_id = _normalize_build_id(str(payload.get("build_id") or ""))
            except (FileNotFoundError, ValueError, TypeError):
                continue
            ordered.append((str(payload.get("created_at") or ""), build_id))
    ordered_ids = [build_id for _, build_id in sorted(set(ordered), key=lambda item: (item[0], item[1]))]
    if target not in ordered_ids:
        return [target]
    return ordered_ids[: ordered_ids.index(target) + 1]


def _normalize_build_id(build_id: str) -> str:
    value = str(build_id or "").strip()
    if not BUILD_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid visual window build_id")
    return value


def _cache_root() -> Path:
    return ensure_data_dirs().cache_dir / "material_visual_window_scout" / "d11"


def _build_reports_root() -> Path:
    return _cache_root() / "builds"


def _build_manifests_root() -> Path:
    return _cache_root() / "manifests"


def _experiments_root() -> Path:
    return _cache_root() / "experiments"


def _build_report_path(build_id: str) -> Path:
    return _build_reports_root() / f"{_normalize_build_id(build_id)}.json"


def _build_manifest_path(build_id: str) -> Path:
    return _build_manifests_root() / f"{_normalize_build_id(build_id)}.json"


def _latest_report_path() -> Path:
    return _cache_root() / "latest.json"


def _latest_report() -> dict:
    path = _latest_report_path()
    return read_json(path) if path.is_file() else {}


def _build_summary(report: dict) -> dict:
    summary = {
        key: report.get(key)
        for key in [
            "build_id", "lifecycle", "status", "sample_count", "candidate_count", "embedding_ready_count", "build_counts",
            "embedding_coverage", "embedding_gate", "selection_summary", "manifest_summary",
            "persistent_frame_bytes_added", "frame_cache_bytes", "service_status", "generated_at",
        ]
        if key in report
    }
    candidate_count = int(report.get("candidate_count") or 0)
    ready_count = int(report.get("embedding_ready_count") or 0)
    summary["embedding_coverage"] = round(ready_count / max(1, candidate_count), 4) if candidate_count else 0.0
    return summary


def _effective_visual_window_status(report: dict, *, batch_progress: dict) -> str:
    sample_count = int(report.get("sample_count") or 0)
    candidate_count = int(report.get("candidate_count") or 0)
    embedding_ready = int(report.get("embedding_ready_count") or 0)
    embedding_coverage = embedding_ready / max(1, candidate_count) if candidate_count else 0.0
    if sample_count and embedding_coverage < 0.90:
        return "needs_embedding_retry"
    if sample_count and int(batch_progress.get("pending_sample_count") or 0) == 0:
        return "ready_for_paired_evaluation"
    return str(report.get("status") or "")


def _batch_review_progress(report: dict, *, annotations: dict[str, dict] | None = None) -> dict:
    annotation_index = annotations if annotations is not None else material_window_annotation_index(confirmed_only=False)
    sample_count = len(report.get("samples") or [])
    reviewed_sample_count = 0
    pending_sample_count = 0
    required_window_count = 0
    confirmed_window_count = 0
    uncertain_window_count = 0
    positive_window_count = 0
    for sample in report.get("samples") or []:
        window_ids = {
            str(item.get("window_id") or "")
            for item in sample.get("review_windows") or []
            if str(item.get("window_id") or "")
        }
        required_window_count += len(window_ids)
        sample_annotations = [annotation_index.get(window_id) for window_id in window_ids]
        confirmed = [item for item in sample_annotations if item and item.get("review_status") == "confirmed"]
        confirmed_window_count += len(confirmed)
        uncertain_window_count += sum(1 for item in confirmed if item.get("selection_quality") == "uncertain")
        positive_window_count += sum(1 for item in confirmed if item.get("selection_quality") in {"target", "useful"})
        if window_ids and len(confirmed) == len(window_ids):
            reviewed_sample_count += 1
        elif window_ids:
            pending_sample_count += 1
    return {
        "build_id": report.get("build_id"),
        "sample_count": sample_count,
        "reviewed_sample_count": reviewed_sample_count,
        "pending_sample_count": pending_sample_count,
        "required_window_count": required_window_count,
        "confirmed_window_count": confirmed_window_count,
        "uncertain_window_count": uncertain_window_count,
        "positive_window_count": positive_window_count,
        "completion_rate": round(reviewed_sample_count / max(1, sample_count), 4),
    }


def _canonical_digest(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], text=True, capture_output=True, check=False, timeout=3)
    except Exception:
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _safe_probe(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        return probe_video(path)
    except Exception as exc:
        return {"error": str(exc)}


def _first_existing(values: list[object]) -> Path | None:
    for value in values:
        path = Path(str(value or "")).expanduser()
        if path.is_file():
            return path.resolve()
    return None


def _directory_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) if root.is_dir() else 0


def _mean_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dimension = len(vectors[0])
    valid = [vector for vector in vectors if len(vector) == dimension]
    if not valid:
        return []
    return [statistics.fmean(vector[index] for vector in valid) for index in range(dimension)]


def _even_sample(values: list[float], limit: int) -> list[float]:
    if limit <= 0 or not values:
        return []
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[len(values) // 2]]
    indexes = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return [values[index] for index in indexes]


def _service_ready(payload: dict) -> bool:
    return embedding_service_ready(payload)


def _safe_name(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))[:120] or "unknown"


def _option_contract(values: tuple[str, ...]) -> list[dict]:
    labels = {
        "stage_performance": "舞台演唱",
        "rehearsal": "彩排/排练",
        "backstage_interview": "后台/访谈",
        "vocal_teaching": "声乐教学",
        "compilation": "合集盘点",
        "news_document": "资讯/文档",
        "unknown": "未知",
        "mixed": "混合形态",
        "present": "存在节目语境",
        "absent": "无节目语境",
        "target": "目标窗口",
        "useful": "可用窗口",
        "irrelevant": "无关窗口",
        "uncertain": "暂不确定",
    }
    return [{"value": value, "label_zh": labels.get(value, value)} for value in values]
