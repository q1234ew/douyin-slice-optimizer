from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dso.db.session import init_db
from dso.learning.propagation_feature_validation import (
    evaluate_propagation_account_holdout,
    merge_propagation_feature_reports,
    select_matched_propagation_pairs,
)
from dso.utils import write_json


def _feature_payload(*, high: bool) -> dict:
    return {
        "content_form": "performance" if high else "commentary",
        "hook": {
            "modality": "audio_visual" if high else "speech",
            "strength": "high" if high else "low",
        },
        "audio": {
            "energy": "high" if high else "low",
            "energy_change": "rising" if high else "flat",
            "audience_reaction": high,
        },
        "visual": {
            "primary_scene": "stage" if high else "studio",
            "cut_density": "high" if high else "low",
            "text_density": "low" if high else "high",
        },
        "narrative": {
            "arc": "build" if high else "flat",
            "context_dependency": "low" if high else "high",
            "novelty": "high" if high else "low",
            "emotional_intensity": "high" if high else "low",
            "payoff_present": high,
        },
    }


def _row(account: str, label: str, index: int) -> dict:
    high = label == "high"
    return {
        "id": f"{account}-{label}-{index}",
        "account_id": account,
        "performance_label": label,
        "publication_age_bucket": "age_30_90d",
        "duration_bucket": "duration_15_30s",
        "content_category": "music_variety",
        "actual_duration_seconds": 20.0 + (0.5 if high else 0.0),
        "normalized_reward": 95.0 if high else 10.0,
        "reward_proxy": 90.0 if high else 10.0,
        "title": f"{account}-{label}-{index}-unique",
    }


class PropagationFeatureValidationTest(unittest.TestCase):
    def test_feature_report_merge_prefers_bounded_recovery_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "manifest.json"
            write_json(
                manifest_path,
                {
                    "manifest_id": "merge-test",
                    "clips": [{"sample_id": "a"}, {"sample_id": "b"}],
                },
            )
            provider = {
                "provider_id": "aliyun_bailian",
                "model_id": "qwen3.5-omni-plus-2026-03-15",
                "prompt_version": "prompt-v2",
            }
            primary_path = root / "primary.json"
            write_json(
                primary_path,
                {
                    "experiment_id": "primary",
                    "provider": provider,
                    "feature_policy": {"output_token_limit": 1200},
                    "network_request_count": 2,
                    "usage_estimated_cost_cny": "0.2",
                    "clips": [
                        {
                            "sample_id": "a",
                            "provider_status": "shadow_succeeded",
                            "provider_output": _feature_payload(high=True),
                        },
                        {
                            "sample_id": "b",
                            "provider_status": "fallback_local",
                            "provider_output": {},
                        },
                    ],
                },
            )
            recovery_path = root / "recovery.json"
            write_json(
                recovery_path,
                {
                    "experiment_id": "recovery",
                    "provider": provider,
                    "feature_policy": {"output_token_limit": 1800},
                    "network_request_count": 1,
                    "usage_estimated_cost_cny": "0.15",
                    "clips": [
                        {
                            "sample_id": "b",
                            "provider_status": "shadow_succeeded",
                            "provider_output": _feature_payload(high=False),
                        }
                    ],
                },
            )

            report = merge_propagation_feature_reports(
                manifest_path,
                [primary_path, recovery_path],
            )

            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["evaluation"]["successful_count"], 2)
            self.assertEqual(report["network_request_count"], 3)
            self.assertEqual(report["usage_estimated_cost_cny"], "0.350000")
            self.assertEqual(report["merge_policy"]["output_token_limits"], [1200, 1800])

    def test_pair_selection_covers_requested_accounts_and_constraints(self) -> None:
        rows = [
            _row(f"account-{letter}", label, index)
            for index, letter in enumerate("abcdefgh")
            for label in ("high", "low")
        ]

        pairs, capacities = select_matched_propagation_pairs(
            rows,
            pair_count=8,
            min_accounts=8,
            max_duration_delta_seconds=4.0,
        )

        self.assertEqual(len(pairs), 8)
        self.assertEqual(len({pair["account_id"] for pair in pairs}), 8)
        self.assertEqual(set(capacities.values()), {1})
        for pair in pairs:
            self.assertEqual(pair["high"]["account_id"], pair["low"]["account_id"])
            self.assertGreaterEqual(pair["normalized_reward_gap"], 55.0)
            self.assertGreaterEqual(pair["reward_proxy_ratio"], 1.8)

    def test_account_holdout_uses_only_successful_complete_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "data" / "db" / "dso.sqlite3"
            db_path.parent.mkdir(parents=True)
            init_db(db_path)
            clips = []
            pairs = []
            for index in range(8):
                account = f"account-{index}"
                left = f"{account}-high"
                right = f"{account}-low"
                pairs.append(
                    {
                        "pair_id": f"pair-{index}",
                        "account_id": account,
                        "left_sample_id": left,
                        "right_sample_id": right,
                    }
                )
                for sample_id, label in ((left, "high"), (right, "low")):
                    clips.append(
                        {
                            "sample_id": sample_id,
                            "account_id": account,
                            "platform_item_id": sample_id,
                            "performance_label": label,
                        }
                    )
                    self._insert_sample(db_path, sample_id, account, label)
            manifest = {
                "manifest_id": "synthetic-account-holdout",
                "pairs": pairs,
                "clips": clips,
                "outcome_availability": {"visible_engagement_heat_proxy": 1.0},
            }
            manifest_path = root / "manifest.json"
            write_json(manifest_path, manifest)
            feature_report = {
                "source_manifest_id": manifest["manifest_id"],
                "source_manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "network_request_count": 16,
                "usage_estimated_cost_cny": "1.6",
                "clips": [
                    {
                        "sample_id": clip["sample_id"],
                        "provider_status": "shadow_succeeded",
                        "provider_output": _feature_payload(
                            high=clip["performance_label"] == "high"
                        ),
                    }
                    for clip in clips
                ],
            }
            feature_path = root / "features.json"
            write_json(feature_path, feature_report)

            report = evaluate_propagation_account_holdout(
                manifest_path,
                feature_path,
                db_path=db_path,
                baseline_score_builder=lambda rows: {str(row["id"]): 50.0 for row in rows},
            )

            self.assertEqual(report["feature_coverage"]["coverage"], 1.0)
            self.assertEqual(report["split_policy"]["account_count"], 8)
            self.assertEqual(
                report["pair_metrics"]["omni_factual_features_account_isolated"][
                    "pair_accuracy"
                ],
                1.0,
            )
            self.assertEqual(
                report["pair_metrics"]["v2_4_plus_omni_factual_fixed_15"][
                    "pair_accuracy"
                ],
                1.0,
            )
            self.assertFalse(report["promotion_gate"]["production_promotion_allowed"])
            self.assertEqual(
                report["comparison_diagnostics"]["corrected_pair_count"], 8
            )
            self.assertEqual(report["comparison_diagnostics"]["harmed_pair_count"], 0)

    def test_account_holdout_rejects_incomplete_feature_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "data" / "db" / "dso.sqlite3"
            db_path.parent.mkdir(parents=True)
            init_db(db_path)
            self._insert_sample(db_path, "high", "account-a", "high")
            self._insert_sample(db_path, "low", "account-a", "low")
            manifest = {
                "manifest_id": "incomplete",
                "pairs": [
                    {
                        "pair_id": "pair-1",
                        "account_id": "account-a",
                        "left_sample_id": "high",
                        "right_sample_id": "low",
                    }
                ],
                "clips": [
                    {
                        "sample_id": sample_id,
                        "account_id": "account-a",
                        "performance_label": label,
                    }
                    for sample_id, label in (("high", "high"), ("low", "low"))
                ],
            }
            manifest_path = root / "manifest.json"
            write_json(manifest_path, manifest)
            feature_report = {
                "source_manifest_id": "incomplete",
                "source_manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "clips": [
                    {
                        "sample_id": "high",
                        "provider_status": "shadow_succeeded",
                        "provider_output": _feature_payload(high=True),
                    }
                ],
            }
            feature_path = root / "features.json"
            write_json(feature_path, feature_report)

            with self.assertRaisesRegex(ValueError, "coverage is incomplete"):
                evaluate_propagation_account_holdout(
                    manifest_path,
                    feature_path,
                    db_path=db_path,
                    baseline_score_builder=lambda rows: {
                        str(row["id"]): 50.0 for row in rows
                    },
                )

    @staticmethod
    def _insert_sample(db_path: Path, sample_id: str, account: str, label: str) -> None:
        now = "2026-07-20T00:00:00+00:00"
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                INSERT INTO historical_capture_samples (
                    id, account_id, dataset_id, sample_key, platform_item_id,
                    performance_label, reward_proxy, normalized_reward,
                    research_label_version, created_at, updated_at
                ) VALUES (?, ?, 'synthetic', ?, ?, ?, 1, 1,
                          'research_labels.visible_engagement_v2', ?, ?)
                """,
                (sample_id, account, sample_id, sample_id, label, now, now),
            )


if __name__ == "__main__":
    unittest.main()
