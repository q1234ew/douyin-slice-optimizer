from __future__ import annotations

import base64
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Callable

from dso.learning.bailian_complete_clip_batch import (
    COMPLETE_CLIP_PROXY_VERSION,
    DEFAULT_FPS,
    DEFAULT_MAX_PIXELS,
    DEFAULT_TIMEOUT_SECONDS,
    prepare_complete_clip_proxy,
)
from dso.providers.aliyun_bailian import (
    BAILIAN_QWEN35_OMNI_FEATURE_PROFILE,
    BAILIAN_QWEN35_OMNI_MODEL,
)
from dso.providers.contracts import (
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderRequest,
    stable_json_sha256,
)
from dso.providers.policy import UploadLevel
from dso.providers.service import AliyunBailianRuntime, build_aliyun_bailian_runtime
from dso.utils import write_json


BAILIAN_PROPAGATION_FEATURE_BATCH_VERSION = "bailian_propagation_feature_batch.v1"
PROPAGATION_FEATURE_DATASET_VERSION = "propagation_feature_outcome_dataset.v1"
DEFAULT_BATCH_LIMIT = 10
MAX_BATCH_LIMIT = 100
DEFAULT_HARD_BUDGET_CNY = Decimal("50")
DEFAULT_OUTPUT_TOKENS = 1_200
NEUTRAL_FEATURE_SUMMARY = (
    "已授权的完整短视频切片。只抽取实际音画与时序事实；"
    "不要参考或猜测账号、标题、标签、互动表现或平台结果。"
)
FEATURE_PATHS = {
    "content_form": ("content_form",),
    "hook_modality": ("hook", "modality"),
    "hook_strength": ("hook", "strength"),
    "audio_energy": ("audio", "energy"),
    "audio_energy_change": ("audio", "energy_change"),
    "audience_reaction": ("audio", "audience_reaction"),
    "primary_scene": ("visual", "primary_scene"),
    "cut_density": ("visual", "cut_density"),
    "text_density": ("visual", "text_density"),
    "narrative_arc": ("narrative", "arc"),
    "context_dependency": ("narrative", "context_dependency"),
    "novelty": ("narrative", "novelty"),
    "emotional_intensity": ("narrative", "emotional_intensity"),
    "payoff_present": ("narrative", "payoff_present"),
}


def run_bailian_propagation_feature_batch(
    manifest_path: Path,
    *,
    media_root: Path | None = None,
    output_path: Path | None = None,
    execute: bool = False,
    limit: int = DEFAULT_BATCH_LIMIT,
    force_proxies: bool = False,
    batch_id: str | None = None,
    hard_budget_cny: Decimal = DEFAULT_HARD_BUDGET_CNY,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    runtime_builder: Callable[..., AliyunBailianRuntime] = build_aliyun_bailian_runtime,
) -> dict[str, Any]:
    selected_limit = int(limit)
    if not 1 <= selected_limit <= MAX_BATCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH_LIMIT}")
    if hard_budget_cny <= 0:
        raise ValueError("hard_budget_cny must be positive")
    if isinstance(output_tokens, bool) or not 1 <= int(output_tokens) <= 8_192:
        raise ValueError("output_tokens must be between 1 and 8192")

    resolved_manifest = manifest_path.resolve()
    manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    clips = [item for item in manifest.get("clips") or [] if isinstance(item, dict)]
    if not clips:
        raise ValueError("propagation feature manifest contains no clips")
    selected_clips = clips[:selected_limit]
    root = (media_root or resolved_manifest.parent).resolve()
    proxy_root = root / "derived" / COMPLETE_CLIP_PROXY_VERSION
    proxy_root.mkdir(parents=True, exist_ok=True)
    resolved_batch_id = str(
        batch_id
        or f"propagation-feature-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    ).strip()
    runtime = runtime_builder(
        batch_id=resolved_batch_id,
        model_id=BAILIAN_QWEN35_OMNI_MODEL,
        request_profile=BAILIAN_QWEN35_OMNI_FEATURE_PROFILE,
    )
    if UploadLevel.FULL_MEDIA not in runtime.allowed_upload_levels:
        raise RuntimeError("propagation feature batch requires explicit full_media permission")

    prepared: list[dict[str, Any]] = []
    reserved_total = Decimal("0")
    for clip in selected_clips:
        source = root / _required_text(clip, "remote_filename")
        proxy = proxy_root / f"{_required_text(clip, 'sample_id')}.mp4"
        proxy_info = prepare_complete_clip_proxy(
            source,
            proxy,
            expected_source_sha256=_required_text(clip, "sha256"),
            expected_duration_seconds=float(clip.get("duration_seconds") or 0.0),
            force=force_proxies,
        )
        request = build_propagation_feature_request(
            runtime,
            clip,
            proxy,
            proxy_info,
            resolved_batch_id,
            output_tokens=int(output_tokens),
        )
        preflight = runtime.provider.preflight_request(request)
        reservation = runtime.provider.estimate_max_cost(request)
        reserved_total += reservation.amount
        prepared.append(
            {
                "clip": clip,
                "proxy_path": proxy,
                "proxy": proxy_info,
                "preflight": preflight,
                "reserved_cost_cny": str(reservation.amount),
            }
        )
    if reserved_total > hard_budget_cny:
        raise RuntimeError(
            "propagation feature batch preflight exceeds hard budget: "
            f"{reserved_total} > {hard_budget_cny} CNY"
        )

    report: dict[str, Any] = {
        "contract_version": BAILIAN_PROPAGATION_FEATURE_BATCH_VERSION,
        "experiment_id": resolved_batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "preflight_passed",
        "admission_status": "research_only",
        "goal_alignment": ["G1", "G3"],
        "source_manifest_id": manifest.get("manifest_id") or manifest.get("benchmark_id"),
        "source_manifest_sha256": _file_sha256(resolved_manifest),
        "evaluation_validity": "feature_schema_pilot_not_ranking_holdout",
        "feature_policy": {
            "representation": "complete_temporal_h264_aac_proxy",
            "frame_extraction_used": False,
            "full_duration_required": True,
            "audio_required": True,
            "traffic_score_requested": False,
            "output_token_limit": int(output_tokens),
            "platform_outcomes_excluded_from_provider_request": True,
            "neutral_summary_sha256": hashlib.sha256(
                NEUTRAL_FEATURE_SUMMARY.encode("utf-8")
            ).hexdigest(),
        },
        "provider": {
            "provider_id": runtime.provider.descriptor.identity.provider_id,
            "model_id": runtime.provider.descriptor.identity.model_id,
            "api_version": runtime.provider.descriptor.identity.api_version,
            "prompt_version": runtime.provider.descriptor.identity.prompt_version,
            "request_profile": BAILIAN_QWEN35_OMNI_FEATURE_PROFILE,
            "request_type": "omni_propagation_feature_extraction",
            "upload_level": UploadLevel.FULL_MEDIA.value,
            "max_retries": 0,
        },
        "budget": {
            "hard_batch_cap_cny": str(hard_budget_cny),
            "preflight_reserved_total_cny": str(reserved_total),
            "within_hard_cap": reserved_total <= hard_budget_cny,
        },
        "clips": [
            {
                "sample_id": item["clip"].get("sample_id"),
                "source_pair_id": item["clip"].get("source_pair_id"),
                "proxy": item["proxy"],
                "preflight": item["preflight"],
                "reserved_cost_cny": item["reserved_cost_cny"],
                "outcomes_locked": True,
            }
            for item in prepared
        ],
        "network_request_count": 0,
        "usage_estimated_cost_cny": "0",
        "production_impact": _production_impact(),
    }
    if not execute:
        return _finish_report(report, output_path)

    results: list[dict[str, Any]] = []
    for item in prepared:
        clip = item["clip"]
        request = build_propagation_feature_request(
            runtime,
            clip,
            item["proxy_path"],
            item["proxy"],
            resolved_batch_id,
            output_tokens=int(output_tokens),
        )
        reservation = runtime.provider.estimate_max_cost(request)
        outcome = runtime.runner.execute(
            request,
            estimated_cost=reservation,
            upload_level=UploadLevel.FULL_MEDIA,
            batch_id=resolved_batch_id,
            local_baseline={
                "sample_id": _required_text(clip, "sample_id"),
                "status": "platform_outcomes_locked_until_batch_completion",
            },
        )
        ledger = runtime.runner.ledger.get(outcome.ledger_call_id) or {}
        results.append(_result_summary(clip, item["proxy"], outcome.to_dict(), ledger))

    report.update(
        {
            "status": (
                "completed"
                if all(
                    item["provider_status"] in {"shadow_succeeded", "shadow_cached"}
                    for item in results
                )
                else "partial"
            ),
            "clips": results,
            "evaluation": evaluate_propagation_feature_results(results),
            "network_request_count": sum(int(item["network_request_count"]) for item in results),
            "usage_estimated_cost_cny": str(
                sum(
                    (
                        Decimal(str(item["usage_estimated_cost_cny"]))
                        for item in results
                    ),
                    Decimal("0"),
                )
            ),
        }
    )
    return _finish_report(report, output_path)


def build_propagation_feature_request(
    runtime: AliyunBailianRuntime,
    clip: dict[str, Any],
    proxy_path: Path,
    proxy_info: dict[str, Any],
    batch_id: str,
    *,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
) -> ProviderRequest:
    video = proxy_path.read_bytes()
    video_sha256 = hashlib.sha256(video).hexdigest()
    duration = float(proxy_info["duration_seconds"])
    audio_seconds = float(proxy_info["audio_seconds"])
    frame_count = math.ceil(duration * DEFAULT_FPS)
    estimated_audio_tokens = math.ceil(max(1.0, audio_seconds) * 7)
    conservative_input_tokens = (
        frame_count * math.ceil(DEFAULT_MAX_PIXELS / 1024)
        + estimated_audio_tokens
        + len(NEUTRAL_FEATURE_SUMMARY.encode("utf-8")) * 4
        + 1_024
    )
    target = runtime.provider.descriptor.identity
    content_sha256 = stable_json_sha256(
        {
            "proxy_sha256": video_sha256,
            "summary_sha256": hashlib.sha256(
                NEUTRAL_FEATURE_SUMMARY.encode("utf-8")
            ).hexdigest(),
            "fps": DEFAULT_FPS,
            "max_pixels": DEFAULT_MAX_PIXELS,
            "estimated_output_tokens": int(output_tokens),
            "prompt_version": target.prompt_version,
            "proxy_version": COMPLETE_CLIP_PROXY_VERSION,
        }
    )
    sample_id = _required_text(clip, "sample_id")
    return ProviderRequest(
        request_id=f"{batch_id}-{sample_id}-{video_sha256[:10]}",
        request_type="omni_propagation_feature_extraction",
        target=target,
        content_sha256=content_sha256,
        input_size=ProviderInputSize(
            video_seconds=duration,
            audio_seconds=audio_seconds,
            frame_count=frame_count,
            text_characters=len(NEUTRAL_FEATURE_SUMMARY),
            input_tokens=conservative_input_tokens,
            request_bytes=len(video),
        ),
        data_permission=runtime.data_permission,
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            max_retries=0,
        ),
        payload={
            "summary": NEUTRAL_FEATURE_SUMMARY,
            "video_base64": base64.b64encode(video).decode("ascii"),
            "video_mime_type": "video/mp4",
            "video_sha256": video_sha256,
        },
        parameters={
            "estimated_output_tokens": int(output_tokens),
            "estimated_audio_tokens": estimated_audio_tokens,
            "fps": DEFAULT_FPS,
            "max_pixels": DEFAULT_MAX_PIXELS,
        },
    )


def evaluate_propagation_feature_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [
        item
        for item in results
        if item.get("provider_status") in {"shadow_succeeded", "shadow_cached"}
        and isinstance(item.get("provider_output"), dict)
        and item.get("provider_output")
    ]
    feature_rows = [_feature_outcome_row(item) for item in results]
    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in successful:
        pairs[str(item.get("source_pair_id") or "")].append(item)
    comparable_pairs = [key for key, values in pairs.items() if key and len(values) == 2]
    latencies = sorted(float(item.get("latency_ms") or 0.0) for item in successful)
    costs = [Decimal(str(item.get("usage_estimated_cost_cny") or "0")) for item in results]
    return {
        "feature_dataset_version": PROPAGATION_FEATURE_DATASET_VERSION,
        "sample_count": len(results),
        "successful_count": len(successful),
        "schema_valid_rate": round(len(successful) / max(1, len(results)), 4),
        "abstain_count": sum(
            bool((item.get("provider_output") or {}).get("abstain")) for item in successful
        ),
        "timeline_coverage": round(
            sum(bool((item.get("provider_output") or {}).get("timeline")) for item in successful)
            / max(1, len(successful)),
            4,
        ),
        "audio_evidence_coverage": round(
            sum(bool(((item.get("provider_output") or {}).get("audio") or {}).get("evidence")) for item in successful)
            / max(1, len(successful)),
            4,
        ),
        "visual_evidence_coverage": round(
            sum(bool(((item.get("provider_output") or {}).get("visual") or {}).get("evidence")) for item in successful)
            / max(1, len(successful)),
            4,
        ),
        "comparable_pair_count": len(comparable_pairs),
        "latency_ms": {
            "median": round(median(latencies), 1) if latencies else 0.0,
            "p95": round(_percentile(latencies, 0.95), 1) if latencies else 0.0,
        },
        "usage_estimated_cost_cny": str(sum(costs, Decimal("0"))),
        "outcome_availability": _outcome_availability(feature_rows),
        "feature_distributions_by_heat_label": _feature_distributions(successful),
        "exploratory_feature_outcome_associations": _feature_outcome_associations(
            successful
        ),
        "feature_outcome_rows": feature_rows,
        "promotion_gate": {
            "status": "research_only",
            "passed": False,
            "reason": (
                "This batch validates feature extraction and outcome mapping only. "
                "It does not train or promote a propagation ranker."
            ),
        },
    }


def _feature_outcome_row(item: dict[str, Any]) -> dict[str, Any]:
    engagement = item.get("visible_engagement") or {}
    views = _optional_positive_number(item.get("views"))
    follows = _optional_non_negative_number(item.get("follows"))
    shares = _optional_non_negative_number(engagement.get("shares"))
    watch_fields = {
        key: item.get(key)
        for key in ("five_second_retention", "average_watch_ratio", "completion_rate")
        if item.get(key) is not None
    }
    return {
        "sample_id": item.get("sample_id"),
        "platform_item_id": item.get("platform_item_id"),
        "provider_status": item.get("provider_status"),
        "features": item.get("provider_output") or None,
        "outcomes": {
            "visible_engagement_heat": {
                "status": "available_proxy",
                "performance_label": item.get("performance_label"),
                "normalized_reward": item.get("normalized_reward"),
                "reward_proxy": item.get("reward_proxy"),
                "components": engagement,
            },
            "share_rate": {
                "status": "available" if views and shares is not None else "unavailable_missing_views",
                "value": round(shares / views, 8) if views and shares is not None else None,
                "numerator_shares": shares,
                "denominator_views": views,
            },
            "follow_conversion_rate": {
                "status": (
                    "available"
                    if views and follows is not None
                    else "unavailable_missing_views_or_follows"
                ),
                "value": round(follows / views, 8) if views and follows is not None else None,
                "numerator_follows": follows,
                "denominator_views": views,
            },
            "watch_quality": {
                "status": "available" if len(watch_fields) == 3 else "unavailable_missing_watch_metrics",
                **watch_fields,
            },
        },
    }


def _outcome_availability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "visible_engagement_heat",
        "share_rate",
        "follow_conversion_rate",
        "watch_quality",
    )
    result: dict[str, Any] = {}
    for field in fields:
        statuses = Counter(
            str(((row.get("outcomes") or {}).get(field) or {}).get("status") or "unknown")
            for row in rows
        )
        available = sum(count for status, count in statuses.items() if status.startswith("available"))
        result[field] = {
            "available_count": available,
            "coverage": round(available / max(1, len(rows)), 4),
            "statuses": dict(statuses),
        }
    return result


def _feature_distributions(results: list[dict[str, Any]]) -> dict[str, Any]:
    distributions: dict[str, dict[str, Counter[str]]] = {
        name: {"high": Counter(), "low": Counter(), "mid": Counter()}
        for name in FEATURE_PATHS
    }
    for item in results:
        label = str(item.get("performance_label") or "mid")
        if label not in {"high", "mid", "low"}:
            label = "mid"
        output = item.get("provider_output") or {}
        for name, path in FEATURE_PATHS.items():
            value = _nested_feature_value(output, path)
            distributions[name][label][str(value)] += 1
    return {
        name: {label: dict(counter) for label, counter in by_label.items()}
        for name, by_label in distributions.items()
    }


def _feature_outcome_associations(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_label = Counter(str(item.get("performance_label") or "mid") for item in results)
    high_count = by_label["high"]
    low_count = by_label["low"]
    if not high_count or not low_count:
        return {
            "status": "insufficient_label_coverage",
            "sample_counts": dict(by_label),
            "top_prevalence_differences": [],
            "paired_contrasts": [],
        }

    distributions = _feature_distributions(results)
    differences: list[dict[str, Any]] = []
    for feature, labels in distributions.items():
        high_values = labels["high"]
        low_values = labels["low"]
        for value in sorted(set(high_values) | set(low_values)):
            high_hits = int(high_values.get(value, 0))
            low_hits = int(low_values.get(value, 0))
            high_rate = high_hits / high_count
            low_rate = low_hits / low_count
            delta = high_rate - low_rate
            differences.append(
                {
                    "feature": feature,
                    "value": value,
                    "high_count": high_hits,
                    "low_count": low_hits,
                    "high_prevalence": round(high_rate, 4),
                    "low_prevalence": round(low_rate, 4),
                    "prevalence_delta": round(delta, 4),
                    "direction": "high" if delta > 0 else "low" if delta < 0 else "neutral",
                }
            )
    differences.sort(
        key=lambda item: (
            -abs(float(item["prevalence_delta"])),
            str(item["feature"]),
            str(item["value"]),
        )
    )

    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        pair_id = str(item.get("source_pair_id") or "")
        if pair_id:
            pairs[pair_id].append(item)
    contrast_counts: dict[str, Counter[str]] = {
        feature: Counter() for feature in FEATURE_PATHS
    }
    comparable_pairs = 0
    for items in pairs.values():
        high = next((item for item in items if item.get("performance_label") == "high"), None)
        low = next((item for item in items if item.get("performance_label") == "low"), None)
        if not high or not low:
            continue
        comparable_pairs += 1
        high_output = high.get("provider_output") or {}
        low_output = low.get("provider_output") or {}
        for feature, path in FEATURE_PATHS.items():
            high_value = str(_nested_feature_value(high_output, path))
            low_value = str(_nested_feature_value(low_output, path))
            if high_value != low_value:
                contrast_counts[feature][f"{high_value} > {low_value}"] += 1
    paired_contrasts = [
        {
            "feature": feature,
            "high_vs_low": contrast,
            "pair_count": count,
        }
        for feature, counter in contrast_counts.items()
        for contrast, count in counter.most_common()
    ]
    paired_contrasts.sort(
        key=lambda item: (-int(item["pair_count"]), str(item["feature"]), str(item["high_vs_low"]))
    )
    minimum_group = min(high_count, low_count)
    return {
        "status": "exploratory_only" if minimum_group >= 20 else "low_confidence_exploratory_only",
        "sample_counts": dict(by_label),
        "comparable_pair_count": comparable_pairs,
        "top_prevalence_differences": differences[:20],
        "paired_contrasts": paired_contrasts[:20],
        "causal_claim_allowed": False,
        "minimum_group_size_for_directional_review": 20,
    }


def _nested_feature_value(output: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = output
    for part in path:
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _result_summary(
    clip: dict[str, Any],
    proxy: dict[str, Any],
    outcome: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    return {
        "sample_id": clip.get("sample_id"),
        "source_pair_id": clip.get("source_pair_id"),
        "side": clip.get("side"),
        "account_id": clip.get("account_id"),
        "platform_item_id": clip.get("platform_item_id"),
        "performance_label": clip.get("performance_label"),
        "normalized_reward": float(clip.get("normalized_reward") or 0.0),
        "reward_proxy": float(clip.get("reward_proxy") or 0.0),
        "visible_engagement": clip.get("visible_engagement") or {},
        "views": clip.get("views"),
        "follows": clip.get("follows"),
        "five_second_retention": clip.get("five_second_retention"),
        "average_watch_ratio": clip.get("average_watch_ratio"),
        "completion_rate": clip.get("completion_rate"),
        "proxy": proxy,
        "provider_status": outcome.get("status"),
        "provider_output": outcome.get("provider_output") or {},
        "network_request_count": int(outcome.get("network_request_count") or 0),
        "cache_hit": bool(outcome.get("cache_hit")),
        "usage_estimated_cost_cny": str(outcome.get("usage_estimated_cost") or "0"),
        "billing_status": outcome.get("billing_status"),
        "latency_ms": float(ledger.get("latency_ms") or 0.0),
        "input_tokens": int(ledger.get("input_tokens") or 0),
        "output_tokens": int(ledger.get("output_tokens") or 0),
        "provider_request_id_present": bool(ledger.get("provider_request_id")),
        "ledger_call_id": outcome.get("ledger_call_id"),
    }


def _optional_positive_number(value: Any) -> float | None:
    result = _optional_non_negative_number(value)
    return result if result is not None and result > 0 else None


def _optional_non_negative_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _production_impact() -> dict[str, bool]:
    return {
        "production_weight_changed": False,
        "writes_manual_gold": False,
        "automatic_export": False,
        "automatic_publish": False,
    }


def _finish_report(report: dict[str, Any], output_path: Path | None) -> dict[str, Any]:
    core = dict(report)
    core.pop("report_sha256", None)
    report["report_sha256"] = stable_json_sha256(core)
    report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if output_path is not None:
        write_json(output_path.resolve(), report)
    return report


def _required_text(value: dict[str, Any], field: str) -> str:
    result = str(value.get(field) or "").strip()
    if not result:
        raise ValueError(f"manifest clip requires {field}")
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight
