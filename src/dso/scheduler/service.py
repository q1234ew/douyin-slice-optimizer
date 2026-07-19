from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import time
from typing import Any

from dso.learning.omni_slice_ranker import omni_rerank_input_snapshot
from dso.learning.qwen_omni import QWEN_OMNI_MODEL
from dso.learning.qwen_embeddings import QWEN_EMBEDDING_MODEL, qwen_embedding_scheduler_snapshot
from dso.scheduler.contracts import (
    MODEL_SCHEDULER_VERSION,
    OMNI_PROFILE_ID,
    OMNI_RERANK_JOB_KIND,
    QWEN_EMBEDDING_PROFILE_ID,
    TEXT_EMBEDDING_JOB_KIND,
    VISUAL_EMBEDDING_JOB_KIND,
    JobItemSpec,
    ModelJobSpec,
    PRIORITY_DEFAULTS,
    stable_json_hash,
)
from dso.scheduler.repository import ModelJobRepository
from dso.scoring.scorer import suggestions
from dso.versions import OMNI_SLICE_RANKER_VERSION, QWEN_OMNI_VERSION
from dso.versions import QWEN_EMBEDDING_VERSION
from dso.utils import utc_now


_TRUE_VALUES = {"1", "true", "yes", "on"}


def model_scheduler_enabled() -> bool:
    return str(os.environ.get("DSO_MODEL_SCHEDULER_ENABLED") or "").strip().lower() in _TRUE_VALUES


def submit_omni_rerank_job(
    video_id: str,
    *,
    candidate_limit: int = 3,
    max_clip_seconds: float = 6.0,
    omni_weight: float = 0.15,
    load_model: bool = False,
    force: bool = False,
    repository: ModelJobRepository | None = None,
) -> dict[str, Any]:
    selected_limit = max(1, min(20, int(candidate_limit or 3)))
    selected_clip = max(4.0, min(15.0, float(max_clip_seconds or 6.0)))
    selected_weight = max(0.0, min(0.30, float(omni_weight or 0.15)))
    parameters: dict[str, Any] = {
        "candidate_limit": selected_limit,
        "max_clip_seconds": selected_clip,
        "omni_weight": selected_weight,
        "load_model": bool(load_model),
        "force": bool(force),
    }
    snapshot = omni_rerank_input_snapshot(
        video_id,
        candidate_limit=selected_limit,
        max_clip_seconds=selected_clip,
        omni_weight=selected_weight,
    )
    if int(snapshot.get("candidate_count") or 0) == 0:
        return {
            "contract_version": MODEL_SCHEDULER_VERSION,
            "status": "empty",
            "baseline": _baseline(video_id, selected_limit),
            "model_job": None,
            "reason": "no_scored_candidates",
        }
    parameters_for_hash = dict(parameters)
    if force:
        parameters_for_hash["force_nonce"] = utc_now()
    parameters_hash = stable_json_hash(parameters_for_hash)
    dedupe_key = stable_json_hash(
        {
            "job_kind": OMNI_RERANK_JOB_KIND,
            "input_hash": snapshot["input_hash"],
            "model_profile_id": OMNI_PROFILE_ID,
            "model_version": QWEN_OMNI_VERSION,
            "prompt_version": "hybrid_slice_rerank.v1",
            "parameters_hash": parameters_hash,
            "media_profile": "omni_slice_640p_2fps_mono16k.v1",
        }
    )
    deadline_seconds = _int_env("DSO_MODEL_INTERACTIVE_DEADLINE_SECONDS", 300, 30, 3600)
    deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)).isoformat()
    spec = ModelJobSpec(
        job_kind=OMNI_RERANK_JOB_KIND,
        subject_type="source_video",
        subject_id=video_id,
        account_id=str(snapshot.get("account_id") or "main"),
        resource_class=str(os.environ.get("DSO_MODEL_RESOURCE_ID") or "gpu:0"),
        model_profile_id=OMNI_PROFILE_ID,
        model_id=QWEN_OMNI_MODEL,
        model_version=QWEN_OMNI_VERSION,
        prompt_version="hybrid_slice_rerank.v1",
        priority_class="interactive_product",
        base_priority=PRIORITY_DEFAULTS["interactive_product"],
        input_hash=str(snapshot["input_hash"]),
        parameters_hash=parameters_hash,
        dedupe_key=dedupe_key,
        request_summary={
            "video_id": video_id,
            "parameters": parameters,
            "candidate_count": snapshot["candidate_count"],
            "candidate_ids": snapshot["candidate_ids"],
        },
        fallback_ref={"status": "ready", "source": "current_rules"},
        items=tuple(
            JobItemSpec(
                item_kind="omni_candidate_window",
                item_role=str(item.get("window_role") or "middle"),
                input_hash=str(item.get("input_hash") or snapshot["input_hash"]),
                request={
                    "video_id": video_id,
                    "segment_id": str(item.get("segment_id") or ""),
                    "window": dict(item.get("window") or {}),
                },
                estimated_units=float((item.get("window") or {}).get("duration_seconds") or 1),
                max_attempts=3,
            )
            for item in snapshot.get("window_items") or []
        ),
        max_attempts=3,
        deadline_at=deadline_at,
    )
    selected_repository = repository or ModelJobRepository()
    enqueued = selected_repository.enqueue(spec)
    response_status = "cached" if enqueued.cache_hit else "accepted"
    return {
        "contract_version": MODEL_SCHEDULER_VERSION,
        "status": response_status,
        "baseline": _baseline(video_id, selected_limit),
        "model_job": {
            **enqueued.job,
            "deduplicated": enqueued.deduplicated,
            "cache_hit": enqueued.cache_hit,
        },
    }


def wait_for_model_job(job_id: str, *, timeout_seconds: float, repository: ModelJobRepository | None = None) -> dict[str, Any]:
    selected_repository = repository or ModelJobRepository()
    deadline = time.monotonic() + max(0.0, min(120.0, float(timeout_seconds)))
    while True:
        job = selected_repository.get(job_id)
        if job["status"] in {"succeeded", "degraded", "failed", "cancelled", "cancelled_partial", "expired"}:
            return job
        if time.monotonic() >= deadline:
            return job
        time.sleep(0.1)


def submit_embedding_build_job(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    entity_type: str = "historical_sample",
    entity_ids: list[str] | tuple[str, ...] | None = None,
    modality: str = "text",
    limit: int = 300,
    force: bool = False,
    repository: ModelJobRepository | None = None,
) -> dict[str, Any]:
    snapshot = qwen_embedding_scheduler_snapshot(
        account_id=account_id,
        dataset_id=dataset_id,
        entity_type=entity_type,
        entity_ids=entity_ids,
        modality=modality,
        limit=max(1, min(5000, int(limit or 300))),
        force=force,
    )
    if not snapshot.get("items"):
        reused = snapshot.get("reused") or []
        return {
            "contract_version": MODEL_SCHEDULER_VERSION,
            "status": "cached" if reused else "empty",
            "model_job": None,
            "baseline": {"status": "ready" if reused else "missing", "source": "embedding_cache" if reused else "embedding_abstention"},
            "skipped": snapshot.get("skipped") or [],
            "reused": reused,
        }
    parameters = {
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "entity_type": entity_type,
        "entity_ids_sha256": stable_json_hash(list(entity_ids or [])) if entity_ids else "",
        "entity_id_count": len(set(str(value) for value in (entity_ids or []) if value)),
        "modality": modality,
        "limit": max(1, min(5000, int(limit or 300))),
        "force": bool(force),
    }
    parameters_hash = stable_json_hash({**parameters, **({"force_nonce": utc_now()} if force else {})})
    job_kind = VISUAL_EMBEDDING_JOB_KIND if "visual" in snapshot.get("modalities", []) else TEXT_EMBEDDING_JOB_KIND
    dedupe_key = stable_json_hash(
        {
            "job_kind": job_kind,
            "input_hash": snapshot["input_hash"],
            "model_profile_id": QWEN_EMBEDDING_PROFILE_ID,
            "model_version": QWEN_EMBEDDING_VERSION,
            "parameters_hash": parameters_hash,
        }
    )
    scope_id = stable_json_hash({"account": account_id or "all", "dataset": dataset_id or "all", "entity_type": entity_type})[:20]
    spec = ModelJobSpec(
        job_kind=job_kind,
        subject_type="embedding_scope",
        subject_id=scope_id,
        account_id=str(account_id or ""),
        resource_class=str(os.environ.get("DSO_MODEL_RESOURCE_ID") or "gpu:0"),
        model_profile_id=QWEN_EMBEDDING_PROFILE_ID,
        model_id=QWEN_EMBEDDING_MODEL,
        model_version=QWEN_EMBEDDING_VERSION,
        prompt_version="qwen_embedding_input.v1",
        priority_class="maintenance",
        base_priority=PRIORITY_DEFAULTS["maintenance"],
        input_hash=str(snapshot["input_hash"]),
        parameters_hash=parameters_hash,
        dedupe_key=dedupe_key,
        request_summary={**parameters, "item_count": snapshot["item_count"], "skipped_before_enqueue": len(snapshot.get("skipped") or [])},
        fallback_ref={"status": "ready", "source": "embedding_abstention"},
        items=tuple(
            JobItemSpec(
                item_kind=f"{item['modality']}_embedding",
                item_role=str(item["modality"]),
                input_hash=str(item["input_hash"]),
                request=item,
                estimated_units=1.0,
                max_attempts=3,
            )
            for item in snapshot["items"]
        ),
        max_attempts=3,
    )
    enqueued = (repository or ModelJobRepository()).enqueue(spec)
    return {
        "contract_version": MODEL_SCHEDULER_VERSION,
        "status": "cached" if enqueued.cache_hit else "accepted",
        "baseline": {"status": "ready", "source": "embedding_abstention", "production_weight_changed": False},
        "model_job": {**enqueued.job, "deduplicated": enqueued.deduplicated, "cache_hit": enqueued.cache_hit},
        "skipped": snapshot.get("skipped") or [],
        "reused": snapshot.get("reused") or [],
    }


def scheduler_status() -> dict[str, Any]:
    return ModelJobRepository().status(enabled=model_scheduler_enabled())


def scheduler_resources() -> dict[str, Any]:
    from dso.scheduler.resource_agent import ResourceAgentClient

    return {
        "contract_version": "model_scheduler_resources.v1",
        "enabled": model_scheduler_enabled(),
        "resources": ModelJobRepository().resources(),
        "resource_agent": ResourceAgentClient().health(),
    }


def _baseline(video_id: str, limit: int) -> dict[str, Any]:
    return {
        "status": "ready",
        "ranking_source": "current_rules",
        "suggestions": suggestions(video_id, top_k=max(1, int(limit))),
        "production_weight_changed": False,
    }


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))
