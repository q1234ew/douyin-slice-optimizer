from __future__ import annotations

import csv
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.feedback.reward import (
    baseline_stats,
    compute_reward_proxy,
    duration_bucket,
    feedback_signal_rates,
    infer_hook_type,
    normalize_against_baseline,
    parse_datetime,
    publish_hour,
    publish_time_bucket,
)
from dso.feedback.platform import map_platform_metric_row, platform_metric_contract, resolve_platform_mapping
from dso.spreadsheets import XLSX_SUFFIXES, read_table_rows
from dso.utils import new_id, utc_now
from dso.versions import FEEDBACK_INSIGHTS_VERSION, FEEDBACK_STATE_VERSION, METRICS_IMPORT_VERSION


METRIC_FIELDS = [
    "views",
    "impressions",
    "avg_watch_seconds",
    "avg_watch_ratio",
    "five_second_retention",
    "completion_rate",
    "rewatch_rate",
    "likes",
    "comments",
    "favorites",
    "shares",
    "follows",
    "negative_feedback",
    "comment_quality_score",
]

RATIO_FIELDS = {
    "avg_watch_ratio",
    "five_second_retention",
    "completion_rate",
    "rewatch_rate",
    "comment_quality_score",
}

def import_metrics(csv_path: str | Path, *, sample_source: str = "csv") -> dict:
    path = Path(csv_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() in XLSX_SUFFIXES:
        rows = read_table_rows(path, preferred_sheets=("指标", "metrics", "Metrics", "作品去重", "原始清洗记录"))
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    return import_metric_rows(rows, sample_source=sample_source, source_label=str(path))


def import_metric_rows(rows: Iterable[dict[str, Any]], *, sample_source: str = "api", source_label: str = "inline_rows") -> dict:
    normalized_rows = [map_platform_metric_row(dict(row), sample_source=str(row.get("sample_source") or sample_source or "api")) for row in rows]
    imported = 0
    snapshots = 0
    total_rows = 0
    linked_rows = 0
    unlinked_rows = 0
    row_issues: list[dict] = []
    with connect() as conn:
        for row_number, raw in enumerate(normalized_rows, start=2):
            total_rows += 1
            links = _resolve_links(conn, raw)
            if links["candidate_segment_id"]:
                linked_rows += 1
            else:
                unlinked_rows += 1
                row_issues.append(_link_issue(row_number, raw))
            rights_risk_score = _rights_risk_score(conn, links["candidate_segment_id"])
            metrics = _metric_values(raw, duration_seconds=_candidate_duration_seconds(conn, links["candidate_segment_id"]))
            reward_proxy, _components = compute_reward_proxy(metrics, rights_risk_score=rights_risk_score)
            collected_at = raw.get("collected_at") or utc_now()
            window_name = raw.get("window_name") or raw.get("label_window") or _infer_window_name(raw)
            row_source = (raw.get("sample_source") or sample_source or "csv").strip()
            platform_item_id = raw.get("platform_item_id") or ""
            row = {
                "id": new_id("metric"),
                "experiment_id": links["experiment_id"],
                "slice_variant_id": links["slice_variant_id"],
                "candidate_segment_id": links["candidate_segment_id"],
                "window_name": window_name,
                "collected_at": collected_at,
                "hours_since_publish": _num(raw.get("hours_since_publish"), integer=False),
                "created_at": utc_now(),
                "reward_proxy": reward_proxy,
                "normalized_reward": reward_proxy,
                "uncertainty": 1.0,
                "sample_source": row_source,
                "platform_item_id": platform_item_id,
            }
            row.update(metrics)
            insert_row(conn, "performance_metrics", row)
            snapshot = {
                "id": new_id("snap"),
                "performance_metric_id": row["id"],
                "experiment_id": links["experiment_id"],
                "slice_variant_id": links["slice_variant_id"],
                "candidate_segment_id": links["candidate_segment_id"],
                "window_name": window_name,
                "collected_at": collected_at,
                "hours_since_publish": row["hours_since_publish"],
                "created_at": utc_now(),
                "reward_proxy": reward_proxy,
                "normalized_reward": reward_proxy,
                "uncertainty": 1.0,
                "sample_source": row_source,
                "platform_item_id": platform_item_id,
            }
            snapshot.update(metrics)
            insert_row(conn, "metric_snapshots", snapshot)
            imported += 1
            snapshots += 1
        conn.commit()
    feedback = rebuild_feedback_state()
    row_summary = {
        "total_rows": total_rows,
        "imported_metrics": imported,
        "created_snapshots": snapshots,
        "linked_rows": linked_rows,
        "unlinked_rows": unlinked_rows,
        "skipped_rows": 0,
        "created_training_samples": feedback["training_samples"],
        "rebuilt_baselines": feedback["baselines"],
    }
    status = "import_completed"
    if total_rows <= 0 or imported <= 0:
        status = "import_failed"
    elif unlinked_rows:
        status = "import_completed_with_warnings"
    return {
        "contract_version": METRICS_IMPORT_VERSION,
        "status": status,
        "generated_at": utc_now(),
        "imported": imported,
        "snapshots": snapshots,
        "training_samples": feedback["training_samples"],
        "baselines": feedback["baselines"],
        "path": source_label,
        "row_summary": row_summary,
        "row_issues": row_issues,
        "training_eligibility": {
            "eligible_rows": linked_rows,
            "ineligible_rows": unlinked_rows,
            "policy": "Only linked metric_snapshots create training_samples.",
        },
        "feedback_state": feedback.get("feedback_state", {}),
        "input_contract": metrics_import_input_contract(),
    }


def _existing_id(conn, table: str, value: str | None) -> str | None:
    if not value:
        return None
    row = fetch_one(conn, f"SELECT id FROM {table} WHERE id = ?", [value])
    return value if row else None


def _resolve_links(conn, raw: dict) -> dict[str, str | None]:
    experiment_id = _existing_id(conn, "publishing_experiments", raw.get("experiment_id"))
    slice_variant_id = _existing_id(conn, "slice_variants", raw.get("slice_variant_id"))
    candidate_segment_id = _existing_id(conn, "candidate_segments", raw.get("candidate_segment_id"))
    platform_links = resolve_platform_mapping(conn, raw)
    experiment_id = experiment_id or platform_links["experiment_id"]
    slice_variant_id = slice_variant_id or platform_links["slice_variant_id"]
    candidate_segment_id = candidate_segment_id or platform_links["candidate_segment_id"]
    if experiment_id and not slice_variant_id:
        row = fetch_one(conn, "SELECT slice_variant_id FROM publishing_experiments WHERE id = ?", [experiment_id])
        slice_variant_id = row["slice_variant_id"] if row else None
    if slice_variant_id and not candidate_segment_id:
        row = fetch_one(conn, "SELECT candidate_segment_id FROM slice_variants WHERE id = ?", [slice_variant_id])
        candidate_segment_id = row["candidate_segment_id"] if row else None
    return {"experiment_id": experiment_id, "slice_variant_id": slice_variant_id, "candidate_segment_id": candidate_segment_id}


def _link_issue(row_number: int, raw: dict) -> dict:
    identifiers = {
        "candidate_segment_id": raw.get("candidate_segment_id") or "",
        "slice_variant_id": raw.get("slice_variant_id") or "",
        "experiment_id": raw.get("experiment_id") or "",
        "platform_item_id": raw.get("platform_item_id") or raw.get("item_id") or raw.get("video_id") or raw.get("aweme_id") or "",
    }
    provided = [key for key, value in identifiers.items() if value]
    if not provided:
        reason = "no candidate_segment_id, slice_variant_id, or experiment_id provided"
    else:
        reason = "linked candidate segment not found"
    return {
        "row_number": row_number,
        "link_status": "unlinked",
        "trust_status": "partial",
        "reason": reason,
        "identifiers": identifiers,
        "training_eligible": False,
        "action": "确认 CSV 中 candidate_segment_id，或先创建对应 slice variant / experiment 后再导入。",
    }


def metrics_import_input_contract() -> dict:
    return {
        "identifier_fields": ["candidate_segment_id", "slice_variant_id", "experiment_id", "platform_item_id"],
        "metric_fields": METRIC_FIELDS,
        "sample_sources": ["csv", "api", "mock"],
        "file_formats": ["csv", "xlsx"],
        "platform_contract": platform_metric_contract(),
        "ratio_fields": sorted(RATIO_FIELDS),
        "window_fields": ["window_name", "label_window", "hours_since_publish", "collected_at"],
        "ratio_format": "Use 0-1 decimals or percentage strings such as 82%.",
        "training_policy": "Only rows linked to an existing candidate segment create training_samples.",
    }


def _rights_risk_score(conn, candidate_segment_id: str | None) -> float:
    if not candidate_segment_id:
        return 0.0
    row = fetch_one(conn, "SELECT rights_risk_score FROM slice_scores WHERE candidate_segment_id = ?", [candidate_segment_id])
    return float(row["rights_risk_score"] or 0.0) if row else 0.0


def _candidate_duration_seconds(conn, candidate_segment_id: str | None) -> float | None:
    if not candidate_segment_id:
        return None
    row = fetch_one(conn, "SELECT duration_seconds FROM candidate_segments WHERE id = ?", [candidate_segment_id])
    return float(row["duration_seconds"] or 0.0) if row else None


def _metric_values(raw: dict, *, duration_seconds: float | None = None) -> dict:
    values = {}
    for field in METRIC_FIELDS:
        values[field] = _num(
            raw.get(field),
            integer=field in {"views", "impressions", "likes", "comments", "favorites", "shares", "follows", "negative_feedback"},
        )
        if field in RATIO_FIELDS:
            values[field] = _ratio_value(values[field])
    if not values["avg_watch_ratio"] and values["avg_watch_seconds"] and duration_seconds:
        values["avg_watch_ratio"] = round(float(values["avg_watch_seconds"]) / max(1.0, float(duration_seconds)), 4)
    return values


def _infer_window_name(raw: dict) -> str:
    hours = _num(raw.get("hours_since_publish"), integer=False)
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


def rebuild_feedback_state(account_id: str | None = None) -> dict:
    """Recompute account baselines, normalized rewards, and training samples."""
    with connect() as conn:
        baseline_count = _rebuild_account_baselines(conn, account_id)
        sample_count = _rebuild_training_samples(conn, account_id)
        conn.commit()
    return {
        "contract_version": FEEDBACK_STATE_VERSION,
        "status": "rebuilt",
        "account_id": account_id or "all",
        "generated_at": utc_now(),
        "baselines": baseline_count,
        "training_samples": sample_count,
        "feedback_state": {
            "rebuilt_baselines": baseline_count,
            "rebuilt_training_samples": sample_count,
        },
    }


def list_training_samples(account_id: str | None = None, limit: int = 50) -> list[dict]:
    query = """
        SELECT ts.*, c.music_slice_type, c.duration_seconds, v.account_id, ms.window_name, ms.views, ms.impressions
        FROM training_samples ts
        JOIN metric_snapshots ms ON ms.id = ts.metric_snapshot_id
        LEFT JOIN candidate_segments c ON c.id = ts.candidate_segment_id
        LEFT JOIN source_videos v ON v.id = c.source_video_id
    """
    params: list[object] = []
    if account_id:
        query += " WHERE v.account_id = ?"
        params.append(account_id)
    query += " ORDER BY ts.created_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return fetch_all(conn, query, params)


def account_baselines(account_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM account_baselines"
    params: list[object] = []
    if account_id:
        query += " WHERE account_id = ?"
        params.append(account_id)
    query += " ORDER BY account_id, content_type, duration_bucket, publish_hour, metric_name"
    with connect() as conn:
        return fetch_all(conn, query, params)


def account_insights(account_id: str | None = None) -> dict:
    query = """
        SELECT
            c.music_slice_type,
            c.duration_seconds,
            c.short_video_structure,
            c.musical_moment,
            c.comment_trigger,
            c.program_context,
            c.transcript,
            c.summary,
            e.published_at,
            m.*
        FROM performance_metrics m
        JOIN candidate_segments c ON c.id = m.candidate_segment_id
        LEFT JOIN source_videos v ON v.id = c.source_video_id
        LEFT JOIN publishing_experiments e ON e.id = m.experiment_id
        WHERE m.sample_source != 'mock'
    """
    params: list[str] = []
    if account_id:
        query += " AND v.account_id = ?"
        params.append(account_id)
    with connect() as conn:
        rows = fetch_all(conn, query, params)
    if not rows:
        return _empty_account_insights(account_id)

    by_type: dict[str, dict] = {}
    by_structure: dict[str, dict] = {}
    by_duration: dict[str, dict] = {}
    by_hook_type: dict[str, dict] = {}
    by_musical_moment: dict[str, dict] = {}
    by_publish_time: dict[str, dict] = {}
    for row in rows:
        _add_insight_row(by_type, row.get("music_slice_type") or "unknown", row)
        _add_insight_row(by_structure, row.get("short_video_structure") or "unknown", row)
        _add_insight_row(by_duration, duration_bucket(row.get("duration_seconds")), row)
        _add_insight_row(by_hook_type, infer_hook_type(row), row)
        _add_insight_row(by_musical_moment, row.get("musical_moment") or "unknown", row)
        _add_insight_row(by_publish_time, publish_time_bucket(row.get("published_at"), row.get("collected_at")), row)

    groups = {
        "by_slice_type": _finalize_insight_groups(by_type),
        "by_structure": _finalize_insight_groups(by_structure),
        "by_duration_bucket": _finalize_insight_groups(by_duration),
        "by_hook_type": _finalize_insight_groups(by_hook_type),
        "by_musical_moment": _finalize_insight_groups(by_musical_moment),
        "by_publish_time": _finalize_insight_groups(by_publish_time),
    }
    rankings = {
        "slice_type": _ranked_groups(groups["by_slice_type"]),
        "structure": _ranked_groups(groups["by_structure"]),
        "duration_bucket": _ranked_groups(groups["by_duration_bucket"]),
        "hook_type": _ranked_groups(groups["by_hook_type"]),
        "musical_moment": _ranked_groups(groups["by_musical_moment"]),
        "publish_time": _ranked_groups(groups["by_publish_time"]),
    }
    return {
        "contract_version": FEEDBACK_INSIGHTS_VERSION,
        "status": "ready",
        "generated_at": utc_now(),
        "account_id": account_id or "all",
        "query": {"account_id": account_id or "all"},
        "sample_count": len(rows),
        "metric_notes": _metric_notes(),
        "top_signals": {key: (values[0] if values else None) for key, values in rankings.items()},
        "rankings": rankings,
        **groups,
    }


def _empty_account_insights(account_id: str | None = None) -> dict:
    empty_rankings = {
        "slice_type": [],
        "structure": [],
        "duration_bucket": [],
        "hook_type": [],
        "musical_moment": [],
        "publish_time": [],
    }
    return {
        "contract_version": FEEDBACK_INSIGHTS_VERSION,
        "status": "empty",
        "generated_at": utc_now(),
        "account_id": account_id or "all",
        "query": {"account_id": account_id or "all"},
        "sample_count": 0,
        "message": "暂无表现数据",
        "metric_notes": _metric_notes(),
        "top_signals": {key: None for key in empty_rankings},
        "rankings": empty_rankings,
        "by_slice_type": {},
        "by_structure": {},
        "by_duration_bucket": {},
        "by_hook_type": {},
        "by_musical_moment": {},
        "by_publish_time": {},
    }


def _rebuild_account_baselines(conn, account_id: str | None = None) -> int:
    if account_id:
        conn.execute("DELETE FROM account_baselines WHERE account_id = ?", [account_id])
    else:
        conn.execute("DELETE FROM account_baselines")
    rows = _snapshot_rows(conn, account_id, include_mock=False)
    grouped: dict[tuple[str, str, str, int, str], list[float]] = {}
    for row in rows:
        key_base = _baseline_key(row)
        rates = feedback_signal_rates(row)
        grouped.setdefault((*key_base, "reward_proxy"), []).append(float(row["reward_proxy"] or 0))
        grouped.setdefault((*key_base, "avg_watch_ratio"), []).append(float(row["avg_watch_ratio"] or 0))
        grouped.setdefault((*key_base, "completion_rate"), []).append(float(row["completion_rate"] or 0))
        grouped.setdefault((*key_base, "five_second_retention"), []).append(float(row["five_second_retention"] or 0))
        grouped.setdefault((*key_base, "play_conversion_rate"), []).append(rates["play_conversion_rate"])
        grouped.setdefault((*key_base, "engagement_rate"), []).append(rates["engagement_rate"])
        grouped.setdefault((*key_base, "follow_rate"), []).append(rates["follow_rate"])
        grouped.setdefault((*key_base, "negative_feedback_rate"), []).append(rates["negative_feedback_rate"])
    for (acct, content_type, bucket, hour, metric_name), values in grouped.items():
        median_value, p75_value, p90_value, sample_count = baseline_stats(values)
        insert_row(
            conn,
            "account_baselines",
            {
                "id": new_id("base"),
                "account_id": acct,
                "content_type": content_type,
                "duration_bucket": bucket,
                "publish_hour": hour,
                "metric_name": metric_name,
                "median_value": median_value,
                "p75_value": p75_value,
                "p90_value": p90_value,
                "sample_count": sample_count,
                "updated_at": utc_now(),
            },
        )
    return len(grouped)


def _rebuild_training_samples(conn, account_id: str | None = None) -> int:
    if account_id:
        rows_to_delete = fetch_all(
            conn,
            """
            SELECT ts.id
            FROM training_samples ts
            JOIN metric_snapshots ms ON ms.id = ts.metric_snapshot_id
            JOIN candidate_segments c ON c.id = ms.candidate_segment_id
            JOIN source_videos v ON v.id = c.source_video_id
            WHERE v.account_id = ?
            """,
            [account_id],
        )
        conn.executemany("DELETE FROM training_samples WHERE id = ?", [(row["id"],) for row in rows_to_delete])
    else:
        conn.execute("DELETE FROM training_samples")
    rows = _snapshot_rows(conn, account_id)
    count = 0
    for row in rows:
        baseline = _baseline_for_snapshot(conn, row)
        normalized, uncertainty = normalize_against_baseline(
            float(row["reward_proxy"] or 0),
            median_value=float(baseline.get("median_value") or 0),
            p75_value=float(baseline.get("p75_value") or 0),
            sample_count=int(baseline.get("sample_count") or 0),
            impressions=int(row["impressions"] or 0),
        )
        conn.execute("UPDATE metric_snapshots SET normalized_reward = ?, uncertainty = ? WHERE id = ?", [normalized, uncertainty, row["id"]])
        conn.execute("UPDATE performance_metrics SET normalized_reward = ?, uncertainty = ? WHERE id = ?", [normalized, uncertainty, row["performance_metric_id"]])
        insert_row(
            conn,
            "training_samples",
            {
                "id": new_id("train"),
                "metric_snapshot_id": row["id"],
                "candidate_segment_id": row["candidate_segment_id"],
                "slice_variant_id": row["slice_variant_id"],
                "experiment_id": row["experiment_id"],
                "sample_source": row.get("sample_source") or "csv",
                "feature_version": "v1.rules",
                "label_window": row["window_name"] or "final",
                "reward_proxy": float(row["reward_proxy"] or 0),
                "normalized_reward": normalized,
                "account_baseline_snapshot": json.dumps(baseline, ensure_ascii=False),
                "rights_policy_status": _rights_policy_status(row),
                "train_split": "mock" if row.get("sample_source") == "mock" else _train_split(row["id"]),
                "created_at": utc_now(),
            },
        )
        count += 1
    return count


def _snapshot_rows(conn, account_id: str | None = None, *, include_mock: bool = True) -> list[dict]:
    query = """
        SELECT ms.*, c.music_slice_type, c.duration_seconds, v.account_id, e.published_at, s.rights_risk_score
        FROM metric_snapshots ms
        JOIN candidate_segments c ON c.id = ms.candidate_segment_id
        JOIN source_videos v ON v.id = c.source_video_id
        LEFT JOIN publishing_experiments e ON e.id = ms.experiment_id
        LEFT JOIN slice_scores s ON s.candidate_segment_id = c.id
    """
    params: list[object] = []
    clauses: list[str] = []
    if account_id:
        clauses.append("v.account_id = ?")
        params.append(account_id)
    if not include_mock:
        clauses.append("ms.sample_source != 'mock'")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    return fetch_all(conn, query, params)


def _baseline_key(row: dict) -> tuple[str, str, str, int]:
    return (
        row.get("account_id") or "unknown",
        row.get("music_slice_type") or "unknown",
        duration_bucket(row.get("duration_seconds")),
        _publish_hour_for_snapshot(row),
    )


def _baseline_for_snapshot(conn, row: dict) -> dict:
    acct, content_type, bucket, hour = _baseline_key(row)
    baseline = fetch_one(
        conn,
        """
        SELECT * FROM account_baselines
        WHERE account_id = ? AND content_type = ? AND duration_bucket = ? AND publish_hour = ? AND metric_name = 'reward_proxy'
        """,
        [acct, content_type, bucket, hour],
    )
    return baseline or {
        "account_id": acct,
        "content_type": content_type,
        "duration_bucket": bucket,
        "publish_hour": hour,
        "metric_name": "reward_proxy",
        "median_value": 0,
        "p75_value": 0,
        "p90_value": 0,
        "sample_count": 0,
    }


def _rights_policy_status(row: dict) -> str:
    if row.get("sample_source") == "mock":
        return "mock_not_for_production"
    risk = float(row.get("rights_risk_score") or 0)
    if risk >= 80:
        return "blocked"
    if risk >= 50:
        return "review"
    return "clear_or_unknown"


def _train_split(value: str) -> str:
    bucket = sum(ord(ch) for ch in value) % 10
    if bucket == 0:
        return "test"
    if bucket == 1:
        return "validation"
    return "train"


def _publish_hour_for_snapshot(row: dict) -> int:
    direct = publish_hour(row.get("published_at"))
    if direct >= 0:
        return direct
    collected = parse_datetime(row.get("collected_at"))
    if not collected:
        return -1
    try:
        hours = float(row.get("hours_since_publish") or 0)
    except (TypeError, ValueError):
        hours = 0
    if hours > 0:
        return (collected - timedelta(hours=hours)).hour
    return collected.hour


def _num(value: str | None, *, integer: bool) -> int | float:
    if value in (None, ""):
        return 0 if integer else 0.0
    text = str(value).strip().replace(",", "")
    multiplier = 1.0
    if text.endswith("万"):
        multiplier = 10000.0
        text = text[:-1].strip()
    elif text.endswith("亿"):
        multiplier = 100000000.0
        text = text[:-1].strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        number = float(text) * multiplier
        return int(number) if integer else number
    except ValueError:
        return 0 if integer else 0.0


def _add_insight_row(groups: dict[str, dict], key: str, row: dict) -> None:
    bucket = groups.setdefault(
        key,
        {
            "count": 0,
            "views": 0,
            "impressions": 0,
            "interactions": 0,
            "follows": 0,
            "negative_feedback": 0,
            "_avg_watch_ratio": 0.0,
            "_five_second_retention": 0.0,
            "_completion_rate": 0.0,
            "_reward_proxy": 0.0,
        },
    )
    bucket["count"] += 1
    bucket["views"] += int(row.get("views") or 0)
    bucket["impressions"] += int(row.get("impressions") or 0)
    bucket["interactions"] += sum(int(row.get(field) or 0) for field in ("likes", "comments", "favorites", "shares"))
    bucket["follows"] += int(row.get("follows") or 0)
    bucket["negative_feedback"] += int(row.get("negative_feedback") or 0)
    bucket["_avg_watch_ratio"] += _ratio_value(row.get("avg_watch_ratio"))
    bucket["_five_second_retention"] += _ratio_value(row.get("five_second_retention"))
    bucket["_completion_rate"] += _ratio_value(row.get("completion_rate"))
    bucket["_reward_proxy"] += float(row.get("reward_proxy") or 0)


def _finalize_insight_groups(groups: dict[str, dict]) -> dict[str, dict]:
    finalized = {}
    for key, bucket in groups.items():
        count = max(1, int(bucket["count"]))
        views = int(bucket["views"] or 0)
        impressions = int(bucket["impressions"] or 0)
        finalized[key] = {
            "count": bucket["count"],
            "views": views,
            "impressions": impressions,
            "play_conversion_rate": round((views / impressions) if impressions > 0 else 0.0, 4),
            "avg_watch_ratio": round(bucket["_avg_watch_ratio"] / count, 4),
            "five_second_retention": round(bucket["_five_second_retention"] / count, 4),
            "completion_rate": round(bucket["_completion_rate"] / count, 4),
            "engagement_rate": round(bucket["interactions"] / max(1, views), 4),
            "follow_rate": round(bucket["follows"] / max(1, views), 4),
            "negative_feedback_rate": round(bucket["negative_feedback"] / max(1, views), 4),
            "reward_proxy": round(bucket["_reward_proxy"] / count, 4),
        }
    return finalized


def _ranked_groups(groups: dict[str, dict], *, limit: int = 5) -> list[dict]:
    rows = [{"name": key, **value} for key, value in groups.items()]
    rows.sort(key=lambda row: (row["reward_proxy"], row["count"], row["views"]), reverse=True)
    return rows[:limit]


def _ratio_value(value: object) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if number > 1:
        number = number / 100.0
    return round(max(0.0, min(1.0, number)), 4)


def _metric_notes() -> dict:
    return {
        "play_conversion_rate": "views / impressions",
        "five_second_retention": "five_second_retention; percentage CSV values are accepted",
        "completion_rate": "completion_rate; percentage CSV values are accepted",
        "avg_watch_ratio": "avg_watch_ratio, or avg_watch_seconds / candidate duration when the ratio is missing",
        "engagement_rate": "(likes + comments + favorites + shares) / views",
        "follow_rate": "follows / views",
        "negative_feedback_rate": "negative_feedback / views",
        "reward_proxy": "weighted watch, conversion, engagement and follow signals minus negative feedback and rights risk",
        "hook_type": "candidate_segments has no hook_type column yet; inferred from short_video_structure first, then comment/summary/context/transcript text",
    }
