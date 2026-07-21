from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dso.learning.interaction_heat_v3 import freeze_interaction_heat_artifact
from dso.learning.interaction_heat_holdout import (
    HoldoutReadinessThresholds,
    assess_holdout_readiness,
    assess_interaction_heat_holdout_readiness,
)


def _row(sample_id: str, account_id: str, published_at: str) -> dict:
    return {
        "id": sample_id,
        "account_id": account_id,
        "published_at": published_at,
    }


class InteractionHeatHoldoutReadinessTest(unittest.TestCase):
    def test_artifact_gate_uses_verified_labels_and_provenance_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            words = (
                "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                "golf", "hotel", "india", "juliet", "kilo", "lima",
            )
            account_tokens = ("atlas", "boreal", "cedar", "dune", "elm", "fjord")
            source_rows = [
                {
                    "id": f"account-{account}-{index}",
                    "account_id": f"account-{account}",
                    "platform_item_id": f"item-{account}-{index}",
                    "title": f"{word} unique title {account}",
                    "program_name": f"program-{account}-{word}",
                    "song_title": f"song-{word}",
                    "published_at": f"2026-05-{index + 1:02d}T00:00:00+00:00",
                    "observed_at": "2026-06-30T00:00:00+00:00",
                    "duration_seconds": 20,
                    "likes": index + 1,
                    "comments": index,
                    "favorites": index,
                    "shares": index,
                    "metric_sources": {
                        "likes": "fixture",
                        "comments": "fixture",
                        "favorites": "fixture",
                        "shares": "fixture",
                    },
                }
                for account in account_tokens
                for index, word in enumerate(words)
            ]
            frozen = freeze_interaction_heat_artifact(
                artifact_id="fixture-holdout-labels-r1",
                rows=source_rows,
                output_root=root / "labels",
                min_group_samples=3,
            )
            database = root / "fixture.sqlite3"
            columns = (
                "id, account_id, dataset_id, program_key, program_name, song_title, "
                "platform, platform_item_id, sample_key, title, published_at, "
                "collected_at, duration_seconds, likes, comments, favorites, shares, "
                "reward_proxy, raw_json"
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE historical_capture_samples (
                        id TEXT PRIMARY KEY, account_id TEXT, dataset_id TEXT,
                        program_key TEXT, program_name TEXT, song_title TEXT,
                        platform TEXT, platform_item_id TEXT, sample_key TEXT,
                        title TEXT, published_at TEXT, collected_at TEXT,
                        duration_seconds REAL, likes INTEGER, comments INTEGER,
                        favorites INTEGER, shares INTEGER, reward_proxy REAL,
                        raw_json TEXT
                    )
                    """
                )
                database_rows = []
                for row in source_rows + [
                    {
                        **source_rows[0],
                        "id": "new-account-z-1",
                        "account_id": "account-z",
                        "platform_item_id": "new-item-z-1",
                        "title": "zulu unique title newaccount",
                        "program_name": "program-newaccount-zulu",
                        "song_title": "song-zulu",
                        "published_at": "2026-07-10T00:00:00+00:00",
                    }
                ]:
                    raw_json = json.dumps(
                        {
                            "clean": {
                                "observed_at": "2026-07-15T00:00:00+00:00",
                                "metric_sources": row["metric_sources"],
                            }
                        }
                    )
                    database_rows.append(
                        (
                            row["id"], row["account_id"], "fixture", "",
                            row["program_name"], row["song_title"], "douyin",
                            row["platform_item_id"], row["id"], row["title"],
                            row["published_at"], "2026-07-15T00:00:00+00:00",
                            row["duration_seconds"], row["likes"], row["comments"],
                            row["favorites"], row["shares"], 0.0, raw_json,
                        )
                    )
                connection.executemany(
                    f"INSERT INTO historical_capture_samples ({columns}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    database_rows,
                )

            report = assess_interaction_heat_holdout_readiness(
                label_artifact_dir=Path(frozen["artifact_dir"]),
                expected_label_manifest_sha256=frozen["manifest_sha256"],
                db_path=database,
                thresholds=HoldoutReadinessThresholds(
                    min_forward_samples=1,
                    min_forward_accounts=1,
                    min_forward_span_days=0,
                    min_new_accounts=1,
                    min_samples_per_new_account=1,
                ),
            )

            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["forward_time"]["candidate_count"], 1)
            self.assertEqual(report["account_holdout"]["eligible_accounts"], ["account-z"])
            self.assertEqual(
                report["label_manifest_sha256"],
                frozen["manifest_sha256"],
            )
            self.assertEqual(report["network_request_count"], 0)

    def test_gate_is_ready_with_forward_window_and_new_accounts(self) -> None:
        current_rows = [
            _row("frozen", "account-a", "2026-07-15T00:00:00+00:00"),
            _row("old-new-id", "account-a", "2026-06-20T00:00:00+00:00"),
            _row("historic-new-account", "account-d", "2026-06-15T00:00:00+00:00"),
            _row("forward-a", "account-a", "2026-07-01T00:00:00+00:00"),
            _row("forward-b1", "account-b", "2026-07-03T00:00:00+00:00"),
            _row("forward-b2", "account-b", "2026-07-10T00:00:00+00:00"),
            _row("forward-c", "account-c", "2026-07-08T00:00:00+00:00"),
        ]

        report = assess_holdout_readiness(
            frozen_sample_ids={"frozen"},
            frozen_account_ids={"account-a"},
            frozen_cutoff="2026-06-30T00:00:00+00:00",
            current_rows=current_rows,
            thresholds=HoldoutReadinessThresholds(
                min_forward_samples=4,
                min_forward_accounts=3,
                min_forward_span_days=7,
                min_new_accounts=2,
                min_samples_per_new_account=1,
            ),
        )

        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["forward_time"]["ready"])
        self.assertEqual(report["forward_time"]["candidate_count"], 4)
        self.assertTrue(report["account_holdout"]["ready"])
        self.assertEqual(
            report["account_holdout"]["eligible_accounts"],
            ["account-b", "account-c", "account-d"],
        )
        self.assertEqual(report["account_holdout"]["candidate_count"], 4)
        self.assertEqual(report["excluded"]["already_frozen"], 1)
        self.assertEqual(report["forward_time"]["excluded_not_after_cutoff"], 2)

    def test_gate_is_not_ready_without_new_eligible_rows(self) -> None:
        report = assess_holdout_readiness(
            frozen_sample_ids={"frozen"},
            frozen_account_ids={"account-a"},
            frozen_cutoff="2026-06-30T00:00:00+00:00",
            current_rows=[
                _row("frozen", "account-a", "2026-06-30T00:00:00+00:00"),
                _row("missing-date", "account-b", ""),
            ],
            thresholds=HoldoutReadinessThresholds(
                min_forward_samples=1,
                min_forward_accounts=1,
                min_forward_span_days=0,
                min_new_accounts=1,
                min_samples_per_new_account=1,
            ),
        )

        self.assertEqual(report["status"], "not_ready")
        self.assertFalse(report["forward_time"]["ready"])
        self.assertFalse(report["account_holdout"]["ready"])
        self.assertIn("forward_sample_count", report["forward_time"]["unmet"])
        self.assertIn("new_account_count", report["account_holdout"]["unmet"])
        self.assertEqual(report["excluded"]["missing_published_at"], 1)


if __name__ == "__main__":
    unittest.main()
