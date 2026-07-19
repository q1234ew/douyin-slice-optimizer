from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso.db.session import connect, init_db
from dso.learning.qwen_embeddings import (
    QWEN_EMBEDDING_DIM,
    commit_scheduled_qwen_embedding,
    compute_scheduled_qwen_embedding,
    qwen_embedding_evidence_for_segment,
    qwen_embedding_scheduler_snapshot,
)
from dso.scheduler.benchmark import run_model_scheduler_benchmark
from dso.scheduler.profiles import RuntimeProfile
from dso.scheduler.resource_agent import ResourceAgentClient, RuntimeManager


class FakeEmbeddingClient:
    def health(self) -> dict:
        return {"status": "ready", "model_loaded": True, "model_id": "Qwen/Qwen3-VL-Embedding-2B"}

    def embed_text(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (QWEN_EMBEDDING_DIM - 1)


class ModelSchedulerPhase2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        init_db()

    def tearDown(self) -> None:
        os.environ.pop("DSO_ROOT", None)
        self.tmp.cleanup()

    def test_embedding_is_staged_before_business_record_commit(self) -> None:
        _insert_candidate()
        snapshot = qwen_embedding_scheduler_snapshot(entity_type="candidate", modality="text", limit=1)
        self.assertEqual(snapshot["item_count"], 1)
        result = compute_scheduled_qwen_embedding(snapshot["items"][0], client=FakeEmbeddingClient())
        with connect() as connection:
            before = connection.execute("SELECT COUNT(*) AS count FROM embedding_records").fetchone()
        self.assertEqual(before["count"], 0)

        summary = commit_scheduled_qwen_embedding(result)

        self.assertEqual(summary["status"], "ready")
        with connect() as connection:
            after = connection.execute(
                "SELECT status, vector_dim FROM embedding_records WHERE entity_id = 'phase2_segment'"
            ).fetchone()
        self.assertEqual(after["status"], "ready")
        self.assertEqual(after["vector_dim"], QWEN_EMBEDDING_DIM)

    def test_frozen_synthetic_mixed_workload_passes_contract_gates(self) -> None:
        output = self.root / "scheduler-report.json"
        report = run_model_scheduler_benchmark(output_path=output)

        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(report["checks"].values()))
        self.assertGreaterEqual(report["metrics"]["switch_reduction_rate"], 0.6)
        self.assertTrue(output.is_file())

    def test_runtime_manager_activates_only_whitelisted_profile_contract(self) -> None:
        calls = {"health": 0, "activate": 0}

        def health() -> dict:
            calls["health"] += 1
            return {"status": "ready" if calls["health"] >= 2 else "available", "model_id": "fake/model"}

        profile = RuntimeProfile(
            profile_id="fake.profile.v1",
            model_id="fake/model",
            service_url="http://test.invalid",
            capability="test",
            health=health,
            is_ready=lambda value: value.get("status") == "ready",
            actual_model_id=lambda value: str(value.get("model_id") or ""),
        )

        class Agent:
            def activate(self, claim, selected_profile):
                calls["activate"] += 1
                self.assert_profile = selected_profile.profile_id
                return {"status": "ready", "fencing_token": claim.fencing_token}

        claim = SimpleNamespace(
            job={"id": "job", "model_profile_id": profile.profile_id, "model_id": profile.model_id},
            item={"id": "item"},
            resource_id="gpu:0",
            attempt_id="attempt",
            fencing_token=7,
        )
        manager = RuntimeManager(agent=Agent())  # type: ignore[arg-type]
        with patch("dso.scheduler.resource_agent.runtime_profile", return_value=profile):
            result = manager.ensure_profile(claim)

        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["warm_hit"])
        self.assertEqual(calls["activate"], 1)

    def test_resource_agent_uses_separate_health_and_activation_timeouts(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DSO_GPU_RESOURCE_AGENT_ACTIVATION_TIMEOUT_SECONDS": "900",
                "DSO_GPU_RESOURCE_AGENT_HEALTH_TIMEOUT_SECONDS": "4",
            },
        ):
            client = ResourceAgentClient("http://agent.invalid", token="test-token")

        self.assertEqual(client.activation_timeout_seconds, 900.0)
        self.assertEqual(client.health_timeout_seconds, 4.0)

    def test_embedding_evidence_read_path_defers_gpu_work_when_scheduler_enabled(self) -> None:
        _insert_candidate()

        class ReadOnlyClient:
            def health(self) -> dict:
                return {"status": "service_unavailable"}

            def load(self) -> dict:  # pragma: no cover - a call would fail the test.
                raise AssertionError("read path must not load the embedding model")

        with patch.dict(os.environ, {"DSO_MODEL_SCHEDULER_ENABLED": "1"}):
            evidence = qwen_embedding_evidence_for_segment(
                "phase2_segment",
                client=ReadOnlyClient(),  # type: ignore[arg-type]
            )

        self.assertEqual(evidence["status"], "insufficient_embedding_evidence")
        self.assertTrue(evidence["build_results"])
        self.assertTrue(all(item["status"] == "deferred_scheduler" for item in evidence["build_results"]))


def _insert_candidate() -> None:
    now = "2026-07-18T00:00:00+00:00"
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO source_videos
            (id, account_id, title, original_path, file_path, duration_seconds, width,
             height, fps, audio_streams, status, created_at, updated_at)
            VALUES ('phase2_video', 'main', 'Phase 2', '/tmp/phase2.mp4',
                    '/tmp/phase2.mp4', 60, 1920, 1080, 25, 1, 'transcribed', ?, ?)
            """,
            [now, now],
        )
        connection.execute(
            """
            INSERT INTO candidate_segments
            (id, source_video_id, start_time, end_time, duration_seconds, transcript,
             summary, primary_topic, song_section_type, music_slice_type, emotion_type,
             short_video_structure, musical_moment, program_context, comment_trigger,
             cover_time, status, created_at)
            VALUES ('phase2_segment', 'phase2_video', 5, 35, 30,
                    '副歌高音爆发，全场观众欢呼', '完整音乐综艺爆点', '音乐综艺',
                    'climax_candidate', '综艺叙事爆点闭环型', '热血',
                    '节目上下文 -> 歌曲爆点 -> 现场反应', '副歌高音爆发',
                    '含节目上下文', '可讨论舞台爆发', 20, 'candidate', ?)
            """,
            [now],
        )
        connection.commit()


if __name__ == "__main__":
    unittest.main()
