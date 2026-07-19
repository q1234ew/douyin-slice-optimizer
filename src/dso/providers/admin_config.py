"""Fail-closed administration of Aliyun Bailian connection credentials.

The API key is persisted only in a dedicated environment file. Public status
payloads expose a boolean configured flag and never return the secret value.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import secrets
import threading

from dso.providers.aliyun_bailian import (
    BAILIAN_MODEL_IDS,
    BAILIAN_PROVIDER_ID,
    BAILIAN_SECRET_ENV,
    DEFAULT_BAILIAN_MODEL,
    BailianConfigurationError,
    validate_bailian_base_url,
)


PROVIDER_ADMIN_CONFIG_CONTRACT_VERSION = "provider_admin_config.v1"
DEFAULT_BUDGETS = {
    "per_request_cny": "0.05",
    "per_batch_cny": "0.20",
    "per_day_cny": "1.00",
}
_BUDGET_ENV_NAMES = {
    "per_request_cny": "DSO_PUBLIC_MODEL_BUDGET_PER_REQUEST_CNY",
    "per_batch_cny": "DSO_PUBLIC_MODEL_BUDGET_PER_BATCH_CNY",
    "per_day_cny": "DSO_PUBLIC_MODEL_BUDGET_PER_DAY_CNY",
}
_WRITTEN_ENV_NAMES = (
    "DSO_PUBLIC_MODEL_API_ENABLED",
    "DSO_PUBLIC_MODEL_PROVIDER",
    "DSO_BAILIAN_MODEL_ID",
    "DSO_BAILIAN_BASE_URL",
    BAILIAN_SECRET_ENV,
    *_BUDGET_ENV_NAMES.values(),
)
_PRESERVED_ENV_NAMES = (
    "DSO_BAILIAN_DATA_ALLOWED",
    "DSO_BAILIAN_AUTHORIZATION_BASIS",
    "DSO_BAILIAN_REDACTION_STRATEGY",
    "DSO_BAILIAN_RETENTION_DAYS",
    "DSO_BAILIAN_RETENTION_POLICY_REFERENCE",
    "DSO_BAILIAN_ALLOWED_UPLOAD_LEVELS",
)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_WRITE_LOCK = threading.Lock()


class ProviderAdminConfigError(ValueError):
    """A safe, user-facing provider configuration validation error."""


def provider_environment_file() -> Path:
    """Select the dedicated secret file without accepting a path from the API payload."""

    override = os.environ.get("DSO_PUBLIC_MODEL_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/etc/dso/bailian.env")
    return Path("data/auth/bailian.env")


def _parse_owned_environment_file(path: Path) -> dict[str, str]:
    """Read only the fixed allowlist; reject shell syntax and unknown variables."""

    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProviderAdminConfigError("无法读取 Provider 权限文件") from exc
    supported = set(_WRITTEN_ENV_NAMES) | set(_PRESERVED_ENV_NAMES)
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not _ENV_NAME.fullmatch(name) or name not in supported:
            raise ProviderAdminConfigError("Provider 权限文件包含不受支持的配置项")
        values[name] = value
    return values


def _validate_api_key(value: object) -> str:
    key = str(value or "")
    if not key.startswith("sk-"):
        raise ProviderAdminConfigError("请输入以 sk- 开头的百炼 API Key")
    if key.startswith("sk-sp-"):
        raise ProviderAdminConfigError("请使用百炼业务空间 API Key，不支持 Coding Plan Token")
    if not 16 <= len(key) <= 256:
        raise ProviderAdminConfigError("API Key 长度应为 16 到 256 个字符")
    if any(ord(character) < 33 or ord(character) > 126 for character in key):
        raise ProviderAdminConfigError("API Key 不能包含空格、换行或控制字符")
    return key


def _validate_budget(payload: dict, field: str) -> tuple[str, Decimal]:
    raw = str(payload.get(field, DEFAULT_BUDGETS[field])).strip()
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ProviderAdminConfigError("预算必须是有效的人民币金额") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -4:
        raise ProviderAdminConfigError("预算必须为正数，最多保留 4 位小数")
    return format(amount, "f"), amount


def _validate_payload(payload: dict, existing: dict[str, str]) -> dict[str, str]:
    """Normalize UI input and force every saved configuration back to disabled."""

    if payload.get("provider", BAILIAN_PROVIDER_ID) != BAILIAN_PROVIDER_ID:
        raise ProviderAdminConfigError("当前仅支持 Aliyun Bailian Provider")
    model_id = str(payload.get("model_id") or DEFAULT_BAILIAN_MODEL).strip()
    if model_id not in BAILIAN_MODEL_IDS:
        raise ProviderAdminConfigError("请选择允许的固定模型快照")
    try:
        base_url = validate_bailian_base_url(str(payload.get("base_url") or ""))
    except BailianConfigurationError as exc:
        raise ProviderAdminConfigError(str(exc)) from exc

    key_input = payload.get("api_key")
    if key_input is None or str(key_input) == "":
        key = existing.get(BAILIAN_SECRET_ENV) or os.environ.get(BAILIAN_SECRET_ENV, "")
        if not key:
            raise ProviderAdminConfigError("首次保存必须输入 API Key")
        key = _validate_api_key(key)
    else:
        key = _validate_api_key(key_input)

    budgets: dict[str, str] = {}
    amounts: list[Decimal] = []
    for field, env_name in _BUDGET_ENV_NAMES.items():
        rendered, amount = _validate_budget(payload, field)
        budgets[env_name] = rendered
        amounts.append(amount)
    if not amounts[0] <= amounts[1] <= amounts[2]:
        raise ProviderAdminConfigError("预算必须满足：单请求 ≤ 单批次 ≤ 单日")

    return {
        "DSO_PUBLIC_MODEL_API_ENABLED": "0",
        "DSO_PUBLIC_MODEL_PROVIDER": BAILIAN_PROVIDER_ID,
        "DSO_BAILIAN_MODEL_ID": model_id,
        "DSO_BAILIAN_BASE_URL": base_url,
        BAILIAN_SECRET_ENV: key,
        **budgets,
    }


def _atomic_write_environment(path: Path, values: dict[str, str]) -> None:
    """Replace the secret file atomically with restrictive directory/file modes."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    content = "# Managed by Douyin Slice Optimizer. Do not commit this file.\n" + "".join(
        f"{name}={values[name]}\n" for name in (*_WRITTEN_ENV_NAMES, *_PRESERVED_ENV_NAMES)
        if name in values
    )
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def save_provider_connection_config(payload: dict) -> None:
    """Validate and persist connection settings without enabling or invoking a model."""

    if not isinstance(payload, dict):
        raise ProviderAdminConfigError("请求内容必须是 JSON object")
    path = provider_environment_file()
    with _WRITE_LOCK:
        existing = _parse_owned_environment_file(path)
        validated = _validate_payload(payload, existing)
        preserved = {
            name: existing[name]
            for name in _PRESERVED_ENV_NAMES
            if name in existing
        }
        persisted = {**validated, **preserved}
        try:
            _atomic_write_environment(path, persisted)
        except OSError as exc:
            raise ProviderAdminConfigError("无法写入 Provider 权限文件，请检查服务权限") from exc
        for name, value in persisted.items():
            os.environ[name] = value


def provider_config_values() -> dict:
    """Expose safe UI configuration metadata without returning the API key."""

    model_id = os.environ.get("DSO_BAILIAN_MODEL_ID", DEFAULT_BAILIAN_MODEL).strip()
    base_url = os.environ.get("DSO_BAILIAN_BASE_URL", "").strip()
    budgets = {
        field: os.environ.get(env_name, DEFAULT_BUDGETS[field]).strip()
        or DEFAULT_BUDGETS[field]
        for field, env_name in _BUDGET_ENV_NAMES.items()
    }
    path = provider_environment_file()
    parent = path.parent
    persistence_ready = (
        os.access(path, os.W_OK) if path.exists() else parent.exists() and os.access(parent, os.W_OK)
    )
    if not parent.exists():
        persistence_ready = os.access(parent.parent, os.W_OK)
    return {
        "contract_version": PROVIDER_ADMIN_CONFIG_CONTRACT_VERSION,
        "provider": BAILIAN_PROVIDER_ID,
        "model_id": model_id if model_id in BAILIAN_MODEL_IDS else DEFAULT_BAILIAN_MODEL,
        "base_url": base_url,
        "api_key_configured": bool(os.environ.get(BAILIAN_SECRET_ENV, "")),
        "budgets": budgets,
        "allowed_models": list(BAILIAN_MODEL_IDS),
        "persistence_ready": persistence_ready,
        "save_forces_public_api_disabled": True,
    }
