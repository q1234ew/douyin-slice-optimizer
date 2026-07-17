#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dso.db.session import connect, fetch_all
from dso.learning.multimodal_validation import _build_asset_index
from dso.media.ffmpeg import probe_video
from dso.utils import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESOLVER_REPORT = PROJECT_ROOT / "outputs" / "material_evidence" / "resolver_shadow_metric_fix.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit confirmed Material Gold media before selector evaluation.")
    parser.add_argument("--resolver-report", type=Path, default=DEFAULT_RESOLVER_REPORT)
    parser.add_argument("--duration-tolerance", type=float, default=0.15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pair_index = _confusion_pair_index(args.resolver_report.expanduser().resolve())
    assets = _build_asset_index()
    with connect() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT h.id AS sample_id, h.account_id, h.dataset_id, h.platform_item_id,
                   h.title, h.duration_seconds AS expected_duration_seconds,
                   g.material_type AS gold_material_type, g.program_context,
                   g.presentation_style
            FROM historical_capture_samples h
            JOIN material_gold_annotations g ON g.sample_id = h.id
            WHERE g.review_status = 'confirmed'
            ORDER BY h.id
            """,
        )

    samples = [
        _audit_row(
            row,
            assets.get(str(row.get("platform_item_id") or "")) or {},
            pair_index.get(str(row.get("sample_id") or "")) or "unknown",
            duration_tolerance=max(0.0, float(args.duration_tolerance)),
        )
        for row in rows
    ]
    ready = [item for item in samples if item["media_readiness"] == "ready_for_selector"]
    payload = {
        "contract_version": "material_gold_media_audit.v1",
        "status": "ready" if samples else "empty",
        "query": {
            "review_status": "confirmed",
            "duration_tolerance": max(0.0, float(args.duration_tolerance)),
            "requires_audio": True,
            "requires_manual_identity_review": True,
        },
        "summary": {
            "confirmed_gold_count": len(samples),
            "ready_for_selector_count": len(ready),
            "ready_for_selector_rate": round(len(ready) / max(1, len(samples)), 4),
            "missing_video_count": sum(1 for item in samples if "video_missing" in item["exclusion_reasons"]),
            "missing_audio_count": sum(1 for item in samples if "audio_missing" in item["exclusion_reasons"]),
            "duration_mismatch_count": sum(1 for item in samples if "duration_mismatch" in item["exclusion_reasons"]),
            "probe_error_count": sum(1 for item in samples if "probe_error" in item["exclusion_reasons"]),
            "ready_pair_counts": _count_values(ready, "confusion_pair"),
        },
        "samples": samples,
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "generated_at": utc_now(),
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **payload["summary"]}, ensure_ascii=False, indent=2))


def _audit_row(row: dict, assets: dict, confusion_pair: str, *, duration_tolerance: float) -> dict:
    video_path = _first_existing(assets.get("video") or [])
    external_audio_path = _first_existing(assets.get("audio") or [])
    expected_duration = float(row.get("expected_duration_seconds") or 0.0)
    video_probe: dict = {}
    audio_probe: dict = {}
    probe_failed = False
    if video_path:
        try:
            video_probe = probe_video(video_path)
        except Exception as exc:
            video_probe = {"error": str(exc)}
            probe_failed = True
    if external_audio_path:
        try:
            audio_probe = probe_video(external_audio_path)
        except Exception as exc:
            audio_probe = {"error": str(exc)}
            probe_failed = True

    actual_duration = float(video_probe.get("duration_seconds") or 0.0)
    embedded_audio = int(video_probe.get("audio_streams") or 0) > 0
    external_audio = bool(external_audio_path and int(audio_probe.get("audio_streams") or 0) > 0)
    audio_duration = actual_duration if embedded_audio else float(audio_probe.get("duration_seconds") or 0.0)
    duration_ratio = actual_duration / expected_duration if actual_duration > 0 and expected_duration > 0 else 0.0
    audio_duration_ratio = audio_duration / actual_duration if audio_duration > 0 and actual_duration > 0 else 0.0
    reasons: list[str] = []
    if not video_path:
        reasons.append("video_missing")
    if probe_failed:
        reasons.append("probe_error")
    if video_path and expected_duration > 0 and abs(duration_ratio - 1.0) > duration_tolerance:
        reasons.append("duration_mismatch")
    if not embedded_audio and not external_audio:
        reasons.append("audio_missing")
    elif actual_duration > 0 and abs(audio_duration_ratio - 1.0) > duration_tolerance:
        reasons.append("audio_duration_mismatch")
    return {
        **row,
        "confusion_pair": confusion_pair,
        "video_path": str(video_path) if video_path else "",
        "external_audio_path": str(external_audio_path) if external_audio_path else "",
        "actual_duration_seconds": round(actual_duration, 3),
        "duration_ratio": round(duration_ratio, 4),
        "audio_source": "embedded_audio" if embedded_audio else ("external_audio" if external_audio else "missing_audio"),
        "audio_duration_seconds": round(audio_duration, 3),
        "audio_duration_ratio": round(audio_duration_ratio, 4),
        "media_readiness": "ready_for_selector" if not reasons else "not_evaluable",
        "exclusion_reasons": reasons,
        "identity_review_status": "pending" if not reasons else "not_queued",
    }


def _confusion_pair_index(report_path: Path) -> dict[str, str]:
    if not report_path.is_file():
        return {}
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        str(item.get("sample_id") or ""): str(item.get("confusion_pair") or "unknown")
        for item in payload.get("samples") or []
        if item.get("sample_id")
    }


def _first_existing(values: list[object]) -> Path | None:
    for value in values:
        path = Path(str(value or "")).expanduser()
        if path.is_file():
            return path.resolve()
    return None


def _count_values(rows: list[dict], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field) or "unknown")
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


if __name__ == "__main__":
    main()
