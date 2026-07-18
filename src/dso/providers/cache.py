"""Deterministic, atomic file cache for validated public-model responses."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping


_CACHE_VERSION = "public_model_cache.v1"
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_FIELDS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "secret_key",
    "access_token",
    "refresh_token",
    "raw_media",
    "media_bytes",
    "prompt",
    "prompt_text",
    "system_prompt",
}


class UnsafeCacheData(ValueError):
    pass


def _normalized_field_name(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")


def _validate_no_sensitive_fields(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_field_name(key)
            if normalized in _SENSITIVE_FIELDS:
                raise UnsafeCacheData(f"sensitive field is forbidden in cache data: {path}.{key}")
            _validate_no_sensitive_fields(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _validate_no_sensitive_fields(nested, path=f"{path}[{index}]")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise UnsafeCacheData(f"binary media is forbidden in cache data: {path}")


def _canonical(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("cache parameters cannot contain non-finite Decimal values")
        return {"$decimal": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cache parameters cannot contain non-finite float values")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("cache parameter object keys must be strings")
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise TypeError(f"unsupported cache parameter type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class CacheKeyParts:
    content_hash: str
    provider: str
    model: str
    api_version: str
    prompt_version: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        for field_name in ("content_hash", "provider", "model", "api_version", "prompt_version"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required for the public model cache key")
        _validate_no_sensitive_fields(self.parameters)

    def canonical_json(self) -> str:
        value = {
            "api_version": self.api_version,
            "content_hash": self.content_hash,
            "model": self.model,
            "parameters": _canonical(self.parameters),
            "prompt_version": self.prompt_version,
            "provider": self.provider,
            "version": _CACHE_VERSION,
        }
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def build_cache_key(
    *,
    content_hash: str,
    provider: str,
    model: str,
    api_version: str,
    prompt_version: str,
    parameters: Mapping[str, object],
) -> str:
    """Build a stable key containing every result-affecting contract field."""

    return CacheKeyParts(
        content_hash=content_hash,
        provider=provider,
        model=model,
        api_version=api_version,
        prompt_version=prompt_version,
        parameters=parameters,
    ).digest()


class FileResponseCache:
    """Small JSON cache using same-filesystem temp files and ``os.replace``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if not _KEY_RE.fullmatch(key):
            raise ValueError("cache key must be a lowercase SHA-256 hex digest")
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            return None
        if envelope.get("version") != _CACHE_VERSION or envelope.get("key") != key:
            return None
        payload = envelope.get("payload")
        return payload if isinstance(payload, dict) else None

    def put(self, key: str, payload: Mapping[str, Any]) -> Path:
        """Atomically store a validated JSON response without sensitive inputs."""

        _validate_no_sensitive_fields(payload)
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        envelope = {"key": key, "payload": dict(payload), "version": _CACHE_VERSION}
        serialized = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{key}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
            temp_name = None
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The file replacement remains atomic on filesystems that do not
                # support fsync on directories.
                pass
            return destination
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def contains(self, key: str) -> bool:
        return self.get(key) is not None
