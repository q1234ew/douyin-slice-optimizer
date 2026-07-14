from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.features.asr import transcribe_audio_file
from dso.learning.material_calibration import material_gold_annotation_index
from dso.learning.material_confusion import MATERIAL_CONFUSION_PAIRS, material_confusion_queue
from dso.learning.material_taxonomy import canonical_material_type, material_type_taxonomy_relation
from dso.learning.qwen_omni import (
    OMNI_PROGRAM_CONTEXTS,
    QWEN_OMNI_MODEL,
    QwenOmniClient,
    _omni_window_plan,
)
from dso.media.ffmpeg import extract_audio, extract_frame, probe_video
from dso.utils import read_json, run_cmd, utc_now, write_json


MATERIAL_EVIDENCE_VERSION = "material_evidence.d10b.v1"
MATERIAL_RESOLVER_VERSION = "material_confusion_resolver.shadow_v1"
DEFAULT_EVIDENCE_WINDOW_SECONDS = 8.0
DEFAULT_EVIDENCE_LIMIT = 10
D10B_VIDEO_FPS = 2
D10B_VIDEO_MAX_WIDTH = 448
EVIDENCE_SIGNAL_LABELS_ZH = {
    "teaching_instruction": "教学指令",
    "viewing_reaction": "观看反应",
    "list_structure": "盘点结构",
    "news_narration": "资讯叙述",
    "backstage_context": "幕后语境",
    "sustained_performance": "持续演唱",
}

_SIGNAL_CUES: dict[str, list[str]] = {
    "teaching_instruction": [
        "教学", "怎么唱", "如何唱", "你要", "需要注意", "练习", "示范", "气息", "发声",
        "声带", "共鸣", "咬字", "音准", "混声", "头声", "胸声", "唱法", "技巧",
    ],
    "viewing_reaction": [
        "reaction", "听完", "看完", "我的反应", "我觉得", "点评", "锐评", "复盘", "解析",
        "没想到", "居然", "太强", "逐帧", "带你看",
    ],
    "list_structure": [
        "盘点", "合集", "排名", "top", "第一个", "第二个", "第三个", "第一名", "第二名",
        "一口气", "汇总", "名场面", "分别", "上篇", "下篇", "十大",
    ],
    "news_narration": [
        "资讯", "近日", "今天", "最新", "消息", "官宣", "回应", "宣布", "事件", "争议",
        "爆料", "曝光", "近况", "引发", "登上热搜", "据悉", "媒体报道",
    ],
    "backstage_context": [
        "幕后", "后台", "花絮", "排练", "彩排", "候场", "采访", "准备中", "上台前",
        "下台后", "化妆间", "休息室", "感言",
    ],
    "sustained_performance": [
        "现场演唱", "舞台直拍", "演唱会", "live", "清唱", "合唱", "副歌", "高音", "开口跪",
        "全场大合唱", "音乐现场", "完整演唱", "唱完", "舞台表演",
    ],
}

_PAIR_SIGNAL_MAP: dict[str, tuple[list[str], list[str]]] = {
    "reaction_vocal_teaching": (["viewing_reaction"], ["teaching_instruction"]),
    "reaction_compilation": (["viewing_reaction"], ["list_structure"]),
    "compilation_entertainment_news": (["list_structure"], ["news_narration"]),
    "behind_the_scenes_performance": (["backstage_context"], ["sustained_performance"]),
    "performance_program_context": (["sustained_performance"], []),
}


def material_evidence_status(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    confusion_pair: str | None = None,
    limit: int = 80,
    include_reviewed: bool = True,
) -> dict:
    queue = material_confusion_queue(
        account_id=account_id,
        dataset_id=dataset_id,
        confusion_pair=confusion_pair,
        limit=limit,
        local_media_only=True,
        include_reviewed=include_reviewed,
    )
    cache = material_evidence_cache_index()
    samples = []
    for sample in queue.get("samples") or []:
        sample_id = str(sample.get("sample_id") or "")
        record = cache.get(sample_id) or {}
        samples.append(_evidence_status_sample(sample, record))
    summary = _evidence_coverage_summary(samples)
    latest_resolver = read_json(_latest_resolver_path(), {})
    return {
        "contract_version": MATERIAL_EVIDENCE_VERSION,
        "resolver_version": MATERIAL_RESOLVER_VERSION,
        "status": "ready" if summary["cached_count"] else "not_started",
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "confusion_pair": confusion_pair or "all",
        "include_reviewed": bool(include_reviewed),
        "queue_count": len(samples),
        "batch_summary": summary,
        "samples": samples,
        "latest_resolver_summary": (latest_resolver or {}).get("summary") or {},
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def run_material_evidence_batch(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    confusion_pair: str | None = None,
    limit: int = DEFAULT_EVIDENCE_LIMIT,
    window_seconds: float = DEFAULT_EVIDENCE_WINDOW_SECONDS,
    run_asr: bool = True,
    run_ocr: bool = True,
    run_omni: bool = True,
    load_model: bool = False,
    force: bool = False,
    include_reviewed: bool = True,
    sample_ids: list[str] | None = None,
    output_path: str | Path | None = None,
    client: QwenOmniClient | None = None,
) -> dict:
    cap = max(1, min(100, int(limit or DEFAULT_EVIDENCE_LIMIT)))
    seconds = max(4.0, min(15.0, float(window_seconds or DEFAULT_EVIDENCE_WINDOW_SECONDS)))
    requested_ids = {str(value).strip() for value in (sample_ids or []) if str(value).strip()}
    queue = material_confusion_queue(
        account_id=account_id,
        dataset_id=dataset_id,
        confusion_pair=confusion_pair,
        limit=100 if requested_ids else cap,
        local_media_only=True,
        include_reviewed=include_reviewed,
    )
    samples = [item for item in queue.get("samples") or [] if not requested_ids or str(item.get("sample_id") or "") in requested_ids]
    samples = samples[:cap]
    client = client or QwenOmniClient()
    service_status = {"status": "disabled"}
    if run_omni:
        service_status = client.load(max_clip_seconds=seconds) if load_model else client.health()
    report_path = Path(output_path) if output_path else _batch_report_path()
    results: list[dict] = []
    counts: Counter[str] = Counter()
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        try:
            item = _extract_sample_evidence(
                sample,
                window_seconds=seconds,
                run_asr=run_asr,
                run_ocr=run_ocr,
                run_omni=run_omni,
                force=force,
                client=client,
                service_status=service_status,
            )
            counts[str(item.get("status") or "unknown")] += 1
        except Exception as exc:
            item = {
                "contract_version": MATERIAL_EVIDENCE_VERSION,
                "sample_id": sample_id,
                "account_id": sample.get("account_id") or "",
                "title": sample.get("title") or "",
                "confusion_pair": sample.get("confusion_pair") or "",
                "status": "failed",
                "error": str(exc),
                "writes_main_semantic_labels": False,
                "rewrites_existing_gold": False,
                "production_weight": False,
                "generated_at": utc_now(),
            }
            counts["failed"] += 1
        results.append(item)
        _write_batch_report(
            report_path,
            status="running",
            queue=queue,
            samples=results,
            counts=counts,
            query={
                "account_id": account_id or "all",
                "dataset_id": dataset_id or "all",
                "confusion_pair": confusion_pair or "all",
                "limit": cap,
                "window_seconds": seconds,
                "run_asr": run_asr,
                "run_ocr": run_ocr,
                "run_omni": run_omni,
                "force": force,
                "include_reviewed": bool(include_reviewed),
            },
            service_status=service_status,
        )
    status = "ready" if any(item.get("status") in {"ready", "partial"} for item in results) else ("empty" if not results else "failed")
    report = _write_batch_report(
        report_path,
        status=status,
        queue=queue,
        samples=results,
        counts=counts,
        query={
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "confusion_pair": confusion_pair or "all",
            "limit": cap,
            "window_seconds": seconds,
            "run_asr": run_asr,
            "run_ocr": run_ocr,
            "run_omni": run_omni,
            "force": force,
            "include_reviewed": bool(include_reviewed),
            "sample_ids": sorted(requested_ids),
        },
        service_status=service_status,
    )
    return report


def run_material_resolver_shadow(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    confusion_pair: str | None = None,
    limit: int = 80,
    include_reviewed: bool = True,
    output_path: str | Path | None = None,
) -> dict:
    queue = material_confusion_queue(
        account_id=account_id,
        dataset_id=dataset_id,
        confusion_pair=confusion_pair,
        limit=max(1, min(100, int(limit or 80))),
        local_media_only=True,
        include_reviewed=include_reviewed,
    )
    cache = material_evidence_cache_index()
    gold = material_gold_annotation_index(confirmed_only=False)
    rows: list[dict] = []
    for sample in queue.get("samples") or []:
        sample_id = str(sample.get("sample_id") or "")
        evidence = cache.get(sample_id) or {}
        strategies = _resolve_strategies(sample, evidence.get("windows") if isinstance(evidence.get("windows"), list) else [])
        annotation = gold.get(sample_id) or {}
        rows.append(
            {
                "sample_id": sample_id,
                "account_id": sample.get("account_id") or "",
                "title": sample.get("title") or "",
                "confusion_pair": sample.get("confusion_pair") or "",
                "confusion_pair_label_zh": sample.get("confusion_pair_label_zh") or "",
                "evidence_status": evidence.get("status") or "missing",
                "window_count": len(evidence.get("windows") or []),
                "strategies": strategies,
                "gold": annotation if annotation.get("review_status") == "confirmed" else None,
                "writes_main_semantic_labels": False,
                "production_weight": False,
            }
        )
    strategy_comparison = {
        strategy: _resolver_strategy_metrics(rows, strategy)
        for strategy in ["title_only", "omni_only", "asr_ocr", "multi_window"]
    }
    cached_gold_rows = [
        row
        for row in rows
        if row.get("evidence_status") in {"ready", "partial"} and row.get("gold")
    ]
    cached_eval_strategy_comparison = {
        strategy: _resolver_strategy_metrics(cached_gold_rows, strategy)
        for strategy in ["title_only", "omni_only", "asr_ocr", "multi_window"]
    }
    disagreements = _resolver_disagreements(rows)
    evidence_ready = sum(1 for row in rows if row.get("evidence_status") in {"ready", "partial"})
    gold_count = sum(1 for row in rows if row.get("gold"))
    cached_gold_count = len(cached_gold_rows)
    multi = cached_eval_strategy_comparison["multi_window"]
    omni = cached_eval_strategy_comparison["omni_only"]
    accuracy_gain = round(float(multi.get("canonical_accuracy") or 0.0) - float(omni.get("canonical_accuracy") or 0.0), 4)
    checks = {
        "evidence_coverage_at_least_85pct": evidence_ready / max(1, len(rows)) >= 0.85,
        "gold_evaluable_at_least_30": cached_gold_count >= 30,
        "canonical_accuracy_at_least_85pct": cached_gold_count >= 30 and float(multi.get("canonical_accuracy") or 0.0) >= 0.85,
        "severe_error_at_most_10pct": cached_gold_count >= 30 and float(multi.get("severe_error_rate") or 1.0) <= 0.10,
        "gain_vs_omni_at_least_3pct": cached_gold_count >= 30 and accuracy_gain >= 0.03,
    }
    gate_passed = all(checks.values())
    summary = {
        "sample_count": len(rows),
        "evidence_ready_count": evidence_ready,
        "evidence_coverage": round(evidence_ready / max(1, len(rows)), 4),
        "gold_evaluable_count": gold_count,
        "cached_gold_evaluable_count": cached_gold_count,
        "gold_without_evidence_count": max(0, gold_count - cached_gold_count),
        "awaiting_gold_count": len(rows) - gold_count,
        "disagreement_count": len(disagreements),
        "multi_window_gain_vs_omni": accuracy_gain,
    }
    report = {
        "contract_version": MATERIAL_EVIDENCE_VERSION,
        "resolver_version": MATERIAL_RESOLVER_VERSION,
        "status": "eligible_for_ranker_ablation" if gate_passed else "resolver_research_only",
        "mode": "material_confusion_resolver_shadow",
        "query": {
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "confusion_pair": confusion_pair or "all",
            "limit": int(limit or 80),
            "include_reviewed": bool(include_reviewed),
        },
        "summary": summary,
        "strategy_comparison": strategy_comparison,
        "cached_eval_strategy_comparison": cached_eval_strategy_comparison,
        "evaluation_scope_note": "Promotion metrics use only confirmed Gold samples with completed D10-B evidence.",
        "disagreement_samples": disagreements[:40],
        "samples": rows,
        "promotion_gate": {
            "passed": gate_passed,
            "status": "eligible_for_ranker_ablation" if gate_passed else "research_only",
            "checks": checks,
            "note": "This gate only allows a later ranker ablation. It never changes production weights or manual labels.",
        },
        "recommended_next_action": (
            "run_ranker_ablation_without_production_weight"
            if gate_passed
            else (
                "extract_targeted_evidence"
                if evidence_ready / max(1, len(rows)) < 0.85
                else ("complete_second_targeted_gold_set" if cached_gold_count < 30 else "inspect_resolver_disagreements")
            )
        ),
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }
    path = Path(output_path) if output_path else _resolver_report_path()
    write_json(path, report)
    write_json(_latest_resolver_path(), report)
    report["report_path"] = str(path)
    return report


def material_evidence_cache_index() -> dict[str, dict]:
    root = _evidence_cache_root()
    if not root.is_dir():
        return {}
    indexed: dict[str, dict] = {}
    for path in root.glob("*.json"):
        item = read_json(path, None)
        if not isinstance(item, dict) or item.get("contract_version") != MATERIAL_EVIDENCE_VERSION:
            continue
        sample_id = str(item.get("sample_id") or "")
        if not sample_id:
            continue
        previous = indexed.get(sample_id)
        if not previous or str(item.get("generated_at") or "") >= str(previous.get("generated_at") or ""):
            indexed[sample_id] = {**item, "cache_path": str(path)}
    return indexed


def _extract_sample_evidence(
    sample: dict,
    *,
    window_seconds: float,
    run_asr: bool,
    run_ocr: bool,
    run_omni: bool,
    force: bool,
    client: QwenOmniClient,
    service_status: dict,
) -> dict:
    video_path = _sample_video_path(sample)
    probe = probe_video(video_path)
    external_audio_path = _sample_audio_path(sample)
    duration = float(probe.get("duration_seconds") or 0.0)
    plan = _omni_window_plan(duration, window_seconds)
    cache_path = _evidence_cache_path(sample, video_path, window_seconds)
    cached = read_json(cache_path, {}) if not force else {}
    previous_windows = {
        str(item.get("window") or ""): item
        for item in (cached.get("windows") or [])
        if isinstance(item, dict)
    }
    windows: list[dict] = []
    base = {
        "contract_version": MATERIAL_EVIDENCE_VERSION,
        "resolver_version": MATERIAL_RESOLVER_VERSION,
        "sample_id": sample.get("sample_id") or "",
        "platform_item_id": sample.get("platform_item_id") or "",
        "account_id": sample.get("account_id") or "",
        "dataset_id": sample.get("dataset_id") or "",
        "title": sample.get("title") or "",
        "confusion_pair": sample.get("confusion_pair") or "",
        "confusion_pair_label_zh": sample.get("confusion_pair_label_zh") or "",
        "video_path": str(video_path),
        "source_duration_seconds": round(duration, 3),
        "source_has_audio": bool(probe.get("audio_streams") or external_audio_path),
        "source_audio_type": "embedded_audio" if probe.get("audio_streams") else ("external_audio" if external_audio_path else "missing_audio"),
        "source_audio_path": str(external_audio_path or ""),
        "window_seconds": float(window_seconds),
        "window_plan": plan,
        "status": "running",
        "windows": windows,
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }
    write_json(cache_path, base)
    for spec in plan:
        name = str(spec.get("window") or "window")
        previous = previous_windows.get(name) or {}
        if isinstance(previous.get("asr"), dict):
            previous["asr"] = _gate_asr_payload(previous["asr"])
        if not force and _window_satisfies(previous, run_asr=run_asr, run_ocr=run_ocr, run_omni=run_omni):
            window = previous
            window["cache_hit"] = True
        else:
            window = _extract_window_evidence(
                sample,
                video_path=video_path,
                probe=probe,
                external_audio_path=external_audio_path,
                spec=spec,
                plan=plan,
                run_asr=run_asr,
                run_ocr=run_ocr,
                run_omni=run_omni,
                force=force,
                client=client,
                service_status=service_status,
            )
        windows.append(window)
        write_json(cache_path, {**base, "windows": windows, "generated_at": utc_now()})
    strategies = _resolve_strategies(sample, windows)
    ready_windows = sum(1 for item in windows if item.get("status") in {"ready", "partial"})
    failed_windows = sum(1 for item in windows if item.get("status") == "failed")
    status = "ready" if ready_windows == len(windows) and windows else ("partial" if ready_windows else "failed")
    result = {
        **base,
        "status": status,
        "windows": windows,
        "component_summary": _sample_component_summary(windows),
        "resolver_strategies": strategies,
        "ready_window_count": ready_windows,
        "failed_window_count": failed_windows,
        "cache_path": str(cache_path),
        "generated_at": utc_now(),
    }
    write_json(cache_path, result)
    return result


def _extract_window_evidence(
    sample: dict,
    *,
    video_path: Path,
    probe: dict,
    external_audio_path: Path | None,
    spec: dict,
    plan: list[dict],
    run_asr: bool,
    run_ocr: bool,
    run_omni: bool,
    force: bool,
    client: QwenOmniClient,
    service_status: dict,
) -> dict:
    name = str(spec.get("window") or "window")
    start = float(spec.get("start_seconds") or 0.0)
    duration = max(0.1, float(spec.get("duration_seconds") or 0.0))
    clip_path = _material_clip_cache_path(video_path, sample, window_start=start, window_duration=duration)
    clip_cache_hit = clip_path.is_file() and clip_path.stat().st_size > 0
    if force or not clip_cache_hit:
        _transcode_material_window(video_path, clip_path, start_seconds=start, duration_seconds=duration)
    omni_clip_path = clip_path
    external_audio_attached = False
    if not probe.get("audio_streams") and external_audio_path:
        try:
            omni_clip_path = _attach_external_audio(
                clip_path,
                external_audio_path,
                start_seconds=start,
                duration_seconds=duration,
                force=force,
            )
            external_audio_attached = True
        except Exception:
            omni_clip_path = clip_path
    frame_paths = _extract_window_frames(video_path, sample, spec, force=force) if run_ocr else []
    ocr = _ocr_images(frame_paths) if run_ocr else {"status": "disabled", "engine": "disabled", "lines": []}
    asr = (
        _transcribe_window(
            clip_path,
            sample,
            spec,
            has_audio=bool(probe.get("audio_streams") or external_audio_path),
            external_audio_path=external_audio_path if not probe.get("audio_streams") else None,
            force=force,
        )
        if run_asr
        else {"status": "disabled", "source": "disabled", "text": "", "segments": []}
    )
    media_context = {
        "clip_path": str(omni_clip_path),
        "source_path": str(video_path),
        "source_duration_seconds": float(probe.get("duration_seconds") or 0.0),
        "clip_duration_seconds": duration,
        "window_start_seconds": start,
        "window_end_seconds": float(spec.get("end_seconds") or start + duration),
        "windowed_clip": True,
        "normalized_clip": True,
        "cache_hit": clip_cache_hit,
        "has_audio": bool(probe.get("audio_streams") or external_audio_attached),
        "audio_source": "embedded_audio" if probe.get("audio_streams") else ("external_audio" if external_audio_attached else "missing_audio"),
        "active_window": name,
        "multi_window_policy": "hook_middle_payoff_executed",
        "window_plan": plan,
        "planned_window_count": len(plan),
        "video_fps": D10B_VIDEO_FPS,
        "video_max_width": D10B_VIDEO_MAX_WIDTH,
    }
    omni = {"status": "disabled", "prompt_supported": False, "material_type": "unknown", "evidence_signals": {}}
    if run_omni:
        if _omni_service_ready(service_status):
            try:
                payload = _material_evidence_payload(sample, spec=spec, asr=asr, ocr=ocr)
                raw = client.analyze_clip_file(payload, omni_clip_path)
                omni = _normalize_material_evidence_response(raw)
                omni["raw"] = raw
            except Exception as exc:
                omni = {"status": "failed", "error": str(exc), "prompt_supported": False, "material_type": "unknown", "evidence_signals": {}}
        else:
            omni = {
                "status": "service_unavailable",
                "error": service_status.get("error") or "omni_service_not_ready",
                "prompt_supported": False,
                "material_type": "unknown",
                "evidence_signals": {},
            }
    component_ready = [
        asr.get("status") == "ready",
        ocr.get("status") == "ready",
        omni.get("status") in {"ready", "model"} and omni.get("prompt_supported"),
    ]
    status = "ready" if any(component_ready) else ("partial" if clip_path.is_file() else "failed")
    return {
        "window": name,
        "start_seconds": start,
        "end_seconds": float(spec.get("end_seconds") or start + duration),
        "duration_seconds": duration,
        "status": status,
        "clip_path": str(omni_clip_path),
        "clip_cache_hit": clip_cache_hit,
        "frame_paths": [str(path) for path in frame_paths],
        "asr": asr,
        "ocr": ocr,
        "omni": omni,
        "media_context": media_context,
        "generated_at": utc_now(),
    }


def _material_evidence_payload(sample: dict, *, spec: dict, asr: dict, ocr: dict) -> dict:
    candidates = [str(value) for value in (sample.get("candidate_material_types") or []) if str(value)]
    allowed = list(dict.fromkeys([*candidates, "unknown"]))
    pair = str(sample.get("confusion_pair") or "")
    no_audio = asr.get("status") == "audio_missing"
    prompt = [
        "你是素材形态证据抽取器，只分析当前视频窗口，不预测播放量或爆款。",
        "只返回一行紧凑 JSON，不要 Markdown，不要解释性前后缀。",
        f"混淆任务是 {sample.get('confusion_pair_label_zh') or pair}；material_type 只能取 {', '.join(allowed)}。",
        "program_context 是独立字段，不得把节目名称或 program_context 当成 material_type。",
        "严格按 {\"m\":material_type,\"p\":program_context,\"c\":confidence,\"e\":[六个数]} 返回。",
        "e 的固定顺序是 teaching_instruction, viewing_reaction, list_structure, news_narration, backstage_context, sustained_performance。",
        "六项必须根据观察到的证据独立打 0、0.5 或 1；禁止按顺序递减或套用默认序列。持续唱歌/舞台表演才提高最后一项，明确教学指令才提高第一项。",
        "优先根据当前窗口真实画面、字幕和声音判断；标题只能作为弱辅助。没有证据时输出 unknown 和低置信度。",
        f"当前窗口是 {spec.get('window')}，范围 {float(spec.get('start_seconds') or 0):.1f}-{float(spec.get('end_seconds') or 0):.1f} 秒。",
    ]
    if no_audio:
        prompt.append("当前源视频没有音轨，不得虚构 spoken_text_summary 或声音证据。")
    return {
        "model": QWEN_OMNI_MODEL,
        "mode": "shadow",
        "return_audio": False,
        "max_clip_seconds": float(spec.get("duration_seconds") or DEFAULT_EVIDENCE_WINDOW_SECONDS),
        "max_new_tokens": 64,
        "prompt_profile": "material_evidence_d10b",
        "entity_type": "material_evidence_window",
        "sample_id": sample.get("sample_id") or "",
        "account_id": sample.get("account_id") or "",
        "dataset_id": sample.get("dataset_id") or "",
        "title": sample.get("title") or "",
        "transcript": asr.get("text") or "",
        "tags": [
            pair,
            str(spec.get("window") or ""),
            *[str(value) for value in (ocr.get("lines") or [])[:8]],
        ],
        "duration_seconds": float(spec.get("duration_seconds") or 0.0),
        "analysis_prompt": prompt,
        "semantic_schema": {
            "schema_version": MATERIAL_EVIDENCE_VERSION,
            "allowed_material_types": allowed,
            "program_context_is_separate": True,
            "evidence_signals": list(EVIDENCE_SIGNAL_LABELS_ZH),
            "compact_output": True,
            "compact_keys": {"m": "material_type", "p": "program_context", "c": "confidence", "e": "evidence_signals"},
        },
    }


def _normalize_material_evidence_response(raw: dict) -> dict:
    payload = raw.get("material_evidence") if isinstance(raw.get("material_evidence"), dict) else None
    semantic = raw.get("semantic_suggestions") if isinstance(raw.get("semantic_suggestions"), dict) else {}
    if payload is None:
        payload = semantic.get("material_evidence") if isinstance(semantic.get("material_evidence"), dict) else semantic
    expected = any(
        key in payload
        for key in ["material_type", "evidence_signals", "spoken_text_summary", "visible_text", "uncertainty_reason", "m", "e"]
    )
    raw_material = str(payload.get("material_type") or payload.get("m") or "unknown").strip().lower()
    raw_signals = payload.get("evidence_signals")
    if isinstance(raw_signals, dict):
        signals = raw_signals
    elif isinstance(payload.get("e"), list):
        signals = dict(zip(EVIDENCE_SIGNAL_LABELS_ZH, payload.get("e") or []))
    else:
        signals = {}
    normalized_signals = {name: _unit_score(signals.get(name)) for name in EVIDENCE_SIGNAL_LABELS_ZH}
    visible = payload.get("visible_text")
    if isinstance(visible, str):
        visible_lines = _dedupe_text_lines(re.split(r"[\n|]+", visible))
    elif isinstance(visible, list):
        visible_lines = _dedupe_text_lines([str(value) for value in visible])
    else:
        visible_lines = []
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        evidence = [str(evidence)] if evidence else []
    return {
        "status": str(raw.get("status") or "ready"),
        "prompt_supported": bool(
            expected
            and (
                "evidence_signals" in payload
                or "material_type" in payload
                or "e" in payload
                or "m" in payload
            )
        ),
        "material_type": canonical_material_type(raw_material),
        "raw_material_type": raw_material,
        "program_context": str(payload.get("program_context") or payload.get("p") or "unknown"),
        "confidence": _unit_score(payload.get("confidence") if "confidence" in payload else payload.get("c")),
        "spoken_text_summary": str(payload.get("spoken_text_summary") or "").strip()[:500],
        "visible_text": visible_lines[:20],
        "evidence_signals": normalized_signals,
        "evidence": [str(value)[:240] for value in evidence[:10]],
        "uncertainty_reason": str(payload.get("uncertainty_reason") or "").strip()[:500],
        "media_used": bool(raw.get("media_used")),
        "use_audio_in_video": bool((raw.get("media_payload") or {}).get("use_audio_in_video")),
    }


def _resolve_strategies(sample: dict, windows: list[dict]) -> dict[str, dict]:
    title = str(sample.get("title") or "")
    asr_text = " ".join(str((item.get("asr") or {}).get("text") or "") for item in windows)
    ocr_text = " ".join(" ".join(str(value) for value in ((item.get("ocr") or {}).get("lines") or [])) for item in windows)
    title_only = _resolve_from_text(sample, title, source="title_only")
    omni_only = _resolve_from_existing_omni(sample)
    asr_ocr = _resolve_from_text(sample, f"{asr_text} {ocr_text}".strip(), source="asr_ocr")
    multi_window = _resolve_multi_window(sample, windows, title=title, asr_text=asr_text, ocr_text=ocr_text)
    return {
        "title_only": title_only,
        "omni_only": omni_only,
        "asr_ocr": asr_ocr,
        "multi_window": multi_window,
    }


def _resolve_from_existing_omni(sample: dict) -> dict:
    predicted = canonical_material_type(sample.get("omni_raw_material_type"))
    program_context = str(sample.get("omni_program_context") or "unknown")
    return {
        "predicted_material_type": predicted,
        "predicted_program_context": program_context,
        "confidence": 0.62 if predicted != "unknown" else 0.25,
        "scores": {},
        "evidence_sources": ["existing_omni_hook"],
        "reason": "使用 D10-A 已缓存的单窗口 Omni 原判。" if predicted != "unknown" else "已有 Omni 原判未形成规范素材形态。",
    }


def _resolve_from_text(sample: dict, text: str, *, source: str) -> dict:
    pair = str(sample.get("confusion_pair") or "")
    definition = MATERIAL_CONFUSION_PAIRS.get(pair) or {}
    left = str(definition.get("left") or "unknown")
    right = str(definition.get("right") or "unknown")
    normalized = str(text or "").lower()
    left_score = _cue_score(normalized, definition.get("left_cues") or [])
    right_score = _cue_score(normalized, definition.get("right_cues") or [])
    signal_scores = _signal_scores_from_text(normalized)
    left_signals, right_signals = _PAIR_SIGNAL_MAP.get(pair, ([], []))
    left_score += sum(signal_scores.get(name, 0.0) * 2.0 for name in left_signals)
    right_score += sum(signal_scores.get(name, 0.0) * 2.0 for name in right_signals)
    return _resolver_decision(
        pair=pair,
        left=left,
        right=right,
        left_score=left_score,
        right_score=right_score,
        program_context=_program_context_from_text(normalized, sample),
        sources=[source] if normalized.strip() else [],
    )


def _resolve_multi_window(sample: dict, windows: list[dict], *, title: str, asr_text: str, ocr_text: str) -> dict:
    pair = str(sample.get("confusion_pair") or "")
    definition = MATERIAL_CONFUSION_PAIRS.get(pair) or {}
    left = str(definition.get("left") or "unknown")
    right = str(definition.get("right") or "unknown")
    base = _resolve_from_text(sample, f"{asr_text} {ocr_text}", source="asr_ocr")
    scores = base.get("scores") or {}
    left_score = float(scores.get(left) or 0.0)
    right_score = float(scores.get(right) or 0.0)
    title_hint = _resolve_from_text(sample, title, source="title_hint")
    title_scores = title_hint.get("scores") or {}
    left_score += float(title_scores.get(left) or 0.0) * 0.3
    right_score += float(title_scores.get(right) or 0.0) * 0.3
    sources = ["asr_ocr", "title_weak_hint"]
    program_contexts: list[str] = []
    left_signals, right_signals = _PAIR_SIGNAL_MAP.get(pair, ([], []))
    for window in windows:
        omni = window.get("omni") if isinstance(window.get("omni"), dict) else {}
        if not omni.get("prompt_supported"):
            continue
        sources.append(f"omni_{window.get('window') or 'window'}")
        confidence = max(0.25, float(omni.get("confidence") or 0.0))
        predicted = canonical_material_type(omni.get("material_type"))
        if predicted == left:
            left_score += 2.8 * confidence
        elif predicted == right:
            right_score += 2.8 * confidence
        signals = omni.get("evidence_signals") if isinstance(omni.get("evidence_signals"), dict) else {}
        left_score += sum(float(signals.get(name) or 0.0) * 1.8 for name in left_signals)
        right_score += sum(float(signals.get(name) or 0.0) * 1.8 for name in right_signals)
        context = str(omni.get("program_context") or "unknown")
        if context not in {"", "unknown"}:
            program_contexts.append(context)
    context = Counter(program_contexts).most_common(1)[0][0] if program_contexts else _program_context_from_text(f"{title} {ocr_text}", sample)
    return _resolver_decision(
        pair=pair,
        left=left,
        right=right,
        left_score=left_score,
        right_score=right_score,
        program_context=context,
        sources=list(dict.fromkeys(sources)),
    )


def _resolver_decision(
    *,
    pair: str,
    left: str,
    right: str,
    left_score: float,
    right_score: float,
    program_context: str,
    sources: list[str],
) -> dict:
    total = left_score + right_score
    margin = abs(left_score - right_score)
    winner = left if left_score > right_score else right
    if total <= 0.0 or margin < 0.65:
        winner = "unknown"
    predicted_context = program_context or "unknown"
    if pair == "performance_program_context":
        if winner == "program_context":
            winner = "unknown"
        if left_score >= 0.8:
            winner = "performance_clip"
    confidence = 0.2 if total <= 0 else min(0.96, 0.42 + margin / max(1.0, total) * 0.5)
    label = "证据不足" if winner == "unknown" else f"{winner} 证据更强"
    return {
        "predicted_material_type": winner,
        "predicted_program_context": predicted_context,
        "confidence": round(confidence, 4),
        "scores": {left: round(left_score, 4), right: round(right_score, 4)},
        "evidence_sources": sources,
        "reason": label,
    }


def _resolver_strategy_metrics(rows: list[dict], strategy: str) -> dict:
    predicted = 0
    evaluated = 0
    accepted = 0
    severe = 0
    relation_counts: Counter[str] = Counter()
    per_pair: dict[str, Counter[str]] = {}
    for row in rows:
        result = (row.get("strategies") or {}).get(strategy) or {}
        value = str(result.get("predicted_material_type") or "unknown")
        if value != "unknown":
            predicted += 1
        gold = row.get("gold") or {}
        gold_value = str(gold.get("material_type") or "unknown")
        if gold_value in {"", "unknown"}:
            continue
        evaluated += 1
        relation = material_type_taxonomy_relation(gold_value, value)
        relation_counts[relation] += 1
        pair = str(row.get("confusion_pair") or "unknown")
        per_pair.setdefault(pair, Counter())[relation] += 1
        if relation in {"exact", "coarse_match", "specific_match"}:
            accepted += 1
        if relation == "mismatch":
            severe += 1
    return {
        "sample_count": len(rows),
        "predicted_count": predicted,
        "coverage": round(predicted / max(1, len(rows)), 4),
        "gold_evaluable_count": evaluated,
        "canonical_accuracy": round(accepted / max(1, evaluated), 4) if evaluated else 0.0,
        "severe_error_rate": round(severe / max(1, evaluated), 4) if evaluated else 0.0,
        "relation_counts": dict(relation_counts),
        "per_pair": {key: dict(value) for key, value in per_pair.items()},
    }


def _resolver_disagreements(rows: list[dict]) -> list[dict]:
    output = []
    for row in rows:
        strategies = row.get("strategies") or {}
        predictions = {
            key: str((strategies.get(key) or {}).get("predicted_material_type") or "unknown")
            for key in ["title_only", "omni_only", "asr_ocr", "multi_window"]
        }
        known = {value for value in predictions.values() if value != "unknown"}
        if len(known) < 2:
            continue
        output.append(
            {
                "sample_id": row.get("sample_id") or "",
                "account_id": row.get("account_id") or "",
                "title": row.get("title") or "",
                "confusion_pair": row.get("confusion_pair") or "",
                "predictions": predictions,
                "gold_material_type": (row.get("gold") or {}).get("material_type") or "",
                "priority": round(1.0 + len(known) * 0.5 + (0.5 if row.get("gold") else 0.0), 3),
            }
        )
    output.sort(key=lambda item: (float(item.get("priority") or 0.0), str(item.get("sample_id") or "")), reverse=True)
    return output


def _material_clip_cache_path(
    video_path: Path,
    sample: dict,
    *,
    window_start: float,
    window_duration: float,
) -> Path:
    stat = video_path.stat()
    source_hash = hashlib.sha256(
        "|".join(
            [
                str(video_path.resolve()),
                str(stat.st_size),
                str(int(stat.st_mtime)),
                f"{window_start:.3f}",
                f"{window_duration:.3f}",
                f"fps={D10B_VIDEO_FPS}",
                f"width={D10B_VIDEO_MAX_WIDTH}",
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    sample_id = _safe_part(str(sample.get("sample_id") or sample.get("platform_item_id") or video_path.stem))
    root = ensure_data_dirs().cache_dir / "material_evidence_clips" / "d10b"
    return root / f"{sample_id}_{source_hash}_s{window_start:.0f}_d{window_duration:.0f}.mp4"


def _transcode_material_window(
    video_path: Path,
    output_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for D10-B evidence windows")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start_seconds):.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{max(0.1, duration_seconds):.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        f"fps={D10B_VIDEO_FPS},scale='min({D10B_VIDEO_MAX_WIDTH},iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "32",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        run_cmd(command)
    except Exception:
        fallback = command.copy()
        fallback[fallback.index("libx264")] = "mpeg4"
        for option in ["-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p"]:
            if option in fallback:
                fallback.remove(option)
        run_cmd(fallback)


def _extract_window_frames(video_path: Path, sample: dict, spec: dict, *, force: bool) -> list[Path]:
    root = ensure_data_dirs().cache_dir / "material_evidence_frames" / _safe_part(str(sample.get("sample_id") or "sample"))
    stat = video_path.stat()
    source_hash = hashlib.sha256(f"{video_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode("utf-8")).hexdigest()[:12]
    start = float(spec.get("start_seconds") or 0.0)
    duration = max(0.1, float(spec.get("duration_seconds") or 0.0))
    paths: list[Path] = []
    for index, ratio in enumerate((0.18, 0.52, 0.84), start=1):
        timestamp = start + min(duration - 0.05, max(0.0, duration * ratio))
        path = root / f"{source_hash}_{spec.get('window')}_{index}.jpg"
        if force or not path.is_file() or path.stat().st_size <= 0:
            extract_frame(video_path, path, timestamp)
        paths.append(path)
    return paths


def _ocr_images(paths: list[Path]) -> dict:
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return {"status": "missing_frames", "engine": "none", "lines": [], "frame_count": 0}
    if platform.system() == "Darwin" and shutil.which("swiftc"):
        script = Path(__file__).resolve().parents[3] / "scripts" / "macos_vision_ocr.swift"
        if script.is_file():
            try:
                binary = _macos_vision_ocr_binary(script)
                result = subprocess.run(
                    [str(binary), *[str(path) for path in existing]],
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
                payload = json.loads(result.stdout or "{}")
                lines = []
                for item in payload.get("items") or []:
                    lines.extend(str(value) for value in (item.get("lines") or []))
                lines = _dedupe_text_lines(lines)
                return {
                    "status": "ready" if lines else "empty",
                    "engine": "macos_vision",
                    "lines": lines[:40],
                    "frame_count": len(existing),
                }
            except Exception as exc:
                vision_error = str(exc)
            else:  # pragma: no cover
                vision_error = ""
        else:
            vision_error = "vision_script_missing"
    else:
        vision_error = "macos_vision_unavailable"
    tesseract = shutil.which("tesseract")
    if tesseract and _tesseract_has_chinese(tesseract):
        lines = []
        for path in existing:
            result = subprocess.run(
                [tesseract, str(path), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
            if result.returncode == 0:
                lines.extend(result.stdout.splitlines())
        lines = _dedupe_text_lines(lines)
        return {"status": "ready" if lines else "empty", "engine": "tesseract_chi_sim", "lines": lines[:40], "frame_count": len(existing)}
    return {
        "status": "engine_unavailable",
        "engine": "none",
        "lines": [],
        "frame_count": len(existing),
        "error": vision_error,
    }


def _transcribe_window(
    clip_path: Path,
    sample: dict,
    spec: dict,
    *,
    has_audio: bool,
    external_audio_path: Path | None = None,
    force: bool,
) -> dict:
    if not has_audio:
        return {"status": "audio_missing", "source": "missing_audio", "text": "", "segments": []}
    sample_id = _safe_part(str(sample.get("sample_id") or "sample"))
    window = _safe_part(str(spec.get("window") or "window"))
    root = ensure_data_dirs().cache_dir / "material_evidence_asr" / sample_id / window
    result_path = root / "result.json"
    cached = read_json(result_path, None) if not force else None
    if isinstance(cached, dict) and cached.get("status") in {"ready", "empty", "low_information"}:
        payload = _gate_asr_payload(cached)
        write_json(result_path, payload)
        return {**payload, "cache_hit": True}
    try:
        wav_path = root / "audio.wav"
        if external_audio_path:
            _extract_audio_window(
                external_audio_path,
                wav_path,
                start_seconds=float(spec.get("start_seconds") or 0.0),
                duration_seconds=float(spec.get("duration_seconds") or 0.0),
            )
        else:
            extract_audio(clip_path, wav_path)
        result = transcribe_audio_file(
            wav_path,
            root,
            model_size=os.getenv("DSO_D10B_ASR_MODEL", "base"),
            asr_profile="fast",
        )
        segments = result.get("segments") if isinstance(result.get("segments"), list) else []
        text = " ".join(str(item.get("text") or "").strip() for item in segments if str(item.get("text") or "").strip())
        payload = _gate_asr_payload({
            "status": "ready" if text else "empty",
            "source": result.get("source") or "",
            "text": text[:3000],
            "segments": segments,
            "metadata": result.get("metadata") or {},
            "cache_hit": False,
        })
    except Exception as exc:
        payload = {"status": "failed", "source": "", "text": "", "segments": [], "error": str(exc), "cache_hit": False}
    write_json(result_path, payload)
    return payload


def _gate_asr_payload(payload: dict) -> dict:
    text = str(payload.get("text") or "").strip()
    if not text:
        return payload
    compact = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text).lower()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    prompt = str(metadata.get("prompt") or "").strip()
    prompt_compact = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", prompt).lower()
    generic = {
        "音乐",
        "音乐综艺",
        "音乐综艺节目中文转写",
        "音乐综艺节目中文转录",
        "中文字幕",
    }
    reason = ""
    if compact in generic:
        reason = "generic_music_transcript"
    elif prompt_compact and compact and prompt_compact.startswith(compact) and len(compact) <= 18:
        reason = "initial_prompt_echo"
    elif len(compact) < 4:
        reason = "too_short"
    if not reason:
        return payload
    return {
        **payload,
        "status": "low_information",
        "text": "",
        "raw_text": text[:3000],
        "quality_reason": reason,
    }


def _evidence_status_sample(sample: dict, record: dict) -> dict:
    summary = record.get("component_summary") if isinstance(record.get("component_summary"), dict) else _sample_component_summary(record.get("windows") or [])
    strategies = record.get("resolver_strategies") if isinstance(record.get("resolver_strategies"), dict) else {}
    return {
        "sample_id": sample.get("sample_id") or "",
        "account_id": sample.get("account_id") or "",
        "title": sample.get("title") or "",
        "confusion_pair": sample.get("confusion_pair") or "",
        "status": record.get("status") or "missing",
        "source_has_audio": bool(record.get("source_has_audio") or (sample.get("assets") or {}).get("audio")),
        "component_summary": summary,
        "multi_window_prediction": (strategies.get("multi_window") or {}).get("predicted_material_type") or "unknown",
        "multi_window_confidence": float((strategies.get("multi_window") or {}).get("confidence") or 0.0),
    }


def _evidence_coverage_summary(samples: list[dict]) -> dict:
    cached = sum(1 for item in samples if item.get("status") != "missing")
    ready = sum(1 for item in samples if item.get("status") in {"ready", "partial"})
    asr = sum(1 for item in samples if int((item.get("component_summary") or {}).get("asr_ready_windows") or 0) > 0)
    ocr = sum(1 for item in samples if int((item.get("component_summary") or {}).get("ocr_ready_windows") or 0) > 0)
    omni = sum(1 for item in samples if int((item.get("component_summary") or {}).get("omni_ready_windows") or 0) > 0)
    multi = sum(1 for item in samples if int((item.get("component_summary") or {}).get("executed_window_count") or 0) >= 2)
    audio = sum(1 for item in samples if item.get("source_has_audio"))
    return {
        "selected_count": len(samples),
        "cached_count": cached,
        "evidence_ready_count": ready,
        "evidence_ready_rate": round(ready / max(1, len(samples)), 4),
        "audio_source_count": audio,
        "audio_source_rate": round(audio / max(1, len(samples)), 4),
        "asr_ready_count": asr,
        "ocr_ready_count": ocr,
        "omni_ready_count": omni,
        "multi_window_ready_count": multi,
    }


def _sample_component_summary(windows: list[dict]) -> dict:
    return {
        "planned_window_count": len(windows),
        "executed_window_count": sum(1 for item in windows if item.get("status") in {"ready", "partial"}),
        "asr_ready_windows": sum(1 for item in windows if (item.get("asr") or {}).get("status") == "ready"),
        "asr_low_information_windows": sum(
            1 for item in windows if (item.get("asr") or {}).get("status") == "low_information"
        ),
        "asr_audio_missing_windows": sum(1 for item in windows if (item.get("asr") or {}).get("status") == "audio_missing"),
        "ocr_ready_windows": sum(1 for item in windows if (item.get("ocr") or {}).get("status") == "ready"),
        "omni_ready_windows": sum(
            1
            for item in windows
            if (item.get("omni") or {}).get("status") in {"ready", "model"} and (item.get("omni") or {}).get("prompt_supported")
        ),
        "omni_legacy_response_windows": sum(
            1
            for item in windows
            if (item.get("omni") or {}).get("status") in {"ready", "model"} and not (item.get("omni") or {}).get("prompt_supported")
        ),
    }


def _write_batch_report(
    path: Path,
    *,
    status: str,
    queue: dict,
    samples: list[dict],
    counts: Counter[str],
    query: dict,
    service_status: dict,
) -> dict:
    compact = [_compact_batch_sample(item) for item in samples]
    status_samples = [
        {
            "status": item.get("status") or "missing",
            "source_has_audio": bool(item.get("source_has_audio")),
            "component_summary": item.get("component_summary") or _sample_component_summary(item.get("windows") or []),
        }
        for item in samples
    ]
    report = {
        "contract_version": MATERIAL_EVIDENCE_VERSION,
        "resolver_version": MATERIAL_RESOLVER_VERSION,
        "status": status,
        "mode": "targeted_material_evidence_extraction",
        "query": query,
        "queue_summary": queue.get("batch_summary") or {},
        "sample_count": len(samples),
        "status_counts": dict(counts),
        "coverage": _evidence_coverage_summary(status_samples),
        "service_status": service_status,
        "samples": compact,
        "report_path": str(path),
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
        "recommendations": _batch_recommendations(status_samples),
        "generated_at": utc_now(),
    }
    write_json(path, report)
    return report


def _compact_batch_sample(item: dict) -> dict:
    return {
        "sample_id": item.get("sample_id") or "",
        "account_id": item.get("account_id") or "",
        "title": item.get("title") or "",
        "confusion_pair": item.get("confusion_pair") or "",
        "status": item.get("status") or "",
        "source_has_audio": bool(item.get("source_has_audio")),
        "component_summary": item.get("component_summary") or _sample_component_summary(item.get("windows") or []),
        "resolver_strategies": item.get("resolver_strategies") or {},
        "cache_path": item.get("cache_path") or "",
        "error": item.get("error") or "",
    }


def _batch_recommendations(samples: list[dict]) -> list[str]:
    coverage = _evidence_coverage_summary(samples)
    recommendations = []
    if coverage["audio_source_rate"] < 0.5:
        recommendations.append("多数源视频不含音轨，ASR 缺失必须保留为事实，优先依赖 OCR 与多窗口画面证据。")
    if coverage["ocr_ready_count"] < coverage["selected_count"]:
        recommendations.append("OCR 尚未覆盖全部样本，检查 macOS Vision 或中文 OCR 运行环境。")
    if coverage["multi_window_ready_count"] < coverage["selected_count"]:
        recommendations.append("继续断点执行 hook / middle / payoff，达到至少两个真实窗口后再做 Resolver 对比。")
    recommendations.append("证据结果只进入 Shadow Resolver，不自动改写 Gold、主语义标签或排序权重。")
    return recommendations


def _window_satisfies(window: dict, *, run_asr: bool, run_ocr: bool, run_omni: bool) -> bool:
    if not window or window.get("status") not in {"ready", "partial"}:
        return False
    if run_asr and (window.get("asr") or {}).get("status") not in {"ready", "empty", "low_information", "audio_missing"}:
        return False
    if run_ocr and (window.get("ocr") or {}).get("status") not in {"ready", "empty"}:
        return False
    if run_omni:
        omni = window.get("omni") or {}
        if omni.get("status") not in {"ready", "model"} or not omni.get("prompt_supported"):
            return False
    return True


def _sample_video_path(sample: dict) -> Path:
    for value in (((sample.get("assets") or {}).get("paths") or {}).get("video") or []):
        path = Path(str(value)).expanduser()
        if path.is_file():
            return path
    raise FileNotFoundError(f"video_missing:{sample.get('sample_id') or ''}")


def _sample_audio_path(sample: dict) -> Path | None:
    for value in (((sample.get("assets") or {}).get("paths") or {}).get("audio") or []):
        path = Path(str(value)).expanduser()
        if path.is_file():
            return path
    return None


def _attach_external_audio(
    clip_path: Path,
    audio_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    force: bool,
) -> Path:
    stat = audio_path.stat()
    digest = hashlib.sha256(
        f"{audio_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}|{start_seconds:.3f}|{duration_seconds:.3f}".encode("utf-8")
    ).hexdigest()[:10]
    output = clip_path.with_name(f"{clip_path.stem}_{digest}_audio.mp4")
    if output.is_file() and output.stat().st_size > 0 and not force:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(clip_path),
            "-ss", f"{max(0.0, start_seconds):.3f}", "-i", str(audio_path),
            "-t", f"{max(0.1, duration_seconds):.3f}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000",
            "-shortest", "-movflags", "+faststart", str(output),
        ]
    )
    return output


def _extract_audio_window(
    audio_path: Path,
    output_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, start_seconds):.3f}", "-i", str(audio_path),
            "-t", f"{max(0.1, duration_seconds):.3f}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output_path),
        ]
    )
    return output_path


def _evidence_cache_root() -> Path:
    return ensure_data_dirs().cache_dir / "material_evidence" / "d10b"


def _evidence_cache_path(sample: dict, video_path: Path, window_seconds: float) -> Path:
    stat = video_path.stat()
    digest = hashlib.sha256(
        "|".join(
            [
                MATERIAL_EVIDENCE_VERSION,
                str(video_path.resolve()),
                str(stat.st_size),
                str(int(stat.st_mtime)),
                f"{window_seconds:.3f}",
                QWEN_OMNI_MODEL,
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    sample_id = _safe_part(str(sample.get("sample_id") or sample.get("platform_item_id") or "sample"))
    return _evidence_cache_root() / f"{sample_id}_{digest}.json"


def _batch_report_path() -> Path:
    stamp = utc_now().replace(":", "").replace("-", "").replace(".", "_")
    return ensure_data_dirs().root / "outputs" / "material_evidence" / f"d10b_batch_{stamp}.json"


def _resolver_report_path() -> Path:
    stamp = utc_now().replace(":", "").replace("-", "").replace(".", "_")
    return ensure_data_dirs().root / "outputs" / "material_evidence" / f"resolver_shadow_{stamp}.json"


def _latest_resolver_path() -> Path:
    return ensure_data_dirs().root / "outputs" / "material_evidence" / "resolver_shadow_latest.json"


def _safe_part(value: str) -> str:
    text = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value or ""))
    return text[:96] or "sample"


def _cue_score(text: str, cues: list[str]) -> float:
    return float(sum(1 for cue in dict.fromkeys(str(value).lower() for value in cues) if cue and cue in text))


def _signal_scores_from_text(text: str) -> dict[str, float]:
    scores = {}
    for signal, cues in _SIGNAL_CUES.items():
        hits = sum(1 for cue in cues if cue.lower() in text)
        scores[signal] = min(1.0, hits / 2.0)
    return scores


def _program_context_from_text(text: str, sample: dict) -> str:
    for value in sorted(OMNI_PROGRAM_CONTEXTS, key=len, reverse=True):
        if str(value).lower() in text.lower():
            return str(value)
    existing = str(sample.get("omni_program_context") or "unknown")
    return existing if existing not in {"", "unknown"} and existing.lower() in text.lower() else "unknown"


def _unit_score(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        score = float(value or 0.0)
    except (TypeError, ValueError):
        normalized = str(value or "").strip().lower()
        return 1.0 if normalized in {"yes", "true", "high", "present", "有", "是"} else 0.0
    if score > 1.0 and score <= 100.0:
        score /= 100.0
    return round(max(0.0, min(1.0, score)), 4)


def _dedupe_text_lines(values: list[str]) -> list[str]:
    output = []
    seen = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        key = re.sub(r"\W+", "", text).lower()
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        output.append(text[:240])
    return output


def _tesseract_has_chinese(binary: str) -> bool:
    try:
        result = subprocess.run([binary, "--list-langs"], text=True, capture_output=True, check=False, timeout=10)
        return "chi_sim" in result.stdout.split()
    except Exception:
        return False


def _macos_vision_ocr_binary(script: Path) -> Path:
    binary = ensure_data_dirs().cache_dir / "tools" / "macos_vision_ocr"
    needs_build = not binary.is_file() or binary.stat().st_mtime < script.stat().st_mtime
    if needs_build:
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [shutil.which("swiftc") or "swiftc", str(script), "-o", str(binary)],
            text=True,
            capture_output=True,
            check=True,
            timeout=120,
        )
    return binary


def _omni_service_ready(status: dict) -> bool:
    if status.get("status") not in {"ready", "model", "loaded"}:
        return False
    raw = status.get("raw") if isinstance(status.get("raw"), dict) else {}
    model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    loaded = str(model.get("model_id") or raw.get("model_id") or "")
    return not loaded or "qwen2.5-omni" in loaded.lower()
