from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from dso.accounts import account_metadata, dataset_display_name
from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.memory import TEXT_EMBEDDING_MODEL, TEXT_VECTOR_DIM, cosine_similarity, segment_memory_text, text_embedding
from dso.spreadsheets import XLSX_SUFFIXES, read_table_rows
from dso.utils import clamp, new_id, read_json, utc_now, write_json
from dso.versions import PROTOTYPE_BANK_VERSION


GENERIC_KEYWORDS = {
    "天赐的声音",
    "歌手",
    "歌手2025",
    "音乐",
    "舞台",
    "现场",
    "视频",
    "切片",
    "douyin",
}

ARCHETYPES = [
    {
        "key": "regret_resonance",
        "name": "遗憾共鸣型",
        "keywords": ["遗憾", "没选", "错过", "眼泪", "青春", "具体", "唱尽", "破防"],
        "title_patterns": ["把遗憾唱具体", "这句唱到谁的故事里", "没选我之后的情绪爆点"],
        "duration_range": [22, 45],
        "hook": "前 3 秒先给遗憾金句或关系冲突，再接副歌/高音释放。",
    },
    {
        "key": "stage_blast",
        "name": "舞台炸场型",
        "keywords": ["高音", "转调", "炸场", "爆发", "全场", "欢呼", "起立", "燃"],
        "title_patterns": ["这一段直接把现场点燃", "副歌爆发全场反应", "高音出来那一秒"],
        "duration_range": [16, 34],
        "hook": "开头直接进入爆点前 1-2 秒，保留观众/导师反应闭环。",
    },
    {
        "key": "singer_story",
        "name": "歌手故事型",
        "keywords": ["第一次", "妈妈", "坚持", "故事", "梦想", "回忆", "成长", "压力"],
        "title_patterns": ["他把一路坚持唱进这首歌", "第一次听懂这段故事", "歌手背后的那句话"],
        "duration_range": [30, 58],
        "hook": "先放一句人物故事，再进入旋律爆点，结尾留可评论问题。",
    },
    {
        "key": "competition_suspense",
        "name": "赛制悬念型",
        "keywords": ["排名", "晋级", "淘汰", "结果", "揭晓", "竞演", "赛制", "胜负"],
        "title_patterns": ["这段会改变排名吗", "晋级悬念就在这一句", "结果公布前最关键一段"],
        "duration_range": [18, 42],
        "hook": "开头交代赛制/排名悬念，中段给关键演唱证据，结尾抛讨论。",
    },
    {
        "key": "celebrity_pairing",
        "name": "明星组合型",
        "keywords": ["合唱", "搭配", "默契", "合作", "组合", "互相", "共创", "同台"],
        "title_patterns": ["两个人的声线太搭了", "这个合作舞台值得二刷", "他们的默契藏在这一句"],
        "duration_range": [20, 44],
        "hook": "开头明确组合关系，剪出声线互补或互动瞬间。",
    },
    {
        "key": "national_aesthetic",
        "name": "国风审美型",
        "keywords": ["国风", "东方", "山水", "桃花", "古典", "半壶纱", "风雅", "清凉"],
        "title_patterns": ["这一段国风审美拉满", "东方风雅唱出来了", "像一幅会唱歌的山水画"],
        "duration_range": [22, 48],
        "hook": "封面和首句突出画面/意象，字幕保留最有画面感的歌词。",
    },
    {
        "key": "debate_trigger",
        "name": "争议讨论型",
        "keywords": ["公平", "争议", "不服", "失误", "谁更强", "评价", "导师", "加薪"],
        "title_patterns": ["这次评价你同意吗", "这一票到底公不公平", "谁才是这一段真正的亮点"],
        "duration_range": [16, 36],
        "hook": "把可争议的问题前置，但避免标题党，正文给足判断依据。",
    },
    {
        "key": "comprehensive_stage",
        "name": "综合舞台型",
        "keywords": ["歌曲", "演唱", "表演", "改编", "旋律", "情绪", "副歌", "作品"],
        "title_patterns": ["这段适合单独剪出来", "舞台里最有传播力的一段", "这一句是整首歌的记忆点"],
        "duration_range": [24, 52],
        "hook": "用一句清晰标题解释看点，保留完整情绪起承转合。",
    },
]


ARCHETYPE_BY_KEY = {item["key"]: item for item in ARCHETYPES}

ABSOLUTE_VIEW_LEVELS = [
    ("L0", "未过冷启", 0, 1000),
    ("L1", "基础分发", 1000, 5000),
    ("L2", "小流量", 5000, 10000),
    ("L3", "垂类有效", 10000, 50000),
    ("L4", "高潜流量", 50000, 100000),
    ("L5", "高流量", 100000, 500000),
    ("L6", "爆款", 500000, 1000000),
    ("L7", "超级爆款", 1000000, None),
]


def list_capture_datasets(*, include_all: bool = True) -> dict:
    settings = ensure_data_dirs()
    datasets = _discover_capture_datasets(settings.root)
    if include_all and datasets:
        all_paths = [path for dataset in datasets for path in dataset.get("source_paths") or []]
        all_summary = _summarize_capture_paths([Path(path) for path in all_paths])
        datasets = [
            {
                "id": "all",
                "name": "全部采集",
                "display_name": "全部采集",
                "account_id": "all",
                "account_display_name": "全部账号",
                "program_key": "all",
                "kind": "capture_collection",
                "source_paths": all_paths,
                **all_summary,
            },
            *datasets,
        ]
    return {
        "contract_version": "dataset_catalog.v1",
        "count": len(datasets),
        "datasets": datasets,
    }


def build_prototype_bank(
    account_id: str = "main",
    *,
    source: str = "external",
    dataset_id: str | None = None,
    source_path: str | Path | None = None,
    limit: int = 80,
    min_views: int = 0,
    force: bool = False,
) -> dict:
    source_name = (source or "external").strip() or "external"
    dataset = _resolve_dataset(dataset_id, source_path=source_path)
    raw_samples = _load_samples(account_id, source=source_name, source_path=source_path, dataset=dataset)
    samples = _dedupe_samples(raw_samples)
    min_view_floor = max(0, int(min_views or 0))
    samples = [sample for sample in samples if _sample_has_prototype_signal(sample, min_view_floor)]
    samples.sort(key=lambda row: (_performance_value(row), row.get("collected_at") or ""), reverse=True)
    selected = samples[: max(1, int(limit or 80))]
    _score_samples(selected)
    account_distribution = _account_distribution(selected)
    grouped = _group_samples(selected)
    prototypes = [
        _prototype_from_group(
            account_id,
            source_name,
            key,
            rows,
            account_distribution=account_distribution,
            dataset=dataset,
        )
        for key, rows in grouped.items()
    ]
    prototypes.sort(key=_prototype_rank, reverse=True)
    _store_prototypes(account_id, source_name, prototypes, dataset=dataset, force=force)
    status = "empty"
    if prototypes and len(selected) >= 8:
        status = "ready"
    elif prototypes:
        status = "low_confidence"
    return {
        "contract_version": PROTOTYPE_BANK_VERSION,
        "status": status,
        "account_id": account_id,
        "dataset_id": dataset["id"],
        "dataset_name": dataset["name"],
        "dataset": dataset,
        "source": source_name,
        "generated_at": utc_now(),
        "sample_count": len(selected),
        "prototype_count": len(prototypes),
        "source_summary": _source_summary(raw_samples, selected, source_path, dataset),
        "account_distribution": account_distribution,
        "model": {
            "name": "rule_cluster_plus_hash_embedding",
            "embedding_model": TEXT_EMBEDDING_MODEL,
            "vector_dim": TEXT_VECTOR_DIM,
            "score": "explicit play/view counts when present; otherwise reward_proxy/normalized_reward from engagement metrics",
            "absolute_level_policy": "prototype P75 views map to internal L0-L7 platform-scale bands only when real play/view counts exist.",
            "lift_policy": "prototype P75 is compared with the same account/sample pool using the active performance basis.",
        },
        "prototypes": prototypes,
        "next_actions": _next_actions(status, len(selected), len(prototypes)),
    }


def list_prototype_bank(account_id: str = "main", *, source: str = "external", dataset_id: str | None = None, limit: int = 20) -> dict:
    source_name = (source or "external").strip() or "external"
    dataset_key = _normalize_dataset_id(dataset_id)
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT *
            FROM prototype_bank_items
            WHERE account_id = ? AND dataset_id = ? AND source = ? AND version = ?
            ORDER BY updated_at DESC
            """,
            [account_id, dataset_key, source_name, PROTOTYPE_BANK_VERSION],
        )
    prototypes = [_row_to_prototype(row) for row in rows]
    prototypes.sort(key=_prototype_rank, reverse=True)
    prototypes = prototypes[: max(1, int(limit or 20))]
    return {
        "contract_version": PROTOTYPE_BANK_VERSION,
        "status": "ready" if prototypes else "empty",
        "account_id": account_id,
        "dataset_id": dataset_key,
        "source": source_name,
        "count": len(prototypes),
        "prototypes": prototypes,
    }


def match_segment_prototypes(
    segment_id: str,
    *,
    account_id: str | None = None,
    source: str = "external",
    dataset_id: str | None = None,
    limit: int = 5,
) -> dict:
    with connect() as conn:
        segment = fetch_one(
            conn,
            """
            SELECT c.*, v.account_id
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            WHERE c.id = ?
            """,
            [segment_id],
        )
    if not segment:
        raise KeyError(f"segment not found: {segment_id}")
    account = account_id or segment.get("account_id") or "main"
    bank = list_prototype_bank(account, source=source, dataset_id=dataset_id, limit=100)
    if not bank["prototypes"]:
        bank = build_prototype_bank(account, source=source, dataset_id=dataset_id, limit=80)
    target_text = segment_memory_text(segment)
    target_vector = text_embedding(target_text)
    matches = []
    for prototype in bank.get("prototypes", []):
        vector = _prototype_vector(prototype)
        similarity = cosine_similarity(target_vector, vector)
        keyword_overlap = _keyword_overlap(target_text, prototype.get("keywords") or [])
        blended = max(similarity, keyword_overlap)
        fit_score = blended * float(prototype.get("avg_score") or 0) * (0.65 + 0.35 * float(prototype.get("confidence") or 0))
        matches.append(
            {
                "segment_id": segment_id,
                "prototype_key": prototype.get("prototype_key"),
                "prototype_name": prototype.get("prototype_name"),
                "similarity": round(blended, 4),
                "embedding_similarity": round(similarity, 4),
                "keyword_overlap": round(keyword_overlap, 4),
                "prototype_score": round(float(prototype.get("avg_score") or 0), 2),
                "confidence": round(float(prototype.get("confidence") or 0), 4),
                "fit_score": round(fit_score, 2),
                "keywords": prototype.get("keywords") or [],
                "parameters": prototype.get("parameters") or {},
                "examples": (prototype.get("examples") or [])[:3],
            }
        )
    matches.sort(key=lambda row: (row["fit_score"], row["similarity"]), reverse=True)
    selected = matches[: max(1, int(limit or 5))]
    return {
        "contract_version": PROTOTYPE_BANK_VERSION,
        "status": "ready" if selected else "empty",
        "segment_id": segment_id,
        "account_id": account,
        "dataset_id": bank.get("dataset_id") or _normalize_dataset_id(dataset_id),
        "source": source,
        "matched_count": len(selected),
        "matches": selected,
    }


def _load_samples(account_id: str, *, source: str, source_path: str | Path | None, dataset: dict) -> list[dict]:
    samples: list[dict] = []
    dataset_id = _normalize_dataset_id(dataset.get("id"))
    include_metrics = source in {"douyin", "douyin_metrics"} or (source in {"external", "all"} and dataset_id == "default")
    if include_metrics:
        samples.extend(_metric_samples(account_id))
    if source in {"external", "capture_csv", "visible_capture", "all"}:
        if not source_path:
            try:
                from dso.learning.historical_samples import historical_samples_for_prototypes

                stored = historical_samples_for_prototypes(account_id, dataset_id=dataset_id)
            except Exception:
                stored = []
            if stored or source == "visible_capture":
                samples.extend(stored)
                return samples
        samples.extend(_capture_samples(account_id, source_path=source_path, dataset=dataset))
    elif source_path:
        samples.extend(_capture_samples(account_id, source_path=source_path, dataset=dataset))
    return samples


def _metric_samples(account_id: str) -> list[dict]:
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT pm.*, pvm.account_id, pvm.platform_title, pvm.platform_url, pvm.published_at,
                   pvm.last_metrics_at
            FROM performance_metrics pm
            JOIN platform_video_mappings pvm
              ON pvm.platform = 'douyin'
             AND pvm.platform_item_id = pm.platform_item_id
            WHERE pvm.account_id = ?
              AND pm.platform_item_id != ''
              AND pm.sample_source IN ('api', 'csv', 'mock')
            """,
            [account_id],
        )
    samples = []
    for row in rows:
        title = _text(row.get("platform_title"))
        if not title:
            continue
        samples.append(
            {
                "source_kind": "metric_db",
                "account_id": account_id,
                "sample_id": row.get("platform_item_id") or row.get("id"),
                "platform_item_id": row.get("platform_item_id") or "",
                "title": title,
                "platform_url": row.get("platform_url") or "",
                "published_at": row.get("published_at") or "",
                "collected_at": row.get("collected_at") or row.get("last_metrics_at") or "",
                "views": int(_num(row.get("views"))),
                "likes": int(_num(row.get("likes"))),
                "comments": int(_num(row.get("comments"))),
                "favorites": int(_num(row.get("favorites"))),
                "shares": int(_num(row.get("shares"))),
                "follows": int(_num(row.get("follows"))),
                "hook_type": "",
                "slice_structure": "",
                "content_category": "",
                "program_name": "",
                "artist_names": "",
                "song_title": "",
                "raw": dict(row),
            }
        )
    return samples


def _capture_samples(account_id: str, *, source_path: str | Path | None, dataset: dict) -> list[dict]:
    paths = _capture_paths(source_path, dataset=dataset)
    samples = []
    for path in paths:
        source_kind = "capture_xlsx" if path.suffix.lower() in XLSX_SUFFIXES else "capture_csv"
        for row in _read_rows(path):
            title = _text(row.get("normalized_title") or row.get("标题") or row.get("platform_title") or row.get("title_tags_text") or row.get("title"))
            views = _num(row.get("best_visible_count_number") or row.get("计数数值") or row.get("visible_count_number") or row.get("views") or row.get("play_count"))
            if not title or views <= 0:
                continue
            item_id = _clean_item_id(row.get("aweme_id") or row.get("视频ID文本") or row.get("platform_item_id") or row.get("work_key"))
            samples.append(
                {
                    "source_kind": source_kind,
                    "account_id": account_id,
                    "sample_id": item_id or _stable_key(title),
                    "platform_item_id": item_id,
                    "title": title,
                    "platform_url": _text(row.get("video_url") or row.get("视频URL") or row.get("platform_url") or row.get("profile_url") or row.get("page_url")),
                    "published_at": "",
                    "collected_at": _text(row.get("last_observed_at") or row.get("observed_at") or row.get("first_observed_at")),
                    "views": int(views),
                    "likes": 0,
                    "comments": 0,
                    "favorites": 0,
                    "shares": 0,
                    "follows": 0,
                    "hook_type": _text(row.get("hook_type") or row.get("钩子类型")),
                    "slice_structure": _text(row.get("slice_structure") or row.get("切片结构")),
                    "content_category": _text(row.get("content_category") or row.get("内容类别")),
                    "program_name": _text(row.get("program_name") or row.get("节目")),
                    "artist_names": _text(row.get("artist_names") or row.get("艺人")),
                    "song_title": _text(row.get("song_title") or row.get("歌曲")),
                    "tags": _text(row.get("tags") or row.get("话题标签")),
                    "raw": dict(row),
                    "source_file": str(path),
                    "dataset_id": dataset.get("id") or "default",
                    "dataset_name": dataset.get("name") or "",
                }
            )
    return samples


def _capture_paths(source_path: str | Path | None, *, dataset: dict | None = None) -> list[Path]:
    if source_path:
        path = Path(source_path).expanduser().resolve()
        if path.is_dir():
            return sorted([*path.glob("*.csv"), *path.glob("*.xlsx"), *path.glob("*.xslx")])
        return [path]
    dataset = dataset or {}
    paths = [Path(path) for path in dataset.get("source_paths") or []]
    if paths:
        return paths
    settings = ensure_data_dirs()
    latest_xlsx = _latest_capture_workbook(settings.root)
    if latest_xlsx:
        return [latest_xlsx]
    default = settings.data_dir / "douyin_capture" / "douyin_visible_works_dedup_latest.csv"
    return [default] if default.exists() else []


def _resolve_dataset(dataset_id: str | None, *, source_path: str | Path | None) -> dict:
    if source_path:
        path = Path(source_path).expanduser().resolve()
        dataset = _dataset_from_path(path)
        dataset["source_paths"] = [str(path)]
        dataset.update(_summarize_capture_paths([path]))
        if dataset_id:
            dataset["id"] = _normalize_dataset_id(dataset_id)
        return dataset
    dataset_key = _normalize_dataset_id(dataset_id)
    if dataset_key == "default":
        return {"id": "default", "name": "默认数据源", "program_key": "default", "kind": "default", "source_paths": []}
    discovered = _discover_capture_datasets(ensure_data_dirs().root)
    if dataset_key == "all":
        paths = [Path(path) for item in discovered for path in item.get("source_paths") or []]
        return {
            "id": "all",
            "name": "全部采集",
            "program_key": "all",
            "kind": "capture_collection",
            "source_paths": [str(path) for path in paths],
            **_summarize_capture_paths(paths),
        }
    for item in discovered:
        if item.get("id") == dataset_key:
            return item
    return {"id": dataset_key, "name": dataset_key, "program_key": dataset_key, "kind": "missing", "source_paths": []}


def _normalize_dataset_id(value: str | None) -> str:
    text = _text(value).strip()
    return text or "default"


def _discover_capture_datasets(root: Path) -> list[dict]:
    output_dir = root / "outputs"
    if not output_dir.exists():
        return []
    patterns = [
        "douyin_three_accounts_*/accounts/*/*_douyin_collection_latest.xlsx",
        "douyin_*_*/*_visible_collection_latest.xlsx",
        "douyin_*/*_visible_collection_latest.xlsx",
        "**/*_douyin_visible_collection_latest.xlsx",
    ]
    seen: set[Path] = set()
    datasets: dict[str, dict] = {}
    for pattern in patterns:
        for path in output_dir.glob(pattern):
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            dataset = _dataset_from_path(path)
            summary = _summarize_capture_paths([path])
            existing = datasets.get(dataset["id"])
            if existing:
                if (summary.get("latest_at") or "") >= (existing.get("latest_at") or ""):
                    existing["source_paths"] = [str(path)]
                    existing.update(summary)
                existing["latest_at"] = max(existing.get("latest_at") or "", summary.get("latest_at") or "")
            else:
                datasets[dataset["id"]] = {**dataset, **summary}
    return sorted(datasets.values(), key=lambda item: (item.get("latest_at") or "", item.get("id") or ""), reverse=True)


def _dataset_from_path(path: Path) -> dict:
    directory = path.parent.name
    name = path.stem
    account_parent = path.parent.parent.name if path.parent.parent else ""
    batch_parent = path.parent.parent.parent.name if path.parent.parent.parent else ""
    if account_parent == "accounts":
        program_key = directory
        batch_match = re.match(r"douyin_three_accounts_(\d{8})$", batch_parent)
        date_key = batch_match.group(1) if batch_match else ""
    else:
        match = re.match(r"douyin_(.+?)_(\d{8})$", directory)
        if match:
            program_key, date_key = match.groups()
        else:
            stem_match = re.match(r"(.+?)_douyin_(?:visible_)?collection_latest$", name)
            program_key = stem_match.group(1) if stem_match else name.replace("_visible_collection_latest", "").replace("_collection_latest", "")
            date_key = ""
    dataset_id = f"{program_key}_{date_key}" if date_key else program_key
    meta = account_metadata(program_key)
    display_name = _dataset_name(program_key, date_key)
    return {
        "id": dataset_id,
        "name": display_name,
        "display_name": display_name,
        "account_id": program_key,
        "account_display_name": meta.get("account_display_name") or program_key,
        "account_tier": meta.get("account_tier") or "",
        "program_key": program_key,
        "kind": "capture_workbook",
        "source_paths": [str(path)],
    }


def _dataset_name(program_key: str, date_key: str) -> str:
    dataset_id = f"{program_key}_{date_key}" if date_key else program_key
    return dataset_display_name(program_key, dataset_id=dataset_id)


def _summarize_capture_paths(paths: list[Path]) -> dict:
    signatures = []
    for path in paths:
        if not path.exists():
            continue
        resolved = path.resolve()
        stat = resolved.stat()
        signatures.append((str(resolved), stat.st_mtime_ns, stat.st_size))
    return dict(_summarize_capture_paths_cached(tuple(signatures)))


@lru_cache(maxsize=128)
def _summarize_capture_paths_cached(signatures: tuple[tuple[str, int, int], ...]) -> dict:
    raw_rows = 0
    valid_rows = 0
    unique_keys: set[str] = set()
    max_views = 0
    latest_at = ""
    for path_text, _mtime_ns, _size in signatures:
        path = Path(path_text)
        try:
            rows = _read_rows(path)
        except Exception:
            rows = []
        raw_rows += len(rows)
        latest_at = max(latest_at, datetime.fromtimestamp(path.stat().st_mtime).isoformat())
        for row in rows:
            title = _text(row.get("normalized_title") or row.get("标题") or row.get("platform_title") or row.get("title_tags_text") or row.get("title"))
            views = _num(row.get("best_visible_count_number") or row.get("计数数值") or row.get("visible_count_number") or row.get("views") or row.get("play_count"))
            if not title or views <= 0:
                continue
            item_id = _clean_item_id(row.get("aweme_id") or row.get("视频ID文本") or row.get("platform_item_id") or row.get("work_key"))
            valid_rows += 1
            unique_keys.add(item_id or _stable_key(title))
            max_views = max(max_views, int(views))
    return {
        "raw_rows": raw_rows,
        "sample_count": valid_rows,
        "unique_count": len(unique_keys),
        "max_views": max_views,
        "latest_at": latest_at,
    }


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    resolved = path.resolve()
    stat = resolved.stat()
    return [dict(row) for row in _read_rows_cached(str(resolved), stat.st_mtime_ns, stat.st_size)]


@lru_cache(maxsize=256)
def _read_rows_cached(path_text: str, mtime_ns: int, size: int) -> tuple[dict, ...]:
    del mtime_ns, size
    path = Path(path_text)
    if path.suffix.lower() in XLSX_SUFFIXES:
        rows = read_table_rows(
            path,
            preferred_sheets=("作品去重", "作品明细", "三账号作品", "天赐作品", "歌手2026作品", "思绪作品", "原始清洗记录"),
        )
    elif path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
    else:
        rows = []
    return tuple(dict(row) for row in rows)


def _latest_capture_workbook(root: Path) -> Path | None:
    output_dir = root / "outputs"
    if not output_dir.exists():
        return None
    patterns = [
        "douyin_three_accounts_*/accounts/*/*_douyin_collection_latest.xlsx",
        "douyin_*_*/*_visible_collection_latest.xlsx",
        "douyin_*/*_visible_collection_latest.xlsx",
        "**/*_douyin_visible_collection_latest.xlsx",
    ]
    seen = set()
    matches = []
    for pattern in patterns:
        for path in output_dir.glob(pattern):
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            matches.append(path)
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _dedupe_samples(samples: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for sample in samples:
        key = sample.get("platform_item_id") or sample.get("sample_id") or _stable_key(sample.get("title") or "")
        current = best.get(key)
        if not current or _sample_preference(sample) > _sample_preference(current):
            best[key] = sample
    return list(best.values())


def _sample_has_prototype_signal(sample: dict, min_view_floor: int) -> bool:
    views = float(sample.get("views") or 0)
    if views > 0:
        return views >= min_view_floor
    if min_view_floor > 0:
        return False
    return float(sample.get("reward_proxy") or 0) > 0 or any(
        float(sample.get(key) or 0) > 0
        for key in ["likes", "comments", "favorites", "shares", "follows"]
    )


def _score_samples(samples: list[dict]) -> None:
    values = sorted(_performance_value(row) for row in samples if _performance_value(row) > 0)
    median_value = _percentile(values, 0.5) or 1.0
    p75_value = _percentile(values, 0.75) or median_value
    for sample in samples:
        performance = _performance_value(sample)
        relative = performance / max(1.0, median_value)
        score = 50.0 + math.log(max(0.05, relative), 2) * 18.0
        engagement = _engagement_rate(sample)
        if engagement:
            score += min(10.0, engagement * 240.0)
        elif float(sample.get("views") or 0) <= 0:
            score += min(10.0, math.log1p(_engagement_total(sample)) * 0.8)
        if p75_value and performance >= p75_value:
            score += 4.0
        sample["score"] = round(clamp(score), 2)
        archetype = classify_archetype(sample)
        sample["prototype_key"] = archetype["key"]
        sample["prototype_name"] = archetype["name"]
        sample["keywords"] = extract_keywords(sample)
        sample["publish_hour"] = _publish_hour(sample.get("published_at") or sample.get("collected_at"))


def classify_archetype(sample: dict) -> dict:
    text = " ".join(
        [
            _text(sample.get("title")),
            _text(sample.get("tags")),
            _text(sample.get("hook_type")),
            _text(sample.get("slice_structure")),
            _text(sample.get("song_title")),
        ]
    ).lower()
    hook = _text(sample.get("hook_type")).lower()
    if _contains_any(text, ["遗憾", "没选", "错过"]):
        return ARCHETYPE_BY_KEY["regret_resonance"]
    if _contains_any(text, ["国风", "东方", "半壶纱", "桃花", "风雅"]):
        return ARCHETYPE_BY_KEY["national_aesthetic"]
    if hook in {"high_note", "music_burst", "stage_blast", "高音爆点"}:
        return ARCHETYPE_BY_KEY["stage_blast"]
    if hook in {"emotional_story", "情绪故事"}:
        return ARCHETYPE_BY_KEY["regret_resonance"] if _contains_any(text, ["遗憾", "没选", "错过"]) else ARCHETYPE_BY_KEY["singer_story"]
    if hook in {"celebrity_pairing", "明星组合"}:
        return ARCHETYPE_BY_KEY["celebrity_pairing"]
    scores = []
    for item in ARCHETYPES:
        score = sum(1 for keyword in item["keywords"] if keyword.lower() in text)
        if item["key"] == "national_aesthetic" and _contains_any(text, ["国风", "东方", "半壶纱"]):
            score += 2
        if item["key"] == "stage_blast" and _contains_any(text, ["副歌", "高音", "全场"]):
            score += 1
        scores.append((score, item))
    scores.sort(key=lambda pair: pair[0], reverse=True)
    if scores and scores[0][0] > 0:
        return scores[0][1]
    return ARCHETYPE_BY_KEY["comprehensive_stage"]


def extract_keywords(sample: dict) -> list[str]:
    text = " ".join(
        _text(sample.get(key))
        for key in ["title", "tags", "program_name", "artist_names", "song_title", "hook_type", "slice_structure"]
    )
    keywords = []
    for part in re.findall(r"#([^#\s|，,。!！?？《》]+)", text):
        keywords.append(part)
    for field in ["tags", "artist_names"]:
        for part in re.split(r"[|,/，、\s]+", _text(sample.get(field))):
            if part:
                keywords.append(part.lstrip("#"))
    song = _text(sample.get("song_title"))
    if song:
        keywords.append(song)
    for phrase in ["遗憾", "没选我", "高音", "转调", "国风", "合作舞台", "导师评价", "排名", "晋级", "全场欢呼", "故事"]:
        if phrase in text:
            keywords.append(phrase)
    cleaned = []
    seen = set()
    for keyword in keywords:
        value = keyword.strip(" #|，,。!！?？《》\"'")
        if not value or value in GENERIC_KEYWORDS or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned[:10]


def _group_samples(samples: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        grouped[sample["prototype_key"]].append(sample)
    return dict(grouped)


def _prototype_from_group(
    account_id: str,
    source: str,
    key: str,
    samples: list[dict],
    *,
    account_distribution: dict,
    dataset: dict,
) -> dict:
    archetype = ARCHETYPE_BY_KEY.get(key, ARCHETYPE_BY_KEY["comprehensive_stage"])
    ordered = sorted(samples, key=_performance_value, reverse=True)
    views = sorted(float(row.get("views") or 0) for row in samples)
    performance_values = sorted(_performance_value(row) for row in samples)
    scores = [float(row.get("score") or 0) for row in samples]
    median_views = _percentile(views, 0.5)
    p75_views = _percentile(views, 0.75)
    max_views = max(views) if views else 0.0
    median_performance = _percentile(performance_values, 0.5)
    p75_performance = _percentile(performance_values, 0.75)
    max_performance = max(performance_values) if performance_values else 0.0
    performance_basis = _performance_basis(samples)
    performance_label = _performance_label(performance_basis)
    keyword_weights: Counter[str] = Counter()
    for sample in ordered:
        weight = 1.0 + float(sample.get("score") or 0) / 100.0
        for keyword in sample.get("keywords") or []:
            keyword_weights[keyword] += weight
    keywords = [keyword for keyword, _weight in keyword_weights.most_common(8)]
    examples = [
        {
            "title": row.get("title") or "",
            "views": int(row.get("views") or 0),
            "performance_value": round(_performance_value(row), 4),
            "performance_basis": performance_basis,
            "score": round(float(row.get("score") or 0), 2),
            "platform_item_id": row.get("platform_item_id") or "",
            "url": row.get("platform_url") or "",
            "source_kind": row.get("source_kind") or "",
        }
        for row in ordered[:5]
    ]
    avg_score = round(sum(scores) / max(1, len(scores)), 2)
    confidence = _confidence(len(samples), median_performance, avg_score)
    performance_metric = _performance_metric(
        basis=performance_basis,
        label=performance_label,
        median_value=median_performance,
        p75_value=p75_performance,
        max_value=max_performance,
        account_distribution=account_distribution,
    )
    parameters = _parameters_for_group(
        archetype,
        ordered,
        keywords,
        p75_views=p75_views,
        median_views=median_views,
        max_views=max_views,
        avg_score=avg_score,
        confidence=confidence,
        account_distribution=account_distribution,
        performance_metric=performance_metric,
    )
    prototype = {
        "contract_version": PROTOTYPE_BANK_VERSION,
        "account_id": account_id,
        "dataset_id": dataset.get("id") or "default",
        "dataset_name": dataset.get("name") or "",
        "prototype_key": archetype["key"],
        "prototype_name": archetype["name"],
        "source": source,
        "sample_count": len(samples),
        "median_views": round(median_views, 2),
        "p75_views": round(p75_views, 2),
        "max_views": round(max_views, 2),
        "median_performance": round(median_performance, 4),
        "p75_performance": round(p75_performance, 4),
        "max_performance": round(max_performance, 4),
        "performance_basis": performance_basis,
        "avg_score": avg_score,
        "confidence": confidence,
        "keywords": keywords,
        "examples": examples,
        "parameters": parameters,
        "updated_at": utc_now(),
    }
    vector_path = _write_prototype_vector(account_id, prototype)
    prototype["vector_path"] = str(vector_path)
    return prototype


def _prototype_rank(row: dict) -> tuple[float, ...]:
    sample_count = float(row.get("sample_count") or 0)
    mature = 1.0 if sample_count >= 3 else 0.0
    parameters = row.get("parameters") or {}
    stability = parameters.get("stability") or {}
    stability_rank = float(stability.get("rank") or 0)
    performance_metric = parameters.get("performance_metric") or {}
    lift = performance_metric.get("p75_lift") or (parameters.get("account_lift") or {}).get("p75_lift") or 0
    performance_max = float(performance_metric.get("max") or row.get("max_performance") or row.get("max_views") or 0)
    weighted_score = float(row.get("avg_score") or 0) * float(row.get("confidence") or 0)
    return (mature, stability_rank, float(lift), weighted_score, sample_count, performance_max)


def _parameters_for_group(
    archetype: dict,
    samples: list[dict],
    keywords: list[str],
    *,
    p75_views: float,
    median_views: float,
    max_views: float,
    avg_score: float,
    confidence: float,
    account_distribution: dict,
    performance_metric: dict,
) -> dict:
    hour_counts = Counter(row.get("publish_hour") for row in samples if isinstance(row.get("publish_hour"), int) and row.get("publish_hour") >= 0)
    top_hours = [hour for hour, _count in hour_counts.most_common(3)]
    absolute_level = _absolute_level(p75_views)
    max_absolute_level = _absolute_level(max_views)
    if performance_metric.get("basis") != "views":
        absolute_level = _engagement_level(performance_metric)
        max_absolute_level = {
            **absolute_level,
            "value": round(float(performance_metric.get("max") or 0), 4),
            "basis": performance_metric.get("basis"),
        }
    account_lift = _account_lift(
        p75_views=p75_views,
        median_views=median_views,
        max_views=max_views,
        account_distribution=account_distribution,
    )
    stability = _stability(
        sample_count=len(samples),
        p75_views=p75_views,
        max_views=max_views,
        confidence=confidence,
        avg_score=avg_score,
        performance_metric=performance_metric,
    )
    return {
        "duration_seconds_range": archetype["duration_range"],
        "opening_hook": archetype["hook"],
        "title_patterns": _title_patterns(archetype, keywords),
        "cover_focus": _cover_focus(archetype["key"]),
        "publish_hours": top_hours,
        "absolute_level": absolute_level,
        "max_absolute_level": max_absolute_level,
        "account_lift": account_lift,
        "performance_metric": performance_metric,
        "stability": stability,
        "decision_label": _decision_label(absolute_level, performance_metric or account_lift, stability),
        "sample_basis": "published_at if present, otherwise capture observed_at",
    }


def _title_patterns(archetype: dict, keywords: list[str]) -> list[str]:
    base = list(archetype["title_patterns"])
    for keyword in keywords[:3]:
        base.append(f"{keyword} 这一段为什么容易被转发")
    return base[:5]


def _cover_focus(key: str) -> str:
    if key == "regret_resonance":
        return "人物近景+遗憾金句字幕"
    if key == "stage_blast":
        return "高音爆发瞬间+观众反应"
    if key == "competition_suspense":
        return "排名/导师表情/悬念问题"
    if key == "national_aesthetic":
        return "国风画面意象+歌名关键词"
    if key == "celebrity_pairing":
        return "两位歌手同框或互动瞬间"
    return "歌手表情清晰、看点字幕不遮脸"


def _store_prototypes(account_id: str, source: str, prototypes: list[dict], *, dataset: dict, force: bool) -> None:
    dataset_id = dataset.get("id") or "default"
    with connect() as conn:
        conn.execute(
            "DELETE FROM prototype_bank_items WHERE account_id = ? AND dataset_id = ? AND source = ? AND version = ?",
            [account_id, dataset_id, source, PROTOTYPE_BANK_VERSION],
        )
        for prototype in prototypes:
            insert_row(
                conn,
                "prototype_bank_items",
                {
                    "id": new_id("proto"),
                    "account_id": account_id,
                    "dataset_id": prototype.get("dataset_id") or dataset_id,
                    "dataset_name": prototype.get("dataset_name") or dataset.get("name") or "",
                    "prototype_key": prototype["prototype_key"],
                    "prototype_name": prototype["prototype_name"],
                    "source": source,
                    "sample_count": int(prototype["sample_count"]),
                    "median_views": float(prototype["median_views"]),
                    "p75_views": float(prototype["p75_views"]),
                    "max_views": float(prototype["max_views"]),
                    "avg_score": float(prototype["avg_score"]),
                    "confidence": float(prototype["confidence"]),
                    "keywords_json": json.dumps(prototype.get("keywords") or [], ensure_ascii=False),
                    "examples_json": json.dumps(prototype.get("examples") or [], ensure_ascii=False),
                    "parameters_json": json.dumps(prototype.get("parameters") or {}, ensure_ascii=False),
                    "vector_path": prototype.get("vector_path") or "",
                    "version": PROTOTYPE_BANK_VERSION,
                    "updated_at": prototype.get("updated_at") or utc_now(),
                },
            )
        conn.commit()


def _row_to_prototype(row: dict) -> dict:
    return {
        "contract_version": PROTOTYPE_BANK_VERSION,
        "account_id": row.get("account_id"),
        "dataset_id": row.get("dataset_id") or "default",
        "dataset_name": row.get("dataset_name") or "",
        "prototype_key": row.get("prototype_key"),
        "prototype_name": row.get("prototype_name"),
        "source": row.get("source"),
        "sample_count": int(row.get("sample_count") or 0),
        "median_views": float(row.get("median_views") or 0),
        "p75_views": float(row.get("p75_views") or 0),
        "max_views": float(row.get("max_views") or 0),
        "avg_score": float(row.get("avg_score") or 0),
        "confidence": float(row.get("confidence") or 0),
        "keywords": _json(row.get("keywords_json"), []),
        "examples": _json(row.get("examples_json"), []),
        "parameters": _json(row.get("parameters_json"), {}),
        "vector_path": row.get("vector_path") or "",
        "version": row.get("version") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _write_prototype_vector(account_id: str, prototype: dict) -> Path:
    settings = ensure_data_dirs()
    text = _prototype_text(prototype)
    vector = text_embedding(text)
    dataset_id = _normalize_dataset_id(prototype.get("dataset_id"))
    path = settings.cache_dir / "prototypes" / account_id / dataset_id / f"{prototype['prototype_key']}.json"
    write_json(
        path,
        {
            "contract_version": PROTOTYPE_BANK_VERSION,
            "prototype_key": prototype["prototype_key"],
            "prototype_name": prototype["prototype_name"],
            "model_name": TEXT_EMBEDDING_MODEL,
            "vector_dim": TEXT_VECTOR_DIM,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "vector": vector,
            "updated_at": prototype.get("updated_at") or utc_now(),
        },
    )
    return path


def _prototype_vector(prototype: dict) -> list[float]:
    vector_path = _text(prototype.get("vector_path"))
    data = read_json(Path(vector_path), default={}) if vector_path else {}
    vector = (data or {}).get("vector")
    if isinstance(vector, list) and len(vector) == TEXT_VECTOR_DIM:
        return [float(value) for value in vector]
    return text_embedding(_prototype_text(prototype))


def _prototype_text(prototype: dict) -> str:
    examples = " ".join(str(item.get("title") or "") for item in prototype.get("examples") or [] if isinstance(item, dict))
    parameters = prototype.get("parameters") or {}
    title_patterns = " ".join(str(item) for item in parameters.get("title_patterns") or [])
    return " ".join(
        [
            _text(prototype.get("prototype_name")),
            " ".join(_text(item) for item in prototype.get("keywords") or []),
            examples,
            _text(parameters.get("opening_hook")),
            title_patterns,
        ]
    )


def _keyword_overlap(text: str, keywords: list[str]) -> float:
    if not text or not keywords:
        return 0.0
    normalized = text.lower()
    hits = sum(1 for keyword in keywords[:10] if _text(keyword).lower() and _text(keyword).lower() in normalized)
    return min(1.0, hits / max(3, min(10, len(keywords))))


def _account_distribution(samples: list[dict]) -> dict:
    views = sorted(float(sample.get("views") or 0) for sample in samples if float(sample.get("views") or 0) > 0)
    performance_values = sorted(_performance_value(sample) for sample in samples if _performance_value(sample) > 0)
    median_views = _percentile(views, 0.5)
    p75_views = _percentile(views, 0.75)
    p90_views = _percentile(views, 0.9)
    median_performance = _percentile(performance_values, 0.5)
    p75_performance = _percentile(performance_values, 0.75)
    p90_performance = _percentile(performance_values, 0.9)
    performance_basis = _performance_basis(samples)
    return {
        "sample_count": len(samples),
        "views_sample_count": len(views),
        "median_views": round(median_views, 2),
        "p75_views": round(p75_views, 2),
        "p90_views": round(p90_views, 2),
        "max_views": round(max(views) if views else 0.0, 2),
        "performance_basis": performance_basis,
        "performance_label": _performance_label(performance_basis),
        "median_performance": round(median_performance, 4),
        "p75_performance": round(p75_performance, 4),
        "p90_performance": round(p90_performance, 4),
        "max_performance": round(max(performance_values) if performance_values else 0.0, 4),
    }


def _absolute_level(views: float) -> dict:
    for code, label, minimum, maximum in ABSOLUTE_VIEW_LEVELS:
        if views >= minimum and (maximum is None or views < maximum):
            return {
                "code": code,
                "label": label,
                "views": round(float(views or 0), 2),
                "min_views": minimum,
                "max_views": maximum,
                "basis": "prototype_p75_views",
            }
    code, label, minimum, maximum = ABSOLUTE_VIEW_LEVELS[-1]
    return {
        "code": code,
        "label": label,
        "views": round(float(views or 0), 2),
        "min_views": minimum,
        "max_views": maximum,
        "basis": "prototype_p75_views",
    }


def _account_lift(*, p75_views: float, median_views: float, max_views: float, account_distribution: dict) -> dict:
    account_median = float(account_distribution.get("median_views") or 0)
    account_p75 = float(account_distribution.get("p75_views") or 0)
    median_lift = _safe_ratio(median_views, account_median)
    p75_lift = _safe_ratio(p75_views, account_p75)
    max_lift = _safe_ratio(max_views, account_p75)
    return {
        "account_median_views": round(account_median, 2),
        "account_p75_views": round(account_p75, 2),
        "median_lift": median_lift,
        "p75_lift": p75_lift,
        "max_vs_account_p75": max_lift,
        "label": _lift_label(p75_lift),
    }


def _stability(
    *,
    sample_count: int,
    p75_views: float,
    max_views: float,
    confidence: float,
    avg_score: float,
    performance_metric: dict | None = None,
) -> dict:
    reasons = [f"样本 {sample_count}", f"confidence {confidence:.2f}", f"score {avg_score:.1f}"]
    metric = performance_metric or {}
    if metric.get("basis") != "views" and float(metric.get("p75") or 0) > 0:
        lift = float(metric.get("p75_lift") or 0)
        reasons = [*reasons, f"{metric.get('label') or '互动热度'} P75 {float(metric.get('p75') or 0):.1f}"]
        if sample_count < 3:
            return {"key": "thin_observation", "label": "样本不足", "rank": 0, "reasons": reasons}
        if lift >= 1.25 and confidence >= 0.70:
            return {"key": "stable_high_interaction", "label": "稳定高互动", "rank": 4, "reasons": [*reasons, "高于账号同口径基线"]}
        if lift >= 1.0 and confidence >= 0.65:
            return {"key": "interaction_potential", "label": "互动潜力", "rank": 3, "reasons": [*reasons, "达到账号高位线"]}
        if lift >= 0.6:
            return {"key": "interaction_observe", "label": "互动观察", "rank": 2, "reasons": reasons}
        return {"key": "weak_or_observe", "label": "普通观察", "rank": 1, "reasons": reasons}
    if sample_count < 3:
        if max_views >= 100000:
            return {"key": "potential_observation", "label": "潜力观察", "rank": 1, "reasons": [*reasons, "样本不足 3 条但出现 10万+ 样本"]}
        return {"key": "thin_observation", "label": "样本不足", "rank": 0, "reasons": reasons}
    if p75_views >= 100000 and confidence >= 0.75:
        return {"key": "stable_high", "label": "稳定高流量", "rank": 4, "reasons": [*reasons, "P75 达到 10万+"]}
    if p75_views >= 50000 and confidence >= 0.70:
        return {"key": "high_potential", "label": "稳定高潜", "rank": 3, "reasons": [*reasons, "P75 达到 5万+"]}
    if p75_views >= 10000:
        return {"key": "category_effective", "label": "垂类有效", "rank": 2, "reasons": [*reasons, "P75 达到 1万+"]}
    return {"key": "weak_or_observe", "label": "普通观察", "rank": 1, "reasons": reasons}


def _decision_label(absolute_level: dict, account_lift: dict, stability: dict) -> str:
    lift = float(account_lift.get("p75_lift") or 0)
    lift_text = f"{lift:.1f}x" if lift > 0 else "n/a"
    basis_label = account_lift.get("label") if account_lift.get("basis") else None
    if basis_label and account_lift.get("basis") != "prototype_p75_views":
        return f"{absolute_level.get('code')} {absolute_level.get('label')} / {basis_label} {lift_text} / {stability.get('label')}"
    return f"{absolute_level.get('code')} {absolute_level.get('label')} / 账号P75 {lift_text} / {stability.get('label')}"


def _lift_label(lift: float) -> str:
    if lift >= 1.5:
        return "显著高于账号基线"
    if lift >= 1.0:
        return "高于账号基线"
    if lift >= 0.6:
        return "接近账号基线"
    if lift > 0:
        return "低于账号基线"
    return "缺少账号基线"


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator or 0) / denominator, 4)


def _source_summary(raw_samples: list[dict], selected: list[dict], source_path: str | Path | None, dataset: dict) -> dict:
    by_kind = Counter(sample.get("source_kind") or "unknown" for sample in raw_samples)
    by_dataset = Counter(sample.get("dataset_id") or dataset.get("id") or "default" for sample in raw_samples)
    by_file = Counter(sample.get("source_file") or "" for sample in raw_samples if sample.get("source_file"))
    return {
        "raw_samples": len(raw_samples),
        "selected_samples": len(selected),
        "by_kind": dict(by_kind),
        "by_dataset": dict(by_dataset),
        "by_file": dict(by_file),
        "dataset_id": dataset.get("id") or "default",
        "dataset_name": dataset.get("name") or "",
        "source_paths": dataset.get("source_paths") or [],
        "source_path": str(source_path) if source_path else "",
    }


def _next_actions(status: str, sample_count: int, prototype_count: int) -> list[str]:
    if status == "empty":
        return ["继续采集关注账号历史作品，或先同步 data/douyin_capture 下的可见作品 CSV。"]
    actions = ["把 Top 原型用于候选切片复核，优先验证相似度高且版权风险低的片段。"]
    if sample_count < 20:
        actions.append("继续补充历史作品，样本达到 20 条后原型置信度会更稳。")
    if prototype_count < 3:
        actions.append("扩展采集账号或节目类型，避免只学到单一内容模式。")
    return actions


def _engagement_rate(sample: dict) -> float:
    views = float(sample.get("views") or 0)
    if views <= 0:
        return 0.0
    return _engagement_total(sample) / views


def _engagement_total(sample: dict) -> float:
    return sum(float(sample.get(key) or 0) for key in ["likes", "comments", "favorites", "shares", "follows"])


def _performance_value(sample: dict) -> float:
    views = float(sample.get("views") or 0)
    if views > 0:
        return views
    reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0)
    if reward > 0:
        return reward
    return math.log1p(_engagement_total(sample))


def _sample_preference(sample: dict) -> tuple[float, int, str]:
    completeness = sum(1 for key in ["likes", "comments", "favorites", "shares", "views"] if float(sample.get(key) or 0) > 0)
    return (_performance_value(sample), completeness, _text(sample.get("collected_at") or sample.get("updated_at")))


def _performance_basis(samples: list[dict]) -> str:
    return "views" if any(float(sample.get("views") or 0) > 0 for sample in samples) else "reward_proxy"


def _performance_label(basis: str) -> str:
    return "播放量" if basis == "views" else "互动热度"


def _performance_metric(
    *,
    basis: str,
    label: str,
    median_value: float,
    p75_value: float,
    max_value: float,
    account_distribution: dict,
) -> dict:
    account_median = float(account_distribution.get("median_performance") or 0)
    account_p75 = float(account_distribution.get("p75_performance") or 0)
    metric = {
        "basis": basis,
        "label": label,
        "median": round(float(median_value or 0), 4),
        "p75": round(float(p75_value or 0), 4),
        "max": round(float(max_value or 0), 4),
        "account_median": round(account_median, 4),
        "account_p75": round(account_p75, 4),
        "median_lift": _safe_ratio(median_value, account_median),
        "p75_lift": _safe_ratio(p75_value, account_p75),
        "max_vs_account_p75": _safe_ratio(max_value, account_p75),
        "play_count_missing": basis != "views",
    }
    metric["lift_label"] = _lift_label(float(metric["p75_lift"]))
    return metric


def _engagement_level(metric: dict) -> dict:
    p75_value = float(metric.get("p75") or 0)
    lift = float(metric.get("p75_lift") or 0)
    if p75_value <= 0:
        code, label = "I0", "互动待学习"
    elif lift >= 1.5:
        code, label = "I4", "显著高互动"
    elif lift >= 1.0:
        code, label = "I3", "高互动"
    elif lift >= 0.6:
        code, label = "I2", "互动有效"
    else:
        code, label = "I1", "互动观察"
    return {
        "code": code,
        "label": label,
        "value": round(p75_value, 4),
        "basis": metric.get("basis") or "reward_proxy",
        "metric_label": metric.get("label") or "互动热度",
        "note": "播放量缺失，使用点赞/评论/收藏/转发生成的互动热度代理分。",
    }


def _confidence(sample_count: int, median_views: float, avg_score: float) -> float:
    count_part = min(0.45, sample_count * 0.07)
    view_part = min(0.25, math.log1p(max(0.0, median_views)) / 48.0)
    score_part = min(0.2, max(0.0, avg_score - 45.0) / 220.0)
    return round(min(0.95, 0.18 + count_part + view_part + score_part), 4)


def _publish_hour(value: str | None) -> int:
    text = _text(value)
    if not text:
        return -1
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return -1
    return int(parsed.hour)


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "")
    if "|" in text:
        parts = [_num(part) for part in text.split("|")]
        return max(parts) if parts else 0.0
    multiplier = 1.0
    if text.endswith("万"):
        multiplier = 10000.0
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100000000.0
        text = text[:-1]
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) * multiplier if match else 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _clean_item_id(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    match = re.search(r"\d{10,}", text)
    return match.group(0) if match else text


def _stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default
