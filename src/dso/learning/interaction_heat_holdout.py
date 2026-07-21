from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from dso.learning.interaction_heat_v3 import (
    load_interaction_heat_rows,
    verify_interaction_heat_artifact,
)


INTERACTION_HEAT_HOLDOUT_READINESS_VERSION = "interaction_heat_holdout_readiness.v1"


@dataclass(frozen=True)
class HoldoutReadinessThresholds:
    min_forward_samples: int = 1000
    min_forward_accounts: int = 5
    min_forward_span_days: int = 7
    min_new_accounts: int = 3
    min_samples_per_new_account: int = 100


def assess_interaction_heat_holdout_readiness(
    *,
    label_artifact_dir: Path,
    expected_label_manifest_sha256: str,
    db_path: Path,
    thresholds: HoldoutReadinessThresholds | None = None,
) -> dict:
    artifact_dir = Path(label_artifact_dir).resolve()
    verification = verify_interaction_heat_artifact(
        artifact_dir,
        expected_manifest_sha256=expected_label_manifest_sha256,
    )
    if not verification["passed"]:
        raise ValueError("interaction heat label artifact verification failed")

    split_rows = _read_split_rows(artifact_dir / "splits.jsonl")
    frozen_sample_ids = {str(row["sample_id"]) for row in split_rows}
    frozen_account_ids = {str(row["account_id"]) for row in split_rows}
    published_values = [
        str(row.get("published_at") or "").strip()
        for row in split_rows
        if str(row.get("published_at") or "").strip()
    ]
    if not published_values:
        raise ValueError("interaction heat split artifact has no published_at cutoff")

    database = Path(db_path).resolve()
    current_rows = load_interaction_heat_rows(database)
    report = assess_holdout_readiness(
        frozen_sample_ids=frozen_sample_ids,
        frozen_account_ids=frozen_account_ids,
        frozen_cutoff=max(published_values, key=_parse_datetime),
        current_rows=current_rows,
        thresholds=thresholds,
    )
    return {
        "contract_version": INTERACTION_HEAT_HOLDOUT_READINESS_VERSION,
        "database_sha256": _file_sha256(database),
        "effective_model_cost_cny": "0.000000",
        "eligible_source_row_count": len(current_rows),
        "label_artifact_id": verification["artifact_id"],
        "label_manifest_sha256": expected_label_manifest_sha256.strip().lower(),
        "network_request_count": 0,
        "production_impact": {
            "database_writes": False,
            "gold_changed": False,
            "production_ranker_changed": False,
        },
        "unlock_nonlinear_ranker": report["status"] == "ready",
        **report,
    }


def assess_holdout_readiness(
    *,
    frozen_sample_ids: set[str],
    frozen_account_ids: set[str],
    frozen_cutoff: str,
    current_rows: Iterable[Mapping],
    thresholds: HoldoutReadinessThresholds | None = None,
) -> dict:
    settings = thresholds or HoldoutReadinessThresholds()
    _validate_thresholds(settings)
    cutoff = _parse_datetime(frozen_cutoff)
    seen_ids: set[str] = set()
    unseen_rows: list[tuple[str, str, datetime]] = []
    forward_candidates: list[tuple[str, str, datetime]] = []
    forward_excluded_not_after_cutoff = 0
    excluded: Counter[str] = Counter()
    for row in current_rows:
        sample_id = str(row.get("id") or row.get("sample_id") or "").strip()
        account_id = str(row.get("account_id") or "").strip()
        if not sample_id or not account_id:
            excluded["missing_identity"] += 1
            continue
        if sample_id in seen_ids:
            raise ValueError(f"duplicate holdout readiness sample ID: {sample_id}")
        seen_ids.add(sample_id)
        if sample_id in frozen_sample_ids:
            excluded["already_frozen"] += 1
            continue
        published_value = str(row.get("published_at") or "").strip()
        if not published_value:
            excluded["missing_published_at"] += 1
            continue
        published_at = _parse_datetime(published_value)
        unseen_rows.append((sample_id, account_id, published_at))
        if published_at > cutoff:
            forward_candidates.append((sample_id, account_id, published_at))
        else:
            forward_excluded_not_after_cutoff += 1

    forward_accounts = sorted({account_id for _, account_id, _ in forward_candidates})
    if forward_candidates:
        forward_start = min(published_at for _, _, published_at in forward_candidates)
        forward_end = max(published_at for _, _, published_at in forward_candidates)
        forward_span_days = (forward_end - forward_start).days
    else:
        forward_start = None
        forward_end = None
        forward_span_days = 0
    forward_unmet = []
    if len(forward_candidates) < settings.min_forward_samples:
        forward_unmet.append("forward_sample_count")
    if len(forward_accounts) < settings.min_forward_accounts:
        forward_unmet.append("forward_account_count")
    if forward_span_days < settings.min_forward_span_days:
        forward_unmet.append("forward_window_span_days")

    new_account_counts = Counter(
        account_id
        for _, account_id, _ in unseen_rows
        if account_id not in frozen_account_ids
    )
    eligible_accounts = sorted(
        account_id
        for account_id, count in new_account_counts.items()
        if count >= settings.min_samples_per_new_account
    )
    account_unmet = []
    if len(eligible_accounts) < settings.min_new_accounts:
        account_unmet.append("new_account_count")
    account_candidate_count = sum(new_account_counts[account] for account in eligible_accounts)

    forward_ready = not forward_unmet
    account_ready = not account_unmet
    return {
        "account_holdout": {
            "candidate_count": account_candidate_count,
            "eligible_accounts": eligible_accounts,
            "new_account_counts": dict(sorted(new_account_counts.items())),
            "ready": account_ready,
            "unmet": account_unmet,
        },
        "excluded": {
            key: excluded.get(key, 0)
            for key in (
                "already_frozen",
                "missing_identity",
                "missing_published_at",
            )
        },
        "forward_time": {
            "account_count": len(forward_accounts),
            "accounts": forward_accounts,
            "candidate_count": len(forward_candidates),
            "end_published_at": _format_datetime(forward_end),
            "excluded_not_after_cutoff": forward_excluded_not_after_cutoff,
            "ready": forward_ready,
            "span_days": forward_span_days,
            "start_published_at": _format_datetime(forward_start),
            "unmet": forward_unmet,
        },
        "frozen_cutoff": _format_datetime(cutoff),
        "status": "ready" if forward_ready and account_ready else "not_ready",
        "thresholds": asdict(settings),
    }


def _validate_thresholds(thresholds: HoldoutReadinessThresholds) -> None:
    values = asdict(thresholds)
    if any(value < 0 for value in values.values()):
        raise ValueError("holdout readiness thresholds must be non-negative")
    if thresholds.min_forward_samples < 1:
        raise ValueError("min_forward_samples must be positive")
    if thresholds.min_forward_accounts < 1:
        raise ValueError("min_forward_accounts must be positive")
    if thresholds.min_new_accounts < 1:
        raise ValueError("min_new_accounts must be positive")
    if thresholds.min_samples_per_new_account < 1:
        raise ValueError("min_samples_per_new_account must be positive")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _read_split_rows(path: Path) -> list[dict]:
    rows = []
    sample_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"split row {line_number} must be an object")
            sample_id = str(row.get("sample_id") or "").strip()
            account_id = str(row.get("account_id") or "").strip()
            if not sample_id or not account_id:
                raise ValueError(f"split row {line_number} is missing identity")
            if sample_id in sample_ids:
                raise ValueError(f"duplicate split sample ID: {sample_id}")
            sample_ids.add(sample_id)
            rows.append(row)
    if not rows:
        raise ValueError("interaction heat split artifact is empty")
    return rows


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
