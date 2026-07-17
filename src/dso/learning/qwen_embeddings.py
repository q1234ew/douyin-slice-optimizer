from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

try:  # Prefer requests for multipart/model-service calls; urllib stays as a dependency-free fallback.
    import requests
except Exception:  # pragma: no cover - exercised only when requests is unavailable.
    requests = None  # type: ignore[assignment]

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.learning.multimodal_validation import _build_asset_index, _prepare_row
from dso.media.ffmpeg import extract_frame, probe_video
from dso.utils import clamp, new_id, read_json, utc_now, write_json
from dso.versions import QWEN_EMBEDDING_VERSION, RESEARCH_RANKER_VERSION


QWEN_EMBEDDING_MODEL = "Qwen/Qwen3-VL-Embedding-2B"
QWEN_EMBEDDING_DIM = 2048
DEFAULT_EMBEDDING_SERVICE_URL = "http://192.168.31.143:8001"
TEXT_EMBEDDING_STRATEGY = "ranker_plus_text_embedding"
VISUAL_EMBEDDING_STRATEGY = "ranker_plus_visual_embedding"
TEXT_VISUAL_EMBEDDING_STRATEGY = "ranker_plus_text_visual_embedding"
EMBEDDING_RESEARCH_STRATEGIES = {
    TEXT_EMBEDDING_STRATEGY,
    VISUAL_EMBEDDING_STRATEGY,
    TEXT_VISUAL_EMBEDDING_STRATEGY,
}
TEXT_FIELDS = [
    "title",
    "tags",
    "artist_names",
    "song_title",
    "content_category",
    "hook_type",
    "slice_structure",
    "program_name",
]


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


class QwenEmbeddingClient:
    def __init__(self, service_url: str | None = None, *, timeout_seconds: float = 60.0) -> None:
        self.service_url = (service_url or os.environ.get("DSO_EMBEDDING_SERVICE_URL") or DEFAULT_EMBEDDING_SERVICE_URL).rstrip("/")
        self.timeout_seconds = float(timeout_seconds or 30.0)
        self._requests_session = None
        if requests is not None:
            self._requests_session = requests.Session()
            self._requests_session.trust_env = _env_truthy("DSO_EMBEDDING_TRUST_ENV_PROXY")

    def health(self) -> dict:
        try:
            payload = self._json_request("GET", "/health")
            status = _embedding_service_status(payload)
            return {
                "status": status,
                "service_url": self.service_url,
                "model_id": _loaded_model_id(payload),
                "model_loaded": _model_loaded(payload),
                "raw": payload,
            }
        except Exception as exc:
            return {"status": "service_unavailable", "service_url": self.service_url, "error": str(exc)}

    def load(self) -> dict:
        try:
            payload = self._json_request(
                "POST",
                "/load",
                {
                    "model": QWEN_EMBEDDING_MODEL,
                    "model_id": QWEN_EMBEDDING_MODEL,
                    "backend": "sentence_transformers",
                },
            )
            status = _embedding_service_status(payload)
            return {
                "status": status,
                "service_url": self.service_url,
                "model_id": _loaded_model_id(payload),
                "model_loaded": _model_loaded(payload),
                "raw": payload,
            }
        except Exception as exc:
            return {"status": "service_unavailable", "service_url": self.service_url, "error": str(exc)}

    def embed_text(self, text: str) -> list[float]:
        text = str(text or "").strip()
        if not text:
            raise ValueError("empty_text")
        last_error: Exception | None = None
        for payload in [{"texts": [text], "model": QWEN_EMBEDDING_MODEL}, {"text": text, "model": QWEN_EMBEDDING_MODEL}]:
            try:
                data = self._json_request("POST", "/embed/text", payload)
                embeddings = _validated_embedding_vectors(data, endpoint="embed/text")
                if embeddings:
                    return embeddings[0]
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {400, 404, 422}:
                    raise
            except Exception as exc:
                last_error = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in {400, 404, 422}:
                    continue
                raise
        raise RuntimeError(f"embedding_response_empty: {last_error}")

    def embed_image(self, image_path: Path) -> list[float]:
        data = self._multipart_request("/embed/image", [("file", image_path)])
        embeddings = _validated_embedding_vectors(data, endpoint="embed/image")
        if not embeddings:
            raise RuntimeError("image_embedding_response_empty")
        return embeddings[0]

    def embed_video_frames(self, frame_paths: list[Path]) -> list[float]:
        if not frame_paths:
            raise ValueError("empty_frame_paths")
        data = self._multipart_request("/embed/video-frames", [("files", path) for path in frame_paths])
        embeddings = _validated_embedding_vectors(data, endpoint="embed/video-frames")
        if not embeddings:
            raise RuntimeError("video_frame_embedding_response_empty")
        if len(embeddings) == 1:
            return embeddings[0]
        return _mean_vector(embeddings)

    def _json_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if self._requests_session is not None:
            response = self._requests_session.request(
                method.upper(),
                f"{self.service_url}{path}",
                json=payload if payload is not None else None,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.service_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=self.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw or "{}")

    def _multipart_request(self, path: str, files: list[tuple[str, Path]]) -> dict:
        if self._requests_session is not None:
            upload_files = []
            handles = []
            try:
                for field_name, file_path in files:
                    path_obj = Path(file_path)
                    if not path_obj.is_file():
                        raise FileNotFoundError(str(path_obj))
                    content_type = mimetypes.guess_type(path_obj.name)[0] or "application/octet-stream"
                    handle = path_obj.open("rb")
                    handles.append(handle)
                    upload_files.append((field_name, (path_obj.name, handle, content_type)))
                response = self._requests_session.post(
                    f"{self.service_url}{path}",
                    files=upload_files,
                    timeout=max(self.timeout_seconds, 60.0),
                )
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
            finally:
                for handle in handles:
                    handle.close()
        boundary = f"----dso-qwen-{hashlib.sha256(os.urandom(16)).hexdigest()[:16]}"
        chunks: list[bytes] = []
        for field_name, file_path in files:
            path_obj = Path(file_path)
            if not path_obj.is_file():
                raise FileNotFoundError(str(path_obj))
            content_type = mimetypes.guess_type(path_obj.name)[0] or "application/octet-stream"
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{path_obj.name}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8")
            )
            chunks.append(path_obj.read_bytes())
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        request = urllib.request.Request(
            f"{self.service_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=max(self.timeout_seconds, 60.0)) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw or "{}")


def build_qwen_embedding_index(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    entity_type: str = "historical_sample",
    modality: str = "text",
    limit: int = 300,
    force: bool = False,
    client: QwenEmbeddingClient | None = None,
) -> dict:
    quarantined_invalid = quarantine_invalid_qwen_embedding_records()
    selected_modalities = _modalities(modality)
    rows = _entity_rows(entity_type, account_id=account_id, dataset_id=dataset_id, limit=int(limit or 0))
    client = client or QwenEmbeddingClient()
    service_status = client.health()
    if not _service_ready(service_status):
        loaded = client.load()
        if _service_ready(loaded):
            service_status = loaded
    counts = Counter()
    errors: list[dict] = []
    asset_index = _build_asset_index() if "visual" in selected_modalities and entity_type == "historical_sample" else {}
    for row in rows:
        for current_modality in selected_modalities:
            result = _ensure_entity_embedding(
                row,
                entity_type=entity_type,
                modality=current_modality,
                force=force,
                client=client,
                service_status=service_status,
                asset_index=asset_index,
            )
            counts[result.get("status") or "failed"] += 1
            if result.get("error"):
                errors.append(result)
    ready_count = _ready_record_count(entity_type, account_id=account_id, dataset_id=dataset_id, modalities=selected_modalities)
    total_slots = max(1, len(rows) * len(selected_modalities))
    return {
        "contract_version": QWEN_EMBEDDING_VERSION,
        "status": "ready" if ready_count else ("service_unavailable" if service_status.get("status") == "service_unavailable" else "empty"),
        "embedding_model": QWEN_EMBEDDING_MODEL,
        "embedding_version": QWEN_EMBEDDING_VERSION,
        "entity_type": entity_type,
        "account_id": account_id or "all",
        "dataset_id": _scope(dataset_id),
        "modality": modality,
        "query": {
            "account_id": account_id or "all",
            "dataset_id": _scope(dataset_id),
            "entity_type": entity_type,
            "modality": modality,
            "limit": int(limit or 0),
            "force": bool(force),
        },
        "sample_count": len(rows),
        "created": int(counts.get("created", 0)),
        "reused": int(counts.get("reused", 0)),
        "skipped": int(counts.get("skipped", 0)),
        "failed": int(counts.get("failed", 0)),
        "quarantined_invalid": quarantined_invalid,
        "coverage": {
            "ready_records": ready_count,
            "total_slots": total_slots,
            "ready_rate": round(ready_count / total_slots, 4),
        },
        "service_status": service_status,
        "errors": errors[:12],
        "cache_root": str(ensure_data_dirs().cache_dir / "qwen_embeddings"),
        "generated_at": utc_now(),
    }


def run_qwen_embedding_evidence(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 300,
    k: int = 10,
    modality: str = "all",
    client: QwenEmbeddingClient | None = None,
) -> dict:
    build = build_qwen_embedding_index(
        account_id=account_id,
        dataset_id=dataset_id,
        entity_type="historical_sample",
        modality=modality,
        limit=limit,
        force=False,
        client=client,
    )
    rows = _entity_rows("historical_sample", account_id=account_id, dataset_id=dataset_id, limit=int(limit or 0))
    context = historical_embedding_backtest_context(rows)
    selected = _modalities(modality)
    evidence_rows = []
    for row in rows[: max(1, int(limit or 300))]:
        scores, components = historical_embedding_strategy_scores(row, rows, context, base_score=50.0)
        if not any(float(components.get(f"qwen_{name}_evidence_quality") or 0.0) > 0 for name in selected):
            continue
        evidence_rows.append(
            {
                "sample_id": row.get("id") or "",
                "account_id": row.get("account_id") or "",
                "title": row.get("title") or "",
                "performance_label": row.get("performance_label") or "",
                "ranker_plus_text_embedding": scores.get(TEXT_EMBEDDING_STRATEGY),
                "ranker_plus_visual_embedding": scores.get(VISUAL_EMBEDDING_STRATEGY),
                "ranker_plus_text_visual_embedding": scores.get(TEXT_VISUAL_EMBEDDING_STRATEGY),
                "component_scores": components,
            }
        )
        if len(evidence_rows) >= max(1, int(k or 10)):
            break
    return {
        "contract_version": QWEN_EMBEDDING_VERSION,
        "status": "ready" if evidence_rows else ("low_confidence" if (build.get("coverage") or {}).get("ready_records") else build.get("status") or "empty"),
        "embedding_model": QWEN_EMBEDDING_MODEL,
        "account_id": account_id or "all",
        "dataset_id": _scope(dataset_id),
        "modality": modality,
        "sample_count": len(rows),
        "embedding_coverage": embedding_coverage_for_scope(account_id=account_id, dataset_id=dataset_id),
        "build_summary": {key: build.get(key) for key in ["created", "reused", "skipped", "failed", "service_status"]},
        "similar_evidence_summary": embedding_evidence_summary(evidence_rows),
        "low_interaction_risk_samples": _top_low_risk_rows(evidence_rows),
        "evidence_samples": evidence_rows,
        "recommendations": _evidence_recommendations(build, evidence_rows),
        "generated_at": utc_now(),
    }


def qwen_embedding_evidence_for_segment(
    segment_id: str,
    *,
    account_id: str | None = None,
    limit: int = 6,
    modality: str = "all",
    client: QwenEmbeddingClient | None = None,
) -> dict:
    row = _candidate_row(segment_id)
    if not row:
        raise KeyError(f"segment not found: {segment_id}")
    if account_id:
        row["account_id"] = account_id
    selected = _modalities(modality)
    client = client or QwenEmbeddingClient()
    service_status = client.health()
    if not _service_ready(service_status):
        loaded = client.load()
        if _service_ready(loaded):
            service_status = loaded
    build_results = []
    for current_modality in selected:
        build_results.append(
            _ensure_entity_embedding(
                row,
                entity_type="candidate",
                modality=current_modality,
                force=False,
                client=client,
                service_status=service_status,
                asset_index={},
            )
        )
    history_rows = _historical_rows_for_evidence(account_id or row.get("account_id"))
    records = _embedding_records_for_entities("historical_sample", [str(item.get("id") or "") for item in history_rows])
    candidate_records = _embedding_records_for_entities("candidate", [segment_id])
    evidence = _embedding_matches_for_entity(
        row,
        history_rows,
        records,
        candidate_records.get(segment_id, {}),
        limit=max(1, int(limit or 6)),
        modalities=selected,
    )
    quality = _combined_embedding_quality(evidence)
    return {
        "embedding_evidence_version": QWEN_EMBEDDING_VERSION,
        "research_ranker_version": f"{RESEARCH_RANKER_VERSION}+qwen_embedding_research",
        "embedding_model": QWEN_EMBEDDING_MODEL,
        "status": "ready" if quality >= 0.45 else ("low_confidence" if quality > 0 else "insufficient_embedding_evidence"),
        "service_status": service_status,
        "build_results": build_results,
        **evidence,
        "embedding_evidence_quality": {
            "score": round(quality, 4),
            "label": "high" if quality >= 0.72 else "medium" if quality >= 0.45 else "low",
            "scope": evidence.get("embedding_scope") or "none",
            "model_name": QWEN_EMBEDDING_MODEL,
        },
        "embedding_ranker_reason": _embedding_ranker_reason(evidence, quality),
    }


def historical_embedding_backtest_context(train_rows: list[dict]) -> dict:
    ids = [str(row.get("id") or row.get("training_sample_id") or "") for row in train_rows if row.get("id") or row.get("training_sample_id")]
    records = _embedding_records_for_entities("historical_sample", ids)
    vectors_by_id = {}
    for entity_id, by_modality in records.items():
        vectors_by_id[entity_id] = {}
        for modality, record in by_modality.items():
            vector = _load_vector(record)
            if vector:
                vectors_by_id[entity_id][modality] = vector
    account_counts = Counter(str(row.get("account_id") or "") for row in train_rows if row.get("account_id"))
    thresholds = _interaction_thresholds(train_rows)
    return {
        "vectors_by_id": vectors_by_id,
        "account_counts": dict(account_counts),
        "thresholds": thresholds,
        "coverage": _coverage_from_vectors(vectors_by_id, train_rows),
    }


def historical_embedding_strategy_scores(
    row: dict,
    train_rows: list[dict],
    context: dict,
    *,
    base_score: float,
) -> tuple[dict[str, float], dict[str, Any]]:
    base = clamp(float(base_score or 50.0))
    text = _historical_modality_adjustment(row, train_rows, context, modality="text", base_score=base)
    visual = _historical_modality_adjustment(row, train_rows, context, modality="visual", base_score=base)
    text_score = text["score"]
    visual_score = visual["score"]
    combined_delta = (text_score - base) * 0.55 + (visual_score - base) * 0.45
    if text["quality"] <= 0 and visual["quality"] <= 0:
        combined_delta = 0.0
    scores = {
        TEXT_EMBEDDING_STRATEGY: round(text_score, 4),
        VISUAL_EMBEDDING_STRATEGY: round(visual_score, 4),
        TEXT_VISUAL_EMBEDDING_STRATEGY: round(clamp(base + combined_delta), 4),
    }
    components = {
        "qwen_text_similarity_score": round(text["positive_similarity"], 4),
        "qwen_text_low_risk_score": round(text["risk_similarity"], 4),
        "qwen_text_evidence_quality": round(text["quality"], 4),
        "qwen_visual_similarity_score": round(visual["positive_similarity"], 4),
        "qwen_visual_low_risk_score": round(visual["risk_similarity"], 4),
        "qwen_visual_evidence_quality": round(visual["quality"], 4),
        "qwen_embedding_scope": text.get("scope") or visual.get("scope") or "none",
        "qwen_embedding_global_fallback": 1.0 if text.get("global_fallback") or visual.get("global_fallback") else 0.0,
    }
    return scores, components


def embedding_coverage_for_scope(account_id: str | None = None, *, dataset_id: str | None = None) -> dict:
    rows = _entity_rows("historical_sample", account_id=account_id, dataset_id=dataset_id, limit=0)
    records = _embedding_records_for_entities("historical_sample", [str(row.get("id") or "") for row in rows])
    text_ready = sum(1 for row in rows if "text" in records.get(str(row.get("id") or ""), {}))
    visual_ready = sum(1 for row in rows if "visual" in records.get(str(row.get("id") or ""), {}))
    visual_missing = _record_status_count(
        entity_type="historical_sample",
        account_id=account_id,
        dataset_id=dataset_id,
        modality="visual",
        status="skipped",
        error="visual_missing",
    )
    total = len(rows)
    return {
        "sample_count": total,
        "text_ready_count": text_ready,
        "text_ready_rate": round(text_ready / max(1, total), 4),
        "visual_ready_count": visual_ready,
        "visual_ready_rate": round(visual_ready / max(1, total), 4),
        "visual_missing_count": visual_missing,
        "model_name": QWEN_EMBEDDING_MODEL,
    }


def embedding_backtest_summary(rows: list[dict]) -> dict:
    if not rows:
        return {"sample_count": 0, "text_ready_rate": 0.0, "visual_ready_rate": 0.0, "evidence_ready_rate": 0.0}
    text_ready = sum(1 for row in rows if float((row.get("component_scores") or {}).get("qwen_text_evidence_quality") or 0.0) > 0)
    visual_ready = sum(1 for row in rows if float((row.get("component_scores") or {}).get("qwen_visual_evidence_quality") or 0.0) > 0)
    both = sum(
        1
        for row in rows
        if float((row.get("component_scores") or {}).get("qwen_text_evidence_quality") or 0.0) > 0
        and float((row.get("component_scores") or {}).get("qwen_visual_evidence_quality") or 0.0) > 0
    )
    return {
        "sample_count": len(rows),
        "text_ready_count": text_ready,
        "text_ready_rate": round(text_ready / max(1, len(rows)), 4),
        "visual_ready_count": visual_ready,
        "visual_ready_rate": round(visual_ready / max(1, len(rows)), 4),
        "text_visual_ready_count": both,
        "text_visual_ready_rate": round(both / max(1, len(rows)), 4),
        "model_name": QWEN_EMBEDDING_MODEL,
    }


def embedding_strategy_gap(strategy_comparison: dict, *, selected_strategy: str = TEXT_VISUAL_EMBEDDING_STRATEGY) -> dict:
    base = strategy_comparison.get("research_ranker_v2_4") or {}
    result = {"baseline_strategy": "research_ranker_v2_4", "required_lift_delta": 0.02, "strategies": {}}
    for strategy in [TEXT_EMBEDDING_STRATEGY, VISUAL_EMBEDDING_STRATEGY, TEXT_VISUAL_EMBEDDING_STRATEGY]:
        target = strategy_comparison.get(strategy) or {}
        lift_delta = float(target.get("topk_lift_vs_random") or 0.0) - float(base.get("topk_lift_vs_random") or 0.0)
        low_delta = float(target.get("low_interaction_avoidance_rate") or 0.0) - float(base.get("low_interaction_avoidance_rate") or 0.0)
        high_delta = float(target.get("high_interaction_hit_rate") or 0.0) - float(base.get("high_interaction_hit_rate") or 0.0)
        result["strategies"][strategy] = {
            "topk_lift_delta_vs_v2_4": round(lift_delta, 4),
            "high_hit_delta_vs_v2_4": round(high_delta, 4),
            "low_avoidance_delta_vs_v2_4": round(low_delta, 4),
            "passed_research_gate": lift_delta >= 0.02 and low_delta >= -0.0001,
            "status": "positive_research_signal" if lift_delta >= 0.02 and low_delta >= -0.0001 else "research_only",
        }
    selected = result["strategies"].get(selected_strategy) or {}
    return {
        **result,
        "selected_strategy": selected_strategy,
        "selected": selected,
        "status": selected.get("status") or "research_only",
    }


def embedding_evidence_summary(rows: list[dict]) -> dict:
    if not rows:
        return {
            "sample_count": 0,
            "text_positive_count": 0,
            "visual_positive_count": 0,
            "low_risk_count": 0,
        }
    return {
        "sample_count": len(rows),
        "text_positive_count": sum(1 for row in rows if float((row.get("component_scores") or {}).get("qwen_text_similarity_score") or 0.0) > 0),
        "visual_positive_count": sum(1 for row in rows if float((row.get("component_scores") or {}).get("qwen_visual_similarity_score") or 0.0) > 0),
        "low_risk_count": sum(
            1
            for row in rows
            if float((row.get("component_scores") or {}).get("qwen_text_low_risk_score") or 0.0) > 0
            or float((row.get("component_scores") or {}).get("qwen_visual_low_risk_score") or 0.0) > 0
        ),
    }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)))


def historical_embedding_text(row: dict) -> str:
    return " ".join(str(row.get(key) or "") for key in TEXT_FIELDS).strip()


def _ensure_entity_embedding(
    row: dict,
    *,
    entity_type: str,
    modality: str,
    force: bool,
    client: QwenEmbeddingClient,
    service_status: dict,
    asset_index: dict[str, dict[str, list[str]]],
) -> dict:
    entity_id = _entity_id(row, entity_type)
    account_id = str(row.get("account_id") or "")
    dataset_id = str(row.get("dataset_id") or "")
    platform_item_id = str(row.get("platform_item_id") or "")
    try:
        source = _embedding_source(row, entity_type=entity_type, modality=modality, asset_index=asset_index)
    except FileNotFoundError as exc:
        source_hash = _source_hash(f"{modality}:{entity_id}:missing")
        _store_embedding_record(
            entity_type=entity_type,
            entity_id=entity_id,
            account_id=account_id,
            dataset_id=dataset_id,
            platform_item_id=platform_item_id,
            modality=modality,
            source_hash=source_hash,
            status="skipped",
            error=str(exc) or "visual_missing",
        )
        return {"status": "skipped", "entity_id": entity_id, "modality": modality, "error": "visual_missing"}
    source_hash = source["source_hash"]
    cached = _find_ready_record(entity_type, entity_id, modality, source_hash)
    if cached and not force and Path(str(cached.get("vector_path") or "")).is_file():
        return {"status": "reused", "entity_id": entity_id, "modality": modality}
    if not _service_ready(service_status):
        _store_embedding_record(
            entity_type=entity_type,
            entity_id=entity_id,
            account_id=account_id,
            dataset_id=dataset_id,
            platform_item_id=platform_item_id,
            modality=modality,
            source_hash=source_hash,
            status="failed",
            error=str(service_status.get("error") or service_status.get("status") or "service_unavailable"),
        )
        return {"status": "failed", "entity_id": entity_id, "modality": modality, "error": "service_unavailable"}
    try:
        vector = _remote_embedding_for_source(client, source)
        if not vector:
            raise RuntimeError("empty_vector")
        vector_path = _vector_cache_path(entity_type, modality, entity_id, source_hash)
        write_json(
            vector_path,
            {
                "contract_version": QWEN_EMBEDDING_VERSION,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "modality": modality,
                "model_name": QWEN_EMBEDDING_MODEL,
                "model_version": QWEN_EMBEDDING_VERSION,
                "source_hash": source_hash,
                "source_summary": source.get("summary") or {},
                "vector_dim": len(vector),
                "vector": vector,
                "created_at": utc_now(),
            },
        )
        _store_embedding_record(
            entity_type=entity_type,
            entity_id=entity_id,
            account_id=account_id,
            dataset_id=dataset_id,
            platform_item_id=platform_item_id,
            modality=modality,
            source_hash=source_hash,
            vector_path=vector_path,
            vector_dim=len(vector),
            status="ready",
            error="",
        )
        return {"status": "created", "entity_id": entity_id, "modality": modality, "vector_dim": len(vector)}
    except Exception as exc:
        _store_embedding_record(
            entity_type=entity_type,
            entity_id=entity_id,
            account_id=account_id,
            dataset_id=dataset_id,
            platform_item_id=platform_item_id,
            modality=modality,
            source_hash=source_hash,
            status="failed",
            error=str(exc),
        )
        return {"status": "failed", "entity_id": entity_id, "modality": modality, "error": str(exc)}


def _embedding_source(row: dict, *, entity_type: str, modality: str, asset_index: dict[str, dict[str, list[str]]]) -> dict:
    if modality == "text":
        text = historical_embedding_text(row) if entity_type == "historical_sample" else _candidate_text(row)
        if not text:
            raise FileNotFoundError("text_missing")
        return {
            "type": "text",
            "text": text,
            "source_hash": _source_hash(text),
            "summary": {"text_length": len(text)},
        }
    if modality != "visual":
        raise ValueError(f"unsupported_modality:{modality}")
    paths = (
        _historical_visual_paths(row, asset_index=asset_index)
        if entity_type == "historical_sample"
        else _candidate_visual_paths(row)
    )
    if not paths:
        raise FileNotFoundError("visual_missing")
    source_key = "|".join(f"{path}:{path.stat().st_size}:{int(path.stat().st_mtime)}" for path in paths if path.exists())
    return {
        "type": "visual",
        "paths": paths,
        "source_hash": _source_hash(source_key),
        "summary": {"path_count": len(paths), "paths": [str(path) for path in paths[:3]]},
    }


def _remote_embedding_for_source(client: QwenEmbeddingClient, source: dict) -> list[float]:
    if source.get("type") == "text":
        return client.embed_text(str(source.get("text") or ""))
    paths = [Path(path) for path in source.get("paths") or []]
    if len(paths) == 1:
        return client.embed_image(paths[0])
    return client.embed_video_frames(paths[:3])


def _historical_visual_paths(row: dict, *, asset_index: dict[str, dict[str, list[str]]]) -> list[Path]:
    prepared = _prepare_row(row, asset_index=asset_index)
    paths = ((prepared.get("assets") or {}).get("paths") or {}) if isinstance(prepared.get("assets"), dict) else {}
    images = _existing_paths([*(paths.get("cover") or []), *(paths.get("frame") or [])])
    if images:
        return images[:3]
    videos = _existing_paths(paths.get("video") or [])
    if not videos:
        return []
    return _extract_visual_frames(videos[0], entity_type="historical_sample", entity_id=str(row.get("id") or row.get("platform_item_id") or "unknown"))


def _candidate_visual_paths(row: dict) -> list[Path]:
    video_path = Path(str(row.get("file_path") or ""))
    if not video_path.is_file():
        return []
    return _extract_visual_frames(video_path, entity_type="candidate", entity_id=str(row.get("id") or "unknown"), row=row)


def _extract_visual_frames(video_path: Path, *, entity_type: str, entity_id: str, row: dict | None = None) -> list[Path]:
    settings = ensure_data_dirs()
    frame_dir = settings.cache_dir / "qwen_embeddings" / entity_type / "visual_frames" / _safe_name(entity_id)
    try:
        duration = float((probe_video(video_path) or {}).get("duration_seconds") or 0.0)
    except Exception:
        duration = float((row or {}).get("duration_seconds") or 0.0)
    if row and row.get("start_time") is not None and row.get("end_time") is not None:
        start = max(0.0, float(row.get("start_time") or 0.0))
        end = max(start + 0.5, float(row.get("end_time") or start + float(row.get("duration_seconds") or 3.0)))
        times = [start + 0.5, (start + end) / 2.0, max(start + 0.5, end - 0.5)]
    else:
        duration = duration or 9.0
        times = [min(duration * 0.2, 1.0), duration * 0.5, max(0.5, duration * 0.8)]
    frames = []
    for index, timestamp in enumerate(times[:3], start=1):
        target = frame_dir / f"frame_{index}.jpg"
        if not target.exists():
            extract_frame(video_path, target, timestamp)
        if target.is_file():
            frames.append(target)
    return frames


def _historical_modality_adjustment(
    row: dict,
    train_rows: list[dict],
    context: dict,
    *,
    modality: str,
    base_score: float,
) -> dict:
    vectors_by_id = context.get("vectors_by_id") if isinstance(context.get("vectors_by_id"), dict) else {}
    target_id = str(row.get("id") or row.get("training_sample_id") or "")
    target = (vectors_by_id.get(target_id) or {}).get(modality)
    if not target:
        return _empty_modality_score(base_score)
    account = str(row.get("account_id") or "")
    same_account = [item for item in train_rows if str(item.get("account_id") or "") == account and item.get("id") != target_id]
    account_counts = context.get("account_counts") if isinstance(context.get("account_counts"), dict) else {}
    global_fallback = int(account_counts.get(account) or 0) < 50
    pool = train_rows if global_fallback else same_account
    thresholds = context.get("thresholds") or _interaction_thresholds(train_rows)
    high_matches = []
    low_matches = []
    best_similarity = 0.0
    for sample in pool:
        sample_id = str(sample.get("id") or sample.get("training_sample_id") or "")
        if sample_id == target_id or _same_entity(row, sample):
            continue
        vector = (vectors_by_id.get(sample_id) or {}).get(modality)
        if not vector:
            continue
        similarity = cosine_similarity(target, vector)
        if similarity <= 0:
            continue
        best_similarity = max(best_similarity, similarity)
        label = _interaction_label(sample, thresholds)
        reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0.0)
        item = (similarity, reward, sample)
        if label == "high":
            high_matches.append(item)
        elif label == "low":
            low_matches.append(item)
    high_matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    low_matches.sort(key=lambda item: (item[0], 100.0 - item[1]), reverse=True)
    positive = max((sim * reward / 100.0 for sim, reward, _ in high_matches[:8]), default=0.0)
    risk = max((sim * (100.0 - reward) / 100.0 for sim, reward, _ in low_matches[:8]), default=0.0)
    quality = max(best_similarity, positive * 0.75, risk * 0.65)
    if quality <= 0:
        score = base_score
    else:
        score = clamp(base_score + positive * 3.2 - risk * 2.7 + max(0.0, quality - 0.48) * 2.0)
    return {
        "score": round(score, 4),
        "quality": round(quality, 4),
        "positive_similarity": round(max((item[0] for item in high_matches[:3]), default=0.0), 4),
        "risk_similarity": round(max((item[0] for item in low_matches[:3]), default=0.0), 4),
        "high_matches": [_public_match(item[2], item[0], modality=modality) for item in high_matches[:3]],
        "low_matches": [_public_match(item[2], item[0], modality=modality) for item in low_matches[:3]],
        "scope": "global_fallback" if global_fallback else "account",
        "global_fallback": global_fallback,
    }


def _embedding_matches_for_entity(
    target_row: dict,
    history_rows: list[dict],
    records: dict[str, dict[str, dict]],
    target_records: dict[str, dict],
    *,
    limit: int,
    modalities: list[str],
) -> dict:
    account = str(target_row.get("account_id") or "")
    same_account = [item for item in history_rows if str(item.get("account_id") or "") == account]
    global_fallback = len(same_account) < 50
    pool = history_rows if global_fallback else same_account
    result: dict[str, Any] = {
        "embedding_scope": "global_fallback" if global_fallback else "account",
        "embedding_global_fallback": global_fallback,
        "matched_text_high_samples": [],
        "matched_text_low_samples": [],
        "matched_visual_high_samples": [],
        "matched_visual_low_samples": [],
        "text_similarity_score": 0.0,
        "visual_similarity_score": 0.0,
    }
    thresholds = _interaction_thresholds(history_rows)
    for modality in modalities:
        target_vector = _load_vector(target_records.get(modality) or {})
        if not target_vector:
            continue
        high_matches: list[tuple[float, dict]] = []
        low_matches: list[tuple[float, dict]] = []
        for sample in pool:
            vector = _load_vector((records.get(str(sample.get("id") or "")) or {}).get(modality) or {})
            if not vector:
                continue
            similarity = cosine_similarity(target_vector, vector)
            label = _interaction_label(sample, thresholds)
            if label == "high":
                high_matches.append((similarity, sample))
            elif label == "low":
                low_matches.append((similarity, sample))
        high_matches.sort(key=lambda item: (item[0], float(item[1].get("normalized_reward") or item[1].get("reward_proxy") or 0.0)), reverse=True)
        low_matches.sort(key=lambda item: (item[0], 100.0 - float(item[1].get("normalized_reward") or item[1].get("reward_proxy") or 0.0)), reverse=True)
        result[f"matched_{modality}_high_samples"] = [_public_match(sample, score, modality=modality) for score, sample in high_matches[:limit]]
        result[f"matched_{modality}_low_samples"] = [_public_match(sample, score, modality=modality) for score, sample in low_matches[:limit]]
        result[f"{modality}_similarity_score"] = round(max([score for score, _ in high_matches[:3]] + [0.0]), 4)
    return result


def _entity_rows(entity_type: str, *, account_id: str | None, dataset_id: str | None, limit: int) -> list[dict]:
    if entity_type == "historical_sample":
        return _historical_rows(account_id=account_id, dataset_id=dataset_id, limit=limit)
    if entity_type == "candidate":
        return _candidate_rows(account_id=account_id, limit=limit)
    raise ValueError(f"unsupported_entity_type:{entity_type}")


def _historical_rows(account_id: str | None, dataset_id: str | None, limit: int) -> list[dict]:
    clauses = [
        "COALESCE(platform_item_id, '') != ''",
        "(COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0)",
    ]
    params: list[Any] = []
    account = str(account_id or "").strip()
    dataset = str(dataset_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if dataset and dataset.lower() != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset)
    query = f"""
        SELECT *
        FROM historical_capture_samples
        WHERE {' AND '.join(clauses)}
        ORDER BY
          CASE performance_label WHEN 'high' THEN 0 WHEN 'low' THEN 1 ELSE 2 END,
          COALESCE(normalized_reward, reward_proxy, 0) DESC,
          updated_at DESC
    """
    if int(limit or 0) > 0:
        query += " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        return fetch_all(conn, query, params)


def _historical_rows_for_evidence(account_id: str | None) -> list[dict]:
    account = str(account_id or "").strip()
    rows = _historical_rows(account, dataset_id=None, limit=0) if account and account.lower() not in {"all"} else []
    if len(rows) >= 50:
        return rows
    return _historical_rows(None, dataset_id=None, limit=0) or rows


def _candidate_rows(account_id: str | None, limit: int) -> list[dict]:
    query = """
        SELECT c.*, v.account_id, v.title AS video_title, v.file_path
        FROM candidate_segments c
        JOIN source_videos v ON v.id = c.source_video_id
    """
    params: list[Any] = []
    if account_id and str(account_id).lower() != "all":
        query += " WHERE v.account_id = ?"
        params.append(account_id)
    query += " ORDER BY c.created_at DESC"
    if int(limit or 0) > 0:
        query += " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        return fetch_all(conn, query, params)


def _candidate_row(segment_id: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT c.*, v.account_id, v.title AS video_title, v.file_path
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            WHERE c.id = ?
            """,
            [segment_id],
        )


def _candidate_text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "transcript",
            "summary",
            "primary_topic",
            "music_slice_type",
            "emotion_type",
            "short_video_structure",
            "musical_moment",
            "program_context",
            "comment_trigger",
            "video_title",
        ]
    ).strip()


def _embedding_records_for_entities(entity_type: str, entity_ids: list[str]) -> dict[str, dict[str, dict]]:
    ids = {str(item) for item in entity_ids if item}
    if not ids:
        return {}
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT *
            FROM embedding_records
            WHERE entity_type = ? AND model_name = ? AND status = 'ready' AND vector_dim = ?
            ORDER BY updated_at DESC
            """,
            [entity_type, QWEN_EMBEDDING_MODEL, QWEN_EMBEDDING_DIM],
        )
    result: dict[str, dict[str, dict]] = {}
    for row in rows:
        entity_id = str(row.get("entity_id") or "")
        if entity_id not in ids:
            continue
        modality = str(row.get("modality") or "")
        if not modality or modality in (result.get(entity_id) or {}):
            continue
        result.setdefault(entity_id, {})[modality] = row
    return result


def _find_ready_record(entity_type: str, entity_id: str, modality: str, source_hash: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT *
            FROM embedding_records
            WHERE entity_type = ? AND entity_id = ? AND modality = ? AND model_name = ?
              AND source_hash = ? AND status = 'ready' AND vector_dim = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            [entity_type, entity_id, modality, QWEN_EMBEDDING_MODEL, source_hash, QWEN_EMBEDDING_DIM],
        )


def quarantine_invalid_qwen_embedding_records() -> int:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE embedding_records
            SET status = 'failed', error = ?, updated_at = ?
            WHERE model_name = ? AND status = 'ready' AND COALESCE(vector_dim, 0) <> ?
            """,
            ["invalid_qwen_embedding_dimension", now, QWEN_EMBEDDING_MODEL, QWEN_EMBEDDING_DIM],
        )
        conn.commit()
    return max(0, int(cursor.rowcount or 0))


def _store_embedding_record(
    *,
    entity_type: str,
    entity_id: str,
    account_id: str,
    dataset_id: str,
    platform_item_id: str,
    modality: str,
    source_hash: str,
    vector_path: Path | str = "",
    vector_dim: int = 0,
    status: str,
    error: str = "",
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM embedding_records
            WHERE entity_type = ? AND entity_id = ? AND modality = ? AND model_name = ? AND source_hash = ?
            """,
            [entity_type, entity_id, modality, QWEN_EMBEDDING_MODEL, source_hash],
        )
        insert_row(
            conn,
            "embedding_records",
            {
                "id": new_id("embrec"),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "account_id": account_id,
                "dataset_id": dataset_id,
                "platform_item_id": platform_item_id,
                "modality": modality,
                "model_name": QWEN_EMBEDDING_MODEL,
                "model_version": QWEN_EMBEDDING_VERSION,
                "vector_path": str(vector_path or ""),
                "vector_dim": int(vector_dim or 0),
                "source_hash": source_hash,
                "status": status,
                "error": error,
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.commit()


def _ready_record_count(entity_type: str, *, account_id: str | None, dataset_id: str | None, modalities: list[str]) -> int:
    clauses = ["entity_type = ?", "model_name = ?", "status = 'ready'", "vector_dim = ?"]
    params: list[Any] = [entity_type, QWEN_EMBEDDING_MODEL, QWEN_EMBEDDING_DIM]
    account = str(account_id or "").strip()
    dataset = str(dataset_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if dataset and dataset.lower() != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset)
    if modalities:
        clauses.append(f"modality IN ({','.join('?' for _ in modalities)})")
        params.extend(modalities)
    with connect() as conn:
        row = fetch_one(conn, f"SELECT COUNT(*) AS count FROM embedding_records WHERE {' AND '.join(clauses)}", params)
    return int((row or {}).get("count") or 0)


def _record_status_count(
    *,
    entity_type: str,
    account_id: str | None,
    dataset_id: str | None,
    modality: str,
    status: str,
    error: str,
) -> int:
    clauses = ["entity_type = ?", "modality = ?", "status = ?", "error = ?"]
    params: list[Any] = [entity_type, modality, status, error]
    account = str(account_id or "").strip()
    dataset = str(dataset_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if dataset and dataset.lower() != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset)
    with connect() as conn:
        row = fetch_one(conn, f"SELECT COUNT(*) AS count FROM embedding_records WHERE {' AND '.join(clauses)}", params)
    return int((row or {}).get("count") or 0)


def _vector_cache_path(entity_type: str, modality: str, entity_id: str, source_hash: str) -> Path:
    settings = ensure_data_dirs()
    return settings.cache_dir / "qwen_embeddings" / entity_type / modality / f"{_safe_name(entity_id)}_{source_hash[:16]}.json"


def _load_vector(record: dict) -> list[float]:
    path = Path(str(record.get("vector_path") or ""))
    if not path.is_file():
        return []
    data = read_json(path, default={}) or {}
    vector = data.get("vector") or data.get("embedding")
    if not isinstance(vector, list) or len(vector) != QWEN_EMBEDDING_DIM:
        return []
    try:
        parsed = [float(value) for value in vector]
        return parsed if all(math.isfinite(value) for value in parsed) else []
    except (TypeError, ValueError):
        return []


def _public_match(row: dict, similarity: float, *, modality: str) -> dict:
    return {
        "historical_sample_id": row.get("id") or row.get("training_sample_id") or "",
        "platform_item_id": row.get("platform_item_id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "",
        "title": row.get("title") or "",
        "similarity": round(float(similarity or 0.0), 4),
        "modality": modality,
        "reward_proxy": round(float(row.get("reward_proxy") or 0.0), 4),
        "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
        "performance_label": row.get("performance_label") or "",
        "match_type": row.get("performance_label") or "",
        "content_category": row.get("content_category") or "",
        "hook_type": row.get("hook_type") or "",
        "slice_structure": row.get("slice_structure") or "",
        "artist_names": row.get("artist_names") or "",
        "song_title": row.get("song_title") or "",
    }


def _coverage_from_vectors(vectors_by_id: dict, rows: list[dict]) -> dict:
    total = len(rows)
    text = sum(1 for row in rows if (vectors_by_id.get(str(row.get("id") or "")) or {}).get("text"))
    visual = sum(1 for row in rows if (vectors_by_id.get(str(row.get("id") or "")) or {}).get("visual"))
    return {
        "train_sample_count": total,
        "text_ready_count": text,
        "text_ready_rate": round(text / max(1, total), 4),
        "visual_ready_count": visual,
        "visual_ready_rate": round(visual / max(1, total), 4),
    }


def _top_low_risk_rows(rows: list[dict]) -> list[dict]:
    ranked = sorted(
        rows,
        key=lambda item: max(
            float((item.get("component_scores") or {}).get("qwen_text_low_risk_score") or 0.0),
            float((item.get("component_scores") or {}).get("qwen_visual_low_risk_score") or 0.0),
        ),
        reverse=True,
    )
    return ranked[:8]


def _evidence_recommendations(build: dict, rows: list[dict]) -> list[str]:
    coverage = build.get("coverage") if isinstance(build.get("coverage"), dict) else {}
    ready_rate = float(coverage.get("ready_rate") or 0.0)
    recs = []
    if build.get("status") == "service_unavailable":
        recs.append("Qwen embedding 服务不可达，先检查 DSO_EMBEDDING_SERVICE_URL 或远程 /health。")
    if ready_rate < 0.5:
        recs.append("embedding 覆盖不足，优先对高互动和低互动风险样本补建文本向量。")
    if not rows:
        recs.append("当前缺少可检索的相似证据，先运行 qwen-embeddings-build 后再做回测。")
    else:
        recs.append("已有相似历史证据，可进入 embedding research 策略回测；仍保持审核辅助口径。")
    return recs


def _embedding_ranker_reason(evidence: dict, quality: float) -> str:
    text_high = evidence.get("matched_text_high_samples") or []
    text_low = evidence.get("matched_text_low_samples") or []
    visual_high = evidence.get("matched_visual_high_samples") or []
    visual_low = evidence.get("matched_visual_low_samples") or []
    if quality <= 0:
        return "Qwen embedding 尚无可用历史相似证据，仅保留原历史排序器结论。"
    parts = []
    if text_high:
        parts.append(f"文本相似高互动 {len(text_high)} 条")
    if text_low:
        parts.append(f"文本低互动风险 {len(text_low)} 条")
    if visual_high:
        parts.append(f"视觉相似高互动 {len(visual_high)} 条")
    if visual_low:
        parts.append(f"视觉低互动风险 {len(visual_low)} 条")
    scope = evidence.get("embedding_scope") or "account"
    return f"Qwen embedding 检索到{' / '.join(parts) or '弱相似'}，证据范围 {scope}，仅作为审核辅助。"


def _combined_embedding_quality(evidence: dict) -> float:
    scores = []
    for key in ["text_similarity_score", "visual_similarity_score"]:
        scores.append(float(evidence.get(key) or 0.0))
    for key in ["matched_text_low_samples", "matched_visual_low_samples"]:
        values = evidence.get(key) if isinstance(evidence.get(key), list) else []
        if values:
            scores.append(max(float(item.get("similarity") or 0.0) for item in values))
    return max(scores or [0.0])


def _same_entity(left: dict, right: dict) -> bool:
    left_item = str(left.get("platform_item_id") or "").strip()
    right_item = str(right.get("platform_item_id") or "").strip()
    if left_item and right_item and left_item == right_item:
        return True
    left_title = _stable_title_key(left.get("title"))
    right_title = _stable_title_key(right.get("title"))
    return bool(left_title and right_title and left_title == right_title)


def _stable_title_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[@#《》【】\[\]（）()，,。.!！?？:：;；\"'“”‘’、\s]+", "", text)
    text = re.sub(r"\d+", "#", text)
    return text[:80]


def _interaction_label(row: dict, thresholds: tuple[float, float]) -> str:
    label = str(row.get("performance_label") or "").strip().lower()
    if label in {"high", "mid", "low"}:
        return label
    value = float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0)
    low_threshold, high_threshold = thresholds
    if value >= high_threshold:
        return "high"
    if value <= low_threshold:
        return "low"
    return "mid"


def _interaction_thresholds(rows: list[dict]) -> tuple[float, float]:
    values = sorted(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0) for row in rows)
    if not values:
        return (0.0, 0.0)
    return (_quantile(values, 0.25), _quantile(values, 0.75))


def _quantile(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    position = max(0.0, min(1.0, q)) * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[int(position)]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    totals = [0.0] * dim
    count = 0
    for vector in vectors:
        if len(vector) != dim:
            continue
        count += 1
        for index, value in enumerate(vector):
            totals[index] += float(value)
    if count <= 0:
        return []
    averaged = [value / count for value in totals]
    norm = math.sqrt(sum(value * value for value in averaged)) or 1.0
    return [round(value / norm, 8) for value in averaged]


def _extract_embedding_vectors(payload: Any) -> list[list[float]]:
    if not isinstance(payload, dict):
        return []
    candidates = []
    for key in ["embeddings", "embedding", "vectors", "vector"]:
        value = payload.get(key)
        if value is not None:
            candidates.append(value)
    data = payload.get("data")
    if isinstance(data, list):
        candidates.extend(item.get("embedding") for item in data if isinstance(item, dict))
    for candidate in candidates:
        parsed = _parse_vector_candidate(candidate)
        if parsed:
            return parsed
    return []


def _validated_embedding_vectors(payload: Any, *, endpoint: str) -> list[list[float]]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"{endpoint}_invalid_response")
    status = str(payload.get("status") or "").strip().lower()
    if status in {"fallback", "heuristic", "mock"}:
        raise RuntimeError(f"{endpoint}_fallback_rejected")
    vectors = _extract_embedding_vectors(payload)
    if not vectors:
        return []
    response_dim = int(payload.get("embedding_dim") or 0)
    for vector in vectors:
        if len(vector) != QWEN_EMBEDDING_DIM:
            raise RuntimeError(f"{endpoint}_unexpected_dimension:{len(vector)}")
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError(f"{endpoint}_non_finite_vector")
    if response_dim and response_dim != QWEN_EMBEDDING_DIM:
        raise RuntimeError(f"{endpoint}_unexpected_response_dimension:{response_dim}")
    return vectors


def _parse_vector_candidate(value: Any) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        return []
    if all(isinstance(item, (int, float)) for item in value):
        return [[float(item) for item in value]]
    vectors = []
    for item in value:
        if isinstance(item, list) and item and all(isinstance(value_item, (int, float)) for value_item in item):
            vectors.append([float(value_item) for value_item in item])
    return vectors


def _modalities(modality: str) -> list[str]:
    value = str(modality or "text").strip().lower()
    if value in {"all", "text_visual", "text+visual"}:
        return ["text", "visual"]
    if value in {"visual", "image", "video"}:
        return ["visual"]
    return ["text"]


def _entity_id(row: dict, entity_type: str) -> str:
    return str(row.get("id") or row.get("sample_id") or row.get("candidate_segment_id") or row.get("platform_item_id") or entity_type)


def _source_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or "unknown"))[:96] or "unknown"


def _scope(value: str | None) -> str:
    text = str(value or "").strip()
    return text if text else "all"


def embedding_service_ready(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status in {"service_unavailable", "model_switch_required", "model_not_loaded", "load_failed", "error"}:
        return False
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else payload
    if not isinstance(raw, dict):
        return False
    model_id = _loaded_model_id(raw)
    if model_id and _normalize_model_id(model_id) != _normalize_model_id(QWEN_EMBEDDING_MODEL):
        return False
    model = raw.get("model") if isinstance(raw.get("model"), dict) else None
    if model is not None and not _model_loaded(raw):
        return False
    backend = str((model or raw).get("backend") or raw.get("backend") or "").strip().lower()
    if backend == "qwen_omni":
        return False
    raw_status = str(raw.get("status") or "").strip().lower()
    return status in {"ready", "ok", "loaded", "model", "healthy"} or raw_status in {
        "ready",
        "ok",
        "loaded",
        "model",
        "healthy",
    }


def _service_ready(payload: dict | None) -> bool:
    return embedding_service_ready(payload)


def _embedding_service_status(payload: dict) -> str:
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status not in {"ready", "ok", "loaded", "model", "healthy"}:
        return raw_status or "unknown"
    model_id = _loaded_model_id(payload)
    if model_id and _normalize_model_id(model_id) != _normalize_model_id(QWEN_EMBEDDING_MODEL):
        return "model_switch_required"
    model = payload.get("model") if isinstance(payload.get("model"), dict) else None
    if model is not None and not _model_loaded(payload):
        return "model_not_loaded"
    backend = str((model or payload).get("backend") or payload.get("backend") or "").strip().lower()
    if backend == "qwen_omni":
        return "model_switch_required"
    return "ready"


def _loaded_model_id(payload: dict) -> str:
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    return str(model.get("model_id") or payload.get("model_id") or env.get("model_id") or "").strip()


def _model_loaded(payload: dict) -> bool:
    model = payload.get("model") if isinstance(payload.get("model"), dict) else None
    if model is not None:
        return bool(model.get("loaded"))
    if "model_loaded" in payload:
        return bool(payload.get("model_loaded"))
    if "loaded" in payload:
        return bool(payload.get("loaded"))
    return str(payload.get("status") or "").strip().lower() in {"loaded", "model"}


def _normalize_model_id(value: str) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _existing_paths(values: list[str]) -> list[Path]:
    result = []
    seen = set()
    for value in values:
        path = Path(str(value))
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        result.append(path)
    return result


def _empty_modality_score(base_score: float) -> dict:
    return {
        "score": round(float(base_score or 50.0), 4),
        "quality": 0.0,
        "positive_similarity": 0.0,
        "risk_similarity": 0.0,
        "high_matches": [],
        "low_matches": [],
        "scope": "none",
        "global_fallback": False,
    }
