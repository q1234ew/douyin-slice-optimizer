from __future__ import annotations

import fcntl
import hmac
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


SERVICE_VERSION = "gpu-resource-agent.v1"
STATE_PATH = Path(os.environ.get("DSO_GPU_AGENT_STATE_PATH") or f"/run/user/{os.getuid()}/dso-gpu-resource-agent.json")
LOCK_PATH = STATE_PATH.with_suffix(".lock")
PROFILE_COMMANDS = {
    "qwen3_asr_1_7b_forced_aligner.local_v1": (os.environ.get("DSO_GPU_AGENT_ASR_COMMAND") or "/home/aidev/bin/dso-asr-on",),
    "qwen2_5_omni_7b_gptq_int4.local_v1": (os.environ.get("DSO_GPU_AGENT_OMNI_COMMAND") or "/home/aidev/bin/dso-omni-on",),
    "qwen3_vl_embedding_2b.local_v1": (os.environ.get("DSO_GPU_AGENT_EMBEDDING_COMMAND") or "/home/aidev/bin/dso-embedding-on",),
}


class LeaseRequest(BaseModel):
    resource_id: str = Field(pattern=r"^gpu:[0-9]+$")
    job_id: str = Field(min_length=3, max_length=120)
    attempt_id: str = Field(min_length=3, max_length=120)
    fencing_token: int = Field(ge=1)


app = FastAPI(title="DSO GPU Resource Agent", version=SERVICE_VERSION)


def _authorize(authorization: str = Header(default="")) -> None:
    expected = str(os.environ.get("DSO_GPU_RESOURCE_AGENT_TOKEN") or "")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid resource-agent token")


@app.get("/health")
def health(_: None = Depends(_authorize)) -> dict[str, Any]:
    state = _read_state()
    return {
        "status": "ready",
        "service_version": SERVICE_VERSION,
        "resource_id": state.get("resource_id") or "gpu:0",
        "active_profile_id": state.get("profile_id") or "",
        "fencing_token": int(state.get("fencing_token") or 0),
        "profiles": _profile_inventory(),
    }


@app.get("/inventory")
def inventory(_: None = Depends(_authorize)) -> dict[str, Any]:
    return {"status": "ready", "service_version": SERVICE_VERSION, "profiles": _profile_inventory(), "state": _read_state()}


@app.post("/profiles/{profile_id}/activate")
def activate(profile_id: str, request: LeaseRequest, _: None = Depends(_authorize)) -> dict[str, Any]:
    command = PROFILE_COMMANDS.get(profile_id)
    if command is None:
        raise HTTPException(status_code=404, detail="profile is not whitelisted")
    if not Path(command[0]).is_file():
        raise HTTPException(status_code=503, detail="whitelisted profile command is not installed")
    started = time.monotonic()
    with _locked_state() as state:
        current_token = int(state.get("fencing_token") or 0)
        same_attempt = state.get("attempt_id") == request.attempt_id and state.get("job_id") == request.job_id
        if request.fencing_token < current_token or (request.fencing_token == current_token and not same_attempt):
            raise HTTPException(status_code=409, detail="stale fencing token")
        warm_hit = state.get("profile_id") == profile_id
        if not warm_hit:
            try:
                completed = subprocess.run(
                    list(command),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=_int_env("DSO_GPU_AGENT_ACTIVATE_TIMEOUT_SECONDS", 1800, 30, 3600),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise HTTPException(status_code=503, detail=f"profile activation failed: {type(exc).__name__}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "activation command failed").strip().splitlines()[-1][:300]
                raise HTTPException(status_code=503, detail=detail)
        state.update(
            {
                "resource_id": request.resource_id,
                "profile_id": profile_id,
                "job_id": request.job_id,
                "attempt_id": request.attempt_id,
                "fencing_token": request.fencing_token,
                "activated_at": time.time(),
            }
        )
    return {
        "status": "ready",
        "profile_id": profile_id,
        "resource_id": request.resource_id,
        "fencing_token": request.fencing_token,
        "warm_hit": warm_hit,
        "activation_ms": int((time.monotonic() - started) * 1000),
    }


def _profile_inventory() -> list[dict[str, Any]]:
    return [
        {"profile_id": profile_id, "installed": Path(command[0]).is_file(), "command_name": Path(command[0]).name}
        for profile_id, command in PROFILE_COMMANDS.items()
    ]


class _locked_state:
    def __enter__(self) -> dict[str, Any]:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.handle = LOCK_PATH.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        self.state = _read_state()
        return self.state

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp = STATE_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps(self.state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            temp.replace(STATE_PATH)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))
