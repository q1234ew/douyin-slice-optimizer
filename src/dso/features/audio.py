from __future__ import annotations

import audioop
import math
import wave
from pathlib import Path

from dso.config import ensure_data_dirs
from dso.media.ffmpeg import extract_audio
from dso.media.ingest import get_video


def extract_audio_features(video_id: str) -> dict:
    settings = ensure_data_dirs()
    video = get_video(video_id)
    wav_path = settings.cache_dir / video_id / "audio" / "audio.wav"
    if not wav_path.exists():
        extract_audio(Path(video["file_path"]), wav_path)
    frames = _rms_frames(wav_path)
    peaks = _detect_peaks(frames)
    return {"video_id": video_id, "wav_path": str(wav_path), "frames": frames, "peaks": peaks}


def _rms_frames(wav_path: Path, window_seconds: float = 1.0) -> list[dict]:
    with wave.open(str(wav_path), "rb") as wav:
        rate = wav.getframerate()
        width = wav.getsampwidth()
        channels = wav.getnchannels()
        frames_per_window = max(1, int(rate * window_seconds))
        result = []
        index = 0
        while True:
            raw = wav.readframes(frames_per_window)
            if not raw:
                break
            if channels > 1:
                raw = audioop.tomono(raw, width, 0.5, 0.5)
            rms = audioop.rms(raw, width)
            result.append({"time": index * window_seconds, "rms": rms})
            index += 1
    max_rms = max((frame["rms"] for frame in result), default=1) or 1
    for frame in result:
        frame["energy"] = round(frame["rms"] / max_rms, 4)
    return result


def _detect_peaks(frames: list[dict]) -> list[dict]:
    if not frames:
        return []
    energies = [float(frame["energy"]) for frame in frames]
    mean = sum(energies) / len(energies)
    threshold = max(0.55, mean * 1.35)
    peaks = []
    for frame in frames:
        energy = float(frame["energy"])
        if energy >= threshold:
            peaks.append({"time": float(frame["time"]), "energy": energy})
    peaks.sort(key=lambda item: item["energy"], reverse=True)
    return peaks[:40]


def energy_between(frames: list[dict], start: float, end: float) -> float:
    values = [float(f["energy"]) for f in frames if start <= float(f["time"]) <= end]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
