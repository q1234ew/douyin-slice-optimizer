from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs

ACCOUNT_TIERS = {"A", "B", "C", "X"}

ACCOUNT_LIBRARY_FILENAME = "douyin_account_library_latest.json"
RAW_WORKS_FILENAME = "{account}_post_api_works.json"
CLEAN_WORKS_JSON_FILENAME = "douyin_account_works_clean_latest.json"
CLEAN_WORKS_CSV_FILENAME = "douyin_account_works_clean_latest.csv"
DEDUP_WORKS_JSON_FILENAME = "douyin_visible_works_dedup_latest.json"
QUALITY_FILENAME = "douyin_collection_quality_latest.json"

WORK_HEADERS = [
    "account_key",
    "account_nickname",
    "profile_url",
    "aweme_id",
    "video_url",
    "title",
    "tags",
    "published_at",
    "duration",
    "duration_seconds",
    "likes",
    "favorites",
    "comments",
    "shares",
    "play_count",
    "play_count_missing",
    "metric_quality_flags",
    "source_method",
    "observed_at",
    "author_nickname",
    "author_sec_uid",
    "author_user_id",
    "raw_index",
    "duplicate_count",
    "quality_flags",
]


@dataclass(frozen=True)
class AccountLibraryResult:
    output_path: Path
    accounts: list[dict[str, Any]]
    paths: dict[str, str]


@dataclass(frozen=True)
class AccountWorksResult:
    account_key: str
    account_dir: Path
    raw_dir: Path
    clean_dir: Path
    clean_works: list[dict[str, Any]]
    rejected_author_mismatch: list[dict[str, Any]]
    quality_report: dict[str, Any]
    paths: dict[str, str]


def build_account_library(
    accounts_input: str | Path | list[dict[str, Any]] | dict[str, Any],
    output_path: str | Path | None = None,
    *,
    observed_at: str | None = None,
    source_method: str = "manual_account_library",
) -> AccountLibraryResult:
    """Normalize a V1 account library without reaching Douyin or Chrome."""

    raw_accounts = _load_account_rows(accounts_input)
    normalized = [
        _normalize_account(item, observed_at=observed_at, source_method=source_method)
        for item in raw_accounts
        if isinstance(item, dict)
    ]
    normalized = _dedupe_accounts(normalized)

    if output_path:
        target = Path(output_path)
    else:
        settings = ensure_data_dirs()
        target = settings.data_dir / "douyin_capture" / ACCOUNT_LIBRARY_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, normalized)
    return AccountLibraryResult(target, normalized, {"account_library": str(target)})


def clean_account_api_works(
    *,
    account_library: str | Path | list[dict[str, Any]] | dict[str, Any],
    account_key: str,
    raw_works: str | Path | list[dict[str, Any]] | dict[str, Any],
    output_root: str | Path | None = None,
    run_id: str | None = None,
    rejected_author_mismatch: str | Path | list[dict[str, Any]] | dict[str, Any] | None = None,
    source_method: str = "appleevents_api_json",
    observed_at: str | None = None,
) -> AccountWorksResult:
    """Clean one account's raw API works into account-isolated V1 outputs."""

    account = _find_account(account_library, account_key)
    account_key = _text(account.get("account_key") or account_key)
    account_slug = _account_slug(account_key)
    run_id = _normalize_run_id(run_id)
    observed_at = _text(observed_at or account.get("observed_at")) or _now_iso()
    settings = ensure_data_dirs()
    account_dir = (Path(output_root) if output_root else settings.data_dir / "douyin_capture") / account_slug
    raw_dir = account_dir / f"raw_{run_id}"
    clean_dir = account_dir / f"clean_{run_id}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clean_dir.mkdir(parents=True, exist_ok=True)

    raw_payload = _load_jsonish(raw_works)
    raw_rows = _extract_work_items(_select_account_payload(raw_payload, account_key))
    external_rejected = _load_rejected_rows(rejected_author_mismatch)

    accepted_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    for raw_index, item in enumerate(raw_rows):
        accepted, reason = _author_matches_account(item, account)
        if not accepted:
            rejected_rows.append(
                {
                    "raw_index": raw_index,
                    "reject_reason": reason,
                    "aweme_id": _aweme_id(item),
                    "author_nickname": _author_field(item, "nickname"),
                    "author_sec_uid": _author_field(item, "sec_uid"),
                    "author_user_id": _author_field(item, "user_id"),
                    "raw": item,
                }
            )
            continue
        accepted_rows.append(
            _clean_work_row(
                item,
                account=account,
                raw_index=raw_index,
                source_method=source_method,
                observed_at=observed_at,
            )
        )

    clean_works = _dedupe_clean_rows(accepted_rows)
    all_rejected = rejected_rows + external_rejected
    quality_report = _quality_report(
        account=account,
        run_id=run_id,
        raw_rows=len(raw_rows),
        accepted_rows=len(accepted_rows),
        clean_works=clean_works,
        rejected_author_mismatch=all_rejected,
        external_author_mismatch_rows=len(external_rejected),
    )
    paths = _write_account_outputs(
        account_key=account_key,
        raw_dir=raw_dir,
        clean_dir=clean_dir,
        account_dir=account_dir,
        run_id=run_id,
        raw_payload=raw_payload,
        clean_works=clean_works,
        rejected_author_mismatch=all_rejected,
        quality_report=quality_report,
    )
    return AccountWorksResult(account_key, account_dir, raw_dir, clean_dir, clean_works, all_rejected, quality_report, paths)


def _normalize_account(raw: dict[str, Any], *, observed_at: str | None, source_method: str) -> dict[str, Any]:
    nested_account = raw.get("account") if isinstance(raw.get("account"), dict) else {}
    raw_account_name = raw.get("account") if not isinstance(raw.get("account"), dict) else ""
    source = {**nested_account, **raw}
    nickname = _text(source.get("nickname") or raw_account_name or source.get("account_nickname") or source.get("display_name"))
    sec_uid = _text(source.get("sec_uid") or source.get("secUid") or source.get("user_sec_uid"))
    user_id = _text(source.get("user_id") or source.get("uid") or source.get("author_uid") or source.get("platform_account_id"))
    unique_id = _text(source.get("unique_id") or source.get("douyin_id"))
    short_id = _text(source.get("short_id"))
    profile_url = _text(source.get("profile_url") or source.get("url"))
    if not profile_url and sec_uid:
        profile_url = f"https://www.douyin.com/user/{sec_uid}"
    account_key = _text(source.get("account_key") or source.get("key") or source.get("account_id"))
    if not account_key:
        account_key = _account_key_from_identity(nickname=nickname, sec_uid=sec_uid, user_id=user_id, profile_url=profile_url)

    tier_raw = _text(source.get("account_tier") or source.get("tier") or source.get("grade")).upper()
    flags = _flags(source.get("quality_flags"))
    if not tier_raw:
        tier = "X"
        flags.append("missing_account_tier_defaulted_x")
    elif tier_raw in ACCOUNT_TIERS:
        tier = tier_raw
    else:
        tier = "X"
        flags.append("invalid_account_tier_defaulted_x")

    aweme_count_value = source.get("aweme_count")
    if aweme_count_value is None:
        aweme_count_value = source.get("works")
    if aweme_count_value is None:
        aweme_count_value = source.get("total_works_visible")
    aweme_count = _safe_int(aweme_count_value)
    if aweme_count <= 0 and aweme_count_value in (None, ""):
        flags.append("missing_aweme_count")
    follower_count = _safe_int(source.get("follower_count") or source.get("followers") or source.get("followers_count"))
    if not nickname:
        flags.append("missing_nickname")
    if not profile_url:
        flags.append("missing_profile_url")
    if not any([sec_uid, user_id, profile_url]):
        flags.append("missing_stable_account_identity")

    return {
        "account_key": account_key,
        "account_slug": _account_slug(account_key),
        "account_tier": tier,
        "tier": tier,
        "profile_url": profile_url,
        "nickname": nickname,
        "sec_uid": sec_uid,
        "user_id": user_id,
        "unique_id": unique_id,
        "short_id": short_id,
        "follower_count": follower_count,
        "aweme_count": aweme_count,
        "account_type": _text(source.get("account_type") or source.get("type")),
        "program_key": _text(source.get("program_key")),
        "source_kind": _text(source.get("source_kind") or source.get("source") or "account_library"),
        "collection_priority": _safe_int(source.get("collection_priority") or source.get("priority")),
        "collection_depth_limit": _safe_int(source.get("collection_depth_limit") or source.get("depth_limit")),
        "v1_role": _text(source.get("v1_role") or source.get("role")),
        "signature": _text(source.get("signature")),
        "enterprise_verify_reason": _text(source.get("enterprise_verify_reason")),
        "verification_type": _safe_int(source.get("verification_type")),
        "source_method": _text(source.get("source_method") or source_method),
        "observed_at": _text(source.get("observed_at") or observed_at) or _now_iso(),
        "quality_flags": _unique(flags),
    }


def _dedupe_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for account in accounts:
        key = _text(account.get("account_key")) or _text(account.get("profile_url")) or _text(account.get("nickname"))
        if key not in by_key:
            by_key[key] = account
            continue
        current = by_key[key]
        merged = dict(current)
        for field in [
            "profile_url",
            "nickname",
            "sec_uid",
            "user_id",
            "unique_id",
            "short_id",
            "account_type",
            "program_key",
            "source_kind",
            "v1_role",
            "signature",
            "enterprise_verify_reason",
            "source_method",
            "observed_at",
        ]:
            if not merged.get(field) and account.get(field):
                merged[field] = account[field]
        for field in ["follower_count", "collection_priority", "collection_depth_limit", "verification_type"]:
            merged[field] = max(_safe_int(merged.get(field)), _safe_int(account.get(field)))
        merged["aweme_count"] = max(_safe_int(merged.get("aweme_count")), _safe_int(account.get("aweme_count")))
        merged["quality_flags"] = _unique([*_flags(merged.get("quality_flags")), *_flags(account.get("quality_flags")), "duplicate_account_key_merged"])
        by_key[key] = merged
    return list(by_key.values())


def _find_account(account_library: str | Path | list[dict[str, Any]] | dict[str, Any], account_key: str) -> dict[str, Any]:
    accounts = [_normalize_account(item, observed_at=None, source_method="input_account_library") for item in _load_account_rows(account_library)]
    wanted = _text(account_key)
    for account in accounts:
        identifiers = {
            _text(account.get("account_key")),
            _text(account.get("account_slug")),
            _text(account.get("nickname")),
            _text(account.get("sec_uid")),
            _text(account.get("user_id")),
        }
        if wanted in identifiers:
            return account
    known = ", ".join(_text(item.get("account_key")) for item in accounts[:10])
    raise ValueError(f"account_key not found in account library: {wanted}; known: {known}")


def _clean_work_row(
    raw: dict[str, Any],
    *,
    account: dict[str, Any],
    raw_index: int,
    source_method: str,
    observed_at: str,
) -> dict[str, Any]:
    aweme_id = _aweme_id(raw)
    title = _text(raw.get("title") or raw.get("desc") or raw.get("item_title"))
    tags = _extract_tags(raw.get("tags"), title)
    play_count, play_field = _metric(raw, ["play_count", "view_count"])
    likes, likes_field = _metric(raw, ["digg_count", "like_count", "likes"])
    favorites, favorites_field = _metric(raw, ["collect_count", "favorite_count", "favorites"])
    comments, comments_field = _metric(raw, ["comment_count", "comments"])
    shares, shares_field = _metric(raw, ["share_count", "shares"])
    play_count_missing = play_count <= 0
    metric_flags = []
    if play_count_missing:
        metric_flags.append("missing_play_count")
    for metric_name, field_name in [
        ("likes", likes_field),
        ("favorites", favorites_field),
        ("comments", comments_field),
        ("shares", shares_field),
    ]:
        if not field_name:
            metric_flags.append(f"missing_{metric_name}")
    quality_flags = []
    if not aweme_id:
        quality_flags.append("missing_aweme_id")
    if not title:
        quality_flags.append("missing_title")
    if not _author_field(raw, "sec_uid") and not _author_field(raw, "user_id") and not _author_field(raw, "nickname"):
        quality_flags.append("author_not_verified")

    return {
        "account_key": account.get("account_key") or "",
        "account_nickname": account.get("nickname") or "",
        "profile_url": account.get("profile_url") or "",
        "aweme_id": aweme_id,
        "video_url": _text(raw.get("video_url") or raw.get("share_url") or _douyin_video_url(aweme_id)),
        "title": title,
        "tags": tags,
        "published_at": _published_at(raw),
        "duration": raw.get("duration") if raw.get("duration") is not None else raw.get("duration_ms"),
        "duration_seconds": _duration_seconds(raw.get("duration") if raw.get("duration") is not None else raw.get("duration_ms")),
        "likes": likes,
        "favorites": favorites,
        "comments": comments,
        "shares": shares,
        "play_count": play_count if play_field else None,
        "play_count_missing": play_count_missing,
        "metric_quality_flags": metric_flags,
        "metric_sources": {
            "play_count": play_field,
            "likes": likes_field,
            "favorites": favorites_field,
            "comments": comments_field,
            "shares": shares_field,
        },
        "source_method": source_method,
        "observed_at": observed_at,
        "author_nickname": _author_field(raw, "nickname"),
        "author_sec_uid": _author_field(raw, "sec_uid"),
        "author_user_id": _author_field(raw, "user_id"),
        "music_title": _text(raw.get("music_title")),
        "media_type": _text(raw.get("media_type") or raw.get("aweme_type")),
        "raw_index": raw_index,
        "duplicate_count": 1,
        "quality_flags": quality_flags,
        "raw": raw,
    }


def _dedupe_clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _work_key(row)
        grouped.setdefault(key, []).append(row)

    clean: list[dict[str, Any]] = []
    for items in grouped.values():
        best = max(items, key=_metric_completeness)
        merged = dict(best)
        merged["duplicate_count"] = len(items)
        merged["source_raw_indices"] = [item["raw_index"] for item in items]
        flags = [flag for item in items for flag in _flags(item.get("quality_flags"))]
        if len(items) > 1:
            flags.append("deduped_duplicate_work")
        merged["quality_flags"] = _unique(flags)
        clean.append(merged)
    return sorted(clean, key=lambda item: (_safe_int(item.get("raw_index")), _text(item.get("aweme_id"))))


def _quality_report(
    *,
    account: dict[str, Any],
    run_id: str,
    raw_rows: int,
    accepted_rows: int,
    clean_works: list[dict[str, Any]],
    rejected_author_mismatch: list[dict[str, Any]],
    external_author_mismatch_rows: int,
) -> dict[str, Any]:
    dedup_rows = len(clean_works)
    duplicate_rows = max(0, accepted_rows - dedup_rows)
    duplicate_ratio = duplicate_rows / accepted_rows if accepted_rows else 0.0
    required_metrics = {
        metric: _coverage(clean_works, metric)
        for metric in ["likes", "favorites", "comments", "shares"]
    }
    play_missing_count = sum(1 for row in clean_works if row.get("play_count_missing"))
    play_count_missing_rate = play_missing_count / dedup_rows if dedup_rows else 0.0
    score = 100
    if raw_rows <= 0:
        score -= 80
    if rejected_author_mismatch:
        score -= min(30, 10 + len(rejected_author_mismatch) * 2)
    if duplicate_ratio > 0.5:
        score -= 25
    elif duplicate_ratio > 0.2:
        score -= 10
    missing_required_rates = [1 - item["rate"] for item in required_metrics.values()]
    score -= int(round(sum(missing_required_rates) * 10))
    if play_count_missing_rate >= 0.95 and dedup_rows:
        score -= 15
    elif play_count_missing_rate > 0.5:
        score -= 8
    score = max(0, score)
    return {
        "pipeline_version": "douyin_account_api_clean_v1",
        "run_id": run_id,
        "account_key": account.get("account_key") or "",
        "account_nickname": account.get("nickname") or "",
        "profile_url": account.get("profile_url") or "",
        "sec_uid": account.get("sec_uid") or "",
        "user_id": account.get("user_id") or "",
        "raw_rows": raw_rows,
        "accepted_rows": accepted_rows,
        "dedup_rows": dedup_rows,
        "author_mismatch_rejected": len(rejected_author_mismatch),
        "computed_author_mismatch_rejected": len(rejected_author_mismatch) - external_author_mismatch_rows,
        "external_author_mismatch_rejected": external_author_mismatch_rows,
        "duplicate_rows": duplicate_rows,
        "duplicate_ratio": round(duplicate_ratio, 4),
        "required_metric_coverage": required_metrics,
        "play_count_missing_count": play_missing_count,
        "play_count_missing_rate": round(play_count_missing_rate, 4),
        "quality_score": score,
        "quality_grade": _quality_grade(score),
        "recommendations": _recommendations(
            raw_rows=raw_rows,
            rejected=len(rejected_author_mismatch),
            duplicate_ratio=duplicate_ratio,
            required_metrics=required_metrics,
            play_count_missing_rate=play_count_missing_rate,
        ),
    }


def _write_account_outputs(
    *,
    account_key: str,
    raw_dir: Path,
    clean_dir: Path,
    account_dir: Path,
    run_id: str,
    raw_payload: Any,
    clean_works: list[dict[str, Any]],
    rejected_author_mismatch: list[dict[str, Any]],
    quality_report: dict[str, Any],
) -> dict[str, str]:
    raw_path = raw_dir / RAW_WORKS_FILENAME.format(account=_account_slug(account_key))
    clean_json = clean_dir / CLEAN_WORKS_JSON_FILENAME
    clean_csv = clean_dir / CLEAN_WORKS_CSV_FILENAME
    dedup_json = clean_dir / DEDUP_WORKS_JSON_FILENAME
    quality_clean = clean_dir / QUALITY_FILENAME
    quality_account = account_dir / f"quality_{run_id}.json"
    rejected_path = raw_dir / f"{_account_slug(account_key)}_post_api_rejected_author_mismatch.json"
    _write_json(raw_path, raw_payload)
    _write_json(clean_json, clean_works)
    _write_json(dedup_json, clean_works)
    _write_json(quality_clean, quality_report)
    _write_json(quality_account, quality_report)
    _write_json(rejected_path, rejected_author_mismatch)
    _write_csv(clean_csv, clean_works, WORK_HEADERS)
    return {
        "raw_works": str(raw_path),
        "clean_works_json": str(clean_json),
        "clean_works_csv": str(clean_csv),
        "dedup_works_json": str(dedup_json),
        "quality_report": str(quality_clean),
        "quality_report_account": str(quality_account),
        "rejected_author_mismatch": str(rejected_path),
    }


def _load_account_rows(payload: str | Path | list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    data = _load_jsonish(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ["accounts", "account_library", "rows", "items"]:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [
                    {"account_key": item_key, **item}
                    for item_key, item in value.items()
                    if isinstance(item_key, str) and isinstance(item, dict)
                ]
        if any(key in data for key in ["account_key", "key", "nickname", "account", "sec_uid", "user_id"]):
            return [data]
    raise ValueError("account library JSON must be a list or contain an accounts/account_library list")


def _load_jsonish(payload: str | Path | list[dict[str, Any]] | dict[str, Any] | Any) -> Any:
    if isinstance(payload, (str, Path)):
        path = Path(payload)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return payload


def _load_rejected_rows(payload: str | Path | list[dict[str, Any]] | dict[str, Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    data = _load_jsonish(payload)
    rows = _extract_work_items(data)
    if rows:
        return [{"external_rejected": True, **row} for row in rows]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ["rejected", "rows", "items"]:
            value = data.get(key)
            if isinstance(value, list):
                return [{"external_rejected": True, **item} for item in value if isinstance(item, dict)]
    return []


def _extract_work_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict) and _looks_like_work(item):
                rows.append(item)
            else:
                rows.extend(_extract_work_items(item))
        return rows
    if not isinstance(payload, dict):
        return []
    for key in ["aweme_list", "works", "rows", "items", "data", "list"]:
        value = payload.get(key)
        if value is not None:
            rows = _extract_work_items(value)
            if rows:
                return rows
    if _looks_like_work(payload):
        return [payload]
    return []


def _select_account_payload(payload: Any, account_key: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    candidates = [account_key, _account_slug(account_key)]
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, (list, dict)):
            return value
    return payload


def _looks_like_work(item: dict[str, Any]) -> bool:
    work_keys = {
        "aweme_id",
        "awemeId",
        "id",
        "desc",
        "title",
        "create_time",
        "digg_count",
        "comment_count",
        "share_count",
        "collect_count",
        "statistics",
        "video_url",
        "share_url",
    }
    return any(key in item for key in work_keys)


def _author_matches_account(raw: dict[str, Any], account: dict[str, Any]) -> tuple[bool, str]:
    expected_sec_uid = _text(account.get("sec_uid"))
    expected_user_id = _text(account.get("user_id"))
    expected_nickname = _text(account.get("nickname"))
    author_sec_uid = _author_field(raw, "sec_uid")
    author_user_id = _author_field(raw, "user_id")
    author_nickname = _author_field(raw, "nickname")
    if expected_sec_uid and author_sec_uid and expected_sec_uid != author_sec_uid:
        return False, "author_sec_uid_mismatch"
    if expected_user_id and author_user_id and expected_user_id != author_user_id:
        return False, "author_user_id_mismatch"
    if expected_nickname and author_nickname and _normalize_text(expected_nickname) != _normalize_text(author_nickname):
        return False, "author_nickname_mismatch"
    return True, ""


def _author_field(raw: dict[str, Any], field: str) -> str:
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    if field == "nickname":
        return _text(raw.get("author_nickname") or raw.get("nickname") or author.get("nickname") or author.get("name"))
    if field == "sec_uid":
        return _text(raw.get("author_sec_uid") or raw.get("sec_uid") or author.get("sec_uid") or author.get("secUid"))
    if field == "user_id":
        return _text(
            raw.get("author_uid")
            or raw.get("author_user_id")
            or raw.get("uid")
            or raw.get("user_id")
            or author.get("uid")
            or author.get("user_id")
        )
    return ""


def _metric(raw: dict[str, Any], keys: list[str]) -> tuple[int, str]:
    containers: list[tuple[str, dict[str, Any]]] = [("", raw)]
    for nested_key in ["statistics", "metrics"]:
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            containers.append((nested_key, nested))
    for prefix, container in containers:
        for key in keys:
            value = container.get(key)
            if value is not None and _text(value) != "":
                return _safe_int(value), f"{prefix + '.' if prefix else ''}{key}"
    return 0, ""


def _coverage(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    count = sum(1 for row in rows if row.get(metric) is not None and _safe_int(row.get(metric)) >= 0 and row["metric_sources"].get(metric))
    total = len(rows)
    return {"count": count, "total": total, "rate": round(count / total, 4) if total else 0.0}


def _metric_completeness(row: dict[str, Any]) -> tuple[int, int, int]:
    metric_count = sum(1 for field in ["likes", "favorites", "comments", "shares"] if row["metric_sources"].get(field))
    play_count_present = 0 if row.get("play_count_missing") else 1
    engagement_total = sum(_safe_int(row.get(field)) for field in ["likes", "favorites", "comments", "shares"])
    return metric_count, play_count_present, engagement_total


def _work_key(row: dict[str, Any]) -> str:
    aweme_id = _text(row.get("aweme_id"))
    if aweme_id:
        return f"aweme:{aweme_id}"
    video_url = _text(row.get("video_url"))
    if video_url:
        return f"url:{video_url}"
    raw = "|".join([_text(row.get("account_key")), _text(row.get("title")), _text(row.get("published_at"))])
    return "hash:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _aweme_id(raw: dict[str, Any]) -> str:
    value = _text(raw.get("aweme_id") or raw.get("awemeId") or raw.get("id"))
    if value:
        return value
    for field in ["video_url", "share_url"]:
        match = re.search(r"/video/(\d{8,})", _text(raw.get(field)))
        if match:
            return match.group(1)
    return ""


def _extract_tags(raw_tags: Any, title: str) -> list[str]:
    tags: list[str] = []
    if isinstance(raw_tags, list):
        tags.extend(_text(item) for item in raw_tags if _text(item))
    elif _text(raw_tags):
        tags.extend(part.strip() for part in re.split(r"[|,，\s]+", _text(raw_tags)) if part.strip())
    tags.extend(re.findall(r"#[^#\s]+", title))
    normalized = []
    for tag in tags:
        clean = _text(tag)
        if not clean:
            continue
        if not clean.startswith("#"):
            clean = f"#{clean}"
        normalized.append(clean)
    return _unique(normalized)


def _published_at(raw: dict[str, Any]) -> str:
    for field in ["published_at", "create_time", "createTime", "created_at"]:
        value = raw.get(field)
        if value is None or _text(value) == "":
            continue
        if field in {"published_at", "created_at"} and not str(value).isdigit():
            return _text(value)
        iso = _timestamp_iso(value)
        if iso:
            return iso
    return ""


def _duration_seconds(value: Any) -> float | None:
    if value is None or _text(value) == "":
        return None
    numeric = _safe_float(value)
    if numeric <= 0:
        return 0.0
    if numeric > 1000:
        return round(numeric / 1000, 3)
    return round(numeric, 3)


def _timestamp_iso(value: Any) -> str:
    numeric = _safe_int(value)
    if numeric <= 0:
        return ""
    if numeric > 10_000_000_000:
        numeric = int(numeric / 1000)
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _douyin_video_url(aweme_id: str) -> str:
    return f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""


def _account_key_from_identity(*, nickname: str, sec_uid: str, user_id: str, profile_url: str) -> str:
    seed = nickname or sec_uid or user_id or profile_url or "douyin_account"
    slug = _account_slug(seed)
    if slug != "douyin_account":
        return slug
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"douyin_account_{digest}"


def _account_slug(value: str) -> str:
    text = _text(value).strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "_", text).strip("._-")
    if slug:
        return slug[:80]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8] if text else "unknown"
    return f"account_{digest}"


def _normalize_run_id(run_id: str | None) -> str:
    if run_id:
        return re.sub(r"[^A-Za-z0-9T_.-]+", "_", run_id).strip("_")
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_appleevents_api")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _recommendations(
    *,
    raw_rows: int,
    rejected: int,
    duplicate_ratio: float,
    required_metrics: dict[str, dict[str, Any]],
    play_count_missing_rate: float,
) -> list[str]:
    notes = []
    if raw_rows <= 0:
        notes.append("未识别到 API 作品行；检查页面执行结果是否包含 aweme_list/works/rows。")
    if rejected:
        notes.append("存在作者不匹配作品，后续采集应继续按 sec_uid/user_id 校验账号边界。")
    if duplicate_ratio > 0.2:
        notes.append("作品重复率偏高；主流程应优先使用 dedup clean 输出。")
    if any(item["rate"] < 1.0 for item in required_metrics.values()):
        notes.append("点赞/收藏/评论/分享字段覆盖不完整；不要用可见计数或其他指标补齐缺失项。")
    if play_count_missing_rate > 0:
        notes.append("播放量缺失已显式标记；不得用点赞数、可见计数或互动数替代播放量。")
    return notes


def _quality_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "X"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in headers})


def _csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return "|".join(_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _flags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if _text(value):
        return [item for item in re.split(r"[|,，\s]+", _text(value)) if item]
    return []


def _unique(values: list[str] | Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = _text(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0
    number = float(match.group(0))
    if "亿" in text:
        number *= 100_000_000
    elif "万" in text:
        number *= 10_000
    return int(number)


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else 0.0


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).strip().lower()
