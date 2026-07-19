from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from dso.scheduler.profiles import RuntimeProfile, runtime_profile
from dso.scheduler.repository import ClaimedJob


class RuntimeActivationError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retry_delay_seconds: float = 5.0) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_delay_seconds = retry_delay_seconds


class ResourceAgentClient:
    def __init__(
        self,
        service_url: str | None = None,
        *,
        token: str | None = None,
        timeout_seconds: float | None = None,
        health_timeout_seconds: float | None = None,
    ) -> None:
        self.service_url = str(service_url or os.environ.get("DSO_GPU_RESOURCE_AGENT_URL") or "").strip().rstrip("/")
        self.token = str(token or os.environ.get("DSO_GPU_RESOURCE_AGENT_TOKEN") or "").strip()
        self.activation_timeout_seconds = max(
            30.0,
            float(timeout_seconds)
            if timeout_seconds is not None
            else _float_env("DSO_GPU_RESOURCE_AGENT_ACTIVATION_TIMEOUT_SECONDS", 1800.0, 30.0, 3600.0),
        )
        self.health_timeout_seconds = max(
            1.0,
            float(health_timeout_seconds)
            if health_timeout_seconds is not None
            else _float_env("DSO_GPU_RESOURCE_AGENT_HEALTH_TIMEOUT_SECONDS", 5.0, 1.0, 30.0),
        )

    @property
    def configured(self) -> bool:
        return bool(self.service_url and self.token)

    def health(self) -> dict[str, Any]:
        if not self.configured:
            return {"status": "disabled", "configured": False}
        try:
            return {
                **self._request("GET", "/health", timeout_seconds=self.health_timeout_seconds),
                "configured": True,
            }
        except RuntimeActivationError as exc:
            return {"status": "unavailable", "configured": True, "error": str(exc)}

    def activate(self, claim: ClaimedJob, profile: RuntimeProfile) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeActivationError(
                "model_unavailable",
                f"profile {profile.profile_id} is not resident and gpu-resource-agent is not configured",
            )
        return self._request(
            "POST",
            f"/profiles/{profile.profile_id}/activate",
            {
                "resource_id": claim.resource_id,
                "job_id": claim.job["id"],
                "attempt_id": claim.attempt_id,
                "fencing_token": claim.fencing_token,
            },
            timeout_seconds=self.activation_timeout_seconds,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.service_url}{path}", data=body, method=method, headers=headers)
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            code = "lease_lost" if exc.code == 409 else "resource_unavailable"
            raise RuntimeActivationError(code, f"gpu-resource-agent HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeActivationError("resource_unavailable", f"gpu-resource-agent request failed: {exc}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeActivationError("schema_invalid", "gpu-resource-agent response is not an object")
        return decoded


class RuntimeManager:
    def __init__(self, agent: ResourceAgentClient | None = None) -> None:
        self.agent = agent or ResourceAgentClient()

    def ensure_profile(self, claim: ClaimedJob) -> dict[str, Any]:
        profile = runtime_profile(str(claim.job.get("model_profile_id") or ""))
        if profile is None:
            return {"status": "unmanaged", "warm_hit": True, "actual_model_id": str(claim.job.get("model_id") or ""), "load_ms": 0}
        initial = profile.health()
        if profile.is_ready(initial):
            return {"status": "ready", "warm_hit": True, "actual_model_id": profile.actual_model_id(initial) or profile.model_id, "load_ms": 0}

        started = time.monotonic()
        activation = self.agent.activate(claim, profile)
        if int(activation.get("fencing_token") or claim.fencing_token) != int(claim.fencing_token):
            raise RuntimeActivationError("lease_lost", "gpu-resource-agent returned a different fencing token")
        timeout = _float_env("DSO_MODEL_PROFILE_READY_TIMEOUT_SECONDS", 180.0, 5.0, 1800.0)
        deadline = time.monotonic() + timeout
        latest = profile.health()
        while not profile.is_ready(latest) and time.monotonic() < deadline:
            time.sleep(1.0)
            latest = profile.health()
        if not profile.is_ready(latest):
            raise RuntimeActivationError(
                "model_identity_mismatch",
                f"profile activation did not make {profile.profile_id} ready; actual={profile.actual_model_id(latest) or 'unknown'}",
                retry_delay_seconds=20.0,
            )
        return {
            "status": "ready",
            "warm_hit": False,
            "actual_model_id": profile.actual_model_id(latest) or profile.model_id,
            "load_ms": int((time.monotonic() - started) * 1000),
        }


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))
