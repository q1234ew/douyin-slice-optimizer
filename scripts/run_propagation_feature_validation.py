#!/usr/bin/env python3
"""Freeze or evaluate the account-isolated Omni propagation feature holdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dso.learning.propagation_feature_validation import (
    DEFAULT_MAX_DURATION_DELTA_SECONDS,
    DEFAULT_MIN_ACCOUNTS,
    DEFAULT_OMNI_WEIGHT,
    DEFAULT_PAIR_COUNT,
    DEFAULT_TOP_K,
    build_propagation_validation_manifest,
    evaluate_propagation_account_holdout,
    merge_propagation_feature_reports,
)
from dso.utils import write_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build or evaluate the frozen complete-video propagation feature holdout."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Freeze matched high/low media pairs.")
    build.add_argument("--db-path", type=Path, required=True)
    build.add_argument("--media-root", type=Path, required=True)
    build.add_argument("--repo-root", type=Path, default=Path.cwd())
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--manifest-id")
    build.add_argument("--pair-count", type=int, default=DEFAULT_PAIR_COUNT)
    build.add_argument("--min-accounts", type=int, default=DEFAULT_MIN_ACCOUNTS)
    build.add_argument(
        "--max-duration-delta",
        type=float,
        default=DEFAULT_MAX_DURATION_DELTA_SECONDS,
    )
    build.add_argument("--ffprobe")
    build.add_argument("--benchmark-dir", type=Path)
    build.add_argument(
        "--exclude-manifest",
        action="append",
        type=Path,
        default=[],
        help="Complete-video manifest whose platform items and stable titles must be excluded.",
    )

    evaluate = subparsers.add_parser(
        "evaluate", help="Run leave-one-account-out feature and v2.4 comparisons."
    )
    evaluate.add_argument("--manifest", type=Path, required=True)
    evaluate.add_argument("--feature-report", type=Path, required=True)
    evaluate.add_argument("--db-path", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--omni-weight", type=float, default=DEFAULT_OMNI_WEIGHT)
    evaluate.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)

    merge = subparsers.add_parser(
        "merge", help="Merge a primary feature report with bounded recovery reports."
    )
    merge.add_argument("--manifest", type=Path, required=True)
    merge.add_argument(
        "--feature-report", action="append", type=Path, required=True
    )
    merge.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build":
        report = build_propagation_validation_manifest(
            db_path=args.db_path,
            media_root=args.media_root,
            repo_root=args.repo_root,
            excluded_manifest_paths=args.exclude_manifest,
            benchmark_dir=args.benchmark_dir,
            manifest_id=args.manifest_id,
            pair_count=args.pair_count,
            min_accounts=args.min_accounts,
            max_duration_delta_seconds=args.max_duration_delta,
            ffprobe_path=args.ffprobe,
        )
    elif args.command == "evaluate":
        report = evaluate_propagation_account_holdout(
            args.manifest,
            args.feature_report,
            db_path=args.db_path,
            omni_weight=args.omni_weight,
            top_k=args.top_k,
        )
    else:
        report = merge_propagation_feature_reports(
            args.manifest,
            args.feature_report,
        )
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
