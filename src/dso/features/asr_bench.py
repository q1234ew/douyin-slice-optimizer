from __future__ import annotations

import time
from pathlib import Path

from dso.config import ensure_data_dirs
from dso.features.asr import active_asr_backend, transcribe_audio_file
from dso.features.asr_profile import normalize_asr_profile, resolve_asr_model_list
from dso.media.ffmpeg import require_binary
from dso.utils import run_cmd, utc_now, write_json

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg"}


def benchmark_asr(
    input_path: str | Path,
    *,
    backend: str = "auto",
    models: str | None = None,
    profile: str | None = None,
    output_dir: str | Path | None = None,
    duration_seconds: float | None = None,
) -> dict:
    settings = ensure_data_dirs()
    source_path = Path(input_path).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    bench_dir = Path(output_dir).expanduser().resolve() if output_dir else settings.root / "output/asr-bench"
    bench_dir.mkdir(parents=True, exist_ok=True)
    audio_path = _prepare_audio(source_path, bench_dir, duration_seconds)
    audio_seconds = _duration_seconds(audio_path)
    profile_name = normalize_asr_profile(profile, allow_compare=True)
    model_names = resolve_asr_model_list(models, profile=profile_name)

    results = []
    for model_name in model_names:
        run_profile = _profile_for_model(profile_name, model_name)
        run_dir = bench_dir / f"{source_path.stem}_{backend}_{model_name}_{int(time.time())}"
        run_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        result = transcribe_audio_file(audio_path, run_dir, model_size=model_name, asr_profile=run_profile, backend=backend)
        wall = time.perf_counter() - started
        transcript_path = run_dir / "transcript.json"
        write_json(
            transcript_path,
            {
                "source": result["source"],
                "segments": result["segments"],
                "metadata": result["metadata"],
                "created_at": utc_now(),
            },
        )
        results.append(
            {
                "backend_requested": backend,
                "active_backend": active_asr_backend(backend, model_size=model_name, asr_profile=run_profile),
                "profile": run_profile,
                "benchmark_profile": profile_name,
                "source": result["source"],
                "model": model_name,
                "audio_seconds": round(audio_seconds, 3),
                "wall_seconds": round(wall, 3),
                "rtf": round(wall / audio_seconds, 4) if audio_seconds else None,
                "segments": len(result["segments"]),
                "transcript_path": str(transcript_path),
                "run_dir": str(run_dir),
            }
        )

    summary = {
        "input_path": str(source_path),
        "audio_path": str(audio_path),
        "output_dir": str(bench_dir),
        "profile": profile_name,
        "duration_limit_seconds": duration_seconds,
        "results": results,
    }
    write_json(bench_dir / "last_benchmark.json", summary)
    return summary


def _profile_for_model(profile_name: str, model_name: str) -> str:
    if profile_name != "compare":
        return profile_name
    if model_name == "small":
        return "quality"
    return "fast"


def _prepare_audio(source_path: Path, bench_dir: Path, duration_seconds: float | None) -> Path:
    if source_path.suffix.lower() in AUDIO_EXTENSIONS and not duration_seconds:
        return source_path
    require_binary("ffmpeg")
    audio_path = bench_dir / f"{source_path.stem}.bench.wav"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
    ]
    if duration_seconds:
        command.extend(["-t", f"{duration_seconds:.3f}"])
    command.append(str(audio_path))
    run_cmd(command)
    return audio_path


def _duration_seconds(path: Path) -> float:
    require_binary("ffprobe")
    result = run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ]
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
