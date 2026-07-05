from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

from dso.db.session import connect, fetch_all, fetch_one
from dso.utils import utc_now
from dso.versions import QWEN_OMNI_VERSION


QWEN_OMNI_MODEL = "Qwen/Qwen2.5-Omni-7B-GPTQ-Int4"
DEFAULT_OMNI_SERVICE_URL = "http://192.168.31.143:8001"
DEFAULT_MAX_CLIP_SECONDS = 15.0
GPTQ_INT4_15S_MIN_VRAM_GB = 11.64
GPTQ_INT4_30S_MIN_VRAM_GB = 17.43
BF16_15S_MIN_VRAM_GB = 31.11
VRAM_SAFETY_MULTIPLIER = 1.2


class QwenOmniClient:
    def __init__(
        self,
        service_url: str | None = None,
        *,
        model_id: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.service_url = (
            service_url
            or os.environ.get("DSO_QWEN_OMNI_SERVICE_URL")
            or os.environ.get("DSO_OMNI_SERVICE_URL")
            or os.environ.get("DSO_EMBEDDING_SERVICE_URL")
            or DEFAULT_OMNI_SERVICE_URL
        ).rstrip("/")
        self.model_id = model_id or os.environ.get("DSO_QWEN_OMNI_MODEL") or QWEN_OMNI_MODEL
        self.timeout_seconds = float(os.environ.get("DSO_QWEN_OMNI_TIMEOUT_SECONDS") or timeout_seconds or 60.0)

    def health(self) -> dict:
        try:
            payload = self._json_request("GET", "/health", timeout_seconds=min(self.timeout_seconds, 8.0))
            return _service_health(payload, self.service_url)
        except Exception as exc:
            return {"status": "service_unavailable", "service_url": self.service_url, "error": str(exc)}

    def load(self, *, model_id: str | None = None, max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS) -> dict:
        payload = {
            "model": model_id or self.model_id,
            "model_id": model_id or self.model_id,
            "profile": "low_vram",
            "low_vram": True,
            "return_audio": False,
            "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        }
        try:
            data = self._json_request("POST", "/load", payload, timeout_seconds=max(self.timeout_seconds, 120.0))
            return _service_health(data, self.service_url)
        except Exception as exc:
            return {"status": "service_unavailable", "service_url": self.service_url, "error": str(exc)}

    def analyze_clip(self, payload: dict) -> dict:
        return self._json_request("POST", "/analyze/clip", payload, timeout_seconds=max(self.timeout_seconds, 120.0))

    def _json_request(self, method: str, path: str, payload: dict | None = None, *, timeout_seconds: float | None = None) -> dict:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if shutil.which("curl"):
            return self._curl_json_request(method, path, body, timeout_seconds=timeout_seconds)
        request = urllib.request.Request(
            f"{self.service_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        with urllib.request.urlopen(request, timeout=float(timeout_seconds or self.timeout_seconds)) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw or "{}")

    def _curl_json_request(self, method: str, path: str, body: bytes | None, *, timeout_seconds: float | None = None) -> dict:
        timeout = max(1.0, float(timeout_seconds or self.timeout_seconds))
        command = [
            "curl",
            "-sS",
            "--connect-timeout",
            str(min(3.0, timeout)),
            "--max-time",
            str(timeout),
            "-w",
            "\n%{http_code}",
            "-X",
            method,
        ]
        if body is not None:
            command.extend(["-H", "Content-Type: application/json", "--data-binary", "@-"])
        command.append(f"{self.service_url}{path}")
        result = subprocess.run(command, input=body, capture_output=True, check=False)
        output = result.stdout.decode("utf-8", errors="replace")
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or f"curl_exit_{result.returncode}")
        if "\n" not in output:
            raise RuntimeError("curl_response_missing_status")
        raw, status_code = output.rsplit("\n", 1)
        try:
            code = int(status_code)
        except ValueError as exc:
            raise RuntimeError(f"curl_response_bad_status:{status_code}") from exc
        if code >= 400:
            raise RuntimeError(f"HTTP Error {code}: {raw[:240]}")
        return json.loads(raw or "{}")


def qwen_omni_status(client: QwenOmniClient | None = None) -> dict:
    client = client or QwenOmniClient()
    health = client.health()
    raw = _raw_health(health)
    loaded_model = _loaded_model_id(raw)
    gate = _resource_gate(raw)
    loaded_omni = _is_omni_model(loaded_model)
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": _deployment_status(health, gate, loaded_omni),
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "service_url": getattr(client, "service_url", ""),
        "service_status": health,
        "resource_gate": gate,
        "loaded_model": loaded_model,
        "loaded_omni": loaded_omni,
        "model_switch_required": bool(loaded_model and not loaded_omni),
        "mode": "shadow",
        "limits": {
            "default_max_clip_seconds": DEFAULT_MAX_CLIP_SECONDS,
            "batch_size": 1,
            "return_audio": False,
            "writes_labels": False,
            "production_weight": False,
        },
        "recommendations": _status_recommendations(health, gate, loaded_omni),
        "generated_at": utc_now(),
    }


def analyze_candidate_with_qwen_omni(
    segment_id: str,
    *,
    account_id: str | None = None,
    max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS,
    load_model: bool = False,
    client: QwenOmniClient | None = None,
) -> dict:
    row = _candidate_row(segment_id)
    if not row:
        raise KeyError(f"segment not found: {segment_id}")
    if account_id:
        row["account_id"] = account_id
    client = client or QwenOmniClient()
    duration = _duration_seconds(row)
    if duration > float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS):
        return _skipped_payload(
            entity_type="candidate",
            entity_id=segment_id,
            reason="clip_too_long_for_low_vram",
            duration_seconds=duration,
            max_clip_seconds=max_clip_seconds,
            client=client,
        )
    service_status = client.load(max_clip_seconds=max_clip_seconds) if load_model else client.health()
    if not _service_ready(service_status):
        return _service_unavailable_payload(
            entity_type="candidate",
            entity_id=segment_id,
            service_status=service_status,
            client=client,
        )
    if not _service_loaded_omni(service_status):
        return _model_switch_required_payload(
            entity_type="candidate",
            entity_id=segment_id,
            service_status=service_status,
            client=client,
        )
    payload = _candidate_payload(row, max_clip_seconds=max_clip_seconds, model_id=getattr(client, "model_id", QWEN_OMNI_MODEL))
    raw = client.analyze_clip(payload)
    return _analysis_payload(
        entity_type="candidate",
        entity_id=segment_id,
        row=row,
        raw=raw,
        service_status=service_status,
        client=client,
        max_clip_seconds=max_clip_seconds,
    )


def run_qwen_omni_shadow(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 20,
    max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS,
    load_model: bool = False,
    client: QwenOmniClient | None = None,
) -> dict:
    client = client or QwenOmniClient()
    rows = _historical_rows(account_id=account_id, dataset_id=dataset_id, limit=max(1, int(limit or 20)))
    service_status = client.load(max_clip_seconds=max_clip_seconds) if load_model else client.health()
    if not _service_ready(service_status):
        return {
            "contract_version": QWEN_OMNI_VERSION,
            "status": "service_unavailable",
            "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
            "mode": "shadow",
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "sample_count": len(rows),
            "analyzed_count": 0,
            "skipped_count": 0,
            "service_status": service_status,
            "recommendations": ["Omni 服务不可用或模型未加载，先检查 /health 与 /load。"],
            "generated_at": utc_now(),
        }
    if not _service_loaded_omni(service_status):
        loaded_model = _loaded_model_id(_raw_health(service_status))
        return {
            "contract_version": QWEN_OMNI_VERSION,
            "status": "model_switch_required",
            "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
            "mode": "shadow",
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "sample_count": len(rows),
            "analyzed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "loaded_model": loaded_model,
            "model_switch_required": True,
            "service_status": service_status,
            "recommendations": [
                f"当前服务加载的是 {loaded_model or 'unknown'}，不是 Qwen2.5-Omni 低显存模型。",
                "需要在目标服务端重启或修复 /load，使其加载 Qwen/Qwen2.5-Omni-7B-GPTQ-Int4 后再跑 shadow-run。",
            ],
            "generated_at": utc_now(),
        }
    samples = []
    counts: Counter[str] = Counter()
    for row in rows:
        duration = _duration_seconds(row)
        sample_id = str(row.get("id") or row.get("platform_item_id") or "")
        if duration > float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS):
            counts["skipped"] += 1
            samples.append(
                {
                    "sample_id": sample_id,
                    "status": "skipped",
                    "reason": "clip_too_long_for_low_vram",
                    "duration_seconds": round(duration, 3),
                    "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
                    "title": row.get("title") or "",
                }
            )
            continue
        try:
            raw = client.analyze_clip(_historical_payload(row, max_clip_seconds=max_clip_seconds, model_id=getattr(client, "model_id", QWEN_OMNI_MODEL)))
            item = _analysis_payload(
                entity_type="historical_sample",
                entity_id=sample_id,
                row=row,
                raw=raw,
                service_status=service_status,
                client=client,
                max_clip_seconds=max_clip_seconds,
            )
            counts[item["status"]] += 1
            samples.append(item)
        except Exception as exc:
            counts["failed"] += 1
            samples.append({"sample_id": sample_id, "status": "failed", "error": str(exc), "title": row.get("title") or ""})
    analyzed = sum(count for key, count in counts.items() if key not in {"skipped", "failed"})
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "ready" if analyzed else ("low_confidence" if samples else "empty"),
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "sample_count": len(rows),
        "analyzed_count": analyzed,
        "skipped_count": int(counts.get("skipped", 0)),
        "failed_count": int(counts.get("failed", 0)),
        "status_counts": dict(counts),
        "service_status": service_status,
        "samples": samples[: max(1, int(limit or 20))],
        "recommendations": _shadow_recommendations(counts, len(rows), max_clip_seconds),
        "generated_at": utc_now(),
    }


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


def _historical_rows(account_id: str | None, dataset_id: str | None, limit: int) -> list[dict]:
    clauses = ["COALESCE(platform_item_id, '') != ''"]
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
        LIMIT ?
    """
    params.append(max(1, int(limit or 20)))
    with connect() as conn:
        return fetch_all(conn, query, params)


def _candidate_payload(row: dict, *, max_clip_seconds: float, model_id: str) -> dict:
    return {
        "model": model_id,
        "mode": "shadow",
        "return_audio": False,
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "entity_type": "candidate",
        "segment_id": row.get("id") or "",
        "account_id": row.get("account_id") or "",
        "title": row.get("video_title") or row.get("summary") or "",
        "transcript": row.get("transcript") or row.get("summary") or "",
        "tags": _tags(
            [
                row.get("primary_topic"),
                row.get("music_slice_type"),
                row.get("emotion_type"),
                row.get("short_video_structure"),
                row.get("musical_moment"),
                row.get("program_context"),
            ]
        ),
        "duration_seconds": _duration_seconds(row),
    }


def _historical_payload(row: dict, *, max_clip_seconds: float, model_id: str) -> dict:
    return {
        "model": model_id,
        "mode": "shadow",
        "return_audio": False,
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "entity_type": "historical_sample",
        "sample_id": row.get("id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "",
        "title": row.get("title") or "",
        "transcript": row.get("description") or row.get("title") or "",
        "tags": _tags([row.get("tags"), row.get("artist_names"), row.get("song_title"), row.get("hook_type"), row.get("slice_structure")]),
        "duration_seconds": _duration_seconds(row),
    }


def _analysis_payload(
    *,
    entity_type: str,
    entity_id: str,
    row: dict,
    raw: dict,
    service_status: dict,
    client: QwenOmniClient,
    max_clip_seconds: float,
) -> dict:
    status = str(raw.get("status") or raw.get("decision") or "ready")
    suggestions = _semantic_suggestions(raw)
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": status,
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": row.get("title") or row.get("video_title") or "",
        "duration_seconds": round(_duration_seconds(row), 3),
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "service_status": service_status,
        "semantic_suggestions": suggestions,
        "scores": raw.get("scores") if isinstance(raw.get("scores"), dict) else {},
        "advice": raw.get("advice") or raw.get("recommendation") or "recommend_review",
        "risk_flags": raw.get("risk_flags") if isinstance(raw.get("risk_flags"), list) else [],
        "raw": raw,
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _semantic_suggestions(raw: dict) -> dict:
    candidates = [raw.get("semantic_suggestions"), raw.get("semantic"), raw.get("labels"), raw]
    fields = ["content_category", "hook_type", "slice_structure", "artist_names", "song_title", "tags"]
    result = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for field in fields:
            if field in candidate and field not in result:
                result[field] = candidate.get(field)
    return result


def _skipped_payload(
    *,
    entity_type: str,
    entity_id: str,
    reason: str,
    duration_seconds: float,
    max_clip_seconds: float,
    client: QwenOmniClient,
) -> dict:
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "skipped",
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "reason": reason,
        "duration_seconds": round(float(duration_seconds or 0.0), 3),
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _service_unavailable_payload(*, entity_type: str, entity_id: str, service_status: dict, client: QwenOmniClient) -> dict:
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "service_unavailable",
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "service_status": service_status,
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _model_switch_required_payload(*, entity_type: str, entity_id: str, service_status: dict, client: QwenOmniClient) -> dict:
    loaded_model = _loaded_model_id(_raw_health(service_status))
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "model_switch_required",
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "loaded_model": loaded_model,
        "model_switch_required": True,
        "service_status": service_status,
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _service_health(payload: dict, service_url: str) -> dict:
    status = "ready" if str(payload.get("status") or "").lower() in {"ready", "ok", "model", "loaded"} else str(payload.get("status") or "unknown")
    return {"status": status, "service_url": service_url, "raw": payload}


def _service_ready(status: dict) -> bool:
    return str(status.get("status") or "").lower() in {"ready", "ok", "model", "loaded", "heuristic"}


def _raw_health(status: dict) -> dict:
    return status.get("raw") if isinstance(status.get("raw"), dict) else status


def _service_loaded_omni(status: dict) -> bool:
    return _is_omni_model(_loaded_model_id(_raw_health(status)))


def _resource_gate(raw: dict) -> dict:
    device = _primary_cuda_device(raw)
    total = float(device.get("total_memory_gb") or 0.0) if device else 0.0
    required_15 = round(GPTQ_INT4_15S_MIN_VRAM_GB * VRAM_SAFETY_MULTIPLIER, 2)
    required_30 = round(GPTQ_INT4_30S_MIN_VRAM_GB * VRAM_SAFETY_MULTIPLIER, 2)
    bf16_required = round(BF16_15S_MIN_VRAM_GB * VRAM_SAFETY_MULTIPLIER, 2)
    return {
        "cuda_available": bool(((raw.get("torch") or {}).get("cuda_available"))),
        "device": device,
        "total_memory_gb": round(total, 2),
        "gptq_int4_15s_required_gb_with_margin": required_15,
        "gptq_int4_30s_required_gb_with_margin": required_30,
        "bf16_15s_required_gb_with_margin": bf16_required,
        "supports_gptq_int4_15s": total >= required_15,
        "supports_gptq_int4_30s": total >= required_30,
        "supports_bf16_15s": total >= bf16_required,
        "recommended_max_clip_seconds": 30 if total >= required_30 else 15 if total >= required_15 else 0,
    }


def _primary_cuda_device(raw: dict) -> dict:
    devices = ((raw.get("torch") or {}).get("devices") or []) if isinstance(raw.get("torch"), dict) else []
    return devices[0] if devices and isinstance(devices[0], dict) else {}


def _loaded_model_id(raw: dict) -> str:
    model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    return str(model.get("model_id") or raw.get("model_id") or env.get("model_id") or "")


def _is_omni_model(model_id: str) -> bool:
    normalized = str(model_id or "").lower()
    return "qwen2.5-omni" in normalized and ("gptq" in normalized or "awq" in normalized or "int4" in normalized)


def _deployment_status(health: dict, gate: dict, loaded_omni: bool) -> str:
    if not _service_ready(health):
        return "service_unavailable"
    if not gate.get("supports_gptq_int4_15s"):
        return "insufficient_vram"
    return "ready" if loaded_omni else "model_switch_required"


def _status_recommendations(health: dict, gate: dict, loaded_omni: bool) -> list[str]:
    recs = []
    if not _service_ready(health):
        recs.append("先启动远程多模态模型服务，并确认 /health 可访问。")
    if not gate.get("supports_gptq_int4_15s"):
        recs.append("当前显存不足以稳定运行 15 秒 GPTQ-Int4 Omni 低显存实验。")
    elif not gate.get("supports_gptq_int4_30s"):
        recs.append("当前资源只建议跑 15 秒以内短片段，batch_size=1。")
    if not loaded_omni:
        recs.append("当前服务未加载 Omni 低显存模型；需要显式 /load 后再跑 shadow 分析。")
    recs.append("Omni 输出仅作为校准建议，不直接写 manual_verified 或生产排序权重。")
    return recs


def _shadow_recommendations(counts: Counter[str], total: int, max_clip_seconds: float) -> list[str]:
    recs = [f"继续保持 shadow mode，短片段上限 {float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS):.0f} 秒。"]
    if counts.get("skipped"):
        recs.append("存在超长样本被跳过；低显存版本优先采样 15 秒以内候选。")
    if not total:
        recs.append("当前筛选下没有历史样本，先确认 account_id/dataset_id。")
    recs.append("对比 Omni 建议与人工校准一致率后，再决定是否进入回测特征。")
    return recs


def _duration_seconds(row: dict) -> float:
    value = row.get("duration_seconds")
    try:
        duration = float(value or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0 and row.get("start_time") is not None and row.get("end_time") is not None:
        try:
            duration = max(0.0, float(row.get("end_time") or 0.0) - float(row.get("start_time") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
    return duration


def _tags(values: list[Any]) -> list[str]:
    tags = []
    for value in values:
        if isinstance(value, list):
            parts = value
        else:
            parts = str(value or "").replace("|", ",").replace("，", ",").split(",")
        for part in parts:
            text = str(part or "").strip(" #")
            if text and text not in tags:
                tags.append(text)
    return tags[:12]
