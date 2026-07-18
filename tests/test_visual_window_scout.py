from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dso.db.session import connect, init_db
from dso.learning.visual_window_scout import (
    QWEN_EMBEDDING_DIM,
    QWEN_EMBEDDING_MODEL,
    VISUAL_WINDOW_ENTITY_TYPE,
    VISUAL_WINDOW_MODALITY,
    _persist_visual_window_build,
    _select_visual_window_batch,
    _visual_media_contract,
    _window_entity_id,
    _window_prototypes,
    dynamic_window_fusion,
    load_visual_window_build,
    load_visual_window_build_manifest,
    run_visual_window_experiment,
    update_material_window_annotation,
    visual_window_scout_status,
)


class VisualWindowScoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DSO_ROOT"] = self.tmp.name
        init_db()

    def tearDown(self) -> None:
        os.environ.pop("DSO_ROOT", None)
        self.tmp.cleanup()

    def test_visual_route_does_not_require_audio(self) -> None:
        video = Path(self.tmp.name) / "sample.mp4"
        video.touch()
        row = {
            "sample_id": "sample_1",
            "expected_duration_seconds": 60.0,
        }
        with patch(
            "dso.learning.visual_window_scout.probe_video",
            return_value={"duration_seconds": 60.0, "audio_streams": 0},
        ):
            visual = _visual_media_contract(row, {"video": [str(video)]}, requires_audio=False, duration_tolerance=0.15)
            audio = _visual_media_contract(row, {"video": [str(video)]}, requires_audio=True, duration_tolerance=0.15)

        self.assertTrue(visual["eligible"])
        self.assertEqual(visual["audio_source"], "missing_audio")
        self.assertFalse(audio["eligible"])
        self.assertIn("audio_missing", audio["exclusion_reasons"])

    def test_dynamic_fusion_changes_with_scene_form(self) -> None:
        stage_score, stage_weights = dynamic_window_fusion(
            visual_score=0.8,
            text_score=0.2,
            visual_available=True,
            text_available=True,
            predicted_scene_form="stage_performance",
        )
        news_score, news_weights = dynamic_window_fusion(
            visual_score=0.8,
            text_score=0.2,
            visual_available=True,
            text_available=True,
            predicted_scene_form="news_document",
        )

        self.assertGreater(stage_weights["visual"], news_weights["visual"])
        self.assertGreater(stage_score, news_score)

    def test_summary_status_reuses_matching_latest_media_readiness(self) -> None:
        latest = {
            "status": "needs_window_gold",
            "query": {"account_id": "all", "dataset_id": "all"},
            "media_readiness": {
                "route": "visual_audio_optional",
                "confirmed_gold_count": 60,
                "eligible_count": 58,
                "eligible_rate": 0.9667,
                "visual_ready_count": 58,
                "audio_ready_count": 27,
            },
            "samples": [],
        }
        with patch("dso.learning.visual_window_scout._latest_report", return_value=latest), patch(
            "dso.learning.visual_window_scout.material_visual_media_readiness"
        ) as full_scan:
            status = visual_window_scout_status(summary_only=True)

        full_scan.assert_not_called()
        self.assertEqual(status["media_readiness"]["eligible_count"], 58)
        self.assertEqual(status["media_readiness"]["audio_ready_count"], 27)
        self.assertEqual(status["media_readiness"]["source"], "latest_build_summary")

    def test_status_does_not_mark_partial_embeddings_ready(self) -> None:
        latest = {
            "status": "ready_for_window_gold_review",
            "query": {"account_id": "all", "dataset_id": "all"},
            "media_readiness": {"eligible_count": 10, "visual_ready_count": 10},
            "sample_count": 2,
            "candidate_count": 6,
            "embedding_ready_count": 1,
            "samples": [],
        }
        with patch("dso.learning.visual_window_scout._latest_report", return_value=latest), patch(
            "dso.learning.visual_window_scout.material_visual_media_readiness",
            return_value={"eligible_count": 10},
        ):
            status = visual_window_scout_status(summary_only=True)

        self.assertEqual(status["status"], "needs_embedding_retry")
        self.assertEqual(status["latest_build"]["embedding_coverage"], 0.1667)

    def test_window_gold_is_isolated_and_audited(self) -> None:
        self._insert_sample("sample_1")

        result = update_material_window_annotation(
            "sample_1",
            {
                "start_seconds": 10,
                "end_seconds": 25,
                "scene_form": "rehearsal",
                "program_context_mode": "present",
                "selection_quality": "target",
                "review_note": "visible rehearsal cues",
                "operator": "test",
            },
        )

        with connect() as conn:
            annotation = conn.execute("SELECT * FROM material_window_annotations").fetchone()
            change = conn.execute(
                "SELECT * FROM change_events WHERE entity_type = 'material_window_annotation'"
            ).fetchone()
            historical = conn.execute(
                "SELECT content_category, slice_structure FROM historical_capture_samples WHERE id = 'sample_1'"
            ).fetchone()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(annotation["scene_form"], "rehearsal")
        self.assertEqual(annotation["selection_quality"], "target")
        self.assertIsNotNone(change)
        self.assertEqual(historical["content_category"], "")
        self.assertEqual(historical["slice_structure"], "")

    def test_frozen_experiment_uses_reviewed_top2_only(self) -> None:
        self._insert_sample("sample_1")
        target_id = _window_entity_id("sample_1", 0, 15)
        miss_id = _window_entity_id("sample_1", 20, 35)
        update_material_window_annotation(
            "sample_1",
            {
                "start_seconds": 0,
                "end_seconds": 15,
                "scene_form": "stage_performance",
                "program_context_mode": "present",
                "selection_quality": "target",
            },
        )
        update_material_window_annotation(
            "sample_1",
            {
                "start_seconds": 20,
                "end_seconds": 35,
                "scene_form": "backstage_interview",
                "program_context_mode": "absent",
                "selection_quality": "irrelevant",
            },
        )
        report = {
            "samples": [
                {
                    "sample_id": "sample_1",
                    "strategy_windows": {
                        "fixed": [miss_id],
                        "text": [miss_id],
                        "visual": [target_id],
                        "fusion": [target_id],
                    },
                    "candidates": [
                        {"window_id": target_id, "start_seconds": 0, "end_seconds": 15},
                        {"window_id": miss_id, "start_seconds": 20, "end_seconds": 35},
                    ],
                }
            ]
        }

        result = run_visual_window_experiment(report=report)

        self.assertEqual(result["strategy_comparison"]["fusion"]["recall_at_2"], 1.0)
        self.assertEqual(result["strategy_comparison"]["fixed"]["severe_miss_rate"], 1.0)
        self.assertEqual(result["paired_comparison"]["fusion_vs_fixed"]["paired_sample_count"], 1)
        self.assertEqual(result["paired_comparison"]["fusion_vs_text"]["recall_delta"], 1.0)
        self.assertEqual(result["status"], "needs_window_gold")

    def test_uncertain_gold_is_abstention_not_false_miss(self) -> None:
        self._insert_sample("sample_1")
        uncertain_id = _window_entity_id("sample_1", 0, 15)
        update_material_window_annotation(
            "sample_1",
            {
                "start_seconds": 0,
                "end_seconds": 15,
                "scene_form": "stage_performance",
                "program_context_mode": "present",
                "selection_quality": "uncertain",
            },
        )
        report = {
            "samples": [
                {
                    "sample_id": "sample_1",
                    "account_id": "account_1",
                    "strategy_windows": {
                        "fixed": [uncertain_id],
                        "text": [],
                        "visual": [uncertain_id],
                        "fusion": [uncertain_id],
                    },
                    "review_windows": [{"window_id": uncertain_id}],
                    "candidates": [{"window_id": uncertain_id}],
                }
            ]
        }

        result = run_visual_window_experiment(report=report, persist=False)

        fusion = result["strategy_comparison"]["fusion"]
        self.assertEqual(fusion["abstained_sample_count"], 1)
        self.assertEqual(fusion["evaluable_sample_count"], 0)
        self.assertEqual(fusion["unknown_abstention_rate"], 1.0)
        self.assertIsNone(fusion["recall_at_2"])
        self.assertEqual(result["paired_comparison"]["fusion_vs_text"]["status"], "not_comparable")
        self.assertIsNone(result["promotion_gate"]["observed"]["delta_vs_text"])

    def test_next_batch_is_deterministic_balanced_and_excludes_reviewed(self) -> None:
        rows = [
            {"sample_id": "s1", "account_id": "a1", "gold_material_type": "performance_clip"},
            {"sample_id": "s2", "account_id": "a1", "gold_material_type": "performance_clip"},
            {"sample_id": "s3", "account_id": "a2", "gold_material_type": "vocal_teaching"},
            {"sample_id": "s4", "account_id": "a3", "gold_material_type": "compilation"},
            {"sample_id": "s5", "account_id": "a4", "gold_material_type": "reaction"},
        ]

        first, summary = _select_visual_window_batch(
            rows,
            requested_sample_ids=set(),
            reviewed_sample_ids={"s1"},
            limit=3,
            batch_mode="next",
        )
        second, _ = _select_visual_window_batch(
            rows,
            requested_sample_ids=set(),
            reviewed_sample_ids={"s1"},
            limit=3,
            batch_mode="next",
        )

        first_ids = [row["sample_id"] for row in first]
        self.assertEqual(first_ids, [row["sample_id"] for row in second])
        self.assertNotIn("s1", first_ids)
        self.assertEqual(len(set(first_ids)), 3)
        self.assertEqual(summary["excluded_reviewed_sample_count"], 1)
        self.assertGreaterEqual(len(summary["selected_material_type_counts"]), 3)

    def test_build_manifest_is_immutable_and_verifiable(self) -> None:
        report = {
            "contract_version": "material_visual_window_scout.v1.1",
            "build_id": "d11b_test_manifest",
            "generated_at": "2026-07-17T00:00:00Z",
            "query": {"batch_mode": "next"},
            "selection_summary": {"selected_count": 1},
            "samples": [
                {
                    "sample_id": "sample_1",
                    "account_id": "account_1",
                    "gold_material_type": "performance_clip",
                    "review_windows": [{"window_id": "window_1"}],
                    "strategy_windows": {"fixed": ["window_1"]},
                    "prototype_summary": {"policy": "leave_one_sample_out", "source_sample_ids": []},
                }
            ],
        }

        persisted = _persist_visual_window_build(report)
        loaded = load_visual_window_build(report["build_id"])
        manifest = load_visual_window_build_manifest(report["build_id"])

        self.assertEqual(persisted["build_id"], report["build_id"])
        self.assertEqual(loaded["build_id"], report["build_id"])
        self.assertTrue(manifest["verification"]["passed"])
        with self.assertRaises(FileExistsError):
            _persist_visual_window_build(report)

    def test_cumulative_experiment_combines_frozen_batches_without_duplicate_samples(self) -> None:
        for sample_id in ["sample_1", "sample_2"]:
            self._insert_sample(sample_id)
        reports = []
        for index, sample_id in enumerate(["sample_1", "sample_2", "sample_1"], start=1):
            start = float(index * 20)
            window_id = _window_entity_id(sample_id, start, start + 15)
            update_material_window_annotation(
                sample_id,
                {
                    "start_seconds": start,
                    "end_seconds": start + 15,
                    "scene_form": "stage_performance",
                    "program_context_mode": "present",
                    "selection_quality": "target",
                },
            )
            report = {
                "contract_version": "material_visual_window_scout.v1.1",
                "build_id": f"d11b_cumulative_{index}",
                "generated_at": f"2026-07-17T00:00:0{index}Z",
                "sample_count": 1,
                "candidate_count": 1,
                "embedding_ready_count": 1,
                "selection_summary": {"selected_count": 1},
                "samples": [
                    {
                        "sample_id": sample_id,
                        "account_id": f"account_{index}",
                        "candidate_count": 1,
                        "embedding_ready_count": 1,
                        "review_windows": [{"window_id": window_id}],
                        "candidates": [{"window_id": window_id, "embedding_status": "created"}],
                        "strategy_windows": {
                            "fixed": [window_id],
                            "text": [window_id],
                            "visual": [window_id],
                            "fusion": [window_id],
                        },
                        "prototype_summary": {
                            "policy": "leave_one_sample_out",
                            "source_sample_ids": [],
                        },
                    }
                ],
            }
            _persist_visual_window_build(report)
            reports.append(report)

        result = run_visual_window_experiment(
            build_id=reports[-1]["build_id"],
            scope="cumulative",
            persist=False,
        )

        self.assertEqual(result["evaluation_scope"], "cumulative_frozen_builds")
        self.assertEqual(result["source_build_count"], 3)
        self.assertEqual(result["evaluation_summary"]["sample_count"], 2)
        self.assertEqual(result["strategy_comparison"]["fusion"]["evaluable_sample_count"], 2)
        self.assertTrue(result["build_manifest_verification"]["passed"])
        self.assertTrue(result["embedding_coverage_summary"]["passed"])

    def test_prototypes_exclude_current_sample(self) -> None:
        self._insert_sample("sample_1")
        self._insert_sample("sample_2")
        window_ids = []
        for index, sample_id in enumerate(["sample_1", "sample_2"], start=1):
            result = update_material_window_annotation(
                sample_id,
                {
                    "start_seconds": index * 10,
                    "end_seconds": index * 10 + 15,
                    "scene_form": "vocal_teaching",
                    "program_context_mode": "absent",
                    "selection_quality": "target",
                },
            )
            window_ids.append(result["window_id"])
        with connect() as conn:
            for index, window_id in enumerate(window_ids, start=1):
                conn.execute(
                    """
                    INSERT INTO embedding_records (
                        id, entity_type, entity_id, modality, model_name, vector_dim,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ready', '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z')
                    """,
                    [
                        f"emb_{index}", VISUAL_WINDOW_ENTITY_TYPE, window_id,
                        VISUAL_WINDOW_MODALITY, QWEN_EMBEDDING_MODEL, QWEN_EMBEDDING_DIM,
                    ],
                )
            conn.commit()

        with patch("dso.learning.visual_window_scout._load_vector", return_value=[1.0] * QWEN_EMBEDDING_DIM):
            prototypes = _window_prototypes(exclude_sample_ids={"sample_1"})

        self.assertEqual(prototypes["vocal_teaching"]["sample_ids"], ["sample_2"])

    @staticmethod
    def _insert_sample(sample_id: str) -> None:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO historical_capture_samples (
                    id, account_id, dataset_id, sample_key, title, created_at, updated_at
                ) VALUES (?, 'account_1', 'dataset_1', ?, 'test sample', '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z')
                """,
                [sample_id, sample_id],
            )
            conn.commit()


if __name__ == "__main__":
    unittest.main()
