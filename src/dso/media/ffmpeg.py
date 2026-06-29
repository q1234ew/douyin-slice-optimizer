from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from dso.utils import run_cmd


def require_binary(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise RuntimeError(f"{name} not found in PATH")
    return binary


def probe_video(path: Path) -> dict:
    require_binary("ffprobe")
    result = run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    data = json.loads(result.stdout)
    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    duration = float(data.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1")
    return {
        "duration_seconds": duration,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": fps,
        "audio_streams": len(audio_streams),
    }


def _parse_fps(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def extract_audio(video_path: Path, wav_path: Path) -> Path:
    require_binary("ffmpeg")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
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
    return wav_path


def extract_frame(video_path: Path, output_path: Path, timestamp: float) -> Path:
    require_binary("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ]
    )
    return output_path


def export_vertical_clip(
    video_path: Path,
    output_path: Path,
    start_time: float,
    end_time: float,
    subtitle_path: Path | None = None,
) -> Path:
    require_binary("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, end_time - start_time)
    video_filter = (
        "[0:v]split=2[bgsrc][fgsrc];"
        "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=24:1[bg];"
        "[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
    )
    map_video = "[vout]"
    if subtitle_path and subtitle_path.exists():
        escaped = str(subtitle_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        video_filter += (
            f";[vout]subtitles='{escaped}':force_style="
            "'Fontsize=20,Outline=2,Shadow=1,Alignment=2,MarginV=96'[vsub]"
        )
        map_video = "[vsub]"

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_time:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(video_path),
        "-filter_complex",
        video_filter,
        "-map",
        map_video,
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(output_path),
    ]
    try:
        run_cmd(cmd)
    except Exception:
        if subtitle_path:
            return export_vertical_clip(video_path, output_path, start_time, end_time, None)
        raise
    return output_path


def bounded_window(center: float, duration: float, video_duration: float) -> tuple[float, float]:
    start = max(0.0, center - duration / 2)
    end = min(video_duration, start + duration)
    start = max(0.0, end - duration)
    return (round(start, 3), round(end, 3))
