from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dso.collectors.douyin_classification import classify_published_work
from dso.config import ensure_data_dirs

COUNT_RE = re.compile(r"(?P<count>\d+(?:\.\d+)?(?:万|亿)?)")
PREFIX_COUNT_RE = re.compile(r"^(?:共创|热点)\s+(?P<count>\d+(?:\.\d+)?(?:万|亿)?)\s+(?P<title>.+)$")
LEADING_COUNT_RE = re.compile(r"^(?P<count>\d+(?:\.\d+)?(?:万|亿)?)\s+(?P<title>.+)$")
TAG_RE = re.compile(r"#[^#\s]+")
AWEME_ID_RE = re.compile(r"\d{10,}")
KNOWN_ARTISTS = [
    "万妮达",
    "约翰·传奇",
    "John Legend",
    "艾略特",
    "Elliot James Reay",
    "侯明昊",
    "刘惜君",
    "张远",
    "窦靖童",
    "尤长靖",
    "胡彦斌",
    "齐豫",
]

SNAPSHOT_GLOBS = ("douyin_follow_visible_*.json", "douyin_profile_visible_*.json")
SNAPSHOT_EXCLUDE_NAMES = {"douyin_follow_visible_latest.json", "douyin_follow_visible_series_latest.json"}

CURRENT_VIDEO_HEADERS = [
    "source_file",
    "platform",
    "source_method",
    "observed_at",
    "account_nickname",
    "account_key",
    "profile_url",
    "followers_visible",
    "likes_received_visible",
    "account_total_works_visible",
    "account_type",
    "content_domain",
    "following_state_visible",
    "current_aweme_id",
    "raw_aweme_ids_visible",
    "current_tags",
    "related_searches",
    "visible_metric_numbers_unlabeled",
    "page_url",
    "quality_flags",
]

CLEAN_RECORD_HEADERS = [
    "source_file",
    "record_type",
    "platform",
    "source_method",
    "observed_at",
    "account_nickname",
    "account_key",
    "profile_url",
    "followers_visible",
    "likes_received_visible",
    "account_total_works_visible",
    "account_type",
    "content_domain",
    "following_state_visible",
    "current_aweme_id",
    "aweme_id",
    "video_url",
    "is_pinned_visible",
    "visible_count",
    "visible_count_number",
    "visible_count_unit",
    "title_tags_text",
    "normalized_title",
    "tags",
    "content_category",
    "program_name",
    "artist_names",
    "song_title",
    "hook_type",
    "slice_structure",
    "commercial_intent",
    "rights_risk",
    "classification_confidence",
    "page_url",
    "quality_flags",
]

DEDUP_WORK_HEADERS = [
    "work_key",
    "account_nickname",
    "account_key",
    "profile_url",
    "aweme_id",
    "video_url",
    "is_pinned_visible",
    "normalized_title",
    "tags",
    "content_category",
    "program_name",
    "artist_names",
    "song_title",
    "hook_type",
    "slice_structure",
    "commercial_intent",
    "rights_risk",
    "classification_confidence",
    "best_visible_count",
    "best_visible_count_number",
    "best_visible_count_unit",
    "all_visible_counts",
    "snapshot_count",
    "source_files",
    "first_observed_at",
    "last_observed_at",
    "quality_flags",
]


@dataclass(frozen=True)
class CleanResult:
    output_dir: Path
    clean_records: list[dict[str, Any]]
    dedup_works: list[dict[str, Any]]
    current_videos: list[dict[str, Any]]
    quality_report: dict[str, Any]
    paths: dict[str, str]


def clean_visible_snapshots(input_dir: str | Path | None = None, output_dir: str | Path | None = None) -> CleanResult:
    settings = ensure_data_dirs()
    source_dir = Path(input_dir) if input_dir else settings.data_dir / "douyin_capture"
    target_dir = Path(output_dir) if output_dir else source_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    snapshots = _load_snapshots(source_dir)
    clean_records: list[dict[str, Any]] = []
    current_videos: list[dict[str, Any]] = []
    for source_file, snapshot in snapshots:
        current = _current_video_row(source_file, snapshot)
        current_videos.append(current)
        clean_records.append(_current_video_clean_record(current))
        clean_records.extend(_work_card_rows(source_file, snapshot, current.get("current_aweme_id") or ""))

    dedup_works = _dedupe_work_rows([row for row in clean_records if row.get("record_type") == "visible_work_card"])
    quality_report = _quality_report(
        snapshots=snapshots,
        clean_records=clean_records,
        dedup_works=dedup_works,
        current_videos=current_videos,
    )
    paths = _write_outputs(target_dir, clean_records, dedup_works, current_videos, quality_report)
    return CleanResult(target_dir, clean_records, dedup_works, current_videos, quality_report, paths)


def _load_snapshots(input_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    if not input_dir.exists():
        raise FileNotFoundError(f"douyin capture input dir not found: {input_dir}")
    snapshots: list[tuple[str, dict[str, Any]]] = []
    for glob in SNAPSHOT_GLOBS:
        for path in sorted(input_dir.glob(glob)):
            if path.name in SNAPSHOT_EXCLUDE_NAMES:
                continue
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and "visible_works" in payload:
                snapshots.append((path.name, payload))
    if not snapshots:
        raise FileNotFoundError(f"no visible douyin snapshots found in {input_dir}")
    return snapshots


def _current_video_row(source_file: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    account = snapshot.get("account") or {}
    page = snapshot.get("page") or {}
    current = snapshot.get("current_video") or {}
    account_context = _account_context(account)
    raw_ids = [str(item) for item in current.get("aweme_ids_visible") or [] if str(item).strip()]
    hashtag_links = [item for item in current.get("hashtag_links") or [] if isinstance(item, dict)]
    link_ids = [_aweme_id_from_href(item.get("href") or "") for item in hashtag_links]
    link_ids = [item for item in link_ids if item]
    current_aweme_id = _select_current_aweme_id(raw_ids, link_ids)
    current_tags = [_clean_text(item.get("text") or "") for item in hashtag_links if _clean_text(item.get("text") or "").startswith("#")]
    related_searches = [_clean_text(item.get("text") or "") for item in hashtag_links if not _clean_text(item.get("text") or "").startswith("#")]
    flags = []
    if len(set(raw_ids)) > 1:
        flags.append("multiple_raw_aweme_ids")
    if raw_ids and current_aweme_id not in raw_ids:
        flags.append("current_aweme_inferred_from_link")
    if related_searches:
        flags.append("has_related_search_anchor")
    return {
        "source_file": source_file,
        "platform": "douyin",
        "source_method": "browser_visible",
        "observed_at": snapshot.get("observed_at") or "",
        "account_nickname": _clean_text(account.get("nickname") or ""),
        "account_key": account_context["account_key"],
        "profile_url": _clean_text(account.get("profile_url") or ""),
        "followers_visible": _clean_text(account.get("followers_visible") or ""),
        "likes_received_visible": _clean_text(account.get("likes_received_visible") or ""),
        "account_total_works_visible": account_context["account_total_works_visible"],
        "account_type": account_context["account_type"],
        "content_domain": account_context["content_domain"],
        "following_state_visible": account_context["following_state_visible"],
        "current_aweme_id": current_aweme_id,
        "raw_aweme_ids_visible": "|".join(raw_ids),
        "current_tags": "|".join(_unique(current_tags)),
        "related_searches": "|".join(_unique(related_searches)),
        "visible_metric_numbers_unlabeled": "|".join(_clean_text(item) for item in current.get("visible_metric_numbers_unlabeled") or []),
        "page_url": _clean_text(page.get("url") or ""),
        "quality_flags": "|".join(flags),
    }


def _current_video_clean_record(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_file": current.get("source_file", ""),
        "record_type": "current_video",
        "platform": current.get("platform", "douyin"),
        "source_method": current.get("source_method", "browser_visible"),
        "observed_at": current.get("observed_at", ""),
        "account_nickname": current.get("account_nickname", ""),
        "account_key": current.get("account_key", ""),
        "profile_url": current.get("profile_url", ""),
        "followers_visible": current.get("followers_visible", ""),
        "likes_received_visible": current.get("likes_received_visible", ""),
        "account_total_works_visible": current.get("account_total_works_visible", ""),
        "account_type": current.get("account_type", ""),
        "content_domain": current.get("content_domain", ""),
        "following_state_visible": current.get("following_state_visible", ""),
        "current_aweme_id": current.get("current_aweme_id", ""),
        "aweme_id": current.get("current_aweme_id", ""),
        "video_url": "",
        "is_pinned_visible": "",
        "visible_count": current.get("visible_metric_numbers_unlabeled", ""),
        "visible_count_number": "",
        "visible_count_unit": "",
        "title_tags_text": current.get("current_tags", ""),
        "normalized_title": current.get("current_tags", ""),
        "tags": current.get("current_tags", ""),
        "content_category": "unknown",
        "program_name": "",
        "artist_names": "",
        "song_title": "",
        "hook_type": "unknown",
        "slice_structure": "unknown",
        "commercial_intent": "unknown",
        "rights_risk": "unknown",
        "classification_confidence": "low",
        "page_url": current.get("page_url", ""),
        "quality_flags": current.get("quality_flags", ""),
    }


def _work_card_rows(source_file: str, snapshot: dict[str, Any], current_aweme_id: str) -> list[dict[str, Any]]:
    account = snapshot.get("account") or {}
    page = snapshot.get("page") or {}
    account_context = _account_context(account)
    rows = []
    for work in snapshot.get("visible_works") or []:
        if not isinstance(work, dict):
            continue
        original_title = str(work.get("title_tags_text") or "")
        raw_title = _clean_text(original_title)
        is_pinned = raw_title.startswith("置顶 ")
        if is_pinned:
            raw_title = re.sub(r"^置顶\s+", "", raw_title).strip()
        visible_count, normalized_title, flags = _recover_visible_count(work.get("visible_count"), raw_title)
        normalized_title, repeated = _collapse_repeated_text(normalized_title)
        if repeated:
            flags.append("repeated_title_collapsed")
        if is_pinned:
            flags.append("pinned_visible")
        if html.unescape(original_title) != original_title:
            flags.append("html_unescaped")
        count_number, count_unit = _parse_count(visible_count)
        tags = _extract_tags(work.get("tags"), normalized_title)
        video_url = _normalize_video_url(work.get("href") or work.get("video_url") or "")
        aweme_id = _aweme_id_from_video_url(video_url) or _clean_text(work.get("aweme_id") or "")
        classification = _classify_work(normalized_title, tags, aweme_id, visible_count)
        rows.append(
            {
                "source_file": source_file,
                "record_type": "visible_work_card",
                "platform": "douyin",
                "source_method": "browser_visible",
                "observed_at": snapshot.get("observed_at") or "",
                "account_nickname": _clean_text(account.get("nickname") or ""),
                "account_key": account_context["account_key"],
                "profile_url": _clean_text(account.get("profile_url") or ""),
                "followers_visible": _clean_text(account.get("followers_visible") or ""),
                "likes_received_visible": _clean_text(account.get("likes_received_visible") or ""),
                "account_total_works_visible": account_context["account_total_works_visible"],
                "account_type": account_context["account_type"],
                "content_domain": account_context["content_domain"],
                "following_state_visible": account_context["following_state_visible"],
                "current_aweme_id": current_aweme_id,
                "aweme_id": aweme_id,
                "video_url": video_url,
                "is_pinned_visible": bool(is_pinned),
                "visible_count": visible_count,
                "visible_count_number": count_number,
                "visible_count_unit": count_unit,
                "title_tags_text": raw_title,
                "normalized_title": normalized_title,
                "tags": "|".join(tags),
                **classification,
                "page_url": _clean_text(page.get("url") or ""),
                "quality_flags": "|".join(flags),
            }
        )
    return rows


def _dedupe_work_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _work_key(
            row.get("profile_url") or row.get("account_nickname") or "",
            row.get("normalized_title") or row.get("title_tags_text") or "",
            row.get("aweme_id") or "",
        )
        groups[key].append(row)

    deduped = []
    for key, items in groups.items():
        items = sorted(items, key=lambda item: (item.get("observed_at") or "", item.get("source_file") or ""))
        counts = _unique([item.get("visible_count") or "" for item in items])
        best_count = _best_count(counts)
        best_number, best_unit = _parse_count(best_count)
        tags = _unique(tag for item in items for tag in str(item.get("tags") or "").split("|") if tag)
        flags = set()
        for item in items:
            flags.update(flag for flag in str(item.get("quality_flags") or "").split("|") if flag)
        if len(items) > 1:
            flags.add("deduped_across_snapshots")
        if len(counts) > 1:
            flags.add("same_title_multiple_visible_counts")
        deduped.append(
            {
                "work_key": key,
                "account_nickname": items[0].get("account_nickname", ""),
                "account_key": items[0].get("account_key", ""),
                "profile_url": items[0].get("profile_url", ""),
                "aweme_id": items[0].get("aweme_id", ""),
                "video_url": items[0].get("video_url", ""),
                "is_pinned_visible": any(bool(item.get("is_pinned_visible")) for item in items),
                "normalized_title": items[0].get("normalized_title", ""),
                "tags": "|".join(tags),
                "content_category": _most_common([item.get("content_category") or "" for item in items]),
                "program_name": _most_common([item.get("program_name") or "" for item in items]),
                "artist_names": _merge_pipe_values([item.get("artist_names") or "" for item in items]),
                "song_title": _most_common([item.get("song_title") or "" for item in items]),
                "hook_type": _most_common([item.get("hook_type") or "" for item in items]),
                "slice_structure": _most_common([item.get("slice_structure") or "" for item in items]),
                "commercial_intent": _most_common([item.get("commercial_intent") or "" for item in items]),
                "rights_risk": _most_common([item.get("rights_risk") or "" for item in items]),
                "classification_confidence": _best_confidence([item.get("classification_confidence") or "" for item in items]),
                "best_visible_count": best_count,
                "best_visible_count_number": best_number,
                "best_visible_count_unit": best_unit,
                "all_visible_counts": "|".join(counts),
                "snapshot_count": len(_unique(item.get("source_file") or "" for item in items)),
                "source_files": "|".join(_unique(item.get("source_file") or "" for item in items)),
                "first_observed_at": items[0].get("observed_at", ""),
                "last_observed_at": items[-1].get("observed_at", ""),
                "quality_flags": "|".join(sorted(flags)),
            }
        )
    return sorted(deduped, key=lambda item: (-int(item.get("snapshot_count") or 0), item.get("normalized_title") or ""))


def _quality_report(
    snapshots: list[tuple[str, dict[str, Any]]],
    clean_records: list[dict[str, Any]],
    dedup_works: list[dict[str, Any]],
    current_videos: list[dict[str, Any]],
) -> dict[str, Any]:
    work_rows = [row for row in clean_records if row.get("record_type") == "visible_work_card"]
    accounts = _unique(row.get("account_nickname") or "" for row in clean_records)
    profile_snapshot_count = sum(
        1
        for _, payload in snapshots
        if str(((payload.get("page") or {}).get("source") or "")).strip() == "douyin_profile_visible_dom"
    )
    collection_mode = "targeted_profile" if profile_snapshot_count and len(accounts) == 1 else "follow_feed_sample"
    blank_required = {
        field: sum(1 for row in clean_records if not str(row.get(field) or "").strip())
        for field in ["source_file", "record_type", "observed_at", "account_nickname", "profile_url", "page_url"]
    }
    flags = Counter(flag for row in clean_records for flag in str(row.get("quality_flags") or "").split("|") if flag)
    current_flags = Counter(flag for row in current_videos for flag in str(row.get("quality_flags") or "").split("|") if flag)
    duplicate_rows = max(0, len(work_rows) - len(dedup_works))
    duplicate_ratio = duplicate_rows / len(work_rows) if work_rows else 0.0
    score = 100
    if len(accounts) <= 1 and collection_mode != "targeted_profile":
        score -= 25
    if duplicate_ratio > 0.5:
        score -= 25
    elif duplicate_ratio > 0.2:
        score -= 10
    if current_flags.get("multiple_raw_aweme_ids"):
        score -= 10
    if any(blank_required.values()):
        score -= 10
    score = max(0, score)
    recommendations = []
    if len(accounts) <= 1 and collection_mode != "targeted_profile":
        recommendations.append("采集仍只覆盖单个账号；下一轮应先建立关注账号库，再逐账号采作品页。")
    if collection_mode == "targeted_profile":
        recommendations.append("本批次为单账号主页定向采集；可用于该账号作品分析，但不代表关注账号库或竞品样本。")
    if duplicate_ratio > 0.2:
        recommendations.append("作品卡片存在滚动窗口重复；分析时优先使用 dedup works 输出。")
    if current_flags.get("multiple_raw_aweme_ids"):
        recommendations.append("当前视频 ID 已按话题链接重新推断；浏览器采集端应缩小 DOM 容器，避免残留 ID 混入。")
    if flags.get("count_recovered_from_prefix"):
        recommendations.append("计数已从“共创/热点”前缀恢复；后续采集端应直接按卡片结构拆分徽标、计数、标题。")
    if flags.get("html_unescaped"):
        recommendations.append("文本已做 HTML 解码；后续所有可见文本进入分析前都应统一清洗。")
    return {
        "pipeline_version": "douyin_visible_clean_v1",
        "collection_mode": collection_mode,
        "profile_snapshot_count": profile_snapshot_count,
        "snapshot_count": len(snapshots),
        "record_count": len(clean_records),
        "current_video_count": len(current_videos),
        "work_card_count_raw": len(work_rows),
        "work_card_count_deduped": len(dedup_works),
        "estimated_duplicate_work_rows": duplicate_rows,
        "estimated_duplicate_ratio": round(duplicate_ratio, 4),
        "account_count": len(accounts),
        "accounts": accounts,
        "blank_required_counts": blank_required,
        "record_quality_flags": dict(sorted(flags.items())),
        "current_video_quality_flags": dict(sorted(current_flags.items())),
        "quality_score": score,
        "quality_grade": _quality_grade(score),
        "recommendations": recommendations,
        "snapshot_summary": [
            {
                "source_file": source_file,
                "account": (payload.get("account") or {}).get("nickname") or "",
                "visible_work_count": len(payload.get("visible_works") or []),
            }
            for source_file, payload in snapshots
        ],
    }


def _write_outputs(
    output_dir: Path,
    clean_records: list[dict[str, Any]],
    dedup_works: list[dict[str, Any]],
    current_videos: list[dict[str, Any]],
    quality_report: dict[str, Any],
) -> dict[str, str]:
    paths = {
        "clean_records_json": output_dir / "douyin_visible_records_clean_latest.json",
        "clean_records_csv": output_dir / "douyin_visible_records_clean_latest.csv",
        "clean_records_bom_csv": output_dir / "douyin_visible_records_clean_latest_utf8_bom.csv",
        "dedup_works_json": output_dir / "douyin_visible_works_dedup_latest.json",
        "dedup_works_csv": output_dir / "douyin_visible_works_dedup_latest.csv",
        "current_videos_json": output_dir / "douyin_current_videos_clean_latest.json",
        "current_videos_csv": output_dir / "douyin_current_videos_clean_latest.csv",
        "quality_report": output_dir / "douyin_collection_quality_latest.json",
    }
    _write_json(paths["clean_records_json"], clean_records)
    _write_json(paths["dedup_works_json"], dedup_works)
    _write_json(paths["current_videos_json"], current_videos)
    _write_json(paths["quality_report"], quality_report)
    _write_csv(paths["clean_records_csv"], clean_records, CLEAN_RECORD_HEADERS)
    _write_csv(paths["clean_records_bom_csv"], clean_records, CLEAN_RECORD_HEADERS, bom=True)
    _write_csv(paths["dedup_works_csv"], dedup_works, DEDUP_WORK_HEADERS)
    _write_csv(paths["current_videos_csv"], current_videos, CURRENT_VIDEO_HEADERS)
    return {name: str(path) for name, path in paths.items()}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str], bom: bool = False) -> None:
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _recover_visible_count(raw_count: Any, raw_title: str) -> tuple[str, str, list[str]]:
    flags = []
    title = _clean_text(raw_title)
    raw_count_text = _clean_text(raw_count or "")
    if raw_count_text:
        return raw_count_text, title, flags
    match = PREFIX_COUNT_RE.match(title)
    if match:
        flags.append("count_recovered_from_prefix")
        return match.group("count"), match.group("title").strip(), flags
    match = LEADING_COUNT_RE.match(title)
    if match:
        flags.append("count_recovered_from_leading_text")
        return match.group("count"), match.group("title").strip(), flags
    return "", title, ["missing_visible_count"]


def _account_context(account: dict[str, Any]) -> dict[str, str]:
    profile_url = _clean_text(account.get("profile_url") or "")
    return {
        "account_key": profile_url or _clean_text(account.get("nickname") or ""),
        "account_total_works_visible": _clean_text(account.get("total_works_visible") or ""),
        "account_type": _clean_text(account.get("account_type") or "unknown"),
        "content_domain": _clean_text(account.get("content_domain") or "unknown"),
        "following_state_visible": _clean_text(account.get("following_state_visible") or ""),
    }


def _classify_work(title: str, tags: list[str], aweme_id: str, visible_count: str) -> dict[str, str]:
    return classify_published_work(title=title, tags=tags, aweme_id=aweme_id, visible_count=visible_count)


def _extract_song_title(text: str) -> str:
    match = re.search(r"《([^》]{1,40})》", text)
    return _clean_text(match.group(1)) if match else ""


def _extract_artist_names(tag_names: list[str], song_title: str = "", text: str = "") -> list[str]:
    excluded = {
        "天赐的声音",
        "天赐的声音6",
        "歌手2026",
        "歌手排名",
        "歌手歌单",
        "歌手第六期歌单",
        "歌手彩排音源",
        "歌手小放送",
        "歌手2",
        "歌手20",
        "热点",
        "共创",
    }
    artists = []
    for known in KNOWN_ARTISTS:
        if known in text:
            artists.append(_normalize_artist_name(known))
    for mention in re.findall(r"@([^#@\n\r]{2,60})", text):
        artists.append(_normalize_artist_name(mention))
    for tag in tag_names:
        clean = _clean_text(tag)
        if not clean or clean in excluded:
            continue
        if song_title and clean == song_title:
            continue
        if any(word in clean for word in ["合唱", "唱尽", "唱得", "舞台", "声音", "星星", "爱情故事"]):
            continue
        if 2 <= len(clean) <= 5:
            artists.append(_normalize_artist_name(clean))
    return _unique(artists)


def _normalize_artist_name(value: str) -> str:
    clean = _clean_text(value).strip("@#")
    if not clean:
        return ""
    if clean.startswith("歌手"):
        return ""
    if "万妮达" in clean or "Vinida" in clean:
        return "万妮达"
    if "John Legend" in clean or clean.startswith("John Le") or "约翰·传奇" in clean:
        return "约翰·传奇"
    if "Elliot James Reay" in clean or "艾略特" in clean:
        return "艾略特"
    if "齐豫" in clean or "齊豫" in clean or "Chyi" in clean:
        return "齐豫"
    for known in ["侯明昊", "刘惜君", "张远", "窦靖童", "尤长靖", "胡彦斌"]:
        if known in clean:
            return known
    clean = re.split(r"[，,。！!？?：:、|/《#@]", clean)[0].strip()
    clean = re.sub(
        r"\s+(选择|作为|歌声|全程|超绝|带来|唱|说|没想到|情绪|律动|深情|状态|互动|特邀|场外|邀您).*$",
        "",
        clean,
    ).strip()
    if len(clean) < 2:
        return ""
    replacements = {
        "萬妮达Vinida": "万妮达",
        "万妮达Vinida": "万妮达",
        "张远Bird": "张远",
        "齊豫Chyi,Yu": "齐豫",
        "John Legend": "约翰·传奇",
        "Elliot James Reay": "艾略特",
    }
    clean = replacements.get(clean, clean)
    clean = re.sub(r"\s+(选择|歌声|《|用|全程|超绝|带来|唱)", "", clean).strip()
    return replacements.get(clean, clean)


def _content_category(text: str, song_title: str) -> str:
    if any(word in text for word in ["幕后", "花絮", "排练", "采访"]):
        return "behind_the_scenes"
    if any(word in text for word in ["点评", "导师", "评价", "晋级", "淘汰"]):
        return "judge_comment"
    if any(word in text for word in ["反应", "reaction", "观众", "全场"]):
        return "reaction"
    if any(word in text for word in ["合集", "盘点", "混剪"]):
        return "compilation"
    if song_title or any(word in text for word in ["清唱", "无伴奏", "合唱", "副歌", "唱", "舞台"]):
        return "performance_clip"
    if "天赐的声音" in text:
        return "music_variety"
    return "unknown"


def _hook_type(text: str, artist_names: list[str]) -> str:
    if any(word in text for word in ["高音", "爆发", "力量", "唱功", "直击"]):
        return "high_note"
    if any(word in text for word in ["遗憾", "想见你", "有我呢", "靠近", "心底", "泪", "爱情故事"]):
        return "emotional_story"
    if any(word in text for word in ["合唱", "共创", "搭配", "声线", "默契"]) or len(artist_names) >= 2:
        return "celebrity_pairing"
    if any(word in text for word in ["搞笑", "一台戏", "岳云鹏"]):
        return "funny"
    if "副歌" in text:
        return "chorus"
    if any(word in text for word in ["导师", "点评", "评价"]):
        return "judge_comment"
    return "unknown"


def _slice_structure(text: str) -> str:
    if any(word in text for word in ["清唱", "无伴奏"]):
        return "pure_highlight"
    if any(word in text for word in ["反应", "reaction", "全场"]):
        return "reaction_first"
    if any(word in text for word in ["副歌", "爆发", "直击", "听不够"]):
        return "pure_highlight"
    return "unknown"


def _commercial_intent(text: str) -> str:
    if any(word in text for word in ["购买", "下单", "直播间", "同款", "链接"]):
        return "ecommerce"
    if any(word in text for word in ["预约", "开播", "会员", "正片"]):
        return "soft_promo"
    return "none"


def _classification_confidence(aweme_id: str, visible_count: str, tags: list[str]) -> str:
    if aweme_id and visible_count and tags:
        return "high"
    if aweme_id or (visible_count and tags):
        return "medium"
    return "low"


def _parse_count(value: str) -> tuple[float | "", str]:
    clean = _clean_text(value)
    match = COUNT_RE.fullmatch(clean)
    if not match:
        return "", ""
    number = float(match.group("count").rstrip("万亿"))
    if clean.endswith("亿"):
        return number * 100000000, "亿"
    if clean.endswith("万"):
        return number * 10000, "万"
    return number, ""


def _extract_tags(raw_tags: Any, fallback_text: str) -> list[str]:
    tags: list[str] = []
    if isinstance(raw_tags, list):
        tags = [_clean_text(item) for item in raw_tags]
    elif isinstance(raw_tags, str):
        tags = [_clean_text(item) for item in raw_tags.split("|")]
    if not tags:
        tags = TAG_RE.findall(fallback_text)
    return _unique(tag for tag in tags if tag.startswith("#"))


def _select_current_aweme_id(raw_ids: list[str], link_ids: list[str]) -> str:
    if link_ids:
        counts = Counter(link_ids)
        return counts.most_common(1)[0][0]
    if raw_ids:
        return raw_ids[-1]
    return ""


def _aweme_id_from_href(href: str) -> str:
    clean = html.unescape(str(href or ""))
    try:
        parsed = urlparse(clean)
    except Exception:
        return ""
    query = parse_qs(parsed.query)
    for key in ["aweme_id", "gid", "from_gid", "vid"]:
        for value in query.get(key, []):
            match = AWEME_ID_RE.search(str(value))
            if match:
                return match.group(0)
    match = AWEME_ID_RE.search(clean)
    return match.group(0) if match else ""


def _work_key(account_key: str, normalized_title: str, aweme_id: str = "") -> str:
    if aweme_id:
        return f"aweme_{aweme_id}"
    raw = f"{_clean_text(account_key)}\n{_clean_text(normalized_title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _best_count(counts: list[str]) -> str:
    parsed = [(_parse_count(item)[0], item) for item in counts]
    numeric = [(number, item) for number, item in parsed if isinstance(number, float)]
    if not numeric:
        return counts[0] if counts else ""
    numeric.sort(key=lambda pair: pair[0], reverse=True)
    return numeric[0][1]


def _quality_grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _clean_text(value: Any) -> str:
    raw = str(value or "")
    decoded = html.unescape(raw)
    return re.sub(r"\s+", " ", decoded).strip()


def _collapse_repeated_text(value: str) -> tuple[str, bool]:
    text = _clean_text(value)
    if not text:
        return "", False
    midpoint = len(text) // 2
    if len(text) % 2 == 0 and text[:midpoint].strip() == text[midpoint:].strip():
        return text[:midpoint].strip(), True
    tokens = text.split(" ")
    if len(tokens) % 2 == 0:
        half = len(tokens) // 2
        if tokens[:half] == tokens[half:]:
            return " ".join(tokens[:half]), True
    return text, False


def _normalize_video_url(value: Any) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif raw.startswith("/video/"):
        raw = f"https://www.douyin.com{raw}"
    elif raw.startswith("www."):
        raw = f"https://{raw}"
    return raw


def _aweme_id_from_video_url(value: str) -> str:
    match = re.search(r"/video/(\d{10,})", value or "")
    if match:
        return match.group(1)
    return _aweme_id_from_href(value)


def _most_common(values: list[str]) -> str:
    clean = [value for value in values if value]
    if not clean:
        return ""
    return Counter(clean).most_common(1)[0][0]


def _merge_pipe_values(values: list[str]) -> str:
    merged = []
    for value in values:
        merged.extend(part for part in str(value or "").split("|") if part)
    return "|".join(_unique(merged))


def _best_confidence(values: list[str]) -> str:
    order = {"high": 3, "medium": 2, "low": 1}
    clean = [value for value in values if value]
    if not clean:
        return ""
    return sorted(clean, key=lambda item: order.get(item, 0), reverse=True)[0]


def _unique(values: Any) -> list[Any]:
    seen = set()
    result = []
    for value in values:
        if value in seen or value in ("", None):
            continue
        seen.add(value)
        result.append(value)
    return result
