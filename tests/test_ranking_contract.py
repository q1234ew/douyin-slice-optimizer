from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from dso.db.session import connect, init_db
from dso.learning.backtest import RESEARCH_RANKER_V24_STRATEGY, _promotion_gate
from dso.learning.benchmark_manifest import (
    CROSS_ENTRY_BENCHMARK_KIND,
    freeze_benchmark_manifest,
    verify_benchmark_manifest,
)
from dso.scoring.ranking_policy import attach_ranking_policy, production_ranking_contract
from dso.scoring.scorer import score_segment, suggestions
from dso.versions import STANDARD_CANDIDATE_VERSION


class RankingContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)

    def test_suggestions_default_to_current_rules_and_require_explicit_research_scope(self) -> None:
        self._insert_video_and_candidate("candidate-production", start=0, end=20)
        self._insert_video_and_candidate("candidate-research", start=20, end=40)
        score_segment("candidate-production")
        score_segment("candidate-research")
        with connect() as conn:
            conn.execute(
                "UPDATE slice_scores SET final_score = 90, ranker_score = 10, hybrid_score = 0 WHERE candidate_segment_id = ?",
                ["candidate-production"],
            )
            conn.execute(
                "UPDATE slice_scores SET final_score = 60, ranker_score = 99, hybrid_score = 99 WHERE candidate_segment_id = ?",
                ["candidate-research"],
            )
            conn.commit()

        production_rows = suggestions("ranking-video", top_k=2)
        research_rows = suggestions("ranking-video", top_k=2, ranking_scope="research")

        self.assertEqual(production_rows[0]["id"], "candidate-production")
        self.assertEqual(production_rows[0]["effective_score"], 90)
        self.assertEqual(production_rows[0]["production_ranking_strategy"], "current_rules")
        self.assertEqual(research_rows[0]["id"], "candidate-research")
        self.assertEqual(research_rows[0]["research_promotion_status"], "research_only")

    def test_promotion_gate_requires_absolute_thresholds_and_strongest_baseline_gain(self) -> None:
        target = {
            "topk_lift_vs_random": 1.90,
            "ndcg_at_k": 0.90,
            "high_interaction_hit_rate": 0.95,
            "low_interaction_avoidance_rate": 0.97,
        }
        accounts = [
            {"status": "ready", "improved_vs_current_rules": True}
            for _ in range(10)
        ]
        blocked = _promotion_gate(
            {
                "current_rules": {
                    "topk_lift_vs_random": 1.88,
                    "ndcg_at_k": 0.89,
                    "high_interaction_hit_rate": 0.94,
                    "low_interaction_avoidance_rate": 0.96,
                },
                "semantic_baseline_v2": {
                    "topk_lift_vs_random": 1.80,
                    "ndcg_at_k": 0.88,
                    "high_interaction_hit_rate": 0.93,
                    "low_interaction_avoidance_rate": 0.95,
                },
                RESEARCH_RANKER_V24_STRATEGY: target,
            },
            accounts,
            strategy=RESEARCH_RANKER_V24_STRATEGY,
        )
        self.assertTrue(blocked["threshold_gate_passed"])
        self.assertFalse(blocked["baseline_guard"]["passed"])
        self.assertFalse(blocked["passed"])

        eligible = _promotion_gate(
            {
                "current_rules": {
                    "topk_lift_vs_random": 1.80,
                    "ndcg_at_k": 0.88,
                    "high_interaction_hit_rate": 0.94,
                    "low_interaction_avoidance_rate": 0.96,
                },
                "semantic_baseline_v2": {
                    "topk_lift_vs_random": 1.81,
                    "ndcg_at_k": 0.89,
                    "high_interaction_hit_rate": 0.93,
                    "low_interaction_avoidance_rate": 0.95,
                },
                RESEARCH_RANKER_V24_STRATEGY: target,
            },
            accounts,
            strategy=RESEARCH_RANKER_V24_STRATEGY,
        )
        self.assertTrue(eligible["baseline_guard"]["passed"])
        self.assertTrue(eligible["passed"])
        self.assertFalse(eligible["automatic_promotion"])

    def test_cross_entry_manifest_freezes_both_origins_and_shared_contract(self) -> None:
        self._insert_source("program-video", input_mode="program", duration=120)
        self._insert_source("precut-video", input_mode="precut", duration=30)
        self._insert_candidate(
            "generated-candidate",
            "program-video",
            start=10,
            end=40,
            origin="generated",
            locked=0,
        )
        self._insert_candidate(
            "precut-candidate",
            "precut-video",
            start=0,
            end=30,
            origin="precut",
            locked=1,
        )
        manifest_path = self.root / "benchmarks" / "cross-entry-test-v1.json"

        frozen = freeze_benchmark_manifest(
            "cross-entry-test-v1",
            path=manifest_path,
            source_files=[],
            benchmark_kind=CROSS_ENTRY_BENCHMARK_KIND,
        )
        manifest = frozen["manifest"]
        snapshot = manifest["snapshot"]["cross_entry_candidates"]

        self.assertEqual(manifest["run_config"]["strategy"], "current_rules")
        self.assertEqual(manifest["production_ranking_policy"], production_ranking_contract())
        self.assertEqual(snapshot["candidate_origin_counts"], {"generated": 1, "precut": 1})
        self.assertTrue(snapshot["contract_checks"]["shared_candidate_contract_observed"])
        self.assertTrue(snapshot["contract_checks"]["precut_boundary_invariant"])
        self.assertTrue(verify_benchmark_manifest("cross-entry-test-v1", path=manifest_path)["passed"])

    def _insert_video_and_candidate(self, candidate_id: str, *, start: float, end: float) -> None:
        with connect() as conn:
            exists = conn.execute("SELECT 1 FROM source_videos WHERE id = 'ranking-video'").fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO source_videos
                    (id, account_id, title, original_path, file_path, duration_seconds, width,
                     height, fps, audio_streams, status, created_at, updated_at)
                    VALUES ('ranking-video', 'main', '排序测试', '/tmp/ranking.mp4',
                            '/tmp/ranking.mp4', 60, 1920, 1080, 25, 1, 'transcribed', ?, ?)
                    """,
                    ["2026-07-18T00:00:00+00:00", "2026-07-18T00:00:00+00:00"],
                )
            conn.execute(
                """
                INSERT INTO candidate_segments
                (id, source_video_id, start_time, end_time, duration_seconds, transcript,
                 summary, status, created_at)
                VALUES (?, 'ranking-video', ?, ?, ?, '副歌高音爆发，全场欢呼', '排序测试候选',
                        'candidate', ?)
                """,
                [candidate_id, start, end, end - start, "2026-07-18T00:00:00+00:00"],
            )
            conn.commit()

    def _insert_source(self, source_id: str, *, input_mode: str, duration: float) -> None:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO source_videos
                (id, account_id, title, original_path, file_path, duration_seconds, width,
                 height, fps, audio_streams, status, input_mode, created_at, updated_at)
                VALUES (?, 'main', ?, ?, ?, ?, 1920, 1080, 25, 1, 'transcribed', ?, ?, ?)
                """,
                [
                    source_id,
                    source_id,
                    f"/tmp/{source_id}.mp4",
                    f"/tmp/{source_id}.mp4",
                    duration,
                    input_mode,
                    "2026-07-18T00:00:00+00:00",
                    "2026-07-18T00:00:00+00:00",
                ],
            )
            conn.commit()

    def _insert_candidate(
        self,
        candidate_id: str,
        source_id: str,
        *,
        start: float,
        end: float,
        origin: str,
        locked: int,
    ) -> None:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO candidate_segments
                (id, source_video_id, start_time, end_time, duration_seconds, transcript,
                 summary, status, boundary_strategy, boundary_confidence, candidate_origin,
                 boundary_locked, candidate_contract_version, created_at)
                VALUES (?, ?, ?, ?, ?, '测试文本', '跨入口候选', 'candidate', ?, 1, ?, ?, ?, ?)
                """,
                [
                    candidate_id,
                    source_id,
                    start,
                    end,
                    end - start,
                    "source_asset_full_duration" if origin == "precut" else "program_recall",
                    origin,
                    locked,
                    STANDARD_CANDIDATE_VERSION,
                    "2026-07-18T00:00:00+00:00",
                ],
            )
            conn.commit()


class RankingPolicyUnitTest(unittest.TestCase):
    def test_unscored_candidate_does_not_gain_a_zero_production_score(self) -> None:
        row = attach_ranking_policy({"id": "unscored", "ranker_score": 88})
        self.assertIsNone(row["production_score"])
        self.assertIsNone(row["effective_score"])
        self.assertEqual(row["research_score"], 88)


if __name__ == "__main__":
    unittest.main()
