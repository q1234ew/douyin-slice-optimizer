from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all
from dso.learning.qwen_omni import QWEN_OMNI_MODEL, QwenOmniClient, qwen_omni_status
from dso.media.ffmpeg import require_binary
from dso.scoring.scorer import score_video, suggestions
from dso.segments.generator import generate_segments
from dso.utils import clamp, read_json, run_cmd, utc_now, write_json
from dso.versions import HYBRID_SLICE_PIPELINE_VERSION, OMNI_SLICE_RANKER_VERSION


DEFAULT_CANDIDATE_LIMIT = 3
DEFAULT_MAX_CLIP_SECONDS = 6.0
DEFAULT_OMNI_WEIGHT = 0.15


def run_hybrid_slice_pipeline(
    video_id: str,
    *,
    top_k: int = 10,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS,
    omni_weight: float = DEFAULT_OMNI_WEIGHT,
    load_model: bool = False,
    force: bool = False,
    client: QwenOmniClient | None = None,
) -> dict:
    recall_count = max(30, int(candidate_limit or DEFAULT_CANDIDATE_LIMIT) * 4, int(top_k or 10) * 3)
    segments = generate_segments(video_id, top_k=recall_count)
    scores = score_video(video_id)
    ranking = rerank_video_candidates_with_omni(
        video_id,
        candidate_limit=candidate_limit,
        max_clip_seconds=max_clip_seconds,
        omni_weight=omni_weight,
        load_model=load_model,
        force=force,
        client=client,
    )
    with connect() as conn:
        conn.execute(
            "UPDATE source_videos SET status = 'scored', updated_at = ? WHERE id = ?",
            [utc_now(), video_id],
        )
        conn.commit()
    return {
        "contract_version": HYBRID_SLICE_PIPELINE_VERSION,
        "status": ranking.get("status") or "fallback",
        "video_id": video_id,
        "pipeline": {
            "recall": "timeline_signal_segmenter",
            "pre_rank": "current_rules",
            "rerank": "qwen_omni_multi_window_research",
            "fallback": "current_rules",
        },
        "counts": {
            "recalled": len(segments),
            "scored": len(scores),
            "preselected": int(ranking.get("preselected_count") or 0),
            "omni_applied": int(ranking.get("omni_applied_count") or 0),
        },
        "ranking": ranking,
        "suggestions": suggestions(video_id, top_k=max(1, int(top_k or 10))),
        "generated_at": utc_now(),
        "production_weight": False,
        "research_only": True,
    }


def rerank_video_candidates_with_omni(
    video_id: str,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS,
    omni_weight: float = DEFAULT_OMNI_WEIGHT,
    load_model: bool = False,
    force: bool = False,
    client: QwenOmniClient | None = None,
) -> dict:
    rows = _scored_candidate_rows(video_id)
    if not rows:
        return _empty_report(video_id, "no_scored_candidates")
    candidate_limit = max(1, min(20, int(candidate_limit or DEFAULT_CANDIDATE_LIMIT)))
    clip_limit = max(4.0, min(15.0, float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS)))
    max_weight = max(0.0, min(0.30, float(omni_weight or 0.0)))
    client = client or QwenOmniClient(timeout_seconds=90.0)
    if load_model:
        try:
            client.load(max_clip_seconds=clip_limit)
        except Exception:
            pass
    deployment = qwen_omni_status(client=client)
    ready = deployment.get("status") == "ready"
    selected = rows[:candidate_limit]
    selected_ids = {str(row.get("id") or "") for row in selected}
    results: list[dict] = []

    if not ready:
        reason = str(deployment.get("status") or "model_unavailable")
        _persist_fallback(rows, status=f"fallback_{reason}", reason=reason)
        return {
            "contract_version": OMNI_SLICE_RANKER_VERSION,
            "status": "fallback",
            "video_id": video_id,
            "model": deployment.get("model") or getattr(client, "model_id", QWEN_OMNI_MODEL),
            "deployment": deployment,
            "fallback_reason": reason,
            "preselected_count": len(selected),
            "omni_applied_count": 0,
            "candidate_limit": candidate_limit,
            "max_clip_seconds": clip_limit,
            "omni_weight_cap": max_weight,
            "recommendation": "模型未就绪，已保持 current_rules 默认排序；候选和导出流程不受影响。",
            "production_weight": False,
            "research_only": True,
            "generated_at": utc_now(),
        }

    for row in selected:
        try:
            result = _analyze_candidate_multi_window(
                row,
                client=client,
                max_clip_seconds=clip_limit,
                max_weight=max_weight,
                force=force,
            )
        except Exception as exc:
            result = _candidate_failure(row, str(exc))
        results.append(result)

    by_id = {str(item.get("segment_id") or ""): item for item in results}
    ranked_rows: list[dict] = []
    for row in rows:
        segment_id = str(row.get("id") or "")
        base_score = _base_score(row)
        result = by_id.get(segment_id)
        if result and result.get("status") == "ready":
            hybrid_score = float(result.get("hybrid_score") or base_score)
            omni_score = float(result.get("omni_score") or 0.0)
            confidence = float(result.get("confidence") or 0.0)
            status = "ready"
            analysis = result
        else:
            hybrid_score = base_score
            omni_score = 0.0
            confidence = 0.0
            status = "error" if result else "not_selected"
            analysis = result or {
                "status": "not_selected",
                "reason": "outside_preselection_pool",
                "candidate_limit": candidate_limit,
            }
        ranked_rows.append(
            {
                "segment_id": segment_id,
                "base_score": base_score,
                "hybrid_score": round(hybrid_score, 2),
                "omni_score": round(omni_score, 2),
                "confidence": round(confidence, 4),
                "omni_status": status,
                "analysis": analysis,
            }
        )
    ranked_rows.sort(key=lambda item: (item["hybrid_score"], item["base_score"]), reverse=True)
    _persist_hybrid_results(ranked_rows)
    applied = sum(1 for item in ranked_rows if item["omni_status"] == "ready")
    cache_hits = sum(1 for item in results if item.get("cache_hit"))
    return {
        "contract_version": OMNI_SLICE_RANKER_VERSION,
        "status": "ready" if applied else "fallback",
        "video_id": video_id,
        "model": deployment.get("model") or getattr(client, "model_id", QWEN_OMNI_MODEL),
        "deployment": deployment,
        "preselected_count": len(selected_ids),
        "omni_applied_count": applied,
        "cache_hit_count": cache_hits,
        "candidate_limit": candidate_limit,
        "max_clip_seconds": clip_limit,
        "omni_weight_cap": max_weight,
        "ranked": ranked_rows[:candidate_limit],
        "recommendation": "Omni 只影响候选复排，不自动改写人工标签或导出边界。",
        "production_weight": False,
        "research_only": True,
        "generated_at": utc_now(),
    }


def candidate_window_plan(row: dict, *, max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS) -> list[dict]:
    duration = max(0.0, float(row.get("duration_seconds") or 0.0))
    candidate_start = max(0.0, float(row.get("start_time") or 0.0))
    clip = max(4.0, min(float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS), max(4.0, duration)))
    relative: list[tuple[str, float]] = [("hook", 0.0)]
    if duration > clip * 1.7:
        relative.append(("middle", max(0.0, (duration - clip) / 2.0)))
    if duration > clip * 1.15:
        relative.append(("payoff", max(0.0, duration - clip)))
    plan: list[dict] = []
    for role, relative_start in relative:
        if any(abs(relative_start - float(item["relative_start_seconds"])) < clip * 0.45 for item in plan):
            continue
        window_duration = min(clip, max(0.0, duration - relative_start))
        plan.append(
            {
                "window": role,
                "relative_start_seconds": round(relative_start, 3),
                "absolute_start_seconds": round(candidate_start + relative_start, 3),
                "duration_seconds": round(window_duration, 3),
                "absolute_end_seconds": round(candidate_start + relative_start + window_duration, 3),
            }
        )
    return plan


def _analyze_candidate_multi_window(
    row: dict,
    *,
    client: QwenOmniClient,
    max_clip_seconds: float,
    max_weight: float,
    force: bool,
) -> dict:
    cache_path = _result_cache_path(row, model_id=getattr(client, "model_id", QWEN_OMNI_MODEL), max_clip_seconds=max_clip_seconds)
    if cache_path.exists() and not force:
        cached = read_json(cache_path, default={}) or {}
        if cached.get("status") == "ready":
            return {**cached, "cache_hit": True, "result_cache_path": str(cache_path)}
    plan = candidate_window_plan(row, max_clip_seconds=max_clip_seconds)
    window_results = []
    for window in plan:
        clip_path, media = _prepare_candidate_window(row, window)
        payload = _window_payload(row, window, model_id=getattr(client, "model_id", QWEN_OMNI_MODEL))
        raw = client.analyze_clip_file(payload, clip_path)
        normalized = _normalize_window_result(raw, role=str(window.get("window") or "middle"))
        window_results.append({"window": window, "media": media, **normalized, "raw": raw})
    if not window_results:
        raise RuntimeError("no_candidate_windows")
    omni_score, confidence = _aggregate_window_results(window_results)
    base_score = _base_score(row)
    effective_weight = max_weight * confidence
    hybrid_score = clamp(base_score * (1.0 - effective_weight) + omni_score * effective_weight)
    result = {
        "contract_version": OMNI_SLICE_RANKER_VERSION,
        "status": "ready",
        "segment_id": row.get("id") or "",
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "base_score": round(base_score, 2),
        "omni_score": round(omni_score, 2),
        "confidence": round(confidence, 4),
        "effective_weight": round(effective_weight, 4),
        "hybrid_score": round(hybrid_score, 2),
        "window_count": len(window_results),
        "window_roles": [str(item["window"].get("window") or "") for item in window_results],
        "windows": window_results,
        "boundary_advice": _boundary_advice(window_results),
        "writes_labels": False,
        "adjusts_boundaries": False,
        "generated_at": utc_now(),
    }
    write_json(cache_path, result)
    return {**result, "cache_hit": False, "result_cache_path": str(cache_path)}


def _prepare_candidate_window(row: dict, window: dict) -> tuple[Path, dict]:
    source = Path(str(row.get("file_path") or "")).expanduser()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    cache_path = _window_cache_path(row, window, source)
    cache_hit = cache_path.is_file() and cache_path.stat().st_size > 0
    if not cache_hit:
        require_binary("ffmpeg")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{float(window.get('absolute_start_seconds') or 0.0):.3f}",
            "-t",
            f"{float(window.get('duration_seconds') or DEFAULT_MAX_CLIP_SECONDS):.3f}",
            "-i",
            str(source),
            "-vf",
            "scale=640:-2,fps=2",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "27",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(cache_path),
        ]
        try:
            run_cmd(command)
        except Exception as exc:
            stderr = str(getattr(exc, "stderr", "") or "")
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else str(exc)
            raise RuntimeError(f"omni_window_transcode_failed: {detail}") from exc
    return cache_path, {
        "clip_path": str(cache_path),
        "source_path": str(source),
        "cache_hit": cache_hit,
        "window": window.get("window") or "",
        "absolute_start_seconds": window.get("absolute_start_seconds") or 0,
        "duration_seconds": window.get("duration_seconds") or 0,
    }


def _window_payload(row: dict, window: dict, *, model_id: str) -> dict:
    return {
        "model": model_id,
        "mode": "hybrid_slice_rerank",
        "prompt_profile": "hybrid_slice_rerank",
        "max_new_tokens": 128,
        "return_audio": False,
        "entity_type": "candidate_window",
        "segment_id": row.get("id") or "",
        "window_role": window.get("window") or "middle",
        "title": row.get("video_title") or row.get("summary") or "",
        "transcript": row.get("transcript") or "",
        "candidate_range": {
            "start_seconds": row.get("start_time") or 0,
            "end_seconds": row.get("end_time") or 0,
            "duration_seconds": row.get("duration_seconds") or 0,
        },
        "window_range": window,
        "score_schema": {
            "scale": "0-100",
            "required": [
                "hook_strength",
                "context_completeness",
                "payoff_strength",
                "reaction_strength",
                "audio_visual_alignment",
                "boundary_quality",
                "risk",
            ],
        },
        "analysis_prompt": [
            "Analyze the uploaded audio-video window as evidence for short-video slice ranking.",
            "Return JSON only with scores, evidence, boundary_advice, risk_flags, and advice.",
            "Use 0-100 scores for hook_strength, context_completeness, payoff_strength, reaction_strength, audio_visual_alignment, boundary_quality, and risk.",
            "Judge only visible or audible evidence. Do not predict views, virality, or platform distribution.",
            "The exact window role is supplied in window_role; score that role while considering the candidate transcript.",
            "boundary_advice is advisory only and may contain start_adjust_seconds and end_adjust_seconds.",
        ],
    }


def _normalize_window_result(raw: dict, *, role: str) -> dict:
    scores = _raw_scores(raw)
    audio = _score_from(scores, ["audio_visual_alignment", "audio_moment", "audio_score"], 55.0)
    stage = _score_from(scores, ["stage_moment", "visual_moment", "visual_score"], 55.0)
    hook = _score_from(scores, ["hook_strength", "hook", "hook_score"], (audio + stage) / 2.0)
    context = _score_from(scores, ["context_completeness", "context", "narrative_context"], 52.0)
    payoff = _score_from(scores, ["payoff_strength", "payoff", "climax", "climax_score"], (audio + stage) / 2.0)
    reaction = _score_from(scores, ["reaction_strength", "reaction", "audience_reaction"], 50.0)
    alignment = _score_from(scores, ["audio_visual_alignment", "alignment"], (audio + stage) / 2.0)
    boundary = _score_from(scores, ["boundary_quality", "boundary", "cut_quality"], 55.0)
    risk = _score_from(scores, ["risk", "risk_score"], 10.0)
    normalized = {
        "hook_strength": hook,
        "context_completeness": context,
        "payoff_strength": payoff,
        "reaction_strength": reaction,
        "audio_visual_alignment": alignment,
        "boundary_quality": boundary,
        "risk": risk,
    }
    weights = {
        "hook": {"hook_strength": 0.42, "context_completeness": 0.12, "payoff_strength": 0.12, "reaction_strength": 0.06, "audio_visual_alignment": 0.16, "boundary_quality": 0.12},
        "middle": {"hook_strength": 0.12, "context_completeness": 0.28, "payoff_strength": 0.18, "reaction_strength": 0.08, "audio_visual_alignment": 0.20, "boundary_quality": 0.14},
        "payoff": {"hook_strength": 0.08, "context_completeness": 0.08, "payoff_strength": 0.36, "reaction_strength": 0.22, "audio_visual_alignment": 0.16, "boundary_quality": 0.10},
    }.get(role, {})
    role_score = sum(normalized[key] * value for key, value in weights.items()) - risk * 0.20
    coverage = sum(1 for key in normalized if any(alias in scores for alias in _score_aliases(key))) / len(normalized)
    confidence = min(1.0, 0.48 + coverage * 0.42)
    return {
        "normalized_scores": {key: round(value, 2) for key, value in normalized.items()},
        "role_score": round(clamp(role_score), 2),
        "confidence": round(confidence, 4),
        "evidence": _raw_field(raw, "evidence", []),
        "boundary_advice": _raw_field(raw, "boundary_advice", {}),
        "risk_flags": _raw_field(raw, "risk_flags", []),
        "advice": _raw_field(raw, "advice", "recommend_review"),
    }


def _aggregate_window_results(results: list[dict]) -> tuple[float, float]:
    role_weights = {"hook": 0.38, "middle": 0.24, "payoff": 0.38}
    weighted = []
    for item in results:
        role = str((item.get("window") or {}).get("window") or "middle")
        weighted.append((float(item.get("role_score") or 0.0), role_weights.get(role, 0.25)))
    total_weight = sum(weight for _, weight in weighted) or 1.0
    score = sum(value * weight for value, weight in weighted) / total_weight
    confidence = sum(float(item.get("confidence") or 0.0) for item in results) / max(1, len(results))
    role_factor = min(1.0, 0.62 + len(results) * 0.13)
    return round(clamp(score), 2), round(min(1.0, confidence * role_factor), 4)


def _boundary_advice(results: list[dict]) -> dict:
    advice = []
    for item in results:
        value = item.get("boundary_advice")
        if value:
            advice.append({"window": (item.get("window") or {}).get("window") or "", "advice": value})
    return {"status": "advisory_only", "items": advice}


def _raw_scores(raw: dict) -> dict:
    semantic = raw.get("semantic_suggestions") if isinstance(raw.get("semantic_suggestions"), dict) else {}
    candidates = [
        raw.get("scores"),
        semantic.get("scores"),
        semantic,
        (raw.get("result") or {}).get("scores") if isinstance(raw.get("result"), dict) else None,
    ]
    parsed = _parsed_json_payload(raw)
    if parsed:
        candidates.extend([parsed.get("scores"), parsed])
    merged: dict[str, Any] = {}
    for item in candidates:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _parsed_json_payload(raw: dict) -> dict:
    semantic = raw.get("semantic_suggestions")
    if isinstance(semantic, dict) and semantic:
        return semantic
    for key in ["response", "text", "output", "content", "raw_text"]:
        value = raw.get(key)
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            continue
        text = value.strip()
        if "```" in text:
            text = text.replace("```json", "").replace("```", "").strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


def _raw_field(raw: dict, key: str, default: Any) -> Any:
    if key in raw:
        return raw.get(key)
    semantic = raw.get("semantic_suggestions")
    if isinstance(semantic, dict) and key in semantic:
        return semantic.get(key)
    parsed = _parsed_json_payload(raw)
    return parsed.get(key, default) if parsed else default


def _score_from(scores: dict, aliases: list[str], default: float) -> float:
    for alias in aliases:
        if alias not in scores:
            continue
        try:
            value = float(scores[alias])
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 1.0:
            value *= 100.0
        return clamp(value)
    return clamp(default)


def _score_aliases(field: str) -> list[str]:
    return {
        "hook_strength": ["hook_strength", "hook", "hook_score"],
        "context_completeness": ["context_completeness", "context", "narrative_context"],
        "payoff_strength": ["payoff_strength", "payoff", "climax", "climax_score", "audio_moment"],
        "reaction_strength": ["reaction_strength", "reaction", "audience_reaction"],
        "audio_visual_alignment": ["audio_visual_alignment", "alignment", "audio_moment", "stage_moment"],
        "boundary_quality": ["boundary_quality", "boundary", "cut_quality"],
        "risk": ["risk", "risk_score"],
    }.get(field, [field])


def _scored_candidate_rows(video_id: str) -> list[dict]:
    with connect() as conn:
        return fetch_all(
            conn,
            """
            SELECT c.*, v.account_id, v.title AS video_title, v.file_path,
                   s.final_score, s.ranker_score, s.ranker_version
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            JOIN slice_scores s ON s.candidate_segment_id = c.id
            WHERE c.source_video_id = ?
            ORDER BY s.final_score DESC, c.id ASC
            """,
            [video_id],
        )


def _persist_fallback(rows: list[dict], *, status: str, reason: str) -> None:
    payload = json.dumps({"status": status, "reason": reason, "fallback": "current_rules"}, ensure_ascii=False)
    ranked = sorted(rows, key=_base_score, reverse=True)
    with connect() as conn:
        for rank, row in enumerate(ranked, start=1):
            conn.execute(
                """
                UPDATE slice_scores
                SET omni_score = 0, omni_confidence = 0, omni_status = ?, omni_analysis_json = ?,
                    hybrid_score = ?, hybrid_rank = ?, hybrid_ranker_version = ?
                WHERE candidate_segment_id = ?
                """,
                [status, payload, _base_score(row), rank, OMNI_SLICE_RANKER_VERSION, row["id"]],
            )
        conn.commit()


def _persist_hybrid_results(rows: list[dict]) -> None:
    with connect() as conn:
        for rank, item in enumerate(rows, start=1):
            conn.execute(
                """
                UPDATE slice_scores
                SET omni_score = ?, omni_confidence = ?, omni_status = ?, omni_analysis_json = ?,
                    hybrid_score = ?, hybrid_rank = ?, hybrid_ranker_version = ?
                WHERE candidate_segment_id = ?
                """,
                [
                    item["omni_score"],
                    item["confidence"],
                    item["omni_status"],
                    json.dumps(item["analysis"], ensure_ascii=False),
                    item["hybrid_score"],
                    rank,
                    OMNI_SLICE_RANKER_VERSION,
                    item["segment_id"],
                ],
            )
        conn.commit()


def _candidate_failure(row: dict, error: str) -> dict:
    base_score = _base_score(row)
    return {
        "contract_version": OMNI_SLICE_RANKER_VERSION,
        "status": "error",
        "segment_id": row.get("id") or "",
        "reason": "candidate_analysis_failed",
        "error": error[:500],
        "base_score": base_score,
        "hybrid_score": base_score,
        "omni_score": 0.0,
        "confidence": 0.0,
        "writes_labels": False,
        "adjusts_boundaries": False,
    }


def _base_score(row: dict) -> float:
    return round(float(row.get("final_score") or 0.0), 2)


def _result_cache_path(row: dict, *, model_id: str, max_clip_seconds: float) -> Path:
    source = Path(str(row.get("file_path") or ""))
    signature = _source_signature(source)
    raw = "|".join(
        [
            signature,
            str(row.get("start_time") or 0),
            str(row.get("end_time") or 0),
            str(row.get("transcript") or ""),
            str(model_id),
            str(max_clip_seconds),
            OMNI_SLICE_RANKER_VERSION,
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:18]
    return ensure_data_dirs().cache_dir / "omni_slice_results" / str(row.get("source_video_id") or "video") / f"{digest}.json"


def _window_cache_path(row: dict, window: dict, source: Path) -> Path:
    raw = "|".join(
        [
            _source_signature(source),
            str(window.get("absolute_start_seconds") or 0),
            str(window.get("duration_seconds") or 0),
            str(window.get("window") or ""),
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return ensure_data_dirs().cache_dir / "omni_slice_windows" / str(row.get("source_video_id") or "video") / f"{digest}.mp4"


def _source_signature(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return str(path)


def _empty_report(video_id: str, reason: str) -> dict:
    return {
        "contract_version": OMNI_SLICE_RANKER_VERSION,
        "status": "empty",
        "video_id": video_id,
        "fallback_reason": reason,
        "preselected_count": 0,
        "omni_applied_count": 0,
        "production_weight": False,
        "research_only": True,
        "generated_at": utc_now(),
    }
