#!/usr/bin/env python3
"""Run one explicitly authorized, research-only Bailian full-clip shadow call."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from dso.providers.aliyun_bailian import (
    BAILIAN_COMPLETE_SHORT_CLIP_PROFILE,
    BAILIAN_PRIMARY_JUDGE_MODEL,
)
from dso.providers.contracts import (
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderRequest,
    stable_json_sha256,
)
from dso.providers.policy import UploadLevel
from dso.providers.service import build_aliyun_bailian_runtime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight or execute one bounded qwen3.7 complete-short-clip shadow request."
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--performance-label", default="unknown")
    parser.add_argument("--batch-id")
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--max-pixels", type=int, default=262_144)
    parser.add_argument("--max-output-tokens", type=int, default=1_200)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    ) + "\n"
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
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def main() -> None:
    args = _parser().parse_args()
    video_path = args.video.resolve()
    video = video_path.read_bytes()
    video_sha256 = hashlib.sha256(video).hexdigest()
    summary = str(args.summary).strip()
    if not summary:
        raise SystemExit("--summary must not be empty")
    if not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0:
        raise SystemExit("--duration-seconds must be positive")
    frame_count = math.ceil(args.duration_seconds * args.fps)
    conservative_input_tokens = (
        frame_count * math.ceil(args.max_pixels / 1024)
        + len(summary.encode("utf-8")) * 4
        + 1_024
    )
    batch_id = str(
        args.batch_id
        or f"bailian-complete-short-clip-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    runtime = build_aliyun_bailian_runtime(
        batch_id=batch_id,
        model_id=BAILIAN_PRIMARY_JUDGE_MODEL,
        request_profile=BAILIAN_COMPLETE_SHORT_CLIP_PROFILE,
    )
    target = runtime.provider.descriptor.identity
    content_sha256 = stable_json_sha256(
        {
            "video_sha256": video_sha256,
            "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            "fps": args.fps,
            "max_pixels": args.max_pixels,
            "prompt_version": target.prompt_version,
        }
    )
    request = ProviderRequest(
        request_id=f"{batch_id}-{uuid4().hex[:12]}",
        request_type="complete_short_clip_analysis",
        target=target,
        content_sha256=content_sha256,
        input_size=ProviderInputSize(
            video_seconds=args.duration_seconds,
            audio_seconds=0,
            frame_count=frame_count,
            text_characters=len(summary),
            input_tokens=conservative_input_tokens,
            request_bytes=len(video),
        ),
        data_permission=runtime.data_permission,
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=args.timeout_seconds,
            max_retries=0,
        ),
        payload={
            "summary": summary,
            "video_base64": base64.b64encode(video).decode("ascii"),
            "video_mime_type": "video/mp4",
            "video_sha256": video_sha256,
        },
        parameters={
            "estimated_output_tokens": args.max_output_tokens,
            "fps": args.fps,
            "max_pixels": args.max_pixels,
        },
    )
    preflight = runtime.provider.preflight_request(request)
    report: dict[str, object] = {
        "contract_version": "dso_bailian_complete_short_clip_shadow.v1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "preflight_passed",
        "lifecycle_status": "research_only",
        "sample_id": args.sample_id,
        "input": {
            "filename": video_path.name,
            "sha256": video_sha256,
            "size_bytes": len(video),
            "duration_seconds": args.duration_seconds,
            "audio_seconds_sent_for_analysis": 0,
            "estimated_frame_count": frame_count,
            "fps": args.fps,
            "max_pixels": args.max_pixels,
            "estimated_input_tokens": conservative_input_tokens,
        },
        "preflight": preflight,
        "production_impact": {
            "production_weight_changed": False,
            "writes_manual_gold": False,
            "automatic_publish": False,
        },
    }
    if args.execute:
        outcome = runtime.runner.execute(
            request,
            estimated_cost=runtime.provider.estimate_max_cost(request),
            upload_level=UploadLevel.FULL_MEDIA,
            batch_id=batch_id,
            local_baseline={
                "sample_id": args.sample_id,
                "performance_label": args.performance_label,
                "status": "frozen_local_reference",
            },
        )
        ledger = runtime.runner.ledger.get(outcome.ledger_call_id)
        attempts = list(runtime.runner.ledger.iter_attempts(call_id=outcome.ledger_call_id))
        report.update(
            {
                "status": outcome.status,
                "outcome": outcome.to_dict(),
                "ledger": ledger,
                "attempts": attempts,
            }
        )
    if args.output:
        _write_atomic(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
