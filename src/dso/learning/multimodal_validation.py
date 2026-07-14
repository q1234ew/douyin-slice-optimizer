from __future__ import annotations

import audioop
import json
import math
import os
import re
import tempfile
import wave
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.collectors.douyin_media import collect_douyin_media
from dso.db.session import connect, fetch_all
from dso.utils import clamp, run_cmd, utc_now, write_json
from dso.versions import DOUYIN_HISTORY_VERSION, MULTIMODAL_FEATURE_VERSION, MULTIMODAL_VALIDATION_VERSION, RESEARCH_RANKER_VERSION


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac"}
TEXT_SUFFIXES = {".json", ".srt", ".vtt", ".txt"}

VISUAL_KEYWORDS = ["封面", "表情", "全场", "观众", "尖叫", "掌声", "泪目", "舞台", "镜头", "reaction", "现场"]
AUDIO_KEYWORDS = ["高音", "副歌", "合唱", "清唱", "转音", "爆发", "炸", "戏腔", "和声", "开口跪"]
SPEECH_KEYWORDS = ["这句话", "金句", "点评", "为什么", "第一次", "没想到", "反转", "故事", "解释", "复盘"]
TEXT_OVERLAY_KEYWORDS = ["字幕", "歌词", "台词", "标题", "话题", "文案", "评论", "弹幕"]
RISK_KEYWORDS = ["直播", "下单", "优惠", "福利", "广告", "带货", "同款", "橱窗"]
DEFAULT_MULTIMODAL_COLLECTION_TARGET = 300
DEFAULT_MULTIMODAL_COLLECTION_MAX_STORAGE_GB = 5.0
DEFAULT_MULTIMODAL_COLLECTION_MAX_STORAGE_BYTES = int(DEFAULT_MULTIMODAL_COLLECTION_MAX_STORAGE_GB * 1024 * 1024 * 1024)
MUSIC_DOMAIN_KEYWORDS = [
    "歌", "音乐", "舞台", "副歌", "高音", "合唱", "清唱", "歌手", "唱", "乐队",
    "演唱", "声乐", "导师", "改编", "音色", "转音", "观众", "全场", "现场",
]
COLLECTION_EXCLUDE_KEYWORDS = [
    "招生", "志愿填报", "复读", "学费", "报名", "专业选择", "高考", "下单", "优惠", "团购", "福利",
]


def run_multimodal_validation(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 300,
    k: int = 10,
    min_samples: int = 100,
    min_asset_coverage: float = 0.7,
) -> dict:
    raw_rows = _fetch_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    asset_index = _build_asset_index()
    rows = _balanced_validation_rows(raw_rows, limit=max(0, int(limit or 0)), asset_index=asset_index)
    prepared = [_prepare_row(row, asset_index=asset_index) for row in rows]
    availability = _asset_readiness(prepared)
    proxy = _proxy_signal_experiment(prepared, k=max(1, int(k or 10)))
    queue = _asset_review_queue(prepared)
    gate = _validation_gate(
        sample_count=len(prepared),
        min_samples=max(1, int(min_samples or 100)),
        min_asset_coverage=max(0.0, min(1.0, float(min_asset_coverage or 0.0))),
        availability=availability,
        proxy=proxy,
    )
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "validation_version": MULTIMODAL_VALIDATION_VERSION,
        "research_ranker_version": RESEARCH_RANKER_VERSION,
        "status": _status(prepared, gate),
        "validation_mode": "offline_read_only",
        "account_id": account_id or "all",
        "dataset_id": _normalize_scope(dataset_id),
        "sample_count": len(rows),
        "evaluated_count": len(prepared),
        "query": {
            "account_id": account_id or "all",
            "dataset_id": _normalize_scope(dataset_id),
            "limit": int(limit or 0),
            "k": int(k or 10),
            "min_samples": int(min_samples or 100),
            "min_asset_coverage": float(min_asset_coverage or 0.0),
        },
        "asset_readiness": availability,
        "proxy_signal_experiment": proxy,
        "promotion_gate": gate,
        "review_queue": queue,
        "recommendations": _recommendations(availability, proxy, gate, queue),
        "generated_at": utc_now(),
    }


def run_multimodal_feature_experiment(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 300,
    k: int = 10,
    min_feature_samples: int = 60,
    audio_window_seconds: float = 10.0,
    force: bool = False,
) -> dict:
    raw_rows = _fetch_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    asset_index = _build_asset_index()
    rows = _balanced_validation_rows(raw_rows, limit=max(0, int(limit or 0)), asset_index=asset_index)
    prepared = [_prepare_row(row, asset_index=asset_index) for row in rows]
    feature_rows = []
    for row in prepared:
        features = _extract_real_multimodal_features(row, audio_window_seconds=audio_window_seconds, force=force)
        row["real_multimodal_features"] = features
        audio = features.get("audio") or {}
        visual = features.get("visual") or {}
        row["real_audio_score"] = float(audio.get("score") if audio.get("available") else 50.0)
        row["real_visual_score"] = float(visual.get("score") if visual.get("available") else 50.0)
        row["real_combined_score"] = _real_combined_score(row)
        if audio.get("available") or visual.get("available"):
            feature_rows.append(row)

    strategies = _real_feature_strategy_comparison(feature_rows, k=max(1, int(k or 10)))
    gate = _feature_experiment_gate(
        feature_rows=feature_rows,
        min_feature_samples=max(1, int(min_feature_samples or 60)),
        strategies=strategies,
    )
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "validation_version": MULTIMODAL_VALIDATION_VERSION,
        "feature_version": MULTIMODAL_FEATURE_VERSION,
        "research_ranker_version": RESEARCH_RANKER_VERSION,
        "status": "ready" if gate.get("passed") else ("low_confidence" if len(feature_rows) < int(min_feature_samples or 60) else "research_only"),
        "validation_mode": "offline_real_feature_experiment",
        "account_id": account_id or "all",
        "dataset_id": _normalize_scope(dataset_id),
        "sample_count": len(rows),
        "feature_ready_count": len(feature_rows),
        "audio_ready_count": sum(1 for row in feature_rows if (row.get("real_multimodal_features") or {}).get("audio", {}).get("available")),
        "visual_ready_count": sum(1 for row in feature_rows if (row.get("real_multimodal_features") or {}).get("visual", {}).get("available")),
        "query": {
            "account_id": account_id or "all",
            "dataset_id": _normalize_scope(dataset_id),
            "limit": int(limit or 0),
            "k": int(k or 10),
            "min_feature_samples": int(min_feature_samples or 60),
            "audio_window_seconds": float(audio_window_seconds or 10.0),
            "force": bool(force),
        },
        "feature_coverage": _feature_coverage(feature_rows, total=len(rows)),
        "strategy_comparison": strategies,
        "feature_diagnostics": _feature_diagnostics(feature_rows),
        "promotion_gate": gate,
        "recommendations": _feature_recommendations(feature_rows, strategies, gate),
        "generated_at": utc_now(),
    }


def build_multimodal_collection_plan(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 120,
    stage: str = "beta_d1",
    output_path: str | Path | None = None,
    include_ready: bool = False,
) -> dict:
    rows = _fetch_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    asset_index = _build_asset_index()
    prepared = [_prepare_row(row, asset_index=asset_index) for row in rows]
    candidates = []
    for row in prepared:
        if not _collection_candidate_allowed(row):
            continue
        source_url = _sample_source_url(row)
        aweme_id = _aweme_id(row, source_url)
        if not source_url or not aweme_id:
            continue
        assets = row.get("assets") or {}
        if not include_ready and assets.get("ready_for_multimodal"):
            continue
        missing = [field for field in ["video", "visual", "audio", "speech_or_text"] if not assets.get(field)]
        priority = _collection_priority(row, missing)
        candidates.append(
            {
                "sample_id": row.get("id") or "",
                "collection_order": 0,
                "account_id": row.get("account_id") or account_id or "main",
                "dataset_id": row.get("dataset_id") or dataset_id or "default",
                "performance_label": row.get("resolved_performance_label") or row.get("performance_label") or "",
                "aweme_id": aweme_id,
                "source_url": source_url,
                "title": row.get("title") or "",
                "stage": stage or "beta_d1",
                "priority_score": round(priority, 4),
                "missing_assets": missing,
                "recommended_collection": _recommended_collection(missing, row),
                "reward_proxy": round(float(row.get("reward_proxy") or 0.0), 4),
                "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
                "multimodal_proxy_score": round(float(row.get("multimodal_proxy_score") or 0.0), 4),
                "reason": _queue_reason(str(row.get("resolved_performance_label") or "mid"), missing, row.get("multimodal_components") or {}),
            }
        )
    selected = _balanced_plan_samples(candidates, limit=max(1, int(limit or 120)))
    for index, item in enumerate(selected, start=1):
        item["collection_order"] = index
    settings = ensure_data_dirs()
    target = Path(output_path) if output_path else settings.root / "outputs" / "beta_d1_multimodal" / f"multimodal_collection_plan_{_timestamp_slug()}.json"
    plan = {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "validation_version": MULTIMODAL_VALIDATION_VERSION,
        "plan_type": "beta_d1_multimodal_asset_collection",
        "status": "ready" if selected else "empty",
        "account_id": account_id or "all",
        "dataset_id": _normalize_scope(dataset_id),
        "stage": stage or "beta_d1",
        "sample_count": len(selected),
        "candidate_count": len(candidates),
        "filters": {
            "limit": int(limit or 120),
            "include_ready": bool(include_ready),
        },
        "summary": _plan_summary(selected),
        "samples": selected,
        "generated_at": utc_now(),
    }
    write_json(target, plan)
    return {
        **plan,
        "plan_path": str(target),
        "next_command": (
            "PYTHONPATH=src python -m dso.cli douyin-media-collect "
            f"{target} --stage {stage or 'beta_d1'} --run-id beta_d1_{_timestamp_slug()} --limit {len(selected)}"
        ),
    }


def collect_multimodal_assets(
    plan_path: str | Path | None = None,
    *,
    account_id: str | None = None,
    dataset_id: str | None = None,
    limit: int = 30,
    stage: str = "beta_d1",
    output_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    run_id: str = "",
    page_delay_seconds: int = 14,
    extra_wait_seconds: int = 5,
    extract_audio: bool = True,
    dry_run: bool = True,
    max_storage_bytes: int | None = None,
) -> dict:
    if plan_path:
        resolved_plan = Path(plan_path)
        plan = _json_file(resolved_plan, {})
    else:
        plan_result = build_multimodal_collection_plan(
            account_id=account_id,
            dataset_id=dataset_id,
            limit=limit,
            stage=stage,
        )
        resolved_plan = Path(str(plan_result["plan_path"]))
        plan = plan_result
    settings = ensure_data_dirs()
    run = run_id or f"beta_d1_{_timestamp_slug()}"
    output = Path(output_root) if output_root else settings.data_dir / "douyin_media_assets"
    reports = Path(report_dir) if report_dir else settings.root / "outputs" / "beta_d1_multimodal"
    result = collect_douyin_media(
        resolved_plan,
        stage=stage,
        account=account_id if account_id and account_id != "all" else None,
        limit=limit,
        output_root=output,
        report_dir=reports,
        run_id=run,
        page_delay_seconds=page_delay_seconds,
        extra_wait_seconds=extra_wait_seconds,
        extract_audio=extract_audio,
        dry_run=dry_run,
        max_storage_bytes=resolve_multimodal_storage_limit_bytes(max_storage_bytes=max_storage_bytes),
    )
    return {
        **result,
        "validation_version": MULTIMODAL_VALIDATION_VERSION,
        "plan_path": str(resolved_plan),
        "plan_sample_count": int(plan.get("sample_count") or len(plan.get("samples") or [])),
        "collection_mode": "dry_run" if dry_run else "read_only_browser_download",
        "safety_policy": "read-only media discovery; no follow/like/comment/publish action.",
    }


def resolve_multimodal_storage_limit_bytes(
    *,
    max_storage_bytes: int | str | None = None,
    max_storage_gb: float | str | None = None,
) -> int:
    if max_storage_bytes not in (None, ""):
        try:
            return max(0, int(max_storage_bytes))
        except (TypeError, ValueError):
            return DEFAULT_MULTIMODAL_COLLECTION_MAX_STORAGE_BYTES
    raw_gb = max_storage_gb
    if raw_gb in (None, ""):
        raw_gb = os.getenv(
            "DSO_MULTIMODAL_COLLECTION_MAX_STORAGE_GB",
            str(DEFAULT_MULTIMODAL_COLLECTION_MAX_STORAGE_GB),
        )
    try:
        storage_gb = float(raw_gb or 0)
    except (TypeError, ValueError):
        storage_gb = DEFAULT_MULTIMODAL_COLLECTION_MAX_STORAGE_GB
    return max(0, int(storage_gb * 1024 * 1024 * 1024))


def _extract_real_multimodal_features(row: dict, *, audio_window_seconds: float, force: bool = False) -> dict:
    assets = row.get("assets") or {}
    paths = assets.get("paths") if isinstance(assets.get("paths"), dict) else {}
    item_id = str(row.get("platform_item_id") or row.get("id") or "").strip() or "unknown"
    cache_path = _feature_cache_path(item_id)
    if cache_path.exists() and not force:
        cached = _json_file(cache_path, {})
        if cached.get("feature_version") == MULTIMODAL_FEATURE_VERSION:
            return cached
    features = {
        "feature_version": MULTIMODAL_FEATURE_VERSION,
        "sample_id": row.get("id") or "",
        "platform_item_id": item_id,
        "audio": _audio_feature_summary(paths, audio_window_seconds=audio_window_seconds),
        "visual": _visual_feature_summary(paths),
        "generated_at": utc_now(),
    }
    write_json(cache_path, features)
    return features


def _feature_cache_path(item_id: str) -> Path:
    settings = ensure_data_dirs()
    safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", item_id or "unknown")[:80]
    return settings.cache_dir / "multimodal_features" / f"{safe_id}.json"


def _audio_feature_summary(paths: dict, *, audio_window_seconds: float) -> dict:
    audio_paths = [Path(path) for path in paths.get("audio") or [] if Path(path).exists()]
    video_paths = [Path(path) for path in paths.get("video") or [] if Path(path).exists()]
    errors = []
    for audio_path in audio_paths:
        try:
            return _wav_feature_summary(audio_path, source="audio_asset", window_seconds=audio_window_seconds)
        except Exception as exc:
            errors.append(f"{audio_path.name}: {exc}")
    for video_path in video_paths[:1]:
        try:
            with tempfile.TemporaryDirectory(prefix="dso_mm_audio_") as tmp:
                wav_path = Path(tmp) / "window.wav"
                _extract_short_audio(video_path, wav_path, duration_seconds=audio_window_seconds)
                return _wav_feature_summary(wav_path, source="video_short_audio", window_seconds=audio_window_seconds)
        except Exception as exc:
            errors.append(f"{video_path.name}: {exc}")
    return {"available": False, "score": 50.0, "source": "missing", "errors": errors[:3]}


def _extract_short_audio(video_path: Path, wav_path: Path, *, duration_seconds: float) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-t",
            f"{max(1.0, float(duration_seconds or 10.0)):.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
    )


def _wav_feature_summary(wav_path: Path, *, source: str, window_seconds: float) -> dict:
    frames = _rms_windows(wav_path, max_duration_seconds=window_seconds, step_seconds=0.5)
    energies = [float(frame["energy"]) for frame in frames]
    if not energies:
        return {"available": False, "score": 50.0, "source": source, "errors": ["empty_audio"]}
    mean_energy = sum(energies) / len(energies)
    max_energy = max(energies)
    min_energy = min(energies)
    std_energy = _stddev(energies)
    silence_ratio = sum(1 for value in energies if value < 0.015) / max(1, len(energies))
    peak_index = max(range(len(energies)), key=lambda index: energies[index])
    peak_time = float(frames[peak_index]["time"])
    deltas = [energies[index + 1] - energies[index] for index in range(len(energies) - 1)]
    max_rise = max(deltas) if deltas else 0.0
    early = energies[: max(1, len(energies) // 3)]
    late = energies[-max(1, len(energies) // 3) :]
    energy_rise = (sum(late) / len(late)) - (sum(early) / len(early))
    score = clamp(
        max_energy * 42.0
        + mean_energy * 26.0
        + max(0.0, max_rise) * 55.0
        + max(0.0, energy_rise) * 30.0
        + (1.0 - silence_ratio) * 18.0
        + min(12.0, (max_energy / max(0.001, mean_energy)) * 2.5),
    )
    return {
        "available": True,
        "source": source,
        "score": round(score, 4),
        "window_seconds": float(window_seconds or 10.0),
        "mean_energy": round(mean_energy, 6),
        "max_energy": round(max_energy, 6),
        "min_energy": round(min_energy, 6),
        "std_energy": round(std_energy, 6),
        "silence_ratio": round(silence_ratio, 4),
        "peak_time": round(peak_time, 3),
        "max_rise": round(max_rise, 6),
        "energy_rise": round(energy_rise, 6),
        "frame_count": len(frames),
    }


def _rms_windows(wav_path: Path, *, max_duration_seconds: float, step_seconds: float) -> list[dict]:
    result = []
    with wave.open(str(wav_path), "rb") as wav:
        rate = wav.getframerate()
        width = wav.getsampwidth()
        channels = wav.getnchannels()
        max_amp = float(2 ** (8 * width - 1)) if width > 0 else 32768.0
        frames_per_window = max(1, int(rate * step_seconds))
        max_windows = max(1, int(math.ceil(float(max_duration_seconds or 10.0) / step_seconds)))
        for index in range(max_windows):
            raw = wav.readframes(frames_per_window)
            if not raw:
                break
            if channels > 1:
                raw = audioop.tomono(raw, width, 0.5, 0.5)
            rms = audioop.rms(raw, width)
            result.append({"time": round(index * step_seconds, 3), "rms": rms, "energy": round(min(1.0, rms / max_amp), 6)})
    return result


def _stddev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _visual_feature_summary(paths: dict) -> dict:
    image_paths = [Path(path) for key in ["cover", "frame"] for path in (paths.get(key) or []) if Path(path).exists()]
    if not image_paths:
        return {"available": False, "score": 50.0, "source": "missing", "errors": ["missing_visual_asset"]}
    try:
        from PIL import Image, ImageStat  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional Pillow
        return {"available": False, "score": 50.0, "source": "pillow_unavailable", "errors": [str(exc)]}
    errors = []
    for image_path in image_paths[:2]:
        try:
            with Image.open(image_path) as image:
                rgb = image.convert("RGB").resize((96, 96))
                gray = rgb.convert("L")
                hsv = rgb.convert("HSV")
                brightness = float(ImageStat.Stat(gray).mean[0]) / 255.0
                contrast = float(ImageStat.Stat(gray).stddev[0]) / 255.0
                saturation = float(ImageStat.Stat(hsv).mean[1]) / 255.0
                sharpness = _image_sharpness(gray)
                score = clamp(
                    (1.0 - min(1.0, abs(brightness - 0.56) / 0.56)) * 28.0
                    + contrast * 35.0
                    + saturation * 18.0
                    + sharpness * 28.0
                )
                return {
                    "available": True,
                    "source": "cover_or_frame",
                    "path_kind": "frame" if "/frames/" in str(image_path) else "cover",
                    "score": round(score, 4),
                    "brightness": round(brightness, 6),
                    "contrast": round(contrast, 6),
                    "saturation": round(saturation, 6),
                    "sharpness": round(sharpness, 6),
                    "width": int(image.width),
                    "height": int(image.height),
                }
        except Exception as exc:
            errors.append(f"{image_path.name}: {exc}")
    return {"available": False, "score": 50.0, "source": "decode_failed", "errors": errors[:3]}


def _image_sharpness(gray_image) -> float:
    pixels = list(gray_image.getdata())
    width, height = gray_image.size
    if width < 2 or height < 2:
        return 0.0
    diffs = []
    for y in range(0, height - 1, 2):
        offset = y * width
        next_offset = (y + 1) * width
        for x in range(0, width - 1, 2):
            value = pixels[offset + x]
            diffs.append(abs(value - pixels[offset + x + 1]))
            diffs.append(abs(value - pixels[next_offset + x]))
    if not diffs:
        return 0.0
    return min(1.0, (sum(diffs) / len(diffs)) / 64.0)


def _real_combined_score(row: dict) -> float:
    semantic = float(row.get("semantic_proxy_score") or 0.0)
    audio = float(row.get("real_audio_score") or 50.0)
    visual = float(row.get("real_visual_score") or 50.0)
    return round(clamp(semantic * 0.72 + audio * 0.15 + visual * 0.13), 4)


def _real_feature_strategy_comparison(rows: list[dict], *, k: int) -> dict[str, dict]:
    if not rows:
        return {}
    strategies = {
        "semantic_baseline": lambda row: float(row.get("semantic_proxy_score") or 0.0),
        "semantic_plus_audio": lambda row: clamp(float(row.get("semantic_proxy_score") or 0.0) * 0.84 + float(row.get("real_audio_score") or 50.0) * 0.16),
        "semantic_plus_visual": lambda row: clamp(float(row.get("semantic_proxy_score") or 0.0) * 0.84 + float(row.get("real_visual_score") or 50.0) * 0.16),
        "semantic_plus_audio_visual": lambda row: float(row.get("real_combined_score") or 0.0),
        "audio_only": lambda row: float(row.get("real_audio_score") or 50.0),
        "visual_only": lambda row: float(row.get("real_visual_score") or 50.0),
    }
    result = {name: _ranking_metrics(rows, score_fn=score_fn, k=k) for name, score_fn in strategies.items()}
    baseline_lift = float((result.get("semantic_baseline") or {}).get("topk_lift_vs_random") or 0.0)
    for item in result.values():
        item["lift_delta_vs_semantic"] = round(float(item.get("topk_lift_vs_random") or 0.0) - baseline_lift, 4)
    return result


def _feature_coverage(rows: list[dict], *, total: int) -> dict:
    audio = sum(1 for row in rows if (row.get("real_multimodal_features") or {}).get("audio", {}).get("available"))
    visual = sum(1 for row in rows if (row.get("real_multimodal_features") or {}).get("visual", {}).get("available"))
    both = sum(
        1
        for row in rows
        if (row.get("real_multimodal_features") or {}).get("audio", {}).get("available")
        and (row.get("real_multimodal_features") or {}).get("visual", {}).get("available")
    )
    return {
        "total": total,
        "feature_ready_count": len(rows),
        "feature_ready_rate": round(len(rows) / max(1, total), 4),
        "audio_ready_count": audio,
        "audio_ready_rate": round(audio / max(1, total), 4),
        "visual_ready_count": visual,
        "visual_ready_rate": round(visual / max(1, total), 4),
        "audio_visual_ready_count": both,
        "audio_visual_ready_rate": round(both / max(1, total), 4),
    }


def _feature_diagnostics(rows: list[dict]) -> dict:
    by_label: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_label[str(row.get("resolved_performance_label") or "mid")].append(row)
    label_summary = {}
    for label, items in by_label.items():
        audio_scores = [float(row.get("real_audio_score") or 50.0) for row in items]
        visual_scores = [float(row.get("real_visual_score") or 50.0) for row in items]
        combined_scores = [float(row.get("real_combined_score") or 0.0) for row in items]
        label_summary[label] = {
            "count": len(items),
            "avg_audio_score": round(sum(audio_scores) / max(1, len(audio_scores)), 4),
            "avg_visual_score": round(sum(visual_scores) / max(1, len(visual_scores)), 4),
            "avg_combined_score": round(sum(combined_scores) / max(1, len(combined_scores)), 4),
        }
    high = label_summary.get("high") or {}
    low = label_summary.get("low") or {}
    return {
        "by_label": label_summary,
        "high_low_gap": {
            "audio_score": round(float(high.get("avg_audio_score") or 0.0) - float(low.get("avg_audio_score") or 0.0), 4),
            "visual_score": round(float(high.get("avg_visual_score") or 0.0) - float(low.get("avg_visual_score") or 0.0), 4),
            "combined_score": round(float(high.get("avg_combined_score") or 0.0) - float(low.get("avg_combined_score") or 0.0), 4),
        },
        "top_feature_rows": [
            {
                **_public_sample(row),
                "real_audio_score": round(float(row.get("real_audio_score") or 0.0), 4),
                "real_visual_score": round(float(row.get("real_visual_score") or 0.0), 4),
                "real_combined_score": round(float(row.get("real_combined_score") or 0.0), 4),
            }
            for row in sorted(rows, key=lambda item: float(item.get("real_combined_score") or 0.0), reverse=True)[:8]
        ],
    }


def _feature_experiment_gate(*, feature_rows: list[dict], min_feature_samples: int, strategies: dict) -> dict:
    semantic = strategies.get("semantic_baseline") or {}
    combined = strategies.get("semantic_plus_audio_visual") or {}
    lift_delta = float(combined.get("lift_delta_vs_semantic") or 0.0)
    high_delta = float(combined.get("high_interaction_hit_rate") or 0.0) - float(semantic.get("high_interaction_hit_rate") or 0.0)
    low_delta = float(combined.get("low_interaction_avoidance_rate") or 0.0) - float(semantic.get("low_interaction_avoidance_rate") or 0.0)
    checks = {
        "min_feature_samples": len(feature_rows) >= min_feature_samples,
        "real_feature_lift_delta": lift_delta >= 0.02,
        "high_hit_not_worse": high_delta >= -0.01,
        "low_avoidance_not_worse": low_delta >= -0.01,
    }
    passed = all(checks.values())
    if len(feature_rows) < min_feature_samples:
        decision = "collect_or_extract_more_features"
    elif lift_delta < 0.02:
        decision = "keep_as_research_only"
    else:
        decision = "ready_for_weight_search"
    return {
        "passed": passed,
        "decision": decision,
        "feature_ready_count": len(feature_rows),
        "required_feature_ready_count": min_feature_samples,
        "lift_delta_vs_semantic": round(lift_delta, 4),
        "high_hit_delta": round(high_delta, 4),
        "low_avoidance_delta": round(low_delta, 4),
        "checks": checks,
        "note": "Passing this gate only allows lightweight feature weight search; it does not change production ranking weights.",
    }


def _feature_recommendations(rows: list[dict], strategies: dict, gate: dict) -> list[str]:
    recs = []
    if gate.get("decision") == "collect_or_extract_more_features":
        recs.append("真实特征样本仍偏少，优先补齐已有视频的短窗音频和封面/首帧特征。")
    combined = strategies.get("semantic_plus_audio_visual") or {}
    audio = strategies.get("semantic_plus_audio") or {}
    visual = strategies.get("semantic_plus_visual") or {}
    if float(combined.get("lift_delta_vs_semantic") or 0.0) >= 0.02:
        recs.append("音频+视觉轻量特征相对语义基线有正增益，可进入解释权重搜索。")
    else:
        recs.append("真实轻量特征当前未证明优于语义基线，继续作为研究诊断，不进入强排序权重。")
    best = max(
        [
            ("audio", float(audio.get("lift_delta_vs_semantic") or 0.0)),
            ("visual", float(visual.get("lift_delta_vs_semantic") or 0.0)),
            ("audio_visual", float(combined.get("lift_delta_vs_semantic") or 0.0)),
        ],
        key=lambda item: item[1],
    )
    recs.append(f"当前最佳真实特征组为 {best[0]}，相对语义 lift {best[1]:+.4f}。")
    if rows and sum(1 for row in rows if (row.get("real_multimodal_features") or {}).get("audio", {}).get("available")) / max(1, len(rows)) < 0.5:
        recs.append("音频特征覆盖仍不足，下一轮优先对已有视频批量抽 10 秒短窗音频特征。")
    return recs


def _fetch_rows(account_id: str | None, dataset_id: str | None, limit: int) -> list[dict]:
    clauses = ["(COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0)"]
    params: list[Any] = []
    account = str(account_id or "").strip()
    dataset = str(dataset_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if dataset and dataset.lower() != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset)
    query = f"""
        SELECT id, account_id, dataset_id, platform_item_id, sample_key, title,
               platform_url, likes, comments, favorites, shares, reward_proxy,
               normalized_reward, performance_label, label_reason, content_category,
               hook_type, slice_structure, structure_confidence, structure_evidence,
               structure_unknown_reason, program_name, artist_names, song_title, tags,
               duration_seconds, media_type, classification_confidence, published_at,
               collected_at, raw_json, updated_at
        FROM historical_capture_samples
        WHERE {' AND '.join(clauses)}
        ORDER BY
          CASE performance_label WHEN 'high' THEN 0 WHEN 'low' THEN 1 ELSE 2 END,
          COALESCE(normalized_reward, reward_proxy, 0) DESC,
          updated_at DESC
    """
    if int(limit or 0) > 0:
        query += " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        return fetch_all(conn, query, params)


def _prepare_row(row: dict, *, asset_index: dict[str, dict[str, list[str]]]) -> dict:
    raw = _json_field(row.get("raw_json"), {})
    assets = _asset_availability(row, raw, asset_index=asset_index)
    text = _row_text(row, raw)
    label = _performance_label(row)
    semantic_score = _semantic_proxy_score(row, text)
    multimodal = _multimodal_proxy_components(row, text, assets)
    multimodal_score = clamp(
        semantic_score * 0.72
        + multimodal["visual_moment"] * 0.12
        + multimodal["audio_moment"] * 0.14
        + multimodal["speech_context"] * 0.08
        + multimodal["text_overlay"] * 0.05
        + multimodal["asset_bonus"] * 0.06
        - multimodal["risk_penalty"] * 0.10
    )
    return {
        **row,
        "raw": raw,
        "text": text,
        "resolved_performance_label": label,
        "reward_value": _reward(row),
        "assets": assets,
        "semantic_proxy_score": round(semantic_score, 4),
        "multimodal_proxy_score": round(multimodal_score, 4),
        "multimodal_components": {key: round(value, 4) for key, value in multimodal.items()},
    }


def _asset_availability(row: dict, raw: dict, *, asset_index: dict[str, dict[str, list[str]]]) -> dict:
    paths = _extract_paths(raw)
    item_id = str(row.get("platform_item_id") or "").strip()
    if item_id and item_id in asset_index:
        for key, values in asset_index[item_id].items():
            paths.setdefault(key, []).extend(values)
    resolved = {key: _existing_paths(values) for key, values in paths.items()}
    has_visual = bool(resolved.get("cover") or resolved.get("frame"))
    has_speech = bool(resolved.get("transcript") or resolved.get("ocr"))
    return {
        "video": bool(resolved.get("video")),
        "cover": bool(resolved.get("cover")),
        "frame": bool(resolved.get("frame")),
        "audio": bool(resolved.get("audio")),
        "transcript": bool(resolved.get("transcript")),
        "ocr": bool(resolved.get("ocr")),
        "features": bool(resolved.get("features")),
        "visual": has_visual,
        "speech_or_text": has_speech,
        "ready_for_multimodal": bool(resolved.get("video") and (has_visual or resolved.get("audio") or has_speech)),
        "paths": {key: values[:3] for key, values in resolved.items() if values},
    }


def _asset_readiness(rows: list[dict]) -> dict:
    total = len(rows)
    fields = ["video", "cover", "frame", "visual", "audio", "transcript", "ocr", "speech_or_text", "features", "ready_for_multimodal"]
    coverage = {}
    for field in fields:
        count = sum(1 for row in rows if row.get("assets", {}).get(field))
        coverage[field] = {"count": count, "total": total, "rate": round(count / max(1, total), 4)}
    by_label: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "ready": 0, "video": 0, "audio": 0, "visual": 0})
    for row in rows:
        label = str(row.get("resolved_performance_label") or "mid")
        assets = row.get("assets") or {}
        bucket = by_label[label]
        bucket["total"] += 1
        bucket["ready"] += int(bool(assets.get("ready_for_multimodal")))
        bucket["video"] += int(bool(assets.get("video")))
        bucket["audio"] += int(bool(assets.get("audio")))
        bucket["visual"] += int(bool(assets.get("visual")))
    return {
        "sample_count": total,
        "coverage": coverage,
        "by_label": {
            key: {
                **value,
                "ready_rate": round(value["ready"] / max(1, value["total"]), 4),
                "video_rate": round(value["video"] / max(1, value["total"]), 4),
                "audio_rate": round(value["audio"] / max(1, value["total"]), 4),
                "visual_rate": round(value["visual"] / max(1, value["total"]), 4),
            }
            for key, value in sorted(by_label.items())
        },
        "asset_policy": "local paths and collected media reports only; no platform fetch is performed.",
    }


def _proxy_signal_experiment(rows: list[dict], *, k: int) -> dict:
    baseline = _ranking_metrics(rows, score_key="semantic_proxy_score", k=k)
    multimodal = _ranking_metrics(rows, score_key="multimodal_proxy_score", k=k)
    component_rows = []
    for component in ["visual_moment", "audio_moment", "speech_context", "text_overlay", "asset_bonus", "risk_penalty"]:
        metrics = _ranking_metrics(
            rows,
            score_fn=lambda row, key=component: float((row.get("multimodal_components") or {}).get(key) or 0.0)
            if key != "risk_penalty"
            else 100.0 - float((row.get("multimodal_components") or {}).get(key) or 0.0),
            k=k,
        )
        component_rows.append(
            {
                "feature_group": component,
                "topk_lift_vs_random": metrics["topk_lift_vs_random"],
                "high_interaction_hit_rate": metrics["high_interaction_hit_rate"],
                "low_interaction_avoidance_rate": metrics["low_interaction_avoidance_rate"],
            }
        )
    component_rows.sort(key=lambda item: float(item.get("topk_lift_vs_random") or 0.0), reverse=True)
    return {
        "baseline": baseline,
        "multimodal_proxy": multimodal,
        "lift_delta": round(float(multimodal["topk_lift_vs_random"]) - float(baseline["topk_lift_vs_random"]), 4),
        "high_hit_delta": round(float(multimodal["high_interaction_hit_rate"]) - float(baseline["high_interaction_hit_rate"]), 4),
        "low_avoidance_delta": round(float(multimodal["low_interaction_avoidance_rate"]) - float(baseline["low_interaction_avoidance_rate"]), 4),
        "feature_group_ablation": component_rows,
        "useful_signal_groups": [row["feature_group"] for row in component_rows if float(row.get("topk_lift_vs_random") or 0) >= float(baseline["topk_lift_vs_random"])],
        "caveat": "Proxy signals use metadata text and local asset availability; this is not a production multimodal model.",
    }


def _ranking_metrics(rows: list[dict], *, k: int, score_key: str | None = None, score_fn=None) -> dict:
    if not rows:
        return _empty_metrics(k)
    scored = []
    for row in rows:
        score = float(score_fn(row) if score_fn else row.get(score_key or "") or 0.0)
        scored.append((score, _stable_rank_tiebreak(row), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    top = [row for _score, _key, row in scored[: max(1, min(k, len(scored)))]]
    avg_reward = sum(float(row.get("reward_value") or 0.0) for row in top) / max(1, len(top))
    random_avg = sum(float(row.get("reward_value") or 0.0) for row in rows) / max(1, len(rows))
    high_rows = [row for row in rows if row.get("resolved_performance_label") == "high"]
    low_rows = [row for row in rows if row.get("resolved_performance_label") == "low"]
    high_in_top = sum(1 for row in top if row.get("resolved_performance_label") == "high")
    low_in_top = sum(1 for row in top if row.get("resolved_performance_label") == "low")
    return {
        "sample_count": len(rows),
        "k": k,
        "topk_avg_reward": round(avg_reward, 4),
        "random_avg_reward": round(random_avg, 4),
        "topk_lift_vs_random": round(avg_reward / max(1e-6, random_avg), 4),
        "high_interaction_hit_rate": round(high_in_top / max(1, min(k, len(high_rows))), 4),
        "low_interaction_avoidance_rate": round(1.0 - low_in_top / max(1, min(k, len(low_rows))), 4),
        "high_in_topk": high_in_top,
        "low_in_topk": low_in_top,
        "top_rows": [_public_sample(row) for row in top[:5]],
    }


def _stable_rank_tiebreak(row: dict) -> str:
    return str(row.get("sample_key") or row.get("platform_item_id") or row.get("id") or "")


def _asset_review_queue(rows: list[dict], *, limit: int = 30) -> list[dict]:
    queue = []
    for row in rows:
        assets = row.get("assets") or {}
        missing = [
            field
            for field in ["video", "visual", "audio", "speech_or_text", "ocr"]
            if not assets.get(field)
        ]
        if not missing:
            continue
        label = str(row.get("resolved_performance_label") or "mid")
        components = row.get("multimodal_components") or {}
        impact = 30.0 if label == "high" else 24.0 if label == "low" else 12.0
        signal_need = max(
            float(components.get("visual_moment") or 0.0),
            float(components.get("audio_moment") or 0.0),
            float(components.get("speech_context") or 0.0),
        )
        priority = clamp(impact + signal_need * 0.35 + float(row.get("reward_value") or 0.0) * 0.12)
        queue.append(
            {
                **_public_sample(row),
                "missing_assets": missing,
                "available_assets": [field for field in ["video", "visual", "audio", "speech_or_text", "ocr"] if assets.get(field)],
                "recommended_collection": _recommended_collection(missing, row),
                "priority_score": round(priority, 4),
                "reason": _queue_reason(label, missing, components),
            }
        )
    queue.sort(key=lambda item: float(item.get("priority_score") or 0.0), reverse=True)
    return queue[:limit]


def _collection_priority(row: dict, missing: list[str]) -> float:
    label = str(row.get("resolved_performance_label") or "mid")
    impact = 36.0 if label == "high" else 32.0 if label == "low" else 16.0
    components = row.get("multimodal_components") or {}
    signal = max([float(value or 0.0) for value in components.values()] or [0.0])
    missing_weight = len(missing) * 7.0
    reward = float(row.get("reward_value") or 0.0)
    return clamp(impact + signal * 0.25 + missing_weight + reward * 0.10)


def _collection_candidate_allowed(row: dict) -> bool:
    text = str(row.get("text") or row.get("title") or "")
    lowered = text.lower()
    if float(row.get("reward_proxy") or 0.0) <= 0 and _engagement_count(row) <= 0:
        return False
    if any(keyword.lower() in lowered for keyword in COLLECTION_EXCLUDE_KEYWORDS):
        return False
    if any(keyword.lower() in lowered for keyword in MUSIC_DOMAIN_KEYWORDS):
        return True
    if _known(row.get("artist_names")) or _known(row.get("song_title")):
        return True
    category = str(row.get("content_category") or "").lower()
    hook = str(row.get("hook_type") or "").lower()
    return any(token in category or token in hook for token in ["music", "song", "stage", "performance", "vocal", "sing"])


def _engagement_count(row: dict) -> float:
    return sum(float(row.get(field) or 0.0) for field in ["likes", "comments", "favorites", "shares"])


def _balanced_plan_samples(candidates: list[dict], *, limit: int) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in candidates:
        buckets[str(item.get("performance_label") or "mid")].append(item)
    for rows in buckets.values():
        rows.sort(key=lambda item: float(item.get("priority_score") or 0.0), reverse=True)
    selected = []
    order = ["high", "low", "mid"]
    while len(selected) < limit:
        progressed = False
        for label in order:
            if buckets[label]:
                selected.append(buckets[label].pop(0))
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    selected.sort(key=lambda item: float(item.get("priority_score") or 0.0), reverse=True)
    return selected[:limit]


def _balanced_validation_rows(rows: list[dict], *, limit: int, asset_index: dict[str, dict[str, list[str]]] | None = None) -> list[dict]:
    if limit <= 0 or len(rows) <= limit:
        return rows
    asset_index = asset_index or {}
    selected = []
    selected_keys = set()
    asset_rows = [row for row in rows if str(row.get("platform_item_id") or "") in asset_index]
    asset_rows.sort(
        key=lambda item: (
            {"high": 2, "low": 2, "mid": 1}.get(_performance_label(item), 0),
            float(item.get("normalized_reward") or item.get("reward_proxy") or 0.0),
        ),
        reverse=True,
    )
    for row in asset_rows[:limit]:
        key = str(row.get("id") or row.get("platform_item_id") or "")
        if key in selected_keys:
            continue
        selected.append(row)
        selected_keys.add(key)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = str(row.get("id") or row.get("platform_item_id") or "")
        if key in selected_keys:
            continue
        buckets[_performance_label(row)].append(row)
    for items in buckets.values():
        items.sort(key=lambda item: float(item.get("normalized_reward") or item.get("reward_proxy") or 0.0), reverse=True)
    order = ["high", "low", "mid"]
    while len(selected) < limit:
        progressed = False
        for label in order:
            if buckets[label]:
                selected.append(buckets[label].pop(0))
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    if len(selected) < limit:
        remaining = [item for bucket in buckets.values() for item in bucket]
        remaining.sort(key=lambda item: float(item.get("normalized_reward") or item.get("reward_proxy") or 0.0), reverse=True)
        selected.extend(remaining[: limit - len(selected)])
    return selected[:limit]


def _plan_summary(samples: list[dict]) -> dict:
    labels = Counter(str(item.get("performance_label") or "mid") for item in samples)
    missing = Counter(field for item in samples for field in item.get("missing_assets") or [])
    accounts = Counter(str(item.get("account_id") or "main") for item in samples)
    return {
        "by_label": dict(labels),
        "missing_assets": dict(missing),
        "by_account": dict(accounts),
        "ready_to_collect": len(samples),
    }


def _validation_gate(
    *,
    sample_count: int,
    min_samples: int,
    min_asset_coverage: float,
    availability: dict,
    proxy: dict,
) -> dict:
    coverage = availability.get("coverage") or {}
    ready_rate = float((coverage.get("ready_for_multimodal") or {}).get("rate") or 0.0)
    lift_delta = float(proxy.get("lift_delta") or 0.0)
    high_delta = float(proxy.get("high_hit_delta") or 0.0)
    low_delta = float(proxy.get("low_avoidance_delta") or 0.0)
    checks = {
        "min_samples": sample_count >= min_samples,
        "asset_coverage": ready_rate >= min_asset_coverage,
        "proxy_lift_delta": lift_delta >= 0.02,
        "high_hit_not_worse": high_delta >= -0.01,
        "low_avoidance_not_worse": low_delta >= -0.01,
    }
    passed = all(checks.values())
    if sample_count <= 0:
        decision = "empty"
    elif sample_count < min_samples:
        decision = "expand_validation_cohort"
    elif ready_rate < min_asset_coverage:
        decision = "collect_assets_first"
    elif not checks["proxy_lift_delta"]:
        decision = "keep_as_research_only"
    else:
        decision = "ready_for_beta_d2"
    return {
        "passed": passed,
        "decision": decision,
        "scope": "offline_validation_only",
        "required_sample_count": min_samples,
        "sample_count": sample_count,
        "required_asset_coverage": min_asset_coverage,
        "asset_coverage_rate": round(ready_rate, 4),
        "required_proxy_lift_delta": 0.02,
        "proxy_lift_delta": round(lift_delta, 4),
        "high_hit_delta": round(high_delta, 4),
        "low_avoidance_delta": round(low_delta, 4),
        "checks": checks,
        "note": "Passing this gate only allows a Beta-D-2 controlled experiment; it does not change production ranking weights.",
    }


def _status(rows: list[dict], gate: dict) -> str:
    if not rows:
        return "empty"
    if gate.get("passed"):
        return "ready"
    if gate.get("decision") in {"expand_validation_cohort", "collect_assets_first"}:
        return "low_confidence"
    return "research_only"


def _recommendations(availability: dict, proxy: dict, gate: dict, queue: list[dict]) -> list[str]:
    coverage = availability.get("coverage") or {}
    ready_rate = float((coverage.get("ready_for_multimodal") or {}).get("rate") or 0.0)
    visual_rate = float((coverage.get("visual") or {}).get("rate") or 0.0)
    audio_rate = float((coverage.get("audio") or {}).get("rate") or 0.0)
    lift_delta = float(proxy.get("lift_delta") or 0.0)
    recs = []
    if gate.get("decision") == "collect_assets_first":
        recs.append("优先补齐 review_queue 中高互动和低互动风险样本的视频、封面/抽帧和音频素材。")
    if visual_rate < 0.7:
        recs.append("视觉素材覆盖不足，先固化封面/首帧抽取，避免直接训练视觉模型。")
    if audio_rate < 0.7:
        recs.append("音频素材覆盖不足，先补 3-5 秒音频能量和副歌/高音代理特征。")
    if lift_delta >= 0.02:
        recs.append("代理实验显示多模态方向有增益，可进入小样本人工核验与特征实测。")
    else:
        recs.append("代理实验尚未证明稳定增益，多模态暂不进入排序强权重。")
    if queue:
        recs.append(f"下一批建议采集 {min(len(queue), 30)} 条高影响样本，并按缺失素材类型分批处理。")
    recs.append(f"当前多模态 ready 覆盖为 {ready_rate:.0%}，本轮结论只作为 Beta-D-1 离线验证。")
    return recs


def _semantic_proxy_score(row: dict, text: str) -> float:
    score = 38.0
    for field, weight in [
        ("content_category", 10.0),
        ("hook_type", 9.0),
        ("slice_structure", 8.0),
        ("artist_names", 6.0),
        ("song_title", 5.0),
        ("entity_signal", 5.0),
    ]:
        if _known(row.get(field)):
            score += weight
    confidence = str(row.get("classification_confidence") or "").lower()
    if confidence == "manual_verified":
        score += 12.0
    elif confidence in {"high", "medium"}:
        score += 5.0
    score += min(12.0, _keyword_score(text, AUDIO_KEYWORDS + VISUAL_KEYWORDS + SPEECH_KEYWORDS) * 0.4)
    score -= min(18.0, _keyword_score(text, RISK_KEYWORDS) * 0.8)
    return clamp(score)


def _multimodal_proxy_components(row: dict, text: str, assets: dict) -> dict[str, float]:
    visual = _keyword_score(text, VISUAL_KEYWORDS) + (18.0 if assets.get("visual") else 0.0)
    audio = _keyword_score(text, AUDIO_KEYWORDS) + (18.0 if assets.get("audio") else 0.0)
    speech = _keyword_score(text, SPEECH_KEYWORDS) + (12.0 if assets.get("transcript") else 0.0)
    overlay = _keyword_score(text, TEXT_OVERLAY_KEYWORDS) + (12.0 if assets.get("ocr") else 0.0)
    asset_bonus = (
        (18.0 if assets.get("video") else 0.0)
        + (12.0 if assets.get("visual") else 0.0)
        + (12.0 if assets.get("audio") else 0.0)
        + (8.0 if assets.get("speech_or_text") else 0.0)
    )
    risk = _keyword_score(text, RISK_KEYWORDS)
    return {
        "visual_moment": clamp(visual),
        "audio_moment": clamp(audio),
        "speech_context": clamp(speech),
        "text_overlay": clamp(overlay),
        "asset_bonus": clamp(asset_bonus),
        "risk_penalty": clamp(risk),
    }


def _keyword_score(text: str, keywords: list[str]) -> float:
    lower = text.lower()
    score = 0.0
    seen = set()
    for keyword in keywords:
        probe = keyword.lower() if re.search(r"[A-Za-z]", keyword) else keyword
        target = lower if re.search(r"[A-Za-z]", keyword) else text
        if probe in target and keyword not in seen:
            score += 16.0
            seen.add(keyword)
    return min(100.0, score)


def _extract_paths(raw: Any) -> dict[str, list[str]]:
    paths: dict[str, list[str]] = defaultdict(list)

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
            return
        if isinstance(value, list):
            for item in value:
                visit(item, key)
            return
        if not isinstance(value, str) or not value.strip():
            return
        text = value.strip()
        suffix = Path(text.split("?", 1)[0]).suffix.lower()
        key_lower = key.lower()
        category = ""
        if "video" in key_lower or suffix in VIDEO_SUFFIXES:
            category = "video"
        elif "cover" in key_lower or "poster" in key_lower:
            category = "cover"
        elif "frame" in key_lower or "shot" in key_lower:
            category = "frame"
        elif "audio" in key_lower or "wav" in key_lower or suffix in AUDIO_SUFFIXES:
            category = "audio"
        elif "ocr" in key_lower:
            category = "ocr"
        elif "transcript" in key_lower or "subtitle" in key_lower or "asr" in key_lower:
            category = "transcript"
        elif "feature" in key_lower:
            category = "features"
        elif suffix in IMAGE_SUFFIXES:
            category = "cover"
        elif suffix in TEXT_SUFFIXES:
            category = "transcript"
        if category:
            paths[category].append(text)

    visit(raw)
    return dict(paths)


def _existing_paths(values: list[str]) -> list[str]:
    settings = ensure_data_dirs()
    candidates = []
    for value in values:
        text = str(value or "").strip()
        if not text or text.startswith(("http://", "https://")):
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = settings.root / path
        if path.exists() and path.is_file():
            candidates.append(str(path.resolve()))
    return sorted(set(candidates))


def _build_asset_index() -> dict[str, dict[str, list[str]]]:
    settings = ensure_data_dirs()
    roots = [settings.data_dir / "douyin_media_assets", settings.root / "outputs" / "v0.7_media_collection_test"]
    index: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            category = _path_category(path)
            if not category:
                continue
            ids = re.findall(r"\d{10,24}", str(path))
            for item_id in ids[-2:]:
                index[item_id][category].append(str(path.resolve()))
    return {key: dict(value) for key, value in index.items()}


def _path_category(path: Path) -> str:
    suffix = path.suffix.lower()
    parts = {part.lower() for part in path.parts}
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in IMAGE_SUFFIXES:
        if "frames" in parts:
            return "frame"
        return "cover"
    if suffix in TEXT_SUFFIXES:
        if "ocr" in parts:
            return "ocr"
        if "features" in parts:
            return "features"
        return "transcript"
    return ""


def _public_sample(row: dict) -> dict:
    return {
        "sample_id": row.get("id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "default",
        "platform_item_id": row.get("platform_item_id") or "",
        "title": row.get("title") or "",
        "performance_label": row.get("resolved_performance_label") or row.get("performance_label") or "",
        "reward_proxy": round(float(row.get("reward_proxy") or 0.0), 4),
        "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
        "semantic_proxy_score": round(float(row.get("semantic_proxy_score") or 0.0), 4),
        "multimodal_proxy_score": round(float(row.get("multimodal_proxy_score") or 0.0), 4),
    }


def _recommended_collection(missing: list[str], row: dict) -> list[str]:
    actions = []
    if "video" in missing:
        actions.append("collect_video_file")
    if "visual" in missing:
        actions.append("extract_cover_or_first_frame")
    if "audio" in missing:
        actions.append("extract_audio_window")
    if "speech_or_text" in missing:
        actions.append("collect_asr_or_title_ocr")
    if "ocr" in missing and _keyword_score(str(row.get("text") or ""), TEXT_OVERLAY_KEYWORDS) > 0:
        actions.append("run_cover_ocr")
    return actions


def _queue_reason(label: str, missing: list[str], components: dict) -> str:
    if label == "high":
        return f"高互动样本缺少 {', '.join(missing[:3])}，适合作为多模态正例补采。"
    if label == "low":
        return f"低互动风险样本缺少 {', '.join(missing[:3])}，适合作为误推避让负例。"
    top_component = max(components.items(), key=lambda item: float(item[1] or 0.0))[0] if components else "multimodal"
    return f"代理信号 {top_component} 较强但素材不完整，建议进入小样本验证。"


def _row_text(row: dict, raw: dict) -> str:
    classification = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    values = [
        row.get("title"),
        row.get("tags"),
        row.get("program_name"),
        row.get("content_category"),
        row.get("hook_type"),
        row.get("slice_structure"),
        row.get("structure_evidence"),
        row.get("artist_names"),
        row.get("song_title"),
        classification.get("hook_type") if isinstance(classification, dict) else "",
        classification.get("slice_structure") if isinstance(classification, dict) else "",
        classification.get("structure_evidence") if isinstance(classification, dict) else "",
    ]
    return " ".join(str(value or "") for value in values)


def _performance_label(row: dict) -> str:
    label = str(row.get("performance_label") or "").strip().lower()
    if label in {"high", "mid", "low"}:
        return label
    reward = _reward(row)
    if reward >= 75:
        return "high"
    if reward <= 35:
        return "low"
    return "mid"


def _reward(row: dict) -> float:
    return float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0)


def _known(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and text not in {"unknown", "none", "null", "其他", "其它", "0", "[]"})


def _json_field(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def _json_file(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _sample_source_url(row: dict) -> str:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else _json_field(row.get("raw_json"), {})
    candidates = [
        row.get("platform_url"),
        raw.get("platform_url") if isinstance(raw, dict) else "",
        raw.get("source_url") if isinstance(raw, dict) else "",
        raw.get("share_url") if isinstance(raw, dict) else "",
        raw.get("url") if isinstance(raw, dict) else "",
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text.startswith(("http://", "https://")):
            return text
    aweme_id = _aweme_id(row, "")
    if aweme_id:
        return f"https://www.douyin.com/video/{aweme_id}"
    return ""


def _aweme_id(row: dict, source_url: str) -> str:
    for value in [row.get("platform_item_id"), row.get("sample_key"), source_url]:
        text = str(value or "").strip()
        match = re.search(r"(\d{10,24})", text)
        if match:
            return match.group(1)
    return ""


def _timestamp_slug() -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", utc_now())[:15]


def _normalize_scope(value: str | None) -> str:
    text = str(value or "").strip()
    return text if text and text.lower() != "all" else "all"


def _empty_metrics(k: int) -> dict:
    return {
        "sample_count": 0,
        "k": k,
        "topk_avg_reward": 0.0,
        "random_avg_reward": 0.0,
        "topk_lift_vs_random": 0.0,
        "high_interaction_hit_rate": 0.0,
        "low_interaction_avoidance_rate": 0.0,
        "high_in_topk": 0,
        "low_in_topk": 0,
        "top_rows": [],
    }
