from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shutil
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dso.learning.backtest import (
    RESEARCH_RANKER_V23_STRATEGY,
    RESEARCH_RANKER_V24_STRATEGY,
    RESEARCH_RANKER_V24_WEIGHT_CONFIG,
    _apply_leakage_guard,
    _apply_v24_diversity,
    _account_ranker_profiles,
    _historical_group_baselines,
    _historical_strategy_scores,
    _history_candidate_index,
    _interaction_thresholds,
    _prepare_history_tokens,
    _score_v24_from_components,
    _select_v24_signal_gate_score,
    _v24_reliable_signal_row,
    _v24_signal_quality,
)
from dso.learning.bailian_propagation_features import (
    FEATURE_PATHS,
    evaluate_propagation_feature_results,
)
from dso.providers.contracts import stable_json_sha256
from dso.utils import clamp
from dso.versions import RESEARCH_LABEL_VERSION


PROPAGATION_VALIDATION_MANIFEST_VERSION = "propagation_feature_validation_manifest.v1"
PROPAGATION_VALIDATION_REPORT_VERSION = "propagation_feature_account_holdout.v1"
DEFAULT_PAIR_COUNT = 30
DEFAULT_MIN_ACCOUNTS = 8
DEFAULT_MAX_DURATION_DELTA_SECONDS = 4.0
DEFAULT_OMNI_WEIGHT = 0.15
DEFAULT_TOP_K = 15
_MEDIA_SUFFIXES = frozenset({".mp4", ".mov", ".m4v"})
_TITLE_CLEANUP = re.compile(r"https?://\S+|[@#《》【】\[\]（）()，,。.!！?？:：;；\"'“”‘’、\s]+")
_AGE_BUCKET_PATTERN = re.compile(r"(?:^|;)age_bucket=([^;]+)")
_DURATION_BUCKET_PATTERN = re.compile(r"(?:^|;)duration_bucket=([^;]+)")


def build_propagation_validation_manifest(
    *,
    db_path: Path,
    media_root: Path,
    repo_root: Path,
    excluded_manifest_paths: list[Path],
    benchmark_dir: Path | None = None,
    manifest_id: str | None = None,
    pair_count: int = DEFAULT_PAIR_COUNT,
    min_accounts: int = DEFAULT_MIN_ACCOUNTS,
    max_duration_delta_seconds: float = DEFAULT_MAX_DURATION_DELTA_SECONDS,
    ffprobe_path: str | None = None,
    media_probe: Callable[[Path, str | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if pair_count < 1:
        raise ValueError("pair_count must be positive")
    if min_accounts < 2 or min_accounts > pair_count:
        raise ValueError("min_accounts must be between 2 and pair_count")
    if max_duration_delta_seconds <= 0:
        raise ValueError("max_duration_delta_seconds must be positive")

    resolved_repo = repo_root.resolve()
    resolved_media = media_root.resolve()
    excluded = _excluded_evidence(excluded_manifest_paths)
    media_index = _media_index(resolved_media)
    probe = media_probe or _probe_media
    rows = _eligible_rows(db_path.resolve(), media_index, excluded)
    probed_rows: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    for row in rows:
        try:
            media = probe(Path(row["media_path"]), ffprobe_path)
        except (OSError, ValueError, subprocess.SubprocessError):
            rejection_counts["probe_failed"] += 1
            continue
        if not media.get("has_video"):
            rejection_counts["video_missing"] += 1
            continue
        if not media.get("has_audio"):
            rejection_counts["audio_missing"] += 1
            continue
        actual_duration = float(media.get("duration_seconds") or 0.0)
        if not 2.0 <= actual_duration <= 60.0:
            rejection_counts["actual_duration_out_of_range"] += 1
            continue
        expected_duration = float(row.get("duration_seconds") or 0.0)
        if abs(actual_duration - expected_duration) > max(1.0, expected_duration * 0.05):
            rejection_counts["database_media_duration_mismatch"] += 1
            continue
        probed_rows.append({**row, "media": media, "actual_duration_seconds": actual_duration})

    pairs, account_capacities = select_matched_propagation_pairs(
        probed_rows,
        pair_count=pair_count,
        min_accounts=min_accounts,
        max_duration_delta_seconds=max_duration_delta_seconds,
    )
    content_hashes: set[str] = set()
    clips: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs, start=1):
        pair_id = f"propagation-validation-{index:03d}"
        high_left = int(hashlib.sha256(pair_id.encode("utf-8")).hexdigest()[:2], 16) % 2 == 0
        ordered = [pair["high"], pair["low"]] if high_left else [pair["low"], pair["high"]]
        pair_clip_ids: list[str] = []
        for side, row in zip(("left", "right"), ordered):
            source = Path(row["media_path"])
            source_sha256 = _file_sha256(source)
            if source_sha256 in content_hashes:
                raise ValueError("selected media SHA-256 values must be unique")
            content_hashes.add(source_sha256)
            sample_id = str(row["id"])
            pair_clip_ids.append(sample_id)
            media = row["media"]
            title = str(row.get("title") or "")
            stable_title = _stable_title_key(title)
            clips.append(
                {
                    "sample_id": sample_id,
                    "source_pair_id": pair_id,
                    "side": side,
                    "diagnostic_role": "account_isolated_propagation_feature_pair",
                    "account_id": row.get("account_id") or "",
                    "dataset_id": row.get("dataset_id") or "",
                    "platform_item_id": str(row.get("platform_item_id") or ""),
                    "performance_label": row.get("performance_label") or "",
                    "normalized_reward": round(float(row.get("normalized_reward") or 0.0), 6),
                    "reward_proxy": round(float(row.get("reward_proxy") or 0.0), 6),
                    "visible_engagement": {
                        "likes": int(row.get("likes") or 0),
                        "comments": int(row.get("comments") or 0),
                        "favorites": int(row.get("favorites") or 0),
                        "shares": int(row.get("shares") or 0),
                    },
                    "views": int(row.get("views") or 0) or None,
                    "follows": int(row.get("follows") or 0) or None,
                    "five_second_retention": None,
                    "average_watch_ratio": None,
                    "completion_rate": None,
                    "publication_age_bucket": row["publication_age_bucket"],
                    "duration_bucket": row["duration_bucket"],
                    "content_category": row.get("content_category") or "unknown",
                    "program_name": row.get("program_name") or "",
                    "published_at": row.get("published_at") or "",
                    "title_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
                    "stable_title_key_sha256": hashlib.sha256(
                        stable_title.encode("utf-8")
                    ).hexdigest(),
                    "local_path": _relative_path(source, resolved_repo),
                    "remote_filename": str(source.relative_to(resolved_media)),
                    "sha256": source_sha256,
                    "size_bytes": source.stat().st_size,
                    "duration_seconds": round(float(media["duration_seconds"]), 3),
                    "video_codec": media.get("video_codec") or "",
                    "audio_codec": media.get("audio_codec") or "",
                    "width": int(media.get("width") or 0),
                    "height": int(media.get("height") or 0),
                }
            )
        pair_rows.append(
            {
                "pair_id": pair_id,
                "account_id": pair["account_id"],
                "left_sample_id": pair_clip_ids[0],
                "right_sample_id": pair_clip_ids[1],
                "duration_delta_seconds": round(float(pair["duration_delta_seconds"]), 4),
                "normalized_reward_gap": round(float(pair["normalized_reward_gap"]), 4),
                "reward_proxy_ratio": round(float(pair["reward_proxy_ratio"]), 4),
            }
        )

    selected_ids = {str(item["platform_item_id"]) for item in clips}
    broad_overlap = _benchmark_overlap_summary(
        selected_ids,
        benchmark_dir,
        excluded_manifest_paths,
    )
    resolved_manifest_id = str(
        manifest_id
        or f"dso-omni-propagation-account-holdout-{datetime.now(timezone.utc).strftime('%Y%m%d')}-r1"
    )
    account_counts = Counter(str(item["account_id"]) for item in clips)
    manifest: dict[str, Any] = {
        "contract_version": PROPAGATION_VALIDATION_MANIFEST_VERSION,
        "manifest_id": resolved_manifest_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "goal_alignment": ["G1", "G3"],
        "admission_status": "research_only",
        "evaluation_validity": "outcome_enriched_pair_selection_with_leave_one_account_out_evaluation",
        "outcome_target": "visible_engagement_reward_proxy_v2_not_views",
        "selection_policy": {
            "pair_count": len(pairs),
            "clip_count": len(clips),
            "account_count": len(account_counts),
            "account_clip_counts": dict(sorted(account_counts.items())),
            "same_account": True,
            "same_publication_age_bucket": True,
            "same_duration_bucket": True,
            "same_content_category": True,
            "maximum_duration_delta_seconds": max_duration_delta_seconds,
            "minimum_normalized_reward_gap": 55.0,
            "minimum_reward_proxy_ratio": 1.8,
            "provider_blinding": (
                "account, title, labels, interaction outcomes and prior strategy results are "
                "excluded from provider requests"
            ),
            "evaluation_split": "leave_one_account_out",
            "label_version": RESEARCH_LABEL_VERSION,
            "candidate_count_before_media_probe": len(rows),
            "candidate_count_after_media_probe": len(probed_rows),
            "media_rejection_counts": dict(rejection_counts),
            "account_pair_capacities": account_capacities,
        },
        "leakage_guard": {
            "excluded_manifest_ids": excluded["manifest_ids"],
            "excluded_platform_item_count": len(excluded["platform_item_ids"]),
            "excluded_stable_title_hash_count": len(excluded["stable_title_hashes"]),
            "selected_overlap_with_excluded_platform_items": len(
                selected_ids & excluded["platform_item_ids"]
            ),
            "selected_media_sha_unique": len(content_hashes) == len(clips),
            "broader_prior_benchmark_overlap": broad_overlap,
            "interpretation": (
                "Some clips may have appeared in earlier representative-frame or embedding research. "
                "No clip has appeared in the excluded complete-video Omni manifests."
            ),
        },
        "outcome_availability": {
            "visible_engagement_heat_proxy": 1.0,
            "share_rate": sum(bool(item.get("views")) for item in clips) / max(1, len(clips)),
            "follow_conversion_rate": sum(
                bool(item.get("views")) and item.get("follows") is not None for item in clips
            )
            / max(1, len(clips)),
            "watch_quality": 0.0,
        },
        "pairs": pair_rows,
        "clips": clips,
        "production_impact": {
            "production_weight_changed": False,
            "writes_manual_gold": False,
            "automatic_export": False,
            "automatic_publish": False,
        },
    }
    manifest["manifest_sha256"] = stable_json_sha256(manifest)
    return manifest


def select_matched_propagation_pairs(
    rows: list[dict[str, Any]],
    *,
    pair_count: int,
    min_accounts: int,
    max_duration_delta_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("account_id") or "")].append(row)
    account_pairs: dict[str, list[dict[str, Any]]] = {}
    for account_id, account_rows in grouped.items():
        highs = [row for row in account_rows if row.get("performance_label") == "high"]
        lows = [row for row in account_rows if row.get("performance_label") == "low"]
        edges: list[tuple[Any, ...]] = []
        for high in highs:
            for low in lows:
                if high.get("publication_age_bucket") != low.get("publication_age_bucket"):
                    continue
                if high.get("duration_bucket") != low.get("duration_bucket"):
                    continue
                if (high.get("content_category") or "unknown") != (
                    low.get("content_category") or "unknown"
                ):
                    continue
                duration_delta = abs(
                    float(high.get("actual_duration_seconds") or high.get("duration_seconds") or 0.0)
                    - float(low.get("actual_duration_seconds") or low.get("duration_seconds") or 0.0)
                )
                normalized_gap = float(high.get("normalized_reward") or 0.0) - float(
                    low.get("normalized_reward") or 0.0
                )
                ratio = float(high.get("reward_proxy") or 0.0) / max(
                    0.01, float(low.get("reward_proxy") or 0.0)
                )
                if (
                    duration_delta > max_duration_delta_seconds
                    or normalized_gap < 55.0
                    or ratio < 1.8
                ):
                    continue
                edges.append(
                    (
                        duration_delta,
                        -normalized_gap,
                        -ratio,
                        str(high.get("id") or ""),
                        str(low.get("id") or ""),
                        high,
                        low,
                    )
                )
        edges.sort(key=lambda item: item[:5])
        used_ids: set[str] = set()
        used_titles: set[str] = set()
        matched: list[dict[str, Any]] = []
        for edge in edges:
            high, low = edge[-2], edge[-1]
            high_id = str(high.get("id") or "")
            low_id = str(low.get("id") or "")
            high_title = _stable_title_key(high.get("title"))
            low_title = _stable_title_key(low.get("title"))
            if high_id in used_ids or low_id in used_ids:
                continue
            if not high_title or not low_title or high_title == low_title:
                continue
            if high_title in used_titles or low_title in used_titles:
                continue
            used_ids.update({high_id, low_id})
            used_titles.update({high_title, low_title})
            matched.append(
                {
                    "account_id": account_id,
                    "high": high,
                    "low": low,
                    "duration_delta_seconds": edge[0],
                    "normalized_reward_gap": -float(edge[1]),
                    "reward_proxy_ratio": -float(edge[2]),
                }
            )
        if matched:
            account_pairs[account_id] = matched

    capacities = {account: len(items) for account, items in sorted(account_pairs.items())}
    if len(account_pairs) < min_accounts:
        raise ValueError(
            f"only {len(account_pairs)} accounts have matched pairs; {min_accounts} required"
        )
    if sum(capacities.values()) < pair_count:
        raise ValueError(
            f"only {sum(capacities.values())} matched pairs are available; {pair_count} required"
        )

    queues = {account: list(items) for account, items in account_pairs.items()}
    selected: list[dict[str, Any]] = []
    selected_counts: Counter[str] = Counter()
    globally_used_titles: set[str] = set()
    accounts = sorted(queues)
    while len(selected) < pair_count:
        available = [account for account in accounts if queues[account]]
        if not available:
            break
        available.sort(key=lambda account: (selected_counts[account], -len(queues[account]), account))
        added = False
        for account in available:
            while queues[account]:
                pair = queues[account].pop(0)
                title_keys = {
                    _stable_title_key(pair["high"].get("title")),
                    _stable_title_key(pair["low"].get("title")),
                }
                if "" in title_keys or title_keys & globally_used_titles:
                    continue
                globally_used_titles.update(title_keys)
                selected.append(pair)
                selected_counts[account] += 1
                added = True
                break
            if added:
                break
        if not added:
            break
    if len(selected) < pair_count:
        raise ValueError(
            f"global title leakage guard leaves {len(selected)} pairs; {pair_count} required"
        )
    if len(selected_counts) < min_accounts:
        raise ValueError(
            f"selected pairs cover {len(selected_counts)} accounts; {min_accounts} required"
        )
    return selected, capacities


def evaluate_propagation_account_holdout(
    manifest_path: Path,
    feature_report_path: Path,
    *,
    db_path: Path,
    omni_weight: float = DEFAULT_OMNI_WEIGHT,
    top_k: int = DEFAULT_TOP_K,
    baseline_score_builder: Callable[[list[dict[str, Any]]], dict[str, float]] | None = None,
) -> dict[str, Any]:
    if not 0.0 <= omni_weight <= 0.5:
        raise ValueError("omni_weight must be between 0 and 0.5")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    feature_report = json.loads(feature_report_path.read_text(encoding="utf-8"))
    clips = [item for item in manifest.get("clips") or [] if isinstance(item, dict)]
    results = [item for item in feature_report.get("clips") or [] if isinstance(item, dict)]
    if str(feature_report.get("source_manifest_id") or "") != str(
        manifest.get("manifest_id") or ""
    ):
        raise ValueError("feature report source manifest ID does not match")
    if str(feature_report.get("source_manifest_sha256") or "") != _file_sha256(manifest_path):
        raise ValueError("feature report source manifest SHA-256 does not match")
    result_by_id = {str(item.get("sample_id") or ""): item for item in results}
    successful = {
        sample_id: item
        for sample_id, item in result_by_id.items()
        if item.get("provider_status") in {"shadow_succeeded", "shadow_cached"}
        and isinstance(item.get("provider_output"), dict)
        and item.get("provider_output")
    }
    if len(successful) != len(clips):
        missing = sorted(
            str(item.get("sample_id") or "")
            for item in clips
            if str(item.get("sample_id") or "") not in successful
        )
        raise ValueError(f"feature coverage is incomplete for {len(missing)} samples")

    clip_by_id = {str(item.get("sample_id") or ""): item for item in clips}
    selected_rows = _selected_database_rows(db_path, set(clip_by_id))
    baseline_scores = (
        baseline_score_builder(selected_rows)
        if baseline_score_builder
        else _account_isolated_v24_scores(selected_rows, db_path=db_path)
    )
    if set(baseline_scores) != set(clip_by_id):
        raise ValueError("v2.4 account-isolated score coverage is incomplete")

    omni_scores: dict[str, float] = {}
    fold_summaries: list[dict[str, Any]] = []
    accounts = sorted({str(item.get("account_id") or "") for item in clips})
    for held_account in accounts:
        train_ids = [
            sample_id
            for sample_id, clip in clip_by_id.items()
            if str(clip.get("account_id") or "") != held_account
        ]
        eval_ids = [
            sample_id
            for sample_id, clip in clip_by_id.items()
            if str(clip.get("account_id") or "") == held_account
        ]
        model = _fit_feature_log_odds(
            [(clip_by_id[sample_id], successful[sample_id]["provider_output"]) for sample_id in train_ids]
        )
        for sample_id in eval_ids:
            omni_scores[sample_id] = _feature_log_odds_score(
                successful[sample_id]["provider_output"], model
            )
        fold_summaries.append(
            {
                "held_account": held_account,
                "training_account_count": len(accounts) - 1,
                "training_sample_count": len(train_ids),
                "evaluation_sample_count": len(eval_ids),
                "feature_value_count": int(model["feature_value_count"]),
            }
        )

    strategies = {
        "research_ranker_v2_4_account_isolated": baseline_scores,
        "omni_factual_features_account_isolated": omni_scores,
        "v2_4_plus_omni_factual_fixed_15": {
            sample_id: round(
                (1.0 - omni_weight) * float(baseline_scores[sample_id])
                + omni_weight * float(omni_scores[sample_id]),
                6,
            )
            for sample_id in clip_by_id
        },
    }
    pair_metrics = {
        name: _paired_strategy_metrics(manifest, scores) for name, scores in strategies.items()
    }
    ranking_metrics = {
        name: _balanced_ranking_metrics(clips, scores, top_k=top_k)
        for name, scores in strategies.items()
    }
    baseline_name = "research_ranker_v2_4_account_isolated"
    combined_name = "v2_4_plus_omni_factual_fixed_15"
    per_account = _per_account_pair_metrics(manifest, strategies)
    comparison_diagnostics = _paired_comparison_diagnostics(
        manifest,
        pair_metrics[baseline_name]["pairs"],
        pair_metrics[combined_name]["pairs"],
    )
    ready_accounts = [item for item in per_account if int(item["pair_count"]) >= 3]
    account_wins = sum(
        float(item[combined_name]) > float(item[baseline_name]) for item in ready_accounts
    )
    account_regressions = sum(
        float(item[combined_name]) < float(item[baseline_name]) for item in ready_accounts
    )
    baseline_accuracy = float(pair_metrics[baseline_name]["pair_accuracy"])
    combined_accuracy = float(pair_metrics[combined_name]["pair_accuracy"])
    ready_baseline_macro = _mean(
        [float(item[baseline_name]) for item in ready_accounts]
    )
    ready_combined_macro = _mean(
        [float(item[combined_name]) for item in ready_accounts]
    )
    gate_passed = (
        len(clips) >= 60
        and len(accounts) >= 8
        and combined_accuracy >= baseline_accuracy + 0.05
        and ready_combined_macro >= ready_baseline_macro + 0.05
        and account_wins >= 3
        and account_regressions == 0
    )
    report: dict[str, Any] = {
        "contract_version": PROPAGATION_VALIDATION_REPORT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "completed",
        "admission_status": "research_only",
        "goal_alignment": ["G1", "G3"],
        "manifest_id": manifest.get("manifest_id"),
        "manifest_file_sha256": _file_sha256(manifest_path),
        "feature_report_sha256": _file_sha256(feature_report_path),
        "split_policy": {
            "name": "leave_one_account_out",
            "account_count": len(accounts),
            "sample_count": len(clips),
            "pair_count": len(manifest.get("pairs") or []),
            "folds": fold_summaries,
            "platform_item_overlap_between_train_and_eval": 0,
            "feature_outcomes_used_only_from_other_accounts": True,
        },
        "fixed_configuration": {
            "baseline": baseline_name,
            "feature_strategy": "categorical_laplace_log_odds",
            "omni_weight": omni_weight,
            "v2_4_weight": 1.0 - omni_weight,
            "top_k": top_k,
            "weight_search_performed": False,
        },
        "feature_coverage": {
            "successful_count": len(successful),
            "sample_count": len(clips),
            "coverage": len(successful) / max(1, len(clips)),
        },
        "pair_metrics": pair_metrics,
        "ranking_metrics": ranking_metrics,
        "per_account_metrics": per_account,
        "comparison_diagnostics": comparison_diagnostics,
        "promotion_gate": {
            "passed": gate_passed,
            "status": "eligible_for_new_proxy_holdout" if gate_passed else "keep_v2_4",
            "required_pair_accuracy_delta": 0.05,
            "actual_pair_accuracy_delta": round(combined_accuracy - baseline_accuracy, 6),
            "ready_account_count": len(ready_accounts),
            "required_ready_account_macro_delta": 0.05,
            "baseline_ready_account_macro_accuracy": round(ready_baseline_macro, 6),
            "combined_ready_account_macro_accuracy": round(ready_combined_macro, 6),
            "actual_ready_account_macro_delta": round(
                ready_combined_macro - ready_baseline_macro, 6
            ),
            "ready_account_wins": account_wins,
            "ready_account_regressions": account_regressions,
            "production_promotion_allowed": False,
            "reason": (
                "Passing only allows a new frozen proxy holdout. True platform share, follow and "
                "watch-quality outcomes remain unavailable."
            ),
        },
        "outcome_availability": (feature_report.get("evaluation") or {}).get(
            "outcome_availability"
        )
        or manifest.get("outcome_availability"),
        "usage": {
            "network_request_count": int(feature_report.get("network_request_count") or 0),
            "usage_estimated_cost_cny": str(
                feature_report.get("usage_estimated_cost_cny") or "0"
            ),
            "latency_ms": (feature_report.get("evaluation") or {}).get("latency_ms") or {},
        },
        "interpretation": {
            "outcome": "visible_engagement_proxy_not_views",
            "causal_claim_allowed": False,
            "selection_is_outcome_enriched": True,
            "prior_non_full_media_benchmark_overlap": (
                (manifest.get("leakage_guard") or {}).get("broader_prior_benchmark_overlap")
                or {}
            ),
        },
        "production_impact": {
            "production_weight_changed": False,
            "writes_manual_gold": False,
            "automatic_export": False,
            "automatic_publish": False,
        },
    }
    report["report_sha256"] = stable_json_sha256(report)
    return report


def merge_propagation_feature_reports(
    manifest_path: Path,
    feature_report_paths: list[Path],
) -> dict[str, Any]:
    if not feature_report_paths:
        raise ValueError("at least one feature report is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clips = [item for item in manifest.get("clips") or [] if isinstance(item, dict)]
    expected_ids = [str(item.get("sample_id") or "") for item in clips]
    if not expected_ids or len(set(expected_ids)) != len(expected_ids):
        raise ValueError("manifest sample IDs must be non-empty and unique")
    expected_id_set = set(expected_ids)

    successful: dict[str, dict[str, Any]] = {}
    latest: dict[str, dict[str, Any]] = {}
    source_reports: list[dict[str, Any]] = []
    total_network_requests = 0
    total_usage_cost = 0.0
    output_token_limits: set[int] = set()
    provider_identity: tuple[str, str, str] | None = None
    for path in feature_report_paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        provider = report.get("provider") or {}
        identity = (
            str(provider.get("provider_id") or ""),
            str(provider.get("model_id") or ""),
            str(provider.get("prompt_version") or ""),
        )
        if provider_identity is None:
            provider_identity = identity
        elif identity != provider_identity:
            raise ValueError("feature reports must use the same provider/model/prompt")
        output_limit = int((report.get("feature_policy") or {}).get("output_token_limit") or 1200)
        output_token_limits.add(output_limit)
        total_network_requests += int(report.get("network_request_count") or 0)
        total_usage_cost += float(report.get("usage_estimated_cost_cny") or 0.0)
        source_reports.append(
            {
                "path": str(path),
                "sha256": _file_sha256(path),
                "experiment_id": report.get("experiment_id"),
                "source_manifest_id": report.get("source_manifest_id"),
                "source_manifest_sha256": report.get("source_manifest_sha256"),
                "status": report.get("status"),
                "output_token_limit": output_limit,
                "network_request_count": int(report.get("network_request_count") or 0),
                "usage_estimated_cost_cny": str(
                    report.get("usage_estimated_cost_cny") or "0"
                ),
            }
        )
        for item in report.get("clips") or []:
            if not isinstance(item, dict):
                continue
            sample_id = str(item.get("sample_id") or "")
            if sample_id not in expected_id_set:
                raise ValueError(f"feature report contains unknown sample {sample_id}")
            latest[sample_id] = item
            if item.get("provider_status") in {"shadow_succeeded", "shadow_cached"}:
                successful[sample_id] = item

    merged_clips = [successful.get(sample_id) or latest.get(sample_id) for sample_id in expected_ids]
    missing = [sample_id for sample_id, item in zip(expected_ids, merged_clips) if item is None]
    if missing:
        raise ValueError(f"feature reports are missing {len(missing)} manifest samples")
    merged = [item for item in merged_clips if isinstance(item, dict)]
    complete = all(
        item.get("provider_status") in {"shadow_succeeded", "shadow_cached"}
        and isinstance(item.get("provider_output"), dict)
        and item.get("provider_output")
        for item in merged
    )
    report: dict[str, Any] = {
        "contract_version": "bailian_propagation_feature_report_merge.v1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "completed" if complete else "partial",
        "admission_status": "research_only",
        "goal_alignment": ["G1", "G3"],
        "source_manifest_id": manifest.get("manifest_id") or manifest.get("benchmark_id"),
        "source_manifest_sha256": _file_sha256(manifest_path),
        "merge_policy": {
            "successful_result_precedence": True,
            "sample_order": "frozen_manifest",
            "source_reports": source_reports,
            "output_token_limits": sorted(output_token_limits),
        },
        "provider": {
            "provider_id": provider_identity[0] if provider_identity else "",
            "model_id": provider_identity[1] if provider_identity else "",
            "prompt_version": provider_identity[2] if provider_identity else "",
        },
        "clips": merged,
        "evaluation": evaluate_propagation_feature_results(merged),
        "network_request_count": total_network_requests,
        "usage_estimated_cost_cny": f"{total_usage_cost:.6f}",
        "production_impact": {
            "production_weight_changed": False,
            "writes_manual_gold": False,
            "automatic_export": False,
            "automatic_publish": False,
        },
    }
    report["report_sha256"] = stable_json_sha256(report)
    return report


def _account_isolated_v24_scores(
    rows: list[dict[str, Any]], *, db_path: Path
) -> dict[str, float]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        all_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT *
                FROM historical_capture_samples
                WHERE COALESCE(platform_item_id, '') != ''
                  AND (COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0)
                  AND research_label_version = ?
                ORDER BY updated_at DESC, account_id ASC, published_at ASC,
                         platform_item_id ASC, id ASC
                """,
                (RESEARCH_LABEL_VERSION,),
            ).fetchall()
        ]
    scores: dict[str, float] = {}
    accounts = sorted({str(row.get("account_id") or "") for row in rows})
    for held_account in accounts:
        eval_rows = [row for row in rows if str(row.get("account_id") or "") == held_account]
        train_rows = [
            row for row in all_rows if str(row.get("account_id") or "") != held_account
        ]
        train_rows, _ = _apply_leakage_guard(train_rows, eval_rows)
        train_basis = _prepare_history_tokens(train_rows)
        eval_basis = _prepare_history_tokens(eval_rows)
        history_index = _history_candidate_index(train_basis)
        baselines = _historical_group_baselines(train_basis)
        thresholds = _interaction_thresholds(train_basis)
        account_profiles = _account_ranker_profiles(train_basis, thresholds=thresholds)
        fold_rows: list[dict[str, Any]] = []
        for row in eval_basis:
            strategy_scores, components = _historical_strategy_scores(
                row,
                train_basis,
                baselines,
                history_index=history_index,
                thresholds=thresholds,
                account_profiles=account_profiles,
            )
            reliable_row = _v24_reliable_signal_row(row)
            signal_quality = _v24_signal_quality(row, reliable_row, components)
            gated_score = _score_v24_from_components(
                components,
                row=row,
                account_profiles=account_profiles,
                config=RESEARCH_RANKER_V24_WEIGHT_CONFIG,
                signal_quality=signal_quality,
            )
            strategy_scores[RESEARCH_RANKER_V24_STRATEGY] = _select_v24_signal_gate_score(
                raw_score=float(strategy_scores.get(RESEARCH_RANKER_V23_STRATEGY) or 0.0),
                gated_score=gated_score,
                raw_components=components,
                gated_components=components,
                signal_quality=signal_quality,
            )
            fold_rows.append(
                {
                    "training_sample_id": row.get("id") or "",
                    "account_id": row.get("account_id") or "",
                    "title": row.get("title") or "",
                    "song_title": row.get("song_title") or "",
                    "artist_names": row.get("artist_names") or "",
                    "content_category": row.get("content_category") or "",
                    "strategy_scores": strategy_scores,
                    "component_scores": {**components, **signal_quality},
                    "v24_component_scores": components,
                    "final_score": strategy_scores[RESEARCH_RANKER_V24_STRATEGY],
                }
            )
        for item in _apply_v24_diversity(
            fold_rows, config=RESEARCH_RANKER_V24_WEIGHT_CONFIG
        ):
            sample_id = str(item.get("training_sample_id") or "")
            strategy_scores = item.get("strategy_scores") or {}
            scores[sample_id] = float(strategy_scores[RESEARCH_RANKER_V24_STRATEGY])
    return scores


def _fit_feature_log_odds(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    label_counts = Counter(str(clip.get("performance_label") or "") for clip, _ in rows)
    value_counts: dict[str, dict[str, Counter[str]]] = {
        name: {"high": Counter(), "low": Counter()} for name in FEATURE_PATHS
    }
    for clip, features in rows:
        label = str(clip.get("performance_label") or "")
        if label not in {"high", "low"}:
            continue
        for name, path in FEATURE_PATHS.items():
            value_counts[name][label][_feature_value(features, path)] += 1
    weights: dict[str, dict[str, float]] = {}
    feature_value_count = 0
    for name, counts in value_counts.items():
        values = sorted(set(counts["high"]) | set(counts["low"]))
        cardinality = max(1, len(values))
        feature_weights: dict[str, float] = {}
        for value in values:
            p_high = (counts["high"][value] + 1.0) / (
                label_counts["high"] + cardinality
            )
            p_low = (counts["low"][value] + 1.0) / (
                label_counts["low"] + cardinality
            )
            feature_weights[value] = max(-1.5, min(1.5, math.log(p_high / p_low)))
        feature_value_count += len(feature_weights)
        weights[name] = feature_weights
    return {
        "weights": weights,
        "label_counts": dict(label_counts),
        "feature_value_count": feature_value_count,
    }


def _feature_log_odds_score(features: dict[str, Any], model: dict[str, Any]) -> float:
    contributions: list[float] = []
    weights = model.get("weights") or {}
    for name, path in FEATURE_PATHS.items():
        value = _feature_value(features, path)
        contributions.append(float((weights.get(name) or {}).get(value) or 0.0))
    mean_contribution = sum(contributions) / max(1, len(contributions))
    return round(clamp(50.0 + mean_contribution * 24.0), 6)


def _paired_strategy_metrics(manifest: dict[str, Any], scores: dict[str, float]) -> dict[str, Any]:
    clip_by_id = {
        str(item.get("sample_id") or ""): item for item in manifest.get("clips") or []
    }
    correct = 0.0
    ties = 0
    rows: list[dict[str, Any]] = []
    for pair in manifest.get("pairs") or []:
        left_id = str(pair.get("left_sample_id") or "")
        right_id = str(pair.get("right_sample_id") or "")
        left = clip_by_id[left_id]
        right = clip_by_id[right_id]
        expected = left_id if left.get("performance_label") == "high" else right_id
        left_score = float(scores[left_id])
        right_score = float(scores[right_id])
        if math.isclose(left_score, right_score, abs_tol=1e-9):
            selected = "tie"
            correct += 0.5
            ties += 1
        else:
            selected = left_id if left_score > right_score else right_id
            correct += float(selected == expected)
        rows.append(
            {
                "pair_id": pair.get("pair_id"),
                "account_id": pair.get("account_id"),
                "expected_sample_id": expected,
                "selected_sample_id": selected,
                "left_score": round(left_score, 6),
                "right_score": round(right_score, 6),
                "correct": selected == expected if selected != "tie" else None,
            }
        )
    pair_count = len(rows)
    return {
        "pair_count": pair_count,
        "correct_credit": round(correct, 4),
        "tie_count": ties,
        "pair_accuracy": round(correct / max(1, pair_count), 6),
        "pairs": rows,
    }


def _per_account_pair_metrics(
    manifest: dict[str, Any], strategies: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in manifest.get("pairs") or []:
        by_account[str(pair.get("account_id") or "")].append(pair)
    clips = manifest.get("clips") or []
    result: list[dict[str, Any]] = []
    for account, pairs in sorted(by_account.items()):
        subset_ids = {
            str(value)
            for pair in pairs
            for value in (pair.get("left_sample_id"), pair.get("right_sample_id"))
        }
        subset = {
            **manifest,
            "pairs": pairs,
            "clips": [item for item in clips if str(item.get("sample_id") or "") in subset_ids],
        }
        row: dict[str, Any] = {"account_id": account, "pair_count": len(pairs)}
        for name, scores in strategies.items():
            row[name] = _paired_strategy_metrics(subset, scores)["pair_accuracy"]
        result.append(row)
    return result


def _paired_comparison_diagnostics(
    manifest: dict[str, Any],
    baseline_pairs: list[dict[str, Any]],
    combined_pairs: list[dict[str, Any]],
    *,
    bootstrap_iterations: int = 2_000,
) -> dict[str, Any]:
    baseline = {str(item.get("pair_id") or ""): item for item in baseline_pairs}
    combined = {str(item.get("pair_id") or ""): item for item in combined_pairs}
    deltas_by_account: dict[str, list[float]] = defaultdict(list)
    corrected = 0
    harmed = 0
    unchanged = 0
    changed_pairs: list[dict[str, Any]] = []
    for pair in manifest.get("pairs") or []:
        pair_id = str(pair.get("pair_id") or "")
        account = str(pair.get("account_id") or "")
        baseline_credit = _pair_correct_credit(baseline[pair_id])
        combined_credit = _pair_correct_credit(combined[pair_id])
        delta = combined_credit - baseline_credit
        deltas_by_account[account].append(delta)
        if delta > 0:
            corrected += 1
        elif delta < 0:
            harmed += 1
        else:
            unchanged += 1
        if delta or baseline[pair_id].get("selected_sample_id") != combined[pair_id].get(
            "selected_sample_id"
        ):
            changed_pairs.append(
                {
                    "pair_id": pair_id,
                    "account_id": account,
                    "baseline_correct_credit": baseline_credit,
                    "combined_correct_credit": combined_credit,
                    "baseline_selected_sample_id": baseline[pair_id].get(
                        "selected_sample_id"
                    ),
                    "combined_selected_sample_id": combined[pair_id].get(
                        "selected_sample_id"
                    ),
                }
            )

    discordant = corrected + harmed
    exact_p = _two_sided_sign_test_p_value(corrected, harmed)
    accounts = sorted(deltas_by_account)
    seed = int(
        stable_json_sha256(
            {
                "manifest_id": manifest.get("manifest_id"),
                "accounts": accounts,
                "bootstrap_iterations": bootstrap_iterations,
            }
        )[:16],
        16,
    )
    rng = random.Random(seed)
    bootstrap_deltas: list[float] = []
    if accounts:
        for _ in range(bootstrap_iterations):
            sampled = [accounts[rng.randrange(len(accounts))] for _ in accounts]
            values = [delta for account in sampled for delta in deltas_by_account[account]]
            bootstrap_deltas.append(_mean(values))
    bootstrap_deltas.sort()
    return {
        "decision_change_count": len(changed_pairs),
        "corrected_pair_count": corrected,
        "harmed_pair_count": harmed,
        "unchanged_pair_count": unchanged,
        "discordant_pair_count": discordant,
        "two_sided_exact_sign_test_p_value": round(exact_p, 6),
        "account_cluster_bootstrap_iterations": bootstrap_iterations,
        "account_cluster_bootstrap_95_ci": [
            round(_percentile(bootstrap_deltas, 0.025), 6),
            round(_percentile(bootstrap_deltas, 0.975), 6),
        ],
        "changed_pairs": changed_pairs,
    }


def _pair_correct_credit(row: dict[str, Any]) -> float:
    if row.get("selected_sample_id") == "tie":
        return 0.5
    return float(row.get("selected_sample_id") == row.get("expected_sample_id"))


def _two_sided_sign_test_p_value(corrected: int, harmed: int) -> float:
    discordant = corrected + harmed
    if discordant == 0:
        return 1.0
    tail = min(corrected, harmed)
    probability = sum(math.comb(discordant, value) for value in range(tail + 1)) / (
        2**discordant
    )
    return min(1.0, 2.0 * probability)


def _balanced_ranking_metrics(
    clips: list[dict[str, Any]], scores: dict[str, float], *, top_k: int
) -> dict[str, Any]:
    ranked = sorted(
        clips,
        key=lambda item: (
            float(scores[str(item.get("sample_id") or "")]),
            str(item.get("sample_id") or ""),
        ),
        reverse=True,
    )
    k = min(max(1, top_k), len(ranked))
    top = ranked[:k]
    high_hits = sum(item.get("performance_label") == "high" for item in top)
    high_rate = high_hits / max(1, k)
    gains = [1.0 if item.get("performance_label") == "high" else 0.0 for item in ranked]
    ideal = sorted(gains, reverse=True)
    dcg = sum(value / math.log2(index + 2) for index, value in enumerate(gains[:k]))
    idcg = sum(value / math.log2(index + 2) for index, value in enumerate(ideal[:k]))
    return {
        "sample_count": len(ranked),
        "k": k,
        "high_interaction_hit_rate": round(high_rate, 6),
        "topk_lift_vs_balanced_random": round(high_rate / 0.5, 6),
        "ndcg_at_k": round(dcg / idcg, 6) if idcg else 0.0,
    }


def _selected_database_rows(db_path: Path, sample_ids: set[str]) -> list[dict[str, Any]]:
    if not sample_ids:
        return []
    placeholders = ",".join("?" for _ in sample_ids)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT * FROM historical_capture_samples WHERE id IN ({placeholders})",
            sorted(sample_ids),
        ).fetchall()
    result = [dict(row) for row in rows]
    if len(result) != len(sample_ids):
        raise ValueError("manifest samples are missing from historical_capture_samples")
    return result


def _eligible_rows(
    db_path: Path,
    media_index: dict[tuple[str, str], Path],
    excluded: dict[str, Any],
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        source_rows = connection.execute(
            """
            SELECT * FROM historical_capture_samples
            WHERE performance_label IN ('high', 'low')
              AND research_label_version = ?
              AND duration_seconds BETWEEN 2 AND 60
              AND platform_item_id != ''
              AND reward_proxy > 0
            ORDER BY account_id, published_at, platform_item_id, id
            """,
            (RESEARCH_LABEL_VERSION,),
        ).fetchall()
    rows: list[dict[str, Any]] = []
    for source in source_rows:
        row = dict(source)
        account = str(row.get("account_id") or "")
        platform_item_id = str(row.get("platform_item_id") or "")
        media_path = media_index.get((account, platform_item_id))
        if media_path is None or platform_item_id in excluded["platform_item_ids"]:
            continue
        stable_title_hash = hashlib.sha256(
            _stable_title_key(row.get("title")).encode("utf-8")
        ).hexdigest()
        if stable_title_hash in excluded["stable_title_hashes"]:
            continue
        age_bucket, duration_bucket = _label_buckets(row.get("label_reason"))
        if age_bucket == "unknown" or duration_bucket == "unknown":
            continue
        rows.append(
            {
                **row,
                "media_path": str(media_path),
                "publication_age_bucket": age_bucket,
                "duration_bucket": duration_bucket,
            }
        )
    return rows


def _media_index(media_root: Path) -> dict[tuple[str, str], Path]:
    candidates: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for path in media_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _MEDIA_SUFFIXES:
            continue
        relative = path.relative_to(media_root)
        if len(relative.parts) < 2:
            continue
        candidates[(relative.parts[0], path.stem)].append(path)
    result: dict[tuple[str, str], Path] = {}
    for key, paths in candidates.items():
        result[key] = sorted(paths, key=lambda path: (len(path.parts), str(path)))[0]
    return result


def _probe_media(path: Path, ffprobe_path: str | None = None) -> dict[str, Any]:
    executable = ffprobe_path or shutil.which("ffprobe")
    if not executable:
        raise OSError("ffprobe is unavailable")
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration_values = [
        (payload.get("format") or {}).get("duration"),
        video.get("duration"),
        audio.get("duration"),
    ]
    duration = next(
        (
            float(value)
            for value in duration_values
            if value not in (None, "N/A") and math.isfinite(float(value)) and float(value) > 0
        ),
        0.0,
    )
    return {
        "has_video": bool(video),
        "has_audio": bool(audio),
        "duration_seconds": duration,
        "video_codec": video.get("codec_name") or "",
        "audio_codec": audio.get("codec_name") or "",
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def _excluded_evidence(paths: list[Path]) -> dict[str, Any]:
    platform_item_ids: set[str] = set()
    stable_title_hashes: set[str] = set()
    manifest_ids: list[str] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest_ids.append(str(payload.get("manifest_id") or payload.get("benchmark_id") or path.stem))
        stack: list[Any] = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                platform_item_id = value.get("platform_item_id")
                if platform_item_id:
                    platform_item_ids.add(str(platform_item_id))
                stable_hash = value.get("stable_title_key_sha256")
                if stable_hash:
                    stable_title_hashes.add(str(stable_hash))
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return {
        "platform_item_ids": platform_item_ids,
        "stable_title_hashes": stable_title_hashes,
        "manifest_ids": sorted(manifest_ids),
    }


def _benchmark_overlap_summary(
    selected_ids: set[str],
    benchmark_dir: Path | None,
    excluded_paths: list[Path],
) -> dict[str, Any]:
    if benchmark_dir is None or not benchmark_dir.exists():
        return {"platform_item_count": 0, "benchmark_ids": []}
    excluded_resolved = {path.resolve() for path in excluded_paths}
    overlaps: dict[str, int] = {}
    overlapping_platform_items: set[str] = set()
    for path in sorted(benchmark_dir.glob("*.json")):
        if path.resolve() in excluded_resolved:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found: set[str] = set()
        stack: list[Any] = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if value.get("platform_item_id"):
                    found.add(str(value["platform_item_id"]))
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        count = len(found & selected_ids)
        if count:
            benchmark_id = str(
                payload.get("benchmark_id") or payload.get("manifest_id") or path.stem
            )
            overlaps[benchmark_id] = count
            overlapping_platform_items.update(found & selected_ids)
    return {
        "platform_item_count": len(overlapping_platform_items),
        "benchmark_ids": sorted(overlaps),
        "counts_by_benchmark": dict(sorted(overlaps.items())),
    }


def _feature_value(features: dict[str, Any], path: tuple[str, ...]) -> str:
    value: Any = features
    for part in path:
        value = value.get(part) if isinstance(value, dict) else None
    return str(value if value is not None else "unknown")


def _label_buckets(reason: Any) -> tuple[str, str]:
    text = str(reason or "")
    age = _AGE_BUCKET_PATTERN.search(text)
    duration = _DURATION_BUCKET_PATTERN.search(text)
    return (
        age.group(1) if age else "unknown",
        duration.group(1) if duration else "unknown",
    )


def _stable_title_key(value: Any) -> str:
    text = _TITLE_CLEANUP.sub("", str(value or "").strip().lower())
    text = re.sub(r"\d+", "#", text)
    return text[:160]


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError as exc:
        raise ValueError("media path must be inside repo_root") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight
