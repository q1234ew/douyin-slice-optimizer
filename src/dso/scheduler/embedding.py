from __future__ import annotations

from pathlib import Path
from typing import Any

from dso.learning.qwen_embeddings import compute_scheduled_qwen_embedding, commit_scheduled_qwen_embedding


class QwenEmbeddingJobAdapter:
    def prepare(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        request = dict(item.get("request") or {})
        source = request.get("source") if isinstance(request.get("source"), dict) else {}
        if source.get("type") == "text":
            if not str(source.get("text") or "").strip():
                raise FileNotFoundError("embedding text is missing")
            return {"status": "ready", "source_type": "text", "text_length": len(str(source.get("text") or ""))}
        paths = [Path(path) for path in source.get("paths") or []]
        if not paths or any(not path.is_file() for path in paths):
            raise FileNotFoundError("embedding visual artifacts are missing")
        return {"status": "ready", "source_type": "visual", "artifact_count": len(paths), "prepared_media": source.get("prepared_media") or {}}

    def execute(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        return compute_scheduled_qwen_embedding(dict(item.get("request") or {}))

    def commit_item(self, job: dict[str, Any], item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return commit_scheduled_qwen_embedding(result)

    def finalize(self, job: dict[str, Any], item_results: list[dict[str, Any]]) -> dict[str, Any]:
        succeeded = sum(1 for item in item_results if (item.get("result") or {}).get("status") == "ready")
        failed = len(item_results) - succeeded
        request = dict(job.get("request_summary") or {})
        return {
            "contract_version": "qwen_embedding_scheduler.v1",
            "status": "ready" if succeeded and not failed else ("degraded" if succeeded else "failed"),
            "entity_type": str(request.get("entity_type") or ""),
            "modality": str(request.get("modality") or ""),
            "completed_items": succeeded,
            "failed_items": failed,
            "skipped_before_enqueue": int(request.get("skipped_before_enqueue") or 0),
            "writes_manual_gold": False,
            "production_weight_changed": False,
        }
