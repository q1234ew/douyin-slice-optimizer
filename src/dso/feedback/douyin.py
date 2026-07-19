from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from dso.db.session import connect, fetch_all, fetch_one
from dso.feedback.account_context import platform_account_context
from dso.feedback.importer import import_metric_rows
from dso.feedback.platform import (
    create_platform_mapping,
    list_platform_mappings,
    list_platform_sync_runs,
    map_platform_metric_row,
    platform_metric_contract,
    record_platform_sync_run,
    update_platform_sync_state,
    upsert_platform_account,
)
from dso.spreadsheets import XLSX_SUFFIXES, read_table_rows
from dso.utils import utc_now
from dso.versions import PLATFORM_SYNC_VERSION


DEFAULT_WINDOWS = ["6h", "24h", "72h", "7d"]
WINDOW_HOURS = {"6h": 6, "24h": 24, "72h": 72, "7d": 168, "30d": 720, "final": 0}


def douyin_sync_contract() -> dict:
    return {
        "contract_version": PLATFORM_SYNC_VERSION,
        "platform": "douyin",
        "sources": ["mock", "json", "csv", "xlsx", "api"],
        "default_windows": DEFAULT_WINDOWS,
        "metric_contract": platform_metric_contract(),
        "account_roles": ["unassigned", "publishing_target", "research_source"],
        "evidence_scopes": ["unclassified", "target_outcome", "research_proxy"],
        "policy": {
            "read_only": True,
            "no_auto_publish": True,
            "no_real_network_by_default": True,
            "token_storage": "real access/refresh tokens are not persisted by this local connector",
            "publishing_target": "must be explicitly designated; research accounts never become publishing targets implicitly",
            "target_outcome": "only explicit platform metrics on target_outcome mappings count toward account personalization readiness",
        },
    }


def register_douyin_account(account_id: str = "main", payload: dict[str, Any] | None = None) -> dict:
    data = dict(payload or {})
    data.setdefault("account_id", account_id)
    data.setdefault("platform", "douyin")
    data.setdefault("auth_status", "mock_ready")
    data.setdefault("token_status", "not_stored")
    data.setdefault("scopes", ["data.read", "video.list.read"])
    return upsert_platform_account(data)


def sync_douyin_feedback(
    account_id: str = "main",
    *,
    source: str = "mock",
    payload: dict[str, Any] | list[dict[str, Any]] | None = None,
    source_path: str | Path | None = None,
    windows: list[str] | None = None,
    sync_mode: str = "manual",
) -> dict:
    started_at = utc_now()
    source_name = (source or "mock").strip().lower()
    selected_windows = _normalize_windows(windows)
    account = register_douyin_account(
        account_id,
        {
            "auth_status": "mock_ready" if source_name == "mock" else "read_only_ready",
            "notes": "local read-only sync placeholder",
        },
    )
    try:
        sample_source = _sample_source_for_sync(source_name)
        raw_rows = _load_source_rows(account_id, source_name, payload=payload, source_path=source_path, windows=selected_windows)
        normalized_rows = _prepare_rows(account_id, raw_rows, sample_source=sample_source)
        mapping_summary = _upsert_mappings_from_rows(account_id, normalized_rows)
        import_result = import_metric_rows(
            normalized_rows,
            sample_source=sample_source,
            source_label=_source_label(source_name, source_path),
        )
        finished_at = utc_now()
        row_summary = import_result.get("row_summary") or {}
        status = "completed"
        if not normalized_rows:
            status = "empty"
        elif row_summary.get("unlinked_rows"):
            status = "completed_with_warnings"
        update_platform_sync_state(account_id, "douyin", synced_at=finished_at)
        sync_run = record_platform_sync_run(
            {
                "account_id": account_id,
                "platform": "douyin",
                "source": source_name,
                "sync_mode": sync_mode,
                "status": status,
                "requested_windows": selected_windows,
                "started_at": started_at,
                "finished_at": finished_at,
                "pulled_items": mapping_summary["platform_items"],
                "mapped_items": mapping_summary["mapped_items"],
                "imported_metrics": import_result.get("imported", 0),
                "linked_rows": row_summary.get("linked_rows", 0),
                "unlinked_rows": row_summary.get("unlinked_rows", 0),
                "training_samples": import_result.get("training_samples", 0),
                "summary": {
                    "mapping_summary": mapping_summary,
                    "row_summary": row_summary,
                    "source_label": _source_label(source_name, source_path),
                },
            }
        )
        return {
            "contract_version": PLATFORM_SYNC_VERSION,
            "status": status,
            "account": account,
            "source": source_name,
            "windows": selected_windows,
            "pulled_rows": len(normalized_rows),
            "mapping_summary": mapping_summary,
            "import_result": import_result,
            "sync_run": sync_run,
            "recent_runs": list_platform_sync_runs(account_id=account_id, platform="douyin", limit=5),
            "contract": douyin_sync_contract(),
        }
    except Exception as exc:
        finished_at = utc_now()
        sync_run = record_platform_sync_run(
            {
                "account_id": account_id,
                "platform": "douyin",
                "source": source_name,
                "sync_mode": sync_mode,
                "status": "failed",
                "requested_windows": selected_windows,
                "started_at": started_at,
                "finished_at": finished_at,
                "error": str(exc),
            }
        )
        return {
            "contract_version": PLATFORM_SYNC_VERSION,
            "status": "failed",
            "account": account,
            "source": source_name,
            "windows": selected_windows,
            "error": str(exc),
            "sync_run": sync_run,
            "contract": douyin_sync_contract(),
        }


def _load_source_rows(
    account_id: str,
    source: str,
    *,
    payload: dict[str, Any] | list[dict[str, Any]] | None,
    source_path: str | Path | None,
    windows: list[str],
) -> list[dict[str, Any]]:
    if source == "mock" and not source_path and (not payload or not _payload_has_metric_rows(payload)):
        return _mock_rows_for_account(account_id, windows)
    if source_path:
        return _load_rows_from_path(source_path, windows)
    if payload is None:
        if source in {"api", "json", "csv", "xlsx"}:
            raise ValueError("payload or source_path is required for non-mock douyin sync")
        return []
    return _rows_from_payload(payload, windows)


def _payload_has_metric_rows(payload: dict[str, Any] | list[dict[str, Any]]) -> bool:
    if isinstance(payload, list):
        return bool(payload)
    for key in ["rows", "metrics", "records", "items", "videos", "data"]:
        if isinstance(payload.get(key), list) and payload.get(key):
            return True
    item_id = payload.get("platform_item_id") or payload.get("item_id") or payload.get("video_id") or payload.get("aweme_id")
    metric_keys = {"views", "play_count", "impressions", "show_count", "avg_watch_seconds", "avg_play_duration", "likes", "like_count"}
    return bool(item_id and any(payload.get(key) not in (None, "") for key in metric_keys))


def _load_rows_from_path(source_path: str | Path, windows: list[str]) -> list[dict[str, Any]]:
    path = Path(source_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    if path.suffix.lower() in XLSX_SUFFIXES:
        return read_table_rows(path, preferred_sheets=("作品去重", "原始清洗记录", "metrics", "Metrics"))
    with path.open("r", encoding="utf-8") as handle:
        return _rows_from_payload(json.load(handle), windows)


def _rows_from_payload(payload: dict[str, Any] | list[dict[str, Any]], windows: list[str]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload]
    for key in ["rows", "metrics", "records"]:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value]
    for key in ["items", "videos", "data"]:
        value = payload.get(key)
        if isinstance(value, list):
            rows: list[dict[str, Any]] = []
            for item in value:
                if isinstance(item, dict):
                    rows.extend(_rows_from_item(item, windows))
            return rows
    return [dict(payload)]


def _rows_from_item(item: dict[str, Any], windows: list[str]) -> list[dict[str, Any]]:
    base = {
        "platform": item.get("platform") or "douyin",
        "platform_item_id": item.get("platform_item_id") or item.get("item_id") or item.get("video_id") or item.get("aweme_id"),
        "platform_title": item.get("platform_title") or item.get("title") or item.get("desc"),
        "platform_url": item.get("platform_url") or item.get("share_url") or item.get("url"),
        "published_at": item.get("published_at") or item.get("publish_time") or item.get("create_time"),
        "candidate_segment_id": item.get("candidate_segment_id"),
        "slice_variant_id": item.get("slice_variant_id"),
        "experiment_id": item.get("experiment_id"),
    }
    by_window = item.get("metrics_by_window") or item.get("statistics_by_window")
    if isinstance(by_window, dict):
        rows = []
        for window_name, metrics in by_window.items():
            if isinstance(metrics, dict):
                rows.append({**base, **metrics, "window_name": window_name, "hours_since_publish": WINDOW_HOURS.get(str(window_name), 0)})
        return rows
    if isinstance(by_window, list):
        return [{**base, **dict(metrics)} for metrics in by_window if isinstance(metrics, dict)]
    metrics = item.get("metrics") or item.get("statistics")
    if isinstance(metrics, dict):
        return [{**base, **metrics, "window_name": item.get("window_name") or "final"}]
    return [{**base, **item}]


def _prepare_rows(account_id: str, rows: list[dict[str, Any]], *, sample_source: str) -> list[dict[str, Any]]:
    prepared = []
    for raw in rows:
        mapped = map_platform_metric_row(raw, sample_source=str(raw.get("sample_source") or sample_source))
        mapped["account_id"] = raw.get("account_id") or account_id
        if not mapped.get("window_name") and not mapped.get("label_window"):
            mapped["window_name"] = _window_from_hours(mapped.get("hours_since_publish"))
        prepared.append(mapped)
    return prepared


def _upsert_mappings_from_rows(account_id: str, rows: list[dict[str, Any]]) -> dict:
    item_ids = set()
    mapped_items = set()
    for row in rows:
        item_id = str(row.get("platform_item_id") or "").strip()
        if not item_id:
            continue
        item_ids.add(item_id)
        payload = {
            "account_id": row.get("account_id") or account_id,
            "platform": row.get("platform") or "douyin",
            "platform_item_id": item_id,
            "candidate_segment_id": row.get("candidate_segment_id") or "",
            "slice_variant_id": row.get("slice_variant_id") or "",
            "experiment_id": row.get("experiment_id") or "",
            "platform_url": row.get("platform_url") or "",
            "platform_title": row.get("platform_title") or "",
            "published_at": row.get("published_at") or "",
            "sync_status": "metrics_synced",
            "last_synced_at": utc_now(),
            "last_metrics_at": row.get("collected_at") or utc_now(),
        }
        if row.get("evidence_scope"):
            payload["evidence_scope"] = row.get("evidence_scope")
        mapping = create_platform_mapping(payload)
        if mapping.get("candidate_segment_id") or mapping.get("slice_variant_id") or mapping.get("experiment_id"):
            mapped_items.add(item_id)
    return {
        "platform_items": len(item_ids),
        "mapped_items": len(mapped_items),
        "unmapped_items": max(0, len(item_ids) - len(mapped_items)),
    }


def _mock_rows_for_account(account_id: str, windows: list[str]) -> list[dict[str, Any]]:
    mappings = list_platform_mappings(account_id=account_id, platform="douyin")
    if not mappings:
        return []
    rows = []
    with connect() as conn:
        for mapping in mappings:
            candidate = fetch_one(
                conn,
                "SELECT duration_seconds, music_slice_type FROM candidate_segments WHERE id = ?",
                [mapping.get("candidate_segment_id")],
            )
            duration = float((candidate or {}).get("duration_seconds") or 30)
            seed = _stable_seed(mapping["platform_item_id"])
            for index, window in enumerate(windows):
                hours = WINDOW_HOURS.get(window, 0)
                views = (seed % 700 + 300) * (index + 1)
                impressions = int(views * (1.7 + (seed % 4) / 10))
                avg_watch_seconds = round(min(duration * 0.92, 8 + (seed % 12) + index * 1.5), 2)
                rows.append(
                    {
                        "platform": "douyin",
                        "platform_item_id": mapping["platform_item_id"],
                        "candidate_segment_id": mapping.get("candidate_segment_id") or "",
                        "slice_variant_id": mapping.get("slice_variant_id") or "",
                        "experiment_id": mapping.get("experiment_id") or "",
                        "platform_title": mapping.get("platform_title") or "",
                        "platform_url": mapping.get("platform_url") or "",
                        "published_at": mapping.get("published_at") or "",
                        "window_name": window,
                        "hours_since_publish": hours,
                        "collected_at": utc_now(),
                        "views": views,
                        "impressions": impressions,
                        "avg_watch_seconds": avg_watch_seconds,
                        "completion_rate": round(min(0.92, 0.38 + (seed % 28) / 100 + index * 0.03), 4),
                        "five_second_retention": round(min(0.96, 0.55 + (seed % 25) / 100 + index * 0.02), 4),
                        "rewatch_rate": round(0.03 + (seed % 8) / 100, 4),
                        "likes": int(views * (0.04 + (seed % 7) / 100)),
                        "comments": int(views * (0.006 + (seed % 4) / 1000)),
                        "favorites": int(views * (0.012 + (seed % 5) / 1000)),
                        "shares": int(views * (0.008 + (seed % 6) / 1000)),
                        "follows": int(views * (0.002 + (seed % 4) / 1000)),
                        "negative_feedback": int(views * (0.001 + (seed % 3) / 1000)),
                    }
                )
    return rows


def douyin_sync_summary(account_id: str = "main") -> dict:
    with connect() as conn:
        metrics = fetch_one(
            conn,
            """
            SELECT COUNT(pm.id) AS count,
                   MAX(pm.collected_at) AS latest_collected_at,
                   SUM(CASE WHEN pm.candidate_segment_id IS NULL OR pm.candidate_segment_id = '' THEN 1 ELSE 0 END) AS unlinked,
                   SUM(CASE WHEN pm.metric_semantics = 'explicit_platform_outcome' THEN 1 ELSE 0 END) AS explicit_outcome_rows,
                   SUM(CASE WHEN pm.metric_semantics = 'engagement_proxy' THEN 1 ELSE 0 END) AS engagement_proxy_rows,
                   SUM(CASE WHEN pm.metric_semantics = 'legacy_unverified' THEN 1 ELSE 0 END) AS legacy_unverified_rows,
                   SUM(CASE WHEN pm.metric_semantics = 'ambiguous_visible_count' THEN 1 ELSE 0 END) AS ambiguous_visible_count_rows,
                   SUM(CASE WHEN pm.id IS NOT NULL AND m.evidence_scope = 'target_outcome' THEN 1 ELSE 0 END) AS target_outcome_rows,
                   SUM(CASE WHEN pm.id IS NOT NULL AND m.evidence_scope = 'research_proxy' THEN 1 ELSE 0 END) AS research_proxy_rows,
                   SUM(CASE WHEN pm.id IS NOT NULL AND (m.evidence_scope = 'unclassified' OR m.evidence_scope = '') THEN 1 ELSE 0 END) AS unclassified_rows
            FROM platform_video_mappings m
            LEFT JOIN performance_metrics pm ON pm.platform_item_id = m.platform_item_id
            WHERE m.platform = 'douyin' AND m.account_id = ?
            """,
            [account_id],
        )
        mappings = fetch_all(
            conn,
            """
            SELECT * FROM platform_video_mappings
            WHERE platform = 'douyin' AND account_id = ?
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            [account_id],
        )
    runs = list_platform_sync_runs(account_id=account_id, platform="douyin", limit=5)
    account_context = platform_account_context(account_id, "douyin")
    return {
        "contract_version": PLATFORM_SYNC_VERSION,
        "account_id": account_id,
        "metrics": metrics or {"count": 0, "latest_collected_at": "", "unlinked": 0},
        "mappings": mappings,
        "runs": runs,
        "account_context": account_context,
        "contract": douyin_sync_contract(),
    }


def _normalize_windows(windows: list[str] | None) -> list[str]:
    values = [str(item).strip() for item in (windows or DEFAULT_WINDOWS) if str(item).strip()]
    return values or list(DEFAULT_WINDOWS)


def _window_from_hours(value: Any) -> str:
    try:
        hours = float(value or 0)
    except (TypeError, ValueError):
        hours = 0
    if hours <= 0:
        return "final"
    if hours <= 6:
        return "6h"
    if hours <= 24:
        return "24h"
    if hours <= 72:
        return "72h"
    if hours <= 168:
        return "7d"
    return "30d"


def _stable_seed(value: str) -> int:
    return sum((index + 1) * ord(ch) for index, ch in enumerate(value))


def _sample_source_for_sync(source: str) -> str:
    if source == "mock":
        return "mock"
    if source in {"csv", "xlsx", "xslx"}:
        return "csv"
    return "api"


def _source_label(source: str, source_path: str | Path | None) -> str:
    if source_path:
        return str(Path(source_path).expanduser().resolve())
    return f"douyin:{source}"
