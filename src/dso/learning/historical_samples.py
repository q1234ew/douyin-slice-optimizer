from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any

from dso.accounts import account_metadata, dataset_display_name
from dso.collectors.douyin_classification import classify_published_work
from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.prototypes import (
    _capture_paths,
    _capture_samples,
    _clean_item_id,
    _json,
    _normalize_dataset_id,
    _read_rows,
    _resolve_dataset,
    _stable_key,
    _text,
    list_capture_datasets,
)
from dso.learning.qwen_omni import omni_annotation_field_guides, qwen_omni_shadow_cache_index
from dso.learning.semantic_labels import normalize_semantic_labels, semantic_label_catalog
from dso.review import insert_change_event
from dso.utils import new_id, read_json, utc_now, write_json
from dso.versions import (
    DOUYIN_HISTORY_VERSION,
    HISTORICAL_CAPTURE_VERSION,
    RESEARCH_LABEL_VERSION,
    SEMANTIC_FEATURE_VERSION,
)


DOUYIN_CLEAN_WORKS_FILENAME = "douyin_visible_works_dedup_latest.json"
DOUYIN_QUALITY_FILENAME = "douyin_collection_quality_latest.json"
MANUAL_LABEL_FIELDS = {
    "content_category",
    "hook_type",
    "slice_structure",
    "artist_names",
    "song_title",
    "tags",
}
RESEARCH_LABEL_MIN_BASELINE_SAMPLES = 20


def import_historical_samples(
    account_id: str = "main",
    *,
    dataset_id: str | None = None,
    source_path: str | Path | None = None,
    force: bool = False,
) -> dict:
    account = _text(account_id) or "main"
    dataset_key = _normalize_dataset_id(dataset_id)
    if dataset_key == "all" and not source_path:
        return _import_all_datasets(account, force=force)

    dataset = _resolve_dataset(dataset_id, source_path=source_path)
    paths = _capture_paths(source_path, dataset=dataset)
    raw_rows = _raw_row_count(paths)
    samples = _capture_samples(account, source_path=source_path, dataset=dataset)
    now = utc_now()
    with connect() as conn:
        upsert = upsert_historical_capture_samples(
            conn,
            account_id=account,
            dataset=dataset,
            samples=samples,
            now=now,
            force=force,
        )
        conn.commit()
        stored_count = int(
            fetch_one(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM historical_capture_samples
                WHERE account_id = ? AND dataset_id = ?
                """,
                [account, dataset["id"]],
            )["count"]
            or 0
        )
    return {
        "contract_version": HISTORICAL_CAPTURE_VERSION,
        "status": "ready" if samples else "empty",
        "account_id": account,
        "dataset_id": dataset["id"],
        "dataset_name": dataset.get("name") or "",
        "dataset": dataset,
        "source_paths": [str(path) for path in paths],
        "raw_rows": raw_rows,
        "source_row_count": raw_rows,
        "source_unique_count": _sample_unique_count(samples),
        "source_dedup_count": _sample_unique_count(samples),
        "valid_rows": len(samples),
        "inserted": upsert["inserted"],
        "updated": upsert["updated"],
        "deduped": upsert["deduped"],
        "skipped": max(0, raw_rows - len(samples)),
        "sample_count": stored_count,
        "stored_sample_count": stored_count,
        "generated_at": now,
    }


def import_douyin_history(
    account_id: str,
    clean_dir: str | Path,
    *,
    raw_dir: str | Path | None = None,
    dataset_id: str | None = None,
    dataset_name: str | None = None,
    output_dir: str | Path | None = None,
    force: bool = False,
) -> dict:
    """Import account-partitioned Douyin clean JSON as historical learning samples."""
    clean_path = _resolve_clean_works_path(clean_dir)
    clean_root = clean_path.parent
    account = _text(account_id) or _infer_account_from_path(clean_root) or "main"
    run_id = _infer_douyin_run_id(clean_root)
    dataset_key = _normalize_dataset_id(dataset_id or f"{account}_{run_id}")
    dataset = {
        "id": dataset_key,
        "name": _text(dataset_name) or f"{account} Douyin visible works {run_id}",
        "program_key": account,
    }
    quality_path = clean_root / DOUYIN_QUALITY_FILENAME
    quality = read_json(quality_path, {}) if quality_path.exists() else {}
    raw_path = _resolve_raw_api_path(raw_dir, clean_root)
    raw_items = _load_json_sequence(raw_path) if raw_path else []
    raw_by_id = {
        _text(item.get("aweme_id") or item.get("platform_item_id") or item.get("id")): item
        for item in raw_items
        if isinstance(item, dict) and _text(item.get("aweme_id") or item.get("platform_item_id") or item.get("id"))
    }
    works = [
        item
        for item in _load_json_sequence(clean_path)
        if isinstance(item, dict)
    ]
    samples = [
        _douyin_clean_sample(
            account_id=account,
            dataset=dataset,
            work=work,
            raw_api=raw_by_id.get(_text(work.get("aweme_id") or work.get("platform_item_id"))),
            quality=quality,
            clean_path=clean_path,
            raw_path=raw_path,
            run_id=run_id,
        )
        for work in works
    ]
    samples = [sample for sample in samples if sample.get("platform_item_id") or sample.get("title")]
    _label_douyin_samples(samples)
    label_counts = Counter(sample.get("performance_label") or "unlabeled" for sample in samples)
    now = utc_now()
    with connect() as conn:
        upsert = upsert_historical_capture_samples(
            conn,
            account_id=account,
            dataset=dataset,
            samples=samples,
            now=now,
            force=force,
        )
        conn.commit()
        stored_count = int(
            fetch_one(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM historical_capture_samples
                WHERE account_id = ? AND dataset_id = ?
                """,
                [account, dataset["id"]],
            )["count"]
            or 0
        )
    outputs = (
        export_douyin_history_assets(account_id=account, dataset_id=dataset["id"], output_dir=output_dir)
        if output_dir
        else {}
    )
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready" if samples else "empty",
        "account_id": account,
        "dataset_id": dataset["id"],
        "dataset_name": dataset["name"],
        "dataset": dataset,
        "source_paths": [
            str(path)
            for path in [clean_path, raw_path, quality_path if quality_path.exists() else None]
            if path
        ],
        "raw_rows": len(works),
        "source_row_count": len(works),
        "source_unique_count": _sample_unique_count(samples),
        "source_dedup_count": _sample_unique_count(samples),
        "raw_api_rows": len(raw_items),
        "valid_rows": len(samples),
        "inserted": upsert["inserted"],
        "updated": upsert["updated"],
        "deduped": upsert["deduped"],
        "skipped": max(0, len(works) - len(samples)),
        "sample_count": stored_count,
        "stored_sample_count": stored_count,
        "label_counts": dict(label_counts),
        "quality": _douyin_quality_contract(quality),
        "outputs": outputs,
        "generated_at": now,
    }


def upsert_historical_capture_samples(
    conn,
    *,
    account_id: str,
    dataset: dict,
    samples: list[dict],
    now: str | None = None,
    force: bool = False,
) -> dict:
    """Store samples with one row per account/platform/platform_item_id when possible."""
    account = _text(account_id) or "main"
    timestamp = now or utc_now()
    inserted = 0
    updated = 0
    deduped = 0
    cleanup = dedupe_historical_capture_samples(conn, account_id=account, platform="douyin")
    deduped += int(cleanup.get("deduped") or 0)
    if force:
        conn.execute(
            "DELETE FROM historical_capture_samples WHERE account_id = ? AND dataset_id = ?",
            [account, dataset.get("id") or "default"],
        )
    for sample in samples:
        row = _db_row(account, dataset, sample, timestamp)
        existing = _existing_sample_row(conn, row)
        if existing:
            deduped += 1
            if _prefer_sample_row(row, existing):
                updated += 1
                row["id"] = existing["id"]
                row["created_at"] = existing["created_at"]
                _update_sample_row(conn, row)
            continue
        inserted += 1
        insert_row(conn, "historical_capture_samples", row)
    return {"inserted": inserted, "updated": updated, "deduped": deduped}


def dedupe_historical_capture_samples(conn=None, *, account_id: str | None = None, platform: str | None = None) -> dict:
    """Collapse existing historical sample duplicates using the shared row preference policy."""
    if conn is None:
        with connect() as owned:
            result = dedupe_historical_capture_samples(owned, account_id=account_id, platform=platform)
            owned.commit()
            return result
    clauses = ["platform_item_id != ''"]
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    where = " AND ".join(clauses)
    groups = fetch_all(
        conn,
        f"""
        SELECT account_id, platform, platform_item_id, COUNT(*) AS count
        FROM historical_capture_samples
        WHERE {where}
        GROUP BY account_id, platform, platform_item_id
        HAVING COUNT(*) > 1
        """,
        params,
    )
    removed = 0
    for group in groups:
        rows = fetch_all(
            conn,
            """
            SELECT *
            FROM historical_capture_samples
            WHERE account_id = ? AND platform = ? AND platform_item_id = ?
            """,
            [group["account_id"], group["platform"], group["platform_item_id"]],
        )
        keep = _best_sample_row(rows)
        if not keep:
            continue
        remove_ids = [row["id"] for row in rows if row.get("id") != keep.get("id")]
        if remove_ids:
            conn.executemany("DELETE FROM historical_capture_samples WHERE id = ?", [(row_id,) for row_id in remove_ids])
            removed += len(remove_ids)
    return {"deduped": removed, "groups": len(groups)}


def douyin_history_baselines(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    min_count: int = 2,
    limit: int = 80,
    include_groups: bool = True,
) -> dict:
    settings = ensure_data_dirs()
    try:
        stat = settings.db_path.stat()
        revision = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        revision = (0, 0)
    result = _douyin_history_baselines_cached(
        str(settings.root),
        account_id or "",
        dataset_id or "",
        max(1, int(min_count or 1)),
        max(1, int(limit or 80)),
        bool(include_groups),
        revision[0],
        revision[1],
    )
    return deepcopy(result)


@lru_cache(maxsize=128)
def _douyin_history_baselines_cached(
    root: str,
    account_id: str,
    dataset_id: str,
    min_count: int,
    limit: int,
    include_groups: bool,
    db_mtime_ns: int,
    db_size: int,
) -> dict:
    del root, db_mtime_ns, db_size
    return _douyin_history_baselines_uncached(
        account_id or None,
        dataset_id=dataset_id or None,
        min_count=min_count,
        limit=limit,
        include_groups=include_groups,
    )


def _douyin_history_baselines_uncached(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    min_count: int = 2,
    limit: int = 80,
    include_groups: bool = True,
) -> dict:
    rows = _fetch_douyin_history_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    if not rows:
        return {
            "contract_version": DOUYIN_HISTORY_VERSION,
            "status": "empty",
            "account_id": account_id or "all",
            "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
            "sample_count": 0,
            "label_counts": {},
            "top_signals": [],
            "groups": [],
        }
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        _add_baseline_group(groups, "account", row.get("account_id"), row)
        _add_baseline_group(groups, "content_category", row.get("content_category"), row)
        _add_baseline_group(groups, "hook_type", row.get("hook_type"), row)
        _add_baseline_group(groups, "slice_structure", row.get("slice_structure"), row)
        _add_baseline_group(groups, "program_name", row.get("program_name"), row)
        _add_baseline_group(groups, "duration_bucket", _duration_bucket(row.get("duration_seconds")), row)
        _add_baseline_group(groups, "publish_hour", _publish_hour_bucket(row.get("published_at")), row)
        for artist in _split_multi_value(row.get("artist_names")):
            _add_baseline_group(groups, "artist", artist, row)
        for tag in _split_multi_value(row.get("tags")):
            _add_baseline_group(groups, "tag", tag, row)
    group_rows = [
        _summarize_baseline_group(dimension, name, samples)
        for (dimension, name), samples in groups.items()
        if name and len(samples) >= max(1, int(min_count or 1))
    ]
    group_rows.sort(
        key=lambda item: (
            float(item.get("p75_reward") or 0),
            float(item.get("avg_reward") or 0),
            int(item.get("sample_count") or 0),
        ),
        reverse=True,
    )
    rewards = [_safe_float(row.get("reward_proxy")) for row in rows]
    label_counts = Counter(row.get("performance_label") or "unlabeled" for row in rows)
    result = {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "sample_count": len(rows),
        "label_counts": dict(label_counts),
        "avg_reward": round(sum(rewards) / len(rewards), 4) if rewards else 0,
        "median_reward": round(median(rewards), 4) if rewards else 0,
        "p75_reward": _percentile(rewards, 0.75),
        "top_signals": group_rows[: max(1, int(limit or 80))],
        "group_count": len(group_rows),
        "generated_at": utc_now(),
    }
    if include_groups:
        result["groups"] = group_rows
    return result


def research_field_coverage(account_id: str | None = None, *, dataset_id: str | None = None) -> dict:
    rows = _fetch_douyin_history_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    fields = [
        "content_category",
        "hook_type",
        "slice_structure",
        "program_name",
        "artist_names",
        "song_title",
        "original_sound_owner",
        "entity_signal",
        "structure_confidence",
        "tags",
        "published_at",
        "duration_seconds",
    ]
    coverage = {field: _field_coverage(rows, field) for field in fields}
    label_counts = Counter(row.get("performance_label") or "unlabeled" for row in rows)
    usable_dimensions = [
        field
        for field in [
            "account_id",
            "content_category",
            "hook_type",
            "slice_structure",
            "artist_names",
            "song_title",
            "original_sound_owner",
            "entity_signal",
            "tags",
            "published_at",
            "duration_seconds",
        ]
        if field == "account_id" or coverage.get(field, {}).get("rate", 0) >= 0.5
    ]
    play_missing_count = sum(1 for row in rows if _is_play_missing(row))
    status = "ready" if rows else "empty"
    if rows and (coverage["content_category"]["rate"] < 0.5 or coverage["hook_type"]["rate"] < 0.5):
        status = "needs_semantic_backfill"
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": status,
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "sample_count": len(rows),
        "semantic_feature_version": SEMANTIC_FEATURE_VERSION,
        "research_label_version": RESEARCH_LABEL_VERSION,
        "label_policy": "high/mid/low are account-local visible engagement percentiles based on reward_proxy.",
        "metric_basis": "visible engagement proxy; play/view count remains missing unless explicitly available.",
        "label_counts": dict(label_counts),
        "coverage": coverage,
        "usable_dimensions": usable_dimensions,
        "play_missing_count": play_missing_count,
        "play_missing_rate": _rate(play_missing_count, len(rows)),
        "generated_at": utc_now(),
    }


def backfill_semantic_features(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 0,
    force: bool = False,
) -> dict:
    rows = _fetch_douyin_history_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    cap = int(limit or 0)
    if cap > 0:
        rows = rows[:cap]
    updated = 0
    unchanged = 0
    skipped_current = 0
    manual_verified = 0
    now = utc_now()
    with connect() as conn:
        for row in rows:
            if _text(row.get("classification_confidence")) == "manual_verified":
                manual_verified += 1
            if not force and _semantic_features_current(row):
                skipped_current += 1
                continue
            raw = _json(row.get("raw_json"), {})
            api = raw.get("api") if isinstance(raw.get("api"), dict) else {}
            classification = classify_published_work(
                title=row.get("title") or "",
                tags=row.get("tags") or "",
                aweme_id=row.get("platform_item_id") or "",
                visible_count="",
                account_id=row.get("account_id") or account_id,
                existing={**row, "api_music_title": api.get("music_title")},
            )
            raw["classification"] = classification
            updates = {
                "content_category": classification["content_category"],
                "hook_type": classification["hook_type"],
                "slice_structure": classification["slice_structure"],
                "structure_confidence": classification.get("structure_confidence") or "",
                "structure_evidence": classification.get("structure_evidence") or "",
                "structure_unknown_reason": classification.get("structure_unknown_reason") or "",
                "program_name": classification["program_name"],
                "artist_names": classification["artist_names"],
                "song_title": classification["song_title"],
                "original_sound_owner": classification.get("original_sound_owner") or "",
                "is_original_sound": int(classification.get("is_original_sound") == "1"),
                "entity_signal": classification.get("entity_signal") or "",
                "commercial_intent": classification["commercial_intent"],
                "rights_risk": classification["rights_risk"],
                "classification_confidence": classification["classification_confidence"],
                "semantic_unknown_reason": classification.get("semantic_unknown_reason") or "",
                "semantic_feature_version": SEMANTIC_FEATURE_VERSION,
                "raw_json": json.dumps(raw, ensure_ascii=False),
                "updated_at": now,
            }
            changed = any(_db_compare_value(row.get(key), value) != _db_compare_value(value, value) for key, value in updates.items())
            if not changed:
                unchanged += 1
                continue
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE historical_capture_samples SET {assignments} WHERE id = ?",
                [updates[key] for key in updates] + [row["id"]],
            )
            updated += 1
        conn.commit()
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready" if rows else "empty",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "semantic_feature_version": SEMANTIC_FEATURE_VERSION,
        "scanned": len(rows),
        "updated": updated,
        "unchanged": unchanged,
        "skipped_current": skipped_current,
        "manual_verified_seen": manual_verified,
        "force": bool(force),
        "coverage": research_field_coverage(account_id=account_id, dataset_id=dataset_id).get("coverage") if rows else {},
        "generated_at": utc_now(),
    }


def _semantic_features_current(row: dict) -> bool:
    if _text(row.get("semantic_feature_version")) != SEMANTIC_FEATURE_VERSION:
        return False
    if _text(row.get("slice_structure")).lower() not in {"", "unknown"} and not _text(row.get("structure_confidence")):
        return False
    return True


def _db_compare_value(value: Any, fallback: Any) -> str:
    if isinstance(fallback, int):
        return str(int(value or 0))
    return _text(value)


def semantic_calibration_queue(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 50,
    min_priority: float = 0.0,
    label: str | None = None,
    queue_type: str = "mixed",
    strategy: str = "research_ranker_v2_4",
    min_disagreement: float = 0.0,
) -> dict:
    rows = _fetch_douyin_history_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    items = []
    label_filter = _text(label).lower()
    priority_floor = max(0.0, _safe_float(min_priority))
    selected_queue_type = _normalize_queue_type(queue_type)
    disagreement_floor = max(0.0, _safe_float(min_disagreement))
    cap = max(1, int(limit or 50))
    recently_saved = _recently_saved_calibration_samples(rows, label_filter=label_filter, limit=min(cap, 8))
    omni_cache = qwen_omni_shadow_cache_index()
    for row in rows:
        confidence = _text(row.get("classification_confidence"))
        if confidence == "manual_verified":
            continue
        row_label = _text(row.get("performance_label")).lower()
        if label_filter and label_filter not in {"all", "any"} and row_label != label_filter:
            continue
        needs = _semantic_calibration_needs(row)
        omni_signal = _omni_calibration_signal(row, _omni_cache_for_history_row(row, omni_cache))
        signals = _calibration_queue_signals(row, needs, omni_signal=omni_signal)
        if selected_queue_type != "mixed" and signals["queue_type"] != selected_queue_type:
            continue
        if signals["disagreement_score"] < disagreement_floor:
            continue
        priority = max(_semantic_calibration_priority(row, needs), signals["priority_score"])
        if priority < priority_floor:
            continue
        if not needs and priority < 35:
            continue
        sample = _sample_row_contract(row)
        sample.update(
            {
                "needs": needs,
                "suggested_fields": [
                    item["field"]
                    for item in needs
                    if item.get("field") in MANUAL_LABEL_FIELDS
                ] + [
                    field
                    for field in omni_signal.get("recommended_fields", [])
                    if field in MANUAL_LABEL_FIELDS and field not in [item["field"] for item in needs]
                ],
                "recommended_fields": signals["recommended_fields"],
                "recommended_field_guides": _field_guides_for_list(signals["recommended_fields"]),
                "priority_score": priority,
                "impact_reason": _semantic_calibration_reason(row, needs),
                "queue_reason": signals["queue_reason"],
                "queue_type": signals["queue_type"],
                "disagreement_score": signals["disagreement_score"],
                "risk_score": signals["risk_score"],
                "baseline_strategy_score": signals["baseline_strategy_score"],
                "ranker_strategy_score": signals["ranker_strategy_score"],
                "omni_shadow": omni_signal,
                "manual_verified": confidence == "manual_verified",
            }
        )
        items.append(sample)
    items.sort(
        key=lambda item: (
            float(item.get("priority_score") or 0),
            float(item.get("normalized_reward") or item.get("reward_proxy") or 0),
        ),
        reverse=True,
    )
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready" if items else "empty",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "filters": {
            "min_priority": priority_floor,
            "label": label_filter or "all",
            "queue_type": selected_queue_type,
            "strategy": strategy or "research_ranker_v2_4",
            "min_disagreement": disagreement_floor,
        },
        "count": min(len(items), cap),
        "total_candidates": len(items),
        "queue_policy": "prioritize high-impact weak labels, low-interaction risk, and semantic-vs-ranker disagreement for manual calibration.",
        "semantic_label_catalog": semantic_label_catalog(),
        "annotation_field_guides": omni_annotation_field_guides(),
        "batch_summary": _calibration_batch_summary(items, rows),
        "samples": items[:cap],
        "recently_saved_samples": recently_saved,
        "generated_at": utc_now(),
    }


def omni_calibration_replay(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 50,
    k: int = 10,
    holdout_policy: str = "time",
) -> dict:
    from dso.learning.backtest import RESEARCH_RANKER_V24_STRATEGY, RESEARCH_RANKER_V25_SHADOW_STRATEGY, backtest_rule_ranker

    queue = semantic_calibration_queue(
        account_id=account_id,
        dataset_id=dataset_id,
        limit=limit,
        queue_type="mixed",
        strategy=RESEARCH_RANKER_V25_SHADOW_STRATEGY,
    )
    v24_report = backtest_rule_ranker(
        account_id=account_id,
        k=k,
        strategy=RESEARCH_RANKER_V24_STRATEGY,
        holdout_policy=holdout_policy,
    )
    v25_report = backtest_rule_ranker(
        account_id=account_id,
        k=k,
        strategy=RESEARCH_RANKER_V25_SHADOW_STRATEGY,
        holdout_policy=holdout_policy,
    )
    v24_metrics = (v24_report.get("metrics") or {}).get("strategy_comparison", {}).get(RESEARCH_RANKER_V24_STRATEGY, {})
    v25_metrics = (v25_report.get("metrics") or {}).get("strategy_comparison", {}).get(RESEARCH_RANKER_V25_SHADOW_STRATEGY, {})
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready",
        "mode": "omni_shadow_calibration_replay",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "query": {
            "limit": max(1, int(limit or 50)),
            "k": max(1, int(k or 10)),
            "holdout_policy": holdout_policy or "time",
        },
        "queue": {
            "status": queue.get("status"),
            "count": queue.get("count"),
            "total_candidates": queue.get("total_candidates"),
            "batch_summary": queue.get("batch_summary"),
            "samples": queue.get("samples") or [],
        },
        "before_after": {
            "baseline_strategy": RESEARCH_RANKER_V24_STRATEGY,
            "shadow_strategy": RESEARCH_RANKER_V25_SHADOW_STRATEGY,
            "v2_4": v24_metrics,
            "v2_5_shadow": v25_metrics,
            "lift_delta_vs_v2_4": round(
                _safe_float(v25_metrics.get("topk_lift_vs_random"))
                - _safe_float(v24_metrics.get("topk_lift_vs_random")),
                4,
            ),
            "high_hit_delta_vs_v2_4": round(
                _safe_float(v25_metrics.get("high_interaction_hit_rate"))
                - _safe_float(v24_metrics.get("high_interaction_hit_rate")),
                4,
            ),
            "low_avoidance_delta_vs_v2_4": round(
                _safe_float(v25_metrics.get("low_interaction_avoidance_rate"))
                - _safe_float(v24_metrics.get("low_interaction_avoidance_rate")),
                4,
            ),
        },
        "omni_shadow_summary": (v25_report.get("metrics") or {}).get("omni_shadow_summary") or {},
        "omni_shadow_ablation": (v25_report.get("metrics") or {}).get("omni_shadow_ablation") or {},
        "omni_shadow_account_metrics": (v25_report.get("metrics") or {}).get("omni_shadow_account_metrics") or [],
        "promotion_gate": (v25_report.get("metrics") or {}).get("promotion_gate") or {},
        "writes_labels": False,
        "production_weight": False,
        "recommendations": _omni_replay_recommendations(queue, v25_report),
        "generated_at": utc_now(),
    }


def update_historical_sample_labels(sample_id: str, payload: dict[str, Any]) -> dict:
    sample_key = _text(sample_id)
    if not sample_key:
        raise ValueError("sample_id is required")
    updates: dict[str, Any] = {}
    for field in MANUAL_LABEL_FIELDS:
        if field not in payload:
            continue
        value = payload.get(field)
        if field in {"artist_names", "tags"}:
            updates[field] = _join_values(value)
        else:
            updates[field] = _text(value)
    normalized = normalize_semantic_labels({**updates})
    for field in ["content_category", "hook_type", "slice_structure"]:
        if field in updates:
            updates[field] = normalized[field]
    if any(field in updates for field in ["content_category", "hook_type", "slice_structure"]):
        updates["semantic_unknown_reason"] = normalized["semantic_unknown_reason"]
    if not updates:
        raise ValueError(f"payload must include at least one of {sorted(MANUAL_LABEL_FIELDS)}")
    operator = _text(payload.get("operator")) or "local"
    reason = _text(payload.get("reason")) or "manual semantic calibration"
    now = utc_now()
    with connect() as conn:
        current = fetch_one(conn, "SELECT * FROM historical_capture_samples WHERE id = ?", [sample_key])
        if not current:
            raise KeyError(f"historical sample not found: {sample_key}")
        before = {field: current.get(field) for field in sorted(MANUAL_LABEL_FIELDS)}
        before["classification_confidence"] = current.get("classification_confidence") or ""
        before["semantic_unknown_reason"] = current.get("semantic_unknown_reason") or ""
        before["semantic_feature_version"] = current.get("semantic_feature_version") or ""
        after = dict(before)
        after.update(updates)
        after["classification_confidence"] = "manual_verified"
        after["semantic_feature_version"] = SEMANTIC_FEATURE_VERSION
        fields = [*sorted(updates.keys()), "classification_confidence", "semantic_feature_version", "updated_at"]
        values = [updates.get(field) for field in sorted(updates.keys())]
        values.extend(["manual_verified", SEMANTIC_FEATURE_VERSION, now, sample_key])
        assignments = ", ".join(f"{field} = ?" for field in fields)
        conn.execute(f"UPDATE historical_capture_samples SET {assignments} WHERE id = ?", values)
        insert_change_event(
            conn,
            entity_type="historical_capture_sample",
            entity_id=sample_key,
            change_type="semantic_label_calibration",
            before=before,
            after=after,
            reason=reason,
            operator=operator,
        )
        updated = fetch_one(conn, "SELECT * FROM historical_capture_samples WHERE id = ?", [sample_key])
        conn.commit()
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "updated",
        "sample_id": sample_key,
        "operator": operator,
        "reason": reason,
        "sample": _sample_row_contract(updated or {}),
    }


def reopen_historical_sample_calibration(sample_id: str, payload: dict[str, Any] | None = None) -> dict:
    payload = payload or {}
    sample_key = _text(sample_id)
    if not sample_key:
        raise ValueError("sample_id is required")
    confidence = _text(payload.get("classification_confidence")) or "low"
    if confidence == "manual_verified":
        raise ValueError("classification_confidence must reopen the sample, not keep manual_verified")
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("classification_confidence must be one of: low, medium, high")
    operator = _text(payload.get("operator")) or "local"
    reason = _text(payload.get("reason")) or "reopen semantic calibration"
    now = utc_now()
    with connect() as conn:
        current = fetch_one(conn, "SELECT * FROM historical_capture_samples WHERE id = ?", [sample_key])
        if not current:
            raise KeyError(f"historical sample not found: {sample_key}")
        before = {
            "classification_confidence": current.get("classification_confidence") or "",
            "semantic_feature_version": current.get("semantic_feature_version") or "",
            "semantic_unknown_reason": current.get("semantic_unknown_reason") or "",
        }
        after = {**before, "classification_confidence": confidence}
        conn.execute(
            """
            UPDATE historical_capture_samples
            SET classification_confidence = ?, updated_at = ?
            WHERE id = ?
            """,
            [confidence, now, sample_key],
        )
        insert_change_event(
            conn,
            entity_type="historical_capture_sample",
            entity_id=sample_key,
            change_type="semantic_calibration_reopened",
            before=before,
            after=after,
            reason=reason,
            operator=operator,
        )
        updated = fetch_one(conn, "SELECT * FROM historical_capture_samples WHERE id = ?", [sample_key])
        conn.commit()
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "reopened",
        "sample_id": sample_key,
        "classification_confidence": confidence,
        "operator": operator,
        "reason": reason,
        "sample": _sample_row_contract(updated or {}),
    }


def rebuild_research_labels(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    min_baseline_samples: int = RESEARCH_LABEL_MIN_BASELINE_SAMPLES,
) -> dict:
    rows = [
        row
        for row in _fetch_sample_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
        if _safe_float(row.get("reward_proxy")) > 0
    ]
    if not rows:
        return {
            "contract_version": DOUYIN_HISTORY_VERSION,
            "status": "empty",
            "account_id": account_id or "all",
            "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
            "research_label_version": RESEARCH_LABEL_VERSION,
            "updated": 0,
            "generated_at": utc_now(),
        }
    now = utc_now()
    min_count = max(2, int(min_baseline_samples or RESEARCH_LABEL_MIN_BASELINE_SAMPLES))
    groups = _research_label_baseline_groups(rows)
    scored_by_account: dict[str, list[dict]] = defaultdict(list)
    fallback_counts: Counter[str] = Counter()
    for row in rows:
        reward = _safe_float(row.get("reward_proxy"))
        baseline = _research_label_baseline(row, groups, min_count=min_count)
        adjusted = reward - float(baseline.get("median_reward") or 0.0)
        account = _text(row.get("account_id")) or "unknown"
        fallback_counts[baseline["scope"]] += 1
        scored_by_account[account].append(
            {
                "id": row.get("id") or "",
                "reward_proxy": reward,
                "adjusted_reward": adjusted,
                "baseline": baseline,
            }
        )
    updates = []
    account_summaries = []
    for account, scored_rows in scored_by_account.items():
        ranked = sorted(
            scored_rows,
            key=lambda item: (
                float(item.get("adjusted_reward") or 0),
                float(item.get("reward_proxy") or 0),
                _text(item.get("id")),
            ),
            reverse=True,
        )
        n = len(ranked)
        top_count = max(1, math.ceil(n * 0.2))
        bottom_count = max(1, math.ceil(n * 0.2)) if n > 1 else 0
        label_counts: Counter[str] = Counter()
        for index, item in enumerate(ranked):
            rank = index + 1
            percentile = 100.0 if n == 1 else round(((n - rank) / (n - 1)) * 100, 4)
            if rank <= top_count:
                label = "high"
                tail_reason = "top_20"
            elif bottom_count and rank > n - bottom_count:
                label = "low"
                tail_reason = "bottom_20"
            else:
                label = "mid"
                tail_reason = "middle"
            baseline = item["baseline"]
            reason = (
                f"{baseline['scope']}_adjusted_visible_engagement_{tail_reason};"
                f"age_bucket={baseline['age_bucket']};duration_bucket={baseline['duration_bucket']};"
                f"baseline_n={baseline['sample_count']}"
            )
            updates.append(
                {
                    "id": item["id"],
                    "label_rank": rank,
                    "label_percentile": percentile,
                    "normalized_reward": percentile,
                    "performance_label": label,
                    "label_reason": reason,
                    "research_label_version": RESEARCH_LABEL_VERSION,
                }
            )
            label_counts[label] += 1
        account_summaries.append(
            {
                "account_id": account,
                "sample_count": n,
                "label_confidence": "ready" if n >= min_count else "low_confidence",
                "label_counts": dict(label_counts),
            }
        )
    with connect() as conn:
        for item in updates:
            conn.execute(
                """
                UPDATE historical_capture_samples
                SET normalized_reward = ?, performance_label = ?, label_rank = ?,
                    label_percentile = ?, label_reason = ?, research_label_version = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                [
                    item["normalized_reward"],
                    item["performance_label"],
                    item["label_rank"],
                    item["label_percentile"],
                    item["label_reason"],
                    item["research_label_version"],
                    now,
                    item["id"],
                ],
            )
        conn.commit()
    label_counts = Counter(item["performance_label"] for item in updates)
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready" if len(updates) >= min_count else "low_confidence",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "research_label_version": RESEARCH_LABEL_VERSION,
        "updated": len(updates),
        "label_policy": "account-local top/bottom 20% on age/duration adjusted visible engagement residuals; reward_proxy is unchanged.",
        "baseline_policy": "account+age_bucket+duration_bucket, fallback to account+age_bucket, then account baseline, then global baseline.",
        "baseline_fallback_counts": dict(fallback_counts),
        "label_counts": {key: int(label_counts.get(key) or 0) for key in ["high", "mid", "low"]},
        "per_account": sorted(account_summaries, key=lambda item: item["sample_count"], reverse=True),
        "generated_at": now,
    }


def export_douyin_history_assets(
    *,
    account_id: str | None = None,
    dataset_id: str | None = None,
    output_dir: str | Path,
) -> dict:
    out = Path(output_dir)
    rows = _fetch_douyin_history_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    sample_rows = [_sample_row_contract(row) for row in rows]
    samples = {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "status": "ready" if sample_rows else "empty",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "count": len(sample_rows),
        "samples": sample_rows,
    }
    baselines = douyin_history_baselines(account_id=account_id, dataset_id=dataset_id, min_count=1)
    prefix = _export_prefix(account_id)
    paths = {
        "samples": out / f"douyin_history_samples_{prefix}.json",
        "baselines": out / f"douyin_history_baselines_{prefix}.json",
        "insights": out / f"douyin_history_insights_{prefix}.md",
    }
    latest_paths = {
        "samples_latest": out / "history_samples_latest.json",
        "baselines_latest": out / "account_baselines_latest.json",
        "insights_latest": out / "account_insights_latest.md",
    }
    write_json(paths["samples"], samples)
    write_json(paths["baselines"], baselines)
    paths["insights"].parent.mkdir(parents=True, exist_ok=True)
    paths["insights"].write_text(_douyin_insights_markdown(baselines), encoding="utf-8")
    write_json(latest_paths["samples_latest"], samples)
    write_json(latest_paths["baselines_latest"], baselines)
    latest_paths["insights_latest"].write_text(_douyin_insights_markdown(baselines), encoding="utf-8")
    return {key: str(path) for key, path in {**paths, **latest_paths}.items()}


def list_historical_samples(account_id: str | None = "main", *, dataset_id: str | None = None, limit: int = 50) -> dict:
    rows = _fetch_sample_rows(account_id=account_id, dataset_id=dataset_id, limit=limit)
    samples = [_sample_row_contract(row) for row in rows]
    return {
        "contract_version": HISTORICAL_CAPTURE_VERSION,
        "status": "ready" if samples else "empty",
        "account_id": account_id or "all",
        "dataset_id": _normalize_dataset_id(dataset_id) if dataset_id else "all",
        "count": len(samples),
        "samples": samples,
    }


def historical_sample_summary(account_id: str | None = "main") -> dict:
    settings = ensure_data_dirs()
    try:
        stat = settings.db_path.stat()
        revision = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        revision = (0, 0)
    result = _historical_sample_summary_cached(
        str(settings.root),
        account_id or "",
        revision[0],
        revision[1],
    )
    return deepcopy(result)


@lru_cache(maxsize=64)
def _historical_sample_summary_cached(
    root: str,
    account_id: str,
    db_mtime_ns: int,
    db_size: int,
) -> dict:
    del root, db_mtime_ns, db_size
    return _historical_sample_summary_uncached(account_id or None)


def _historical_sample_summary_uncached(account_id: str | None = "main") -> dict:
    where = ""
    params: list[Any] = []
    if account_id:
        where = "WHERE account_id = ?"
        params.append(account_id)
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT *
            FROM historical_capture_samples
            {where}
            ORDER BY updated_at DESC, dataset_id ASC
            """,
            params,
        )

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("account_id") or "", row.get("dataset_id") or "default")].append(row)

    datasets = [_historical_dataset_summary(group_rows) for group_rows in grouped.values()]
    datasets.sort(key=lambda item: (item.get("latest_at") or "", item.get("dataset_id") or ""), reverse=True)
    overall = _historical_scope_stats(rows)
    account_quality = _account_quality_contract(rows)
    response = {
        "contract_version": HISTORICAL_CAPTURE_VERSION,
        "status": "ready" if datasets else "empty",
        "account_id": account_id or "all",
        "count": len(datasets),
        "sample_count": len(rows),
        "datasets": datasets,
        "account_quality": account_quality,
    }
    response.update(_historical_metric_fields(overall))
    response["data_lineage"] = _historical_lineage_contract(overall)
    return response


def _historical_dataset_summary(rows: list[dict]) -> dict:
    first = rows[0] if rows else {}
    labels = Counter(row.get("performance_label") or "" for row in rows)
    rewards = [float(row.get("reward_proxy") or 0) for row in rows]
    stats = _historical_scope_stats(rows)
    account_id = first.get("account_id") or ""
    dataset_id = first.get("dataset_id") or "default"
    meta = account_metadata(account_id)
    display_name = dataset_display_name(account_id, dataset_id=dataset_id, fallback=first.get("dataset_name") or dataset_id)
    item = {
        "account_id": account_id,
        "account_display_name": meta.get("account_display_name") or account_id,
        "account_tier": meta.get("account_tier") or "",
        "id": dataset_id,
        "dataset_id": dataset_id,
        "name": display_name,
        "display_name": display_name,
        "program_key": first.get("program_key") or "",
        "sample_count": len(rows),
        "max_views": max([_safe_int(row.get("views")) for row in rows], default=0),
        "avg_reward": round(sum(rewards) / len(rewards), 4) if rewards else 0,
        "high_count": int(labels.get("high") or 0),
        "low_count": int(labels.get("low") or 0),
        "latest_at": max([row.get("updated_at") or "" for row in rows], default=""),
    }
    item.update(_historical_metric_fields(stats))
    item["data_lineage"] = _historical_lineage_contract(stats)
    return item


def _account_quality_contract(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[_text(row.get("account_id")) or "main"].append(row)
    items = []
    for account, account_rows in grouped.items():
        stats = _historical_scope_stats(account_rows)
        meta = account_metadata(account)
        rewards = sorted(
            _safe_float(row.get("reward_proxy") or row.get("normalized_reward"))
            for row in _dedupe_history_rows(account_rows)
            if _safe_float(row.get("reward_proxy") or row.get("normalized_reward")) > 0
        )
        fields = _historical_metric_fields(stats)
        confidence = _account_confidence(fields)
        items.append(
            {
                "account_id": account,
                "account_display_name": meta.get("account_display_name") or account,
                "account_tier": meta.get("account_tier") or "",
                "account_quality_grade": meta.get("account_quality_grade") or "",
                "sample_count": len(account_rows),
                "stored_sample_count": fields["stored_sample_count"],
                "formal_sample_count": fields["formal_sample_count"],
                "deduped_sample_count": fields["deduped_sample_count"],
                "trainable_sample_count": fields["trainable_sample_count"],
                "duplicate_item_group_count": fields["duplicate_item_group_count"],
                "likes_coverage_rate": fields["likes_coverage_rate"],
                "favorites_coverage_rate": fields["favorites_coverage_rate"],
                "comments_coverage_rate": fields["comments_coverage_rate"],
                "shares_coverage_rate": fields["shares_coverage_rate"],
                "play_missing_rate": fields["play_missing_rate"],
                "metric_coverage": fields["metric_coverage"],
                "interaction_coverage": fields["interaction_coverage"],
                "reward_p50": _percentile(rewards, 0.5),
                "reward_p75": _percentile(rewards, 0.75),
                "confidence": confidence["key"],
                "confidence_label": confidence["label"],
                "confidence_reason": confidence["reason"],
            }
        )
    items.sort(key=lambda item: (int(item["sample_count"]), item["account_id"]), reverse=True)
    return items


def _account_confidence(fields: dict) -> dict:
    sample_count = int(fields.get("trainable_sample_count") or fields.get("deduped_sample_count") or 0)
    coverage_rates = [
        float(fields.get("likes_coverage_rate") or 0),
        float(fields.get("favorites_coverage_rate") or 0),
        float(fields.get("comments_coverage_rate") or 0),
        float(fields.get("shares_coverage_rate") or 0),
    ]
    min_coverage = min(coverage_rates) if coverage_rates else 0.0
    if sample_count >= 300 and min_coverage >= 0.9:
        return {"key": "ready", "label": "可用于账号趋势", "reason": "样本数达到 300，四项互动字段覆盖稳定。"}
    if sample_count >= 50:
        return {"key": "low_confidence", "label": "低置信趋势", "reason": "样本数或互动字段覆盖未达到稳定阈值。"}
    return {"key": "insufficient_history", "label": "样本不足", "reason": "少于 50 条可训练历史样本，只能作为个案参考。"}


def _historical_scope_stats(rows: list[dict]) -> dict:
    deduped_rows = _dedupe_history_rows(rows)
    formal_rows = [row for row in deduped_rows if not _is_mock_history_row(row)]
    coverage_total = len(formal_rows)
    metric_counts = {
        metric: sum(1 for row in formal_rows if _has_valid_history_metric(row, metric))
        for metric in ["likes", "favorites", "comments", "shares"]
    }
    trainable_rows = [row for row in formal_rows if _is_trainable_history_row(row)]
    play_missing_count = sum(1 for row in formal_rows if _is_play_missing(row))
    duplicate_groups = _duplicate_item_groups(rows)
    source = _source_lineage(rows)
    return {
        "source": source,
        "stored_sample_count": len(rows),
        "formal_sample_count": len(rows),
        "deduped_sample_count": len(deduped_rows),
        "metric_coverage_sample_count": coverage_total,
        "trainable_sample_count": len(trainable_rows),
        "duplicate_item_group_count": len(duplicate_groups),
        "duplicate_item_groups": duplicate_groups,
        "metric_counts": metric_counts,
        "metric_coverage": {
            metric: _coverage_contract(count, coverage_total)
            for metric, count in metric_counts.items()
        },
        "play_missing_count": play_missing_count,
        "play_missing_rate": _rate(play_missing_count, coverage_total),
    }


def _historical_metric_fields(stats: dict) -> dict:
    coverage = stats.get("metric_coverage") or {}
    return {
        "source_row_count": int((stats.get("source") or {}).get("source_row_count") or 0),
        "source_unique_count": int((stats.get("source") or {}).get("source_unique_count") or 0),
        "source_dedup_count": int((stats.get("source") or {}).get("source_unique_count") or 0),
        "stored_sample_count": int(stats.get("stored_sample_count") or 0),
        "formal_sample_count": int(stats.get("formal_sample_count") or 0),
        "deduped_sample_count": int(stats.get("deduped_sample_count") or 0),
        "trainable_sample_count": int(stats.get("trainable_sample_count") or 0),
        "metric_coverage_sample_count": int(stats.get("metric_coverage_sample_count") or 0),
        "metric_coverage": coverage,
        "interaction_coverage": coverage,
        "likes_coverage_rate": float((coverage.get("likes") or {}).get("rate") or 0),
        "favorites_coverage_rate": float((coverage.get("favorites") or {}).get("rate") or 0),
        "comments_coverage_rate": float((coverage.get("comments") or {}).get("rate") or 0),
        "shares_coverage_rate": float((coverage.get("shares") or {}).get("rate") or 0),
        "play_missing_count": int(stats.get("play_missing_count") or 0),
        "play_missing_rate": float(stats.get("play_missing_rate") or 0),
        "duplicate_item_group_count": int(stats.get("duplicate_item_group_count") or 0),
        "duplicate_item_groups": stats.get("duplicate_item_groups") or [],
    }


def _historical_lineage_contract(stats: dict) -> dict:
    source = stats.get("source") or {}
    return {
        "source_row_count": int(source.get("source_row_count") or 0),
        "source_unique_count": int(source.get("source_unique_count") or 0),
        "source_dedup_count": int(source.get("source_unique_count") or 0),
        "source_file_count": int(source.get("source_file_count") or 0),
        "source_paths": source.get("source_paths") or [],
        "stored_sample_count": int(stats.get("stored_sample_count") or 0),
        "formal_sample_count": int(stats.get("formal_sample_count") or 0),
        "deduped_sample_count": int(stats.get("deduped_sample_count") or 0),
        "trainable_sample_count": int(stats.get("trainable_sample_count") or 0),
        "metric_coverage_sample_count": int(stats.get("metric_coverage_sample_count") or 0),
        "metric_coverage": stats.get("metric_coverage") or {},
        "play_missing_count": int(stats.get("play_missing_count") or 0),
        "play_missing_rate": float(stats.get("play_missing_rate") or 0),
        "duplicate_item_group_count": int(stats.get("duplicate_item_group_count") or 0),
        "duplicate_item_groups": stats.get("duplicate_item_groups") or [],
    }


def _coverage_contract(count: int, total: int) -> dict:
    return {
        "count": int(count or 0),
        "sample_count": int(total or 0),
        "rate": _rate(count, total),
    }


def _field_coverage(rows: list[dict], field: str) -> dict:
    if field == "duration_seconds":
        count = sum(1 for row in rows if _safe_float(row.get(field)) > 0)
    else:
        count = sum(1 for row in rows if _text(row.get(field)) and _text(row.get(field)).lower() not in {"unknown", "none", "null"})
    return _coverage_contract(count, len(rows))


def _rate(count: int | float, total: int | float) -> float:
    denominator = float(total or 0)
    if denominator <= 0:
        return 0.0
    return round(float(count or 0) / denominator, 4)


def _dedupe_history_rows(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for row in rows:
        key = _history_row_identity(row)
        current = best.get(key)
        if not current or _history_row_rank(row) > _history_row_rank(current):
            best[key] = row
    return list(best.values())


def _history_row_identity(row: dict) -> str:
    account = _text(row.get("account_id")) or "main"
    platform = _text(row.get("platform")) or "douyin"
    item_id = _text(row.get("platform_item_id"))
    if item_id:
        return f"item:{account}:{platform}:{item_id}"
    sample_key = _text(row.get("sample_key"))
    if _is_stable_title_key(sample_key):
        return f"key:{account}:{platform}:{sample_key}"
    return f"id:{row.get('id') or id(row)}"


def _history_row_rank(row: dict) -> tuple[int, int, str]:
    return (
        _dataset_date_rank(row.get("dataset_id")),
        _safe_int(row.get("views")),
        _text(row.get("updated_at")),
    )


def _is_trainable_history_row(row: dict) -> bool:
    return (
        not _is_mock_history_row(row)
        and _has_stable_history_identity(row)
        and any(_has_valid_history_metric(row, metric) for metric in ["likes", "favorites", "comments", "shares"])
    )


def _has_stable_history_identity(row: dict) -> bool:
    if _text(row.get("platform_item_id")):
        return True
    return _is_stable_title_key(_text(row.get("sample_key")))


def _is_stable_title_key(value: str) -> bool:
    text = _text(value)
    return text.startswith("title:") and len(text) > len("title:")


def _is_mock_history_row(row: dict) -> bool:
    source_kind = _text(row.get("source_kind")).lower()
    if "mock" in source_kind:
        return True
    raw = _history_raw_json(row)
    if isinstance(raw, dict):
        raw_source = _text(raw.get("sample_source") or raw.get("source") or raw.get("source_kind")).lower()
        return raw_source == "mock"
    return False


def _has_valid_history_metric(row: dict, metric: str) -> bool:
    return _safe_int(row.get(metric)) > 0


def _is_play_missing(row: dict) -> bool:
    raw = _history_raw_json(row)
    if isinstance(raw, dict):
        availability = raw.get("metric_availability")
        if isinstance(availability, dict) and "views" in availability:
            return not bool(availability.get("views"))
        api = raw.get("api") if isinstance(raw.get("api"), dict) else {}
        clean = raw.get("clean") if isinstance(raw.get("clean"), dict) else {}
        if _text(row.get("source_kind")).lower() == "douyin_clean_json":
            return not any(
                _safe_int(value) > 0
                for value in [
                    api.get("play_count"),
                    api.get("view_count"),
                    api.get("views"),
                    clean.get("play_count"),
                    clean.get("view_count"),
                    clean.get("views"),
                ]
            )
    return _safe_int(row.get("views")) <= 0


def _history_raw_json(row: dict) -> dict:
    cache_key = "_summary_raw_json"
    cached = row.get(cache_key)
    if isinstance(cached, dict):
        return cached
    parsed = _json(row.get("raw_json"), {})
    result = parsed if isinstance(parsed, dict) else {}
    row[cache_key] = result
    return result


def _duplicate_item_groups(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        item_id = _text(row.get("platform_item_id"))
        if not item_id:
            continue
        account = _text(row.get("account_id")) or "main"
        platform = _text(row.get("platform")) or "douyin"
        groups[(account, platform, item_id)].append(row)
    duplicates = []
    for (account, platform, item_id), group_rows in groups.items():
        if len(group_rows) <= 1:
            continue
        duplicates.append(
            {
                "account_id": account,
                "platform": platform,
                "platform_item_id": item_id,
                "count": len(group_rows),
                "sample_count": len(group_rows),
                "dataset_ids": sorted({_text(row.get("dataset_id")) for row in group_rows if _text(row.get("dataset_id"))}),
                "titles": sorted({_text(row.get("title")) for row in group_rows if _text(row.get("title"))})[:5],
            }
        )
    duplicates.sort(key=lambda item: (int(item["count"]), item["account_id"], item["platform_item_id"]), reverse=True)
    return duplicates


def _source_lineage(rows: list[dict]) -> dict:
    paths = []
    seen: set[str] = set()
    for row in rows:
        source_file = _text(row.get("source_file"))
        if source_file and source_file not in seen:
            seen.add(source_file)
            paths.append(source_file)
    source_row_count = 0
    unique_keys: set[str] = set()
    for value in paths:
        path = Path(value)
        source_rows = _source_rows(path)
        source_row_count += len(source_rows)
        for source_row in source_rows:
            key = _source_row_identity(source_row)
            if key:
                unique_keys.add(key)
    return {
        "source_row_count": source_row_count,
        "source_unique_count": len(unique_keys),
        "source_file_count": len(paths),
        "source_paths": paths,
    }


def _source_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    resolved = path.resolve()
    stat = resolved.stat()
    return [dict(row) for row in _source_rows_cached(str(resolved), stat.st_mtime_ns, stat.st_size)]


@lru_cache(maxsize=256)
def _source_rows_cached(path_text: str, mtime_ns: int, size: int) -> tuple[dict, ...]:
    del mtime_ns, size
    path = Path(path_text)
    if path.suffix.lower() == ".json":
        rows = _load_json_sequence(path)
    else:
        try:
            rows = _read_rows(path)
        except Exception:
            rows = []
    return tuple(dict(row) for row in rows)


def _source_row_identity(row: dict) -> str:
    item_id = _clean_item_id(
        row.get("aweme_id")
        or row.get("id")
        or row.get("视频ID文本")
        or row.get("platform_item_id")
        or row.get("work_key")
    )
    if item_id:
        return f"item:{item_id}"
    title = _text(row.get("normalized_title") or row.get("标题") or row.get("platform_title") or row.get("title_tags_text") or row.get("title") or row.get("desc"))
    return f"title:{_stable_key(title)}" if title else ""


def historical_samples_for_prototypes(
    account_id: str = "main",
    *,
    dataset_id: str | None = None,
    limit: int = 0,
) -> list[dict]:
    account = _text(account_id)
    dataset_key = _normalize_dataset_id(dataset_id)
    query_account = None if not account or account.lower() == "all" or (account == "main" and dataset_key == "all") else account
    rows = _fetch_sample_rows(account_id=query_account, dataset_id=dataset_id, limit=limit)
    return [_prototype_sample(row) for row in rows]


def _import_all_datasets(account_id: str, *, force: bool) -> dict:
    catalog = list_capture_datasets(include_all=False)
    datasets = [item for item in catalog.get("datasets", []) if item.get("id")]
    children = [
        import_historical_samples(account_id, dataset_id=str(item["id"]), force=force)
        for item in datasets
    ]
    summary = historical_sample_summary(account_id)
    return {
        "contract_version": HISTORICAL_CAPTURE_VERSION,
        "status": "ready" if children else "empty",
        "account_id": account_id,
        "dataset_id": "all",
        "dataset_name": "全部采集",
        "source_paths": [path for child in children for path in child.get("source_paths") or []],
        "raw_rows": sum(int(child.get("raw_rows") or 0) for child in children),
        "source_row_count": sum(int(child.get("source_row_count") or child.get("raw_rows") or 0) for child in children),
        "source_unique_count": sum(int(child.get("source_unique_count") or 0) for child in children),
        "source_dedup_count": sum(int(child.get("source_dedup_count") or child.get("source_unique_count") or 0) for child in children),
        "valid_rows": sum(int(child.get("valid_rows") or 0) for child in children),
        "inserted": sum(int(child.get("inserted") or 0) for child in children),
        "updated": sum(int(child.get("updated") or 0) for child in children),
        "deduped": sum(int(child.get("deduped") or 0) for child in children),
        "skipped": sum(int(child.get("skipped") or 0) for child in children),
        "sample_count": int(summary.get("sample_count") or 0),
        "stored_sample_count": int(summary.get("stored_sample_count") or summary.get("sample_count") or 0),
        "datasets": children,
        "summary": summary,
        "generated_at": utc_now(),
    }


def _fetch_sample_rows(account_id: str | None, dataset_id: str | None, limit: int = 50) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    dataset_key = _normalize_dataset_id(dataset_id)
    if dataset_id and dataset_key != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset_key)
    query = """
        SELECT *
        FROM historical_capture_samples
    """
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY views DESC, updated_at DESC"
    row_limit = int(limit or 0)
    if row_limit > 0:
        query += " LIMIT ?"
        params.append(row_limit)
    with connect() as conn:
        return fetch_all(conn, query, params)


def _fetch_douyin_history_rows(account_id: str | None, dataset_id: str | None, limit: int = 0) -> list[dict]:
    rows = _fetch_sample_rows(account_id=account_id, dataset_id=dataset_id, limit=limit)
    return [row for row in rows if (row.get("source_kind") or "").startswith("douyin")]


def _raw_row_count(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += len(_read_rows(path))
        except Exception:
            continue
    return total


def _sample_unique_count(samples: list[dict]) -> int:
    keys: set[str] = set()
    for sample in samples:
        item_id = _text(sample.get("platform_item_id"))
        title = _text(sample.get("title"))
        if item_id:
            keys.add(f"item:{item_id}")
        elif title:
            keys.add(f"title:{_stable_key(title)}")
    return len(keys)


def _resolve_clean_works_path(clean_dir: str | Path) -> Path:
    path = Path(clean_dir)
    if path.is_file():
        return path
    candidates = [
        path / DOUYIN_CLEAN_WORKS_FILENAME,
        path / "douyin_visible_works_dedup.json",
        path / "douyin_visible_records_latest.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = sorted(path.glob("*dedup*.json"))
    if matches:
        return matches[-1]
    raise FileNotFoundError(f"Douyin clean works JSON not found under {path}")


def _resolve_raw_api_path(raw_dir: str | Path | None, clean_root: Path) -> Path | None:
    patterns = ["*_post_api_works.json", "*api_works.json", "*works.json"]
    if raw_dir:
        path = Path(raw_dir)
        if path.is_file():
            return path
        return _first_matching_file(path, patterns)
    run_id = _infer_douyin_run_id(clean_root)
    account_root = clean_root.parent
    exact = account_root / f"raw_{run_id}"
    if exact.exists():
        match = _first_matching_file(exact, patterns)
        if match:
            return match
    raw_dirs = sorted(path for path in account_root.glob("raw_*") if path.is_dir())
    for raw_root in reversed(raw_dirs):
        match = _first_matching_file(raw_root, patterns)
        if match:
            return match
    return None


def _first_matching_file(root: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(path for path in root.glob(pattern) if path.is_file())
        if matches:
            return matches[-1]
    return None


def _load_json_sequence(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    data = read_json(path, [])
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ["works", "records", "items", "aweme_list", "data"]:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = _first_list_value(value)
                if nested is not None:
                    return [item for item in nested if isinstance(item, dict)]
    return []


def _first_list_value(data: dict) -> list | None:
    for value in data.values():
        if isinstance(value, list):
            return value
    return None


def _douyin_clean_sample(
    *,
    account_id: str,
    dataset: dict,
    work: dict,
    raw_api: dict | None,
    quality: dict,
    clean_path: Path,
    raw_path: Path | None,
    run_id: str,
) -> dict:
    api = raw_api or {}
    aweme_id = _text(work.get("aweme_id") or work.get("platform_item_id") or api.get("aweme_id") or api.get("id"))
    title = _text(work.get("normalized_title") or work.get("title") or api.get("desc"))
    visible_count = _safe_int(
        work.get("best_visible_count_number")
        or work.get("visible_count_number")
        or work.get("visible_count")
    )
    play_count, play_source = _first_metric_int(
        ("raw_api", api, ["play_count", "view_count"]),
        ("clean_json", work, ["play_count", "view_count"]),
    )
    likes, likes_source = _first_metric_int(
        ("raw_api", api, ["digg_count", "like_count"]),
        ("clean_json", work, ["digg_count", "like_count"]),
    )
    comments, comments_source = _first_metric_int(
        ("raw_api", api, ["comment_count", "comments"]),
        ("clean_json", work, ["comment_count", "comments"]),
    )
    favorites, favorites_source = _first_metric_int(
        ("raw_api", api, ["collect_count", "favorite_count", "favorites"]),
        ("clean_json", work, ["collect_count", "favorite_count", "favorites"]),
    )
    shares, shares_source = _first_metric_int(
        ("raw_api", api, ["share_count", "shares"]),
        ("clean_json", work, ["share_count", "shares"]),
    )
    follows, follows_source = _first_metric_int(
        ("raw_api", api, ["follow_count", "follows"]),
        ("clean_json", work, ["follow_count", "follows"]),
    )
    views = play_count if play_count > 0 else 0
    duration_seconds = _duration_seconds(api.get("duration") or work.get("duration") or work.get("duration_seconds"))
    published_at = _timestamp_iso(api.get("create_time")) or _text(work.get("published_at"))
    collected_at = _text(work.get("last_observed_at") or work.get("first_observed_at"))
    tags_value = work.get("tags")
    classification = classify_published_work(
        title=title,
        tags=tags_value,
        aweme_id=aweme_id,
        visible_count=visible_count,
        account_id=account_id,
        existing={**work, "api_music_title": api.get("music_title")},
    )
    quality_contract = _douyin_quality_contract(quality)
    metric_sources = {
        "play_count": play_source,
        "likes": likes_source,
        "comments": comments_source,
        "favorites": favorites_source,
        "shares": shares_source,
        "follows": follows_source,
    }
    metric_source = _combined_metric_source(metric_sources.values())
    metric_window = (
        _text(api.get("metric_window") or api.get("statistics_window") or api.get("window_name"))
        or _text(work.get("metric_window") or work.get("statistics_window") or work.get("window_name"))
        or "lifetime_at_capture"
    )
    play_count_missing = play_count <= 0
    reward = _douyin_reward_proxy(
        views=views,
        likes=likes,
        comments=comments,
        favorites=favorites,
        shares=shares,
        follows=follows,
    )
    return {
        "account_id": account_id,
        "dataset_id": dataset.get("id") or "",
        "dataset_name": dataset.get("name") or "",
        "source_file": str(clean_path),
        "source_kind": "douyin_clean_json",
        "platform_item_id": aweme_id,
        "title": title,
        "platform_url": _text(work.get("video_url") or api.get("video_url") or _douyin_video_url(aweme_id)),
        "views": views,
        "likes": likes,
        "comments": comments,
        "favorites": favorites,
        "shares": shares,
        "follows": follows,
        "reward_proxy": reward,
        "content_category": classification["content_category"],
        "hook_type": classification["hook_type"],
        "slice_structure": classification["slice_structure"],
        "structure_confidence": classification.get("structure_confidence") or "",
        "structure_evidence": classification.get("structure_evidence") or "",
        "structure_unknown_reason": classification.get("structure_unknown_reason") or "",
        "program_name": classification["program_name"],
        "artist_names": classification["artist_names"],
        "song_title": _text(classification["song_title"] or work.get("song_title") or api.get("music_title")),
        "original_sound_owner": classification.get("original_sound_owner") or "",
        "is_original_sound": int(classification.get("is_original_sound") == "1"),
        "entity_signal": classification.get("entity_signal") or "",
        "tags": _join_values(tags_value),
        "commercial_intent": classification["commercial_intent"],
        "rights_risk": classification["rights_risk"],
        "classification_confidence": classification["classification_confidence"],
        "semantic_unknown_reason": classification.get("semantic_unknown_reason") or "",
        "published_at": published_at,
        "collected_at": collected_at,
        "quality_grade": quality_contract.get("quality_grade") or "",
        "quality_score": float(quality_contract.get("quality_score") or 0),
        "source_run_id": run_id,
        "feature_version": DOUYIN_HISTORY_VERSION,
        "semantic_feature_version": classification["semantic_feature_version"],
        "duration_seconds": duration_seconds,
        "media_type": _text(api.get("media_type") or api.get("aweme_type")),
        "raw": {
            "clean": work,
            "api": api,
            "source_raw_path": str(raw_path) if raw_path else "",
            "quality": quality_contract,
            "metric_quality": {
                "play_count_missing": play_count_missing,
                "visible_count": visible_count,
                "metric_source": metric_source,
                "metric_window": metric_window,
                "field_sources": metric_sources,
            },
            "metric_policy": {
                "views": "explicit play_count/view_count only; 0 when missing",
                "likes": "raw_api.digg_count/raw_api.like_count preferred; visible_count is never used as likes",
                "reward_proxy": "log engagement proxy from explicit play and engagement metrics",
                "play_count_missing": play_count_missing,
                "metric_source": metric_source,
                "metric_window": metric_window,
            },
            "classification": classification,
        },
    }


def _label_douyin_samples(samples: list[dict]) -> None:
    if not samples:
        return
    ranked = sorted(
        samples,
        key=lambda sample: (
            float(sample.get("reward_proxy") or 0),
            int(sample.get("views") or 0),
            _text(sample.get("platform_item_id")),
        ),
        reverse=True,
    )
    n = len(ranked)
    top_count = max(1, math.ceil(n * 0.2))
    bottom_count = max(1, math.ceil(n * 0.2)) if n > 1 else 0
    for index, sample in enumerate(ranked):
        rank = index + 1
        percentile = 100.0 if n == 1 else round(((n - rank) / (n - 1)) * 100, 4)
        if rank <= top_count:
            label = "high"
            reason = "account_reward_rank_top_20"
        elif bottom_count and rank > n - bottom_count:
            label = "low"
            reason = "account_reward_rank_bottom_20"
        else:
            label = "mid"
            reason = "account_reward_rank_middle"
        sample["label_rank"] = rank
        sample["label_percentile"] = percentile
        sample["normalized_reward"] = percentile
        sample["performance_label"] = label
        sample["label_reason"] = reason
        sample["research_label_version"] = RESEARCH_LABEL_VERSION


def _semantic_calibration_needs(row: dict) -> list[dict]:
    needs = []
    for field, label in [
        ("content_category", "内容类别"),
        ("hook_type", "Hook 类型"),
        ("slice_structure", "切片结构"),
        ("artist_names", "艺人名称"),
    ]:
        value = _text(row.get(field))
        if not value or value.lower() in {"unknown", "none", "null", "其他", "其它"}:
            needs.append({"field": field, "label": label, "reason": "missing_or_unknown"})
    confidence = _text(row.get("classification_confidence"))
    if confidence in {"", "low"}:
        needs.append({"field": "classification_confidence", "label": "弱分类可信度", "reason": "low_confidence"})
    return needs


def _semantic_calibration_priority(row: dict, needs: list[dict]) -> float:
    reward = _safe_float(row.get("normalized_reward") or row.get("reward_proxy"))
    label = _text(row.get("performance_label")).lower()
    confidence = _text(row.get("classification_confidence")).lower()
    priority = min(40.0, reward * 0.35)
    if label == "high":
        priority += 28
    elif label == "low":
        priority += 18
    priority += min(36, len(needs) * 9)
    if confidence in {"", "low"}:
        priority += 12
    elif confidence == "medium":
        priority += 5
    if _safe_float(row.get("reward_proxy")) <= 0:
        priority -= 12
    return round(max(0.0, min(100.0, priority)), 2)


def _semantic_calibration_reason(row: dict, needs: list[dict]) -> str:
    label = _text(row.get("performance_label")) or "unlabeled"
    missing = ",".join(item["field"] for item in needs[:4]) or "semantic_fields"
    return f"{label} sample; calibrate {missing} before using it as stronger historical evidence"


def _normalize_queue_type(value: Any) -> str:
    text = _text(value).lower()
    return text if text in {"impact", "risk", "disagreement", "mixed"} else "mixed"


def _calibration_queue_signals(row: dict, needs: list[dict], *, omni_signal: dict | None = None) -> dict:
    omni_signal = omni_signal or {}
    label = _text(row.get("performance_label")).lower()
    reward = _safe_float(row.get("normalized_reward") or row.get("reward_proxy"))
    confidence = _text(row.get("classification_confidence")).lower()
    unknown_count = sum(
        1
        for field in ["content_category", "hook_type", "slice_structure"]
        if _text(row.get(field)).lower() in {"", "unknown", "none", "null", "其他", "其它"}
    )
    low_confidence_penalty = 22.0 if confidence in {"", "low"} else 10.0 if confidence == "medium" else 0.0
    disagreement = min(100.0, unknown_count * 18.0 + len(needs) * 8.0 + low_confidence_penalty)
    if label == "high":
        disagreement += min(24.0, reward * 0.18)
    elif label == "low":
        disagreement += 12.0
    disagreement += _safe_float(omni_signal.get("disagreement_boost"))
    risk = 0.0
    if label == "low":
        risk = 64.0 + min(24.0, (100.0 - reward) * 0.24) + unknown_count * 4.0
    elif reward <= 25:
        risk = 38.0 + unknown_count * 6.0
    impact = min(100.0, reward * 0.55 + (30.0 if label == "high" else 0.0) + low_confidence_penalty)
    impact += _safe_float(omni_signal.get("impact_boost"))
    if label == "low" and risk >= max(impact, disagreement):
        queue_type = "risk"
        queue_reason = "low_interaction_risk"
        priority = risk
    elif omni_signal.get("queue_reason") and disagreement >= max(impact, risk):
        queue_type = "disagreement"
        queue_reason = _text(omni_signal.get("queue_reason"))
        priority = disagreement
    elif disagreement >= max(impact, risk):
        queue_type = "disagreement"
        queue_reason = "semantic_ranker_disagreement"
        priority = disagreement
    else:
        queue_type = "impact"
        queue_reason = "high_interaction_weak_label" if label == "high" else "high_impact_weak_label"
        priority = impact
    recommended = [
        item["field"]
        for item in needs
        if item.get("field") in {"content_category", "hook_type", "slice_structure", "artist_names"}
    ]
    if not recommended:
        recommended = ["content_category", "hook_type", "slice_structure"] if queue_type != "risk" else ["hook_type", "slice_structure"]
    recommended = _dedupe_fields([*recommended, *[field for field in omni_signal.get("recommended_fields", []) if field in MANUAL_LABEL_FIELDS]])
    baseline_score = _queue_semantic_proxy(row)
    ranker_score = max(0.0, min(100.0, baseline_score + (impact - risk) * 0.05 + (disagreement - 40.0) * 0.03))
    return {
        "queue_type": queue_type,
        "queue_reason": queue_reason,
        "priority_score": round(max(0.0, min(100.0, priority)), 2),
        "recommended_fields": recommended,
        "disagreement_score": round(max(0.0, min(100.0, disagreement)), 2),
        "risk_score": round(max(0.0, min(100.0, risk)), 2),
        "baseline_strategy_score": round(baseline_score, 4),
        "ranker_strategy_score": round(ranker_score, 4),
    }


def _dedupe_fields(fields: list[str]) -> list[str]:
    result = []
    seen = set()
    for field in fields:
        if field in seen:
            continue
        seen.add(field)
        result.append(field)
    return result


def _field_guides_for_list(fields: list[str]) -> list[dict[str, Any]]:
    guides = omni_annotation_field_guides(fields)
    return [guides[field] for field in fields if field in guides]


def _omni_cache_for_history_row(row: dict, cache_index: dict[str, dict]) -> dict:
    if not cache_index:
        return {}
    return cache_index.get(_text(row.get("id"))) or cache_index.get(_text(row.get("platform_item_id"))) or {}


def _omni_calibration_signal(row: dict, omni: dict) -> dict:
    if not isinstance(omni, dict) or not omni:
        return {"available": False, "recommended_fields": []}
    suggestions = omni.get("semantic_suggestions") if isinstance(omni.get("semantic_suggestions"), dict) else {}
    quality = omni.get("semantic_quality") if isinstance(omni.get("semantic_quality"), dict) else {}
    field_quality = quality.get("field_quality") if isinstance(quality.get("field_quality"), dict) else {}
    suggested_fields = {}
    recommended = []
    conflicts = []
    missing_rescues = []
    usable = []
    for field in ["content_category", "hook_type", "slice_structure"]:
        item = field_quality.get(field) if isinstance(field_quality.get(field), dict) else {}
        value = _text(item.get("normalized_value") or suggestions.get(field))
        if not value or value.lower() == "unknown":
            continue
        suggested_fields[field] = value
        if item.get("usable_for_ranker"):
            usable.append(field)
        current = _text(row.get(field))
        current_unknown = current.lower() in {"", "unknown", "none", "null", "其他", "其它"}
        if current_unknown:
            missing_rescues.append(field)
            recommended.append(field)
        elif current.lower() != value.lower():
            conflicts.append(field)
            recommended.append(field)
    queue_reason = ""
    if conflicts:
        queue_reason = "omni_shadow_semantic_conflict"
    elif missing_rescues:
        queue_reason = "omni_shadow_missing_field_rescue"
    disagreement_boost = len(conflicts) * 20.0 + len(missing_rescues) * 8.0
    impact_boost = len(usable) * 4.0
    return {
        "available": True,
        "normalization_version": quality.get("normalization_version") or omni.get("normalization_version") or "",
        "cache_path": omni.get("cache_path") or "",
        "suggested_fields": suggested_fields,
        "ranker_usable_fields": quality.get("ranker_usable_fields") if isinstance(quality.get("ranker_usable_fields"), list) else omni.get("ranker_usable_fields") or [],
        "recommended_fields": _dedupe_fields(recommended),
        "conflict_fields": conflicts,
        "missing_rescue_fields": missing_rescues,
        "queue_reason": queue_reason,
        "disagreement_boost": round(disagreement_boost, 4),
        "impact_boost": round(impact_boost, 4),
        "writes_labels": False,
        "production_weight": False,
    }


def _recently_saved_calibration_samples(rows: list[dict], *, label_filter: str = "", limit: int = 8) -> list[dict]:
    samples = []
    for row in rows:
        if _text(row.get("classification_confidence")) != "manual_verified":
            continue
        row_label = _text(row.get("performance_label")).lower()
        if label_filter and label_filter not in {"all", "any"} and row_label != label_filter:
            continue
        sample = _sample_row_contract(row)
        sample["manual_verified"] = True
        sample["reopen_reason"] = "manual_verified"
        samples.append(sample)
    samples.sort(
        key=lambda item: (
            _text(item.get("updated_at") or item.get("collected_at") or item.get("published_at")),
            _text(item.get("id")),
        ),
        reverse=True,
    )
    return samples[: max(0, int(limit or 0))]


def _queue_semantic_proxy(row: dict) -> float:
    reward = _safe_float(row.get("normalized_reward") or row.get("reward_proxy"))
    confidence = _text(row.get("classification_confidence")).lower()
    known_fields = sum(1 for field in ["content_category", "hook_type", "slice_structure"] if _text(row.get(field)).lower() not in {"", "unknown"})
    trust = {"manual_verified": 10.0, "high": 6.0, "medium": 3.0, "low": -3.0, "": -5.0}.get(confidence, 0.0)
    return round(max(0.0, min(100.0, reward * 0.78 + known_fields * 4.0 + trust)), 4)


def _calibration_batch_summary(items: list[dict], rows: list[dict]) -> dict:
    missing_counter: Counter[str] = Counter()
    accounts: Counter[str] = Counter()
    omni_available = 0
    omni_conflict = 0
    saved = 0
    for row in rows:
        if _text(row.get("classification_confidence")) == "manual_verified":
            saved += 1
    for item in items:
        accounts[_text(item.get("account_id")) or "unknown"] += 1
        omni = item.get("omni_shadow") if isinstance(item.get("omni_shadow"), dict) else {}
        if omni.get("available"):
            omni_available += 1
        if omni.get("conflict_fields"):
            omni_conflict += 1
        for field in item.get("recommended_fields") or []:
            missing_counter[field] += 1
    field_guides = omni_annotation_field_guides()
    return {
        "pending_count": len(items),
        "saved_count": saved,
        "top_missing_fields": [
            {
                "field": field,
                "label_zh": (field_guides.get(field) or {}).get("short_label_zh") or field,
                "description_zh": (field_guides.get(field) or {}).get("description_zh") or "",
                "count": count,
            }
            for field, count in missing_counter.most_common(6)
        ],
        "impact_accounts": [
            {"account_id": account, "count": count}
            for account, count in accounts.most_common(8)
        ],
        "omni_shadow_available_count": omni_available,
        "omni_shadow_conflict_count": omni_conflict,
    }


def _omni_replay_recommendations(queue: dict, v25_report: dict) -> list[str]:
    recs = ["Omni 校准回放只做研究验证，不写 manual_verified，不替代 v2.4 生产权重。"]
    summary = (v25_report.get("metrics") or {}).get("omni_shadow_summary") or {}
    eval_rate = _safe_float(summary.get("eval_cache_available_rate"))
    if eval_rate < 0.2:
        recs.append("Omni 验证集覆盖率低于 20%，优先补齐账号级样本后再判断泛化。")
    batch = queue.get("batch_summary") if isinstance(queue.get("batch_summary"), dict) else {}
    if _safe_float(batch.get("omni_shadow_conflict_count")) > 0:
        recs.append("优先人工校准 Omni shadow 冲突样本，再重跑 replay 比较校准前后。")
    gate = (v25_report.get("metrics") or {}).get("promotion_gate") or {}
    if gate.get("research_gate_passed"):
        recs.append("v2.5 shadow 已出现正向研究信号；扩大覆盖并做消融确认后再考虑权重迁移。")
    return recs


def _research_label_baseline_groups(rows: list[dict]) -> dict[str, dict[tuple[str, ...], list[float]]]:
    groups: dict[str, dict[tuple[str, ...], list[float]]] = {
        "account_age_duration": defaultdict(list),
        "account_age": defaultdict(list),
        "account": defaultdict(list),
        "global": defaultdict(list),
    }
    for row in rows:
        account = _text(row.get("account_id")) or "unknown"
        age_bucket = _research_age_bucket(row)
        duration_bucket = _duration_bucket(row.get("duration_seconds")) or "duration_unknown"
        reward = _safe_float(row.get("reward_proxy"))
        groups["account_age_duration"][(account, age_bucket, duration_bucket)].append(reward)
        groups["account_age"][(account, age_bucket)].append(reward)
        groups["account"][(account,)].append(reward)
        groups["global"][("all",)].append(reward)
    return groups


def _research_label_baseline(row: dict, groups: dict[str, dict[tuple[str, ...], list[float]]], *, min_count: int) -> dict:
    account = _text(row.get("account_id")) or "unknown"
    age_bucket = _research_age_bucket(row)
    duration_bucket = _duration_bucket(row.get("duration_seconds")) or "duration_unknown"
    choices = [
        ("account_age_duration", (account, age_bucket, duration_bucket)),
        ("account_age", (account, age_bucket)),
        ("account", (account,)),
        ("global", ("all",)),
    ]
    fallback: tuple[str, tuple[str, ...], list[float]] | None = None
    for scope, key in choices:
        values = list((groups.get(scope) or {}).get(key) or [])
        if values and fallback is None:
            fallback = (scope, key, values)
        if len(values) >= min_count:
            return _research_label_baseline_payload(scope, key, values, age_bucket, duration_bucket)
    if fallback:
        scope, key, values = fallback
        return _research_label_baseline_payload(scope, key, values, age_bucket, duration_bucket)
    return _research_label_baseline_payload("global", ("all",), [0.0], age_bucket, duration_bucket)


def _research_label_baseline_payload(
    scope: str,
    key: tuple[str, ...],
    values: list[float],
    age_bucket: str,
    duration_bucket: str,
) -> dict:
    clean = [float(value or 0.0) for value in values]
    return {
        "scope": scope,
        "key": "|".join(str(part) for part in key),
        "sample_count": len(clean),
        "median_reward": median(clean) if clean else 0.0,
        "mean_reward": sum(clean) / max(1, len(clean)),
        "age_bucket": age_bucket,
        "duration_bucket": duration_bucket,
    }


def _research_age_bucket(row: dict) -> str:
    published = _parse_iso_datetime(row.get("published_at"))
    collected = _parse_iso_datetime(row.get("collected_at")) or _parse_iso_datetime(row.get("updated_at"))
    if not published or not collected:
        return "age_unknown"
    days = max(0, int((collected - published).total_seconds() // 86400))
    if days <= 7:
        return "age_0_7d"
    if days <= 30:
        return "age_8_30d"
    if days <= 90:
        return "age_31_90d"
    if days <= 365:
        return "age_91_365d"
    return "age_366d_plus"


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_metric_int(*sources: tuple[str, dict, list[str]]) -> tuple[int, str]:
    for source_name, payload, keys in sources:
        value, field = _lookup_metric(payload, keys)
        if field:
            return _safe_int(value), f"{source_name}.{field}"
    return 0, ""


def _lookup_metric(payload: Any, keys: list[str]) -> tuple[Any, str]:
    if not isinstance(payload, dict):
        return None, ""
    containers: list[tuple[str, dict]] = [("", payload)]
    for nested_key in ["statistics", "metrics"]:
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            containers.append((nested_key, nested))
    for prefix, container in containers:
        for key in keys:
            value = container.get(key)
            if value is not None and _text(value) != "":
                return value, f"{prefix + '.' if prefix else ''}{key}"
    return None, ""


def _combined_metric_source(sources: Any) -> str:
    source_names = {
        _text(source).split(".", 1)[0]
        for source in sources
        if _text(source)
    }
    if not source_names:
        return "none"
    if source_names == {"raw_api"}:
        return "raw_api"
    if source_names == {"clean_json"}:
        return "clean_json"
    if "raw_api" in source_names and "clean_json" in source_names:
        return "mixed_raw_api_clean_json"
    return "|".join(sorted(source_names))


def _douyin_reward_proxy(
    *,
    views: int,
    likes: int,
    comments: int,
    favorites: int,
    shares: int,
    follows: int,
) -> float:
    score = math.log1p(max(views, 0))
    score += 0.35 * math.log1p(max(likes, 0))
    score += 1.6 * math.log1p(max(comments, 0))
    score += 2.0 * math.log1p(max(favorites, 0))
    score += 2.2 * math.log1p(max(shares, 0))
    score += 2.0 * math.log1p(max(follows, 0))
    return round(score, 6)


def _douyin_quality_contract(quality: Any) -> dict:
    data = quality if isinstance(quality, dict) else {}
    return {
        "quality_grade": _text(data.get("quality_grade")),
        "quality_score": _safe_float(data.get("quality_score")),
        "work_card_count_deduped": _safe_int(data.get("work_card_count_deduped")),
        "work_card_count_raw": _safe_int(data.get("work_card_count_raw")),
        "estimated_duplicate_ratio": _safe_float(data.get("estimated_duplicate_ratio")),
        "snapshot_count": _safe_int(data.get("snapshot_count")),
        "record_count": _safe_int(data.get("record_count")),
    }


def _add_baseline_group(groups: dict[tuple[str, str], list[dict]], dimension: str, name: Any, row: dict) -> None:
    label = _text(name)
    if not label or label.lower() in {"unknown", "none", "null"}:
        return
    groups[(dimension, label)].append(row)


def _summarize_baseline_group(dimension: str, name: str, rows: list[dict]) -> dict:
    rewards = [_safe_float(row.get("reward_proxy")) for row in rows]
    views = [_safe_int(row.get("views")) for row in rows]
    interactions = [
        _safe_int(row.get("likes"))
        + _safe_int(row.get("comments"))
        + _safe_int(row.get("favorites"))
        + _safe_int(row.get("shares"))
        for row in rows
    ]
    label_counts = Counter(row.get("performance_label") or "unlabeled" for row in rows)
    examples = sorted(
        rows,
        key=lambda row: (_safe_float(row.get("reward_proxy")), _safe_int(row.get("views"))),
        reverse=True,
    )[:3]
    return {
        "dimension": dimension,
        "name": name,
        "sample_count": len(rows),
        "avg_reward": round(sum(rewards) / len(rewards), 4) if rewards else 0,
        "median_reward": round(median(rewards), 4) if rewards else 0,
        "p75_reward": _percentile(rewards, 0.75),
        "avg_views": round(sum(views) / len(views), 2) if views else 0,
        "max_views": max(views) if views else 0,
        "avg_interactions": round(sum(interactions) / len(interactions), 2) if interactions else 0,
        "high_count": int(label_counts.get("high") or 0),
        "mid_count": int(label_counts.get("mid") or 0),
        "low_count": int(label_counts.get("low") or 0),
        "confidence": round(min(1.0, len(rows) / 20), 4),
        "examples": [
            {
                "platform_item_id": row.get("platform_item_id") or "",
                "title": row.get("title") or "",
                "views": _safe_int(row.get("views")),
                "reward_proxy": _safe_float(row.get("reward_proxy")),
                "performance_label": row.get("performance_label") or "",
            }
            for row in examples
        ],
    }


def _percentile(values: list[float], q: float) -> float:
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return 0
    if len(clean) == 1:
        return round(clean[0], 4)
    position = (len(clean) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(clean[int(position)], 4)
    lower_value = clean[lower]
    upper_value = clean[upper]
    return round(lower_value + (upper_value - lower_value) * (position - lower), 4)


def _duration_bucket(value: Any) -> str:
    seconds = _safe_float(value)
    if seconds <= 0:
        return ""
    if seconds <= 15:
        return "0-15s"
    if seconds <= 30:
        return "16-30s"
    if seconds <= 60:
        return "31-60s"
    if seconds <= 180:
        return "61-180s"
    return "180s+"


def _publish_hour_bucket(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{parsed.hour:02d}:00"


def _split_multi_value(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = [_text(item).strip("# ") for item in value]
    else:
        items = [item.strip("# ") for item in re.split(r"[|,，、;；\n]+", _text(value))]
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _join_values(value: Any) -> str:
    return "|".join(_split_multi_value(value))


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = _text(value).replace(",", "").strip()
    if not text:
        return 0
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100_000_000
        text = text[:-1]
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0
    return int(float(match.group(0)) * multiplier)


def _safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace(",", "").strip()
    if not text:
        return 0.0
    multiplier = 1.0
    if text.endswith("万"):
        multiplier = 10_000.0
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100_000_000.0
        text = text[:-1]
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) * multiplier if match else 0.0


def _duration_seconds(value: Any) -> float:
    duration = _safe_float(value)
    if duration > 1000:
        duration = duration / 1000.0
    return round(duration, 3)


def _timestamp_iso(value: Any) -> str:
    seconds = _safe_float(value)
    if seconds <= 0:
        return ""
    if seconds > 10_000_000_000:
        seconds = seconds / 1000.0
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _infer_douyin_run_id(path: Path) -> str:
    name = path.name
    if name.startswith("clean_"):
        return name[len("clean_") :]
    if name.startswith("raw_"):
        return name[len("raw_") :]
    return _export_prefix(name) or "default"


def _infer_account_from_path(path: Path) -> str:
    if path.name.startswith(("clean_", "raw_")) and path.parent.name:
        return _export_prefix(path.parent.name)
    return _export_prefix(path.name)


def _export_prefix(account_id: str | None) -> str:
    text = _text(account_id)
    if not text:
        return "all"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return slug or "all"


def _douyin_video_url(aweme_id: str) -> str:
    return f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""


def _douyin_insights_markdown(baselines: dict) -> str:
    lines = [
        "# 抖音历史样本洞察",
        "",
        f"- 账号: {baselines.get('account_id') or 'all'}",
        f"- 数据集: {baselines.get('dataset_id') or 'all'}",
        f"- 样本数: {baselines.get('sample_count') or 0}",
        f"- 标签分布: {json.dumps(baselines.get('label_counts') or {}, ensure_ascii=False)}",
        f"- 奖励中位数: {baselines.get('median_reward') or 0}",
        "",
        "## Top Signals",
        "",
    ]
    for item in (baselines.get("top_signals") or [])[:20]:
        lines.append(
            f"- {item.get('dimension')}={item.get('name')} | n={item.get('sample_count')} "
            f"| p75={item.get('p75_reward')} | high={item.get('high_count')} | confidence={item.get('confidence')}"
        )
    lines.append("")
    return "\n".join(lines)


def _db_row(account_id: str, dataset: dict, sample: dict, now: str) -> dict:
    sample_key = _sample_key(sample)
    raw = sample.get("raw") if isinstance(sample.get("raw"), dict) else {}
    return {
        "id": new_id("hcap"),
        "account_id": account_id,
        "dataset_id": dataset.get("id") or sample.get("dataset_id") or "default",
        "dataset_name": dataset.get("name") or sample.get("dataset_name") or "",
        "program_key": dataset.get("program_key") or "",
        "source_file": sample.get("source_file") or "",
        "source_kind": sample.get("source_kind") or "capture_xlsx",
        "platform": "douyin",
        "platform_item_id": sample.get("platform_item_id") or "",
        "sample_key": sample_key,
        "title": sample.get("title") or "",
        "platform_url": sample.get("platform_url") or "",
        "views": int(sample.get("views") or 0),
        "likes": int(sample.get("likes") or 0),
        "comments": int(sample.get("comments") or 0),
        "favorites": int(sample.get("favorites") or 0),
        "shares": int(sample.get("shares") or 0),
        "follows": int(sample.get("follows") or 0),
        "reward_proxy": float(sample.get("reward_proxy") or 0),
        "normalized_reward": float(sample.get("normalized_reward") or 0),
        "performance_label": sample.get("performance_label") or "",
        "label_rank": int(sample.get("label_rank") or 0),
        "label_percentile": float(sample.get("label_percentile") or 0),
        "label_reason": sample.get("label_reason") or "",
        "quality_grade": sample.get("quality_grade") or "",
        "quality_score": float(sample.get("quality_score") or 0),
        "source_run_id": sample.get("source_run_id") or "",
        "feature_version": sample.get("feature_version") or "",
        "semantic_feature_version": sample.get("semantic_feature_version") or "",
        "research_label_version": sample.get("research_label_version") or "",
        "duration_seconds": float(sample.get("duration_seconds") or 0),
        "media_type": sample.get("media_type") or "",
        "content_category": sample.get("content_category") or "",
        "hook_type": sample.get("hook_type") or "",
        "slice_structure": sample.get("slice_structure") or "",
        "structure_confidence": sample.get("structure_confidence") or "",
        "structure_evidence": sample.get("structure_evidence") or "",
        "structure_unknown_reason": sample.get("structure_unknown_reason") or "",
        "program_name": sample.get("program_name") or "",
        "artist_names": sample.get("artist_names") or "",
        "song_title": sample.get("song_title") or "",
        "original_sound_owner": sample.get("original_sound_owner") or "",
        "is_original_sound": int(sample.get("is_original_sound") or 0),
        "entity_signal": sample.get("entity_signal") or "",
        "tags": sample.get("tags") or "",
        "commercial_intent": sample.get("commercial_intent") or "",
        "rights_risk": sample.get("rights_risk") or "",
        "classification_confidence": sample.get("classification_confidence") or "",
        "semantic_unknown_reason": sample.get("semantic_unknown_reason") or "",
        "published_at": sample.get("published_at") or "",
        "collected_at": sample.get("collected_at") or "",
        "raw_json": json.dumps(raw, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }


def _existing_sample_row(conn, row: dict) -> dict[str, Any] | None:
    item_id = _text(row.get("platform_item_id"))
    if item_id:
        rows = fetch_all(
            conn,
            """
            SELECT *
            FROM historical_capture_samples
            WHERE account_id = ? AND platform = ? AND platform_item_id = ?
            """,
            [row["account_id"], row["platform"], item_id],
        )
        return _best_sample_row(rows)
    rows = fetch_all(
        conn,
        """
        SELECT *
        FROM historical_capture_samples
        WHERE account_id = ? AND sample_key = ?
        """,
        [row["account_id"], row["sample_key"]],
    )
    return _best_sample_row(rows)


def _prefer_sample_row(candidate: dict, existing: dict) -> bool:
    return _sample_preference_rank(candidate) > _sample_preference_rank(existing)


def _best_sample_row(rows: list[dict]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=_sample_preference_rank)


def _sample_preference_rank(row: dict) -> tuple:
    return (
        _metric_completeness_rank(row),
        _source_kind_rank(row.get("source_kind")),
        _dataset_date_rank(row.get("dataset_id")),
        _safe_int(row.get("views")),
        _safe_float(row.get("reward_proxy")),
        _text(row.get("updated_at")),
    )


def _metric_completeness_rank(row: dict) -> int:
    return sum(
        1
        for key in ["views", "likes", "comments", "favorites", "shares", "follows"]
        if _safe_float(row.get(key)) > 0
    )


def _source_kind_rank(value: Any) -> int:
    kind = _text(value)
    if kind == "douyin_clean_json":
        return 30
    if kind.startswith("douyin"):
        return 20
    if kind == "metric_db":
        return 10
    if kind.startswith("capture"):
        return 5
    return 0


def _dataset_date_rank(dataset_id: Any) -> int:
    text = _text(dataset_id)
    matches = re.findall(r"(20\d{6})", text)
    return int(matches[-1]) if matches else 0


def _update_sample_row(conn, row: dict) -> None:
    keys = [key for key in row.keys() if key not in {"id", "created_at"}]
    assignments = ", ".join(f"{key} = ?" for key in keys)
    conn.execute(
        f"UPDATE historical_capture_samples SET {assignments} WHERE id = ?",
        [row[key] for key in keys] + [row["id"]],
    )


def _sample_key(sample: dict) -> str:
    item_id = _text(sample.get("platform_item_id"))
    if item_id:
        return f"item:{item_id}"
    return f"title:{_stable_key(sample.get('title') or '')}"


def _sample_row_contract(row: dict) -> dict:
    raw = _json(row.get("raw_json"), {})
    metric_quality = raw.get("metric_quality") if isinstance(raw.get("metric_quality"), dict) else {}
    metric_policy = raw.get("metric_policy") if isinstance(raw.get("metric_policy"), dict) else {}
    return {
        "id": row.get("id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "",
        "dataset_name": row.get("dataset_name") or "",
        "program_key": row.get("program_key") or "",
        "source_file": row.get("source_file") or "",
        "source_kind": row.get("source_kind") or "",
        "platform": row.get("platform") or "douyin",
        "platform_item_id": row.get("platform_item_id") or "",
        "sample_key": row.get("sample_key") or "",
        "title": row.get("title") or "",
        "platform_url": row.get("platform_url") or "",
        "views": int(row.get("views") or 0),
        "likes": int(row.get("likes") or 0),
        "comments": int(row.get("comments") or 0),
        "favorites": int(row.get("favorites") or 0),
        "shares": int(row.get("shares") or 0),
        "follows": int(row.get("follows") or 0),
        "reward_proxy": float(row.get("reward_proxy") or 0),
        "normalized_reward": float(row.get("normalized_reward") or 0),
        "performance_label": row.get("performance_label") or "",
        "label_rank": int(row.get("label_rank") or 0),
        "label_percentile": float(row.get("label_percentile") or 0),
        "label_reason": row.get("label_reason") or "",
        "quality_grade": row.get("quality_grade") or "",
        "quality_score": float(row.get("quality_score") or 0),
        "source_run_id": row.get("source_run_id") or "",
        "feature_version": row.get("feature_version") or "",
        "semantic_feature_version": row.get("semantic_feature_version") or "",
        "research_label_version": row.get("research_label_version") or "",
        "duration_seconds": float(row.get("duration_seconds") or 0),
        "media_type": row.get("media_type") or "",
        "content_category": row.get("content_category") or "",
        "hook_type": row.get("hook_type") or "",
        "slice_structure": row.get("slice_structure") or "",
        "structure_confidence": row.get("structure_confidence") or "",
        "structure_evidence": row.get("structure_evidence") or "",
        "structure_unknown_reason": row.get("structure_unknown_reason") or "",
        "program_name": row.get("program_name") or "",
        "artist_names": row.get("artist_names") or "",
        "song_title": row.get("song_title") or "",
        "original_sound_owner": row.get("original_sound_owner") or "",
        "is_original_sound": bool(row.get("is_original_sound")),
        "entity_signal": row.get("entity_signal") or "",
        "tags": row.get("tags") or "",
        "commercial_intent": row.get("commercial_intent") or "",
        "rights_risk": row.get("rights_risk") or "",
        "classification_confidence": row.get("classification_confidence") or "",
        "semantic_unknown_reason": row.get("semantic_unknown_reason") or "",
        "published_at": row.get("published_at") or "",
        "collected_at": row.get("collected_at") or "",
        "play_count_missing": bool(metric_quality.get("play_count_missing") or metric_policy.get("play_count_missing")),
        "metric_source": metric_quality.get("metric_source") or metric_policy.get("metric_source") or "",
        "metric_window": metric_quality.get("metric_window") or metric_policy.get("metric_window") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _prototype_sample(row: dict) -> dict:
    return {
        "source_kind": row.get("source_kind") or "historical_capture",
        "account_id": row.get("account_id") or "main",
        "sample_id": row.get("sample_key") or row.get("id") or "",
        "platform_item_id": row.get("platform_item_id") or "",
        "title": row.get("title") or "",
        "platform_url": row.get("platform_url") or "",
        "published_at": row.get("published_at") or "",
        "collected_at": row.get("collected_at") or row.get("updated_at") or "",
        "views": int(row.get("views") or 0),
        "likes": int(row.get("likes") or 0),
        "comments": int(row.get("comments") or 0),
        "favorites": int(row.get("favorites") or 0),
        "shares": int(row.get("shares") or 0),
        "follows": int(row.get("follows") or 0),
        "reward_proxy": float(row.get("reward_proxy") or 0),
        "normalized_reward": float(row.get("normalized_reward") or 0),
        "performance_label": row.get("performance_label") or "",
        "label_rank": int(row.get("label_rank") or 0),
        "semantic_feature_version": row.get("semantic_feature_version") or "",
        "research_label_version": row.get("research_label_version") or "",
        "duration_seconds": float(row.get("duration_seconds") or 0),
        "hook_type": row.get("hook_type") or "",
        "slice_structure": row.get("slice_structure") or "",
        "structure_confidence": row.get("structure_confidence") or "",
        "structure_evidence": row.get("structure_evidence") or "",
        "structure_unknown_reason": row.get("structure_unknown_reason") or "",
        "content_category": row.get("content_category") or "",
        "program_name": row.get("program_name") or "",
        "artist_names": row.get("artist_names") or "",
        "song_title": row.get("song_title") or "",
        "original_sound_owner": row.get("original_sound_owner") or "",
        "is_original_sound": bool(row.get("is_original_sound")),
        "entity_signal": row.get("entity_signal") or "",
        "tags": row.get("tags") or "",
        "classification_confidence": row.get("classification_confidence") or "",
        "semantic_unknown_reason": row.get("semantic_unknown_reason") or "",
        "raw": _json(row.get("raw_json"), {}),
        "source_file": row.get("source_file") or "",
        "dataset_id": row.get("dataset_id") or "default",
        "dataset_name": row.get("dataset_name") or "",
    }
