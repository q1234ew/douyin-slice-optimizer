from __future__ import annotations

import hashlib
import math
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from dso.config import ensure_data_dirs
from dso.learning.bailian_cached_ablation import (
    _balanced_accuracy,
    _evaluation_rows,
    _vectors_for_modality,
)
from dso.learning.bailian_failure_attribution import (
    _retrieval_profiles,
    _retrieval_summary,
    _verify_artifacts,
)
from dso.learning.bailian_vector_chain import (
    _cloud_records,
    _load_stage_report,
    _manifest_samples,
    _persist_stage_report,
)
from dso.learning.multimodal_vector_value import (
    DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    load_multimodal_vector_manifest,
)
from dso.providers.aliyun_bailian import jpeg_dimensions
from dso.providers.contracts import stable_json_sha256
from dso.utils import read_json, utc_now, write_json


BAILIAN_EVIDENCE_QUALITY_VERSION = "bailian_evidence_quality_reconstruction.v1"
EVIDENCE_PACK_VERSION = "dso-bailian-three-window-evidence.v1"
EVIDENCE_VECTOR_INPUT_VERSION = "dso-bailian-vector-input.d12c1"
EVIDENCE_STAGE = "evidence-quality-reconstruction"
EVIDENCE_WINDOW_SECONDS = 15.0
EVIDENCE_FRAME_MODALITY = "fusion_d12c1"
WINDOW_ROLES = ("hook", "middle", "payoff")
REFERENCE_SCOPE_ORDER = ("same_account", "same_program", "same_material", "global")
REFERENCE_SCOPE_QUALITY = {
    "same_account": 1.0,
    "same_program": 0.75,
    "same_material": 0.5,
    "global": 0.2,
}


def run_bailian_evidence_quality_reconstruction(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    *,
    scope: str = "holdout",
    limit: int = 40,
    force: bool = False,
) -> dict:
    """Build local three-window evidence and evaluate stratified cached retrieval."""

    selected_scope = str(scope or "holdout").strip().lower()
    if selected_scope not in {"holdout", "holdout_and_references", "all"}:
        raise ValueError("scope must be holdout, holdout_and_references, or all")
    selected_limit = max(1, min(240, int(limit or 40)))

    manifest = load_multimodal_vector_manifest(benchmark_id)
    config = _required_stage(manifest, "holdout-config")
    predictions = _required_stage(manifest, "holdout-predictions")
    evaluation = _required_stage(manifest, "holdout-evaluation")
    integrity = _verify_artifacts(manifest, config, predictions, evaluation)
    samples = _manifest_samples(manifest)
    holdout_ids = _holdout_sample_ids(predictions)
    reference_ids = [str(value) for value in manifest.get("reference_sample_ids") or []]
    selected_ids = _selected_sample_ids(
        selected_scope,
        holdout_ids,
        reference_ids,
        [str(value) for value in manifest.get("evaluation_sample_ids") or []],
    )[:selected_limit]

    sample_evidence = [
        _build_or_reuse_evidence_pack(
            manifest,
            samples[sample_id],
            force=force,
        )
        for sample_id in selected_ids
        if sample_id in samples
    ]
    evidence_summary = _evidence_summary(sample_evidence)

    cached_ids = list(dict.fromkeys([*holdout_ids, *reference_ids]))
    records = _cloud_records(cached_ids)
    text_vectors = _vectors_for_modality(records, "text")
    available_references = [sample_id for sample_id in reference_ids if sample_id in text_vectors]
    global_profiles = _retrieval_profiles(
        holdout_ids,
        available_references,
        samples,
        text_vectors,
        neighbors_per_label=3,
    )
    stratified_profiles = _stratified_retrieval_profiles(
        holdout_ids,
        available_references,
        samples,
        text_vectors,
        neighbors_per_label=3,
    )
    retrieval_summary = _stratified_retrieval_summary(
        stratified_profiles,
        samples,
        available_references,
    )
    reference_coverage_plan = _reference_coverage_plan(
        holdout_ids,
        reference_ids,
        available_references,
        samples,
    )
    contexts = [
        item
        for item in evaluation.get("holdout_diagnostics") or []
        if isinstance(item, dict)
    ]
    prediction_rows = {
        str(item.get("task_id") or ""): item
        for item in predictions.get("predictions") or []
        if isinstance(item, dict)
    }
    cached_comparison = {
        "global_text": _pairwise_metrics(contexts, prediction_rows, global_profiles),
        "stratified_text": _pairwise_metrics(
            contexts, prediction_rows, stratified_profiles
        ),
    }
    cached_comparison["stratified_text"]["delta_vs_global_text"] = round(
        float(cached_comparison["stratified_text"].get("balanced_accuracy") or 0.0)
        - float(cached_comparison["global_text"].get("balanced_accuracy") or 0.0),
        4,
    )

    c1_vector_ready = sum(
        EVIDENCE_FRAME_MODALITY in records.get(sample_id, {}) for sample_id in holdout_ids
    )
    gate = _evidence_gate(
        holdout_count=len(holdout_ids),
        evidence=evidence_summary,
        retrieval=retrieval_summary,
        c1_vector_ready_count=c1_vector_ready,
        cached_comparison=cached_comparison,
    )
    core = {
        "contract_version": BAILIAN_EVIDENCE_QUALITY_VERSION,
        "status": (
            "ready_for_embedding_rebuild"
            if evidence_summary["ready_rate"] >= 0.95
            else "partial"
        ),
        "admission_status": "research_only",
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "source_artifacts": integrity,
        "scope": selected_scope,
        "sample_limit": selected_limit,
        "evidence_pack": {
            "contract_version": EVIDENCE_PACK_VERSION,
            "window_seconds": EVIDENCE_WINDOW_SECONDS,
            "roles": list(WINDOW_ROLES),
            "summary": evidence_summary,
            "samples": sample_evidence,
        },
        "reference_strategy": {
            "policy": "same_account_then_program_then_material_then_global",
            "label_policy": "retrieve_high_and_low_separately",
            "neighbors_per_label": 3,
            "available_reference_count": len(available_references),
            "summary": retrieval_summary,
            "coverage_plan": reference_coverage_plan,
        },
        "cached_retrieval_comparison": cached_comparison,
        "evidence_gate": gate,
        "embedding_rebuild_plan": {
            "status": (
                "ready"
                if evidence_summary["ready_rate"] >= 0.95
                else "blocked_by_evidence_coverage"
            ),
            "input_version": EVIDENCE_VECTOR_INPUT_VERSION,
            "target_modality": EVIDENCE_FRAME_MODALITY,
            "target_sample_count": len(holdout_ids),
            "ready_vector_count": c1_vector_ready,
            "missing_vector_count": max(0, len(holdout_ids) - c1_vector_ready),
            "network_execution_enabled": False,
            "requires_explicit_future_run": True,
        },
        "decision": gate["decision"],
        "network_request_count": 0,
        "effective_cost_cny": "0",
        "cache_only": True,
        "production_weight_changed": False,
        "writes_manual_gold": False,
        "automatic_publish": False,
    }
    report = {
        **core,
        "report_sha256": stable_json_sha256(core),
        "generated_at": utc_now(),
    }
    _persist_stage_report(manifest, EVIDENCE_STAGE, report)
    return report


def bailian_evidence_quality_status(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    report = _load_stage_report(benchmark_id, EVIDENCE_STAGE) or {}
    if not report:
        return {
            "status": "not_run",
            "contract_version": BAILIAN_EVIDENCE_QUALITY_VERSION,
            "network_request_count": 0,
            "effective_cost_cny": "0",
        }
    evidence_pack = report.get("evidence_pack") if isinstance(report.get("evidence_pack"), dict) else {}
    reference_strategy = (
        report.get("reference_strategy")
        if isinstance(report.get("reference_strategy"), dict)
        else {}
    )
    return {
        "status": report.get("status"),
        "contract_version": report.get("contract_version"),
        "report_sha256": report.get("report_sha256"),
        "evidence_summary": evidence_pack.get("summary") or {},
        "reference_summary": reference_strategy.get("summary") or {},
        "reference_coverage_plan": reference_strategy.get("coverage_plan") or {},
        "cached_retrieval_comparison": report.get("cached_retrieval_comparison") or {},
        "evidence_gate": report.get("evidence_gate") or {},
        "embedding_rebuild_plan": report.get("embedding_rebuild_plan") or {},
        "decision": report.get("decision"),
        "network_request_count": 0,
        "effective_cost_cny": "0",
        "cache_only": True,
        "generated_at": report.get("generated_at"),
    }


def _selected_sample_ids(
    scope: str,
    holdout_ids: list[str],
    reference_ids: list[str],
    evaluation_ids: list[str],
) -> list[str]:
    if scope == "holdout":
        return holdout_ids
    if scope == "holdout_and_references":
        return list(dict.fromkeys([*holdout_ids, *reference_ids]))
    return list(dict.fromkeys([*evaluation_ids, *reference_ids]))


def _holdout_sample_ids(predictions: dict) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get(key) or "")
            for item in predictions.get("predictions") or []
            if isinstance(item, dict)
            for key in ("left_sample_id", "right_sample_id")
            if str(item.get(key) or "")
        )
    )


def _build_or_reuse_evidence_pack(
    manifest: dict,
    sample: dict,
    *,
    force: bool,
) -> dict:
    sample_id = str(sample.get("sample_id") or "")
    pack_path = _evidence_pack_path(str(manifest["benchmark_id"]), sample_id)
    cached = read_json(pack_path, default={}) if pack_path.is_file() else {}
    if not force and _valid_evidence_pack(cached, manifest, sample):
        return {**cached, "cache_hit": True}

    media = sample.get("media") if isinstance(sample.get("media"), dict) else {}
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    root = ensure_data_dirs().root.resolve()
    allowed = (ensure_data_dirs().data_dir / "douyin_media_assets").resolve()
    video_path = (root / str(video.get("path") or "")).resolve()
    duration = float(sample.get("duration_seconds") or 0.0)
    base = {
        "contract_version": EVIDENCE_PACK_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "sample_id": sample_id,
        "account_id": str(sample.get("account_id") or "unknown"),
        "source_video_path": str(video.get("path") or ""),
        "source_video_sha256": str(video.get("sha256") or ""),
        "source_duration_seconds": round(duration, 3),
        "window_seconds": EVIDENCE_WINDOW_SECONDS,
    }
    if (
        not sample_id
        or allowed not in video_path.parents
        or not video_path.is_file()
        or video_path.suffix.lower() != ".mp4"
        or duration <= 0
    ):
        result = {
            **base,
            "status": "source_video_missing",
            "windows": [],
            "embedding_source_hash": "",
            "cache_hit": False,
        }
        write_json(pack_path, result)
        return result

    windows = []
    for spec in _window_plan(duration, EVIDENCE_WINDOW_SECONDS):
        frame_path = pack_path.parent / f"{spec['role']}.jpg"
        extracted = _extract_frame(
            video_path,
            frame_path,
            float(spec["frame_seconds"]),
        )
        if extracted:
            raw = frame_path.read_bytes()
            width, height = jpeg_dimensions(raw)
            frame = {
                "path": str(frame_path.relative_to(root)),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "width": width,
                "height": height,
            }
            status = "ready"
        else:
            frame = None
            status = "frame_failed"
        windows.append({**spec, "status": status, "frame": frame})

    ready_windows = [item for item in windows if item.get("status") == "ready"]
    core = {
        **base,
        "status": "ready" if len(ready_windows) == len(WINDOW_ROLES) else "partial",
        "windows": windows,
    }
    core["embedding_source_hash"] = _evidence_embedding_source_hash(
        manifest,
        sample,
        windows,
    )
    result = {**core, "cache_hit": False}
    write_json(pack_path, result)
    return result


def _window_plan(duration: float, window_seconds: float) -> list[dict]:
    duration = max(0.1, float(duration))
    window = min(max(1.0, float(window_seconds)), duration)
    centers = {
        "hook": min(duration * 0.18, window / 2),
        "middle": duration * 0.5,
        "payoff": max(duration * 0.82, duration - window / 2),
    }
    result = []
    for role in WINDOW_ROLES:
        center = min(max(0.0, centers[role]), duration)
        start = min(max(0.0, center - window / 2), max(0.0, duration - window))
        end = min(duration, start + window)
        # Short clips can share one 15-second window, but their representative
        # frames should still cover early, middle, and late visual evidence.
        frame_seconds = min(max(center, 0.0), max(0.0, duration - 0.05))
        result.append(
            {
                "role": role,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "frame_seconds": round(frame_seconds, 3),
            }
        )
    return result


def _extract_frame(video_path: Path, output_path: Path, timestamp: float) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for D12-C1 evidence reconstruction")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.tmp.jpg")
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(video_path),
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
            timeout=60,
        )
        if completed.returncode != 0 or not temporary.is_file():
            return False
        raw = temporary.read_bytes()
        width, height = jpeg_dimensions(raw)
        if not raw or len(raw) > 1_000_000 or max(width, height) > 1280:
            return False
        temporary.replace(output_path)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _valid_evidence_pack(cached: dict, manifest: dict, sample: dict) -> bool:
    if (
        cached.get("contract_version") != EVIDENCE_PACK_VERSION
        or cached.get("manifest_sha256") != manifest.get("manifest_sha256")
        or cached.get("sample_id") != sample.get("sample_id")
        or cached.get("source_video_sha256")
        != (sample.get("media") or {}).get("video", {}).get("sha256")
        or cached.get("status") != "ready"
    ):
        return False
    root = ensure_data_dirs().root.resolve()
    cache_root = _evidence_cache_root(str(manifest["benchmark_id"])).resolve()
    windows = cached.get("windows") if isinstance(cached.get("windows"), list) else []
    if len(windows) != len(WINDOW_ROLES):
        return False
    for item in windows:
        frame = item.get("frame") if isinstance(item, dict) else None
        if not isinstance(frame, dict):
            return False
        path = (root / str(frame.get("path") or "")).resolve()
        if cache_root not in path.parents or not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != str(frame.get("sha256") or ""):
            return False
    return True


def _evidence_embedding_source_hash(
    manifest: dict,
    sample: dict,
    windows: list[dict],
) -> str:
    return stable_json_sha256(
        {
            "version": EVIDENCE_VECTOR_INPUT_VERSION,
            "manifest_sha256": manifest["manifest_sha256"],
            "sample_id": sample.get("sample_id") or "",
            "summary": _evidence_text_summary(sample),
            "window_frames": [
                {
                    "role": item.get("role"),
                    "frame_seconds": item.get("frame_seconds"),
                    "sha256": (item.get("frame") or {}).get("sha256"),
                }
                for item in windows
            ],
        }
    )


def _evidence_text_summary(sample: dict) -> str:
    semantic = sample.get("semantic") if isinstance(sample.get("semantic"), dict) else {}
    material = sample.get("material_gold") if isinstance(sample.get("material_gold"), dict) else {}
    fields = (
        sample.get("title"),
        semantic.get("content_category"),
        semantic.get("program_name"),
        semantic.get("artist_names"),
        semantic.get("song_title"),
        material.get("material_type"),
        material.get("program_context"),
    )
    return " | ".join(str(value).strip() for value in fields if str(value or "").strip())[:12_000]


def _evidence_summary(rows: list[dict]) -> dict:
    ready = [row for row in rows if row.get("status") == "ready"]
    timestamps = [
        len(
            {
                float(item.get("frame_seconds") or 0.0)
                for item in row.get("windows") or []
                if isinstance(item, dict) and item.get("status") == "ready"
            }
        )
        for row in rows
    ]
    return {
        "sample_count": len(rows),
        "ready_count": len(ready),
        "ready_rate": round(len(ready) / len(rows), 4) if rows else 0.0,
        "cache_hit_count": sum(bool(row.get("cache_hit")) for row in rows),
        "source_video_missing_count": sum(
            row.get("status") == "source_video_missing" for row in rows
        ),
        "partial_count": sum(row.get("status") == "partial" for row in rows),
        "three_distinct_temporal_frames_count": sum(value >= 3 for value in timestamps),
        "three_distinct_temporal_frames_rate": round(
            sum(value >= 3 for value in timestamps) / len(rows), 4
        )
        if rows
        else 0.0,
        "total_frame_count": sum(
            item.get("status") == "ready"
            for row in rows
            for item in row.get("windows") or []
            if isinstance(item, dict)
        ),
        "window_seconds": EVIDENCE_WINDOW_SECONDS,
    }


def _stratified_retrieval_profiles(
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
        query_sample = samples.get(sample_id) or {}
        if not query:
            continue
        matches = []
        for reference_id in reference_ids:
            reference = vectors.get(reference_id)
            reference_sample = samples.get(reference_id) or {}
            label = str(reference_sample.get("performance_label") or "unknown")
            if not reference or label not in {"high", "low"}:
                continue
            scope = _reference_scope(query_sample, reference_sample)
            matches.append(
                {
                    "sample_id": reference_id,
                    "label": label,
                    "account_id": str(reference_sample.get("account_id") or "unknown"),
                    "scope": scope,
                    "scope_rank": REFERENCE_SCOPE_ORDER.index(scope),
                    "scope_quality": REFERENCE_SCOPE_QUALITY[scope],
                    "similarity": _cosine(query, reference),
                    "category_match": _material_key(query_sample)
                    == _material_key(reference_sample),
                }
            )
        primary_scope = next(
            (
                scope
                for scope in REFERENCE_SCOPE_ORDER[:-1]
                if all(
                    any(item["label"] == label and item["scope"] == scope for item in matches)
                    for label in ("high", "low")
                )
            ),
            "global",
        )
        by_label = {
            label: _paired_scope_neighbors(
                matches,
                label=label,
                primary_scope=primary_scope,
                limit=neighbors_per_label,
            )
            for label in ("high", "low")
        }
        if not by_label["high"] or not by_label["low"]:
            continue
        selected = sorted(
            [*by_label["high"], *by_label["low"]],
            key=lambda item: (
                0 if primary_scope == "global" or item["scope"] == primary_scope else 1,
                -float(item["similarity"]),
                str(item["sample_id"]),
            ),
        )
        profiles[sample_id] = {
            "score": mean(float(item["similarity"]) for item in by_label["high"])
            - mean(float(item["similarity"]) for item in by_label["low"]),
            "top1_label": selected[0]["label"],
            "top1_category_match": bool(selected[0]["category_match"]),
            "top1_same_account": selected[0]["scope"] == "same_account",
            "same_account_reference_available": any(
                item["scope"] == "same_account" for item in matches
            ),
            "primary_scope": primary_scope,
            "balanced_same_account_available": primary_scope == "same_account",
            "context_reference_available": primary_scope != "global",
            "selected_same_account_rate": mean(
                item["scope"] == "same_account" for item in selected
            ),
            "selected_context_rate": mean(item["scope"] != "global" for item in selected),
            "mean_scope_quality": mean(float(item["scope_quality"]) for item in selected),
            "max_title_overlap": 0.0,
            "exact_title_match": False,
            "top_matches": selected,
        }
    return profiles


def _paired_scope_neighbors(
    matches: list[dict],
    *,
    label: str,
    primary_scope: str,
    limit: int,
) -> list[dict]:
    label_matches = [item for item in matches if item["label"] == label]
    if primary_scope == "global":
        return sorted(
            label_matches,
            key=lambda item: (-float(item["similarity"]), str(item["sample_id"])),
        )[:limit]
    primary = sorted(
        [item for item in label_matches if item["scope"] == primary_scope],
        key=lambda item: (-float(item["similarity"]), str(item["sample_id"])),
    )
    selected = primary[:limit]
    selected_ids = {str(item["sample_id"]) for item in selected}
    fallback = sorted(
        [item for item in label_matches if str(item["sample_id"]) not in selected_ids],
        key=lambda item: (-float(item["similarity"]), str(item["sample_id"])),
    )
    return [*selected, *fallback[: max(0, limit - len(selected))]]


def _stratified_retrieval_summary(
    profiles: dict[str, dict],
    samples: dict[str, dict],
    reference_ids: list[str],
) -> dict:
    base = _retrieval_summary(profiles, samples)
    selected = [item for profile in profiles.values() for item in profile.get("top_matches") or []]
    return {
        **base,
        "available_reference_count": len(reference_ids),
        "available_reference_label_distribution": dict(
            Counter(
                str((samples.get(sample_id) or {}).get("performance_label") or "unknown")
                for sample_id in reference_ids
            )
        ),
        "balanced_same_account_coverage": round(
            mean(bool(profile.get("balanced_same_account_available")) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "account_or_program_or_material_coverage": round(
            mean(bool(profile.get("context_reference_available")) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "selected_same_account_rate": round(
            mean(float(profile.get("selected_same_account_rate") or 0.0) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "selected_context_rate": round(
            mean(float(profile.get("selected_context_rate") or 0.0) for profile in profiles.values())
            if profiles
            else 0.0,
            4,
        ),
        "selected_scope_distribution": dict(Counter(str(item.get("scope") or "global") for item in selected)),
    }


def _reference_coverage_plan(
    query_ids: list[str],
    reference_ids: list[str],
    cached_reference_ids: list[str],
    samples: dict[str, dict],
) -> dict:
    query_account_counts = Counter(
        str((samples.get(sample_id) or {}).get("account_id") or "unknown")
        for sample_id in query_ids
    )
    cached = set(cached_reference_ids)
    rows = []
    recommended_ids = []
    for account_id, query_count in sorted(query_account_counts.items()):
        account_references = [
            sample_id
            for sample_id in reference_ids
            if str((samples.get(sample_id) or {}).get("account_id") or "unknown")
            == account_id
        ]
        manifest_by_label = {
            label: [
                sample_id
                for sample_id in account_references
                if str((samples.get(sample_id) or {}).get("performance_label") or "")
                == label
            ]
            for label in ("high", "low")
        }
        cached_by_label = {
            label: [sample_id for sample_id in manifest_by_label[label] if sample_id in cached]
            for label in ("high", "low")
        }
        missing_labels = [
            label
            for label in ("high", "low")
            if manifest_by_label[label] and not cached_by_label[label]
        ]
        for label in missing_labels:
            recommended_ids.extend(manifest_by_label[label][:3])
        rows.append(
            {
                "account_id": account_id,
                "query_count": query_count,
                "manifest_high_count": len(manifest_by_label["high"]),
                "manifest_low_count": len(manifest_by_label["low"]),
                "cached_high_count": len(cached_by_label["high"]),
                "cached_low_count": len(cached_by_label["low"]),
                "cached_balanced": bool(cached_by_label["high"] and cached_by_label["low"]),
                "manifest_balanced": bool(manifest_by_label["high"] and manifest_by_label["low"]),
                "missing_cached_labels": missing_labels,
            }
        )
    query_count = max(1, len(query_ids))
    cached_coverage = sum(
        int(row["query_count"]) for row in rows if row["cached_balanced"]
    ) / query_count
    manifest_ceiling = sum(
        int(row["query_count"]) for row in rows if row["manifest_balanced"]
    ) / query_count
    return {
        "query_account_count": len(rows),
        "cached_balanced_same_account_coverage": round(cached_coverage, 4),
        "manifest_balanced_same_account_ceiling": round(manifest_ceiling, 4),
        "recoverable_query_count": sum(
            int(row["query_count"])
            for row in rows
            if row["manifest_balanced"] and not row["cached_balanced"]
        ),
        "unrecoverable_account_count": sum(not row["manifest_balanced"] for row in rows),
        "unrecoverable_accounts": [
            str(row["account_id"]) for row in rows if not row["manifest_balanced"]
        ],
        "recommended_reference_ids": list(dict.fromkeys(recommended_ids)),
        "accounts": rows,
        "requires_new_reference_manifest_for_80pct_same_account_gate": manifest_ceiling < 0.8,
    }


def _reference_scope(query: dict, reference: dict) -> str:
    query_account = str(query.get("account_id") or "").strip()
    reference_account = str(reference.get("account_id") or "").strip()
    if query_account and query_account != "unknown" and query_account == reference_account:
        return "same_account"
    query_program = _program_key(query)
    if query_program and query_program == _program_key(reference):
        return "same_program"
    query_material = _material_key(query)
    if query_material and query_material != "unknown" and query_material == _material_key(reference):
        return "same_material"
    return "global"


def _program_key(sample: dict) -> str:
    semantic = sample.get("semantic") if isinstance(sample.get("semantic"), dict) else {}
    material = sample.get("material_gold") if isinstance(sample.get("material_gold"), dict) else {}
    return str(material.get("program_context") or semantic.get("program_name") or "").strip().lower()


def _material_key(sample: dict) -> str:
    semantic = sample.get("semantic") if isinstance(sample.get("semantic"), dict) else {}
    material = sample.get("material_gold") if isinstance(sample.get("material_gold"), dict) else {}
    return str(material.get("material_type") or semantic.get("content_category") or "unknown").strip().lower()


def _pairwise_metrics(
    contexts: list[dict],
    predictions: dict[str, dict],
    profiles: dict[str, dict],
) -> dict:
    deltas = {}
    for context in contexts:
        task_id = str(context.get("task_id") or "")
        prediction = predictions.get(task_id) or {}
        left = profiles.get(str(prediction.get("left_sample_id") or ""))
        right = profiles.get(str(prediction.get("right_sample_id") or ""))
        if left and right:
            deltas[task_id] = float(left["score"]) - float(right["score"])
    rows = _evaluation_rows(contexts, deltas)
    count = len(rows)
    return {
        "pair_count": count,
        "balanced_accuracy": round(_balanced_accuracy(rows, "cloud_correct"), 4),
        "raw_accuracy": round(
            sum(bool(row["cloud_correct"]) for row in rows) / count if count else 0.0,
            4,
        ),
    }


def _evidence_gate(
    *,
    holdout_count: int,
    evidence: dict,
    retrieval: dict,
    c1_vector_ready_count: int,
    cached_comparison: dict,
) -> dict:
    evidence_ready = float(evidence.get("three_distinct_temporal_frames_rate") or 0.0) >= 0.95
    balanced_references = (
        int((retrieval.get("available_reference_label_distribution") or {}).get("high") or 0) > 0
        and int((retrieval.get("available_reference_label_distribution") or {}).get("low") or 0) > 0
    )
    context_ready = float(retrieval.get("account_or_program_or_material_coverage") or 0.0) >= 0.8
    vector_ready_rate = c1_vector_ready_count / max(1, holdout_count)
    vectors_ready = vector_ready_rate >= 0.8
    cached_delta = float(
        (cached_comparison.get("stratified_text") or {}).get("delta_vs_global_text") or 0.0
    )
    passed = evidence_ready and balanced_references and context_ready and vectors_ready and cached_delta >= 0.0
    if not evidence_ready:
        decision = "complete_three_window_evidence_before_embedding"
    elif not balanced_references or not context_ready:
        decision = "expand_stratified_high_low_reference_coverage"
    elif not vectors_ready:
        decision = "await_explicit_d12c1_embedding_rebuild"
    elif not passed:
        decision = "keep_v2_4_and_revise_reference_objective"
    else:
        decision = "ready_for_new_independent_holdout"
    return {
        "passed": passed,
        "status": "passed" if passed else "research_only",
        "decision": decision,
        "checks": {
            "three_window_evidence_ready": evidence_ready,
            "balanced_high_low_references_ready": balanced_references,
            "context_reference_coverage_at_least_80pct": context_ready,
            "d12c1_fusion_vector_coverage_at_least_80pct": vectors_ready,
            "cached_stratified_not_worse_than_global": cached_delta >= 0.0,
        },
        "c1_vector_ready_count": c1_vector_ready_count,
        "c1_vector_ready_rate": round(vector_ready_rate, 4),
        "automatic_promotion": False,
    }


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return float(dot / (norm_left * norm_right))


def _evidence_cache_root(benchmark_id: str) -> Path:
    safe = "".join(character if character.isalnum() or character in "-_." else "_" for character in benchmark_id)
    return ensure_data_dirs().cache_dir / "bailian_evidence_quality" / safe


def _evidence_pack_path(benchmark_id: str, sample_id: str) -> Path:
    safe = "".join(character if character.isalnum() or character in "-_." else "_" for character in sample_id)
    return _evidence_cache_root(benchmark_id) / safe / "pack.json"


def _required_stage(manifest: dict, stage: str) -> dict:
    report = _load_stage_report(str(manifest["benchmark_id"]), stage) or {}
    if not report:
        raise ValueError(f"D12-C1 requires {stage}")
    if str(report.get("manifest_sha256") or "") != str(manifest.get("manifest_sha256") or ""):
        raise ValueError(f"D12-C1 {stage} manifest mismatch")
    return report
