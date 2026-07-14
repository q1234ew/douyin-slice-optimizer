from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.qwen_omni import (
    OMNI_DOMAIN_CATEGORIES,
    OMNI_MATERIAL_TYPES,
    OMNI_PRESENTATION_STYLES,
    omni_annotation_field_guides,
    qwen_omni_shadow_cache_index,
)
from dso.review import insert_change_event
from dso.utils import new_id, utc_now
from dso.versions import DOUYIN_HISTORY_VERSION


MATERIAL_GOLD_FIELDS = ("domain_category", "material_type", "program_context", "presentation_style")
MATERIAL_GOLD_REVIEW_STATUSES = {"confirmed", "reopened"}
MATERIAL_GOLD_REVIEW_TARGET = 60


def material_gold_annotation_index(*, confirmed_only: bool = True) -> dict[str, dict]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    if confirmed_only:
        clauses.append("review_status = 'confirmed'")
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"SELECT * FROM material_gold_annotations WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC",
            params,
        )
    return {str(row.get("sample_id") or ""): _annotation_contract(row) for row in rows if row.get("sample_id")}


def material_gold_set_queue(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 12,
    include_reviewed: bool = False,
) -> dict:
    cap = max(1, min(100, int(limit or 12)))
    report = _latest_material_backtest(account_id)
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    source_queue = metrics.get("omni_material_gold_set_queue") if isinstance(metrics, dict) else []
    source_queue = source_queue if isinstance(source_queue, list) else []
    annotations = material_gold_annotation_index(confirmed_only=False)
    confirmed_annotation_ids = [
        sample_id
        for sample_id, annotation in annotations.items()
        if annotation.get("review_status") == "confirmed"
    ]
    sample_ids = [str(item.get("sample_id") or "") for item in source_queue if isinstance(item, dict)] + confirmed_annotation_ids
    sample_rows = _historical_sample_index(sample_ids)
    confirmed_group_keys = {
        _material_queue_group_key(
            {
                "sample_id": sample_id,
                "account_id": annotation.get("account_id") or (sample_rows.get(sample_id) or {}).get("account_id") or "",
                "title": (sample_rows.get(sample_id) or {}).get("title") or "",
            }
        )
        for sample_id, annotation in annotations.items()
        if annotation.get("review_status") == "confirmed"
    }
    items: list[dict] = []
    for raw in source_queue:
        if not isinstance(raw, dict):
            continue
        sample_id = str(raw.get("sample_id") or "").strip()
        history = sample_rows.get(sample_id) or {}
        if dataset_id and str(history.get("dataset_id") or raw.get("dataset_id") or "") != str(dataset_id):
            continue
        annotation = annotations.get(sample_id)
        if annotation and annotation.get("review_status") == "confirmed" and not include_reviewed:
            continue
        item = dict(raw)
        item.update(
            {
                "dataset_id": history.get("dataset_id") or raw.get("dataset_id") or "default",
                "platform_url": history.get("platform_url") or raw.get("platform_url") or "",
                "published_at": history.get("published_at") or raw.get("published_at") or "",
                "tags": history.get("tags") or raw.get("tags") or "",
                "artist_names": history.get("artist_names") or raw.get("artist_names") or "",
                "song_title": history.get("song_title") or raw.get("song_title") or "",
                "annotation": annotation,
                "material_label_verified": bool(annotation and annotation.get("review_status") == "confirmed"),
            }
        )
        if not include_reviewed and _material_queue_group_key(item) in confirmed_group_keys:
            continue
        items.append(item)
    source_item_count = len(items)
    source_collapsed_variant_count = sum(max(0, int(item.get("collapsed_variant_count") or 0)) for item in items)
    items = _dedupe_material_queue_items(items)
    collapsed_duplicate_count = max(source_item_count - len(items), source_collapsed_variant_count)
    unique_source_candidate_count = len(items)
    confirmed = _material_gold_annotations_for_scope(account_id=account_id, dataset_id=dataset_id, status="confirmed")
    reopened = _material_gold_annotations_for_scope(account_id=account_id, dataset_id=dataset_id, status="reopened")
    if not include_reviewed:
        remaining_target = max(0, MATERIAL_GOLD_REVIEW_TARGET - len(confirmed))
        items = items[:remaining_target]
    reason_counts = Counter(str(item.get("queue_reason") or "material_shadow_gold_set") for item in items)
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready" if items else ("needs_backtest" if not source_queue else "complete"),
        "mode": "material_gold_set_review",
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "count": min(cap, len(items)),
        "total_candidates": len(items),
        "annotation_field_guides": omni_annotation_field_guides(list(MATERIAL_GOLD_FIELDS) + ["material_label_verified"]),
        "batch_summary": {
            "pending_count": len(items),
            "confirmed_count": len(confirmed),
            "reopened_count": len(reopened),
            "source_candidate_count": len(source_queue),
            "unique_source_candidate_count": unique_source_candidate_count,
            "collapsed_duplicate_count": collapsed_duplicate_count,
            "queue_reason_counts": dict(reason_counts),
            "review_target": MATERIAL_GOLD_REVIEW_TARGET,
            "progress_rate": round(min(1.0, len(confirmed) / MATERIAL_GOLD_REVIEW_TARGET), 4),
        },
        "samples": items[:cap],
        "recently_confirmed_samples": confirmed[:8],
        "source_backtest": {
            "generated_at": report.get("created_at") or report.get("generated_at") or "",
            "strategy": ((report.get("query") or {}).get("strategy") if isinstance(report.get("query"), dict) else "") or metrics.get("strategy") or "",
        },
        "writes_main_semantic_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def update_material_gold_annotation(sample_id: str, payload: dict[str, Any]) -> dict:
    sample_key = str(sample_id or "").strip()
    if not sample_key:
        raise ValueError("sample_id is required")
    normalized = _validated_material_payload(payload)
    operator = str(payload.get("operator") or "local").strip() or "local"
    note = str(payload.get("review_note") or payload.get("reason") or "manual material gold set confirmation").strip()
    now = utc_now()
    cache = qwen_omni_shadow_cache_index()
    with connect() as conn:
        sample = fetch_one(conn, "SELECT * FROM historical_capture_samples WHERE id = ?", [sample_key])
        if not sample:
            raise KeyError(f"historical sample not found: {sample_key}")
        existing = fetch_one(conn, "SELECT * FROM material_gold_annotations WHERE sample_id = ?", [sample_key])
        before = _annotation_contract(existing or {}) if existing else {}
        omni = cache.get(sample_key) or cache.get(str(sample.get("platform_item_id") or "")) or {}
        snapshot = {
            "semantic_suggestions": omni.get("semantic_suggestions") if isinstance(omni, dict) else {},
            "semantic_quality": omni.get("semantic_quality") if isinstance(omni, dict) else {},
        }
        row = {
            "id": existing.get("id") if existing else new_id("matgold"),
            "sample_id": sample_key,
            "account_id": sample.get("account_id") or "",
            "dataset_id": sample.get("dataset_id") or "",
            **normalized,
            "review_status": "confirmed",
            "operator": operator,
            "review_note": note,
            "model_snapshot_json": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            "created_at": existing.get("created_at") if existing else now,
            "updated_at": now,
        }
        if existing:
            assignments = ", ".join(f"{field} = ?" for field in row if field not in {"id", "sample_id", "created_at"})
            values = [row[field] for field in row if field not in {"id", "sample_id", "created_at"}]
            conn.execute(f"UPDATE material_gold_annotations SET {assignments} WHERE sample_id = ?", [*values, sample_key])
        else:
            insert_row(conn, "material_gold_annotations", row)
        after = _annotation_contract(row)
        insert_change_event(
            conn,
            entity_type="material_gold_annotation",
            entity_id=sample_key,
            change_type="material_gold_confirmed",
            before=before,
            after=after,
            reason=note,
            operator=operator,
        )
        conn.commit()
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "confirmed",
        "sample_id": sample_key,
        "annotation": after,
        "writes_main_semantic_labels": False,
        "production_weight": False,
    }


def reopen_material_gold_annotation(sample_id: str, payload: dict[str, Any] | None = None) -> dict:
    payload = payload or {}
    sample_key = str(sample_id or "").strip()
    operator = str(payload.get("operator") or "local").strip() or "local"
    reason = str(payload.get("reason") or "reopen material gold annotation").strip()
    with connect() as conn:
        existing = fetch_one(conn, "SELECT * FROM material_gold_annotations WHERE sample_id = ?", [sample_key])
        if not existing:
            raise KeyError(f"material gold annotation not found: {sample_key}")
        before = _annotation_contract(existing)
        conn.execute(
            "UPDATE material_gold_annotations SET review_status = 'reopened', operator = ?, review_note = ?, updated_at = ? WHERE sample_id = ?",
            [operator, reason, utc_now(), sample_key],
        )
        updated = fetch_one(conn, "SELECT * FROM material_gold_annotations WHERE sample_id = ?", [sample_key]) or {}
        after = _annotation_contract(updated)
        insert_change_event(
            conn,
            entity_type="material_gold_annotation",
            entity_id=sample_key,
            change_type="material_gold_reopened",
            before=before,
            after=after,
            reason=reason,
            operator=operator,
        )
        conn.commit()
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "reopened",
        "sample_id": sample_key,
        "annotation": after,
        "writes_main_semantic_labels": False,
    }


def run_material_calibration_replay(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    k: int = 30,
    holdout_policy: str = "time",
) -> dict:
    from dso.learning.backtest import RESEARCH_RANKER_V29_TAXONOMY_STRATEGY, backtest_rule_ranker

    report = backtest_rule_ranker(
        account_id=account_id,
        k=max(1, int(k or 30)),
        strategy=RESEARCH_RANKER_V29_TAXONOMY_STRATEGY,
        holdout_policy=holdout_policy,
    )
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": report.get("status") or "research_ready",
        "mode": "material_gold_calibration_replay",
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "report_id": report.get("id") or "",
        "metrics": {
            "strategy": metrics.get("strategy"),
            "strategy_comparison": metrics.get("strategy_comparison") or {},
            "promotion_gate": metrics.get("promotion_gate") or {},
            "omni_material_calibration": metrics.get("omni_material_calibration") or {},
            "omni_material_calibration_holdout": metrics.get("omni_material_calibration_holdout") or {},
            "omni_material_gold_split": metrics.get("omni_material_gold_split") or {},
            "omni_material_router_profiles": metrics.get("omni_material_router_profiles") or [],
            "omni_material_taxonomy_router_profiles": metrics.get("omni_material_taxonomy_router_profiles") or [],
            "omni_material_v28_report": metrics.get("omni_material_v28_report") or {},
            "omni_material_v28_gate": metrics.get("omni_material_v28_gate") or {},
            "omni_material_v29_report": metrics.get("omni_material_v29_report") or {},
            "omni_material_v29_gate": metrics.get("omni_material_v29_gate") or {},
        },
        "queue": material_gold_set_queue(account_id=account_id, dataset_id=dataset_id, limit=12),
        "writes_main_semantic_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _validated_material_payload(payload: dict[str, Any]) -> dict[str, str]:
    domain = str(payload.get("domain_category") or "unknown").strip().lower()
    material = str(payload.get("material_type") or "unknown").strip().lower()
    presentation = str(payload.get("presentation_style") or "unknown").strip().lower()
    program = str(payload.get("program_context") or "unknown").strip() or "unknown"
    if domain not in OMNI_DOMAIN_CATEGORIES:
        raise ValueError(f"domain_category must be one of: {', '.join(OMNI_DOMAIN_CATEGORIES)}")
    if material not in OMNI_MATERIAL_TYPES:
        raise ValueError(f"material_type must be one of: {', '.join(OMNI_MATERIAL_TYPES)}")
    if presentation not in OMNI_PRESENTATION_STYLES:
        raise ValueError(f"presentation_style must be one of: {', '.join(OMNI_PRESENTATION_STYLES)}")
    return {
        "domain_category": domain,
        "material_type": material,
        "program_context": program,
        "presentation_style": presentation,
    }


def _annotation_contract(row: dict) -> dict:
    if not row:
        return {}
    try:
        snapshot = json.loads(row.get("model_snapshot_json") or "{}")
    except Exception:
        snapshot = {}
    return {
        "id": row.get("id") or "",
        "sample_id": row.get("sample_id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "",
        "domain_category": row.get("domain_category") or "unknown",
        "material_type": row.get("material_type") or "unknown",
        "program_context": row.get("program_context") or "unknown",
        "presentation_style": row.get("presentation_style") or "unknown",
        "review_status": row.get("review_status") or "reopened",
        "operator": row.get("operator") or "local",
        "review_note": row.get("review_note") or "",
        "model_snapshot": snapshot,
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _latest_material_backtest(account_id: str | None) -> dict:
    account = str(account_id or "all").strip() or "all"
    with connect() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM backtest_reports WHERE account_id = ? ORDER BY created_at DESC LIMIT 20",
            [account],
        )
    for row in rows:
        try:
            payload = json.loads(row.get("metrics_json") or "{}")
        except Exception:
            continue
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        queue = metrics.get("omni_material_gold_set_queue") if isinstance(metrics, dict) else None
        if isinstance(queue, list) and queue:
            return {
                **row,
                "metrics": metrics,
                "top_rows": payload.get("top_rows") or [],
                "query": {"strategy": metrics.get("strategy") or ""},
            }
    return {}


def _historical_sample_index(sample_ids: list[str]) -> dict[str, dict]:
    ids = [item for item in dict.fromkeys(sample_ids) if item]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"SELECT id, account_id, dataset_id, title, platform_url, published_at, tags, artist_names, song_title FROM historical_capture_samples WHERE id IN ({placeholders})",
            ids,
        )
    return {str(row.get("id") or ""): row for row in rows}


def _dedupe_material_queue_items(items: list[dict]) -> list[dict]:
    group_sizes = Counter(_material_queue_group_key(item) for item in items)
    selected: list[dict] = []
    seen: set[str] = set()
    for raw in items:
        group_key = _material_queue_group_key(raw)
        if group_key in seen:
            continue
        seen.add(group_key)
        item = dict(raw)
        group_size = int(group_sizes.get(group_key) or 1)
        item["duplicate_group_size"] = max(group_size, int(item.get("duplicate_group_size") or 1))
        item["collapsed_variant_count"] = max(0, int(item["duplicate_group_size"]) - 1)
        selected.append(item)
    return selected


def _material_queue_group_key(item: dict) -> str:
    account = str(item.get("account_id") or "").strip().lower()
    title = str(item.get("title") or "").strip().lower()
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[@#《》【】\[\]（）()，,。.!！?？:：;；\"'“”‘’、\s]+", "", title)
    title = re.sub(r"\d+", "#", title)[:80]
    if title:
        return f"{account}:title:{title}"
    sample_id = str(item.get("sample_id") or item.get("platform_item_id") or "").strip()
    return f"{account}:sample:{sample_id}"


def _material_gold_annotations_for_scope(
    *,
    account_id: str | None,
    dataset_id: str | None,
    status: str,
) -> list[dict]:
    clauses = ["review_status = ?"]
    params: list[Any] = [status]
    account = str(account_id or "").strip()
    dataset = str(dataset_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if dataset and dataset.lower() != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"SELECT * FROM material_gold_annotations WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC",
            params,
        )
    return [_annotation_contract(row) for row in rows]
