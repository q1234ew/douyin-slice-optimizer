#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all
from dso.learning.backtest import backtest_rule_ranker
from dso.learning.multimodal_validation import _build_asset_index, _prepare_row
from dso.learning.qwen_omni import qwen_omni_shadow_cache_index, qwen_omni_status, run_qwen_omni_media_batch


DEFAULT_ACCOUNTS = [
    "duanduanzhengzheng",
    "geshou2026",
    "taotao_daxiaojie",
    "yuhuan",
    "haiye_yelaoshi",
    "raoxianyin",
    "weibabibibi",
    "tianci",
    "singer_yuhang",
    "sixuweilive",
    "jason_teacher",
    "kuku_oscar",
    "hukan_music",
    "dk_voice_teacher",
    "xingxing_live",
    "yule_xiaoe_yu",
    "manfen_kexuejia",
    "wccyu",
    "xindong_yure_live",
    "adai_valerio",
    "rimu_live",
    "zaijian_jianghuchuan",
    "beidou_live",
    "kim0330music",
]


def _print(payload: object) -> None:
    if isinstance(payload, str):
        print(payload, flush=True)
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _storage_bytes() -> int:
    dirs = [
        Path("data/douyin_media_assets"),
        Path("data/cache/qwen_omni_clips"),
        Path("data/cache/qwen_omni_results"),
    ]
    total = 0
    for root in dirs:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    return total


def _has_video_stream(path: Path) -> bool:
    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return bool(json.loads(output.decode("utf-8") or "{}").get("streams"))
    except Exception:
        return False


def _coverage_report() -> dict:
    cache = qwen_omni_shadow_cache_index()
    asset_index = _build_asset_index()
    with connect() as conn:
        rows = fetch_all(conn, "SELECT * FROM historical_capture_samples ORDER BY account_id, id")
    by_account: dict[str, dict[str, int]] = defaultdict(lambda: {"real": 0, "cached": 0})
    real = 0
    cached = 0
    excluded = 0
    for row in (_prepare_row(item, asset_index=asset_index) for item in rows):
        assets = row.get("assets") or {}
        paths = assets.get("paths") or {}
        videos = paths.get("video") or []
        if not videos or not assets.get("ready_for_multimodal"):
            continue
        video_path = Path(str(videos[0]))
        if not _has_video_stream(video_path):
            excluded += 1
            continue
        account_id = str(row.get("account_id") or "unknown")
        real += 1
        by_account[account_id]["real"] += 1
        if str(row.get("id") or "") in cache:
            cached += 1
            by_account[account_id]["cached"] += 1
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "real_video_count": real,
        "omni_cached_count": cached,
        "coverage_rate": round(cached / real, 4) if real else 0.0,
        "excluded_audio_only_or_bad": excluded,
        "by_account": dict(sorted(by_account.items())),
        "storage_bytes": _storage_bytes(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the remaining local Qwen Omni media queue with storage guard.")
    parser.add_argument("--accounts", nargs="*", default=DEFAULT_ACCOUNTS)
    parser.add_argument("--limit-per-account", type=int, default=120)
    parser.add_argument("--max-clip-seconds", type=float, default=8.0)
    parser.add_argument("--storage-limit-gb", type=float, default=10.0)
    parser.add_argument("--stamp", default=time.strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--skip-backtest", action="store_true")
    parser.add_argument("--skip-status", action="store_true")
    parser.add_argument("--skip-initial-coverage", action="store_true")
    parser.add_argument("--skip-final-coverage", action="store_true")
    args = parser.parse_args()

    data_dirs = ensure_data_dirs()
    output_dir = data_dirs.root / "outputs" / "qwen_omni_shadow"
    output_dir.mkdir(parents=True, exist_ok=True)
    storage_limit_bytes = int(args.storage_limit_gb * 1024 * 1024 * 1024)

    _print({"event": "start", "stamp": args.stamp, "accounts": args.accounts})
    if not args.skip_status:
        _print({"event": "service_status", "payload": qwen_omni_status()})
    if not args.skip_initial_coverage:
        _print({"event": "coverage_before", "payload": _coverage_report()})

    for account in args.accounts:
        storage = _storage_bytes()
        _print({"event": "account_start", "account": account, "storage_bytes": storage})
        if storage >= storage_limit_bytes:
            _print({"event": "storage_limit_reached", "storage_bytes": storage, "limit_bytes": storage_limit_bytes})
            break
        report_path = output_dir / f"batch_{account}_{args.stamp}.json"
        try:
            report = run_qwen_omni_media_batch(
                account_id=account,
                limit=args.limit_per_account,
                max_clip_seconds=args.max_clip_seconds,
                output_path=report_path,
            )
            _print(
                {
                    "event": "account_done",
                    "account": account,
                    "status": report.get("status"),
                    "created": report.get("created"),
                    "reused": report.get("reused"),
                    "failed": report.get("failed"),
                    "report_path": str(report_path),
                }
            )
        except Exception as exc:
            _print({"event": "account_failed", "account": account, "error": str(exc)})

    if not args.skip_final_coverage:
        coverage = _coverage_report()
        coverage_path = output_dir / f"coverage_after_remaining_{args.stamp}.json"
        coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
        _print({"event": "coverage_after", "path": str(coverage_path), "payload": coverage})

    if not args.skip_backtest:
        try:
            backtest = backtest_rule_ranker(strategy="research_ranker_v2_5_shadow", holdout_policy="time", k=10)
            backtest_path = output_dir / f"backtest_v25_after_remaining_{args.stamp}.json"
            backtest_path.write_text(json.dumps(backtest, ensure_ascii=False, indent=2), encoding="utf-8")
            _print({"event": "backtest_done", "path": str(backtest_path), "status": backtest.get("status")})
        except Exception as exc:
            _print({"event": "backtest_failed", "error": str(exc)})

    _print({"event": "finish", "stamp": args.stamp})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
