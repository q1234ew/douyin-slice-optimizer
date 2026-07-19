from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from dso.db.session import connect, init_db
from dso.scheduler.contracts import JobItemSpec, ModelJobSpec, stable_json_hash
from dso.scheduler.db import scheduler_connect
from dso.scheduler.guard import SchedulerLeaseRequired, require_scheduler_lease, scheduler_execution
from dso.scheduler.repository import LeaseLost, ModelJobRepository
from dso.scheduler.worker import ModelWorker
from dso.scoring.scorer import score_segment
from dso.utils import write_json


class FakeAdapter:
    def __init__(self) -> None:
        self.execute_count = 0
        self.commit_count = 0

    def execute(self, job: dict, item: dict) -> dict:
        self.execute_count += 1
        return {"status": "ready", "value": item["request"].get("value", 1)}

    def commit(self, job: dict, result: dict) -> dict:
        self.commit_count += 1
        return {"status": "ready", "value": result["value"]}


class MultiAdapter:
    def __init__(self) -> None:
        self.executed: list[tuple[str, int]] = []

    def execute(self, job: dict, item: dict) -> dict:
        value = int(item["request"].get("value") or 0)
        self.executed.append((str(job["id"]), value))
        return {"status": "ready", "value": value}

    def commit_item(self, job: dict, item: dict, result: dict) -> dict:
        return {"status": "ready", "value": result["value"]}

    def finalize(self, job: dict, results: list[dict]) -> dict:
        values = [int((item.get("result") or {}).get("value") or 0) for item in results]
        return {"status": "ready", "values": values, "item_count": len(values)}


class ModelSchedulerRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        self.repository = ModelJobRepository()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)

    def test_active_dedupe_returns_same_persistent_job(self) -> None:
        first = self.repository.enqueue(_spec("dedupe", value=1))
        second = self.repository.enqueue(_spec("dedupe", value=1))

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertFalse(second.cache_hit)
        self.assertEqual(first.job["job_id"], second.job["job_id"])
        self.assertEqual(second.job["status"], "queued")

    def test_worker_stages_then_commits_and_completed_job_is_cached(self) -> None:
        adapter = FakeAdapter()
        first = self.repository.enqueue(_spec("worker", value=7))
        worker = ModelWorker(
            self.repository,
            "worker-a",
            adapters={"test_job": adapter},
        )

        completed = worker.run_once()

        self.assertIsNotNone(completed)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result_summary"]["value"], 7)
        self.assertEqual(adapter.execute_count, 1)
        self.assertEqual(adapter.commit_count, 1)
        events = self.repository.events(first.job["job_id"])
        self.assertIn("result_staged", [event["event_type"] for event in events])
        cached = self.repository.enqueue(_spec("worker", value=7))
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.job["job_id"], first.job["job_id"])

    def test_single_resource_lease_and_fencing_reject_stale_owner(self) -> None:
        self.repository.enqueue(_spec("lease-a", value=1))
        self.repository.enqueue(_spec("lease-b", value=2))
        _prepare_all(self.repository)
        old_claim = self.repository.claim_next(worker_id="worker-old", resource_id="gpu:0", lease_ttl_seconds=60)
        self.assertIsNotNone(old_claim)
        self.assertIsNone(self.repository.claim_next(worker_id="worker-blocked", resource_id="gpu:0"))

        with scheduler_connect(self.repository.db_path) as connection:
            connection.execute(
                "UPDATE gpu_resource_leases SET expires_at = ? WHERE resource_id = 'gpu:0'",
                [(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()],
            )
        new_claim = self.repository.claim_next(worker_id="worker-new", resource_id="gpu:0", lease_ttl_seconds=60)
        self.assertIsNotNone(new_claim)
        assert old_claim is not None and new_claim is not None
        self.assertGreater(new_claim.fencing_token, old_claim.fencing_token)
        with self.assertRaises(LeaseLost):
            self.repository.stage_result(old_claim, self.root / "stale.json", inference_ms=1)

    def test_staged_result_is_reused_after_stale_worker_recovery(self) -> None:
        adapter = FakeAdapter()
        created = self.repository.enqueue(_spec("recover", value=9))
        _prepare_all(self.repository)
        old_claim = self.repository.claim_next(worker_id="worker-old", resource_id="gpu:0", lease_ttl_seconds=60)
        assert old_claim is not None
        staged_path = self.root / "staged-result.json"
        write_json(
            staged_path,
            {
                "contract_version": "model_staged_result.v1",
                "job_id": old_claim.job["id"],
                "item_id": old_claim.item["id"],
                "attempt_id": old_claim.attempt_id,
                "input_hash": old_claim.item["input_hash"],
                "result": {"status": "ready", "value": 9},
            },
        )
        self.repository.stage_result(old_claim, staged_path, inference_ms=10)
        with scheduler_connect(self.repository.db_path) as connection:
            connection.execute(
                "UPDATE gpu_resource_leases SET expires_at = ? WHERE resource_id = 'gpu:0'",
                [(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()],
            )

        worker = ModelWorker(self.repository, "worker-new", adapters={"test_job": adapter})
        completed = worker.run_once()

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["job_id"], created.job["job_id"])
        self.assertEqual(adapter.execute_count, 0)
        self.assertEqual(adapter.commit_count, 1)

    def test_cancel_is_idempotent_before_dispatch(self) -> None:
        created = self.repository.enqueue(_spec("cancel", value=3))
        first = self.repository.cancel(created.job["job_id"])
        second = self.repository.cancel(created.job["job_id"])

        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(second["status"], "cancelled")
        self.assertIsNone(self.repository.claim_next(worker_id="worker-a"))

    def test_gpu_calls_require_worker_context_when_scheduler_is_enabled(self) -> None:
        os.environ["DSO_MODEL_SCHEDULER_ENABLED"] = "1"
        self.addCleanup(os.environ.pop, "DSO_MODEL_SCHEDULER_ENABLED", None)
        with self.assertRaises(SchedulerLeaseRequired):
            require_scheduler_lease("test.direct_call")
        self.repository.enqueue(_spec("guard", value=1))
        _prepare_all(self.repository)
        claim = self.repository.claim_next(worker_id="worker-a")
        assert claim is not None
        with scheduler_execution(claim):
            require_scheduler_lease("test.worker_call")

    def test_multi_item_job_runs_each_item_and_finalizes_once(self) -> None:
        adapter = MultiAdapter()
        created = self.repository.enqueue(_multi_spec("multi", [1, 2, 3]))
        worker = ModelWorker(self.repository, "worker-multi", adapters={"test_job": adapter})

        first = worker.run_once()
        second = worker.run_once()
        final = worker.run_once()

        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "ready")
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(final["progress"], {"total_items": 3, "completed_items": 3, "failed_items": 0})
        self.assertEqual(final["result_summary"]["values"], [1, 2, 3])
        self.assertEqual(len(adapter.executed), 3)
        self.assertEqual(created.job["job_id"], final["job_id"])

    def test_parent_burst_rotates_equal_priority_jobs(self) -> None:
        os.environ["DSO_MODEL_MAX_PARENT_BURST"] = "1"
        self.addCleanup(os.environ.pop, "DSO_MODEL_MAX_PARENT_BURST", None)
        adapter = MultiAdapter()
        first = self.repository.enqueue(_multi_spec("fair-a", [1, 2, 3])).job["job_id"]
        second = self.repository.enqueue(_multi_spec("fair-b", [4, 5, 6])).job["job_id"]
        worker = ModelWorker(self.repository, "worker-fair", adapters={"test_job": adapter})

        worker.run_once()
        worker.run_once()

        self.assertEqual([job_id for job_id, _ in adapter.executed[:2]], [first, second])


@unittest.skipIf(TestClient is None, "FastAPI/TestClient dependencies are not installed")
class ModelSchedulerApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        os.environ["DSO_MODEL_SCHEDULER_ENABLED"] = "1"
        init_db()
        _insert_candidate()
        score_segment("scheduler_segment")
        from dso.api.main import app

        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)
        os.environ.pop("DSO_MODEL_SCHEDULER_ENABLED", None)

    def test_omni_endpoint_returns_202_and_status_api(self) -> None:
        response = self.client.post(
            "/videos/scheduler_video/omni-rerank",
            json={"candidate_limit": 1, "max_clip_seconds": 6, "omni_weight": 0.15},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["baseline"]["ranking_source"], "current_rules")
        self.assertEqual(payload["model_job"]["status"], "queued")
        job_id = payload["model_job"]["job_id"]
        status = self.client.get(f"/model-jobs/{job_id}")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["job_id"], job_id)
        duplicate = self.client.post(
            "/videos/scheduler_video/omni-rerank",
            json={"candidate_limit": 1, "max_clip_seconds": 6, "omni_weight": 0.15},
        ).json()
        self.assertEqual(duplicate["model_job"]["job_id"], job_id)
        self.assertTrue(duplicate["model_job"]["deduplicated"])
        scheduler = self.client.get("/model-scheduler/status").json()
        self.assertTrue(scheduler["enabled"])
        self.assertEqual(scheduler["jobs"]["queued"], 1)

    def test_extract_endpoint_returns_scheduled_asr_job(self) -> None:
        with patch(
            "dso.api.main.submit_qwen3_asr_job",
            return_value={"status": "accepted", "baseline": {"status": "missing"}, "model_job": _public_fake_job("asr_job")},
        ), patch(
            "dso.api.main.extract_audio_features",
            return_value={"peaks": [], "frames": [], "wav_path": ""},
        ):
            response = self.client.post("/videos/scheduler_video/extract", json={"force": False})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["model_job"]["job_kind"], "qwen3_asr_program")
        self.assertEqual(response.json()["asr_selected_backend"], "qwen3_asr_scheduled")

    def test_embedding_build_endpoint_returns_scheduled_job(self) -> None:
        with patch(
            "dso.api.main.submit_embedding_build_job",
            return_value={"status": "accepted", "baseline": {"status": "ready"}, "model_job": _public_fake_job("embedding_job", "text_embedding_build")},
        ):
            response = self.client.post(
                "/learning/qwen-embeddings/build",
                json={"entity_type": "candidate", "modality": "text", "limit": 1},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["model_job"]["job_kind"], "text_embedding_build")


def _spec(key: str, *, value: int) -> ModelJobSpec:
    input_hash = stable_json_hash({"key": key, "value": value})
    return ModelJobSpec(
        job_kind="test_job",
        subject_type="test",
        subject_id=key,
        account_id="main",
        resource_class="gpu:0",
        model_profile_id="fake_model.v1",
        model_id="fake/model",
        model_version="fake.v1",
        prompt_version="fake_prompt.v1",
        priority_class="interactive_product",
        base_priority=300,
        input_hash=input_hash,
        parameters_hash=stable_json_hash({"value": value}),
        dedupe_key=stable_json_hash({"dedupe": key, "value": value}),
        request_summary={"value": value},
        fallback_ref={"status": "ready", "source": "test_baseline"},
        items=(
            JobItemSpec(
                item_kind="test_item",
                item_role="test",
                input_hash=input_hash,
                request={"value": value},
            ),
        ),
    )


def _multi_spec(key: str, values: list[int]) -> ModelJobSpec:
    input_hash = stable_json_hash({"key": key, "values": values})
    return ModelJobSpec(
        job_kind="test_job",
        subject_type="test",
        subject_id=key,
        account_id="main",
        resource_class="gpu:0",
        model_profile_id="fake_model.v1",
        model_id="fake/model",
        model_version="fake.v1",
        prompt_version="fake_prompt.v1",
        priority_class="interactive_product",
        base_priority=300,
        input_hash=input_hash,
        parameters_hash=stable_json_hash({"values": values}),
        dedupe_key=stable_json_hash({"dedupe": key, "values": values}),
        request_summary={"values": values},
        fallback_ref={"status": "ready", "source": "test_baseline"},
        items=tuple(
            JobItemSpec(
                item_kind="test_item",
                item_role=f"item_{index}",
                input_hash=stable_json_hash({"key": key, "index": index, "value": value}),
                request={"value": value},
            )
            for index, value in enumerate(values)
        ),
    )


def _insert_candidate() -> None:
    now = "2026-07-18T00:00:00+00:00"
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO source_videos
            (id, account_id, title, original_path, file_path, duration_seconds, width,
             height, fps, audio_streams, status, created_at, updated_at)
            VALUES ('scheduler_video', 'main', '调度测试', '/tmp/scheduler.mp4',
                    '/tmp/scheduler.mp4', 60, 1920, 1080, 25, 1, 'transcribed', ?, ?)
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
            VALUES ('scheduler_segment', 'scheduler_video', 5, 35, 30,
                    '副歌高音爆发，全场观众欢呼', '完整音乐综艺爆点', '音乐综艺',
                    'climax_candidate', '综艺叙事爆点闭环型', '热血',
                    '节目上下文 -> 歌曲爆点 -> 现场反应', '副歌高音爆发',
                    '含节目上下文', '可讨论舞台爆发', 20, 'candidate', ?)
            """,
            [now],
        )
        connection.commit()


def _prepare_all(repository: ModelJobRepository) -> None:
    while True:
        claim = repository.claim_preparation(worker_id="test-preparer")
        if claim is None:
            return
        repository.complete_preparation(claim)


def _public_fake_job(job_id: str, job_kind: str = "qwen3_asr_program") -> dict:
    return {
        "contract_version": "model_job.v1",
        "job_id": job_id,
        "job_kind": job_kind,
        "status": "queued",
        "progress": {"total_items": 1, "completed_items": 0, "failed_items": 0},
    }


if __name__ == "__main__":
    unittest.main()
