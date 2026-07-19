from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shlex
from functools import lru_cache
from pathlib import Path

from dso.config import ensure_data_dirs
from dso.db.session import connect
from dso.features.asr_profile import normalize_asr_profile, resolve_asr_model_size
from dso.features.asr_routing import qwen3_asr_primary_policy, route_video_asr
from dso.features.qwen3_asr import (
    Qwen3ASRError,
    qwen3_asr_cache_config,
    qwen3_asr_health,
    qwen3_asr_model,
    transcribe_wav as transcribe_wav_with_qwen3_asr,
)
from dso.features.whisper_cpp import (
    whisper_cpp_binary,
    whisper_cpp_language,
    whisper_cpp_model,
    whisper_cpp_ready,
    whisper_cpp_vad_enabled,
    whisper_cpp_vad_model,
)
from dso.media.ffmpeg import extract_audio
from dso.media.ingest import get_video
from dso.text.zh_hans import to_zh_hans
from dso.utils import read_json, run_cmd, seconds_to_srt_time, utc_now, write_json
from dso.versions import ASR_MODEL_ROUTING_VERSION

POSTPROCESS_VERSION = "2026-06-29.1"
DEFAULT_ASR_PROMPT = (
    "音乐综艺节目中文转写。常见词包括：歌手2025、陈楚生、张韶涵、导师、评委、"
    "副歌、高音、转调、升key、改编、晋级、淘汰、听审、芒果卡、动感地带、"
    "范玮琪、陈楚生、王老吉、vivo、白雀羚、欧丽薇兰、压力、彩排、离开舞台、"
    "补位歌手。英文歌手名保持原文，英文歌词保持英文。"
)
HOTWORD_REPLACEMENTS = {
    "清春": "青春",
    "清水印记": "青春印记",
    "盲國卡": "芒果卡",
    "盲国卡": "芒果卡",
    "芒國卡": "芒果卡",
    "範圍期": "范玮琪",
    "范围期": "范玮琪",
    "範圍棋": "范玮琪",
    "范围棋": "范玮琪",
    "范維奇": "范玮琪",
    "范维奇": "范玮琪",
    "范維琪": "范玮琪",
    "范维琪": "范玮琪",
    "范偉琪": "范玮琪",
    "范偉奇": "范玮琪",
    "魏范維奇": "范玮琪",
    "魏范维奇": "范玮琪",
    "飯飯": "范范",
    "饭饭": "范范",
    "犯法": "范范",
    "陳楚聲": "陈楚生",
    "陈楚聲": "陈楚生",
    "陳處生": "陈楚生",
    "陈處生": "陈楚生",
    "陳处生": "陈楚生",
    "陈处生": "陈楚生",
    "廚生": "陈楚生",
    "厨生": "陈楚生",
    "沉處生": "陈楚生",
    "沉处生": "陈楚生",
    "楚聲": "楚生",
    "楚山歌": "楚生哥",
    "賽值": "赛制",
    "赛值": "赛制",
    "賽指": "赛制",
    "赛指": "赛制",
    "排名莫位": "排名末位",
    "本場最終排名莫位": "本场最终排名末位",
    "本场最终排名莫位": "本场最终排名末位",
    "腰跃": "邀约",
    "邀跃": "邀约",
    "经验顺序": "竞演顺序",
    "经验结果": "竞演结果",
    "经验排名": "竞演排名",
    "經驗結果": "竞演结果",
    "經驗排名": "竞演排名",
    "下一周的经验": "下一周的竞演",
    "下一週的經驗": "下一周的竞演",
    "下次最多的一次": "想得最多的一次",
    "这次正式的这次唱": "这次正式地唱",
    "聽神": "听审",
    "听神": "听审",
    "敬眼": "竞演",
    "進演": "竞演",
    "季節順序": "竞演顺序",
    "季节顺序": "竞演顺序",
    "Grease": "Grace",
    "玄律": "旋律",
    "部位歌手": "补位歌手",
    "不畏歌手": "补位歌手",
    "補位歌手": "补位歌手",
    "NP3": "MP3",
    "名詞沒有接小": "名次没有揭晓",
    "名词没有接小": "名次没有揭晓",
    "王老级": "王老吉",
    "王老積": "王老吉",
    "白确灵": "白雀羚",
    "白却灵": "白雀羚",
    "欧利威兰": "欧丽薇兰",
    "欧丽威兰": "欧丽薇兰",
    "猛牛酸酸乳": "蒙牛酸酸乳",
    "升 key": "升key",
    "升Key": "升key",
}
AD_KEYWORDS = [
    "合作伙伴",
    "超級合作夥伴",
    "超级合作伙伴",
    "提醒您",
    "銷量第一",
    "销量第一",
    "怕上火",
    "广告",
    "廣告",
    "VIP",
    "vivo",
    "王老吉",
    "白雀羚",
    "欧丽薇兰",
    "動感地帶",
    "动感地带",
    "芒果卡",
    "盲國卡",
    "盲国卡",
    "歌手听我唱",
    "歌手聽我唱",
    "合唱官",
    "扫码",
    "掃碼",
    "直播间",
    "直播間",
    "直拍",
    "QQ音乐",
    "QQ音樂",
    "网易云",
    "網易雲",
    "汽水音乐",
    "汽水音樂",
    "酷狗",
    "酷我",
    "酸酸乳",
    "蒙牛",
    "猛流酸酸乳",
    "官方帐号",
    "官方帳號",
    "参与互动",
    "參與互動",
    "登录",
    "登入",
    "收听",
    "收聽",
]


def transcribe_video(
    video_id: str,
    *,
    model_size: str | None = None,
    asr_profile: str | None = None,
    backend: str | None = None,
    force: bool = False,
) -> dict:
    settings = ensure_data_dirs()
    video = get_video(video_id)
    requested_profile = asr_profile if asr_profile is not None else os.getenv("DSO_ASR_PROFILE")
    routing = route_video_asr(video, requested_profile=requested_profile, model_size=model_size)
    profile_name = routing["recommended_profile"]
    model_size = resolve_asr_model_size(model_size, profile=profile_name)
    primary_policy = qwen3_asr_primary_policy(video)
    requested_backend = (backend or os.getenv("DSO_ASR_BACKEND", "auto")).strip().lower() or "auto"
    effective_backend = "qwen3_asr_preferred" if requested_backend == "auto" and primary_policy["eligible"] else backend
    routing = {
        **routing,
        "recommended_model": model_size,
        "primary": primary_policy,
        "requested_backend": requested_backend,
        "backend_preference": effective_backend or requested_backend,
    }
    video_path = Path(video["file_path"])
    transcript_dir = settings.cache_dir / video_id / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / "transcript.json"

    sidecar = _find_sidecar_transcript(video_path)
    if sidecar:
        segments = post_process_segments(_parse_srt(sidecar))
        source = "sidecar_srt"
        metadata = {
            "cache_key": {
                "backend": "sidecar_srt",
                "path": str(sidecar),
                "postprocess_version": POSTPROCESS_VERSION,
            },
            "cache_hit": False,
            "profile": profile_name,
            "model_size": model_size,
            "routing": routing,
        }
    else:
        audio_path = transcript_dir / "audio.wav"
        extract_audio(video_path, audio_path)
        cache_key = _asr_cache_key(audio_path, model_size, profile_name, effective_backend)
        previous_transcript = read_json(transcript_path, default=None)
        cached = _cached_transcript(transcript_path, cache_key, force=force)
        if cached:
            _mark_transcribed(transcript_path, video_id)
            return cached
        result = transcribe_audio_file(
            audio_path,
            transcript_dir,
            model_size=model_size,
            asr_profile=profile_name,
            backend=effective_backend,
            routing_context=routing,
        )
        segments = result["segments"]
        source = result["source"]
        if not segments:
            preserved = _preserve_previous_transcript(
                previous_transcript,
                cache_key=cache_key,
                failed_source=source,
                routing=routing,
            )
            if preserved:
                _mark_transcribed(transcript_path, video_id)
                return preserved
            segments = _placeholder_segments(float(video["duration_seconds"]))
            source = "placeholder"
        selected_backend = source.split(":", 1)[0]
        fallback_used = effective_backend == "qwen3_asr_preferred" and not source.startswith("qwen3_asr:")
        routing = {
            **routing,
            "selected_backend": selected_backend,
            "fallback_used": fallback_used,
            "fallback_backend": selected_backend if fallback_used else "",
        }
        metadata = {
            **result.get("metadata", {}),
            "routing": routing,
            "cache_key": cache_key,
            "cache_hit": False,
        }

    data = {
        "video_id": video_id,
        "source": source,
        "segments": segments,
        "metadata": metadata,
        "created_at": utc_now(),
    }
    write_json(transcript_path, data)
    _mark_transcribed(transcript_path, video_id)
    return data


def _preserve_previous_transcript(
    previous: object,
    *,
    cache_key: dict,
    failed_source: str,
    routing: dict,
) -> dict | None:
    if not isinstance(previous, dict):
        return None
    previous_segments = previous.get("segments")
    previous_source = str(previous.get("source") or "")
    previous_metadata = previous.get("metadata") if isinstance(previous.get("metadata"), dict) else {}
    previous_key = previous_metadata.get("cache_key") if isinstance(previous_metadata.get("cache_key"), dict) else {}
    same_audio = bool(previous_key.get("audio_sha256")) and (
        previous_key.get("audio_sha256") == cache_key.get("audio_sha256")
    )
    if not same_audio or not isinstance(previous_segments, list) or not previous_segments:
        return None
    if not previous_source or previous_source == "placeholder":
        return None

    selected_backend = previous_source.split(":", 1)[0]
    preferred_qwen = str(cache_key.get("backend_preference") or "") in {
        "qwen3_asr_preferred",
        "qwen3-asr-preferred",
    }
    preserved_routing = {
        **routing,
        "selected_backend": selected_backend,
        "fallback_used": preferred_qwen and selected_backend != "qwen3_asr",
        "fallback_backend": selected_backend if preferred_qwen and selected_backend != "qwen3_asr" else "",
        "preserved_previous_transcript": True,
        "failed_attempt_source": failed_source,
    }
    return {
        **previous,
        "cache_hit": True,
        "metadata": {
            **previous_metadata,
            "cache_hit": True,
            "stale_fallback": True,
            "routing": preserved_routing,
            "failed_attempt": {
                "source": failed_source,
                "cache_key": cache_key,
                "created_at": utc_now(),
            },
        },
    }


def transcribe_audio_file(
    audio_path: Path,
    transcript_dir: Path,
    *,
    model_size: str | None = None,
    asr_profile: str | None = None,
    backend: str | None = None,
    routing_context: dict | None = None,
) -> dict:
    profile_name = normalize_asr_profile(asr_profile)
    model_size = resolve_asr_model_size(model_size, profile=profile_name)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    segments, source = _try_configured_asr(audio_path, transcript_dir, model_size, backend=backend)
    processed = post_process_segments(segments)
    effective_model = qwen3_asr_model() if source.startswith("qwen3_asr:") else model_size
    return {
        "source": source,
        "segments": processed,
        "metadata": {
            "backend": source.split(":", 1)[0],
            "profile": profile_name,
            "model_size": effective_model,
            "prompt": asr_prompt(),
            "postprocess_version": POSTPROCESS_VERSION,
            "segment_count_raw": len(segments),
            "segment_count_processed": len(processed),
            "routing": routing_context
            or {
                "contract_version": ASR_MODEL_ROUTING_VERSION,
                "scope": "audio_file",
                "decision": "manual_profile",
                "recommended_profile": profile_name,
                "recommended_model": model_size,
                "reason_keys": ["manual_profile"],
                "reasons": ["手动指定 ASR profile"],
                "candidate_only": False,
                "preserve_quality_result": profile_name == "verify",
                "signals": {},
            },
        },
    }


def commit_scheduled_qwen3_asr(
    video_id: str,
    audio_path: Path,
    item_results: list[dict],
    *,
    expected_audio_sha256: str,
    config: dict,
    role: str = "primary",
) -> dict:
    """Commit fenced per-chunk ASR results, or preserve/fallback safely."""

    if not audio_path.is_file():
        raise RuntimeError("input_missing: scheduled ASR audio artifact is missing")
    if _file_sha256(audio_path) != expected_audio_sha256:
        raise RuntimeError("input_changed: scheduled ASR audio changed before commit")
    settings = ensure_data_dirs()
    transcript_dir = settings.cache_dir / video_id / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    chunk_results = [item.get("result") for item in item_results if isinstance(item.get("result"), dict)]
    failed_chunks = sum(
        1
        for item in item_results
        if item.get("item_status") == "failed"
        or not isinstance(item.get("result"), dict)
        or (item.get("result") or {}).get("status") == "failed"
    )
    qwen_segments = []
    diagnostics = []
    for result in chunk_results:
        qwen_segments.extend(result.get("segments") or [])
        if isinstance(result.get("diagnostics"), dict):
            diagnostics.append({"chunk_index": result.get("chunk_index"), **result["diagnostics"]})

    fallback_used = False
    fallback_source = ""
    if failed_chunks or not qwen_segments:
        if role == "shadow":
            return {
                "contract_version": "model_scheduler_asr.v1",
                "status": "failed",
                "video_id": video_id,
                "source": "qwen3_asr_failed",
                "segment_count": 0,
                "completed_chunk_count": len(item_results) - failed_chunks,
                "failed_chunk_count": failed_chunks,
                "fallback_used": False,
                "role": "shadow",
                "active_transcript_preserved": True,
                "writes_manual_gold": False,
                "adjusts_boundaries": False,
            }
        fallback = transcribe_audio_file(
            audio_path,
            transcript_dir,
            asr_profile="quality",
            backend="whisper_cpp",
            routing_context={
                "contract_version": "model_scheduler_asr.v1",
                "scope": "scheduled_fallback",
                "decision": "qwen3_asr_failed_whisper_fallback",
                "candidate_only": False,
                "preserve_quality_result": True,
            },
        )
        if fallback.get("segments"):
            segments = list(fallback["segments"])
            source = str(fallback.get("source") or "whisper_cpp")
            metadata = dict(fallback.get("metadata") or {})
            fallback_used = True
            fallback_source = source
        else:
            previous_path = transcript_dir / "transcript.json"
            previous = read_json(previous_path, default={}) or {}
            if previous.get("segments"):
                return {
                    "contract_version": "model_scheduler_asr.v1",
                    "status": "degraded",
                    "video_id": video_id,
                    "source": str(previous.get("source") or "existing_transcript"),
                    "segment_count": len(previous.get("segments") or []),
                    "failed_chunk_count": failed_chunks,
                    "fallback_used": False,
                    "preserved_existing_transcript": True,
                    "writes_manual_gold": False,
                    "adjusts_boundaries": False,
                }
            return {
                "contract_version": "model_scheduler_asr.v1",
                "status": "failed",
                "video_id": video_id,
                "source": "missing",
                "segment_count": 0,
                "failed_chunk_count": failed_chunks,
                "fallback_used": False,
                "preserved_existing_transcript": False,
                "writes_manual_gold": False,
                "adjusts_boundaries": False,
            }
    else:
        segments = post_process_segments(sorted(qwen_segments, key=lambda row: (float(row.get("start") or 0.0), float(row.get("end") or 0.0))))
        source = f"qwen3_asr:{qwen3_asr_model().rsplit('/', 1)[-1]}"
        degraded_chunks = sum(1 for result in chunk_results if result.get("status") == "degraded")
        metadata = {
            "backend": "qwen3_asr",
            "profile": "quality",
            "model_size": qwen3_asr_model(),
            "postprocess_version": POSTPROCESS_VERSION,
            "segment_count_raw": len(qwen_segments),
            "segment_count_processed": len(segments),
            "quality_status": "degraded" if degraded_chunks else "ready",
            "chunks": diagnostics,
            "config": config,
            "routing": {
                "contract_version": "model_scheduler_asr.v1",
                "scope": "scheduled_program",
                "decision": "qwen3_asr_scheduler",
                "selected_backend": "qwen3_asr",
                "fallback_used": False,
            },
        }
    metadata = {
        **metadata,
        "cache_key": {
            "backend_preference": "qwen3_asr_preferred",
            "audio_sha256": expected_audio_sha256,
            "qwen3_asr": config,
            "postprocess_version": POSTPROCESS_VERSION,
        },
        "model_scheduler": {"contract_version": "model_scheduler.v1", "role": role},
    }
    transcript = {
        "video_id": video_id,
        "source": source,
        "segments": segments,
        "metadata": metadata,
        "created_at": utc_now(),
    }
    if role == "shadow":
        root = transcript_dir / "shadow" / "qwen3_asr"
        target = root / "transcript.json"
        write_json(target, {**transcript, "role": "shadow", "auto_promote": False})
        write_json(
            root / "status.json",
            {
                "contract_version": "model_scheduler_asr.v1",
                "video_id": video_id,
                "status": "degraded" if failed_chunks or fallback_used else "ready",
                "role": "shadow",
                "source": source,
                "segment_count": len(segments),
                "active_transcript_preserved": True,
                "auto_promote": False,
                "updated_at": utc_now(),
            },
        )
    else:
        target = transcript_dir / "transcript.json"
        write_json(target, transcript)
        _mark_transcribed(target, video_id)
    return {
        "contract_version": "model_scheduler_asr.v1",
        "status": "degraded" if failed_chunks or fallback_used or metadata.get("quality_status") == "degraded" else "ready",
        "video_id": video_id,
        "source": source,
        "segment_count": len(segments),
        "completed_chunk_count": len(item_results) - failed_chunks,
        "failed_chunk_count": failed_chunks,
        "fallback_used": fallback_used,
        "fallback_source": fallback_source,
        "role": role,
        "active_transcript_preserved": role == "shadow",
        "writes_manual_gold": False,
        "adjusts_boundaries": False,
    }


def active_asr_backend(
    backend: str | None = None,
    *,
    model_size: str | None = None,
    asr_profile: str | None = None,
) -> str:
    model_size = resolve_asr_model_size(model_size, profile=asr_profile)
    requested = (backend or os.getenv("DSO_ASR_BACKEND", "auto")).strip().lower() or "auto"
    if requested == "auto":
        if whisper_cpp_ready(model_size):
            return "whisper_cpp"
        if importlib.util.find_spec("faster_whisper") is not None:
            return "faster_whisper"
        return "placeholder"
    if requested in {"whisper_cpp", "whisper.cpp", "cpp"}:
        return "whisper_cpp" if whisper_cpp_ready(model_size) else "missing_whisper_cpp"
    if requested in {"faster_whisper", "faster-whisper", "fw"}:
        return "faster_whisper" if importlib.util.find_spec("faster_whisper") is not None else "missing_faster_whisper"
    if requested in {"qwen3_asr", "qwen3-asr", "qwen_asr", "qwen-asr"}:
        health = qwen3_asr_health()
        model = health.get("model") if isinstance(health.get("model"), dict) else {}
        if model.get("loaded"):
            return "qwen3_asr"
        if health.get("status") == "available":
            return "qwen3_asr_unloaded"
        return "missing_qwen3_asr"
    if requested in {"qwen3_asr_preferred", "qwen3-asr-preferred"}:
        health = qwen3_asr_health()
        model = health.get("model") if isinstance(health.get("model"), dict) else {}
        if model.get("loaded"):
            return "qwen3_asr"
        if whisper_cpp_ready(model_size):
            return "whisper_cpp_fallback"
        if importlib.util.find_spec("faster_whisper") is not None:
            return "faster_whisper_fallback"
        return "missing_preferred_asr"
    return f"unknown:{requested}"


def asr_prompt() -> str:
    configured = os.getenv("DSO_WHISPER_PROMPT")
    if configured is not None:
        return configured.strip()
    extra = os.getenv("DSO_WHISPER_HOTWORDS", "").strip()
    if extra:
        return f"{DEFAULT_ASR_PROMPT} 额外热词：{extra}"
    return DEFAULT_ASR_PROMPT


def _mark_transcribed(transcript_path: Path, video_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE source_videos SET transcript_path = ?, status = ?, updated_at = ? WHERE id = ?",
            [str(transcript_path), "transcribed", utc_now(), video_id],
        )
        conn.commit()


def _try_configured_asr(
    audio_path: Path,
    transcript_dir: Path,
    model_size: str,
    *,
    backend: str | None = None,
) -> tuple[list[dict], str]:
    backend = (backend or os.getenv("DSO_ASR_BACKEND", "auto")).strip().lower() or "auto"
    if backend == "auto":
        if whisper_cpp_ready(model_size):
            segments, source = _try_whisper_cpp(audio_path, transcript_dir, model_size)
            if segments:
                return segments, source
        return _try_faster_whisper(audio_path, model_size)
    if backend in {"whisper_cpp", "whisper.cpp", "cpp"}:
        return _try_whisper_cpp(audio_path, transcript_dir, model_size)
    if backend in {"faster_whisper", "faster-whisper", "fw"}:
        return _try_faster_whisper(audio_path, model_size)
    if backend in {"qwen3_asr", "qwen3-asr", "qwen_asr", "qwen-asr"}:
        return _try_qwen3_asr(audio_path, transcript_dir)
    if backend in {"qwen3_asr_preferred", "qwen3-asr-preferred"}:
        segments, source = _try_qwen3_asr(audio_path, transcript_dir)
        if segments:
            return segments, source
        if whisper_cpp_ready(model_size):
            fallback_segments, fallback_source = _try_whisper_cpp(audio_path, transcript_dir, model_size)
            if fallback_segments:
                return fallback_segments, fallback_source
        return _try_faster_whisper(audio_path, model_size)
    return [], f"unknown_asr_backend:{backend}"


def _try_qwen3_asr(audio_path: Path, transcript_dir: Path) -> tuple[list[dict], str]:
    try:
        segments, metadata = transcribe_wav_with_qwen3_asr(audio_path, transcript_dir)
    except Qwen3ASRError as exc:
        write_json(
            transcript_dir / "qwen3_asr_last_run.json",
            {"status": "failed", "model": qwen3_asr_model(), "error": str(exc), "created_at": utc_now()},
        )
        return [], f"qwen3_asr_failed:{type(exc).__name__}"
    run_status = str(metadata.get("quality_status") or "ready")
    write_json(
        transcript_dir / "qwen3_asr_last_run.json",
        {"status": run_status, **metadata, "segment_count": len(segments), "created_at": utc_now()},
    )
    return segments, f"qwen3_asr:{qwen3_asr_model().rsplit('/', 1)[-1]}"


def _try_faster_whisper(audio_path: Path, model_size: str) -> tuple[list[dict], str]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return [], "missing_faster_whisper"

    device = os.getenv("DSO_WHISPER_DEVICE", "auto")
    compute_type = os.getenv("DSO_WHISPER_COMPUTE_TYPE", "int8")
    model_kwargs: dict[str, int | str] = {"device": device, "compute_type": compute_type}
    cpu_threads = _optional_int_env("DSO_WHISPER_CPU_THREADS")
    num_workers = _optional_int_env("DSO_WHISPER_NUM_WORKERS")
    if cpu_threads:
        model_kwargs["cpu_threads"] = cpu_threads
    if num_workers:
        model_kwargs["num_workers"] = num_workers
    model = WhisperModel(model_size, **model_kwargs)
    prompt = asr_prompt()
    segments_iter, _ = model.transcribe(str(audio_path), vad_filter=True, initial_prompt=prompt or None)
    segments: list[dict] = []
    for index, seg in enumerate(segments_iter, start=1):
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(
            {"index": index, "start": float(seg.start), "end": float(seg.end), "text": text}
        )
    return segments, f"faster_whisper:{model_size}:{device}:{compute_type}"


def _try_whisper_cpp(audio_path: Path, transcript_dir: Path, model_size: str | None = None) -> tuple[list[dict], str]:
    binary = whisper_cpp_binary()
    model = whisper_cpp_model(model_size)
    if not binary:
        return [], "missing_whisper_cpp_binary"
    if not model:
        return [], "missing_whisper_cpp_model"

    output_prefix = transcript_dir / "whisper_cpp"
    language = whisper_cpp_language()
    command = [
        binary,
        "-m",
        model,
        "-f",
        str(audio_path),
        "-oj",
        "-of",
        str(output_prefix),
    ]
    if language:
        command.extend(["-l", language])
    vad_model = whisper_cpp_vad_model()
    if whisper_cpp_vad_enabled() and vad_model:
        command.extend(["--vad", "-vm", vad_model])
    prompt = asr_prompt()
    if prompt:
        command.extend(["--prompt", prompt])
    extra_args = os.getenv("DSO_WHISPER_CPP_EXTRA_ARGS")
    if extra_args:
        command.extend(shlex.split(extra_args))
    try:
        run_cmd(command)
    except Exception as exc:
        return [], f"whisper_cpp_failed:{type(exc).__name__}"

    json_path = output_prefix.with_suffix(".json")
    if not json_path.exists():
        return [], "missing_whisper_cpp_json"
    segments = _parse_whisper_cpp_json(json_path)
    return segments, f"whisper_cpp:{model_size or 'base'}"


def _asr_cache_key(audio_path: Path, model_size: str, profile_name: str, backend: str | None = None) -> dict:
    backend_preference = (backend or os.getenv("DSO_ASR_BACKEND", "auto")).strip().lower() or "auto"
    cache_key = {
        "audio_sha256": _file_sha256(audio_path),
        "profile": profile_name,
        "model_size": model_size,
        "backend_preference": backend_preference,
        "active_backend": active_asr_backend(backend, model_size=model_size, asr_profile=profile_name),
        "whisper_cpp": {
            "binary": whisper_cpp_binary(),
            "model": whisper_cpp_model(model_size),
            "model_name": model_size,
            "language": whisper_cpp_language(),
            "vad_enabled": whisper_cpp_vad_enabled(),
            "vad_model": whisper_cpp_vad_model(),
            "extra_args": os.getenv("DSO_WHISPER_CPP_EXTRA_ARGS"),
        },
        "faster_whisper": {
            "model_size": model_size,
            "device": os.getenv("DSO_WHISPER_DEVICE", "auto"),
            "compute_type": os.getenv("DSO_WHISPER_COMPUTE_TYPE", "int8"),
            "cpu_threads": os.getenv("DSO_WHISPER_CPU_THREADS"),
            "num_workers": os.getenv("DSO_WHISPER_NUM_WORKERS"),
        },
        "prompt": asr_prompt(),
        "postprocess_version": POSTPROCESS_VERSION,
    }
    if backend_preference in {
        "qwen3_asr",
        "qwen3-asr",
        "qwen_asr",
        "qwen-asr",
        "qwen3_asr_preferred",
        "qwen3-asr-preferred",
    }:
        cache_key["qwen3_asr"] = qwen3_asr_cache_config()
    return cache_key


def _cached_transcript(transcript_path: Path, cache_key: dict, *, force: bool) -> dict | None:
    if force:
        return None
    data = read_json(transcript_path, default=None)
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata") or {}
    stored_key = metadata.get("cache_key")
    if stored_key != cache_key:
        preferred = str(cache_key.get("backend_preference") or "") in {
            "qwen3_asr_preferred",
            "qwen3-asr-preferred",
        }
        if not preferred or not str(data.get("source") or "").startswith("qwen3_asr:"):
            return None
        old_key = dict(stored_key or {})
        new_key = dict(cache_key)
        old_key.pop("active_backend", None)
        new_key.pop("active_backend", None)
        if old_key != new_key:
            return None
    data["metadata"] = {**metadata, "cache_hit": True}
    data["cache_hit"] = True
    return data


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_whisper_cpp_json(path: Path) -> list[dict]:
    data = read_json(path, default={}) or {}
    rows = data.get("transcription") or data.get("segments") or []
    segments: list[dict] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start, end = _whisper_cpp_times(row)
        if end <= start:
            end = start + 0.3
        segments.append(
            {
                "index": len(segments) + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
            }
        )
    return segments


def post_process_segments(segments: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for segment in segments:
        text = _normalize_transcript_text(str(segment.get("text") or ""))
        if not text:
            continue
        if _is_repetitive_hallucination(text):
            continue
        start = _safe_float(segment.get("start"), 0.0)
        end = max(start + 0.3, _safe_float(segment.get("end"), start + 0.3))
        row = {
            "index": len(cleaned) + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
        }
        tags = []
        if _ad_noise_score(text) >= 2:
            tags.append("ad_read")
        if _is_mostly_english_lyrics(text):
            tags.append("english_lyrics")
        if tags:
            row["tags"] = tags
        cleaned.append(row)

    merged: list[dict] = []
    for segment in cleaned:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        gap = float(segment["start"]) - float(previous["end"])
        previous_text = str(previous.get("text") or "")
        current_text = str(segment.get("text") or "")
        can_merge = (
            gap <= 0.75
            and (len(previous_text) < 10 or len(current_text) < 10)
            and float(segment["end"]) - float(previous["start"]) <= 8.0
            and previous.get("tags") == segment.get("tags")
        )
        if can_merge:
            previous["end"] = segment["end"]
            previous["text"] = _normalize_transcript_text(f"{previous_text}{current_text}")
            continue
        merged.append(segment)

    for index, segment in enumerate(merged, start=1):
        segment["index"] = index
    return merged


def _normalize_transcript_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()
    normalized = re.sub(r"\s+([，。！？、,.!?])", r"\1", normalized)
    normalized = to_zh_hans(normalized)
    for wrong, right in _zh_hans_hotword_replacements():
        normalized = normalized.replace(wrong, right)
    return to_zh_hans(normalized)


@lru_cache(maxsize=1)
def _zh_hans_hotword_replacements() -> tuple[tuple[str, str], ...]:
    return tuple((to_zh_hans(wrong), to_zh_hans(right)) for wrong, right in HOTWORD_REPLACEMENTS.items())


def _ad_noise_score(text: str) -> int:
    return sum(1 for word in AD_KEYWORDS if word.lower() in text.lower())


def _is_repetitive_hallucination(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 32:
        return False
    most_common = max((compact.count(char) for char in set(compact)), default=0)
    if most_common / len(compact) >= 0.42:
        return True
    for size in (1, 2, 3, 4):
        chunks = [compact[index : index + size] for index in range(0, len(compact) - size + 1, size)]
        if not chunks:
            continue
        repeated = max((chunks.count(chunk) for chunk in set(chunks)), default=0)
        if repeated >= 8 and repeated / len(chunks) >= 0.55:
            return True
    return False


def _is_mostly_english_lyrics(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 24:
        return False
    ascii_letters = sum(1 for char in compact if char.isascii() and char.isalpha())
    cjk = sum(1 for char in compact if "\u4e00" <= char <= "\u9fff")
    return ascii_letters / max(1, len(compact)) >= 0.55 and cjk < 8


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _whisper_cpp_times(row: dict) -> tuple[float, float]:
    if isinstance(row.get("offsets"), dict):
        offsets = row["offsets"]
        start = _milliseconds_to_seconds(offsets.get("from"))
        end = _milliseconds_to_seconds(offsets.get("to"))
        if end > start:
            return start, end
    if isinstance(row.get("timestamps"), dict):
        timestamps = row["timestamps"]
        return _timestamp_to_seconds(timestamps.get("from")), _timestamp_to_seconds(timestamps.get("to"))
    return _offset_to_seconds(row.get("start")), _offset_to_seconds(row.get("end"))


def _milliseconds_to_seconds(value: object) -> float:
    try:
        return float(value or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _offset_to_seconds(value: object) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000.0 if number > 10000 else number


def _timestamp_to_seconds(value: object) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", ".")
    if not text:
        return 0.0
    try:
        if ":" in text:
            parts = [float(part) for part in text.split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
        return float(text)
    except ValueError:
        return 0.0


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _find_sidecar_transcript(video_path: Path) -> Path | None:
    for suffix in [".srt", ".SRT"]:
        candidate = video_path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


def _parse_srt(path: Path) -> list[dict]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", content.strip())
    segments = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line = next((line for line in lines if "-->" in line), "")
        if not time_line:
            continue
        start_s, end_s = [part.strip() for part in time_line.split("-->", 1)]
        text = " ".join(line for line in lines if "-->" not in line and not line.isdigit())
        segments.append(
            {
                "index": len(segments) + 1,
                "start": _srt_time_to_seconds(start_s),
                "end": _srt_time_to_seconds(end_s),
                "text": text,
            }
        )
    return segments


def _srt_time_to_seconds(value: str) -> float:
    value = value.replace(",", ".")
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _placeholder_segments(duration: float) -> list[dict]:
    if duration <= 0:
        return []
    segments = []
    step = 20.0
    index = 1
    start = 0.0
    while start < duration:
        end = min(duration, start + step)
        segments.append(
            {
                "index": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": f"未转写片段 {index}",
            }
        )
        index += 1
        start = end
    return segments


def write_srt(segments: list[dict], path: Path, *, offset: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        start = max(0.0, float(seg["start"]) - offset)
        end = max(start + 0.3, float(seg["end"]) - offset)
        lines.extend(
            [
                str(i),
                f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}",
                str(seg.get("text") or "").strip(),
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
