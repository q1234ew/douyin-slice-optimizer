from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.media.ffmpeg import require_binary
from dso.utils import read_json, run_cmd, utc_now, write_json


TIMELINE_FEATURE_VERSION = "timeline_signals.v1"
DEFAULT_SCENE_THRESHOLD = 0.32
DEFAULT_SCENE_FPS = 2.0


def build_timeline_signals(
    video_id: str,
    video_path: str | Path,
    audio_frames: list[dict],
    transcript: list[dict],
    *,
    scan_scenes: bool = True,
) -> dict:
    """Build cheap, deterministic anchors used for recall and boundary snapping."""
    return {
        "version": TIMELINE_FEATURE_VERSION,
        "scene_changes": detect_scene_changes(video_id, video_path) if scan_scenes else [],
        "silences": silence_ranges(audio_frames),
        "audio_onsets": audio_onsets(audio_frames),
        "sentence_starts": _transcript_times(transcript, "start"),
        "sentence_ends": _transcript_times(transcript, "end"),
        "generated_at": utc_now(),
    }


def silence_ranges(frames: list[dict], *, min_duration: float = 0.8) -> list[dict]:
    if not frames:
        return []
    energies = [max(0.0, float(item.get("energy") or 0.0)) for item in frames]
    mean = sum(energies) / max(1, len(energies))
    threshold = min(0.16, max(0.045, mean * 0.28))
    step = _frame_step(frames)
    ranges: list[dict] = []
    start: float | None = None
    values: list[float] = []
    for item in frames:
        timestamp = float(item.get("time") or 0.0)
        energy = max(0.0, float(item.get("energy") or 0.0))
        if energy <= threshold:
            if start is None:
                start = timestamp
            values.append(energy)
            continue
        if start is not None:
            _append_silence(ranges, start, timestamp, values, min_duration)
        start = None
        values = []
    if start is not None:
        _append_silence(ranges, start, float(frames[-1].get("time") or start) + step, values, min_duration)
    return ranges


def audio_onsets(frames: list[dict], *, min_gap: float = 2.0) -> list[dict]:
    if len(frames) < 2:
        return []
    deltas = [
        max(0.0, float(frames[index].get("energy") or 0.0) - float(frames[index - 1].get("energy") or 0.0))
        for index in range(1, len(frames))
    ]
    positive = sorted(value for value in deltas if value > 0)
    percentile = positive[int((len(positive) - 1) * 0.8)] if positive else 0.0
    threshold = max(0.16, percentile)
    result: list[dict] = []
    last_time = -999.0
    for index in range(1, len(frames)):
        timestamp = float(frames[index].get("time") or 0.0)
        delta = deltas[index - 1]
        energy = float(frames[index].get("energy") or 0.0)
        if delta < threshold or energy < 0.32 or timestamp - last_time < min_gap:
            continue
        result.append({"time": round(timestamp, 3), "delta": round(delta, 4), "energy": round(energy, 4)})
        last_time = timestamp
    return sorted(result, key=lambda item: (item["delta"], item["energy"]), reverse=True)[:40]


def detect_scene_changes(
    video_id: str,
    video_path: str | Path,
    *,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    fps: float = DEFAULT_SCENE_FPS,
    min_gap: float = 1.2,
) -> list[dict]:
    source = Path(video_path).expanduser()
    if not source.is_file():
        return []
    cache_path = _scene_cache_path(video_id, source, threshold=threshold, fps=fps)
    cached = read_json(cache_path, default={}) or {}
    if cached.get("source_signature") == _source_signature(source):
        return list(cached.get("scene_changes") or [])
    try:
        require_binary("ffmpeg")
        result = run_cmd(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "info",
                "-i",
                str(source),
                "-vf",
                f"fps={max(0.5, float(fps)):.2f},scale=320:-2,select='gt(scene,{max(0.05, float(threshold)):.3f})',showinfo",
                "-an",
                "-f",
                "null",
                "-",
            ]
        )
        changes = _parse_scene_changes(result.stderr, min_gap=min_gap)
    except Exception:
        changes = []
    write_json(
        cache_path,
        {
            "version": TIMELINE_FEATURE_VERSION,
            "source_signature": _source_signature(source),
            "threshold": float(threshold),
            "fps": float(fps),
            "scene_changes": changes,
            "generated_at": utc_now(),
        },
    )
    return changes


def _parse_scene_changes(stderr: str, *, min_gap: float) -> list[dict]:
    result: list[dict] = []
    last_time = -999.0
    for line in str(stderr or "").splitlines():
        match = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)
        if not match:
            continue
        timestamp = float(match.group(1))
        if timestamp - last_time < max(0.0, min_gap):
            continue
        result.append({"time": round(timestamp, 3), "source": "ffmpeg_scene"})
        last_time = timestamp
    return result[:240]


def _append_silence(
    ranges: list[dict],
    start: float,
    end: float,
    values: list[float],
    min_duration: float,
) -> None:
    duration = max(0.0, end - start)
    if duration < min_duration:
        return
    ranges.append(
        {
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(duration, 3),
            "mean_energy": round(sum(values) / max(1, len(values)), 4),
        }
    )


def _frame_step(frames: list[dict]) -> float:
    if len(frames) < 2:
        return 1.0
    gaps = [
        max(0.01, float(frames[index].get("time") or 0.0) - float(frames[index - 1].get("time") or 0.0))
        for index in range(1, min(len(frames), 12))
    ]
    return sum(gaps) / max(1, len(gaps))


def _transcript_times(transcript: list[dict], field: str) -> list[float]:
    result = []
    for item in transcript:
        try:
            result.append(round(float(item.get(field) or 0.0), 3))
        except (TypeError, ValueError):
            continue
    return result


def _source_signature(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return str(path)


def _scene_cache_path(video_id: str, source: Path, *, threshold: float, fps: float) -> Path:
    settings = ensure_data_dirs()
    digest = hashlib.sha1(f"{_source_signature(source)}:{threshold:.3f}:{fps:.2f}".encode("utf-8")).hexdigest()[:12]
    return settings.cache_dir / video_id / "timeline" / f"scenes_{digest}.json"
