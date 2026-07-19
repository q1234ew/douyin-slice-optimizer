from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from dso.learning.bailian_cached_ablation import (
    _balanced_references,
    _configuration_summary,
    _embedding_scores,
    _evaluation_rows,
    _group_metrics,
    _median_absolute_scale,
    _pair_deltas,
    _task_contexts,
    _vectors_for_modality,
)
from dso.learning.bailian_vector_chain import (
    BAILIAN_VECTOR_CHAIN_VERSION,
    _cloud_records,
    _load_stage_report,
    _local_vector_report,
    _manifest_samples,
    _persist_stage_report,
    _rerank_request,
    _sample_summary,
    build_bailian_vector_index,
    preflight_bailian_vector_index,
    run_bailian_vector_rerank,
)
from dso.learning.multimodal_vector_value import (
    DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    load_multimodal_vector_manifest,
)
from dso.providers.aliyun_bailian import (
    BAILIAN_RERANK_MODEL,
    AliyunBailianProvider,
)
from dso.providers.budget import BudgetLimits, Money
from dso.providers.contracts import ProviderDataPermissionRecord, stable_json_sha256
from dso.providers.service import AliyunBailianRuntime, build_aliyun_bailian_runtime
from dso.utils import utc_now


BAILIAN_HOLDOUT_VALIDATION_VERSION = "bailian_independent_holdout_validation.v1"
CALIBRATION_PAIR_COUNT = 40
HOLDOUT_PAIR_COUNT = 20
REFERENCE_POOL_SIZE = 20
NEIGHBORS_PER_LABEL = 3
RERANK_TOP_N = 20
EMBEDDING_RERANK_WEIGHT = 0.5
CLOUD_WEIGHT = 0.15
BASELINE_WEIGHT = 0.85
HARD_BATCH_CAP_CNY = Decimal("10.00")
MIN_HOLDOUT_DELTA = 0.05
MATERIAL_REGRESSION_DELTA = -0.05
MIN_COMBINED_ACCOUNT_WINS = 3
MIN_GROUP_PAIR_COUNT = 3
TARGET_HOLDOUT_ACCOUNTS = frozenset({"yuhuan", "hukan_music"})
_PAIR_CHOICES = frozenset({"left", "right", "tie"})
_BLIND_FORBIDDEN_KEYS = frozenset(
    {
        "proxy_choice",
        "outcome_choice",
        "performance_label",
        "normalized_reward",
        "reward_proxy",
        "anchor_sample_id",
        "control_sample_id",
    }
)


def freeze_bailian_holdout_validation(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    """Freeze D12-B without persisting any holdout outcome in the prediction contract."""

    manifest = load_multimodal_vector_manifest(benchmark_id)
    tasks = [item for item in manifest.get("tasks") or [] if isinstance(item, dict)]
    if len(tasks) < CALIBRATION_PAIR_COUNT + HOLDOUT_PAIR_COUNT:
        raise ValueError("D12-B requires the frozen 60-pair benchmark")
    calibration_tasks = tasks[:CALIBRATION_PAIR_COUNT]
    holdout_tasks = tasks[CALIBRATION_PAIR_COUNT : CALIBRATION_PAIR_COUNT + HOLDOUT_PAIR_COUNT]
    samples = _manifest_samples(manifest)
    local_report, baseline_source = _local_vector_report(str(manifest["benchmark_id"]))
    _validate_parent_report(manifest, local_report, "v2.4 baseline")
    ablation_report = _load_stage_report(str(manifest["benchmark_id"]), "ablation") or {}
    _validate_parent_report(manifest, ablation_report, "D12-A ablation")

    records = _cloud_records(list(samples))
    text_vectors = _vectors_for_modality(records, "text")
    reference_ids = _balanced_references(
        [str(value) for value in manifest.get("reference_sample_ids") or []],
        samples,
        text_vectors,
        REFERENCE_POOL_SIZE,
    )
    if len(reference_ids) != REFERENCE_POOL_SIZE:
        raise ValueError("D12-B requires 20 balanced cached text references")

    calibration_manifest = {**manifest, "tasks": calibration_tasks}
    calibration_contexts = _task_contexts(calibration_manifest, local_report, samples)
    if len(calibration_contexts) != CALIBRATION_PAIR_COUNT:
        raise ValueError("D12-B calibration split is not fully comparable")
    embedding_scores = _embedding_scores(
        calibration_contexts,
        samples,
        text_vectors,
        {sample_id: text_vectors[sample_id] for sample_id in reference_ids},
        neighbors_per_label=NEIGHBORS_PER_LABEL,
    )
    embedding_deltas = _pair_deltas(calibration_contexts, embedding_scores)
    rerank_report = _load_stage_report(str(manifest["benchmark_id"]), "rerank") or {}
    _validate_parent_report(manifest, rerank_report, "D12-A rerank")
    rerank_scores = _score_map(rerank_report.get("items"))
    rerank_deltas = _pair_deltas(calibration_contexts, rerank_scores)
    baseline_deltas = {
        str(context["task_id"]): float(context["v2_4_delta"])
        for context in calibration_contexts
    }
    expected_ids = {str(context["task_id"]) for context in calibration_contexts}
    for name, values in (
        ("text embedding", embedding_deltas),
        ("cached rerank", rerank_deltas),
        ("v2.4", baseline_deltas),
    ):
        if set(values) != expected_ids:
            raise ValueError(f"D12-B calibration {name} coverage is incomplete")

    scales = {
        "embedding": _median_absolute_scale(list(embedding_deltas.values())),
        "rerank": _median_absolute_scale(list(rerank_deltas.values())),
        "v2_4": _median_absolute_scale(list(baseline_deltas.values())),
    }
    cloud_deltas = _blend_with_frozen_scales(
        embedding_deltas,
        rerank_deltas,
        right_weight=EMBEDDING_RERANK_WEIGHT,
        left_scale=scales["embedding"],
        right_scale=scales["rerank"],
    )
    scales["cloud"] = _median_absolute_scale(list(cloud_deltas.values()))
    final_deltas = _blend_with_frozen_scales(
        baseline_deltas,
        cloud_deltas,
        right_weight=CLOUD_WEIGHT,
        left_scale=scales["v2_4"],
        right_scale=scales["cloud"],
    )

    holdout_baseline = _blind_baseline_rows(holdout_tasks, local_report)
    calibration_predictions = _blind_prediction_rows(
        calibration_tasks,
        baseline_deltas=baseline_deltas,
        embedding_deltas=embedding_deltas,
        rerank_deltas=rerank_deltas,
        cloud_deltas=cloud_deltas,
        final_deltas=final_deltas,
    )
    core = {
        "contract_version": BAILIAN_HOLDOUT_VALIDATION_VERSION,
        "parent_contract_version": BAILIAN_VECTOR_CHAIN_VERSION,
        "status": "frozen",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "split_policy": {
            "calibration_task_ids": [str(task.get("task_id") or "") for task in calibration_tasks],
            "holdout_task_ids": [str(task.get("task_id") or "") for task in holdout_tasks],
            "calibration_pair_count": CALIBRATION_PAIR_COUNT,
            "holdout_pair_count": HOLDOUT_PAIR_COUNT,
            "sample_overlap_count": len(
                _task_sample_ids(calibration_tasks) & _task_sample_ids(holdout_tasks)
            ),
        },
        "fixed_configuration": {
            "embedding_modality": "text",
            "balanced_reference_pool_size": REFERENCE_POOL_SIZE,
            "neighbors_per_label": NEIGHBORS_PER_LABEL,
            "rerank_top_n": RERANK_TOP_N,
            "embedding_rerank_weight": EMBEDDING_RERANK_WEIGHT,
            "cloud_weight": CLOUD_WEIGHT,
            "v2_4_weight": BASELINE_WEIGHT,
            "normalization": "frozen_d12_a_median_absolute_pair_delta",
            "hard_batch_cap_cny": format(HARD_BATCH_CAP_CNY, "f"),
        },
        "normalization_scales": {key: round(value, 12) for key, value in scales.items()},
        "reference_sample_ids": reference_ids,
        "holdout_baseline": holdout_baseline,
        "calibration_predictions": calibration_predictions,
        "source_reports": {
            "baseline": baseline_source,
            "ablation_generated_at": ablation_report.get("generated_at"),
            "rerank_generated_at": rerank_report.get("generated_at"),
        },
        "implementation_sha256": _implementation_sha256(),
        "labels_locked_during_prediction": True,
        "automatic_promotion": False,
        "production_weight_changed": False,
    }
    _assert_blind_payload(core)
    config_sha256 = stable_json_sha256(core)
    report = {**core, "config_sha256": config_sha256, "generated_at": utc_now()}
    existing = _load_stage_report(str(manifest["benchmark_id"]), "holdout-config") or {}
    if existing:
        if str(existing.get("config_sha256") or "") != config_sha256:
            raise ValueError(
                "D12-B holdout configuration is already frozen with different inputs; "
                "create a new benchmark ID instead of overwriting it"
            )
        return {**existing, "reused": True}
    _persist_stage_report(manifest, "holdout-config", report)
    return report


def run_bailian_holdout_prediction(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    *,
    runtime_builder: Callable[..., AliyunBailianRuntime] = build_aliyun_bailian_runtime,
) -> dict:
    """Generate an immutable prediction artifact before holdout labels are unlocked."""

    manifest = load_multimodal_vector_manifest(benchmark_id)
    config = _required_report(manifest, "holdout-config")
    _verify_frozen_config(config)
    _assert_blind_payload(config)
    samples = _manifest_samples(manifest)
    task_map = {
        str(task.get("task_id") or ""): task
        for task in manifest.get("tasks") or []
        if isinstance(task, dict)
    }
    holdout_tasks = [task_map[task_id] for task_id in config["split_policy"]["holdout_task_ids"]]
    holdout_ids = sorted(_task_sample_ids(holdout_tasks))
    batch_id = f"d12b-{str(manifest['benchmark_id'])[-20:]}-{str(config['config_sha256'])[:10]}"

    preflight = preflight_bailian_vector_index(
        manifest,
        entity_ids=holdout_ids,
        batch_id=batch_id,
        report_stage="holdout-preflight",
    )
    rerank_reserved = _rerank_maximum_reserved_cost(
        manifest,
        holdout_ids,
        [str(value) for value in config["reference_sample_ids"]],
    )
    maximum_reserved = Decimal(str(preflight.get("maximum_reserved_cost_cny") or "0")) + rerank_reserved
    if maximum_reserved > HARD_BATCH_CAP_CNY:
        raise ValueError(
            f"D12-B preflight maximum {maximum_reserved} CNY exceeds the 10.00 CNY hard cap"
        )

    capped_builder = _capped_runtime_builder(runtime_builder, HARD_BATCH_CAP_CNY)
    embedding_report = build_bailian_vector_index(
        manifest,
        entity_ids=holdout_ids,
        force=False,
        batch_id=batch_id,
        report_stage="holdout-embeddings",
        runtime_builder=capped_builder,
    )
    rerank_report = run_bailian_vector_rerank(
        manifest,
        entity_ids=holdout_ids,
        top_n=RERANK_TOP_N,
        batch_id=batch_id,
        include_outcomes=False,
        report_stage="holdout-rerank",
        reference_ids=[str(value) for value in config["reference_sample_ids"]],
        runtime_builder=capped_builder,
    )
    if int(embedding_report.get("failed") or 0) or rerank_report.get("errors"):
        raise RuntimeError("D12-B cloud cache build is incomplete; inspect holdout stage reports")

    text_vectors = _vectors_for_modality(_cloud_records(list(samples)), "text")
    reference_ids = [str(value) for value in config["reference_sample_ids"]]
    blind_contexts = [
        {
            "task_id": str(task.get("task_id") or ""),
            "left_sample_id": str(task.get("left_sample_id") or ""),
            "right_sample_id": str(task.get("right_sample_id") or ""),
        }
        for task in holdout_tasks
    ]
    embedding_scores = _embedding_scores(
        blind_contexts,
        samples,
        text_vectors,
        {sample_id: text_vectors[sample_id] for sample_id in reference_ids},
        neighbors_per_label=NEIGHBORS_PER_LABEL,
    )
    embedding_deltas = _pair_deltas(blind_contexts, embedding_scores)
    rerank_deltas = _pair_deltas(blind_contexts, _score_map(rerank_report.get("items")))
    baseline_deltas = {
        str(item["task_id"]): float(item["v2_4_delta"])
        for item in config["holdout_baseline"]
    }
    scales = {key: float(value) for key, value in config["normalization_scales"].items()}
    cloud_deltas = _blend_with_frozen_scales(
        embedding_deltas,
        rerank_deltas,
        right_weight=EMBEDDING_RERANK_WEIGHT,
        left_scale=scales["embedding"],
        right_scale=scales["rerank"],
    )
    final_deltas = _blend_with_frozen_scales(
        baseline_deltas,
        cloud_deltas,
        right_weight=CLOUD_WEIGHT,
        left_scale=scales["v2_4"],
        right_scale=scales["cloud"],
    )
    predictions = _blind_prediction_rows(
        holdout_tasks,
        baseline_deltas=baseline_deltas,
        embedding_deltas=embedding_deltas,
        rerank_deltas=rerank_deltas,
        cloud_deltas=cloud_deltas,
        final_deltas=final_deltas,
    )
    if len(predictions) != HOLDOUT_PAIR_COUNT:
        raise RuntimeError("D12-B prediction coverage is incomplete")
    effective_cost = Decimal(str(embedding_report.get("effective_cost_cny") or "0")) + Decimal(
        str(rerank_report.get("effective_cost_cny") or "0")
    )
    if effective_cost > HARD_BATCH_CAP_CNY:
        raise RuntimeError("D12-B effective cost exceeded the 10.00 CNY hard cap")

    core = {
        "contract_version": BAILIAN_HOLDOUT_VALIDATION_VERSION,
        "status": "predictions_frozen",
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "config_sha256": config["config_sha256"],
        "batch_id": batch_id,
        "pair_count": len(predictions),
        "predictions": predictions,
        "coverage": {
            "target_sample_count": len(holdout_ids),
            "text_ready_count": embedding_report.get("coverage", {}).get("text_ready_count", 0),
            "fusion_ready_count": embedding_report.get("coverage", {}).get("fusion_ready_count", 0),
            "rerank_completed_count": rerank_report.get("completed_count", 0),
        },
        "budget": {
            "hard_batch_cap_cny": format(HARD_BATCH_CAP_CNY, "f"),
            "maximum_preflight_reserved_cost_cny": format(maximum_reserved, "f"),
            "effective_cost_cny": format(effective_cost, "f"),
            "network_request_count": int(embedding_report.get("network_request_count") or 0)
            + int(rerank_report.get("network_request_count") or 0),
        },
        "labels_locked": True,
        "blind_payload_verified": True,
        "automatic_promotion": False,
        "production_weight_changed": False,
    }
    _assert_blind_payload(core)
    prediction_sha256 = stable_json_sha256(core)
    existing = _load_stage_report(str(manifest["benchmark_id"]), "holdout-predictions") or {}
    if existing:
        if str(existing.get("prediction_sha256") or "") != prediction_sha256:
            raise ValueError("D12-B predictions are already frozen and cannot be overwritten")
        return {**existing, "reused": True}
    report = {**core, "prediction_sha256": prediction_sha256, "generated_at": utc_now()}
    _persist_stage_report(manifest, "holdout-predictions", report)
    return report


def evaluate_bailian_holdout_validation(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    """Unlock the proxy labels only after the prediction checksum is frozen."""

    manifest = load_multimodal_vector_manifest(benchmark_id)
    config = _required_report(manifest, "holdout-config")
    predictions = _required_report(manifest, "holdout-predictions")
    _verify_frozen_config(config)
    if str(predictions.get("config_sha256") or "") != str(config.get("config_sha256") or ""):
        raise ValueError("D12-B prediction/config checksum mismatch")
    prediction_core = {
        key: value
        for key, value in predictions.items()
        if key not in {"prediction_sha256", "generated_at", "reused"}
    }
    if stable_json_sha256(prediction_core) != str(predictions.get("prediction_sha256") or ""):
        raise ValueError("D12-B prediction artifact checksum mismatch")
    _assert_blind_payload(prediction_core)

    samples = _manifest_samples(manifest)
    local_report, _ = _local_vector_report(str(manifest["benchmark_id"]))
    _validate_parent_report(manifest, local_report, "v2.4 baseline")
    task_map = {
        str(task.get("task_id") or ""): task
        for task in manifest.get("tasks") or []
        if isinstance(task, dict)
    }
    holdout_tasks = [task_map[task_id] for task_id in config["split_policy"]["holdout_task_ids"]]
    holdout_contexts = _task_contexts({**manifest, "tasks": holdout_tasks}, local_report, samples)
    holdout_deltas = {
        str(item["task_id"]): float(item["final_delta"])
        for item in predictions["predictions"]
    }
    holdout_summary = _configuration_summary(
        "d12_b_fixed_v24_cloud_w15",
        component="independent_holdout",
        task_contexts=holdout_contexts,
        deltas=holdout_deltas,
        manifest_sha256=str(manifest["manifest_sha256"]),
        parameters=config["fixed_configuration"],
    )
    holdout_rows = _evaluation_rows(holdout_contexts, holdout_deltas)
    holdout_accounts = _group_metrics(holdout_rows, "account_id")
    holdout_categories = _group_metrics(holdout_rows, "content_category")

    calibration_tasks = [
        task_map[task_id] for task_id in config["split_policy"]["calibration_task_ids"]
    ]
    calibration_contexts = _task_contexts(
        {**manifest, "tasks": calibration_tasks}, local_report, samples
    )
    calibration_deltas = {
        str(item["task_id"]): float(item["final_delta"])
        for item in config["calibration_predictions"]
    }
    calibration_summary = _configuration_summary(
        "d12_a_fixed_configuration_replay",
        component="calibration_replay",
        task_contexts=calibration_contexts,
        deltas=calibration_deltas,
        manifest_sha256=str(manifest["manifest_sha256"]),
        parameters=config["fixed_configuration"],
    )
    combined_contexts = [*calibration_contexts, *holdout_contexts]
    combined_deltas = {**calibration_deltas, **holdout_deltas}
    combined_summary = _configuration_summary(
        "d12_b_combined_60_secondary",
        component="combined_secondary",
        task_contexts=combined_contexts,
        deltas=combined_deltas,
        manifest_sha256=str(manifest["manifest_sha256"]),
        parameters=config["fixed_configuration"],
    )
    combined_rows = _evaluation_rows(combined_contexts, combined_deltas)
    combined_accounts = _group_metrics(combined_rows, "account_id")
    combined_categories = _group_metrics(combined_rows, "content_category")
    combined_account_wins = sum(
        row["ready"] and float(row["accuracy_delta_vs_v2_4"]) > 0
        for row in combined_accounts
    )
    target_account_wins = [
        row["group"]
        for row in holdout_accounts
        if row["group"] in TARGET_HOLDOUT_ACCOUNTS
        and row["ready"]
        and float(row["accuracy_delta_vs_v2_4"]) > 0
    ]
    holdout_delta = float(holdout_summary["accuracy_delta_vs_v2_4"])
    raw_delta = float(holdout_summary["raw_accuracy_delta_vs_v2_4"])
    effective_cost = Decimal(str(predictions.get("budget", {}).get("effective_cost_cny") or "0"))
    conditions = {
        "holdout_delta_at_least_5pp": holdout_delta >= MIN_HOLDOUT_DELTA,
        "low_interaction_avoidance_not_worse": raw_delta >= 0,
        "yuhuan_or_hukan_improves": bool(target_account_wins),
        "combined_ready_account_wins_at_least_three": (
            combined_account_wins >= MIN_COMBINED_ACCOUNT_WINS
        ),
        "prediction_cost_within_10_cny": effective_cost <= HARD_BATCH_CAP_CNY,
        "prediction_artifact_blind_and_immutable": True,
    }
    passed = all(conditions.values())
    if holdout_delta <= MATERIAL_REGRESSION_DELTA or raw_delta < MATERIAL_REGRESSION_DELTA:
        decision = "stop_cloud_ranking_route"
        status = "regressed"
    elif passed:
        decision = "continue_larger_shadow_validation"
        status = "positive_research_signal"
    elif abs(holdout_delta) < MIN_HOLDOUT_DELTA:
        decision = "inconclusive_keep_v2_4"
        status = "inconclusive"
    else:
        decision = "keep_v2_4_and_diagnose_group_instability"
        status = "group_gate_not_met"

    report = {
        "contract_version": BAILIAN_HOLDOUT_VALIDATION_VERSION,
        "status": status,
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "config_sha256": config["config_sha256"],
        "prediction_sha256": predictions["prediction_sha256"],
        "evaluation_semantics": (
            "Account-normalized visible-engagement proxy; not views, exposure, or follow conversion."
        ),
        "holdout_primary": holdout_summary,
        "calibration_replay": calibration_summary,
        "combined_60_secondary": combined_summary,
        "per_account_metrics": holdout_accounts,
        "per_category_metrics": holdout_categories,
        "combined_per_account_metrics": combined_accounts,
        "combined_per_category_metrics": combined_categories,
        "holdout_diagnostics": sorted(
            holdout_rows,
            key=lambda row: (
                row["cloud_correct"] == row["v2_4_correct"],
                -abs(float(row["cloud_delta"])),
                row["task_id"],
            ),
        ),
        "validation_gate": {
            "passed": passed,
            "conditions": conditions,
            "holdout_accuracy_delta": holdout_delta,
            "holdout_raw_accuracy_delta": raw_delta,
            "target_account_wins": target_account_wins,
            "combined_ready_account_win_count": combined_account_wins,
            "required_combined_ready_account_win_count": MIN_COMBINED_ACCOUNT_WINS,
            "decision": decision,
            "production_promotion": False,
        },
        "budget": predictions["budget"],
        "labels_unlocked_after_prediction_sha": True,
        "writes_manual_gold": False,
        "automatic_promotion": False,
        "production_weight_changed": False,
        "automatic_publish": False,
        "generated_at": utc_now(),
    }
    report["evaluation_sha256"] = stable_json_sha256(
        {key: value for key, value in report.items() if key != "generated_at"}
    )
    existing = _load_stage_report(str(manifest["benchmark_id"]), "holdout-evaluation") or {}
    if existing:
        if str(existing.get("evaluation_sha256") or "") != report["evaluation_sha256"]:
            raise ValueError("D12-B evaluation is already frozen and cannot be overwritten")
        return {**existing, "reused": True}
    _persist_stage_report(manifest, "holdout-evaluation", report)
    return report


def bailian_holdout_validation_status(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    config = _load_stage_report(benchmark_id, "holdout-config") or {}
    prediction = _load_stage_report(benchmark_id, "holdout-predictions") or {}
    evaluation = _load_stage_report(benchmark_id, "holdout-evaluation") or {}
    if evaluation:
        step = "evaluated"
    elif prediction:
        step = "predictions_frozen"
    elif config:
        step = "configuration_frozen"
    else:
        step = "not_started"
    return {
        "contract_version": BAILIAN_HOLDOUT_VALIDATION_VERSION,
        "status": step,
        "admission_status": "research_only",
        "hard_batch_cap_cny": format(HARD_BATCH_CAP_CNY, "f"),
        "config_sha256": config.get("config_sha256"),
        "prediction_sha256": prediction.get("prediction_sha256"),
        "holdout_pair_count": (config.get("split_policy") or {}).get("holdout_pair_count", 0),
        "coverage": prediction.get("coverage") or {},
        "budget": prediction.get("budget") or {},
        "holdout_primary": evaluation.get("holdout_primary") or {},
        "calibration_replay": evaluation.get("calibration_replay") or {},
        "combined_60_secondary": evaluation.get("combined_60_secondary") or {},
        "validation_gate": evaluation.get("validation_gate") or {},
        "generated_at": evaluation.get("generated_at")
        or prediction.get("generated_at")
        or config.get("generated_at"),
        "production_weight_changed": False,
    }


def _validate_parent_report(manifest: dict, report: dict, label: str) -> None:
    if not report:
        raise ValueError(f"D12-B requires {label}")
    if str(report.get("benchmark_id") or "") != str(manifest.get("benchmark_id") or ""):
        raise ValueError(f"D12-B {label} benchmark mismatch")
    if str(report.get("manifest_sha256") or "") != str(manifest.get("manifest_sha256") or ""):
        raise ValueError(f"D12-B {label} manifest mismatch")


def _blind_baseline_rows(tasks: list[dict], local_report: dict) -> list[dict]:
    local_pairs = {
        str(item.get("task_id") or ""): item
        for item in local_report.get("pair_results") or []
        if isinstance(item, dict)
    }
    result = []
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        local = local_pairs.get(task_id) or {}
        choice = str((local.get("predictions") or {}).get("research_ranker_v2_4") or "")
        delta = (local.get("score_deltas") or {}).get("research_ranker_v2_4")
        if choice not in _PAIR_CHOICES:
            raise ValueError(f"D12-B v2.4 prediction missing for {task_id}")
        try:
            parsed_delta = float(delta)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"D12-B v2.4 score delta missing for {task_id}") from exc
        result.append(
            {
                "task_id": task_id,
                "left_sample_id": str(task.get("left_sample_id") or ""),
                "right_sample_id": str(task.get("right_sample_id") or ""),
                "v2_4_choice": choice,
                "v2_4_delta": parsed_delta,
            }
        )
    return result


def _blind_prediction_rows(
    tasks: list[dict],
    *,
    baseline_deltas: dict[str, float],
    embedding_deltas: dict[str, float],
    rerank_deltas: dict[str, float],
    cloud_deltas: dict[str, float],
    final_deltas: dict[str, float],
) -> list[dict]:
    result = []
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        if not all(
            task_id in values
            for values in (
                baseline_deltas,
                embedding_deltas,
                rerank_deltas,
                cloud_deltas,
                final_deltas,
            )
        ):
            continue
        final_delta = float(final_deltas[task_id])
        result.append(
            {
                "task_id": task_id,
                "left_sample_id": str(task.get("left_sample_id") or ""),
                "right_sample_id": str(task.get("right_sample_id") or ""),
                "v2_4_delta": round(float(baseline_deltas[task_id]), 12),
                "embedding_delta": round(float(embedding_deltas[task_id]), 12),
                "rerank_delta": round(float(rerank_deltas[task_id]), 12),
                "cloud_delta": round(float(cloud_deltas[task_id]), 12),
                "final_delta": round(final_delta, 12),
                "predicted_choice": "tie" if final_delta == 0 else "left" if final_delta > 0 else "right",
            }
        )
    return result


def _blend_with_frozen_scales(
    left: dict[str, float],
    right: dict[str, float],
    *,
    right_weight: float,
    left_scale: float,
    right_scale: float,
) -> dict[str, float]:
    return {
        task_id: (1.0 - right_weight) * float(left[task_id]) / max(left_scale, 1e-9)
        + right_weight * float(right[task_id]) / max(right_scale, 1e-9)
        for task_id in left.keys() & right.keys()
    }


def _score_map(items: Any) -> dict[str, float]:
    return {
        str(item.get("sample_id") or ""): float(item.get("score") or 0.0)
        for item in items or []
        if isinstance(item, dict) and str(item.get("sample_id") or "")
    }


def _task_sample_ids(tasks: list[dict]) -> set[str]:
    return {
        str(task.get(key) or "")
        for task in tasks
        for key in ("left_sample_id", "right_sample_id")
        if str(task.get(key) or "")
    }


def _verify_frozen_config(config: dict) -> None:
    core = {
        key: value
        for key, value in config.items()
        if key not in {"config_sha256", "generated_at", "reused"}
    }
    if stable_json_sha256(core) != str(config.get("config_sha256") or ""):
        raise ValueError("D12-B frozen configuration checksum mismatch")


def _required_report(manifest: dict, stage: str) -> dict:
    report = _load_stage_report(str(manifest["benchmark_id"]), stage) or {}
    if not report:
        raise ValueError(f"D12-B requires {stage} first")
    if str(report.get("manifest_sha256") or "") != str(manifest.get("manifest_sha256") or ""):
        raise ValueError(f"D12-B {stage} manifest mismatch")
    return report


def _assert_blind_payload(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        forbidden = _BLIND_FORBIDDEN_KEYS & set(value)
        if forbidden:
            raise ValueError(
                f"D12-B blind artifact contains forbidden outcome fields at {path}: "
                + ", ".join(sorted(forbidden))
            )
        for key, item in value.items():
            _assert_blind_payload(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_blind_payload(item, f"{path}[{index}]")


def _rerank_maximum_reserved_cost(
    manifest: dict,
    entity_ids: list[str],
    reference_ids: list[str],
) -> Decimal:
    samples = _manifest_samples(manifest)
    provider = AliyunBailianProvider(model_id=BAILIAN_RERANK_MODEL)
    runtime = SimpleNamespace(
        provider=provider,
        data_permission=ProviderDataPermissionRecord(),
    )
    documents = [
        {"sample_id": sample_id, "text": _sample_summary(samples[sample_id])}
        for sample_id in reference_ids
    ]
    total = Decimal("0")
    for sample_id in entity_ids:
        request = _rerank_request(
            runtime,
            manifest=manifest,
            sample=samples[sample_id],
            documents=documents,
            top_n=RERANK_TOP_N,
            batch_id="d12b-preflight",
        )
        total += provider.estimate_max_cost(request).amount
    return total


def _capped_runtime_builder(
    runtime_builder: Callable[..., AliyunBailianRuntime],
    hard_cap: Decimal,
) -> Callable[..., AliyunBailianRuntime]:
    def build(*, batch_id: str, model_id: str) -> AliyunBailianRuntime:
        runtime = runtime_builder(batch_id=batch_id, model_id=model_id)
        guard = getattr(runtime, "budget_guard", None)
        limits = getattr(guard, "limits", None)
        if isinstance(limits, BudgetLimits):
            if limits.per_request.amount > hard_cap:
                raise RuntimeError("configured per-request budget exceeds the D12-B hard batch cap")
            guard.limits = BudgetLimits(
                per_request=limits.per_request,
                per_batch=Money(min(limits.per_batch.amount, hard_cap), limits.currency),
                per_day=limits.per_day,
            )
        return runtime

    return build


def _implementation_sha256() -> str:
    paths = (
        Path(__file__),
        Path(__file__).with_name("bailian_cached_ablation.py"),
        Path(__file__).with_name("bailian_vector_chain.py"),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()
