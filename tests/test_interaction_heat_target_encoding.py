from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dso.learning.interaction_heat_v3 import freeze_interaction_heat_artifact
from dso.learning.interaction_heat_target_encoding import (
    TargetEncodingConfig,
    _protocol_rows,
    _read_jsonl_index,
    cross_fit_target_encoder,
    extract_target_encoding_fields,
    fit_target_encoder,
    predict_target_encoding,
    run_local_target_encoding_experiment,
)


def _row(
    sample_id: str,
    account_id: str,
    target: float,
    *,
    category: str = "performance",
    hook: str = "highlight",
) -> dict:
    return {
        "account_id": account_id,
        "duration_bucket": "duration_16_30s",
        "metadata": {
            "content_category": category,
            "hook_type": hook,
            "slice_structure": "hook-payoff",
            "media_type": "video",
        },
        "publication_age_bucket": "age_31_90d",
        "published_at": "2026-05-20T08:00:00+00:00",
        "sample_id": sample_id,
        "source_group_id": f"source-{sample_id}",
        "target": target,
    }


class InteractionHeatTargetEncodingTest(unittest.TestCase):
    def test_filtered_jsonl_reader_does_not_decode_excluded_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "labels.jsonl"
            path.write_text(
                '{"sample_id":"train","value":1}\n'
                '{"sample_id":"test","broken":not-json}\n',
                encoding="utf-8",
            )

            rows = _read_jsonl_index(
                path,
                "sample_id",
                allowed_values={"train"},
            )

            self.assertEqual(rows, {"train": {"sample_id": "train", "value": 1}})

    def test_validation_row_loading_does_not_parse_test_target(self) -> None:
        labels = {
            sample_id: {
                "account_id": "account-a",
                "duration_bucket": "duration_16_30s",
                "publication_age_bucket": "age_31_90d",
                "protocol_targets": {
                    "account_time": {
                        "confidence": {"grade": "high"},
                        "targets": {"broad_heat": target},
                    }
                },
            }
            for sample_id, target in (
                ("train", 0.2),
                ("validation", 0.5),
                ("test", "must-not-be-parsed"),
            )
        }
        splits = {
            sample_id: {
                "account_time_split": sample_id,
                "published_at": "2026-05-20T08:00:00+00:00",
                "source_group_id": f"source-{sample_id}",
            }
            for sample_id in labels
        }
        metadata = {
            sample_id: {"content_category": "performance"}
            for sample_id in labels
        }

        rows = _protocol_rows(
            "account_time",
            labels=labels,
            splits=splits,
            metadata=metadata,
            allowed_splits={"train", "validation"},
        )

        self.assertEqual({row["sample_id"] for row in rows}, {"train", "validation"})

    def test_validation_artifact_excludes_test_labels_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_rows = []
            database_rows = []
            words = (
                "alpha",
                "bravo",
                "charlie",
                "delta",
                "echo",
                "foxtrot",
                "golf",
                "hotel",
                "india",
                "juliet",
                "kilo",
                "lima",
            )
            account_tokens = ("atlas", "boreal", "cedar", "dune", "elm", "fjord")
            for account_index, account_token in enumerate(account_tokens):
                account_id = f"account-{account_token}"
                for sample_index, word in enumerate(words):
                    sample_id = f"sample-{account_index}-{sample_index}"
                    high = sample_index >= 6
                    source_rows.append(
                        {
                            "account_id": account_id,
                            "captured_at": "2026-06-30T00:00:00+00:00",
                            "comments": sample_index + 1,
                            "comments_observed": True,
                            "duration_seconds": 20,
                            "favorites": sample_index + 2,
                            "favorites_observed": True,
                            "likes": sample_index * 10 + 1,
                            "likes_observed": True,
                            "id": sample_id,
                            "media_sha256": f"{account_index:02x}{sample_index:02x}" * 16,
                            "metric_sources": {
                                "comments": "fixture",
                                "favorites": "fixture",
                                "likes": "fixture",
                                "shares": "fixture",
                            },
                            "observation_date": "2026-06-30",
                            "observed_at": "2026-06-30T00:00:00+00:00",
                            "platform_item_id": f"item-{account_index}-{sample_index}",
                            "program_name": f"program-{account_token}-{word}",
                            "published_at": f"2026-05-{sample_index + 1:02d}T08:00:00+00:00",
                            "sample_id": sample_id,
                            "shares": sample_index + 3,
                            "shares_observed": True,
                            "song_title": f"song-{word}",
                            "title": f"{word} unique title {account_token}",
                        }
                    )
                    database_rows.append(
                        (
                            sample_id,
                            "performance" if high else "interview",
                            "highlight" if high else "setup",
                            "hook-payoff" if high else "setup-payoff",
                            "video",
                            0.9,
                            0.8,
                            "strong" if high else "weak",
                            1,
                            f"{word} unique title {account_token}",
                        )
                    )
            source = freeze_interaction_heat_artifact(
                artifact_id="fixture-labels-r1",
                rows=source_rows,
                output_root=root / "labels",
                source_metadata={"source_kind": "target-encoding-test"},
                min_group_samples=3,
            )
            database = root / "fixture.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE historical_capture_samples (
                        id TEXT PRIMARY KEY,
                        content_category TEXT,
                        hook_type TEXT,
                        slice_structure TEXT,
                        media_type TEXT,
                        classification_confidence REAL,
                        structure_confidence REAL,
                        entity_signal TEXT,
                        is_original_sound INTEGER,
                        title TEXT
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO historical_capture_samples VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    database_rows,
                )

            result = run_local_target_encoding_experiment(
                experiment_id="fixture-target-encoding-r1",
                label_artifact_dir=Path(source["artifact_dir"]),
                expected_label_manifest_sha256=source["manifest_sha256"],
                db_path=database,
                output_root=root / "experiments",
                config=TargetEncodingConfig(alpha=5.0, min_samples=2, folds=3),
                evaluation_scope="validation",
            )

            artifact = Path(result["artifact_dir"])
            self.assertEqual(
                {path.name for path in artifact.iterdir()},
                {"manifest.json", "model.json", "predictions.jsonl", "report.json"},
            )
            report = json.loads((artifact / "report.json").read_text())
            predictions = [
                json.loads(line)
                for line in (artifact / "predictions.jsonl").read_text().splitlines()
            ]
            self.assertEqual(report["evaluation_scope"], "validation")
            self.assertEqual(
                report["test_policy"],
                "sealed_not_loaded_by_this_experiment",
            )
            self.assertEqual(
                set(report["protocols"]),
                {"account_time", "account_holdout"},
            )
            self.assertEqual(
                {prediction["split"] for prediction in predictions},
                {"train_oof", "validation"},
            )
            self.assertTrue(all("target" not in prediction for prediction in predictions))
            self.assertTrue(all("likes" not in prediction for prediction in predictions))
            with self.assertRaises(FileExistsError):
                run_local_target_encoding_experiment(
                    experiment_id="fixture-target-encoding-r1",
                    label_artifact_dir=Path(source["artifact_dir"]),
                    expected_label_manifest_sha256=source["manifest_sha256"],
                    db_path=database,
                    output_root=root / "experiments",
                    config=TargetEncodingConfig(
                        alpha=5.0,
                        min_samples=2,
                        folds=3,
                    ),
                    evaluation_scope="validation",
                )

    def test_cross_fit_prediction_does_not_read_its_own_target(self) -> None:
        rows = [
            _row(f"sample-{index}", f"account-{index % 2}", index / 10)
            for index in range(10)
        ]
        changed = [dict(row) for row in rows]
        changed[3]["target"] = 1.0
        config = TargetEncodingConfig(alpha=5.0, folds=5, min_samples=2)

        original_predictions = cross_fit_target_encoder(
            rows,
            protocol="account_time",
            config=config,
        )
        changed_predictions = cross_fit_target_encoder(
            changed,
            protocol="account_time",
            config=config,
        )

        self.assertEqual(
            original_predictions["sample-3"].score,
            changed_predictions["sample-3"].score,
        )

    def test_account_holdout_prediction_does_not_use_account_identity(self) -> None:
        rows = [
            _row("a-low", "account-a", 0.1, category="interview"),
            _row("a-high", "account-a", 0.9, category="performance"),
            _row("b-low", "account-b", 0.2, category="interview"),
            _row("b-high", "account-b", 0.8, category="performance"),
        ]
        model = fit_target_encoder(
            rows,
            protocol="account_holdout",
            config=TargetEncodingConfig(alpha=2.0, min_samples=2),
        )
        first = _row("eval-a", "unseen-a", 0.0, category="performance")
        second = dict(first, account_id="unseen-b")

        first_prediction = predict_target_encoding(model, first)
        second_prediction = predict_target_encoding(model, second)

        self.assertEqual(first_prediction.score, second_prediction.score)
        self.assertFalse(model.include_account_history)

    def test_feature_extraction_ignores_interaction_outcomes(self) -> None:
        row = _row("sample", "account-a", 0.5)
        row["metadata"]["classification_confidence"] = "high"
        row["metadata"]["structure_confidence"] = "low"
        with_outcomes = dict(row)
        with_outcomes["metadata"] = dict(
            row["metadata"],
            likes=999999,
            comments=888,
            favorites=777,
            shares=666,
            normalized_reward=1.0,
        )

        fields = extract_target_encoding_fields(row)
        self.assertEqual(fields, extract_target_encoding_fields(with_outcomes))
        self.assertEqual(fields["classification_confidence"], ("high",))
        self.assertEqual(fields["structure_confidence"], ("low",))

    def test_unseen_values_fall_back_to_train_global_mean(self) -> None:
        rows = [
            _row("a-1", "account-a", 0.2, category="interview"),
            _row("a-2", "account-a", 0.4, category="interview"),
            _row("b-1", "account-b", 0.6, category="performance"),
            _row("b-2", "account-b", 0.8, category="performance"),
        ]
        model = fit_target_encoder(
            rows,
            protocol="account_holdout",
            config=TargetEncodingConfig(alpha=2.0, min_samples=2),
        )
        unseen = _row("eval", "new-account", 0.0, category="documentary")

        prediction = predict_target_encoding(model, unseen)

        self.assertAlmostEqual(model.global_mean, 0.5)
        self.assertGreater(prediction.fallback_counts.get("global_mean", 0), 0)
        self.assertGreater(prediction.feature_count, 0)


if __name__ == "__main__":
    unittest.main()
