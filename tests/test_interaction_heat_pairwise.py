from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dso.cli import cmd_interaction_heat_pairwise
from dso.learning.interaction_heat_v3 import freeze_interaction_heat_artifact
from dso.learning.interaction_heat_pairwise import (
    PairwiseTrainingConfig,
    fit_pairwise_logistic,
    hash_candidate_features,
    score_sparse_features,
)


TITLE_WORDS = (
    "amber", "birch", "coral", "dawn", "ember", "frost",
    "grove", "harbor", "iris", "jade", "kestrel", "lilac",
)


def _source_rows() -> list[dict]:
    return [
        {
            "id": f"account-{account}-{index:02d}",
            "account_id": f"account-{account}",
            "dataset_id": f"dataset-{account}",
            "program_key": f"program-{account}-{index}",
            "program_name": "music-show",
            "platform": "douyin",
            "platform_item_id": f"item-{account}-{index:02d}",
            "sample_key": f"sample-{account}-{index:02d}",
            "title": (
                f"{'bright' if index >= 6 else 'quiet'} performance "
                f"{account} {TITLE_WORDS[index]}"
            ),
            "published_at": f"2026-05-{index + 1:02d}T08:00:00+00:00",
            "observed_at": "2026-06-30T00:00:00+00:00",
            "duration_seconds": 12.0 + index,
            "likes": 10 + index * 10,
            "comments": index,
            "favorites": index * 2,
            "shares": index,
            "metric_sources": {
                "likes": "fixture",
                "comments": "fixture",
                "favorites": "fixture",
                "shares": "fixture",
            },
        }
        for account in "abcdef"
        for index in range(12)
    ]


class InteractionHeatPairwiseTest(unittest.TestCase):
    def test_core_feature_profile_ignores_high_cardinality_identity_fields(self) -> None:
        label = {
            "account_id": "account-a",
            "duration_bucket": "duration_16_30s",
            "publication_age_bucket": "age_31_90d",
            "confidence": {"grade": "high"},
        }
        split = {"published_at": "2026-05-20T08:00:00+00:00"}
        first = {
            "title": "节目甲独家舞台",
            "content_category": "performance",
            "program_name": "program-a",
            "song_title": "song-a",
            "tags": "tag-a",
        }
        second = dict(
            first,
            title="节目乙独家舞台",
            program_name="program-b",
            song_title="song-b",
            tags="tag-b",
        )

        core_first = hash_candidate_features(
            label,
            split,
            first,
            dimensions=128,
            include_account_id=False,
            feature_profile="core",
        )
        core_second = hash_candidate_features(
            label,
            split,
            second,
            dimensions=128,
            include_account_id=False,
            feature_profile="core",
        )
        full_first = hash_candidate_features(
            label,
            split,
            first,
            dimensions=128,
            include_account_id=False,
            feature_profile="full",
        )
        full_second = hash_candidate_features(
            label,
            split,
            second,
            dimensions=128,
            include_account_id=False,
            feature_profile="full",
        )

        self.assertEqual(core_first, core_second)
        self.assertNotEqual(full_first, full_second)

    def test_local_experiment_writes_models_predictions_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = freeze_interaction_heat_artifact(
                artifact_id="fixture-interaction-heat-r1",
                rows=_source_rows(),
                output_root=root / "source",
                created_at="2026-07-20T00:00:00+00:00",
                min_group_samples=3,
            )
            database = root / "fixture.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE historical_capture_samples (
                      id TEXT PRIMARY KEY,
                      title TEXT,
                      content_category TEXT,
                      hook_type TEXT,
                      slice_structure TEXT,
                      program_name TEXT,
                      artist_names TEXT,
                      song_title TEXT,
                      tags TEXT,
                      media_type TEXT,
                      commercial_intent TEXT,
                      rights_risk TEXT,
                      classification_confidence TEXT,
                      structure_confidence TEXT,
                      entity_signal TEXT,
                      is_original_sound INTEGER
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO historical_capture_samples (
                      id, title, content_category, hook_type, slice_structure,
                      program_name, artist_names, song_title, tags, media_type,
                      commercial_intent, rights_risk, classification_confidence,
                      structure_confidence, entity_signal, is_original_sound
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["id"], row["title"],
                            "strong" if row["likes"] >= 70 else "quiet",
                            "vocal", "payoff", row["program_name"], "artist",
                            "song", "music", "video", "none", "low", "high",
                            "high", "known", 1,
                        )
                        for row in _source_rows()
                    ],
                )

            result = cmd_interaction_heat_pairwise(
                "fixture-pairwise-r1",
                source["artifact_dir"],
                source["manifest_sha256"],
                str(database),
                str(root / "experiments"),
            )

            artifact = Path(result["artifact_dir"])
            report = json.loads((artifact / "report.json").read_text())
            model = json.loads((artifact / "model.json").read_text())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(set(report["protocols"]), {"account_time", "account_holdout"})
            self.assertGreater(model["models"]["account_time"]["pair_count"], 0)
            self.assertGreater(model["models"]["account_holdout"]["pair_count"], 0)
            self.assertEqual(result["network_request_count"], 0)
            self.assertEqual(result["production_weight_changed"], False)

    def test_feature_hash_ignores_outcome_fields_and_scopes_account(self) -> None:
        label = {
            "account_id": "account-a",
            "duration_bucket": "duration_16_30s",
            "publication_age_bucket": "age_31_90d",
            "confidence": {"grade": "high"},
        }
        split = {"published_at": "2026-05-20T08:00:00+00:00"}
        metadata = {
            "title": "清唱副歌现场",
            "content_category": "performance",
            "hook_type": "vocal_hook",
            "likes": 10,
            "shares": 2,
        }

        first = hash_candidate_features(
            label,
            split,
            metadata,
            dimensions=128,
            include_account_id=False,
        )
        changed_outcomes = dict(metadata, likes=999999, shares=9999)
        second = hash_candidate_features(
            label,
            split,
            changed_outcomes,
            dimensions=128,
            include_account_id=False,
        )
        account_scoped = hash_candidate_features(
            label,
            split,
            metadata,
            dimensions=128,
            include_account_id=True,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, account_scoped)

    def test_pairwise_logistic_learns_higher_score_for_better_feature(self) -> None:
        rows = [
            {
                "sample_id": f"account-a-low-{index}",
                "account_id": "account-a",
                "target": 0.1,
                "features": {0: 1.0},
            }
            for index in range(8)
        ] + [
            {
                "sample_id": f"account-a-high-{index}",
                "account_id": "account-a",
                "target": 0.9,
                "features": {1: 1.0},
            }
            for index in range(8)
        ]

        model = fit_pairwise_logistic(
            rows,
            config=PairwiseTrainingConfig(
                dimensions=8,
                epochs=4,
                learning_rate=0.1,
                l2=0.0001,
                min_target_gap=0.2,
                max_pairs_per_sample=4,
                seed=20260720,
            ),
        )

        self.assertGreater(model.pair_count, 0)
        self.assertGreater(
            score_sparse_features(model.weights, {1: 1.0}),
            score_sparse_features(model.weights, {0: 1.0}),
        )


if __name__ == "__main__":
    unittest.main()
