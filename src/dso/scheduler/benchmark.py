from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from dso.config import ensure_data_dirs
from dso.scheduler.contracts import JobItemSpec, ModelJobSpec, stable_json_hash
from dso.scheduler.db import scheduler_connect
from dso.scheduler.repository import ModelJobRepository
from dso.scheduler.worker import ModelWorker
from dso.utils import read_json, utc_now, write_json


DEFAULT_MANIFEST = Path(__file__).resolve().parents[3] / "benchmarks" / "model-scheduler-mixed-20260718-r1.json"


class _BenchmarkAdapter:
    def execute(self, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ready", "value": int((item.get("request") or {}).get("value") or 0)}

    def commit_item(self, job: dict[str, Any], item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ready", "value": result["value"]}

    def finalize(self, job: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
        return {"status": "ready", "item_count": len(results)}


@dataclass
class _BenchmarkRuntimeManager:
    current_profile: str = ""
    switches: int = 0

    def ensure_profile(self, claim) -> dict[str, Any]:
        profile = str(claim.job.get("model_profile_id") or "")
        warm = profile == self.current_profile
        if self.current_profile and not warm:
            self.switches += 1
        self.current_profile = profile
        return {"status": "ready", "warm_hit": warm, "actual_model_id": profile, "load_ms": 0 if warm else 1}


def run_model_scheduler_benchmark(manifest_path: str | Path | None = None, *, output_path: str | Path | None = None) -> dict[str, Any]:
    selected_manifest = Path(manifest_path or DEFAULT_MANIFEST)
    manifest = read_json(selected_manifest, default={}) or {}
    jobs = manifest.get("jobs") if isinstance(manifest.get("jobs"), list) else []
    if not jobs:
        raise ValueError("model scheduler benchmark manifest has no jobs")
    acceptance = manifest.get("acceptance") if isinstance(manifest.get("acceptance"), dict) else {}
    previous_root = os.environ.get("DSO_ROOT")
    previous_burst = os.environ.get("DSO_MODEL_MAX_PARENT_BURST")
    enqueue_ms: list[float] = []
    enqueued_job_ids: dict[str, str] = {}
    runtime = _BenchmarkRuntimeManager()
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="dso-model-scheduler-benchmark-") as temp_root:
            os.environ["DSO_ROOT"] = temp_root
            os.environ["DSO_MODEL_MAX_PARENT_BURST"] = str(int(acceptance.get("max_parent_burst") or 4))
            repository = ModelJobRepository()
            adapter = _BenchmarkAdapter()
            adapters = {str(job["job_kind"]): adapter for job in jobs}
            for job in jobs:
                enqueue_started = time.monotonic()
                enqueued = repository.enqueue(_job_spec(job))
                enqueued_job_ids[str(job.get("id") or "")] = str(enqueued.job["job_id"])
                enqueue_ms.append((time.monotonic() - enqueue_started) * 1000)
            worker = ModelWorker(repository, "benchmark-worker", adapters=adapters, runtime_manager=runtime)  # type: ignore[arg-type]
            expected_items = sum(int(job.get("items") or 0) for job in jobs)
            processed = 0
            while processed < expected_items:
                result = worker.run_once()
                if result is None:
                    raise RuntimeError("benchmark scheduler became idle before all items completed")
                processed += 1
            with scheduler_connect(repository.db_path) as connection:
                attempts = connection.execute(
                    "SELECT job_id, model_profile_id, status, started_at, finished_at FROM model_job_attempts ORDER BY started_at, id"
                ).fetchall()
                terminal = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM model_jobs GROUP BY status"
                ).fetchall()
                events = connection.execute(
                    "SELECT reason_code, COUNT(*) AS count FROM model_job_events WHERE event_type = 'job_dispatched' GROUP BY reason_code"
                ).fetchall()
            dispatch_profiles = [str(row["model_profile_id"] or "") for row in attempts]
            dispatch_switches = _switch_count(dispatch_profiles)
            dispatch_job_ids = [str(row["job_id"] or "") for row in attempts]
            priority_groups: dict[str, list[str]] = {}
            for job in jobs:
                priority_groups.setdefault(str(job.get("priority_class") or ""), []).append(str(job.get("id") or ""))
            fair_ids = {
                enqueued_job_ids[job_id]
                for group in priority_groups.values()
                if len(group) > 1
                for job_id in group
            }
            max_parent_burst = _max_run_for_ids(dispatch_job_ids, fair_ids)
            terminal_counts = {str(row["status"]): int(row["count"] or 0) for row in terminal}
            dispatch_reasons = {str(row["reason_code"]): int(row["count"] or 0) for row in events}
    finally:
        if previous_root is None:
            os.environ.pop("DSO_ROOT", None)
        else:
            os.environ["DSO_ROOT"] = previous_root
        if previous_burst is None:
            os.environ.pop("DSO_MODEL_MAX_PARENT_BURST", None)
        else:
            os.environ["DSO_MODEL_MAX_PARENT_BURST"] = previous_burst

    wall_ms = (time.monotonic() - started) * 1000
    baseline_profiles = [
        str(job.get("profile_id") or "")
        for item_index in range(max(int(job.get("items") or 0) for job in jobs))
        for job in jobs
        if item_index < int(job.get("items") or 0)
    ]
    baseline_switches = _switch_count(baseline_profiles)
    switch_reduction = 1.0 if baseline_switches == 0 and dispatch_switches == 0 else (
        (baseline_switches - dispatch_switches) / baseline_switches if baseline_switches else 0.0
    )
    simulated_inference_ms = sum(int(job.get("items") or 0) * float(job.get("simulated_inference_ms") or 0) for job in jobs)
    overhead_rate = wall_ms / max(1.0, simulated_inference_ms)
    enqueue_p95 = _percentile(enqueue_ms, 0.95)
    checks = {
        "all_jobs_succeeded": terminal_counts.get("succeeded", 0) == len(jobs),
        "all_items_attempted_once": len(dispatch_profiles) == sum(int(job.get("items") or 0) for job in jobs),
        "parent_burst_within_limit": max_parent_burst <= int(acceptance.get("max_parent_burst") or 4),
        "enqueue_p95_within_target": enqueue_p95 <= float(acceptance.get("maximum_enqueue_p95_ms") or 500),
        "scheduler_overhead_within_target": overhead_rate <= float(acceptance.get("maximum_scheduler_overhead_rate") or 0.05),
        "switch_reduction_within_target": switch_reduction >= float(acceptance.get("minimum_switch_reduction_rate") or 0.6),
    }
    result = {
        "contract_version": "model_scheduler_benchmark_report.v1",
        "benchmark_id": manifest.get("benchmark_id") or selected_manifest.stem,
        "status": "passed" if all(checks.values()) else "validate",
        "workload_kind": "synthetic_contract_workload",
        "manifest_path": str(selected_manifest),
        "manifest_sha256": _file_sha256(selected_manifest),
        "job_count": len(jobs),
        "item_count": len(dispatch_profiles),
        "metrics": {
            "enqueue_p95_ms": round(enqueue_p95, 3),
            "scheduler_wall_ms": round(wall_ms, 3),
            "simulated_inference_ms": round(simulated_inference_ms, 3),
            "scheduler_overhead_rate": round(overhead_rate, 6),
            "baseline_switches": baseline_switches,
            "scheduled_switches": dispatch_switches,
            "switch_reduction_rate": round(switch_reduction, 6),
            "max_parent_burst": max_parent_burst,
        },
        "checks": checks,
        "terminal_counts": terminal_counts,
        "dispatch_reasons": dispatch_reasons,
        "limitations": [
            "This workload validates scheduler contracts and policy only; it does not execute real ASR, Omni or Embedding inference.",
            "GPU idle-gap, VRAM peak, OOM and model output equivalence require the live mixed-workload benchmark.",
        ],
        "production_weight_changed": False,
        "writes_manual_gold": False,
        "generated_at": utc_now(),
    }
    target = Path(output_path) if output_path else ensure_data_dirs().root / "outputs" / "model_scheduler_benchmarks" / f"{result['benchmark_id']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    write_json(target, result)
    return {**result, "report_path": str(target)}


def _job_spec(job: dict[str, Any]) -> ModelJobSpec:
    values = list(range(int(job.get("items") or 0)))
    key = str(job.get("id") or "job")
    input_hash = stable_json_hash({"job": key, "values": values})
    return ModelJobSpec(
        job_kind=str(job["job_kind"]),
        subject_type="benchmark",
        subject_id=key,
        account_id="",
        resource_class="gpu:0",
        model_profile_id=str(job["profile_id"]),
        model_id=str(job["profile_id"]),
        model_version="benchmark.fake.v1",
        prompt_version="benchmark.fake.v1",
        priority_class=str(job["priority_class"]),
        base_priority=int(job["base_priority"]),
        input_hash=input_hash,
        parameters_hash=stable_json_hash({"simulated_inference_ms": job.get("simulated_inference_ms")}),
        dedupe_key=stable_json_hash({"benchmark_job": key}),
        request_summary={"benchmark_job": key},
        fallback_ref={"status": "ready", "source": "benchmark_baseline"},
        items=tuple(
            JobItemSpec(
                item_kind="benchmark_item",
                item_role=f"item_{index}",
                input_hash=stable_json_hash({"job": key, "item": index}),
                request={"value": index},
            )
            for index in values
        ),
    )


def _switch_count(profiles: list[str]) -> int:
    return sum(1 for previous, current in zip(profiles, profiles[1:]) if previous != current)


def _max_run_for_ids(values: list[str], eligible: set[str]) -> int:
    maximum = 0
    current = 0
    previous = None
    for value in values:
        current = current + 1 if value == previous else 1
        if value in eligible:
            maximum = max(maximum, current)
        previous = value
    return maximum


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return float(ordered[index])


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
