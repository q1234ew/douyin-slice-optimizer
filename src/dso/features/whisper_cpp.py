from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from dso.config import project_root
from dso.features.asr_profile import resolve_asr_model_size

WHISPER_CPP_REPO_URL = "https://github.com/ggml-org/whisper.cpp.git"


def whisper_cpp_binary() -> str | None:
    configured = os.getenv("DSO_WHISPER_CPP_BIN")
    if configured:
        path = Path(configured)
        return str(path.resolve()) if path.exists() else None
    return _first_existing(_default_binary_candidates()) or shutil.which("whisper-cli") or shutil.which("whisper.cpp") or shutil.which("main")


def whisper_cpp_model(model: str | None = None) -> str | None:
    configured = os.getenv("DSO_WHISPER_CPP_MODEL")
    if configured:
        path = Path(configured)
        return str(path.resolve()) if path.exists() else None
    return _first_existing(_default_model_candidates(model or whisper_cpp_model_name()))


def whisper_cpp_model_name() -> str:
    configured = os.getenv("DSO_WHISPER_CPP_MODEL_NAME")
    return resolve_asr_model_size(configured)


def whisper_cpp_language() -> str | None:
    return os.getenv("DSO_WHISPER_LANGUAGE", "zh").strip() or None


def whisper_cpp_vad_enabled() -> bool:
    value = os.getenv("DSO_WHISPER_CPP_VAD", "auto").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return bool(whisper_cpp_vad_model())
    return bool(whisper_cpp_vad_model())


def whisper_cpp_vad_model(model: str = "silero-v6.2.0") -> str | None:
    configured = os.getenv("DSO_WHISPER_CPP_VAD_MODEL")
    if configured:
        path = Path(configured)
        return str(path.resolve()) if path.exists() else None
    return _first_existing(_default_vad_candidates(model))


def whisper_cpp_ready(model: str | None = None) -> bool:
    return bool(whisper_cpp_binary() and whisper_cpp_model(model))


def setup_whisper_cpp(model: str = "base", *, force: bool = False, vad_model: str = "silero-v6.2.0") -> dict:
    """Install or link a project-local whisper.cpp backend.

    The command prefers the stable project paths, but can reuse the temporary
    benchmark build already created under output/asr-bench-main.
    """
    root = project_root()
    source_dir = root / "tools/whisper.cpp"
    model_dir = root / "data/models/whisper.cpp"
    model_dir.mkdir(parents=True, exist_ok=True)
    stable_model = model_dir / f"ggml-{model}.bin"
    stable_vad_model = model_dir / f"ggml-{vad_model}.bin"
    legacy_dir = root / "output/asr-bench-main/whisper.cpp"
    legacy_model = legacy_dir / "models" / f"ggml-{model}.bin"
    legacy_vad_model = legacy_dir / "models" / f"ggml-{vad_model}.bin"
    actions: list[str] = []

    if force and source_dir.exists() and not source_dir.is_symlink():
        shutil.rmtree(source_dir)
        actions.append(f"removed {source_dir}")
    if force and stable_model.exists():
        stable_model.unlink()
        actions.append(f"removed {stable_model}")
    if force and stable_vad_model.exists():
        stable_vad_model.unlink()
        actions.append(f"removed {stable_vad_model}")

    if not source_dir.exists():
        source_dir.parent.mkdir(parents=True, exist_ok=True)
        if legacy_dir.exists():
            _link_or_copy_dir(legacy_dir, source_dir)
            actions.append(f"linked existing whisper.cpp build from {legacy_dir}")
        else:
            subprocess.run(["git", "clone", "--depth", "1", WHISPER_CPP_REPO_URL, str(source_dir)], check=True)
            actions.append(f"cloned {WHISPER_CPP_REPO_URL}")

    binary = source_dir / "build/bin/whisper-cli"
    if force or not binary.exists():
        subprocess.run(["cmake", "-B", "build", "-DGGML_METAL=ON", "-DCMAKE_BUILD_TYPE=Release"], cwd=source_dir, check=True)
        subprocess.run(["cmake", "--build", "build", "--config", "Release", "-j", "8"], cwd=source_dir, check=True)
        actions.append("built whisper-cli with GGML_METAL=ON")

    if not stable_model.exists():
        if legacy_model.exists():
            _link_or_copy_file(legacy_model, stable_model)
            actions.append(f"linked existing model from {legacy_model}")
        else:
            subprocess.run(["./models/download-ggml-model.sh", model, str(model_dir)], cwd=source_dir, check=True)
            actions.append(f"downloaded ggml-{model}.bin")

    if vad_model and not stable_vad_model.exists():
        if legacy_vad_model.exists():
            _link_or_copy_file(legacy_vad_model, stable_vad_model)
            actions.append(f"linked existing VAD model from {legacy_vad_model}")
        else:
            subprocess.run(["./models/download-vad-model.sh", vad_model, str(model_dir)], cwd=source_dir, check=True)
            actions.append(f"downloaded ggml-{vad_model}.bin")

    return {
        "ready": binary.exists() and stable_model.exists(),
        "binary": str(binary) if binary.exists() else None,
        "model": str(stable_model) if stable_model.exists() else None,
        "vad_model": str(stable_vad_model) if stable_vad_model.exists() else None,
        "vad_enabled": whisper_cpp_vad_enabled(),
        "language": whisper_cpp_language(),
        "actions": actions or ["already_ready"],
    }


def _default_binary_candidates() -> list[Path]:
    root = project_root()
    return [
        root / "tools/whisper.cpp/build/bin/whisper-cli",
        root / "output/asr-bench-main/whisper.cpp/build/bin/whisper-cli",
        root / "whisper.cpp/build/bin/whisper-cli",
    ]


def _default_model_candidates(model: str) -> list[Path]:
    root = project_root()
    filename = f"ggml-{model}.bin"
    return [
        root / "data/models/whisper.cpp" / filename,
        root / "output/asr-bench-main/whisper.cpp/models" / filename,
        root / "models" / filename,
        root / "whisper.cpp/models" / filename,
    ]


def _default_vad_candidates(model: str) -> list[Path]:
    root = project_root()
    filename = f"ggml-{model}.bin"
    return [
        root / "data/models/whisper.cpp" / filename,
        root / "tools/whisper.cpp/models" / filename,
        root / "output/asr-bench-main/whisper.cpp/models" / filename,
        root / "whisper.cpp/models" / filename,
    ]


def _first_existing(candidates: list[Path]) -> str | None:
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _link_or_copy_dir(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source.resolve(), target_is_directory=True)
    except OSError:
        shutil.copytree(source, target)


def _link_or_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source.resolve())
    except OSError:
        shutil.copy2(source, target)
