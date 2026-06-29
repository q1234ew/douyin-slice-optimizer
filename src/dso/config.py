from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def project_root() -> Path:
    return Path(os.environ.get("DSO_ROOT", Path.cwd())).resolve()


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    media_dir: Path
    exports_dir: Path
    cache_dir: Path
    auth_dir: Path
    db_dir: Path
    db_path: Path


def get_settings() -> Settings:
    root = project_root()
    data_dir = root / "data"
    db_dir = data_dir / "db"
    return Settings(
        root=root,
        data_dir=data_dir,
        media_dir=data_dir / "media",
        exports_dir=data_dir / "exports",
        cache_dir=data_dir / "cache",
        auth_dir=data_dir / "auth",
        db_dir=db_dir,
        db_path=db_dir / "dso.sqlite3",
    )


def ensure_data_dirs(settings: Settings | None = None) -> Settings:
    settings = settings or get_settings()
    for path in [
        settings.data_dir,
        settings.media_dir,
        settings.exports_dir,
        settings.cache_dir,
        settings.auth_dir,
        settings.db_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    return settings
