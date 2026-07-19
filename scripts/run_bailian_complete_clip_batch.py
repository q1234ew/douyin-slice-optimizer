#!/usr/bin/env python3
"""Preflight or execute a bounded complete-short-clip Omni research batch."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from dso.learning.bailian_complete_clip_batch import (
    DEFAULT_BATCH_LIMIT,
    DEFAULT_HARD_BUDGET_CNY,
    run_bailian_complete_clip_batch,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze complete H.264/AAC short clips with Qwen3.5-Omni. "
            "No representative frames are extracted or sent."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--media-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_LIMIT)
    parser.add_argument("--hard-budget-cny", type=Decimal, default=DEFAULT_HARD_BUDGET_CNY)
    parser.add_argument("--batch-id")
    parser.add_argument("--force-proxies", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    report = run_bailian_complete_clip_batch(
        args.manifest,
        media_root=args.media_root,
        output_path=args.output,
        execute=args.execute,
        limit=args.limit,
        force_proxies=args.force_proxies,
        batch_id=args.batch_id,
        hard_budget_cny=args.hard_budget_cny,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
