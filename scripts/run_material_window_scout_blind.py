#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from smoke_material_window_scout import CUE_SET_VERSION, cue_set_digest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a frozen local material-window selector benchmark.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs" / "material_window_scout" / "blind6")
    parser.add_argument("--summary", type=Path, default=PROJECT_ROOT / "outputs" / "material_window_scout" / "blind6_summary.json")
    parser.add_argument("--reuse-existing", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    reports: list[tuple[dict, dict]] = []
    samples = manifest.get("samples") or []
    for index, sample in enumerate(samples, start=1):
        report_path = _report_path(sample, output_dir)
        if args.reuse_existing and report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            _validate_report(report, sample, allow_legacy=bool(sample.get("allow_legacy_report")))
            print(f"[{index}/{len(samples)}] reused {sample['sample_id']}", flush=True)
        else:
            print(f"[{index}/{len(samples)}] scanning {sample['sample_id']}", flush=True)
            report = _run_sample(sample, report_path, manifest.get("query") or {})
            _validate_report(report, sample, allow_legacy=False)
            print(
                f"[{index}/{len(samples)}] ready {sample['sample_id']} "
                f"in {float((report.get('timings') or {}).get('total_seconds') or 0):.2f}s",
                flush=True,
            )
        reports.append((sample, report))

    summary = _summarize(manifest, manifest_path, reports)
    summary_path = args.summary.expanduser().resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"], "summary_path": str(summary_path), **summary["metrics"]}, ensure_ascii=False, indent=2))


def _validate_manifest(manifest: dict) -> None:
    if str(manifest.get("contract_version") or "") != "material_window_scout.blind_manifest.v1":
        raise ValueError("unsupported blind manifest contract")
    expected_digest = cue_set_digest()
    if str(manifest.get("cue_set_version") or "") != CUE_SET_VERSION:
        raise ValueError("cue set version drift detected")
    if str(manifest.get("cue_set_sha256") or "") != expected_digest:
        raise ValueError("cue set digest drift detected; create a new manifest instead of changing the frozen run")
    samples = manifest.get("samples") or []
    if not samples:
        raise ValueError("manifest has no samples")
    seen = set()
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        if not sample_id or sample_id in seen:
            raise ValueError(f"invalid or duplicate sample_id: {sample_id}")
        seen.add(sample_id)
        video_path = _project_path(sample.get("video_path"))
        if not video_path.is_file():
            raise FileNotFoundError(f"video not found for {sample_id}: {video_path}")


def _run_sample(sample: dict, report_path: Path, query: dict) -> dict:
    video_path = _project_path(sample.get("video_path"))
    command = [
        sys.executable,
        str(SCRIPT_DIR / "smoke_material_window_scout.py"),
        str(video_path),
        "--sample-id",
        str(sample["sample_id"]),
        "--confusion-pair",
        str(sample["confusion_pair"]),
        "--window-seconds",
        str(float(query.get("window_seconds") or 15.0)),
        "--stride-seconds",
        str(float(query.get("stride_seconds") or 5.0)),
        "--ocr-interval-seconds",
        str(float(query.get("ocr_interval_seconds") or 15.0)),
        "--max-ocr-frames",
        str(int(query.get("max_ocr_frames") or 40)),
        "--max-windows",
        str(int(query.get("max_windows") or 2)),
        "--asr-model",
        str(query.get("asr_model") or "small"),
        "--output",
        str(report_path),
    ]
    environment = dict(os.environ)
    source_root = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(value for value in [source_root, environment.get("PYTHONPATH", "")] if value)
    result = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error")[-4000:]
        raise RuntimeError(f"selector failed for {sample['sample_id']}: {detail}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def _validate_report(report: dict, sample: dict, *, allow_legacy: bool) -> None:
    if str(report.get("sample_id") or "") != str(sample.get("sample_id") or ""):
        raise ValueError(f"report sample mismatch for {sample.get('sample_id')}")
    if str(report.get("confusion_pair") or "") != str(sample.get("confusion_pair") or ""):
        raise ValueError(f"report confusion-pair mismatch for {sample.get('sample_id')}")
    report_digest = str((report.get("query") or {}).get("cue_set_sha256") or "")
    if not allow_legacy and report_digest != cue_set_digest():
        raise ValueError(f"report cue-set drift for {sample.get('sample_id')}")


def _summarize(manifest: dict, manifest_path: Path, reports: list[tuple[dict, dict]]) -> dict:
    rows = [_sample_summary(sample, report) for sample, report in reports]
    blind_rows = [row for row in rows if row["evaluation_role"] == "blind_eval"]
    adaptive_scores = [score for row in rows for score in row["adaptive_scores"]]
    fixed_scores = [score for row in rows for score in row["fixed_scores"]]
    blind_adaptive_scores = [score for row in blind_rows for score in row["adaptive_scores"]]
    blind_fixed_scores = [score for row in blind_rows for score in row["fixed_scores"]]
    blind_useful_count = sum(1 for row in blind_rows if row["selector_useful_proxy"])
    blind_ready_count = sum(1 for row in blind_rows if row["status"] == "ready")
    blind_target = int((manifest.get("acceptance") or {}).get("minimum_useful_blind_samples") or len(blind_rows))
    metrics = {
        "sample_count": len(rows),
        "pilot_count": sum(1 for row in rows if row["evaluation_role"] == "pilot"),
        "blind_sample_count": len(blind_rows),
        "ready_count": sum(1 for row in rows if row["status"] == "ready"),
        "blind_ready_count": blind_ready_count,
        "asr_covered_count": sum(1 for row in rows if row["asr_segment_count"] > 0),
        "ocr_covered_count": sum(1 for row in rows if row["ocr_nonempty_frame_count"] > 0),
        "adaptive_mean_information": _mean(adaptive_scores),
        "fixed_mean_information": _mean(fixed_scores),
        "mean_sample_uplift": _mean([row["adaptive_vs_fixed_mean_uplift"] for row in rows]),
        "blind_adaptive_mean_information": _mean(blind_adaptive_scores),
        "blind_fixed_mean_information": _mean(blind_fixed_scores),
        "blind_mean_sample_uplift": _mean([row["adaptive_vs_fixed_mean_uplift"] for row in blind_rows]),
        "blind_useful_proxy_count": blind_useful_count,
        "blind_useful_proxy_rate": round(blind_useful_count / max(1, len(blind_rows)), 4),
        "pair_relevant_count": sum(1 for row in rows if row["pair_relevant"]),
        "blind_pair_relevant_count": sum(1 for row in blind_rows if row["pair_relevant"]),
        "mixed_or_nonexclusive_count": sum(1 for row in rows if row["needs_boundary_review"]),
        "total_runtime_seconds": round(sum(row["runtime_seconds"] for row in rows), 3),
        "max_temporary_peak_bytes": max((row["temporary_peak_bytes"] for row in rows), default=0),
        "persistent_media_bytes_added": 0,
    }
    passed = blind_ready_count == len(blind_rows) and blind_useful_count >= blind_target
    return {
        "contract_version": "material_window_scout.blind_summary.v1",
        "status": "selector_gate_passed" if passed else "selector_gate_not_met",
        "benchmark_id": manifest.get("benchmark_id"),
        "manifest_path": str(manifest_path),
        "cue_set_version": CUE_SET_VERSION,
        "cue_set_sha256": cue_set_digest(),
        "policy": manifest.get("policy") or {},
        "acceptance": manifest.get("acceptance") or {},
        "metrics": metrics,
        "samples": rows,
        "manual_review_queue": [
            {
                "sample_id": row["sample_id"],
                "gold_material_type": row["gold_material_type"],
                "confusion_pair": row["confusion_pair"],
                "reason": row["review_reason"],
                "selected_windows": row["selected_windows"],
            }
            for row in rows
            if row["needs_boundary_review"] or not row["selector_useful_proxy"]
        ],
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "calls_remote_model": False,
    }


def _sample_summary(sample: dict, report: dict) -> dict:
    selected = report.get("selected_windows") or []
    fixed = report.get("fixed_window_comparison") or []
    adaptive_scores = [float(item.get("information_score") or 0.0) for item in selected]
    fixed_scores = [float(item.get("information_score") or 0.0) for item in fixed]
    adaptive_mean = _mean(adaptive_scores)
    fixed_mean = _mean(fixed_scores)
    pair_relevant = any(max(float(item.get("left_evidence_score") or 0.0), float(item.get("right_evidence_score") or 0.0)) >= 0.5 for item in selected)
    both_sides = any(float(item.get("left_evidence_score") or 0.0) >= 0.5 and float(item.get("right_evidence_score") or 0.0) >= 0.5 for item in selected)
    nonexclusive = str(sample.get("confusion_pair") or "") == "performance_program_context"
    needs_boundary_review = both_sides or nonexclusive
    useful = str(report.get("status") or "") == "ready" and adaptive_mean >= fixed_mean + 0.03 and pair_relevant
    if nonexclusive:
        review_reason = "节目语境与舞台形态可同时成立，只评估选窗信息量，不把左右侧当互斥分类。"
    elif both_sides:
        review_reason = "自适应窗口同时包含混淆对两侧强证据，需要人工复核 Gold 边界。"
    elif not useful:
        review_reason = "自适应窗口未达到预注册的信息增益或相关性门槛。"
    else:
        review_reason = ""
    return {
        "sample_id": sample.get("sample_id"),
        "evaluation_role": sample.get("evaluation_role") or "blind_eval",
        "account_id": sample.get("account_id"),
        "title": sample.get("title"),
        "gold_material_type": sample.get("gold_material_type"),
        "confusion_pair": sample.get("confusion_pair"),
        "status": report.get("status"),
        "runtime_seconds": float((report.get("timings") or {}).get("total_seconds") or 0.0),
        "asr_segment_count": int((report.get("component_summary") or {}).get("asr_segment_count") or 0),
        "ocr_nonempty_frame_count": int((report.get("component_summary") or {}).get("ocr_nonempty_frame_count") or 0),
        "candidate_count": int((report.get("component_summary") or {}).get("candidate_count") or 0),
        "temporary_peak_bytes": int((report.get("component_summary") or {}).get("temporary_peak_bytes_observed") or 0),
        "adaptive_scores": adaptive_scores,
        "fixed_scores": fixed_scores,
        "adaptive_mean_information": adaptive_mean,
        "fixed_mean_information": fixed_mean,
        "adaptive_vs_fixed_mean_uplift": round(adaptive_mean - fixed_mean, 4),
        "pair_relevant": pair_relevant,
        "both_sides_strong": both_sides,
        "needs_boundary_review": needs_boundary_review,
        "selector_useful_proxy": useful,
        "review_reason": review_reason,
        "selected_windows": [
            {
                "window": item.get("window"),
                "start_seconds": item.get("start_seconds"),
                "end_seconds": item.get("end_seconds"),
                "information_score": item.get("information_score"),
                "left_evidence_score": item.get("left_evidence_score"),
                "right_evidence_score": item.get("right_evidence_score"),
                "dominant_side": item.get("dominant_side"),
                "asr_text": item.get("asr_text"),
                "ocr_lines": item.get("ocr_lines"),
                "selection_reasons": item.get("selection_reasons"),
            }
            for item in selected
        ],
    }


def _report_path(sample: dict, output_dir: Path) -> Path:
    explicit = str(sample.get("report_path") or "").strip()
    return _project_path(explicit) if explicit else output_dir / f"{sample['sample_id']}.json"


def _project_path(value: object) -> Path:
    path = Path(str(value or "")).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


if __name__ == "__main__":
    main()
