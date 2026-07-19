from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Any

from dso.config import ensure_data_dirs
from dso.learning.bailian_cached_ablation import (
    _balanced_accuracy,
    _evaluation_rows,
    _pair_deltas,
    _vectors_for_modality,
)
from dso.learning.bailian_holdout_validation import (
    _assert_blind_payload,
    _verify_frozen_config,
)
from dso.learning.bailian_vector_chain import (
    _cloud_records,
    _cosine,
    _load_stage_report,
    _manifest_samples,
    _persist_stage_report,
)
from dso.learning.multimodal_vector_value import (
    DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    load_multimodal_vector_manifest,
)
from dso.providers.contracts import stable_json_sha256
from dso.utils import utc_now


BAILIAN_FAILURE_ATTRIBUTION_VERSION = "bailian_holdout_failure_attribution.v1"
ATTRIBUTION_STAGE = "holdout-failure-attribution"
WEIGHT_GRID = (0.0, 0.15, 0.25, 0.5, 0.75, 1.0)
_TITLE_CLEANUP = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def run_bailian_holdout_failure_attribution(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    """Explain D12-B failures using frozen artifacts and local caches only."""

    manifest = load_multimodal_vector_manifest(benchmark_id)
    config = _required_stage(manifest, "holdout-config")
    predictions = _required_stage(manifest, "holdout-predictions")
    evaluation = _required_stage(manifest, "holdout-evaluation")
    rerank = _required_stage(manifest, "holdout-rerank")
    integrity = _verify_artifacts(manifest, config, predictions, evaluation)

    samples = _manifest_samples(manifest)
    references = [str(value) for value in config.get("reference_sample_ids") or []]
    prediction_rows = {
        str(item.get("task_id") or ""): item
        for item in predictions.get("predictions") or []
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    contexts = [
        item
        for item in evaluation.get("holdout_diagnostics") or []
        if isinstance(item, dict) and str(item.get("task_id") or "") in prediction_rows
    ]
    if len(contexts) != int(config.get("split_policy", {}).get("holdout_pair_count") or 0):
        raise ValueError("D12-B attribution requires complete holdout diagnostics")

    holdout_ids = sorted(
        {
            str(row.get(key) or "")
            for row in prediction_rows.values()
            for key in ("left_sample_id", "right_sample_id")
            if str(row.get(key) or "")
        }
    )
    records = _cloud_records([*holdout_ids, *references])
    text_vectors = _vectors_for_modality(records, "text")
    fusion_vectors = _vectors_for_modality(records, "fusion")
    text_profiles = _retrieval_profiles(
        holdout_ids, references, samples, text_vectors, neighbors_per_label=3
    )
    fusion_profiles = _retrieval_profiles(
        holdout_ids, references, samples, fusion_vectors, neighbors_per_label=3
    )
    rerank_profiles = _rerank_profiles(holdout_ids, rerank.get("items"), samples)

    scales = {
        key: float(value)
        for key, value in (config.get("normalization_scales") or {}).items()
    }
    pair_rows = _pair_attribution_rows(
        contexts,
        prediction_rows,
        text_profiles,
        fusion_profiles,
        scales,
    )
    component_comparison = _component_comparison(
        contexts, prediction_rows, fusion_profiles
    )
    decision_dynamics = _decision_dynamics(pair_rows, scales)
    retrieval_diagnostics = {
        "scope": "fixed_global_balanced_reference_pool",
        "reference_pool": _reference_pool_summary(references, samples),
        "text": _retrieval_summary(text_profiles, samples),
        "fusion": _retrieval_summary(fusion_profiles, samples),
        "rerank": _retrieval_summary(rerank_profiles, samples),
    }
    modality_diagnostics = _modality_diagnostics(
        contexts,
        text_vectors,
        fusion_vectors,
        text_profiles,
        fusion_profiles,
        samples,
    )
    failure_counts = dict(Counter(str(row["failure_type"]) for row in pair_rows))
    root_causes = _root_causes(
        component_comparison,
        decision_dynamics,
        retrieval_diagnostics,
        modality_diagnostics,
    )

    core = {
        "contract_version": BAILIAN_FAILURE_ATTRIBUTION_VERSION,
        "status": "ready",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "source_artifacts": integrity,
        "analysis_scope": {
            "pair_count": len(pair_rows),
            "sample_count": len(holdout_ids),
            "reference_count": len(references),
            "posthoc_diagnostic_only": True,
            "weight_search_forbidden": True,
        },
        "failure_counts": failure_counts,
        "component_comparison": component_comparison,
        "decision_dynamics": decision_dynamics,
        "retrieval_diagnostics": retrieval_diagnostics,
        "modality_diagnostics": modality_diagnostics,
        "root_causes": root_causes,
        "pair_diagnostics": sorted(
            pair_rows,
            key=lambda row: (
                _failure_priority(str(row["failure_type"])),
                str(row["task_id"]),
            ),
        ),
        "decision": "keep_v2_4_and_redesign_evidence_before_new_holdout",
        "network_request_count": 0,
        "effective_cost_cny": "0",
        "cache_only": True,
        "production_weight_changed": False,
        "writes_manual_gold": False,
        "automatic_publish": False,
    }
    report_sha256 = stable_json_sha256(core)
    report = {**core, "report_sha256": report_sha256, "generated_at": utc_now()}
    existing = _load_stage_report(str(manifest["benchmark_id"]), ATTRIBUTION_STAGE) or {}
    if existing:
        if str(existing.get("report_sha256") or "") != report_sha256:
            raise ValueError(
                "D12-C0 attribution already exists with different inputs; create a new contract version"
            )
        return {**existing, "reused": True}
    _persist_stage_report(manifest, ATTRIBUTION_STAGE, report)
    return report


def bailian_failure_attribution_status(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    report = _load_stage_report(benchmark_id, ATTRIBUTION_STAGE) or {}
    if not report:
        return {
            "status": "not_run",
            "contract_version": BAILIAN_FAILURE_ATTRIBUTION_VERSION,
            "network_request_count": 0,
            "effective_cost_cny": "0",
        }
    return {
        key: report.get(key)
        for key in (
            "status",
            "contract_version",
            "report_sha256",
            "analysis_scope",
            "failure_counts",
            "component_comparison",
            "decision_dynamics",
            "retrieval_diagnostics",
            "modality_diagnostics",
            "root_causes",
            "decision",
            "network_request_count",
            "effective_cost_cny",
            "cache_only",
            "generated_at",
        )
    }


def _verify_artifacts(
    manifest: dict, config: dict, predictions: dict, evaluation: dict
) -> dict:
    _verify_frozen_config(config)
    if str(predictions.get("config_sha256") or "") != str(config.get("config_sha256") or ""):
        raise ValueError("D12-C0 prediction/config checksum mismatch")
    prediction_core = {
        key: value
        for key, value in predictions.items()
        if key not in {"prediction_sha256", "generated_at", "reused"}
    }
    prediction_sha = stable_json_sha256(prediction_core)
    if prediction_sha != str(predictions.get("prediction_sha256") or ""):
        raise ValueError("D12-C0 prediction checksum mismatch")
    _assert_blind_payload(prediction_core)
    if str(evaluation.get("prediction_sha256") or "") != prediction_sha:
        raise ValueError("D12-C0 evaluation/prediction checksum mismatch")
    evaluation_core = {
        key: value
        for key, value in evaluation.items()
        if key not in {"evaluation_sha256", "generated_at", "reused"}
    }
    evaluation_sha = stable_json_sha256(evaluation_core)
    if evaluation_sha != str(evaluation.get("evaluation_sha256") or ""):
        raise ValueError("D12-C0 evaluation checksum mismatch")
    return {
        "config_sha256": config["config_sha256"],
        "prediction_sha256": prediction_sha,
        "evaluation_sha256": evaluation_sha,
        "manifest_sha256": manifest["manifest_sha256"],
        "blind_prediction_verified": True,
    }


def _pair_attribution_rows(
    contexts: list[dict],
    predictions: dict[str, dict],
    text_profiles: dict[str, dict],
    fusion_profiles: dict[str, dict],
    scales: dict[str, float],
) -> list[dict]:
    result = []
    for context in contexts:
        task_id = str(context["task_id"])
        prediction = predictions[task_id]
        baseline_delta = float(prediction.get("v2_4_delta") or 0.0)
        cloud_delta = float(prediction.get("cloud_delta") or 0.0)
        final_delta = float(prediction.get("final_delta") or 0.0)
        baseline_choice = _choice(baseline_delta)
        cloud_choice = _choice(cloud_delta)
        final_choice = _choice(final_delta)
        outcome = str(context.get("outcome_choice") or "tie")
        baseline_correct = baseline_choice == outcome
        cloud_correct = cloud_choice == outcome
        final_correct = final_choice == outcome
        left_id = str(prediction["left_sample_id"])
        right_id = str(prediction["right_sample_id"])
        fusion_delta = _profile_delta(left_id, right_id, fusion_profiles)
        flip_weight = _flip_weight(
            baseline_delta / max(float(scales.get("v2_4") or 1.0), 1e-9),
            cloud_delta / max(float(scales.get("cloud") or 1.0), 1e-9),
        )
        if not baseline_correct and cloud_correct:
            failure_type = (
                "cloud_correct_but_suppressed"
                if final_choice == baseline_choice
                else "cloud_correct_and_flipped"
            )
        elif baseline_correct and not cloud_correct:
            failure_type = (
                "cloud_wrong_but_suppressed"
                if final_choice == baseline_choice
                else "cloud_wrong_and_regressed"
            )
        elif not baseline_correct and not cloud_correct:
            failure_type = "shared_failure"
        else:
            failure_type = "shared_success"
        result.append(
            {
                "task_id": task_id,
                "account_id": context.get("account_id") or "unknown",
                "content_category": context.get("content_category") or "unknown",
                "left_sample_id": left_id,
                "right_sample_id": right_id,
                "outcome_choice": outcome,
                "v2_4_choice": baseline_choice,
                "cloud_choice": cloud_choice,
                "final_choice": final_choice,
                "v2_4_correct": baseline_correct,
                "cloud_correct": cloud_correct,
                "final_correct": final_correct,
                "v2_4_delta": round(baseline_delta, 8),
                "text_delta": round(float(prediction.get("embedding_delta") or 0.0), 8),
                "fusion_delta": round(fusion_delta, 8) if fusion_delta is not None else None,
                "rerank_delta": round(float(prediction.get("rerank_delta") or 0.0), 8),
                "cloud_delta": round(cloud_delta, 8),
                "final_delta": round(final_delta, 8),
                "minimum_cloud_weight_to_flip": (
                    round(flip_weight, 4) if flip_weight is not None else None
                ),
                "text_top1_labels": [
                    (text_profiles.get(sample_id) or {}).get("top1_label")
                    for sample_id in (left_id, right_id)
                ],
                "fusion_top1_labels": [
                    (fusion_profiles.get(sample_id) or {}).get("top1_label")
                    for sample_id in (left_id, right_id)
                ],
                "failure_type": failure_type,
            }
        )
    return result


def _component_comparison(
    contexts: list[dict], predictions: dict[str, dict], fusion_profiles: dict[str, dict]
) -> dict:
    fields = {
        "v2_4": "v2_4_delta",
        "text_embedding": "embedding_delta",
        "rerank": "rerank_delta",
        "cloud_50_50": "cloud_delta",
        "fixed_final_85_15": "final_delta",
    }
    result = {
        name: _component_metrics(
            contexts,
            {
                task_id: float(row.get(field) or 0.0)
                for task_id, row in predictions.items()
            },
        )
        for name, field in fields.items()
    }
    fusion_deltas = {
        str(context["task_id"]): value
        for context in contexts
        if (
            value := _profile_delta(
                str(predictions[str(context["task_id"])]["left_sample_id"]),
                str(predictions[str(context["task_id"])]["right_sample_id"]),
                fusion_profiles,
            )
        )
        is not None
    }
    result["fusion_embedding"] = _component_metrics(contexts, fusion_deltas)
    return result


def _component_metrics(contexts: list[dict], deltas: dict[str, float]) -> dict:
    rows = _evaluation_rows(contexts, deltas)
    count = len(rows)
    return {
        "pair_count": count,
        "balanced_accuracy": round(_balanced_accuracy(rows, "cloud_correct"), 4),
        "raw_accuracy": round(
            sum(bool(row["cloud_correct"]) for row in rows) / count if count else 0.0,
            4,
        ),
        "choice_distribution": dict(Counter(str(row["cloud_choice"]) for row in rows)),
    }


def _decision_dynamics(pair_rows: list[dict], scales: dict[str, float]) -> dict:
    baseline_scale = max(float(scales.get("v2_4") or 1.0), 1e-9)
    cloud_scale = max(float(scales.get("cloud") or 1.0), 1e-9)
    grid = []
    for weight in WEIGHT_GRID:
        correct = 0
        changed = 0
        for row in pair_rows:
            delta = (1.0 - weight) * float(row["v2_4_delta"]) / baseline_scale
            delta += weight * float(row["cloud_delta"]) / cloud_scale
            choice = _choice(delta)
            correct += choice == row["outcome_choice"]
            changed += choice != row["v2_4_choice"]
        grid.append(
            {
                "cloud_weight": weight,
                "raw_accuracy": round(correct / len(pair_rows), 4) if pair_rows else 0.0,
                "choice_change_count": changed,
                "posthoc_only": True,
            }
        )
    flip_rows = [
        row
        for row in pair_rows
        if isinstance(row.get("minimum_cloud_weight_to_flip"), (int, float))
    ]
    flip_weights = [float(row["minimum_cloud_weight_to_flip"]) for row in flip_rows]
    return {
        "fixed_cloud_weight": 0.15,
        "fixed_choice_change_count": sum(
            row["final_choice"] != row["v2_4_choice"] for row in pair_rows
        ),
        "cloud_correct_baseline_wrong_count": sum(
            row["cloud_correct"] and not row["v2_4_correct"] for row in pair_rows
        ),
        "cloud_wrong_baseline_correct_count": sum(
            not row["cloud_correct"] and row["v2_4_correct"] for row in pair_rows
        ),
        "shared_failure_count": sum(
            not row["cloud_correct"] and not row["v2_4_correct"] for row in pair_rows
        ),
        "conflicting_choice_count": len(flip_rows),
        "median_cloud_weight_to_flip": round(median(flip_weights), 4) if flip_weights else None,
        "posthoc_weight_grid": grid,
        "weight_grid_is_not_a_tuning_result": True,
    }


def _retrieval_profiles(
    sample_ids: list[str],
    reference_ids: list[str],
    samples: dict[str, dict],
    vectors: dict[str, list[float]],
    *,
    neighbors_per_label: int,
) -> dict[str, dict]:
    profiles = {}
    for sample_id in sample_ids:
        query = vectors.get(sample_id)
        if not query:
            continue
        matches = []
        for reference_id in reference_ids:
            reference = vectors.get(reference_id)
            if not reference:
                continue
            reference_sample = samples.get(reference_id) or {}
            matches.append(
                {
                    "sample_id": reference_id,
                    "label": str(reference_sample.get("performance_label") or "unknown"),
                    "account_id": str(reference_sample.get("account_id") or "unknown"),
                    "similarity": _cosine(query, reference),
                    "title_overlap": _title_overlap(
                        samples.get(sample_id) or {}, reference_sample
                    ),
                    "category_match": _semantic_value(samples.get(sample_id), "content_category")
                    == _semantic_value(reference_sample, "content_category"),
                }
            )
        matches.sort(key=lambda item: (-float(item["similarity"]), item["sample_id"]))
        high = [item for item in matches if item["label"] == "high"][:neighbors_per_label]
        low = [item for item in matches if item["label"] == "low"][:neighbors_per_label]
        if not high or not low:
            continue
        top = matches[: max(6, neighbors_per_label * 2)]
        query_sample = samples.get(sample_id) or {}
        profiles[sample_id] = {
            "score": mean(float(item["similarity"]) for item in high)
            - mean(float(item["similarity"]) for item in low),
            "top1_label": top[0]["label"] if top else "unknown",
            "top1_category_match": bool(top and top[0]["category_match"]),
            "top1_same_account": bool(
                top and top[0]["account_id"] == str(query_sample.get("account_id") or "")
            ),
            "same_account_reference_available": any(
                item["account_id"] == str(query_sample.get("account_id") or "")
                for item in matches
            ),
            "max_title_overlap": max(
                (float(item["title_overlap"]) for item in top), default=0.0
            ),
            "exact_title_match": any(float(item["title_overlap"]) >= 1.0 for item in top),
            "top_matches": top,
        }
    return profiles


def _rerank_profiles(
    sample_ids: list[str], items: Any, samples: dict[str, dict]
) -> dict[str, dict]:
    wanted = set(sample_ids)
    result = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sample_id = str(item.get("sample_id") or "")
        if sample_id not in wanted:
            continue
        query_sample = samples.get(sample_id) or {}
        top = []
        for match in item.get("top_matches") or []:
            if not isinstance(match, dict):
                continue
            reference = samples.get(str(match.get("sample_id") or "")) or {}
            top.append(
                {
                    "sample_id": str(match.get("sample_id") or ""),
                    "label": str(reference.get("performance_label") or "unknown"),
                    "account_id": str(reference.get("account_id") or "unknown"),
                    "similarity": float(match.get("relevance_score") or 0.0),
                    "title_overlap": _title_overlap(query_sample, reference),
                    "category_match": _semantic_value(query_sample, "content_category")
                    == _semantic_value(reference, "content_category"),
                }
            )
        if not top:
            continue
        result[sample_id] = {
            "score": float(item.get("score") or 0.0),
            "top1_label": top[0]["label"],
            "top1_category_match": bool(top[0]["category_match"]),
            "top1_same_account": top[0]["account_id"]
            == str(query_sample.get("account_id") or ""),
            "same_account_reference_available": any(
                match["account_id"] == str(query_sample.get("account_id") or "")
                for match in top
            ),
            "max_title_overlap": max(float(match["title_overlap"]) for match in top),
            "exact_title_match": any(float(match["title_overlap"]) >= 1.0 for match in top),
            "top_matches": top,
        }
    return result


def _retrieval_summary(profiles: dict[str, dict], samples: dict[str, dict]) -> dict:
    eligible = [
        (sample_id, profile)
        for sample_id, profile in profiles.items()
        if str((samples.get(sample_id) or {}).get("performance_label") or "") in {"high", "low"}
    ]
    return {
        "sample_count": len(profiles),
        "high_low_labeled_sample_count": len(eligible),
        "top1_performance_label_accuracy": round(
            sum(
                profile["top1_label"]
                == str((samples.get(sample_id) or {}).get("performance_label") or "")
                for sample_id, profile in eligible
            )
            / len(eligible)
            if eligible
            else 0.0,
            4,
        ),
        "top1_content_category_match_rate": round(
            mean(bool(profile["top1_category_match"]) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "same_account_reference_coverage": round(
            mean(bool(profile["same_account_reference_available"]) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "top1_same_account_rate": round(
            mean(bool(profile["top1_same_account"]) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "mean_max_title_overlap": round(
            mean(float(profile["max_title_overlap"]) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "exact_title_match_count": sum(
            bool(profile["exact_title_match"]) for profile in profiles.values()
        ),
        "mean_absolute_evidence_margin": round(
            mean(abs(float(profile["score"])) for profile in profiles.values())
            if profiles
            else 0.0,
            6,
        ),
    }


def _reference_pool_summary(reference_ids: list[str], samples: dict[str, dict]) -> dict:
    selected = [samples.get(sample_id) or {} for sample_id in reference_ids]
    return {
        "count": len(reference_ids),
        "label_distribution": dict(
            Counter(str(sample.get("performance_label") or "unknown") for sample in selected)
        ),
        "account_distribution": dict(
            sorted(
                Counter(str(sample.get("account_id") or "unknown") for sample in selected).items()
            )
        ),
        "content_category_distribution": dict(
            Counter(_semantic_value(sample, "content_category") for sample in selected)
        ),
        "unique_account_count": len(
            {str(sample.get("account_id") or "unknown") for sample in selected}
        ),
    }


def _modality_diagnostics(
    contexts: list[dict],
    text_vectors: dict[str, list[float]],
    fusion_vectors: dict[str, list[float]],
    text_profiles: dict[str, dict],
    fusion_profiles: dict[str, dict],
    samples: dict[str, dict],
) -> dict:
    sample_ids = sorted(set(text_profiles) & set(fusion_profiles))
    vector_cosines = [
        _cosine(text_vectors[sample_id], fusion_vectors[sample_id])
        for sample_id in sample_ids
        if sample_id in text_vectors and sample_id in fusion_vectors
    ]
    text_deltas = _pair_deltas(contexts, {key: value["score"] for key, value in text_profiles.items()})
    fusion_deltas = _pair_deltas(
        contexts, {key: value["score"] for key, value in fusion_profiles.items()}
    )
    common_tasks = sorted(set(text_deltas) & set(fusion_deltas))
    payload = _visual_payload_summary(sample_ids, samples)
    return {
        "text_fusion_ready_sample_count": len(sample_ids),
        "mean_text_fusion_vector_cosine": round(mean(vector_cosines), 6)
        if vector_cosines
        else 0.0,
        "pair_choice_change_text_to_fusion_count": sum(
            _choice(text_deltas[task_id]) != _choice(fusion_deltas[task_id])
            for task_id in common_tasks
        ),
        "top1_reference_label_change_count": sum(
            text_profiles[sample_id]["top1_label"]
            != fusion_profiles[sample_id]["top1_label"]
            for sample_id in sample_ids
        ),
        "visual_payload": payload,
    }


def _visual_payload_summary(sample_ids: list[str], samples: dict[str, dict]) -> dict:
    root = ensure_data_dirs().root.resolve()
    source_counts = []
    temporal_counts = []
    missing_file_samples = 0
    duplicate_hash_samples = 0
    for sample_id in sample_ids:
        sample = samples.get(sample_id) or {}
        media = sample.get("media") if isinstance(sample.get("media"), dict) else {}
        sources = media.get("visual_sources") if isinstance(media.get("visual_sources"), list) else []
        valid_sources = [item for item in sources if isinstance(item, dict)]
        source_counts.append(len(valid_sources))
        temporal_counts.append(
            sum("/frames/" in str(item.get("path") or "").replace("\\", "/") for item in valid_sources)
        )
        hashes = [str(item.get("sha256") or "") for item in valid_sources if item.get("sha256")]
        duplicate_hash_samples += bool(hashes and len(set(hashes)) < len(hashes))
        missing_file_samples += any(
            not (root / str(item.get("path") or "")).is_file() for item in valid_sources
        )
    return {
        "sample_count": len(sample_ids),
        "source_count_distribution": {
            str(key): value for key, value in sorted(Counter(source_counts).items())
        },
        "mean_visual_source_count": round(mean(source_counts), 3) if source_counts else 0.0,
        "less_than_three_sources_count": sum(value < 3 for value in source_counts),
        "one_or_fewer_temporal_frames_count": sum(value <= 1 for value in temporal_counts),
        "missing_local_file_sample_count": missing_file_samples,
        "duplicate_visual_hash_sample_count": duplicate_hash_samples,
    }


def _root_causes(
    components: dict,
    decisions: dict,
    retrieval: dict,
    modality: dict,
) -> list[dict]:
    baseline = float(components.get("v2_4", {}).get("balanced_accuracy") or 0.0)
    cloud = float(components.get("cloud_50_50", {}).get("balanced_accuracy") or 0.0)
    text = retrieval.get("text") or {}
    visual = modality.get("visual_payload") or {}
    sample_count = max(int(visual.get("sample_count") or 0), 1)
    causes = []
    if cloud <= baseline:
        causes.append(
            {
                "rank": 1,
                "cause": "cloud_signal_not_outcome_aligned",
                "severity": "primary",
                "evidence": f"cloud_balanced={cloud:.4f}, v2_4_balanced={baseline:.4f}",
                "action": "do_not_raise_cloud_weight_or_expand_judge",
            }
        )
    if float(text.get("top1_content_category_match_rate") or 0.0) > float(
        text.get("top1_performance_label_accuracy") or 0.0
    ):
        causes.append(
            {
                "rank": len(causes) + 1,
                "cause": "retrieval_clusters_semantics_more_than_outcomes",
                "severity": "high",
                "evidence": (
                    f"category_match={float(text.get('top1_content_category_match_rate') or 0):.4f}, "
                    f"label_match={float(text.get('top1_performance_label_accuracy') or 0):.4f}"
                ),
                "action": "redesign_reference_objective_and_account_conditioning",
            }
        )
    if int(visual.get("less_than_three_sources_count") or 0) / sample_count >= 0.5:
        causes.append(
            {
                "rank": len(causes) + 1,
                "cause": "visual_payload_has_insufficient_temporal_coverage",
                "severity": "high",
                "evidence": (
                    f"lt3_sources={int(visual.get('less_than_three_sources_count') or 0)}/"
                    f"{sample_count}"
                ),
                "action": "use_true_hook_middle_payoff_frames_before_retesting_fusion",
            }
        )
    if (
        float(text.get("same_account_reference_coverage") or 0.0) <= 0.5
        or float(text.get("top1_same_account_rate") or 0.0) < 0.25
    ):
        causes.append(
            {
                "rank": len(causes) + 1,
                "cause": "global_reference_pool_lacks_account_context",
                "severity": "medium",
                "evidence": (
                    f"same_account_coverage={float(text.get('same_account_reference_coverage') or 0):.4f}, "
                    f"top1_same_account={float(text.get('top1_same_account_rate') or 0):.4f}"
                ),
                "action": "build_account_or_program_stratified_references",
            }
        )
    if int(decisions.get("fixed_choice_change_count") or 0) == 0:
        causes.append(
            {
                "rank": len(causes) + 1,
                "cause": "fixed_fusion_is_decision_inactive",
                "severity": "diagnostic",
                "evidence": "fixed_choice_change_count=0",
                "action": "improve_signal_quality_before_any_new_weight_gate",
            }
        )
    return causes


def _required_stage(manifest: dict, stage: str) -> dict:
    report = _load_stage_report(str(manifest["benchmark_id"]), stage) or {}
    if not report:
        raise ValueError(f"D12-C0 requires {stage}")
    if str(report.get("manifest_sha256") or "") != str(manifest.get("manifest_sha256") or ""):
        raise ValueError(f"D12-C0 {stage} manifest mismatch")
    return report


def _profile_delta(left_id: str, right_id: str, profiles: dict[str, dict]) -> float | None:
    if left_id not in profiles or right_id not in profiles:
        return None
    return float(profiles[left_id]["score"]) - float(profiles[right_id]["score"])


def _choice(delta: float) -> str:
    return "left" if delta > 0 else "right" if delta < 0 else "tie"


def _flip_weight(baseline: float, cloud: float) -> float | None:
    if baseline == 0 or cloud == 0 or math.copysign(1.0, baseline) == math.copysign(1.0, cloud):
        return None
    denominator = cloud - baseline
    if denominator == 0:
        return None
    weight = -baseline / denominator
    return weight if 0.0 < weight <= 1.0 else None


def _semantic_value(sample: dict | None, key: str) -> str:
    value = sample.get("semantic") if isinstance(sample, dict) else {}
    semantic = value if isinstance(value, dict) else {}
    return str(semantic.get(key) or "unknown")


def _title_overlap(left: dict, right: dict) -> float:
    left_key = _TITLE_CLEANUP.sub("", str(left.get("stable_title_key") or left.get("title") or "").lower())
    right_key = _TITLE_CLEANUP.sub("", str(right.get("stable_title_key") or right.get("title") or "").lower())
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    left_grams = _ngrams(left_key, 3)
    right_grams = _ngrams(right_key, 3)
    union = left_grams | right_grams
    return len(left_grams & right_grams) / len(union) if union else 0.0


def _ngrams(value: str, size: int) -> set[str]:
    if len(value) <= size:
        return {value}
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def _failure_priority(value: str) -> int:
    return {
        "cloud_correct_but_suppressed": 0,
        "shared_failure": 1,
        "cloud_wrong_but_suppressed": 2,
        "cloud_wrong_and_regressed": 3,
        "cloud_correct_and_flipped": 4,
        "shared_success": 5,
    }.get(value, 6)
