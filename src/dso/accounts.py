from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from dso.config import get_settings
from dso.utils import read_json


ACCOUNT_REPORT = "douyin_account_collection_report_latest.json"


@lru_cache(maxsize=1)
def account_registry() -> dict[str, dict[str, Any]]:
    settings = get_settings()
    path = settings.data_dir / "douyin_capture" / ACCOUNT_REPORT
    data = read_json(path, {}) if path.exists() else {}
    rows = data.get("accounts") if isinstance(data, dict) else []
    registry: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("account_key") or row.get("account_id") or "").strip()
            if not key:
                continue
            registry[key] = dict(row)
    return registry


def account_metadata(account_id: str | None) -> dict[str, Any]:
    account = (account_id or "").strip()
    if not account or account.lower() == "all":
        return {
            "account_id": account or "all",
            "account_display_name": "全部账号",
            "account_tier": "",
        }
    row = account_registry().get(account) or {}
    return {
        "account_id": account,
        "account_display_name": str(row.get("nickname") or row.get("display_name") or account),
        "account_tier": str(row.get("tier") or row.get("account_tier") or ""),
        "account_status": str(row.get("status") or ""),
        "account_quality_grade": str(row.get("quality_grade") or ""),
    }


def account_display_name(account_id: str | None) -> str:
    return str(account_metadata(account_id).get("account_display_name") or account_id or "")


def dataset_display_name(program_key: str | None, dataset_id: str | None = None, fallback: str | None = None) -> str:
    account = (program_key or "").strip()
    if not account or account.lower() == "all":
        return fallback or "全部采集"
    label = account_display_name(account)
    date_key = _dataset_date(dataset_id or "")
    return f"{label} {date_key}" if date_key else (fallback or label)


def _dataset_date(value: str) -> str:
    match = re.search(r"(?:^|_)(20\d{6})(?:$|_)", value or "")
    return match.group(1) if match else ""
