from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from dso.config import ensure_data_dirs
from dso.learning.material_confusion import material_confusion_queue
from dso.learning.material_evidence import (
    EVIDENCE_SIGNAL_LABELS_ZH,
    _material_clip_cache_path,
    _material_evidence_payload,
    _normalize_material_evidence_response,
    _omni_service_ready,
    _omni_window_plan,
    _PAIR_SIGNAL_MAP,
    _resolve_from_text,
    _resolver_decision,
    _resolver_pair_definition,
    _sample_video_path,
    _transcode_material_window,
)
from dso.learning.material_taxonomy import (
    MATERIAL_TAXONOMY_MATCH_RELATIONS,
    canonical_material_type,
    material_type_taxonomy_relation,
)
from dso.learning.qwen_omni import QWEN_OMNI_MODEL, QwenOmniClient
from dso.media.ffmpeg import probe_video
from dso.utils import read_json, utc_now, write_json


MATERIAL_DESCRIPTION_EXPERIMENT_VERSION = "material_description_experiment.d10c.v2"
MATERIAL_DESCRIPTION_PROMPT_PROFILE = "material_description_d10c"
DEFAULT_DESCRIPTION_LIMIT = 6
DEFAULT_DESCRIPTION_WINDOW_SECONDS = 15.0
DEFAULT_WINDOWS_PER_SAMPLE = 3
DESCRIPTION_MAX_NEW_TOKENS = 128
DESCRIPTION_REQUEST_TIMEOUT_SECONDS = 480.0
DESCRIPTION_SIGNAL_ALIASES = {
    "teach": "teaching_instruction",
    "teaching": "teaching_instruction",
    "teaching_instruction": "teaching_instruction",
    "react": "viewing_reaction",
    "reaction": "viewing_reaction",
    "viewing_reaction": "viewing_reaction",
    "list": "list_structure",
    "list_structure": "list_structure",
    "news": "news_narration",
    "news_narration": "news_narration",
    "backstage": "backstage_context",
    "backstage_context": "backstage_context",
    "perform": "sustained_performance",
    "performance": "sustained_performance",
    "sustained_performance": "sustained_performance",
}


def run_material_description_experiment(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = DEFAULT_DESCRIPTION_LIMIT,
    window_seconds: float = DEFAULT_DESCRIPTION_WINDOW_SECONDS,
    windows_per_sample: int = DEFAULT_WINDOWS_PER_SAMPLE,
    run_direct: bool = True,
    force: bool = False,
    output_path: str | Path | None = None,
    client: QwenOmniClient | None = None,
) -> dict:
    cap = max(1, min(30, int(limit or DEFAULT_DESCRIPTION_LIMIT)))
    seconds = max(4.0, min(15.0, float(window_seconds or DEFAULT_DESCRIPTION_WINDOW_SECONDS)))
    window_cap = max(1, min(3, int(windows_per_sample or DEFAULT_WINDOWS_PER_SAMPLE)))
    queue = material_confusion_queue(
        account_id=account_id,
        dataset_id=dataset_id,
        limit=100,
        local_media_only=True,
        include_reviewed=True,
    )
    selected = _select_diverse_gold_samples(queue.get("samples") or [], limit=cap)
    client = client or QwenOmniClient(timeout_seconds=DESCRIPTION_REQUEST_TIMEOUT_SECONDS)
    service_status = client.health()
    report_path = Path(output_path) if output_path else _default_report_path()
    samples: list[dict] = []

    if not _omni_service_ready(service_status):
        report = _build_report(
            selected=selected,
            samples=samples,
            service_status=service_status,
            query={
                "account_id": account_id or "all",
                "dataset_id": dataset_id or "all",
                "limit": cap,
                "window_seconds": seconds,
                "windows_per_sample": window_cap,
                "run_direct": bool(run_direct),
                "force": bool(force),
            },
            status="service_unavailable",
        )
        _write_report(report_path, report)
        return report

    query = {
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "limit": cap,
        "window_seconds": seconds,
        "windows_per_sample": window_cap,
        "run_direct": bool(run_direct),
        "force": bool(force),
    }
    for sample in selected:
        try:
            result = _run_sample(
                sample,
                client=client,
                window_seconds=seconds,
                windows_per_sample=window_cap,
                run_direct=run_direct,
                force=force,
            )
        except Exception as exc:
            result = {
                "sample_id": sample.get("sample_id") or "",
                "account_id": sample.get("account_id") or "",
                "confusion_pair": sample.get("confusion_pair") or "",
                "gold_material_type": ((sample.get("annotation") or {}).get("material_type") or "unknown"),
                "status": "failed",
                "error": str(exc),
                "strategies": {},
            }
        samples.append(result)
        running = _build_report(
            selected=selected,
            samples=samples,
            service_status=service_status,
            query=query,
            status="running",
        )
        _write_report(report_path, running)

    completed_count = sum(1 for item in samples if item.get("status") in {"ready", "partial"})
    if completed_count == len(selected) and len(samples) == len(selected):
        status = "pilot_ready"
    elif completed_count:
        status = "partial"
    else:
        status = "failed"
    report = _build_report(
        selected=selected,
        samples=samples,
        service_status=service_status,
        query=query,
        status=status,
    )
    _write_report(report_path, report)
    return report


def _select_diverse_gold_samples(samples: list[dict], *, limit: int) -> list[dict]:
    eligible = []
    for sample in samples:
        annotation = sample.get("annotation") if isinstance(sample.get("annotation"), dict) else {}
        if annotation.get("review_status") != "confirmed":
            continue
        if not canonical_material_type(annotation.get("material_type")):
            continue
        if not ((sample.get("assets") or {}).get("video")):
            continue
        eligible.append(sample)

    selected: list[dict] = []
    used_ids: set[str] = set()
    used_pairs: set[str] = set()
    for sample in eligible:
        pair = str(sample.get("confusion_pair") or "")
        sample_id = str(sample.get("sample_id") or "")
        if not pair or pair in used_pairs or not sample_id:
            continue
        selected.append(sample)
        used_ids.add(sample_id)
        used_pairs.add(pair)
        if len(selected) >= limit:
            return selected
    for sample in eligible:
        sample_id = str(sample.get("sample_id") or "")
        if not sample_id or sample_id in used_ids:
            continue
        selected.append(sample)
        used_ids.add(sample_id)
        if len(selected) >= limit:
            break
    return selected


def _run_sample(
    sample: dict,
    *,
    client: QwenOmniClient,
    window_seconds: float,
    windows_per_sample: int,
    run_direct: bool,
    force: bool,
) -> dict:
    video_path = _sample_video_path(sample)
    probe = probe_video(video_path)
    duration = float(probe.get("duration_seconds") or 0.0)
    plan = _omni_window_plan(duration, window_seconds)[:windows_per_sample]
    cache_path = _description_cache_path(sample, video_path, window_seconds, windows_per_sample)
    cached = read_json(cache_path, {}) if not force else {}
    if not cached and not force:
        legacy_path = _legacy_description_cache_path(sample, video_path, window_seconds, 1)
        cached = read_json(legacy_path, {})
    cached_windows = {
        str(item.get("window") or ""): item
        for item in (cached.get("windows") or [])
        if isinstance(item, dict)
    }
    windows: list[dict] = []
    for spec in plan:
        name = str(spec.get("window") or "window")
        prior = cached_windows.get(name) or {}
        if not force and _experiment_window_ready(prior, require_direct=run_direct):
            windows.append({**prior, "cache_hit": True})
            continue
        windows.append(_run_window(sample, video_path=video_path, spec=spec, client=client, run_direct=run_direct))
        write_json(
            cache_path,
            {
                "contract_version": MATERIAL_DESCRIPTION_EXPERIMENT_VERSION,
                "sample_id": sample.get("sample_id") or "",
                "video_path": str(video_path),
                "windows": windows,
                "generated_at": utc_now(),
            },
        )

    descriptions = [item.get("description") or {} for item in windows if (item.get("description") or {}).get("schema_valid")]
    hook_descriptions = [
        item.get("description") or {}
        for item in windows
        if item.get("window") == "hook" and (item.get("description") or {}).get("schema_valid")
    ]
    direct_prediction = _aggregate_direct_prediction(
        windows,
        fallback_material_type=sample.get("omni_raw_material_type"),
        run_direct=run_direct,
    )
    strategies = {
        "direct_classification": direct_prediction,
        "description_hook_text": _resolve_description(
            sample,
            hook_descriptions,
            include_signals=False,
            include_title=False,
        ),
        "description_hook_structured": _resolve_description(
            sample,
            hook_descriptions,
            include_signals=True,
            include_title=False,
        ),
        "description_text_only": _resolve_description(sample, descriptions, include_signals=False, include_title=False),
        "description_structured": _resolve_description(sample, descriptions, include_signals=True, include_title=False),
        "description_structured_title": _resolve_description(sample, descriptions, include_signals=True, include_title=True),
    }
    annotation = sample.get("annotation") if isinstance(sample.get("annotation"), dict) else {}
    gold = str(annotation.get("material_type") or "unknown")
    diagnostics = []
    for item in descriptions:
        diagnostics.extend(_description_consistency_issues(item))
    result = {
        "sample_id": sample.get("sample_id") or "",
        "account_id": sample.get("account_id") or "",
        "dataset_id": sample.get("dataset_id") or "",
        "confusion_pair": sample.get("confusion_pair") or "",
        "title": sample.get("title") or "",
        "gold_material_type": gold,
        "gold_canonical_material_type": canonical_material_type(gold) or "unknown",
        "existing_omni_material_type": sample.get("omni_raw_material_type") or "unknown",
        "status": "ready" if descriptions else "partial",
        "direct_baseline_mode": "same_window_direct" if run_direct else "existing_omni_cache",
        "source_has_audio": bool(probe.get("audio_streams")),
        "window_count": len(windows),
        "schema_valid_window_count": len(descriptions),
        "audio_used_window_count": sum(1 for item in descriptions if item.get("audio_used")),
        "consistency_issue_count": len(diagnostics),
        "consistency_issues": sorted(set(diagnostics)),
        "strategies": strategies,
        "windows": [_compact_window(item) for item in windows],
        "cache_path": str(cache_path),
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
    }
    write_json(cache_path, {**result, "contract_version": MATERIAL_DESCRIPTION_EXPERIMENT_VERSION, "windows": windows, "generated_at": utc_now()})
    return result


def _run_window(
    sample: dict,
    *,
    video_path: Path,
    spec: dict,
    client: QwenOmniClient,
    run_direct: bool,
) -> dict:
    start = float(spec.get("start_seconds") or 0.0)
    duration = max(0.1, float(spec.get("duration_seconds") or 0.0))
    clip_path = _material_clip_cache_path(video_path, sample, window_start=start, window_duration=duration)
    clip_cache_hit = clip_path.is_file() and clip_path.stat().st_size > 0
    if not clip_cache_hit:
        _transcode_material_window(video_path, clip_path, start_seconds=start, duration_seconds=duration)

    direct_raw: dict = {}
    direct = {"status": "disabled", "prompt_supported": False, "material_type": "unknown", "confidence": 0.0}
    direct_latency = 0.0
    if run_direct:
        disabled_asr = {"status": "disabled", "text": ""}
        disabled_ocr = {"status": "disabled", "lines": []}
        direct_started = monotonic()
        direct_raw = client.analyze_clip_file(
            _material_evidence_payload(sample, spec=spec, asr=disabled_asr, ocr=disabled_ocr),
            clip_path,
        )
        direct_latency = monotonic() - direct_started
        direct = _normalize_material_evidence_response(direct_raw)

    description_started = monotonic()
    description_raw = client.analyze_clip_file(_description_payload(sample, spec=spec), clip_path)
    description_latency = monotonic() - description_started
    description = _normalize_description_response(description_raw)
    return {
        "window": spec.get("window") or "window",
        "start_seconds": start,
        "end_seconds": float(spec.get("end_seconds") or start + duration),
        "duration_seconds": duration,
        "clip_path": str(clip_path),
        "clip_cache_hit": clip_cache_hit,
        "direct": direct,
        "direct_raw": direct_raw,
        "direct_latency_seconds": round(direct_latency, 3),
        "description": description,
        "description_raw": description_raw,
        "description_latency_seconds": round(description_latency, 3),
        "generated_at": utc_now(),
    }


def _description_payload(sample: dict, *, spec: dict) -> dict:
    return {
        "model": QWEN_OMNI_MODEL,
        "mode": "shadow",
        "return_audio": False,
        "max_clip_seconds": float(spec.get("duration_seconds") or DEFAULT_DESCRIPTION_WINDOW_SECONDS),
        "max_new_tokens": DESCRIPTION_MAX_NEW_TOKENS,
        "prompt_profile": MATERIAL_DESCRIPTION_PROMPT_PROFILE,
        "entity_type": "material_description_window",
        "sample_id": sample.get("sample_id") or "",
        "account_id": sample.get("account_id") or "",
        "dataset_id": sample.get("dataset_id") or "",
        "title": "",
        "transcript": "",
        "tags": [],
        "duration_seconds": float(spec.get("duration_seconds") or 0.0),
        "analysis_prompt": [
            "只观察当前视频窗口，不分类，不参考标题，不预测效果。只输出一行紧凑JSON，不要Markdown。",
            "格式：{\"d\":{\"v\":\"画面事实短句\",\"a\":\"声音事实短句或unknown\",\"t\":[\"可读字幕\"],\"o\":[\"观察事件\"],\"s\":{\"teach\":0,\"react\":0,\"list\":0,\"news\":0,\"backstage\":0,\"perform\":0},\"u\":\"不确定原因\",\"c\":置信度}}。",
            "s 的六个命名字段必须全部返回，值只能是0、0.5、1。持续唱歌或舞台表演写 perform，排练后台花絮才写 backstage，不得互换，不得输出素材类别。",
            f"当前窗口为 {spec.get('window') or 'window'}，时间 {float(spec.get('start_seconds') or 0):.1f}-{float(spec.get('end_seconds') or 0):.1f} 秒。",
        ],
        "semantic_schema": {
            "schema_version": MATERIAL_DESCRIPTION_EXPERIMENT_VERSION,
            "output_object": "d",
            "compact": True,
            "named_evidence_signals": True,
            "title_visible_to_model": False,
        },
    }


def _normalize_description_response(raw: dict) -> dict:
    semantic = raw.get("semantic_suggestions") if isinstance(raw.get("semantic_suggestions"), dict) else {}
    payload = semantic.get("d") if isinstance(semantic.get("d"), dict) else semantic.get("material_description")
    if not isinstance(payload, dict):
        payload = {}
    events = payload.get("o") if isinstance(payload.get("o"), list) else payload.get("observed_events")
    if not isinstance(events, list):
        events = [events] if events else []
    visible_text = payload.get("t") if isinstance(payload.get("t"), list) else payload.get("visible_text")
    if not isinstance(visible_text, list):
        visible_text = [visible_text] if visible_text else []
    raw_signals = payload.get("s") if "s" in payload else payload.get("e") if "e" in payload else payload.get("evidence_signals")
    signal_names = list(EVIDENCE_SIGNAL_LABELS_ZH)
    if isinstance(raw_signals, list):
        signals = {name: _unit_score(value) for name, value in zip(signal_names, raw_signals)}
    elif isinstance(raw_signals, dict):
        signals = {name: 0.0 for name in signal_names}
        for raw_name, value in raw_signals.items():
            normalized_name = DESCRIPTION_SIGNAL_ALIASES.get(str(raw_name).strip().lower())
            if normalized_name:
                signals[normalized_name] = _unit_score(value)
    else:
        signals = {name: 0.0 for name in signal_names}
    media = raw.get("media_payload") if isinstance(raw.get("media_payload"), dict) else {}
    visual = str(payload.get("v") or payload.get("visual_summary") or "").strip()[:500]
    audio = str(payload.get("a") or payload.get("audio_summary") or "").strip()[:500]
    schema_valid = bool(visual or events) and isinstance(raw_signals, (list, dict))
    return {
        "schema_valid": schema_valid,
        "visual_summary": visual,
        "audio_summary": audio,
        "visible_text": [str(value)[:160] for value in visible_text[:8]],
        "observed_events": [str(value)[:160] for value in events[:8]],
        "evidence_signals": signals,
        "uncertainty_reason": str(payload.get("u") or payload.get("uncertainty_reason") or "").strip()[:500],
        "confidence": _unit_score(payload.get("c") if "c" in payload else payload.get("confidence")),
        "media_used": bool(raw.get("media_used") or media.get("media_used")),
        "audio_used": bool(media.get("use_audio_in_video")),
        "raw_payload": payload,
    }


def _resolve_description(
    sample: dict,
    descriptions: list[dict],
    *,
    include_signals: bool,
    include_title: bool,
) -> dict:
    definition = _resolver_pair_definition(sample)
    left = str(definition.get("left") or "unknown")
    right = str(definition.get("right") or "unknown")
    left_score = 0.0
    right_score = 0.0
    contexts: list[str] = []
    for description in descriptions:
        text = _description_text(description)
        text_result = _resolve_from_text(sample, text, source="omni_description_text")
        scores = text_result.get("scores") or {}
        left_score += float(scores.get(left) or 0.0)
        right_score += float(scores.get(right) or 0.0)
        context = str(text_result.get("predicted_program_context") or "unknown")
        if context not in {"", "unknown"}:
            contexts.append(context)
        if include_signals:
            signals = description.get("evidence_signals") if isinstance(description.get("evidence_signals"), dict) else {}
            left_signals, right_signals = _PAIR_SIGNAL_MAP.get(str(sample.get("confusion_pair") or ""), ([], []))
            left_score += sum(float(signals.get(name) or 0.0) * 1.8 for name in left_signals)
            right_score += sum(float(signals.get(name) or 0.0) * 1.8 for name in right_signals)
    if include_title:
        title_result = _resolve_from_text(sample, str(sample.get("title") or ""), source="title_weak_hint")
        title_scores = title_result.get("scores") or {}
        left_score += float(title_scores.get(left) or 0.0) * 0.3
        right_score += float(title_scores.get(right) or 0.0) * 0.3
        title_context = str(title_result.get("predicted_program_context") or "unknown")
        if title_context not in {"", "unknown"}:
            contexts.append(title_context)
    context = Counter(contexts).most_common(1)[0][0] if contexts else "unknown"
    resolved = _resolver_decision(
        pair=str(sample.get("confusion_pair") or ""),
        left=left,
        right=right,
        left_score=left_score,
        right_score=right_score,
        program_context=context,
        sources=[
            "omni_description_text",
            *(["omni_description_signals"] if include_signals else []),
            *(["title_weak_hint"] if include_title else []),
        ],
    )
    resolved["description_count"] = len(descriptions)
    return resolved


def _aggregate_direct_prediction(
    windows: list[dict],
    *,
    fallback_material_type: Any = None,
    run_direct: bool = True,
) -> dict:
    known = [
        str((item.get("direct") or {}).get("material_type") or "unknown")
        for item in windows
        if str((item.get("direct") or {}).get("material_type") or "unknown") != "unknown"
    ]
    fallback = canonical_material_type(fallback_material_type) or "unknown"
    winner = Counter(known).most_common(1)[0][0] if known else fallback
    confidences = [float((item.get("direct") or {}).get("confidence") or 0.0) for item in windows]
    return {
        "predicted_material_type": winner,
        "confidence": round(sum(confidences) / max(1, len(confidences)), 4),
        "window_predictions": known,
        "evidence_sources": ["same_window_direct_omni" if run_direct else "existing_omni_cache"],
        "reason": "同一视频窗口的现有紧凑分类提示词。" if run_direct else "复用实验前已有 Omni Shadow 分类作为基线。",
    }


def _strategy_metrics(samples: list[dict], strategy: str) -> dict:
    evaluated = 0
    predicted = 0
    accepted = 0
    severe_errors = 0
    abstentions = 0
    relations: Counter[str] = Counter()
    for sample in samples:
        gold = str(sample.get("gold_material_type") or "unknown")
        if not canonical_material_type(gold):
            continue
        evaluated += 1
        result = (sample.get("strategies") or {}).get(strategy) or {}
        value = str(result.get("predicted_material_type") or "unknown")
        if not canonical_material_type(value):
            abstentions += 1
            continue
        predicted += 1
        relation = material_type_taxonomy_relation(gold, value)
        relations[relation] += 1
        if relation in MATERIAL_TAXONOMY_MATCH_RELATIONS:
            accepted += 1
        else:
            severe_errors += 1
    return {
        "strategy": strategy,
        "evaluated_count": evaluated,
        "predicted_count": predicted,
        "accepted_count": accepted,
        "unknown_abstention_count": abstentions,
        "severe_error_count": severe_errors,
        "prediction_coverage": round(predicted / max(1, evaluated), 4),
        "unknown_abstention_rate": round(abstentions / max(1, evaluated), 4),
        "canonical_accuracy": round(accepted / max(1, evaluated), 4),
        "selective_canonical_accuracy": round(accepted / max(1, predicted), 4),
        "severe_error_rate": round(severe_errors / max(1, predicted), 4),
        "taxonomy_relations": dict(relations),
    }


def _build_report(*, selected: list[dict], samples: list[dict], service_status: dict, query: dict, status: str) -> dict:
    strategies = [
        "direct_classification",
        "description_hook_text",
        "description_hook_structured",
        "description_text_only",
        "description_structured",
        "description_structured_title",
    ]
    comparison = {name: _strategy_metrics(samples, name) for name in strategies}
    window_count = sum(int(item.get("window_count") or 0) for item in samples)
    schema_valid = sum(int(item.get("schema_valid_window_count") or 0) for item in samples)
    audio_used = sum(int(item.get("audio_used_window_count") or 0) for item in samples)
    audio_eligible = sum(
        int(item.get("window_count") or 0)
        for item in samples
        if item.get("source_has_audio")
    )
    consistency_issue_count = sum(int(item.get("consistency_issue_count") or 0) for item in samples)
    completed_count = sum(1 for item in samples if item.get("status") in {"ready", "partial"})
    failed_count = sum(1 for item in samples if item.get("status") == "failed")
    description_metric = comparison["description_structured"]
    direct_metric = comparison["direct_classification"]
    if schema_valid < max(1, round(window_count * 0.8)):
        recommendation = "fix_description_schema_before_expansion"
    elif schema_valid and consistency_issue_count / schema_valid > 0.2:
        recommendation = "fix_evidence_signal_alignment_before_expansion"
    elif description_metric["canonical_accuracy"] >= direct_metric["canonical_accuracy"]:
        recommendation = "expand_to_30_gold_samples"
    else:
        recommendation = "keep_description_as_explanation_only"
    return {
        "contract_version": MATERIAL_DESCRIPTION_EXPERIMENT_VERSION,
        "status": status,
        "mode": "shadow_experiment",
        "query": query,
        "selection": {
            "requested_count": len(selected),
            "processed_count": len(samples),
            "completed_count": completed_count,
            "failed_count": failed_count,
            "confusion_pairs": sorted({str(item.get("confusion_pair") or "") for item in selected}),
            "selection_policy": "confirmed evaluable Gold, one sample per confusion pair before fill",
        },
        "description_quality": {
            "window_count": window_count,
            "schema_valid_window_count": schema_valid,
            "schema_valid_rate": round(schema_valid / max(1, window_count), 4),
            "audio_used_window_count": audio_used,
            "audio_used_rate": round(audio_used / max(1, window_count), 4),
            "audio_eligible_window_count": audio_eligible,
            "audio_use_coverage": round(audio_used / max(1, audio_eligible), 4),
            "consistency_issue_count": consistency_issue_count,
            "consistency_issue_rate": round(consistency_issue_count / max(1, schema_valid), 4),
        },
        "strategy_comparison": comparison,
        "recommendation": recommendation,
        "service_status": service_status,
        "samples": samples,
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _description_consistency_issues(description: dict) -> list[str]:
    text = _description_text(description).lower()
    signals = description.get("evidence_signals") if isinstance(description.get("evidence_signals"), dict) else {}
    issues = []
    if any(token in text for token in ["舞台", "唱歌", "演唱", "表演"]):
        if float(signals.get("sustained_performance") or 0.0) < float(signals.get("backstage_context") or 0.0):
            issues.append("performance_backstage_signal_conflict")
    speech_tokens = ["讲话", "讲解", "解说", "评论", "点评"]
    performance_tokens = ["唱歌", "演唱", "表演", "跳舞", "演奏"]
    if any(token in text for token in speech_tokens) and not any(token in text for token in performance_tokens):
        if float(signals.get("sustained_performance") or 0.0) > 0.5:
            issues.append("speech_performance_signal_conflict")
    if any(token in text for token in ["教学", "讲解唱法", "发声", "示范"]):
        if float(signals.get("teaching_instruction") or 0.0) <= 0.0:
            issues.append("teaching_signal_missing")
    if any(token in text for token in ["reaction", "观看反应", "点评", "带你看"]):
        if float(signals.get("viewing_reaction") or 0.0) <= 0.0:
            issues.append("reaction_signal_missing")
    return issues


def _description_text(description: dict) -> str:
    events = " ".join(str(value) for value in (description.get("observed_events") or []))
    visible_text = " ".join(str(value) for value in (description.get("visible_text") or []))
    return " ".join(
        value
        for value in [
            str(description.get("visual_summary") or ""),
            str(description.get("audio_summary") or ""),
            visible_text,
            events,
        ]
        if value.strip()
    )


def _compact_window(window: dict) -> dict:
    description = window.get("description") if isinstance(window.get("description"), dict) else {}
    direct = window.get("direct") if isinstance(window.get("direct"), dict) else {}
    return {
        "window": window.get("window") or "window",
        "start_seconds": window.get("start_seconds") or 0.0,
        "end_seconds": window.get("end_seconds") or 0.0,
        "direct_material_type": direct.get("material_type") or "unknown",
        "direct_confidence": direct.get("confidence") or 0.0,
        "description": {key: value for key, value in description.items() if key != "raw_payload"},
        "consistency_issues": _description_consistency_issues(description),
        "direct_latency_seconds": window.get("direct_latency_seconds") or 0.0,
        "description_latency_seconds": window.get("description_latency_seconds") or 0.0,
        "cache_hit": bool(window.get("cache_hit")),
    }


def _experiment_window_ready(window: dict, *, require_direct: bool) -> bool:
    direct = window.get("direct") if isinstance(window.get("direct"), dict) else {}
    description = window.get("description") if isinstance(window.get("description"), dict) else {}
    return bool(description.get("schema_valid") and (not require_direct or direct.get("prompt_supported")))


def _description_cache_path(sample: dict, video_path: Path, window_seconds: float, windows_per_sample: int) -> Path:
    stat = video_path.stat()
    digest = hashlib.sha256(
        "|".join(
            [
                MATERIAL_DESCRIPTION_EXPERIMENT_VERSION,
                str(video_path.resolve()),
                str(stat.st_size),
                str(stat.st_mtime_ns),
                f"{window_seconds:.3f}",
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    sample_id = str(sample.get("sample_id") or "sample")
    return ensure_data_dirs().cache_dir / "material_description_experiment" / "d10c" / f"{sample_id}_{digest}.json"


def _legacy_description_cache_path(
    sample: dict,
    video_path: Path,
    window_seconds: float,
    windows_per_sample: int,
) -> Path:
    stat = video_path.stat()
    digest = hashlib.sha256(
        "|".join(
            [
                MATERIAL_DESCRIPTION_EXPERIMENT_VERSION,
                str(video_path.resolve()),
                str(stat.st_size),
                str(stat.st_mtime_ns),
                f"{window_seconds:.3f}",
                str(windows_per_sample),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    sample_id = str(sample.get("sample_id") or "sample")
    return ensure_data_dirs().cache_dir / "material_description_experiment" / "d10c" / f"{sample_id}_{digest}.json"


def _default_report_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ensure_data_dirs().root / "outputs" / "material_description_experiment" / f"pilot_{stamp}.json"


def _write_report(path: Path, report: dict) -> None:
    write_json(path, report)
    write_json(ensure_data_dirs().root / "outputs" / "material_description_experiment" / "latest.json", report)


def _unit_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, score)), 4)
