#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

from dso.features.asr import transcribe_audio_file
from dso.features.audio import _rms_frames
from dso.learning.material_evidence import _macos_vision_ocr_binary
from dso.media.ffmpeg import extract_audio, extract_frame, probe_video, require_binary
from dso.utils import utc_now, write_json


SCOUT_VERSION = "material_window_scout.smoke.v1"
CUE_SET_VERSION = "material_window_scout.cues.v1"
PAIR_CUES = {
    "reaction_vocal_teaching": {
        "left": [
            "reaction", "反应", "评价", "点评", "我觉得", "我认为", "没想到", "听起来", "带你看", "看完",
            "惊艳", "震惊", "震撼", "我的天", "刷新我的印象", "印象刷新", "感受一下", "出乎意料",
        ],
        "right": ["教学", "老师", "气息", "发声", "咬字", "混声", "共鸣", "声带", "唱法", "示范", "练习", "高音"],
    },
    "reaction_compilation": {
        "left": ["reaction", "反应", "评价", "点评", "我觉得", "看完"],
        "right": ["盘点", "合集", "第一个", "第二个", "接下来", "排名", "top", "回顾"],
    },
    "compilation_entertainment_news": {
        "left": ["盘点", "合集", "第一个", "第二个", "接下来", "排名", "top", "回顾"],
        "right": ["事件", "回应", "争议", "消息", "爆料", "原唱", "发生", "声明", "热搜"],
    },
    "behind_the_scenes_performance": {
        "left": ["彩排", "排练", "后台", "幕后", "花絮", "候场", "准备", "上台前", "下台后"],
        "right": ["现场演唱", "舞台", "演唱会", "合唱", "高音", "副歌", "表演", "唱歌", "live"],
    },
    "performance_program_context": {
        "left": ["现场", "舞台", "演唱", "演唱会", "直拍", "live", "合唱", "清唱"],
        "right": ["节目", "歌手2026", "天赐的声音", "乘风2026", "音综", "赛段", "第期", "排名"],
    },
    "cross_domain_material": {
        "left": ["舞台", "演唱", "唱歌", "表演", "合唱", "高音", "副歌", "live"],
        "right": ["分析", "解读", "评价", "评论", "观点", "我觉得", "原因", "事件", "争议"],
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local-only material window scout smoke test.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--sample-id", default="smoke_sample")
    parser.add_argument("--confusion-pair", default="reaction_vocal_teaching", choices=sorted(PAIR_CUES))
    parser.add_argument("--window-seconds", type=float, default=15.0)
    parser.add_argument("--stride-seconds", type=float, default=5.0)
    parser.add_argument("--ocr-interval-seconds", type=float, default=15.0)
    parser.add_argument("--max-ocr-frames", type=int, default=40)
    parser.add_argument("--max-windows", type=int, default=2)
    parser.add_argument("--asr-model", default="small")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise SystemExit(f"video not found: {video}")

    started = monotonic()
    timings: dict[str, float] = {}
    probe_started = monotonic()
    probe = probe_video(video)
    timings["probe_seconds"] = _elapsed(probe_started)
    duration = float(probe.get("duration_seconds") or 0.0)

    with tempfile.TemporaryDirectory(prefix="dso-window-scout-") as temp_value:
        temp = Path(temp_value)
        scene_started = monotonic()
        scene_times = _scene_change_times(video)
        timings["scene_scan_seconds"] = _elapsed(scene_started)

        has_audio = int(probe.get("audio_streams") or 0) > 0
        if has_audio:
            audio_started = monotonic()
            audio_path = extract_audio(video, temp / "audio.wav")
            rms_frames = _rms_frames(audio_path, window_seconds=1.0)
            timings["audio_extract_rms_seconds"] = _elapsed(audio_started)

            asr_started = monotonic()
            asr_result = _transcribe_with_cpu_fallback(audio_path, temp / "asr", model_size=args.asr_model)
            timings["asr_seconds"] = _elapsed(asr_started)
        else:
            rms_frames = []
            asr_result = {"source": "audio_missing", "segments": []}
            timings["audio_extract_rms_seconds"] = 0.0
            timings["asr_seconds"] = 0.0
        asr_segments = asr_result.get("segments") if isinstance(asr_result.get("segments"), list) else []

        ocr_started = monotonic()
        ocr_times = _ocr_sample_times(
            duration,
            scene_times,
            interval=max(5.0, float(args.ocr_interval_seconds)),
            limit=max(1, int(args.max_ocr_frames)),
        )
        ocr_rows = _ocr_timeline(video, temp / "frames", ocr_times)
        timings["ocr_seconds"] = _elapsed(ocr_started)

        candidates = _candidate_windows(
            duration=duration,
            window_seconds=max(5.0, float(args.window_seconds)),
            stride_seconds=max(1.0, float(args.stride_seconds)),
            asr_segments=asr_segments,
            ocr_rows=ocr_rows,
            scene_times=scene_times,
            rms_frames=rms_frames,
            confusion_pair=args.confusion_pair,
        )
        selected = _select_windows(
            candidates,
            max_windows=max(1, int(args.max_windows)),
            min_start_distance=max(15.0, float(args.window_seconds)),
        )
        fixed = _fixed_window_comparison(candidates, duration, float(args.window_seconds))
        temporary_bytes = sum(path.stat().st_size for path in temp.rglob("*") if path.is_file())

    timings["total_seconds"] = _elapsed(started)
    ocr_nonempty_count = sum(1 for item in ocr_rows if item.get("lines"))
    evidence_ready = bool(asr_segments or ocr_nonempty_count)
    report = {
        "contract_version": SCOUT_VERSION,
        "status": "ready" if selected and evidence_ready else "insufficient_evidence",
        "mode": "local_only_smoke",
        "sample_id": args.sample_id,
        "confusion_pair": args.confusion_pair,
        "video_path": str(video),
        "source_size_bytes": video.stat().st_size,
        "probe": probe,
        "query": {
            "window_seconds": float(args.window_seconds),
            "stride_seconds": float(args.stride_seconds),
            "ocr_interval_seconds": float(args.ocr_interval_seconds),
            "max_ocr_frames": int(args.max_ocr_frames),
            "max_windows": int(args.max_windows),
            "asr_model": args.asr_model,
            "cue_set_version": CUE_SET_VERSION,
            "cue_set_sha256": cue_set_digest(),
        },
        "component_summary": {
            "audio_available": has_audio,
            "scene_change_count": len(scene_times),
            "rms_bucket_count": len(rms_frames),
            "asr_source": asr_result.get("source") or "",
            "asr_cpu_fallback": bool(asr_result.get("cpu_fallback")),
            "asr_initial_source": asr_result.get("initial_source") or "",
            "asr_segment_count": len(asr_segments),
            "asr_text_chars": sum(len(str(item.get("text") or "")) for item in asr_segments),
            "ocr_frame_count": len(ocr_rows),
            "ocr_nonempty_frame_count": ocr_nonempty_count,
            "candidate_count": len(candidates),
            "temporary_peak_bytes_observed": temporary_bytes,
            "persistent_media_bytes_added": 0,
        },
        "timings": timings,
        "selected_windows": selected,
        "fixed_window_comparison": fixed,
        "top_candidates": candidates[:10],
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "calls_remote_model": False,
        "generated_at": utc_now(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, report)
    report["report_path"] = str(args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _scene_change_times(video: Path, threshold: float = 0.30) -> list[float]:
    ffmpeg = require_binary("ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-vf",
            f"select=gt(scene\\,{threshold:.2f}),showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    values = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", result.stderr or "")]
    return sorted(set(round(value, 3) for value in values))


def _transcribe_with_cpu_fallback(audio_path: Path, output_dir: Path, *, model_size: str) -> dict:
    result = transcribe_audio_file(
        audio_path,
        output_dir,
        model_size=model_size,
        asr_profile="fast",
        backend="whisper_cpp",
    )
    if not str(result.get("source") or "").startswith("whisper_cpp_failed"):
        return result
    previous = os.environ.get("DSO_WHISPER_CPP_EXTRA_ARGS")
    args = (previous or "").split()
    if "-ng" not in args and "--no-gpu" not in args:
        os.environ["DSO_WHISPER_CPP_EXTRA_ARGS"] = " ".join([*(previous or "").split(), "-ng"])
    try:
        retried = transcribe_audio_file(
            audio_path,
            output_dir / "cpu_retry",
            model_size=model_size,
            asr_profile="fast",
            backend="whisper_cpp",
        )
        retried["cpu_fallback"] = True
        retried["initial_source"] = result.get("source") or ""
        return retried
    finally:
        if previous is None:
            os.environ.pop("DSO_WHISPER_CPP_EXTRA_ARGS", None)
        else:
            os.environ["DSO_WHISPER_CPP_EXTRA_ARGS"] = previous


def _ocr_sample_times(duration: float, scene_times: list[float], *, interval: float, limit: int) -> list[float]:
    regular = [min(max(0.0, duration - 0.1), interval * index + interval / 2) for index in range(max(1, math.ceil(duration / interval)))]
    regular = [value for value in regular if value < duration]
    remaining = max(0, limit - len(regular))
    scenes = _even_sample(scene_times, remaining)
    combined = sorted(set(round(value, 3) for value in [*regular, *scenes] if 0 <= value < duration))
    return _even_sample(combined, limit)


def _ocr_timeline(video: Path, root: Path, timestamps: list[float]) -> list[dict]:
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    time_by_path: dict[str, float] = {}
    for timestamp in timestamps:
        path = root / f"frame_{int(round(timestamp * 1000)):09d}.jpg"
        extract_frame(video, path, timestamp)
        paths.append(path)
        time_by_path[str(path)] = timestamp
    if not paths:
        return []
    script = Path(__file__).resolve().parents[1] / "scripts" / "macos_vision_ocr.swift"
    binary = _macos_vision_ocr_binary(script)
    result = subprocess.run([str(binary), *map(str, paths)], text=True, capture_output=True, check=True, timeout=300)
    payload = json.loads(result.stdout or "{}")
    rows = []
    for item in payload.get("items") or []:
        path = str(item.get("path") or "")
        lines = _dedupe_lines(item.get("lines") or [])
        rows.append({"timestamp": round(time_by_path.get(path, 0.0), 3), "lines": lines[:20]})
    return sorted(rows, key=lambda item: float(item["timestamp"]))


def _candidate_windows(
    *,
    duration: float,
    window_seconds: float,
    stride_seconds: float,
    asr_segments: list[dict],
    ocr_rows: list[dict],
    scene_times: list[float],
    rms_frames: list[dict],
    confusion_pair: str,
) -> list[dict]:
    cues = PAIR_CUES[confusion_pair]
    max_start = max(0.0, duration - window_seconds)
    starts = []
    value = 0.0
    while value <= max_start + 0.001:
        starts.append(round(min(max_start, value), 3))
        value += stride_seconds
    if max_start and (not starts or abs(starts[-1] - max_start) > 0.5):
        starts.append(round(max_start, 3))

    candidates = []
    for start in starts:
        end = min(duration, start + window_seconds)
        overlapping = [item for item in asr_segments if float(item.get("end") or 0.0) > start and float(item.get("start") or 0.0) < end]
        speech_seconds = sum(
            max(0.0, min(end, float(item.get("end") or 0.0)) - max(start, float(item.get("start") or 0.0)))
            for item in overlapping
        )
        asr_text = " ".join(str(item.get("text") or "").strip() for item in overlapping if str(item.get("text") or "").strip())
        ocr_in_window = [item for item in ocr_rows if start <= float(item.get("timestamp") or 0.0) < end]
        ocr_lines = _dedupe_lines([line for item in ocr_in_window for line in (item.get("lines") or [])])
        text = f"{asr_text} {' '.join(ocr_lines)}".lower()
        left_hits = [cue for cue in cues["left"] if cue.lower() in text]
        right_hits = [cue for cue in cues["right"] if cue.lower() in text]
        left_score = min(1.0, len(left_hits) / 2.0)
        right_score = min(1.0, len(right_hits) / 2.0)
        speech_ratio = min(1.0, speech_seconds / max(0.1, end - start))
        asr_density = min(1.0, len(asr_text.replace(" ", "")) / 50.0)
        ocr_density = min(1.0, sum(len(line) for line in ocr_lines) / 40.0)
        scene_count = sum(1 for timestamp in scene_times if start <= timestamp < end)
        scene_density = min(1.0, scene_count / 4.0)
        energies = [float(item.get("energy") or 0.0) for item in rms_frames if start <= float(item.get("time") or 0.0) < end]
        energy_mean = statistics.fmean(energies) if energies else 0.0
        energy_variation = min(1.0, statistics.pstdev(energies) * 3.0) if len(energies) > 1 else 0.0
        pair_relevance = max(left_score, right_score)
        information = (
            pair_relevance * 0.38
            + speech_ratio * 0.22
            + asr_density * 0.14
            + ocr_density * 0.12
            + scene_density * 0.08
            + energy_variation * 0.06
        )
        reasons = []
        if left_hits:
            reasons.append(f"left cues: {', '.join(left_hits[:4])}")
        if right_hits:
            reasons.append(f"right cues: {', '.join(right_hits[:4])}")
        if speech_ratio >= 0.35:
            reasons.append(f"speech ratio {speech_ratio:.2f}")
        if ocr_lines:
            reasons.append(f"OCR {len(ocr_lines)} lines")
        if scene_count >= 2:
            reasons.append(f"scene changes {scene_count}")
        candidates.append(
            {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "information_score": round(information, 4),
                "left_evidence_score": round(left_score, 4),
                "right_evidence_score": round(right_score, 4),
                "dominant_side": "left" if left_score > right_score else ("right" if right_score > left_score else "balanced"),
                "speech_ratio": round(speech_ratio, 4),
                "asr_text": asr_text[:500],
                "ocr_lines": ocr_lines[:20],
                "scene_change_count": scene_count,
                "energy_mean": round(energy_mean, 4),
                "energy_variation": round(energy_variation, 4),
                "selection_reasons": reasons,
            }
        )
    return sorted(candidates, key=lambda item: (-float(item["information_score"]), float(item["start_seconds"])))


def _select_windows(candidates: list[dict], *, max_windows: int, min_start_distance: float) -> list[dict]:
    if not candidates:
        return []
    selected = [candidates[0]]
    while len(selected) < max_windows:
        desired_side = "right" if selected[0].get("dominant_side") == "left" else "left"
        eligible = [
            item
            for item in candidates
            if all(abs(float(item["start_seconds"]) - float(chosen["start_seconds"])) >= min_start_distance for chosen in selected)
        ]
        if not eligible:
            break
        diverse = [item for item in eligible if item.get("dominant_side") == desired_side and float(item["information_score"]) >= 0.25]
        selected.append((diverse or eligible)[0])
    return [{**item, "window": f"adaptive_{index}"} for index, item in enumerate(selected, start=1)]


def _fixed_window_comparison(candidates: list[dict], duration: float, window_seconds: float) -> list[dict]:
    starts = [0.0, max(0.0, min(duration - window_seconds, duration * 0.45)), max(0.0, min(duration - window_seconds, duration * 0.78))]
    rows = []
    for name, start in zip(["hook", "middle", "payoff"], starts):
        nearest = min(candidates, key=lambda item: abs(float(item["start_seconds"]) - start)) if candidates else {}
        rows.append({"window": name, **nearest})
    return rows


def _even_sample(values: list[float], limit: int) -> list[float]:
    if limit <= 0 or not values:
        return []
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[len(values) // 2]]
    indexes = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return [values[index] for index in indexes]


def _dedupe_lines(values: list[object]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _elapsed(started: float) -> float:
    return round(monotonic() - started, 3)


def cue_set_digest() -> str:
    payload = json.dumps(PAIR_CUES, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()
