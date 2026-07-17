from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dso.db.session import connect, init_db
from dso.learning.visual_window_scout import (
    _visual_media_contract,
    _window_entity_id,
    dynamic_window_fusion,
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
        self.assertEqual(result["status"], "needs_window_gold")

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
