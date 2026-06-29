from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_one, insert_row
from dso.feedback.platform import upsert_platform_account
from dso.utils import new_id, read_json, utc_now, write_json
from dso.versions import PLATFORM_SYNC_VERSION


AUTHORIZE_URL = "https://open.douyin.com/platform/oauth/connect/"
ACCESS_TOKEN_URL = "https://open.douyin.com/oauth/access_token/"
DEFAULT_SCOPES = ["user_info"]


def douyin_oauth_config(scopes: str | list[str] | None = None, redirect_uri: str | None = None) -> dict:
    client_key = os.environ.get("DSO_DOUYIN_CLIENT_KEY", "").strip()
    client_secret = os.environ.get("DSO_DOUYIN_CLIENT_SECRET", "").strip()
    resolved_redirect = (redirect_uri or os.environ.get("DSO_DOUYIN_REDIRECT_URI", "")).strip()
    resolved_scopes = _normalize_scopes(scopes or os.environ.get("DSO_DOUYIN_SCOPES", ""))
    missing = []
    if not client_key:
        missing.append("DSO_DOUYIN_CLIENT_KEY")
    if not resolved_redirect:
        missing.append("DSO_DOUYIN_REDIRECT_URI")
    warnings = []
    if resolved_redirect and not resolved_redirect.startswith("https://"):
        warnings.append("抖音开放平台 redirect_uri 通常要求 HTTPS，请使用已备案/已配置的 HTTPS 回调地址。")
    if not client_secret:
        warnings.append("缺少 DSO_DOUYIN_CLIENT_SECRET 时只能生成扫码授权 URL，无法自动换取 access_token。")
    return {
        "contract_version": PLATFORM_SYNC_VERSION,
        "platform": "douyin",
        "client_key_configured": bool(client_key),
        "client_secret_configured": bool(client_secret),
        "redirect_uri": resolved_redirect,
        "scopes": resolved_scopes,
        "missing": missing,
        "warnings": warnings,
        "ready_for_qr_login": bool(client_key and resolved_redirect),
        "ready_for_token_exchange": bool(client_key and client_secret),
        "policy": {
            "read_only": True,
            "auth_method": "official OAuth QR authorization page",
            "token_storage": "local file with 0600 permissions; tokens are not stored in SQLite",
        },
    }


def start_douyin_qr_login(
    account_id: str = "main",
    *,
    scopes: str | list[str] | None = None,
    redirect_uri: str | None = None,
    state: str | None = None,
) -> dict:
    config = douyin_oauth_config(scopes=scopes, redirect_uri=redirect_uri)
    resolved_state = state or new_id("dy_state")
    scope_text = ",".join(config["scopes"])
    auth_url = ""
    status = "config_missing"
    if config["ready_for_qr_login"]:
        query = {
            "client_key": os.environ.get("DSO_DOUYIN_CLIENT_KEY", "").strip(),
            "response_type": "code",
            "scope": scope_text,
            "redirect_uri": config["redirect_uri"],
            "state": resolved_state,
        }
        auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(query)
        status = "waiting_scan"
    now = utc_now()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    with connect() as conn:
        existing = fetch_one(conn, "SELECT id FROM platform_oauth_sessions WHERE platform = 'douyin' AND state = ?", [resolved_state])
        row = {
            "account_id": account_id,
            "platform": "douyin",
            "state": resolved_state,
            "auth_url": auth_url,
            "scope": scope_text,
            "redirect_uri": config["redirect_uri"],
            "status": status,
            "error": "; ".join(config["missing"]),
            "updated_at": now,
            "expires_at": expires_at,
        }
        if existing:
            assignments = ", ".join(f"{key} = ?" for key in row)
            conn.execute(f"UPDATE platform_oauth_sessions SET {assignments} WHERE id = ?", [*row.values(), existing["id"]])
            session_id = existing["id"]
        else:
            row = {"id": new_id("oauth"), **row, "code": "", "created_at": now}
            insert_row(conn, "platform_oauth_sessions", row)
            session_id = row["id"]
        conn.commit()
        session = fetch_one(conn, "SELECT * FROM platform_oauth_sessions WHERE id = ?", [session_id])
    return {
        "contract_version": PLATFORM_SYNC_VERSION,
        "status": status,
        "auth_url": auth_url,
        "state": resolved_state,
        "expires_at": expires_at,
        "session": session,
        "config": config,
        "next_action": "open_auth_url_and_scan" if auth_url else "configure_douyin_oauth_env",
    }


def complete_douyin_qr_login(
    code: str,
    state: str,
    *,
    exchange: bool = True,
) -> dict:
    clean_code = str(code or "").strip()
    clean_state = str(state or "").strip()
    if not clean_code:
        raise ValueError("code is required")
    if not clean_state:
        raise ValueError("state is required")
    with connect() as conn:
        session = fetch_one(conn, "SELECT * FROM platform_oauth_sessions WHERE platform = 'douyin' AND state = ?", [clean_state])
    if not session:
        raise ValueError("oauth state not found")
    account_id = session["account_id"] or "main"
    config = douyin_oauth_config(scopes=session.get("scope"), redirect_uri=session.get("redirect_uri"))
    if not exchange or not config["ready_for_token_exchange"]:
        _update_session(clean_state, status="code_received", code=clean_code, error="token exchange not configured")
        account = upsert_platform_account(
            {
                "account_id": account_id,
                "platform": "douyin",
                "auth_status": "code_received",
                "token_status": "not_stored",
                "scopes": session.get("scope") or "",
                "notes": "扫码授权 code 已收到；配置 client_secret 后可换取 token。",
            }
        )
        return {
            "contract_version": PLATFORM_SYNC_VERSION,
            "status": "code_received",
            "account": account,
            "state": clean_state,
            "config": config,
        }

    token_response = _exchange_access_token(clean_code)
    token_data = _extract_token_data(token_response)
    token_record = _save_token(account_id, token_data)
    account = upsert_platform_account(
        {
            "account_id": account_id,
            "platform": "douyin",
            "platform_account_id": token_data.get("open_id") or token_data.get("union_id") or "",
            "auth_status": "connected",
            "token_status": "stored_local_file",
            "scopes": token_data.get("scope") or session.get("scope") or "",
            "token_expires_at": token_record.get("access_token_expires_at") or "",
            "notes": "扫码授权成功，真实 token 仅保存在本地 auth 文件。",
        }
    )
    _update_session(clean_state, status="connected", code="received", error="")
    return {
        "contract_version": PLATFORM_SYNC_VERSION,
        "status": "connected",
        "account": account,
        "state": clean_state,
        "open_id": token_data.get("open_id") or "",
        "scope": token_data.get("scope") or session.get("scope") or "",
        "token_status": "stored_local_file",
        "token_path": str(_token_store_path()),
    }


def douyin_oauth_status(account_id: str = "main", state: str | None = None) -> dict:
    with connect() as conn:
        session = None
        if state:
            session = fetch_one(conn, "SELECT * FROM platform_oauth_sessions WHERE platform = 'douyin' AND state = ?", [state])
        account = fetch_one(conn, "SELECT * FROM platform_accounts WHERE platform = 'douyin' AND account_id = ?", [account_id])
    token = _token_status(account_id)
    return {
        "contract_version": PLATFORM_SYNC_VERSION,
        "platform": "douyin",
        "account_id": account_id,
        "session": session,
        "account": account,
        "token": token,
        "config": douyin_oauth_config(),
    }


def _exchange_access_token(code: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "client_key": os.environ.get("DSO_DOUYIN_CLIENT_KEY", "").strip(),
            "client_secret": os.environ.get("DSO_DOUYIN_CLIENT_SECRET", "").strip(),
            "code": code,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ACCESS_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=12) as response:  # noqa: S310 - user-configured official OAuth endpoint.
        return json.loads(response.read().decode("utf-8"))


def _extract_token_data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    error_code = data.get("error_code") or response.get("error_code")
    if error_code not in (None, 0, "0"):
        raise ValueError(data.get("description") or data.get("message") or response.get("message") or "douyin token exchange failed")
    if not data.get("access_token"):
        raise ValueError("douyin token exchange did not return access_token")
    return data


def _save_token(account_id: str, token_data: dict[str, Any]) -> dict:
    path = _token_store_path()
    store = read_json(path, default={}) or {}
    key = f"douyin:{account_id}"
    record = {
        "platform": "douyin",
        "account_id": account_id,
        "open_id": token_data.get("open_id") or "",
        "union_id": token_data.get("union_id") or "",
        "scope": token_data.get("scope") or "",
        "access_token": token_data.get("access_token") or "",
        "refresh_token": token_data.get("refresh_token") or "",
        "access_token_expires_at": _expires_at(token_data.get("expires_in")),
        "refresh_token_expires_at": _expires_at(token_data.get("refresh_expires_in")),
        "updated_at": utc_now(),
    }
    store[key] = record
    write_json(path, store)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return record


def _token_status(account_id: str) -> dict:
    path = _token_store_path()
    store = read_json(path, default={}) or {}
    record = store.get(f"douyin:{account_id}") or {}
    return {
        "stored": bool(record.get("access_token")),
        "open_id": record.get("open_id") or "",
        "scope": record.get("scope") or "",
        "access_token_expires_at": record.get("access_token_expires_at") or "",
        "refresh_token_expires_at": record.get("refresh_token_expires_at") or "",
        "token_path": str(path) if record else "",
    }


def _update_session(state: str, *, status: str, code: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE platform_oauth_sessions
            SET status = ?, code = ?, error = ?, updated_at = ?
            WHERE platform = 'douyin' AND state = ?
            """,
            [status, code, error, utc_now(), state],
        )
        conn.commit()


def _token_store_path() -> Path:
    return ensure_data_dirs().auth_dir / "douyin_tokens.json"


def _normalize_scopes(scopes: str | list[str] | None) -> list[str]:
    if isinstance(scopes, list):
        values = scopes
    elif scopes:
        values = str(scopes).replace(" ", ",").split(",")
    else:
        values = DEFAULT_SCOPES
    cleaned = [str(item).strip() for item in values if str(item).strip()]
    return cleaned or list(DEFAULT_SCOPES)


def _expires_at(seconds: Any) -> str:
    try:
        delta = int(float(seconds or 0))
    except (TypeError, ValueError):
        delta = 0
    if delta <= 0:
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()
