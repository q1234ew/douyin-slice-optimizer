from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

from dso.providers.aliyun_bailian import (
    BAILIAN_QWEN35_OMNI_MODEL,
    BAILIAN_QWEN35_OMNI_SHORT_CLIP_PROFILE,
)
from dso.providers.contracts import (
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderRequest,
    stable_json_sha256,
)
from dso.providers.policy import UploadLevel
from dso.providers.service import AliyunBailianRuntime, build_aliyun_bailian_runtime


BAILIAN_COMPLETE_CLIP_BATCH_VERSION = "bailian_complete_clip_batch.v1"
COMPLETE_CLIP_PROXY_VERSION = "dso-complete-clip-h264-aac.v1"
DEFAULT_MANIFEST_ID = "dso-qwen37-full-clip-diagnostic-20260719-r1"
DEFAULT_BATCH_LIMIT = 10
MAX_BATCH_LIMIT = 10
DEFAULT_HARD_BUDGET_CNY = Decimal("10")
MAX_PROXY_BYTES = 3_200_000
DEFAULT_FPS = 1.0
DEFAULT_MAX_PIXELS = 262_144
DEFAULT_OUTPUT_TOKENS = 1_200
DEFAULT_TIMEOUT_SECONDS = 180.0
NEUTRAL_CLIP_SUMMARY = (
    "已授权的完整短视频切片。请仅依据实际音频和画面证据分析，"
    "不要参考互动表现、账号先验或既有标签。"
)
_LABEL_ORDER = {"low": 0, "mid": 1, "high": 2}
DEFAULT_OUTCOME_TARGET = "historical_visible_engagement_proxy_not_views"
DEFAULT_PROMOTION_GATE_REASON = (
    "This outcome-selected diagnostic uses interaction proxies rather than an independent "
    "holdout, so it cannot promote a model or ranking weight."
)


def run_bailian_complete_clip_batch(
    manifest_path: Path,
    *,
    media_root: Path | None = None,
    output_path: Path | None = None,
    execute: bool = False,
    limit: int = DEFAULT_BATCH_LIMIT,
    force_proxies: bool = False,
    batch_id: str | None = None,
    hard_budget_cny: Decimal = DEFAULT_HARD_BUDGET_CNY,
    runtime_builder: Callable[..., AliyunBailianRuntime] = build_aliyun_bailian_runtime,
) -> dict[str, Any]:
    """Analyze complete short clips with Qwen3.5-Omni without extracting frames."""

    selected_limit = int(limit)
    if not 1 <= selected_limit <= MAX_BATCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH_LIMIT}")
    if hard_budget_cny <= 0:
        raise ValueError("hard_budget_cny must be positive")

    resolved_manifest = manifest_path.resolve()
    manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    clips = [item for item in manifest.get("clips") or [] if isinstance(item, dict)]
    if not clips:
        raise ValueError("complete clip manifest contains no clips")
    selected_clips = clips[:selected_limit]
    outcome_target = str(manifest.get("outcome_target") or DEFAULT_OUTCOME_TARGET).strip()
    promotion_gate_reason = str(
        manifest.get("promotion_gate_reason") or DEFAULT_PROMOTION_GATE_REASON
    ).strip()
    root = (media_root or resolved_manifest.parent).resolve()
    proxy_root = root / "derived" / COMPLETE_CLIP_PROXY_VERSION
    proxy_root.mkdir(parents=True, exist_ok=True)

    resolved_batch_id = str(
        batch_id
        or f"complete-clip-batch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    ).strip()
    runtime = runtime_builder(
        batch_id=resolved_batch_id,
        model_id=BAILIAN_QWEN35_OMNI_MODEL,
        request_profile=BAILIAN_QWEN35_OMNI_SHORT_CLIP_PROFILE,
    )
    if UploadLevel.FULL_MEDIA not in runtime.allowed_upload_levels:
        raise RuntimeError("complete clip batch requires explicit full_media permission")

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
        request = build_complete_clip_request(
            runtime,
            clip,
            proxy,
            proxy_info,
            resolved_batch_id,
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
            "complete clip batch preflight exceeds hard budget: "
            f"{reserved_total} > {hard_budget_cny} CNY"
        )

    report: dict[str, Any] = {
        "contract_version": BAILIAN_COMPLETE_CLIP_BATCH_VERSION,
        "experiment_id": resolved_batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "preflight_passed",
        "admission_status": "research_only",
        "goal_alignment": ["G1", "G3"],
        "source_manifest_id": manifest.get("manifest_id") or manifest.get("benchmark_id"),
        "source_manifest_sha256": _file_sha256(resolved_manifest),
        "evaluation_validity": (
            manifest.get("evaluation_validity")
            or "diagnostic_only_not_independent_holdout"
        ),
        "outcome_target": outcome_target,
        "input_policy": {
            "representation": "complete_temporal_h264_aac_proxy",
            "frame_extraction_used": False,
            "full_duration_required": True,
            "audio_required": True,
            "labels_excluded_from_provider_request": True,
            "neutral_summary_sha256": hashlib.sha256(
                NEUTRAL_CLIP_SUMMARY.encode("utf-8")
            ).hexdigest(),
        },
        "provider": {
            "provider_id": runtime.provider.descriptor.identity.provider_id,
            "model_id": runtime.provider.descriptor.identity.model_id,
            "api_version": runtime.provider.descriptor.identity.api_version,
            "prompt_version": runtime.provider.descriptor.identity.prompt_version,
            "request_profile": BAILIAN_QWEN35_OMNI_SHORT_CLIP_PROFILE,
            "request_type": "omni_complete_short_clip_analysis",
            "upload_level": UploadLevel.FULL_MEDIA.value,
            "max_retries": 0,
        },
        "budget": {
            "hard_batch_cap_cny": str(hard_budget_cny),
            "preflight_reserved_total_cny": str(reserved_total),
            "within_hard_cap": reserved_total <= hard_budget_cny,
        },
        "clips": [_preflight_clip_summary(item) for item in prepared],
        "network_request_count": 0,
        "usage_estimated_cost_cny": "0",
        "production_impact": _production_impact(),
    }
    if not execute:
        return _finish_report(report, output_path)

    results: list[dict[str, Any]] = []
    for item in prepared:
        clip = item["clip"]
        proxy_path = item["proxy_path"]
        request = build_complete_clip_request(
            runtime,
            clip,
            proxy_path,
            item["proxy"],
            resolved_batch_id,
        )
        reservation = runtime.provider.estimate_max_cost(request)
        outcome = runtime.runner.execute(
            request,
            estimated_cost=reservation,
            upload_level=UploadLevel.FULL_MEDIA,
            batch_id=resolved_batch_id,
            local_baseline={
                "sample_id": _required_text(clip, "sample_id"),
                "status": "labels_locked_until_batch_completion",
            },
        )
        ledger = runtime.runner.ledger.get(outcome.ledger_call_id) or {}
        results.append(_result_summary(clip, item["proxy"], outcome.to_dict(), ledger))

    report.update(
        {
            "status": (
                "completed"
                if all(item["provider_status"] in {"shadow_succeeded", "shadow_cached"} for item in results)
                else "partial"
            ),
            "clips": results,
            "evaluation": evaluate_complete_clip_results(
                results,
                outcome_target=outcome_target,
                promotion_gate_reason=promotion_gate_reason,
            ),
            "network_request_count": sum(int(item["network_request_count"]) for item in results),
            "usage_estimated_cost_cny": str(
                sum((Decimal(str(item["usage_estimated_cost_cny"])) for item in results), Decimal("0"))
            ),
        }
    )
    return _finish_report(report, output_path)


def build_complete_clip_request(
    runtime: AliyunBailianRuntime,
    clip: dict[str, Any],
    proxy_path: Path,
    proxy_info: dict[str, Any],
    batch_id: str,
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
        + len(NEUTRAL_CLIP_SUMMARY.encode("utf-8")) * 4
        + 1_024
    )
    target = runtime.provider.descriptor.identity
    content_sha256 = stable_json_sha256(
        {
            "proxy_sha256": video_sha256,
            "summary_sha256": hashlib.sha256(
                NEUTRAL_CLIP_SUMMARY.encode("utf-8")
            ).hexdigest(),
            "fps": DEFAULT_FPS,
            "max_pixels": DEFAULT_MAX_PIXELS,
            "estimated_output_tokens": DEFAULT_OUTPUT_TOKENS,
            "prompt_version": target.prompt_version,
            "proxy_version": COMPLETE_CLIP_PROXY_VERSION,
        }
    )
    sample_id = _required_text(clip, "sample_id")
    return ProviderRequest(
        request_id=f"{batch_id}-{sample_id}-{video_sha256[:10]}",
        request_type="omni_complete_short_clip_analysis",
        target=target,
        content_sha256=content_sha256,
        input_size=ProviderInputSize(
            video_seconds=duration,
            audio_seconds=audio_seconds,
            frame_count=frame_count,
            text_characters=len(NEUTRAL_CLIP_SUMMARY),
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
            "summary": NEUTRAL_CLIP_SUMMARY,
            "video_base64": base64.b64encode(video).decode("ascii"),
            "video_mime_type": "video/mp4",
            "video_sha256": video_sha256,
        },
        parameters={
            "estimated_output_tokens": DEFAULT_OUTPUT_TOKENS,
            "estimated_audio_tokens": estimated_audio_tokens,
            "fps": DEFAULT_FPS,
            "max_pixels": DEFAULT_MAX_PIXELS,
        },
    )


def prepare_complete_clip_proxy(
    source: Path,
    destination: Path,
    *,
    expected_source_sha256: str,
    expected_duration_seconds: float,
    force: bool = False,
) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(f"complete clip source is missing: {source}")
    source_sha256 = _file_sha256(source)
    if source_sha256 != expected_source_sha256:
        raise ValueError(f"source SHA-256 mismatch for {source.name}")
    source_probe = _probe_media(source)
    source_duration = float(source_probe["duration_seconds"])
    if abs(source_duration - expected_duration_seconds) > 0.35:
        raise ValueError(f"source duration mismatch for {source.name}")
    if not source_probe["has_audio"]:
        raise ValueError(f"complete clip source has no audio: {source.name}")

    metadata_path = destination.with_suffix(".json")
    if not force and destination.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("contract_version") == COMPLETE_CLIP_PROXY_VERSION
            and metadata.get("source_sha256") == source_sha256
            and metadata.get("proxy_sha256") == _file_sha256(destination)
        ):
            _validate_proxy_probe(destination, source_duration)
            return metadata

    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build complete clip proxies")
    with tempfile.TemporaryDirectory(
        prefix="dso-complete-clip-",
        dir=destination.parent,
    ) as temp_dir:
        temp_root = Path(temp_dir)
        temp_output = temp_root / "proxy.mp4"
        for target_bytes in (MAX_PROXY_BYTES, 2_950_000, 2_700_000):
            _transcode_full_duration_proxy(
                ffmpeg,
                source,
                temp_output,
                duration_seconds=source_duration,
                target_bytes=target_bytes,
                passlog=temp_root / "ffmpeg-pass",
            )
            if temp_output.stat().st_size <= MAX_PROXY_BYTES:
                break
        else:  # pragma: no cover - the final attempt either exists or subprocess failed
            raise RuntimeError(f"unable to fit complete clip proxy under {MAX_PROXY_BYTES} bytes")
        _validate_proxy_probe(temp_output, source_duration)
        os.replace(temp_output, destination)

    proxy_probe = _validate_proxy_probe(destination, source_duration)
    metadata = {
        "contract_version": COMPLETE_CLIP_PROXY_VERSION,
        "source_filename": source.name,
        "source_sha256": source_sha256,
        "source_duration_seconds": round(source_duration, 3),
        "proxy_filename": destination.name,
        "proxy_sha256": _file_sha256(destination),
        "size_bytes": destination.stat().st_size,
        "duration_seconds": round(float(proxy_probe["duration_seconds"]), 3),
        "audio_seconds": round(float(proxy_probe["duration_seconds"]), 3),
        "video_codec": proxy_probe["video_codec"],
        "audio_codec": proxy_probe["audio_codec"],
        "width": proxy_probe["width"],
        "height": proxy_probe["height"],
        "frame_extraction_used": False,
        "full_temporal_range_preserved": True,
    }
    _write_json_atomic(metadata_path, metadata)
    return metadata


def evaluate_complete_clip_results(
    results: list[dict[str, Any]],
    *,
    outcome_target: str = DEFAULT_OUTCOME_TARGET,
    promotion_gate_reason: str = DEFAULT_PROMOTION_GATE_REASON,
) -> dict[str, Any]:
    successful = [
        item
        for item in results
        if item.get("provider_status") in {"shadow_succeeded", "shadow_cached"}
        and isinstance(item.get("provider_output"), dict)
    ]
    pairs: dict[str, list[dict[str, Any]]] = {}
    for item in successful:
        pairs.setdefault(str(item.get("source_pair_id") or ""), []).append(item)

    pair_rows: list[dict[str, Any]] = []
    for pair_id, items in pairs.items():
        if not pair_id or len(items) != 2:
            continue
        expected = max(items, key=lambda value: _LABEL_ORDER.get(str(value.get("performance_label")), -1))
        scores = [float((item.get("provider_output") or {}).get("traffic_potential_score") or 0.0) for item in items]
        all_evaluable = all(not bool((item.get("provider_output") or {}).get("abstain")) for item in items)
        if not all_evaluable or abs(scores[0] - scores[1]) < 1e-9:
            selected_sample_id = "abstain"
        else:
            selected_sample_id = str(items[0]["sample_id"] if scores[0] > scores[1] else items[1]["sample_id"])
        v2_4_correct = _v2_4_correct_for_pair(items)
        pair_rows.append(
            {
                "pair_id": pair_id,
                "expected_sample_id": expected["sample_id"],
                "omni_selected_sample_id": selected_sample_id,
                "omni_correct": selected_sample_id == expected["sample_id"],
                "score_gap": round(abs(scores[0] - scores[1]), 4),
                "labels": {str(item["sample_id"]): item.get("performance_label") for item in items},
                "scores": {str(item["sample_id"]): score for item, score in zip(items, scores)},
                "outcome_evidence": {
                    str(item["sample_id"]): {
                        "normalized_reward": float(item.get("normalized_reward") or 0.0),
                        "reward_proxy": float(item.get("reward_proxy") or 0.0),
                        "visible_engagement": item.get("visible_engagement") or {},
                    }
                    for item in items
                },
                "v2_4_correct_from_frozen_diagnostic_role": v2_4_correct,
            }
        )

    comparable = [item for item in pair_rows if isinstance(item["v2_4_correct_from_frozen_diagnostic_role"], bool)]
    latencies = sorted(float(item.get("latency_ms") or 0.0) for item in successful)
    costs = [Decimal(str(item.get("usage_estimated_cost_cny") or "0")) for item in successful]
    timeline_counts = [len((item.get("provider_output") or {}).get("timeline") or []) for item in successful]
    audio_evidence_count = sum(
        bool((item.get("provider_output") or {}).get("audio_characteristics"))
        and any(str(row.get("audio_event") or "").strip() for row in (item.get("provider_output") or {}).get("timeline") or [])
        for item in successful
    )
    return {
        "labels_revealed_after_all_provider_outputs": True,
        "outcome_target": outcome_target,
        "sample_count": len(results),
        "successful_count": len(successful),
        "success_rate": round(len(successful) / max(1, len(results)), 4),
        "abstain_count": sum(bool((item.get("provider_output") or {}).get("abstain")) for item in successful),
        "audio_evidence_coverage": round(audio_evidence_count / max(1, len(successful)), 4),
        "mean_timeline_items": round(mean(timeline_counts), 2) if timeline_counts else 0.0,
        "latency_ms": {
            "median": round(median(latencies), 1) if latencies else 0.0,
            "p95": round(_percentile(latencies, 0.95), 1) if latencies else 0.0,
        },
        "usage_estimated_cost_cny": str(sum(costs, Decimal("0"))),
        "pair_count": len(pair_rows),
        "pair_accuracy": round(sum(bool(item["omni_correct"]) for item in pair_rows) / max(1, len(pair_rows)), 4),
        "v2_4_comparable_pair_count": len(comparable),
        "v2_4_accuracy_on_comparable_pairs": round(
            sum(bool(item["v2_4_correct_from_frozen_diagnostic_role"]) for item in comparable)
            / max(1, len(comparable)),
            4,
        ),
        "pair_results": pair_rows,
        "promotion_gate": {
            "status": "research_only",
            "passed": False,
            "reason": promotion_gate_reason,
        },
    }


def _transcode_full_duration_proxy(
    ffmpeg: str,
    source: Path,
    output: Path,
    *,
    duration_seconds: float,
    target_bytes: int,
    passlog: Path,
) -> None:
    output.unlink(missing_ok=True)
    total_kbps = max(260, int(target_bytes * 8 * 0.96 / duration_seconds / 1_000))
    audio_kbps = 48
    video_kbps = max(180, total_kbps - audio_kbps - 16)
    common = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-sn",
        "-dn",
        "-vf",
        "scale=960:960:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{int(video_kbps * 1.25)}k",
        "-bufsize",
        f"{video_kbps * 2}k",
        "-passlogfile",
        str(passlog),
    ]
    subprocess.run(
        [*common, "-pass", "1", "-an", "-f", "mp4", os.devnull],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        [
            *common,
            "-pass",
            "2",
            "-map",
            "0:a:0",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_kbps}k",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _probe_media(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to validate complete clip proxies")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    return {
        "duration_seconds": float((payload.get("format") or {}).get("duration") or 0.0),
        "size_bytes": int((payload.get("format") or {}).get("size") or path.stat().st_size),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": bool(audio),
    }


def _validate_proxy_probe(path: Path, source_duration: float) -> dict[str, Any]:
    probe = _probe_media(path)
    if probe["video_codec"] != "h264" or probe["audio_codec"] != "aac":
        raise ValueError(f"complete clip proxy must be H.264/AAC: {path.name}")
    if not probe["has_audio"]:
        raise ValueError(f"complete clip proxy has no audio: {path.name}")
    if abs(float(probe["duration_seconds"]) - source_duration) > 0.25:
        raise ValueError(
            "complete clip proxy duration changed: "
            f"{path.name} source={source_duration:.3f}s "
            f"proxy={float(probe['duration_seconds']):.3f}s"
        )
    if path.stat().st_size > MAX_PROXY_BYTES:
        raise ValueError(f"complete clip proxy exceeds {MAX_PROXY_BYTES} bytes: {path.name}")
    return probe


def _preflight_clip_summary(item: dict[str, Any]) -> dict[str, Any]:
    clip = item["clip"]
    return {
        "sample_id": clip.get("sample_id"),
        "source_pair_id": clip.get("source_pair_id"),
        "proxy": item["proxy"],
        "preflight": item["preflight"],
        "reserved_cost_cny": item["reserved_cost_cny"],
        "labels_locked": True,
    }


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
        "diagnostic_role": clip.get("diagnostic_role"),
        "account_id": clip.get("account_id"),
        "performance_label": clip.get("performance_label"),
        "normalized_reward": float(clip.get("normalized_reward") or 0.0),
        "reward_proxy": float(clip.get("reward_proxy") or 0.0),
        "visible_engagement": clip.get("visible_engagement") or {},
        "content_category": clip.get("content_category"),
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


def _v2_4_correct_for_pair(items: list[dict[str, Any]]) -> bool | None:
    roles = {str(item.get("diagnostic_role") or "") for item in items}
    if roles == {"failure_both_wrong"}:
        return False
    if roles == {"failure_cloud_wrong_v2_4_correct"}:
        return True
    if roles == {"control_both_correct"}:
        return True
    return None


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
        _write_json_atomic(output_path.resolve(), report)
    return report


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


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
