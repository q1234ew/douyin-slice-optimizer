from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from dso.corrections.editor import update_candidate_segment
from dso.db.session import connect, init_db
from dso.precut import create_precut_batch, process_precut_batch
from dso.scoring.scorer import _title_suggestions
from dso.segments.generator import _candidate_row, generate_segments
from dso.versions import PRECUT_BATCH_VERSION, RESEARCH_RANKER_VERSION, STANDARD_CANDIDATE_VERSION


VIDEO_METADATA = {
    "duration_seconds": 30.0,
    "width": 1080,
    "height": 1920,
    "fps": 25.0,
    "audio_streams": 1,
}


class PrecutBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)

    def _video(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_batch_deduplicates_content_and_locks_full_source_boundary(self) -> None:
        first = self._video("first.mp4", b"same-video")
        second = self._video("second.mp4", b"same-video")
        with patch("dso.media.ingest.probe_video", return_value=VIDEO_METADATA):
            result = create_precut_batch([first, second], account_id="main", title="第一批")

        self.assertEqual(result["contract_version"], PRECUT_BATCH_VERSION)
        self.assertEqual(result["candidate_contract_version"], STANDARD_CANDIDATE_VERSION)
        self.assertEqual(result["summary"]["created_count"], 1)
        self.assertEqual(result["summary"]["reused_count"], 1)
        self.assertEqual(result["items"][0]["source_video_id"], result["items"][1]["source_video_id"])
        self.assertEqual(result["items"][0]["candidate_segment_id"], result["items"][1]["candidate_segment_id"])
        item = result["items"][0]
        self.assertTrue(item["boundary_invariant"])
        self.assertEqual(item["candidate_origin"], "precut")
        self.assertEqual(item["start_time"], 0)
        self.assertEqual(item["end_time"], 30)
        self.assertEqual(item["boundary_strategy"], "source_asset_full_duration")

        with connect() as conn:
            counts = conn.execute(
                "SELECT COUNT(*) AS videos, (SELECT COUNT(*) FROM candidate_segments) AS candidates FROM source_videos"
            ).fetchone()
        self.assertEqual(counts["videos"], 1)
        self.assertEqual(counts["candidates"], 1)

    def test_locked_boundary_is_protected_by_service_and_database(self) -> None:
        path = self._video("locked.mp4", b"locked-video")
        with patch("dso.media.ingest.probe_video", return_value=VIDEO_METADATA):
            result = create_precut_batch([path], account_id="main")
        segment_id = result["items"][0]["candidate_segment_id"]

        with self.assertRaisesRegex(ValueError, "boundary is immutable"):
            update_candidate_segment(segment_id, {"start_time": 1, "end_time": 29})
        with connect() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "boundary is immutable"):
                conn.execute(
                    "UPDATE candidate_segments SET end_time = 29, duration_seconds = 29 WHERE id = ?",
                    [segment_id],
                )

        corrected = update_candidate_segment(segment_id, {"summary": "人工补充语义，不改变边界"})
        self.assertEqual(corrected["start_time"], 0)
        self.assertEqual(corrected["end_time"], 30)

    def test_program_segmenter_refuses_precut_source(self) -> None:
        path = self._video("immutable.mp4", b"immutable-video")
        with patch("dso.media.ingest.probe_video", return_value=VIDEO_METADATA):
            result = create_precut_batch([path], account_id="main")
        video_id = result["items"][0]["source_video_id"]

        with self.assertRaisesRegex(ValueError, "immutable full-duration candidate"):
            generate_segments(video_id)

    def test_generated_and_precut_entries_share_standard_candidate_contract(self) -> None:
        generated = _candidate_row(
            "video-program",
            10,
            40,
            "导师评价后副歌高音爆发",
            0.8,
            generation_source="test",
        )
        self.assertEqual(generated["candidate_contract_version"], STANDARD_CANDIDATE_VERSION)
        self.assertEqual(generated["candidate_origin"], "generated")
        self.assertEqual(generated["boundary_locked"], 0)

        path = self._video("precut.mp4", b"precut-contract")
        with patch("dso.media.ingest.probe_video", return_value=VIDEO_METADATA):
            result = create_precut_batch([path], account_id="main")
        precut = result["items"][0]
        self.assertEqual(precut["candidate_contract_version"], STANDARD_CANDIDATE_VERSION)
        self.assertEqual(precut["candidate_origin"], "precut")
        self.assertEqual(precut["boundary_locked"], 1)

    def test_short_precut_title_suggestions_never_reference_out_of_range_second(self) -> None:
        titles = _title_suggestions(
            {
                "duration_seconds": 3,
                "transcript": "",
                "emotion_type": "舞台表现",
                "music_slice_type": "直入听觉爆点型",
            }
        )
        self.assertFalse(any("第 12 秒" in title for title in titles))
        self.assertTrue(any("第 1 秒" in title for title in titles))

    def test_processing_uses_shared_scorer_and_returns_batch_ranking(self) -> None:
        first = self._video("high.mp4", b"video-high")
        second = self._video("context.mp4", b"video-context")
        with patch("dso.media.ingest.probe_video", return_value=VIDEO_METADATA):
            created = create_precut_batch([first, second], account_id="main")

        transcripts = [
            {
                "source": "mock_asr",
                "segments": [{"start": 0, "end": 30, "text": "第一次改编副歌高音爆发，全场观众起立欢呼"}],
            },
            {
                "source": "mock_asr",
                "segments": [{"start": 0, "end": 30, "text": "歌手讲述一路坚持的故事，导师给出评价"}],
            },
        ]
        audio = {
            "frames": [{"time": 0, "energy": 0.8}, {"time": 1, "energy": 0.7}],
            "peaks": [],
            "wav_path": "/tmp/mock.wav",
        }
        with patch("dso.precut.transcribe_video", side_effect=transcripts), patch(
            "dso.precut.extract_audio_features", return_value=audio
        ):
            result = process_precut_batch(created["batch_id"])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["processed_count"], 2)
        self.assertEqual(result["summary"]["ranked_count"], 2)
        self.assertEqual([row["batch_rank"] for row in result["rankings"]], [1, 2])
        self.assertTrue(all(row["boundary_invariant"] for row in result["rankings"]))
        self.assertTrue(all(row["ranker_version"] == RESEARCH_RANKER_VERSION for row in result["rankings"]))
        self.assertTrue(all(row["candidate_contract_version"] == STANDARD_CANDIDATE_VERSION for row in result["rankings"]))


@unittest.skipIf(TestClient is None, "FastAPI/TestClient dependencies are not installed")
class PrecutBatchApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        init_db()
        from dso.api.main import app

        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)

    def test_batch_upload_process_and_locked_correction_contract(self) -> None:
        files = [
            ("files", ("one.mp4", b"one-video", "video/mp4")),
            ("files", ("two.mp4", b"two-video", "video/mp4")),
        ]
        with patch("dso.media.ingest.probe_video", return_value=VIDEO_METADATA):
            response = self.client.post(
                "/precut-batches",
                data={"account_id": "main", "batch_title": "API 批次", "process": "false"},
                files=files,
            )
        self.assertEqual(response.status_code, 200)
        created = response.json()
        self.assertEqual(created["summary"]["item_count"], 2)
        self.assertEqual(created["summary"]["boundary_locked_count"], 2)

        transcript = {
            "source": "mock_asr",
            "segments": [{"start": 0, "end": 30, "text": "副歌高音之后全场欢呼"}],
        }
        audio = {"frames": [{"time": 0, "energy": 0.8}], "peaks": [], "wav_path": "/tmp/mock.wav"}
        with patch("dso.precut.transcribe_video", return_value=transcript), patch(
            "dso.precut.extract_audio_features", return_value=audio
        ):
            processed = self.client.post(
                f"/precut-batches/{created['batch_id']}/process",
                json={"wait": True},
            )
        self.assertEqual(processed.status_code, 200)
        self.assertEqual(processed.json()["status"], "completed")
        self.assertEqual(len(processed.json()["rankings"]), 2)

        segment_id = created["items"][0]["candidate_segment_id"]
        correction = self.client.patch(
            f"/segments/{segment_id}/correction",
            json={"start_time": 1, "end_time": 29},
        )
        self.assertEqual(correction.status_code, 400)
        self.assertIn("boundary is immutable", correction.json()["detail"])

        split = self.client.post(f"/videos/{created['items'][0]['source_video_id']}/segments")
        self.assertEqual(split.status_code, 409)


if __name__ == "__main__":
    unittest.main()
