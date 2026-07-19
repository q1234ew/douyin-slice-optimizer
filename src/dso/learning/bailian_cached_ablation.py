from __future__ import annotations

import hashlib
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from dso.learning.bailian_vector_chain import (
    BAILIAN_VECTOR_CHAIN_VERSION,
    CLOUD_EMBEDDING_MODEL_NAME,
    _all_sample_ids,
    _cloud_records,
    _cosine,
    _load_stage_report,
    _local_vector_report,
    _manifest_samples,
    _persist_stage_report,
)
from dso.learning.multimodal_vector_value import (
    DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    load_multimodal_vector_manifest,
)
from dso.providers.aliyun_bailian import BAILIAN_DEFAULT_EMBEDDING_DIMENSION
from dso.utils import read_json, utc_now


BAILIAN_CACHED_ABLATION_VERSION = "bailian_cached_signal_ablation.v1"
DEFAULT_NEIGHBOR_COUNTS = (3, 5, 10)
DEFAULT_REFERENCE_POOL_SIZES = (10, 20, 40)
DEFAULT_CLOUD_FUSION_WEIGHTS = (0.25, 0.5, 0.75)
DEFAULT_V24_FUSION_WEIGHTS = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3)
MIN_EXPANSION_PAIR_COUNT = 40
MIN_EXPANSION_DELTA = 0.02
MIN_ACCOUNT_WINS = 3
MIN_CATEGORY_WINS = 2
MIN_GROUP_PAIR_COUNT = 3
BOOTSTRAP_ITERATIONS = 1000
_PAIR_SIDES = ("left", "right")


def run_bailian_cached_ablation(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    """Attribute cached cloud signals without constructing a network runtime."""

    manifest = load_multimodal_vector_manifest(benchmark_id)
    local_report, baseline_source = _local_vector_report(str(manifest["benchmark_id"]))
    _validate_baseline(manifest, local_report)
    samples = _manifest_samples(manifest)
    task_contexts = _task_contexts(manifest, local_report, samples)
    if not task_contexts:
        raise ValueError("cached ablation requires comparable frozen pair outcomes")

    sample_ids = _all_sample_ids(manifest)
    records = _cloud_records(sample_ids)
    vectors = {
        modality: _vectors_for_modality(records, modality)
        for modality in ("text", "fusion")
    }
    reference_ids = [str(value) for value in manifest.get("reference_sample_ids") or []]
    configurations: list[dict] = []
    pair_deltas: dict[str, dict[str, float]] = {}
    skipped: list[dict] = []

    for modality in ("text", "fusion"):
        modality_vectors = vectors[modality]
        for reference_pool_size in DEFAULT_REFERENCE_POOL_SIZES:
            selected_references = _balanced_references(
                reference_ids,
                samples,
                modality_vectors,
                reference_pool_size,
            )
            if len(selected_references) < reference_pool_size:
                skipped.append(
                    {
                        "component": f"{modality}_embedding",
                        "reference_pool_size": reference_pool_size,
                        "reason": "insufficient_balanced_reference_cache",
                        "available_balanced_count": len(selected_references),
                    }
                )
                continue
            reference_vectors = {
                sample_id: modality_vectors[sample_id] for sample_id in selected_references
            }
            for neighbors_per_label in DEFAULT_NEIGHBOR_COUNTS:
                strategy = (
                    f"{modality}_cosine_ref{reference_pool_size}_k{neighbors_per_label}"
                )
                scores = _embedding_scores(
                    task_contexts,
                    samples,
                    modality_vectors,
                    reference_vectors,
                    neighbors_per_label=neighbors_per_label,
                )
                deltas = _pair_deltas(task_contexts, scores)
                pair_deltas[strategy] = deltas
                configurations.append(
                    _configuration_summary(
                        strategy,
                        component=f"{modality}_embedding",
                        task_contexts=task_contexts,
                        deltas=deltas,
                        manifest_sha256=str(manifest["manifest_sha256"]),
                        parameters={
                            "modality": modality,
                            "reference_pool_size": reference_pool_size,
                            "neighbors_per_label": neighbors_per_label,
                            "retrieval_policy": "balanced_high_low_cosine",
                        },
                    )
                )

    rerank_report = _load_stage_report(str(manifest["benchmark_id"]), "rerank") or {}
    if _report_matches_manifest(manifest, rerank_report):
        rerank_scores = {
            str(item.get("sample_id") or ""): float(item.get("score") or 0.0)
            for item in rerank_report.get("items") or []
            if isinstance(item, dict) and str(item.get("sample_id") or "")
        }
        rerank_deltas = _pair_deltas(task_contexts, rerank_scores)
        if rerank_deltas:
            strategy = "cached_rerank_current"
            pair_deltas[strategy] = rerank_deltas
            configurations.append(
                _configuration_summary(
                    strategy,
                    component="cached_rerank",
                    task_contexts=task_contexts,
                    deltas=rerank_deltas,
                    manifest_sha256=str(manifest["manifest_sha256"]),
                    parameters={
                        "top_n": int(rerank_report.get("top_n") or 0),
                        "source": "rerank-latest.json",
                    },
                )
            )
    else:
        skipped.append(
            {
                "component": "cached_rerank",
                "reason": "compatible_rerank_report_missing",
            }
        )

    best_embedding = _best_configuration(
        [row for row in configurations if str(row.get("component") or "").endswith("_embedding")]
    )
    if best_embedding and "cached_rerank_current" in pair_deltas:
        embedding_strategy = str(best_embedding["strategy"])
        for weight in DEFAULT_CLOUD_FUSION_WEIGHTS:
            strategy = f"embedding_rerank_w{int(weight * 100):02d}"
            deltas = _blend_deltas(
                pair_deltas[embedding_strategy],
                pair_deltas["cached_rerank_current"],
                right_weight=weight,
            )
            pair_deltas[strategy] = deltas
            configurations.append(
                _configuration_summary(
                    strategy,
                    component="embedding_plus_cached_rerank",
                    task_contexts=task_contexts,
                    deltas=deltas,
                    manifest_sha256=str(manifest["manifest_sha256"]),
                    parameters={
                        "embedding_strategy": embedding_strategy,
                        "rerank_weight": weight,
                    },
                )
            )

    baseline_deltas = {
        str(context["task_id"]): float(context["v2_4_delta"])
        for context in task_contexts
    }
    baseline = _configuration_summary(
        "research_ranker_v2_4",
        component="baseline",
        task_contexts=task_contexts,
        deltas=baseline_deltas,
        manifest_sha256=str(manifest["manifest_sha256"]),
        parameters={"source": baseline_source},
    )

    cloud_candidates = sorted(
        configurations,
        key=_configuration_sort_key,
        reverse=True,
    )[:5]
    for candidate in cloud_candidates:
        cloud_strategy = str(candidate["strategy"])
        for cloud_weight in DEFAULT_V24_FUSION_WEIGHTS:
            strategy = f"v2_4_plus_{cloud_strategy}_w{int(cloud_weight * 100):02d}"
            deltas = _blend_deltas(
                baseline_deltas,
                pair_deltas[cloud_strategy],
                right_weight=cloud_weight,
            )
            pair_deltas[strategy] = deltas
            configurations.append(
                _configuration_summary(
                    strategy,
                    component="v2_4_plus_cloud",
                    task_contexts=task_contexts,
                    deltas=deltas,
                    manifest_sha256=str(manifest["manifest_sha256"]),
                    parameters={
                        "cloud_strategy": cloud_strategy,
                        "cloud_weight": cloud_weight,
                        "baseline_weight": round(1.0 - cloud_weight, 4),
                        "normalization": "median_absolute_pair_delta",
                    },
                )
            )

    best_cloud = _best_configuration(
        [row for row in configurations if row.get("component") != "v2_4_plus_cloud"]
    )
    best_fusion = _best_configuration(
        [row for row in configurations if row.get("component") == "v2_4_plus_cloud"]
    )
    best_incremental = _best_configuration(
        [row for row in configurations if row.get("component") != "baseline"]
    )
    if not best_incremental:
        raise ValueError("cached ablation found no evaluable cloud configuration")

    best_strategy = str(best_incremental["strategy"])
    best_rows = _evaluation_rows(task_contexts, pair_deltas[best_strategy])
    comparable_baseline_accuracy = float(
        best_incremental.get("v2_4_balanced_pairwise_accuracy") or 0.0
    )
    comparable_baseline_avoidance = float(
        best_incremental.get("v2_4_raw_pairwise_accuracy") or 0.0
    )
    account_metrics = _group_metrics(best_rows, "account_id")
    category_metrics = _group_metrics(best_rows, "content_category")
    gap_metrics = _group_metrics(best_rows, "outcome_gap_bucket")
    account_wins = sum(
        row["ready"] and float(row["accuracy_delta_vs_v2_4"]) > 0
        for row in account_metrics
    )
    category_wins = sum(
        row["ready"] and float(row["accuracy_delta_vs_v2_4"]) > 0
        for row in category_metrics
        if row["group"] != "mixed"
    )
    best_accuracy = float(best_incremental.get("balanced_pairwise_accuracy") or 0.0)
    best_avoidance = float(best_incremental.get("low_interaction_avoidance_rate") or 0.0)
    pair_count = int(best_incremental.get("evaluable_pair_count") or 0)
    gate_conditions = {
        "minimum_pair_count": pair_count >= MIN_EXPANSION_PAIR_COUNT,
        "accuracy_delta_at_least_2pp": (
            best_accuracy - comparable_baseline_accuracy >= MIN_EXPANSION_DELTA
        ),
        "low_interaction_avoidance_not_worse": (
            best_avoidance >= comparable_baseline_avoidance
        ),
        "at_least_three_ready_account_wins": account_wins >= MIN_ACCOUNT_WINS,
        "at_least_two_ready_category_wins": category_wins >= MIN_CATEGORY_WINS,
        "cache_only_zero_network": True,
    }
    expansion_eligible = all(gate_conditions.values())
    expansion_gate = {
        "status": "eligible_for_60_pair_expansion" if expansion_eligible else "keep_v2_4",
        "passed": expansion_eligible,
        "conditions": gate_conditions,
        "required_accuracy_delta": MIN_EXPANSION_DELTA,
        "actual_accuracy_delta": round(best_accuracy - comparable_baseline_accuracy, 4),
        "comparable_v2_4_balanced_accuracy": round(comparable_baseline_accuracy, 4),
        "comparable_v2_4_low_interaction_avoidance_rate": round(
            comparable_baseline_avoidance, 4
        ),
        "account_win_count": account_wins,
        "required_account_win_count": MIN_ACCOUNT_WINS,
        "category_win_count": category_wins,
        "required_category_win_count": MIN_CATEGORY_WINS,
        "recommended_online_batch_cap_cny": "5.00",
        "decision": (
            "freeze_new_60_pair_manifest_before_bounded_cloud_expansion"
            if expansion_eligible
            else "stop_cloud_expansion_and_keep_v2_4"
        ),
        "production_promotion": False,
    }

    comparison = sorted(configurations, key=_configuration_sort_key, reverse=True)
    report = {
        "contract_version": BAILIAN_CACHED_ABLATION_VERSION,
        "parent_contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "status": "ready" if pair_count >= MIN_EXPANSION_PAIR_COUNT else "insufficient_cache",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "evaluation_semantics": (
            "Account-normalized visible-engagement proxy; not views, exposure, or follow conversion."
        ),
        "selection_bias_notice": (
            "Weights and configurations are explored on the same frozen 40-pair cache. "
            "A new frozen expansion manifest is required before any promotion claim."
        ),
        "cache_policy": {
            "cache_only": True,
            "network_runtime_constructed": False,
            "network_request_count": 0,
            "effective_cost_cny": "0",
        },
        "coverage": {
            "sample_count": len(sample_ids),
            "text_ready_count": len(vectors["text"]),
            "fusion_ready_count": len(vectors["fusion"]),
            "text_fusion_ready_count": sum(
                sample_id in vectors["text"] and sample_id in vectors["fusion"]
                for sample_id in sample_ids
            ),
        },
        "search_space": {
            "neighbor_counts_per_label": list(DEFAULT_NEIGHBOR_COUNTS),
            "balanced_reference_pool_sizes": list(DEFAULT_REFERENCE_POOL_SIZES),
            "embedding_rerank_weights": list(DEFAULT_CLOUD_FUSION_WEIGHTS),
            "v2_4_cloud_weights": list(DEFAULT_V24_FUSION_WEIGHTS),
        },
        "baseline": baseline,
        "best_cloud_configuration": best_cloud,
        "best_fusion_configuration": best_fusion,
        "best_incremental_configuration": best_incremental,
        "configuration_comparison": comparison,
        "skipped_configurations": skipped,
        "per_account_metrics": account_metrics,
        "per_category_metrics": category_metrics,
        "outcome_gap_metrics": gap_metrics,
        "diagnostic_pairs": sorted(
            best_rows,
            key=lambda row: (
                row["cloud_correct"] == row["v2_4_correct"],
                -abs(float(row["cloud_delta"])),
                row["task_id"],
            ),
        )[:20],
        "expansion_gate": expansion_gate,
        "automatic_promotion": False,
        "writes_manual_gold": False,
        "production_weight_changed": False,
        "automatic_publish": False,
        "generated_at": utc_now(),
    }
    _persist_stage_report(manifest, "ablation", report)
    return report


def cached_ablation_public_summary(report: dict | None) -> dict:
    if not isinstance(report, dict) or not report:
        return {}
    comparison = report.get("configuration_comparison") or []
    return {
        key: report.get(key)
        for key in (
            "contract_version",
            "status",
            "admission_status",
            "benchmark_id",
            "manifest_sha256",
            "evaluation_semantics",
            "selection_bias_notice",
            "cache_policy",
            "coverage",
            "baseline",
            "best_cloud_configuration",
            "best_fusion_configuration",
            "best_incremental_configuration",
            "expansion_gate",
            "generated_at",
        )
    } | {"top_configurations": _diverse_top_configurations(comparison, limit=8)}


def _diverse_top_configurations(configurations: list[dict], *, limit: int) -> list[dict]:
    selected = []
    seen_components = set()
    for row in configurations:
        component = str(row.get("component") or "unknown")
        if component in seen_components:
            continue
        selected.append(row)
        seen_components.add(component)
        if len(selected) >= limit:
            return selected
    for row in configurations:
        if row in selected:
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _validate_baseline(manifest: dict, report: dict) -> None:
    if not report:
        raise ValueError("cached ablation requires a frozen v2.4 baseline report")
    if not _report_matches_manifest(manifest, report):
        raise ValueError("cached ablation baseline does not match the frozen manifest")
    if not isinstance(report.get("pair_results"), list):
        raise ValueError("cached ablation baseline pair results are missing")


def _report_matches_manifest(manifest: dict, report: dict) -> bool:
    return bool(report) and (
        str(report.get("benchmark_id") or "") == str(manifest.get("benchmark_id") or "")
        and str(report.get("manifest_sha256") or "")
        == str(manifest.get("manifest_sha256") or "")
    )


def _task_contexts(manifest: dict, local_report: dict, samples: dict[str, dict]) -> list[dict]:
    local_pairs = {
        str(item.get("task_id") or ""): item
        for item in local_report.get("pair_results") or []
        if isinstance(item, dict)
    }
    contexts = []
    for task in manifest.get("tasks") or []:
        task_id = str(task.get("task_id") or "")
        left_id = str(task.get("left_sample_id") or "")
        right_id = str(task.get("right_sample_id") or "")
        local = local_pairs.get(task_id) or {}
        proxy_choice = str(local.get("proxy_choice") or "unknown")
        prediction = str((local.get("predictions") or {}).get("research_ranker_v2_4") or "unknown")
        if proxy_choice not in _PAIR_SIDES or prediction not in {*_PAIR_SIDES, "tie"}:
            continue
        score_delta = (local.get("score_deltas") or {}).get("research_ranker_v2_4")
        try:
            v2_4_delta = float(score_delta)
        except (TypeError, ValueError):
            v2_4_delta = 1.0 if prediction == "left" else -1.0
        left = samples.get(left_id) or {}
        right = samples.get(right_id) or {}
        left_account = str(left.get("account_id") or "unknown")
        right_account = str(right.get("account_id") or "unknown")
        left_category = str((left.get("semantic") or {}).get("content_category") or "unknown")
        right_category = str((right.get("semantic") or {}).get("content_category") or "unknown")
        gap = abs(
            float(left.get("normalized_reward") or 0.0)
            - float(right.get("normalized_reward") or 0.0)
        )
        contexts.append(
            {
                "task_id": task_id,
                "left_sample_id": left_id,
                "right_sample_id": right_id,
                "outcome_choice": proxy_choice,
                "v2_4_choice": prediction,
                "v2_4_delta": v2_4_delta,
                "account_id": left_account if left_account == right_account else "cross_account",
                "content_category": left_category if left_category == right_category else "mixed",
                "outcome_gap": round(gap, 4),
                "outcome_gap_bucket": _gap_bucket(gap),
            }
        )
    return contexts


def _vectors_for_modality(records: dict[str, dict[str, dict]], modality: str) -> dict[str, list[float]]:
    vectors = {}
    for sample_id, modalities in records.items():
        record = modalities.get(modality)
        if not record:
            continue
        path = Path(str(record.get("vector_path") or ""))
        payload = read_json(path, default={}) if path.is_file() else {}
        vector = payload.get("vector") if isinstance(payload, dict) else None
        if not isinstance(vector, list) or len(vector) != BAILIAN_DEFAULT_EMBEDDING_DIMENSION:
            continue
        try:
            parsed = [float(value) for value in vector]
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in parsed):
            vectors[sample_id] = parsed
    return vectors


def _balanced_references(
    reference_ids: list[str],
    samples: dict[str, dict],
    vectors: dict[str, list[float]],
    total: int,
) -> list[str]:
    per_label = total // 2
    buckets = {
        label: [
            sample_id
            for sample_id in reference_ids
            if sample_id in vectors
            and str((samples.get(sample_id) or {}).get("performance_label") or "") == label
        ][:per_label]
        for label in ("high", "low")
    }
    if min(len(buckets["high"]), len(buckets["low"])) < per_label:
        available = min(len(buckets["high"]), len(buckets["low"]))
        return [
            sample_id
            for index in range(available)
            for sample_id in (buckets["high"][index], buckets["low"][index])
        ]
    return [
        sample_id
        for index in range(per_label)
        for sample_id in (buckets["high"][index], buckets["low"][index])
    ]


def _embedding_scores(
    task_contexts: list[dict],
    samples: dict[str, dict],
    vectors: dict[str, list[float]],
    reference_vectors: dict[str, list[float]],
    *,
    neighbors_per_label: int,
) -> dict[str, float]:
    evaluation_ids = {
        str(context[key])
        for context in task_contexts
        for key in ("left_sample_id", "right_sample_id")
    }
    scores = {}
    for sample_id in evaluation_ids:
        query = vectors.get(sample_id)
        if not query:
            continue
        similarities: dict[str, list[float]] = {"high": [], "low": []}
        for reference_id, reference in reference_vectors.items():
            label = str((samples.get(reference_id) or {}).get("performance_label") or "")
            if label in similarities:
                similarities[label].append(_cosine(query, reference))
        if not similarities["high"] or not similarities["low"]:
            continue
        high = sorted(similarities["high"], reverse=True)[:neighbors_per_label]
        low = sorted(similarities["low"], reverse=True)[:neighbors_per_label]
        scores[sample_id] = sum(high) / len(high) - sum(low) / len(low)
    return scores


def _pair_deltas(task_contexts: list[dict], scores: dict[str, float]) -> dict[str, float]:
    result = {}
    for context in task_contexts:
        left_id = str(context["left_sample_id"])
        right_id = str(context["right_sample_id"])
        if left_id in scores and right_id in scores:
            result[str(context["task_id"])] = float(scores[left_id]) - float(scores[right_id])
    return result


def _configuration_summary(
    strategy: str,
    *,
    component: str,
    task_contexts: list[dict],
    deltas: dict[str, float],
    manifest_sha256: str,
    parameters: dict,
) -> dict:
    rows = _evaluation_rows(task_contexts, deltas)
    pair_count = len(rows)
    cloud_accuracy = _balanced_accuracy(rows, "cloud_correct")
    baseline_accuracy = _balanced_accuracy(rows, "v2_4_correct")
    raw_accuracy = sum(row["cloud_correct"] for row in rows) / pair_count if pair_count else 0.0
    baseline_raw = sum(row["v2_4_correct"] for row in rows) / pair_count if pair_count else 0.0
    ci_low, ci_high = _bootstrap_delta_ci(rows, f"{manifest_sha256}:{strategy}")
    return {
        "strategy": strategy,
        "component": component,
        "parameters": parameters,
        "evaluable_pair_count": pair_count,
        "balanced_pairwise_accuracy": round(cloud_accuracy, 4),
        "raw_pairwise_accuracy": round(raw_accuracy, 4),
        "v2_4_balanced_pairwise_accuracy": round(baseline_accuracy, 4),
        "v2_4_raw_pairwise_accuracy": round(baseline_raw, 4),
        "accuracy_delta_vs_v2_4": round(cloud_accuracy - baseline_accuracy, 4),
        "raw_accuracy_delta_vs_v2_4": round(raw_accuracy - baseline_raw, 4),
        "low_interaction_avoidance_rate": round(raw_accuracy, 4),
        "bootstrap_delta_ci95": [round(ci_low, 4), round(ci_high, 4)],
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
        "network_request_count": 0,
        "effective_cost_cny": "0",
    }


def _evaluation_rows(task_contexts: list[dict], deltas: dict[str, float]) -> list[dict]:
    rows = []
    for context in task_contexts:
        task_id = str(context["task_id"])
        if task_id not in deltas:
            continue
        delta = float(deltas[task_id])
        cloud_choice = "left" if delta > 0 else "right" if delta < 0 else "tie"
        rows.append(
            {
                **context,
                "cloud_delta": round(delta, 8),
                "cloud_choice": cloud_choice,
                "cloud_correct": cloud_choice == context["outcome_choice"],
                "v2_4_correct": context["v2_4_choice"] == context["outcome_choice"],
            }
        )
    return rows


def _balanced_accuracy(rows: list[dict], correct_key: str) -> float:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[str(row["outcome_choice"])].append(row)
    if not buckets:
        return 0.0
    return sum(
        sum(bool(row[correct_key]) for row in values) / len(values)
        for values in buckets.values()
    ) / len(buckets)


def _bootstrap_delta_ci(rows: list[dict], seed_key: str) -> tuple[float, float]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[str(row["outcome_choice"])].append(row)
    if len(buckets) < 2 or not all(buckets.values()):
        return (0.0, 0.0)
    seed = int.from_bytes(hashlib.sha256(seed_key.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    deltas = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        class_deltas = []
        for values in buckets.values():
            sampled = [rng.choice(values) for _ in range(len(values))]
            class_deltas.append(
                sum(int(row["cloud_correct"]) - int(row["v2_4_correct"]) for row in sampled)
                / len(sampled)
            )
        deltas.append(sum(class_deltas) / len(class_deltas))
    deltas.sort()
    lower = deltas[int(0.025 * (len(deltas) - 1))]
    upper = deltas[int(0.975 * (len(deltas) - 1))]
    return lower, upper


def _blend_deltas(
    left: dict[str, float],
    right: dict[str, float],
    *,
    right_weight: float,
) -> dict[str, float]:
    common = sorted(set(left) & set(right))
    left_scale = _median_absolute_scale([left[key] for key in common])
    right_scale = _median_absolute_scale([right[key] for key in common])
    return {
        key: (1.0 - right_weight) * float(left[key]) / left_scale
        + right_weight * float(right[key]) / right_scale
        for key in common
    }


def _median_absolute_scale(values: list[float]) -> float:
    absolute = [abs(float(value)) for value in values if math.isfinite(float(value)) and value != 0]
    return median(absolute) if absolute else 1.0


def _best_configuration(configurations: list[dict]) -> dict | None:
    return max(configurations, key=_configuration_sort_key, default=None)


def _configuration_sort_key(configuration: dict) -> tuple:
    ci = configuration.get("bootstrap_delta_ci95") or [0.0, 0.0]
    return (
        float(configuration.get("balanced_pairwise_accuracy") or 0.0),
        float(configuration.get("raw_pairwise_accuracy") or 0.0),
        float(ci[0] if ci else 0.0),
        -len(str(configuration.get("strategy") or "")),
        str(configuration.get("strategy") or ""),
    )


def _group_metrics(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "unknown")].append(row)
    result = []
    for group, values in groups.items():
        cloud = _balanced_accuracy(values, "cloud_correct")
        baseline = _balanced_accuracy(values, "v2_4_correct")
        result.append(
            {
                "group": group,
                "pair_count": len(values),
                "outcome_distribution": dict(Counter(str(row["outcome_choice"]) for row in values)),
                "balanced_pairwise_accuracy": round(cloud, 4),
                "v2_4_balanced_pairwise_accuracy": round(baseline, 4),
                "accuracy_delta_vs_v2_4": round(cloud - baseline, 4),
                "ready": len(values) >= MIN_GROUP_PAIR_COUNT,
            }
        )
    return sorted(result, key=lambda row: (-int(row["pair_count"]), str(row["group"])))


def _gap_bucket(value: float) -> str:
    if value < 10:
        return "small_lt_10"
    if value < 30:
        return "medium_10_30"
    return "large_gte_30"
