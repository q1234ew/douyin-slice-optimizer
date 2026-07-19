from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from dso.api.main import _provider_config_submission_security, app
from dso.providers.admin_config import (
    ProviderAdminConfigError,
    provider_config_values,
    save_provider_connection_config,
)
from dso.providers.service import provider_admin_status


BASE_URL = "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
SECRET = "sk-test-provider-admin-secret"
ENV_NAMES = (
    "DSO_PUBLIC_MODEL_API_ENABLED",
    "DSO_PUBLIC_MODEL_PROVIDER",
    "DSO_BAILIAN_MODEL_ID",
    "DSO_BAILIAN_BASE_URL",
    "DSO_BAILIAN_API_KEY",
    "DSO_PUBLIC_MODEL_BUDGET_PER_REQUEST_CNY",
    "DSO_PUBLIC_MODEL_BUDGET_PER_BATCH_CNY",
    "DSO_PUBLIC_MODEL_BUDGET_PER_DAY_CNY",
)


@pytest.fixture(autouse=True)
def isolated_provider_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    env_file = tmp_path / "auth" / "bailian.env"
    monkeypatch.setenv("DSO_PUBLIC_MODEL_ENV_FILE", str(env_file))
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield env_file


def _payload(**overrides) -> dict:
    return {
        "provider": "aliyun_bailian",
        "model_id": "qwen3.5-flash-2026-02-23",
        "base_url": BASE_URL,
        "api_key": SECRET,
        "per_request_cny": "0.05",
        "per_batch_cny": "0.20",
        "per_day_cny": "1.00",
        **overrides,
    }


def _request(*, client: str, host: str, forwarded_proto: str | None = None) -> Request:
    headers = [(b"host", host.encode())]
    if forwarded_proto is not None:
        headers.extend(
            [
                (b"x-forwarded-proto", forwarded_proto.encode()),
                (b"x-forwarded-for", b"203.0.113.9"),
                (b"x-real-ip", b"203.0.113.9"),
            ]
        )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/providers/config",
            "raw_path": b"/providers/config",
            "query_string": b"",
            "headers": headers,
            "client": (client, 12345),
            "server": ("127.0.0.1", 8000),
        }
    )


def test_save_writes_mode_0600_and_forces_api_disabled(
    isolated_provider_environment: Path,
) -> None:
    save_provider_connection_config(_payload())

    content = isolated_provider_environment.read_text(encoding="utf-8")
    assert f"DSO_BAILIAN_API_KEY={SECRET}" in content
    assert "DSO_PUBLIC_MODEL_API_ENABLED=0" in content
    assert stat.S_IMODE(isolated_provider_environment.stat().st_mode) == 0o600
    assert os.environ["DSO_PUBLIC_MODEL_API_ENABLED"] == "0"
    assert provider_config_values()["api_key_configured"] is True


def test_status_never_returns_secret(isolated_provider_environment: Path) -> None:
    save_provider_connection_config(_payload())

    status_payload = provider_admin_status(
        secure_submission_allowed=True,
        secure_submission_reason="test",
    )

    assert status_payload["api_key_configured"] is True
    assert SECRET not in repr(status_payload)
    assert status_payload["network_calls_allowed"] is False
    assert status_payload["public_api_enabled"] is False


def test_blank_key_preserves_existing_secret(isolated_provider_environment: Path) -> None:
    save_provider_connection_config(_payload())
    save_provider_connection_config(_payload(api_key="", per_day_cny="2.00"))

    content = isolated_provider_environment.read_text(encoding="utf-8")
    assert f"DSO_BAILIAN_API_KEY={SECRET}" in content
    assert "DSO_PUBLIC_MODEL_BUDGET_PER_DAY_CNY=2.00" in content


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"api_key": "bad"}, "sk-"),
        ({"api_key": "sk-sp-coding-plan-token"}, "Coding Plan"),
        ({"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}, "cn-beijing"),
        ({"per_request_cny": "2", "per_batch_cny": "1"}, "单请求"),
    ],
)
def test_invalid_config_is_rejected_without_creating_file(
    isolated_provider_environment: Path,
    overrides: dict,
    message: str,
) -> None:
    with pytest.raises(ProviderAdminConfigError, match=message):
        save_provider_connection_config(_payload(**overrides))
    assert not isolated_provider_environment.exists()


def test_submission_security_accepts_only_https_proxy_or_direct_loopback() -> None:
    direct = _provider_config_submission_security(
        _request(client="127.0.0.1", host="127.0.0.1:8765")
    )
    https_proxy = _provider_config_submission_security(
        _request(client="127.0.0.1", host="example.test", forwarded_proto="https")
    )
    public_http = _provider_config_submission_security(
        _request(client="127.0.0.1", host="121.199.170.85", forwarded_proto="http")
    )
    spoofed = _provider_config_submission_security(
        _request(client="203.0.113.9", host="127.0.0.1:8765")
    )

    assert direct[0] is True
    assert https_proxy[0] is True
    assert public_http[0] is False
    assert spoofed[0] is False


def test_config_api_saves_on_direct_loopback_and_never_echoes_secret(
    isolated_provider_environment: Path,
) -> None:
    client = TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 43120),
    )

    before = client.get("/providers/config")
    saved = client.post(
        "/providers/config",
        json=_payload(),
        headers={"Origin": "http://127.0.0.1:8765"},
    )

    assert before.status_code == 200
    assert before.headers["cache-control"] == "no-store"
    assert before.json()["secure_submission_allowed"] is True
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["saved"] is True
    assert saved.json()["api_key_configured"] is True
    assert SECRET not in saved.text
    assert isolated_provider_environment.exists()


def test_config_api_rejects_public_http_proxy_without_writing(
    isolated_provider_environment: Path,
) -> None:
    client = TestClient(
        app,
        base_url="http://121.199.170.85",
        client=("127.0.0.1", 43121),
        headers={
            "X-Forwarded-Proto": "http",
            "X-Forwarded-For": "203.0.113.9",
            "X-Real-IP": "203.0.113.9",
        },
    )

    response = client.post("/providers/config", json=_payload())

    assert response.status_code == 403
    assert not isolated_provider_environment.exists()
