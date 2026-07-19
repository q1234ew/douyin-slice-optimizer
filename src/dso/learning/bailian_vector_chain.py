from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.multimodal_vector_value import (
    DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    load_multimodal_vector_manifest,
)
from dso.providers.aliyun_bailian import (
    AliyunBailianProvider,
    BAILIAN_CHALLENGER_JUDGE_MODEL,
    BAILIAN_DEFAULT_EMBEDDING_DIMENSION,
    BAILIAN_EMBEDDING_MODEL,
    BAILIAN_PRIMARY_JUDGE_MODEL,
    BAILIAN_RERANK_MODEL,
    jpeg_dimensions,
)
from dso.providers.contracts import (
    ProviderDataPermissionRecord,
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderRequest,
    stable_json_sha256,
)
from dso.providers.policy import UploadLevel
from dso.providers.service import (
    AliyunBailianRuntime,
    build_aliyun_bailian_runtime,
    public_model_status,
)
from dso.utils import new_id, read_json, utc_now, write_json


BAILIAN_VECTOR_CHAIN_VERSION = "bailian_multimodal_vector_chain.v1"
BAILIAN_VECTOR_INPUT_VERSION = "dso-bailian-vector-input.v1"
BAILIAN_RERANK_INPUT_VERSION = "dso-bailian-rerank-input.v1"
BAILIAN_JUDGE_INPUT_VERSION = "dso-bailian-pairwise-input.v2"
CLOUD_EMBEDDING_MODEL_NAME = f"aliyun:{BAILIAN_EMBEDDING_MODEL}"
CLOUD_EMBEDDING_MODEL_VERSION = "cn-beijing-2026-07-19"
CLOUD_VECTOR_MODALITIES = ("text", "fusion")
CLOUD_CHAIN_STAGES = frozenset(
    {"preflight", "smoke", "embeddings", "rerank", "judge", "full"}
)
DEFAULT_TOP_N = 20
DEFAULT_JUDGE_LIMIT = 20
MAX_JUDGE_LIMIT = 40
OUTCOME_PROXY_MIN_PAIR_COUNT = 40
OUTCOME_PROXY_REQUIRED_DELTA = 0.05
OUTCOME_PROXY_EARLY_STOP_PAIR_COUNT = 20
OUTCOME_PROXY_EARLY_STOP_DELTA = -0.10
_FRAME_ROLES = ("hook", "middle", "payoff")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_PAIR_CHOICES = frozenset({"left", "right", "tie"})


def bailian_vector_chain_status(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    manifest = load_multimodal_vector_manifest(benchmark_id)
    sample_ids = _all_sample_ids(manifest)
    coverage = _cloud_embedding_coverage(sample_ids)
    reports = {
        stage: _stage_report_path(str(manifest["benchmark_id"]), stage).is_file()
        for stage in (
            "preflight",
            "embeddings",
            "rerank",
            "judge",
            "ablation",
            "holdout-config",
            "holdout-predictions",
            "holdout-evaluation",
            "holdout-failure-attribution",
            "evidence-quality-reconstruction",
        )
    }
    rerank_report = _load_stage_report(str(manifest["benchmark_id"]), "rerank") or {}
    ablation_report = _load_stage_report(str(manifest["benchmark_id"]), "ablation") or {}
    from dso.learning.bailian_cached_ablation import cached_ablation_public_summary
    from dso.learning.bailian_evidence_quality import bailian_evidence_quality_status
    from dso.learning.bailian_failure_attribution import bailian_failure_attribution_status
    from dso.learning.bailian_holdout_validation import bailian_holdout_validation_status

    runtime = public_model_status()
    return {
        "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "status": "ready_for_shadow" if runtime.get("network_calls_allowed") else "disabled",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "models": {
            "embedding": BAILIAN_EMBEDDING_MODEL,
            "rerank": BAILIAN_RERANK_MODEL,
            "primary_judge": BAILIAN_PRIMARY_JUDGE_MODEL,
            "cost_challenger": BAILIAN_CHALLENGER_JUDGE_MODEL,
        },
        "embedding_coverage": coverage,
        "reports": reports,
        "outcome_proxy_comparison": rerank_report.get("outcome_proxy_comparison") or {},
        "cached_ablation": cached_ablation_public_summary(ablation_report),
        "holdout_validation": bailian_holdout_validation_status(
            str(manifest["benchmark_id"])
        ),
        "failure_attribution": bailian_failure_attribution_status(
            str(manifest["benchmark_id"])
        ),
        "evidence_quality": bailian_evidence_quality_status(
            str(manifest["benchmark_id"])
        ),
        "execution_plan": _execution_plan(manifest),
        "provider_gates": runtime.get("gates") or {},
        "configuration_errors": runtime.get("configuration_errors") or [],
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "automatic_publish": False,
        "generated_at": utc_now(),
    }


def run_bailian_vector_chain(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    *,
    stage: str = "smoke",
    limit: int = 10,
    top_n: int = DEFAULT_TOP_N,
    judge_limit: int = DEFAULT_JUDGE_LIMIT,
    force: bool = False,
    batch_id: str | None = None,
    runtime_builder: Callable[..., AliyunBailianRuntime] = build_aliyun_bailian_runtime,
) -> dict:
    selected_stage = str(stage or "smoke").strip().lower()
    if selected_stage not in CLOUD_CHAIN_STAGES:
        raise ValueError(f"unsupported Bailian vector stage: {selected_stage}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 40:
        raise ValueError("top_n must be between 1 and 40")
    if (
        isinstance(judge_limit, bool)
        or not isinstance(judge_limit, int)
        or not 1 <= judge_limit <= MAX_JUDGE_LIMIT
    ):
        raise ValueError("judge_limit must be between 1 and 40")

    manifest = load_multimodal_vector_manifest(benchmark_id)
    selected_batch = str(batch_id or f"bailian-vector-{uuid4().hex[:12]}")
    results: dict[str, Any] = {}

    if selected_stage == "preflight":
        all_ids = _all_sample_ids(manifest)
        selected_ids = all_ids[:limit] if limit else all_ids
        results["preflight"] = preflight_bailian_vector_index(
            manifest,
            entity_ids=selected_ids,
            batch_id=selected_batch,
        )
    elif selected_stage == "smoke":
        eval_ids = _paired_evaluation_ids(manifest, max(2, limit or 10))
        reference_ids = _balanced_reference_ids(manifest, max(20, top_n * 2))
        results["embeddings"] = build_bailian_vector_index(
            manifest,
            entity_ids=[*eval_ids, *reference_ids],
            force=force,
            batch_id=selected_batch,
            runtime_builder=runtime_builder,
        )
        results["rerank"] = run_bailian_vector_rerank(
            manifest,
            entity_ids=eval_ids,
            top_n=top_n,
            batch_id=selected_batch,
            runtime_builder=runtime_builder,
        )
        results["judge"] = run_bailian_disagreement_judges(
            manifest,
            rerank_report=results["rerank"],
            judge_limit=min(judge_limit, max(1, len(eval_ids) // 2)),
            batch_id=selected_batch,
            runtime_builder=runtime_builder,
        )
    else:
        if selected_stage in {"embeddings", "full"}:
            all_ids = _all_sample_ids(manifest)
            selected_ids = all_ids[:limit] if limit else all_ids
            results["embeddings"] = build_bailian_vector_index(
                manifest,
                entity_ids=selected_ids,
                force=force,
                batch_id=selected_batch,
                runtime_builder=runtime_builder,
            )
        if selected_stage in {"rerank", "full"}:
            eval_ids = _evaluation_ids(manifest)
            selected_eval_ids = (
                _paired_evaluation_ids(manifest, limit) if limit else eval_ids
            )
            results["rerank"] = run_bailian_vector_rerank(
                manifest,
                entity_ids=selected_eval_ids,
                top_n=top_n,
                batch_id=selected_batch,
                runtime_builder=runtime_builder,
            )
        if selected_stage in {"judge", "full"}:
            rerank_report = results.get("rerank") or _load_stage_report(
                str(manifest["benchmark_id"]), "rerank"
            )
            if not rerank_report:
                raise ValueError("judge stage requires a completed rerank report")
            results["judge"] = run_bailian_disagreement_judges(
                manifest,
                rerank_report=rerank_report,
                judge_limit=judge_limit,
                batch_id=selected_batch,
                runtime_builder=runtime_builder,
            )

    return {
        "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "status": "completed",
        "admission_status": "research_only",
        "stage": selected_stage,
        "batch_id": selected_batch,
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "results": results,
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "automatic_publish": False,
        "generated_at": utc_now(),
    }


def preflight_bailian_vector_index(
    manifest: dict,
    *,
    entity_ids: list[str],
    batch_id: str,
    report_stage: str = "preflight",
) -> dict:
    """Validate real local inputs without resolving a secret or invoking Bailian."""

    samples = _manifest_samples(manifest)
    selected_ids = [sample_id for sample_id in dict.fromkeys(entity_ids) if sample_id in samples]
    provider = AliyunBailianProvider(model_id=BAILIAN_EMBEDDING_MODEL)
    runtime = SimpleNamespace(
        provider=provider,
        data_permission=ProviderDataPermissionRecord(),
    )
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    serialized_bytes = 0
    frame_bytes = 0
    frame_count = 0
    reserved_cost = Decimal("0")

    for sample_id in selected_ids:
        sample = samples[sample_id]
        summary = _sample_summary(sample)
        frames = _sample_frames(sample, maximum=3)
        for modality in CLOUD_VECTOR_MODALITIES:
            current_frames = frames if modality == "fusion" else []
            if modality == "fusion" and not current_frames:
                counts["visual_missing"] += 1
                continue
            source_hash = _embedding_source_hash(manifest, sample, modality)
            request = _embedding_request(
                runtime,
                manifest=manifest,
                sample=sample,
                summary=summary,
                frames=current_frames,
                modality=modality,
                source_hash=source_hash,
                batch_id=batch_id,
            )
            try:
                probe = provider.preflight_request(request)
            except (TypeError, ValueError) as exc:
                counts["failed"] += 1
                errors.append(
                    {
                        "sample_id": sample_id,
                        "modality": modality,
                        "error": str(exc),
                    }
                )
                continue
            counts["validated"] += 1
            counts[modality] += 1
            serialized_bytes += int(probe["serialized_request_bytes"])
            frame_count += len(current_frames)
            frame_bytes += sum(
                len(base64.b64decode(frame["data_base64"])) for frame in current_frames
            )
            reserved_cost += Decimal(str(probe["reserved_cost_cny"]))

    report = {
        "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "stage": "preflight",
        "status": "ready" if counts["validated"] and not counts["failed"] else "incomplete",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "model": BAILIAN_EMBEDDING_MODEL,
        "dimension": BAILIAN_DEFAULT_EMBEDDING_DIMENSION,
        "target_sample_count": len(selected_ids),
        "validated_request_count": counts["validated"],
        "text_request_count": counts["text"],
        "fusion_request_count": counts["fusion"],
        "visual_missing_count": counts["visual_missing"],
        "failed_count": counts["failed"],
        "frame_count": frame_count,
        "frame_bytes": frame_bytes,
        "serialized_request_bytes": serialized_bytes,
        "maximum_reserved_cost_cny": str(reserved_cost),
        "network_request_count": 0,
        "effective_cost_cny": "0",
        "secret_resolved": False,
        "errors": errors[:20],
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "generated_at": utc_now(),
    }
    _persist_stage_report(manifest, report_stage, report)
    return report


def build_bailian_vector_index(
    manifest: dict,
    *,
    entity_ids: list[str],
    force: bool,
    batch_id: str,
    report_stage: str = "embeddings",
    runtime_builder: Callable[..., AliyunBailianRuntime] = build_aliyun_bailian_runtime,
) -> dict:
    samples = _manifest_samples(manifest)
    selected_ids = [sample_id for sample_id in dict.fromkeys(entity_ids) if sample_id in samples]
    runtime = runtime_builder(batch_id=batch_id, model_id=BAILIAN_EMBEDDING_MODEL)
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    total_cost = Decimal("0")
    network_requests = 0
    for sample_id in selected_ids:
        sample = samples[sample_id]
        summary = _sample_summary(sample)
        frames = _sample_frames(sample, maximum=3)
        for modality in CLOUD_VECTOR_MODALITIES:
            current_frames = frames if modality == "fusion" else []
            if modality == "fusion" and not current_frames:
                counts["skipped"] += 1
                errors.append({"sample_id": sample_id, "modality": modality, "error": "visual_missing"})
                continue
            source_hash = _embedding_source_hash(manifest, sample, modality)
            cached = _find_cloud_embedding(sample_id, modality, source_hash)
            if cached and not force and Path(str(cached.get("vector_path") or "")).is_file():
                counts["reused"] += 1
                continue
            request = _embedding_request(
                runtime,
                manifest=manifest,
                sample=sample,
                summary=summary,
                frames=current_frames,
                modality=modality,
                source_hash=source_hash,
                batch_id=batch_id,
            )
            reservation = runtime.provider.estimate_max_cost(request)
            outcome = runtime.runner.execute(
                request,
                estimated_cost=reservation,
                upload_level=(
                    UploadLevel.REPRESENTATIVE_FRAMES
                    if current_frames
                    else UploadLevel.STRUCTURED_SUMMARY
                ),
                batch_id=batch_id,
                local_baseline={"status": "cloud_embedding_unavailable"},
            )
            total_cost += Decimal(outcome.estimated_cost)
            network_requests += int(outcome.network_request_count)
            if outcome.status not in {"shadow_succeeded", "shadow_cached"}:
                counts["failed"] += 1
                errors.append(
                    {
                        "sample_id": sample_id,
                        "modality": modality,
                        "error": outcome.policy_code,
                    }
                )
                continue
            vector = _select_embedding_vector(outcome.provider_output, modality)
            if len(vector) != BAILIAN_DEFAULT_EMBEDDING_DIMENSION:
                counts["failed"] += 1
                errors.append(
                    {"sample_id": sample_id, "modality": modality, "error": "invalid_vector"}
                )
                continue
            _store_cloud_embedding(
                sample=sample,
                modality=modality,
                source_hash=source_hash,
                vector=vector,
            )
            counts["created"] += 1

    coverage = _cloud_embedding_coverage(selected_ids)
    report = {
        "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "stage": "embeddings",
        "status": "ready" if coverage.get("text_ready_count") else "incomplete",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "model": BAILIAN_EMBEDDING_MODEL,
        "dimension": BAILIAN_DEFAULT_EMBEDDING_DIMENSION,
        "target_sample_count": len(selected_ids),
        "created": counts["created"],
        "reused": counts["reused"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "coverage": coverage,
        "network_request_count": network_requests,
        "effective_cost_cny": str(total_cost),
        "errors": errors[:20],
        "generated_at": utc_now(),
    }
    _persist_stage_report(manifest, report_stage, report)
    return report


def run_bailian_vector_rerank(
    manifest: dict,
    *,
    entity_ids: list[str],
    top_n: int,
    batch_id: str,
    include_outcomes: bool = True,
    report_stage: str = "rerank",
    reference_ids: list[str] | None = None,
    runtime_builder: Callable[..., AliyunBailianRuntime] = build_aliyun_bailian_runtime,
) -> dict:
    samples = _manifest_samples(manifest)
    selected_ids = [sample_id for sample_id in dict.fromkeys(entity_ids) if sample_id in samples]
    selected_reference_ids = (
        [sample_id for sample_id in dict.fromkeys(reference_ids) if sample_id in samples]
        if reference_ids is not None
        else _reference_ids(manifest)
    )
    available_reference_vectors = {
        sample_id: vector
        for sample_id in selected_reference_ids
        if (vector := _preferred_cloud_vector(sample_id))
    }
    reference_vectors, reference_pool = _balanced_reference_vectors(
        manifest, available_reference_vectors
    )
    if not reference_vectors:
        raise ValueError(
            "rerank stage requires both high and low cloud embedding references"
        )
    runtime = runtime_builder(batch_id=batch_id, model_id=BAILIAN_RERANK_MODEL)
    items = []
    errors = []
    total_cost = Decimal("0")
    network_requests = 0
    for sample_id in selected_ids:
        query_vector = _preferred_cloud_vector(sample_id)
        if not query_vector:
            errors.append({"sample_id": sample_id, "error": "embedding_missing"})
            continue
        retrieved = sorted(
            (
                {
                    "sample_id": reference_id,
                    "embedding_similarity": _cosine(query_vector, reference_vector),
                }
                for reference_id, reference_vector in reference_vectors.items()
            ),
            key=lambda item: (-float(item["embedding_similarity"]), item["sample_id"]),
        )[:top_n]
        documents = [
            {
                "sample_id": item["sample_id"],
                "text": _sample_summary(samples[item["sample_id"]]),
            }
            for item in retrieved
        ]
        request = _rerank_request(
            runtime,
            manifest=manifest,
            sample=samples[sample_id],
            documents=documents,
            top_n=top_n,
            batch_id=batch_id,
        )
        outcome = runtime.runner.execute(
            request,
            estimated_cost=runtime.provider.estimate_max_cost(request),
            upload_level=UploadLevel.STRUCTURED_SUMMARY,
            batch_id=batch_id,
            local_baseline={"retrieved": retrieved},
        )
        total_cost += Decimal(outcome.estimated_cost)
        network_requests += int(outcome.network_request_count)
        if outcome.status not in {"shadow_succeeded", "shadow_cached"}:
            errors.append({"sample_id": sample_id, "error": outcome.policy_code})
            continue
        reranked = outcome.provider_output.get("results")
        if not isinstance(reranked, list):
            errors.append({"sample_id": sample_id, "error": "invalid_rerank_results"})
            continue
        score = _evidence_score(reranked, samples)
        items.append(
            {
                "sample_id": sample_id,
                "score": score["score"],
                "high_evidence": score["high_evidence"],
                "low_risk": score["low_risk"],
                "top_matches": reranked[:5],
            }
        )
    if include_outcomes:
        pair_results, baseline_comparison = _cloud_pair_results(manifest, items)
        outcome_proxy_comparison = _outcome_proxy_comparison(pair_results)
        disagreement_queue = [
            item
            for item in pair_results
            if item.get("comparison_status") == "comparable"
            and item.get("choice_disagreement") is True
        ]
    else:
        pair_results = []
        baseline_comparison = {
            "status": "labels_locked",
            "source": "independent_holdout_prediction",
            "available_pair_count": 0,
        }
        outcome_proxy_comparison = {}
        disagreement_queue = []
    report = {
        "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "stage": "rerank",
        "status": "ready" if items else "incomplete",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "embedding_model": BAILIAN_EMBEDDING_MODEL,
        "rerank_model": BAILIAN_RERANK_MODEL,
        "rerank_modality": "structured_text_over_fused_embedding_retrieval",
        "target_count": len(selected_ids),
        "completed_count": len(items),
        "top_n": top_n,
        "reference_pool": reference_pool,
        "labels_locked": not include_outcomes,
        "network_request_count": network_requests,
        "effective_cost_cny": str(total_cost),
        "items": items,
        "pair_results": pair_results,
        "baseline_comparison": baseline_comparison,
        "outcome_proxy_comparison": outcome_proxy_comparison,
        "disagreement_queue": sorted(
            disagreement_queue,
            key=lambda item: (-float(item.get("disagreement_score") or 0.0), str(item.get("task_id") or "")),
        )[:MAX_JUDGE_LIMIT],
        "errors": errors[:20],
        "production_weight_changed": False,
        "generated_at": utc_now(),
    }
    _persist_stage_report(manifest, report_stage, report)
    return report


def run_bailian_disagreement_judges(
    manifest: dict,
    *,
    rerank_report: dict,
    judge_limit: int,
    batch_id: str,
    runtime_builder: Callable[..., AliyunBailianRuntime] = build_aliyun_bailian_runtime,
) -> dict:
    queue = rerank_report.get("disagreement_queue")
    if not isinstance(queue, list) or not queue:
        baseline = rerank_report.get("baseline_comparison") or {}
        reason = (
            "baseline_missing"
            if baseline.get("status") in {"missing", "incompatible", "partial"}
            else "no_comparable_disagreement_samples"
        )
        report = {
            "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
            "stage": "judge",
            "status": "not_ready",
            "admission_status": "research_only",
            "benchmark_id": manifest["benchmark_id"],
            "manifest_sha256": manifest["manifest_sha256"],
            "selected_count": 0,
            "selection_policy": "verified v2.4 versus fusion-plus-rerank choice disagreement only",
            "judge_input_version": BAILIAN_JUDGE_INPUT_VERSION,
            "blind_to_ranker_choices": True,
            "not_ready_reason": reason,
            "baseline_comparison": baseline,
            "models": {},
            "results": {},
            "comparison": {
                "comparable_count": 0,
                "judge_agreement_rate": 0.0,
                "human_gold_comparison_available": False,
                "note": "No Judge request was sent because a verified v2.4 disagreement queue was unavailable.",
            },
            "writes_manual_gold": False,
            "production_weight_changed": False,
            "generated_at": utc_now(),
        }
        _persist_stage_report(manifest, "judge", report)
        return report
    selected = queue[:judge_limit]
    samples = _manifest_samples(manifest)
    results_by_model: dict[str, list[dict]] = {}
    model_summaries = {}
    for model_id in (BAILIAN_PRIMARY_JUDGE_MODEL, BAILIAN_CHALLENGER_JUDGE_MODEL):
        runtime = runtime_builder(batch_id=batch_id, model_id=model_id)
        model_results = []
        total_cost = Decimal("0")
        network_requests = 0
        for item in selected:
            task_id = str(item.get("task_id") or "")
            left = samples.get(str(item.get("left_sample_id") or "")) or {}
            right = samples.get(str(item.get("right_sample_id") or "")) or {}
            request = _judge_request(
                runtime,
                manifest=manifest,
                task_id=task_id,
                left=left,
                right=right,
                context={
                    "evaluation_scope": "blind_pairwise_editorial_utility",
                    "available_evidence": "candidate summaries and representative frames only",
                    "platform_views_or_exposure_available": False,
                },
                batch_id=batch_id,
            )
            upload_level = (
                UploadLevel.REPRESENTATIVE_FRAMES
                if request.input_size.image_count
                else UploadLevel.STRUCTURED_SUMMARY
            )
            outcome = runtime.runner.execute(
                request,
                estimated_cost=runtime.provider.estimate_max_cost(request),
                upload_level=upload_level,
                batch_id=batch_id,
                local_baseline={"choice": item.get("cloud_choice") or "abstain"},
            )
            total_cost += Decimal(outcome.estimated_cost)
            network_requests += int(outcome.network_request_count)
            model_results.append(
                {
                    "task_id": task_id,
                    "status": outcome.status,
                    "policy_code": outcome.policy_code,
                    "output": outcome.provider_output,
                    "cache_hit": outcome.cache_hit,
                }
            )
        results_by_model[model_id] = model_results
        model_summaries[model_id] = {
            "completed_count": sum(bool(item.get("output")) for item in model_results),
            "network_request_count": network_requests,
            "effective_cost_cny": str(total_cost),
        }
    comparison = _judge_comparison(selected, results_by_model)
    report = {
        "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "stage": "judge",
        "status": "ready",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "selected_count": len(selected),
        "selection_policy": "highest verified choice disagreement between frozen v2.4 and fusion-plus-rerank",
        "judge_input_version": BAILIAN_JUDGE_INPUT_VERSION,
        "blind_to_ranker_choices": True,
        "models": model_summaries,
        "results": results_by_model,
        "comparison": comparison,
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "generated_at": utc_now(),
    }
    _persist_stage_report(manifest, "judge", report)
    return report


def _embedding_request(
    runtime: AliyunBailianRuntime,
    *,
    manifest: dict,
    sample: dict,
    summary: str,
    frames: list[dict],
    modality: str,
    source_hash: str,
    batch_id: str,
) -> ProviderRequest:
    image_bytes = sum(len(base64.b64decode(frame["data_base64"])) for frame in frames)
    estimated_image_tokens = len(frames) * 4096
    input_tokens = max(1, len(summary) + estimated_image_tokens)
    return ProviderRequest(
        request_id=f"{batch_id}-emb-{_safe_name(str(sample.get('sample_id') or 'sample'))}-{modality}",
        request_type="multimodal_embedding",
        target=runtime.provider.descriptor.identity,
        content_sha256=source_hash,
        input_size=ProviderInputSize(
            frame_count=len(frames),
            image_count=len(frames),
            text_characters=len(summary),
            input_tokens=input_tokens,
            request_bytes=len(summary.encode("utf-8")) + image_bytes,
        ),
        data_permission=runtime.data_permission,
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=90,
            max_retries=1,
        ),
        payload={"summary": summary, **({"frames": frames} if frames else {})},
        parameters={
            "dimension": BAILIAN_DEFAULT_EMBEDDING_DIMENSION,
            "enable_fusion": modality == "fusion",
            "instruct": "检索语义、素材形态、叙事结构和传播价值相近的华语音乐综艺短视频。",
            "estimated_image_tokens": estimated_image_tokens,
        },
    )


def _rerank_request(
    runtime: AliyunBailianRuntime,
    *,
    manifest: dict,
    sample: dict,
    documents: list[dict],
    top_n: int,
    batch_id: str,
) -> ProviderRequest:
    query_text = _sample_summary(sample)
    document_characters = sum(len(str(item.get("text") or "")) for item in documents)
    text_characters = len(query_text) + document_characters
    estimated_input_tokens = len(query_text) * len(documents) + document_characters
    content_hash = stable_json_sha256(
        {
            "manifest_sha256": manifest["manifest_sha256"],
            "version": BAILIAN_RERANK_INPUT_VERSION,
            "query_sample_id": sample.get("sample_id") or "",
            "document_ids": [item.get("sample_id") or "" for item in documents],
            "top_n": top_n,
        }
    )
    return ProviderRequest(
        request_id=f"{batch_id}-rerank-{_safe_name(str(sample.get('sample_id') or 'sample'))}",
        request_type="multimodal_rerank",
        target=runtime.provider.descriptor.identity,
        content_sha256=content_hash,
        input_size=ProviderInputSize(
            text_characters=text_characters,
            input_tokens=max(1, estimated_input_tokens),
            request_bytes=text_characters * 3,
        ),
        data_permission=runtime.data_permission,
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=90,
            max_retries=1,
        ),
        payload={"query": {"text": query_text}, "documents": documents},
        parameters={
            "top_n": min(top_n, len(documents)),
            "return_documents": False,
            "instruct": "按短视频语义、素材形态、叙事结构和传播价值相似性重排。",
            "estimated_image_tokens": 0,
        },
    )


def _judge_request(
    runtime: AliyunBailianRuntime,
    *,
    manifest: dict,
    task_id: str,
    left: dict,
    right: dict,
    context: dict,
    batch_id: str,
) -> ProviderRequest:
    left_summary = _sample_summary(left)
    right_summary = _sample_summary(right)
    left_frames = _sample_frames(left, maximum=1)
    right_frames = _sample_frames(right, maximum=1)
    left_frame = left_frames[0] if left_frames else None
    right_frame = right_frames[0] if right_frames else None
    frames = [frame for frame in (left_frame, right_frame) if frame]
    text_characters = len(left_summary) + len(right_summary) + len(json.dumps(context, ensure_ascii=False))
    content_hash = stable_json_sha256(
        {
            "manifest_sha256": manifest["manifest_sha256"],
            "version": BAILIAN_JUDGE_INPUT_VERSION,
            "task_id": task_id,
            "model": runtime.provider.descriptor.identity.model_id,
            "left": left.get("sample_id") or "",
            "right": right.get("sample_id") or "",
            "context": context,
            "frame_sha256": [
                hashlib.sha256(base64.b64decode(frame["data_base64"])).hexdigest()
                for frame in frames
            ],
        }
    )
    return ProviderRequest(
        request_id=f"{batch_id}-judge-{_safe_name(task_id)}-{_safe_name(runtime.provider.descriptor.identity.model_id)}",
        request_type="pairwise_judge",
        target=runtime.provider.descriptor.identity,
        content_sha256=content_hash,
        input_size=ProviderInputSize(
            frame_count=len(frames),
            image_count=len(frames),
            text_characters=text_characters,
            input_tokens=max(1, text_characters + len(frames) * 4096),
            request_bytes=text_characters * 3
            + sum(len(base64.b64decode(frame["data_base64"])) for frame in frames),
        ),
        data_permission=runtime.data_permission,
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=90,
            max_retries=1,
        ),
        payload={
            "left": {"summary": left_summary, **({"frame": left_frame} if left_frame else {})},
            "right": {"summary": right_summary, **({"frame": right_frame} if right_frame else {})},
            "context": json.dumps(context, ensure_ascii=False, sort_keys=True),
        },
        parameters={"estimated_output_tokens": 500},
    )


def _manifest_samples(manifest: dict) -> dict[str, dict]:
    samples = manifest.get("samples")
    if not isinstance(samples, dict):
        raise ValueError("frozen benchmark samples are missing")
    return {str(key): value for key, value in samples.items() if isinstance(value, dict)}


def _evaluation_ids(manifest: dict) -> list[str]:
    return [str(value) for value in manifest.get("evaluation_sample_ids") or []]


def _reference_ids(manifest: dict) -> list[str]:
    return [str(value) for value in manifest.get("reference_sample_ids") or []]


def _balanced_reference_ids(manifest: dict, limit: int) -> list[str]:
    samples = _manifest_samples(manifest)
    buckets = {
        label: [
            sample_id
            for sample_id in _reference_ids(manifest)
            if str((samples.get(sample_id) or {}).get("performance_label") or "") == label
        ]
        for label in ("high", "low")
    }
    result: list[str] = []
    pair_limit = max(1, limit // 2)
    for index in range(pair_limit):
        for label in ("high", "low"):
            if index < len(buckets[label]) and len(result) < limit:
                result.append(buckets[label][index])
    return result


def _balanced_reference_vectors(
    manifest: dict, available_vectors: dict[str, list[float]]
) -> tuple[dict[str, list[float]], dict]:
    samples = _manifest_samples(manifest)
    buckets = {
        label: [
            sample_id
            for sample_id in _reference_ids(manifest)
            if sample_id in available_vectors
            and str((samples.get(sample_id) or {}).get("performance_label") or "") == label
        ]
        for label in ("high", "low")
    }
    per_label = min(len(buckets["high"]), len(buckets["low"]))
    selected = [
        sample_id
        for index in range(per_label)
        for sample_id in (buckets["high"][index], buckets["low"][index])
    ]
    return (
        {sample_id: available_vectors[sample_id] for sample_id in selected},
        {
            "status": "ready" if per_label else "unbalanced",
            "available_count": len(available_vectors),
            "balanced_count": len(selected),
            "high_count": per_label,
            "low_count": per_label,
            "excluded_unbalanced_count": len(available_vectors) - len(selected),
        },
    )


def _paired_evaluation_ids(manifest: dict, limit: int) -> list[str]:
    result = []
    for task in manifest.get("tasks") or []:
        for key in ("left_sample_id", "right_sample_id"):
            sample_id = str(task.get(key) or "")
            if sample_id and sample_id not in result:
                result.append(sample_id)
        if len(result) >= limit:
            break
    return result[: max(2, limit)]


def _all_sample_ids(manifest: dict) -> list[str]:
    return list(dict.fromkeys([*_evaluation_ids(manifest), *_reference_ids(manifest)]))


def _sample_summary(sample: dict) -> str:
    semantic = sample.get("semantic") if isinstance(sample.get("semantic"), dict) else {}
    fields = [
        ("标题", sample.get("title")),
        ("内容分类", semantic.get("content_category")),
        ("开场类型", semantic.get("hook_type")),
        ("切片结构", semantic.get("slice_structure")),
        ("艺人", semantic.get("artist_names")),
        ("歌曲", semantic.get("song_title")),
        ("节目", semantic.get("program_name")),
    ]
    return "\n".join(f"{label}: {str(value).strip()}" for label, value in fields if str(value or "").strip())[:12_000]


def _sample_frames(sample: dict, *, maximum: int) -> list[dict]:
    media = sample.get("media") if isinstance(sample.get("media"), dict) else {}
    sources = media.get("visual_sources") if isinstance(media.get("visual_sources"), list) else []
    root = ensure_data_dirs().root.resolve()
    allowed = (ensure_data_dirs().data_dir / "douyin_media_assets").resolve()
    frames = []
    for source in sources[:maximum]:
        if not isinstance(source, dict):
            continue
        path = (root / str(source.get("path") or "")).resolve()
        if allowed not in path.parents or not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg"}:
            continue
        raw = path.read_bytes()
        try:
            width, height = jpeg_dimensions(raw)
        except ValueError:
            continue
        if len(raw) > 1_000_000 or max(width, height) > 1280:
            normalized = _normalized_representative_frame(path, raw)
            if normalized is None:
                continue
            raw = normalized
        frames.append(
            {
                "role": _FRAME_ROLES[len(frames)],
                "mime_type": "image/jpeg",
                "data_base64": base64.b64encode(raw).decode("ascii"),
            }
        )
    return frames


def _normalized_representative_frame(path: Path, raw: bytes) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    source_hash = hashlib.sha256(raw).hexdigest()
    cache_path = (
        ensure_data_dirs().cache_dir
        / "bailian_embeddings"
        / "normalized_frames"
        / f"{source_hash}.jpg"
    )
    if cache_path.is_file():
        cached = cache_path.read_bytes()
        try:
            width, height = jpeg_dimensions(cached)
        except ValueError:
            cached = b""
        if cached and len(cached) <= 1_000_000 and max(width, height) <= 1280:
            return cached

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f".{cache_path.stem}.{uuid4().hex}.tmp.jpg")
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:1280:force_original_aspect_ratio=decrease",
        "-q:v",
        "4",
        str(temporary),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if completed.returncode != 0 or not temporary.is_file():
            return None
        normalized = temporary.read_bytes()
        width, height = jpeg_dimensions(normalized)
        if len(normalized) > 1_000_000 or max(width, height) > 1280:
            return None
        os.replace(temporary, cache_path)
        return normalized
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        temporary.unlink(missing_ok=True)


def _embedding_source_hash(manifest: dict, sample: dict, modality: str) -> str:
    media = sample.get("media") if isinstance(sample.get("media"), dict) else {}
    visuals = media.get("visual_sources") if isinstance(media.get("visual_sources"), list) else []
    return stable_json_sha256(
        {
            "manifest_sha256": manifest["manifest_sha256"],
            "version": BAILIAN_VECTOR_INPUT_VERSION,
            "model": BAILIAN_EMBEDDING_MODEL,
            "dimension": BAILIAN_DEFAULT_EMBEDDING_DIMENSION,
            "sample_id": sample.get("sample_id") or "",
            "modality": modality,
            "summary": _sample_summary(sample),
            "visual_sha256": [
                str(item.get("sha256") or "")
                for item in visuals[:3]
                if isinstance(item, dict)
            ]
            if modality == "fusion"
            else [],
        }
    )


def _select_embedding_vector(output: dict, modality: str) -> list[float]:
    embeddings = output.get("embeddings") if isinstance(output, dict) else None
    if not isinstance(embeddings, list):
        return []
    preferred_type = "fusion" if modality == "fusion" else "vl"
    item = next(
        (value for value in embeddings if isinstance(value, dict) and value.get("type") == preferred_type),
        None,
    )
    if item is None and len(embeddings) == 1 and isinstance(embeddings[0], dict):
        item = embeddings[0]
    vector = item.get("embedding") if isinstance(item, dict) else None
    if not isinstance(vector, list):
        return []
    try:
        parsed = [float(value) for value in vector]
    except (TypeError, ValueError):
        return []
    return parsed if all(math.isfinite(value) for value in parsed) else []


def _store_cloud_embedding(
    *,
    sample: dict,
    modality: str,
    source_hash: str,
    vector: list[float],
) -> None:
    sample_id = str(sample.get("sample_id") or "")
    path = (
        ensure_data_dirs().cache_dir
        / "bailian_embeddings"
        / "historical_sample"
        / modality
        / f"{_safe_name(sample_id)}_{source_hash[:16]}.json"
    )
    write_json(
        path,
        {
            "contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
            "entity_type": "historical_sample",
            "entity_id": sample_id,
            "modality": modality,
            "model_name": CLOUD_EMBEDDING_MODEL_NAME,
            "model_version": CLOUD_EMBEDDING_MODEL_VERSION,
            "source_hash": source_hash,
            "vector_dim": len(vector),
            "vector": vector,
            "created_at": utc_now(),
        },
    )
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM embedding_records
            WHERE entity_type = 'historical_sample' AND entity_id = ? AND modality = ?
              AND model_name = ? AND source_hash = ?
            """,
            [sample_id, modality, CLOUD_EMBEDDING_MODEL_NAME, source_hash],
        )
        insert_row(
            conn,
            "embedding_records",
            {
                "id": new_id("embrec"),
                "entity_type": "historical_sample",
                "entity_id": sample_id,
                "account_id": str(sample.get("account_id") or ""),
                "dataset_id": str(sample.get("dataset_id") or ""),
                "platform_item_id": str(sample.get("platform_item_id") or ""),
                "modality": modality,
                "model_name": CLOUD_EMBEDDING_MODEL_NAME,
                "model_version": CLOUD_EMBEDDING_MODEL_VERSION,
                "vector_path": str(path),
                "vector_dim": len(vector),
                "source_hash": source_hash,
                "status": "ready",
                "error": "",
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.commit()


def _find_cloud_embedding(sample_id: str, modality: str, source_hash: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT * FROM embedding_records
            WHERE entity_type = 'historical_sample' AND entity_id = ? AND modality = ?
              AND model_name = ? AND source_hash = ? AND status = 'ready' AND vector_dim = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            [
                sample_id,
                modality,
                CLOUD_EMBEDDING_MODEL_NAME,
                source_hash,
                BAILIAN_DEFAULT_EMBEDDING_DIMENSION,
            ],
        )


def _cloud_records(sample_ids: list[str]) -> dict[str, dict[str, dict]]:
    if not sample_ids:
        return {}
    wanted = set(sample_ids)
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT * FROM embedding_records
            WHERE entity_type = 'historical_sample' AND model_name = ?
              AND status = 'ready' AND vector_dim = ?
            ORDER BY updated_at DESC
            """,
            [CLOUD_EMBEDDING_MODEL_NAME, BAILIAN_DEFAULT_EMBEDDING_DIMENSION],
        )
    result: dict[str, dict[str, dict]] = {}
    for row in rows:
        sample_id = str(row.get("entity_id") or "")
        modality = str(row.get("modality") or "")
        if sample_id not in wanted or modality in (result.get(sample_id) or {}):
            continue
        result.setdefault(sample_id, {})[modality] = row
    return result


def _cloud_embedding_coverage(sample_ids: list[str]) -> dict:
    ids = list(dict.fromkeys(sample_ids))
    records = _cloud_records(ids)
    return {
        "sample_count": len(ids),
        "text_ready_count": sum("text" in records.get(sample_id, {}) for sample_id in ids),
        "fusion_ready_count": sum("fusion" in records.get(sample_id, {}) for sample_id in ids),
        "text_fusion_ready_count": sum(
            {"text", "fusion"}.issubset(records.get(sample_id, {})) for sample_id in ids
        ),
        "model_name": CLOUD_EMBEDDING_MODEL_NAME,
        "dimension": BAILIAN_DEFAULT_EMBEDDING_DIMENSION,
    }


def _preferred_cloud_vector(sample_id: str) -> list[float]:
    records = _cloud_records([sample_id]).get(sample_id) or {}
    record = records.get("fusion") or records.get("text")
    if not record:
        return []
    path = Path(str(record.get("vector_path") or ""))
    payload = read_json(path, default={}) if path.is_file() else {}
    vector = payload.get("vector") if isinstance(payload, dict) else None
    if not isinstance(vector, list) or len(vector) != BAILIAN_DEFAULT_EMBEDDING_DIMENSION:
        return []
    try:
        parsed = [float(value) for value in vector]
    except (TypeError, ValueError):
        return []
    return parsed if all(math.isfinite(value) for value in parsed) else []


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return float(dot / (norm_left * norm_right))


def _evidence_score(results: list[dict], samples: dict[str, dict]) -> dict:
    high = []
    low = []
    for result in results:
        sample = samples.get(str(result.get("sample_id") or "")) or {}
        score = float(result.get("relevance_score") or 0.0)
        if sample.get("performance_label") == "high":
            high.append(score)
        elif sample.get("performance_label") == "low":
            low.append(score)
    high_value = sum(high[:5]) / max(1, len(high[:5]))
    low_value = sum(low[:5]) / max(1, len(low[:5]))
    return {
        "score": round(max(0.0, min(100.0, 50.0 + (high_value - low_value) * 30.0)), 4),
        "high_evidence": round(high_value, 4),
        "low_risk": round(low_value, 4),
    }


def _cloud_pair_results(manifest: dict, items: list[dict]) -> tuple[list[dict], dict]:
    scores = {str(item.get("sample_id") or ""): float(item.get("score") or 0.0) for item in items}
    local_report, baseline_source = _local_vector_report(str(manifest["benchmark_id"]))
    manifest_matches = bool(local_report) and (
        str(local_report.get("benchmark_id") or "") == str(manifest.get("benchmark_id") or "")
        and str(local_report.get("manifest_sha256") or "")
        == str(manifest.get("manifest_sha256") or "")
    )
    local_pairs = {
        str(item.get("task_id") or ""): item
        for item in (local_report.get("pair_results") or [])
        if isinstance(item, dict)
    } if manifest_matches else {}
    result = []
    for task in manifest.get("tasks") or []:
        task_id = str(task.get("task_id") or "")
        left_id = str(task.get("left_sample_id") or "")
        right_id = str(task.get("right_sample_id") or "")
        if left_id not in scores or right_id not in scores:
            continue
        delta = scores[left_id] - scores[right_id]
        cloud_choice = "tie" if abs(delta) < 0.5 else "left" if delta > 0 else "right"
        cloud_outcome_choice = "tie" if delta == 0 else "left" if delta > 0 else "right"
        local = local_pairs.get(task_id) or {}
        v24_choice = str((local.get("predictions") or {}).get("research_ranker_v2_4") or "unknown")
        outcome_proxy_choice = str(local.get("proxy_choice") or "unknown")
        baseline_available = v24_choice in _PAIR_CHOICES
        outcome_proxy_available = outcome_proxy_choice in _PAIR_CHOICES
        choice_disagreement = baseline_available and cloud_choice != v24_choice
        disagreement = None
        if baseline_available:
            disagreement = (1.0 if choice_disagreement else 0.0) + min(
                0.5, abs(delta) / 50.0
            )
        result.append(
            {
                "task_id": task_id,
                "left_sample_id": left_id,
                "right_sample_id": right_id,
                "v2_4_choice": v24_choice,
                "cloud_choice": cloud_choice,
                "cloud_outcome_choice": cloud_outcome_choice,
                "outcome_proxy_choice": outcome_proxy_choice,
                "cloud_score_delta": round(delta, 4),
                "disagreement_score": round(disagreement, 4) if disagreement is not None else None,
                "comparison_status": "comparable" if baseline_available else "baseline_missing",
                "outcome_proxy_status": "comparable" if outcome_proxy_available else "proxy_missing",
                "choice_disagreement": choice_disagreement,
                "cloud_matches_outcome_proxy": (
                    cloud_outcome_choice == outcome_proxy_choice
                    if outcome_proxy_available
                    else None
                ),
                "v2_4_matches_outcome_proxy": (
                    v24_choice == outcome_proxy_choice
                    if outcome_proxy_available and baseline_available
                    else None
                ),
            }
        )
    available_count = sum(item["comparison_status"] == "comparable" for item in result)
    if not local_report:
        baseline_status = "missing"
    elif not manifest_matches:
        baseline_status = "incompatible"
    elif available_count < len(result):
        baseline_status = "partial"
    else:
        baseline_status = "ready"
    return result, {
        "status": baseline_status,
        "source": baseline_source,
        "manifest_sha256_match": manifest_matches,
        "expected_pair_count": len(result),
        "available_pair_count": available_count,
        "outcome_proxy_available_pair_count": sum(
            item.get("outcome_proxy_status") == "comparable" for item in result
        ),
        "missing_pair_count": len(result) - available_count,
        "choice_disagreement_count": sum(
            item.get("choice_disagreement") is True for item in result
        ),
        "missing_task_ids": [
            item["task_id"] for item in result if item["comparison_status"] != "comparable"
        ][:20],
    }


def _outcome_proxy_comparison(pair_results: list[dict]) -> dict:
    comparable = [
        item
        for item in pair_results
        if item.get("outcome_proxy_status") == "comparable"
        and item.get("cloud_matches_outcome_proxy") is not None
        and item.get("v2_4_matches_outcome_proxy") is not None
    ]
    pair_count = len(comparable)
    cloud_correct = sum(item.get("cloud_matches_outcome_proxy") is True for item in comparable)
    v24_correct = sum(item.get("v2_4_matches_outcome_proxy") is True for item in comparable)
    cloud_accuracy = cloud_correct / pair_count if pair_count else 0.0
    v24_accuracy = v24_correct / pair_count if pair_count else 0.0
    outcome_choice_counts = Counter(
        str(item.get("outcome_proxy_choice") or "") for item in comparable
    )
    observed_choices = [
        choice for choice in sorted(_PAIR_CHOICES) if outcome_choice_counts.get(choice)
    ]

    def balanced_accuracy(match_key: str) -> float:
        if not observed_choices:
            return 0.0
        return sum(
            sum(
                item.get(match_key) is True
                for item in comparable
                if item.get("outcome_proxy_choice") == choice
            )
            / outcome_choice_counts[choice]
            for choice in observed_choices
        ) / len(observed_choices)

    cloud_balanced_accuracy = balanced_accuracy("cloud_matches_outcome_proxy")
    v24_balanced_accuracy = balanced_accuracy("v2_4_matches_outcome_proxy")
    accuracy_delta = cloud_balanced_accuracy - v24_balanced_accuracy
    raw_accuracy_delta = cloud_accuracy - v24_accuracy
    outcome_diversity_ready = len(observed_choices) >= 2
    sample_ready = pair_count >= OUTCOME_PROXY_MIN_PAIR_COUNT and outcome_diversity_ready
    passed = sample_ready and accuracy_delta >= OUTCOME_PROXY_REQUIRED_DELTA
    early_stop = (
        pair_count >= OUTCOME_PROXY_EARLY_STOP_PAIR_COUNT
        and outcome_diversity_ready
        and accuracy_delta <= OUTCOME_PROXY_EARLY_STOP_DELTA
    )
    if early_stop:
        status = "early_stop"
        decision = "stop_expansion_and_diagnose_retrieval_or_rerank"
    elif not outcome_diversity_ready:
        status = "insufficient_outcome_diversity"
        decision = "rebalance_pair_orientation_before_evaluation"
    elif not sample_ready:
        status = "insufficient_sample"
        decision = "continue_to_minimum_pair_count"
    elif passed:
        status = "ready"
        decision = "positive_research_signal_requires_full_gate_review"
    else:
        status = "ready"
        decision = "keep_v2_4_and_continue_shadow"
    return {
        "status": status,
        "metric_name": "account_normalized_visible_engagement_balanced_pairwise_accuracy",
        "primary_metric": "balanced_pairwise_accuracy",
        "metric_semantics": (
            "Account-normalized lifetime-at-capture visible interaction proxy; "
            "the objective gate uses the score sign while review-only ties remain "
            "separate, then macro-averages observed outcome sides; views, exposure, "
            "and follow conversion are unavailable."
        ),
        "evaluable_pair_count": pair_count,
        "required_pair_count": OUTCOME_PROXY_MIN_PAIR_COUNT,
        "required_outcome_choice_count": 2,
        "outcome_choice_distribution": dict(sorted(outcome_choice_counts.items())),
        "cloud_review_tie_count": sum(
            item.get("cloud_choice") == "tie" for item in comparable
        ),
        "cloud_outcome_forced_from_review_tie_count": sum(
            item.get("cloud_choice") == "tie"
            and item.get("cloud_outcome_choice") in {"left", "right"}
            for item in comparable
        ),
        "majority_baseline_accuracy": round(
            max(outcome_choice_counts.values(), default=0) / pair_count if pair_count else 0.0,
            4,
        ),
        "cloud_correct_count": cloud_correct,
        "v2_4_correct_count": v24_correct,
        "cloud_pairwise_accuracy": round(cloud_accuracy, 4),
        "v2_4_pairwise_accuracy": round(v24_accuracy, 4),
        "cloud_balanced_pairwise_accuracy": round(cloud_balanced_accuracy, 4),
        "v2_4_balanced_pairwise_accuracy": round(v24_balanced_accuracy, 4),
        "accuracy_delta_vs_v2_4": round(accuracy_delta, 4),
        "raw_accuracy_delta_vs_v2_4": round(raw_accuracy_delta, 4),
        "required_accuracy_delta": OUTCOME_PROXY_REQUIRED_DELTA,
        "early_stop_pair_count": OUTCOME_PROXY_EARLY_STOP_PAIR_COUNT,
        "early_stop_delta": OUTCOME_PROXY_EARLY_STOP_DELTA,
        "early_stop": early_stop,
        "passed": passed,
        "automatic_promotion": False,
        "views_available": False,
        "exposure_available": False,
        "follow_conversion_available": False,
        "writes_production": False,
        "decision": decision,
    }


def _judge_comparison(selected: list[dict], results_by_model: dict[str, list[dict]]) -> dict:
    primary = {str(item.get("task_id") or ""): item for item in results_by_model.get(BAILIAN_PRIMARY_JUDGE_MODEL, [])}
    challenger = {str(item.get("task_id") or ""): item for item in results_by_model.get(BAILIAN_CHALLENGER_JUDGE_MODEL, [])}
    comparable = 0
    agreements = 0
    for item in selected:
        task_id = str(item.get("task_id") or "")
        left = (primary.get(task_id) or {}).get("output") or {}
        right = (challenger.get(task_id) or {}).get("output") or {}
        left_choice = str(left.get("choice") or "")
        right_choice = str(right.get("choice") or "")
        if left_choice and right_choice:
            comparable += 1
            agreements += int(left_choice == right_choice)
    return {
        "comparable_count": comparable,
        "judge_agreement_rate": round(agreements / max(1, comparable), 4),
        "human_gold_comparison_available": False,
        "note": "Judge output remains research evidence and never writes manual Gold.",
    }


def _execution_plan(manifest: dict) -> dict:
    sample_count = len(_all_sample_ids(manifest))
    evaluation_count = len(_evaluation_ids(manifest))
    visual_bytes = 0
    for sample in _manifest_samples(manifest).values():
        media = sample.get("media") if isinstance(sample.get("media"), dict) else {}
        visual_bytes += sum(
            int(item.get("size_bytes") or 0)
            for item in (media.get("visual_sources") or [])[:3]
            if isinstance(item, dict)
        )
    return {
        "sample_count": sample_count,
        "embedding_request_count": sample_count * len(CLOUD_VECTOR_MODALITIES),
        "rerank_request_count": evaluation_count,
        "maximum_judge_request_count": MAX_JUDGE_LIMIT * 2,
        "representative_frame_bytes": visual_bytes,
        "full_video_uploaded": False,
        "recommended_first_run": {"stage": "smoke", "limit": 10, "top_n": 10, "judge_limit": 5},
    }


def _persist_stage_report(manifest: dict, stage: str, report: dict) -> None:
    root = _report_root(str(manifest["benchmark_id"]))
    timestamp = re.sub(r"[^0-9]", "", str(report.get("generated_at") or ""))[:14] or "latest"
    write_json(root / f"{stage}-{timestamp}.json", report)
    write_json(_stage_report_path(str(manifest["benchmark_id"]), stage), report)


def _load_stage_report(benchmark_id: str, stage: str) -> dict | None:
    path = _stage_report_path(benchmark_id, stage)
    return read_json(path, default=None) if path.is_file() else None


def _report_root(benchmark_id: str) -> Path:
    return ensure_data_dirs().root / "outputs" / "bailian_vector_chain" / benchmark_id


def _stage_report_path(benchmark_id: str, stage: str) -> Path:
    return _report_root(benchmark_id) / f"{stage}-latest.json"


def _local_vector_report(benchmark_id: str) -> tuple[dict, str]:
    root = ensure_data_dirs().root
    candidates = (
        (
            "generated_report",
            root / "outputs" / "multimodal_vector_value" / benchmark_id / "latest.json",
        ),
        ("frozen_sidecar", root / "benchmarks" / f"{benchmark_id}.baseline.json"),
    )
    for source, path in candidates:
        if path.is_file():
            return read_json(path, default={}), source
    return {}, "missing"


def _safe_name(value: str) -> str:
    normalized = _SAFE_NAME.sub("_", str(value or "")).strip("._")
    return normalized[:120] or "item"
