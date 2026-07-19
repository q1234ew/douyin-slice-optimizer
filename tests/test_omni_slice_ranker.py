from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dso.db.session import connect, init_db
from dso.learning.omni_slice_ranker import (
    candidate_window_plan,
    commit_scheduled_omni_windows,
    commit_scheduled_omni_rerank,
    omni_rerank_input_snapshot,
    rerank_video_candidates_with_omni,
)
from dso.learning.qwen_omni import QWEN_OMNI_MODEL
from dso.scoring.scorer import score_segment


class FakeOmniClient:
    def __init__(self, *, loaded_model: str = QWEN_OMNI_MODEL) -> None:
        self.model_id = QWEN_OMNI_MODEL
        self.service_url = "mock-omni"
        self.loaded_model = loaded_model
        self.payloads: list[dict] = []

    def health(self) -> dict:
        return {
            "status": "ready",
            "service_url": self.service_url,
            "raw": {
                "status": "ready",
                "torch": {
                    "cuda_available": True,
                    "devices": [{"index": 0, "name": "RTX test", "total_memory_gb": 15.47}],
                },
                "model": {"loaded": True, "model_id": self.loaded_model},
            },
        }

    def load(self, *, model_id: str | None = None, max_clip_seconds: float = 8.0) -> dict:
        self.loaded_model = model_id or self.model_id
        return self.health()

    def analyze_clip_file(self, payload: dict, video_path: str | Path) -> dict:
        self.payloads.append({**payload, "video_path": str(video_path)})
        role = str(payload.get("window_role") or "middle")
        boost = {"hook": 92, "middle": 78, "payoff": 96}.get(role, 75)
        return {
            "status": "ready",
            "semantic_suggestions": {
                "scores": {
                    "hook_strength": boost if role == "hook" else 72,
                    "context_completeness": 84,
                    "payoff_strength": boost if role == "payoff" else 76,
                    "reaction_strength": 88,
                    "audio_visual_alignment": 91,
                    "boundary_quality": 86,
                    "risk": 6,
                },
                "evidence": [f"{role}_evidence"],
                "boundary_advice": {"start_adjust_seconds": 0, "end_adjust_seconds": 0},
            },
        }


class BusyOmniClient(FakeOmniClient):
    def health(self) -> dict:
        payload = super().health()
        payload["status"] = "busy"
        payload["raw"]["status"] = "busy"
        payload["raw"]["inference"] = {"status": "busy", "busy": True, "busy_seconds": 4.2}
        return payload


class OmniSliceRankerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        init_db()
        _insert_candidate()
        score_segment("seg_hybrid")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)

    def test_candidate_window_plan_uses_candidate_absolute_range(self) -> None:
        plan = candidate_window_plan(
            {"start_time": 10, "end_time": 50, "duration_seconds": 40},
            max_clip_seconds=8,
        )

        self.assertEqual([item["window"] for item in plan], ["hook", "middle", "payoff"])
        self.assertEqual(plan[0]["absolute_start_seconds"], 10)
        self.assertEqual(plan[-1]["absolute_start_seconds"], 42)

    def test_ready_omni_multi_window_updates_hybrid_rank(self) -> None:
        client = FakeOmniClient()
        dummy_clip = self.root / "window.mp4"
        dummy_clip.write_bytes(b"test")
        with patch(
            "dso.learning.omni_slice_ranker._prepare_candidate_window",
            return_value=(dummy_clip, {"clip_path": str(dummy_clip), "cache_hit": False}),
        ):
            report = rerank_video_candidates_with_omni(
                "video_hybrid",
                candidate_limit=1,
                max_clip_seconds=8,
                omni_weight=0.18,
                client=client,
            )

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["omni_applied_count"], 1)
        self.assertEqual([payload["window_role"] for payload in client.payloads], ["hook", "middle", "payoff"])
        self.assertTrue(all(payload["prompt_profile"] == "hybrid_slice_rerank" for payload in client.payloads))
        self.assertTrue(all(payload["max_new_tokens"] == 128 for payload in client.payloads))
        with connect() as conn:
            row = conn.execute(
                "SELECT omni_status, omni_score, omni_confidence, hybrid_score, hybrid_rank FROM slice_scores WHERE candidate_segment_id = 'seg_hybrid'"
            ).fetchone()
        self.assertEqual(row["omni_status"], "ready")
        self.assertGreater(row["omni_score"], 70)
        self.assertGreater(row["omni_confidence"], 0.5)
        self.assertEqual(row["hybrid_rank"], 1)

    def test_embedding_model_keeps_deterministic_fallback(self) -> None:
        report = rerank_video_candidates_with_omni(
            "video_hybrid",
            candidate_limit=1,
            client=FakeOmniClient(loaded_model="Qwen/Qwen3-VL-Embedding-2B"),
        )

        self.assertEqual(report["status"], "fallback")
        self.assertEqual(report["omni_applied_count"], 0)
        with connect() as conn:
            row = conn.execute(
                "SELECT ranker_score, hybrid_score, omni_status FROM slice_scores WHERE candidate_segment_id = 'seg_hybrid'"
            ).fetchone()
        self.assertEqual(row["hybrid_score"], row["ranker_score"])
        self.assertEqual(row["omni_status"], "fallback_model_switch_required")

    def test_busy_service_uses_explicit_busy_fallback(self) -> None:
        report = rerank_video_candidates_with_omni(
            "video_hybrid",
            candidate_limit=1,
            client=BusyOmniClient(),
        )

        self.assertEqual(report["status"], "fallback")
        self.assertEqual(report["fallback_reason"], "busy")
        with connect() as conn:
            row = conn.execute(
                "SELECT hybrid_score, omni_status FROM slice_scores WHERE candidate_segment_id = 'seg_hybrid'"
            ).fetchone()
        self.assertEqual(row["omni_status"], "fallback_busy")

    def test_scheduled_result_does_not_write_before_fenced_commit(self) -> None:
        client = FakeOmniClient()
        dummy_clip = self.root / "scheduled-window.mp4"
        dummy_clip.write_bytes(b"test")
        snapshot = omni_rerank_input_snapshot(
            "video_hybrid",
            candidate_limit=1,
            max_clip_seconds=8,
            omni_weight=0.18,
        )
        with patch(
            "dso.learning.omni_slice_ranker._prepare_candidate_window",
            return_value=(dummy_clip, {"clip_path": str(dummy_clip), "cache_hit": False}),
        ):
            report = rerank_video_candidates_with_omni(
                "video_hybrid",
                candidate_limit=1,
                max_clip_seconds=8,
                omni_weight=0.18,
                client=client,
                persist=False,
            )

        with connect() as conn:
            before = conn.execute(
                "SELECT omni_status FROM slice_scores WHERE candidate_segment_id = 'seg_hybrid'"
            ).fetchone()
        self.assertEqual(before["omni_status"], "not_run")

        summary = commit_scheduled_omni_rerank(
            report,
            expected_input_hash=snapshot["input_hash"],
            candidate_limit=1,
            max_clip_seconds=8,
            omni_weight=0.18,
        )

        self.assertEqual(summary["status"], "ready")
        with connect() as conn:
            after = conn.execute(
                "SELECT omni_status, hybrid_rank FROM slice_scores WHERE candidate_segment_id = 'seg_hybrid'"
            ).fetchone()
        self.assertEqual(after["omni_status"], "ready")
        self.assertEqual(after["hybrid_rank"], 1)

    def test_scheduler_snapshot_splits_and_commits_independent_windows(self) -> None:
        snapshot = omni_rerank_input_snapshot(
            "video_hybrid",
            candidate_limit=1,
            max_clip_seconds=8,
            omni_weight=0.18,
        )
        self.assertEqual([item["window_role"] for item in snapshot["window_items"]], ["hook", "middle", "payoff"])
        self.assertEqual(len({item["input_hash"] for item in snapshot["window_items"]}), 3)
        item_results = [
            {
                "item_status": "succeeded",
                "result": {
                    "status": "ready",
                    "segment_id": item["segment_id"],
                    "window_role": item["window_role"],
                    "window": item["window"],
                    "role_score": 80.0 + index,
                    "confidence": 0.9,
                    "boundary_advice": {},
                },
            }
            for index, item in enumerate(snapshot["window_items"])
        ]

        summary = commit_scheduled_omni_windows(
            "video_hybrid",
            item_results,
            expected_input_hash=snapshot["input_hash"],
            candidate_limit=1,
            max_clip_seconds=8,
            omni_weight=0.18,
        )

        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["completed_window_count"], 3)
        with connect() as conn:
            row = conn.execute(
                "SELECT omni_status, hybrid_rank FROM slice_scores WHERE candidate_segment_id = 'seg_hybrid'"
            ).fetchone()
        self.assertEqual(row["omni_status"], "ready")
        self.assertEqual(row["hybrid_rank"], 1)


def _insert_candidate() -> None:
    now = "2026-07-17T00:00:00+00:00"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO source_videos
            (id, account_id, title, original_path, file_path, duration_seconds, width, height, fps, audio_streams, status, transcript_path, created_at, updated_at)
            VALUES ('video_hybrid', 'main', 'hybrid demo', '/tmp/demo.mp4', '/tmp/demo.mp4', 120, 1920, 1080, 25, 1, 'transcribed', NULL, ?, ?)
            """,
            [now, now],
        )
        conn.execute(
            """
            INSERT INTO candidate_segments
            (id, source_video_id, performance_id, start_time, end_time, duration_seconds, transcript, summary, primary_topic, song_section_type,
             music_slice_type, emotion_type, short_video_structure, musical_moment, program_context, comment_trigger, cover_time, status, created_at)
            VALUES ('seg_hybrid', 'video_hybrid', NULL, 10, 50, 40,
                    '导师点评之后副歌高音爆发，全场观众起立欢呼', '完整音乐综艺爆点', '音乐综艺', 'climax_candidate',
                    '综艺叙事爆点闭环型', '热血', '节目上下文 -> 歌曲爆点 -> 现场反应', '副歌高音爆发',
                    '含导师评价等节目上下文', '可讨论舞台爆发和观众反应', 26, 'candidate', ?)
            """,
            [now],
        )
        conn.commit()


if __name__ == "__main__":
    unittest.main()
