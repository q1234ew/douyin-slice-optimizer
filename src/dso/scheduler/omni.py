from __future__ import annotations

from typing import Any

from dso.learning.omni_slice_ranker import (
    analyze_scheduled_omni_window,
    commit_scheduled_omni_windows,
    prepare_scheduled_omni_window,
)
class OmniRerankJobAdapter:
    def prepare(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        request = dict(job.get("request_summary") or {})
        parameters = dict(request.get("parameters") or {})
        item_request = dict(item.get("request") or {})
        return prepare_scheduled_omni_window(
            str(job["subject_id"]),
            str(item_request.get("segment_id") or ""),
            dict(item_request.get("window") or {}),
            max_clip_seconds=float(parameters.get("max_clip_seconds") or 6.0),
        )

    def execute(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        request = dict(job.get("request_summary") or {})
        parameters = dict(request.get("parameters") or {})
        item_request = dict(item.get("request") or {})
        result = analyze_scheduled_omni_window(
            str(job["subject_id"]),
            str(item_request.get("segment_id") or ""),
            dict(item_request.get("window") or {}),
            max_clip_seconds=float(parameters.get("max_clip_seconds") or 6.0),
            force=bool(parameters.get("force", False)),
        )
        return result

    def commit_item(self, job: dict[str, Any], item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": str(result.get("status") or "failed"),
            "segment_id": str(result.get("segment_id") or ""),
            "window_role": str(result.get("window_role") or item.get("item_role") or ""),
            "cache_hit": bool(result.get("cache_hit")),
        }

    def finalize(self, job: dict[str, Any], item_results: list[dict[str, Any]]) -> dict[str, Any]:
        request = dict(job.get("request_summary") or {})
        parameters = dict(request.get("parameters") or {})
        return commit_scheduled_omni_windows(
            str(job["subject_id"]),
            item_results,
            expected_input_hash=str(job["input_hash"]),
            candidate_limit=int(parameters.get("candidate_limit") or 3),
            max_clip_seconds=float(parameters.get("max_clip_seconds") or 6.0),
            omni_weight=float(parameters.get("omni_weight") or 0.15),
        )
