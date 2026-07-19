from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dso.features.asr import _cached_transcript, _preserve_previous_transcript, _try_configured_asr
from dso.features.asr_routing import qwen3_asr_shadow_policy, route_video_asr
from dso.features.asr_shadow import qwen3_asr_shadow_status, run_qwen3_asr_shadow


class Qwen3ASRShadowTests(unittest.TestCase):
    def test_program_route_prefers_qwen_primary_with_whisper_fallback(self) -> None:
        route = route_video_asr(
            {"input_mode": "program", "status": "ingested"},
            transcript_summary={"source": "missing", "segment_count": 0},
        )

        primary = route["primary"]
        self.assertTrue(primary["eligible"])
        self.assertEqual(primary["status"], "primary")
        self.assertEqual(primary["preferred_backend"], "qwen3_asr")
        self.assertEqual(primary["fallback_backend"], "whisper_cpp")
        self.assertTrue(primary["replace_whisper_when_ready"])
        self.assertTrue(primary["fallback_on_unavailable_or_failure"])
        self.assertEqual(route["shadow"]["status"], "available")

    def test_precut_input_is_not_eligible_for_full_program_shadow(self) -> None:
        policy = qwen3_asr_shadow_policy({"input_mode": "precut"})

        self.assertFalse(policy["eligible"])
        self.assertEqual(policy["status"], "not_program_input")

    def test_shadow_run_writes_separate_cached_artifact_and_preserves_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            video_id = "video_shadow"
            transcript_root = cache / video_id / "transcript"
            transcript_root.mkdir(parents=True)
            audio = transcript_root / "audio.wav"
            audio.write_bytes(b"frozen shadow audio")
            baseline_path = transcript_root / "transcript.json"
            baseline_payload = {
                "video_id": video_id,
                "source": "whisper_cpp:base",
                "segments": [{"start": 0, "end": 1, "text": "Whisper 主转写"}],
            }
            baseline_path.write_text(json.dumps(baseline_payload, ensure_ascii=False), encoding="utf-8")
            video = {
                "id": video_id,
                "input_mode": "program",
                "file_path": str(root / "program.mp4"),
                "transcript_path": str(baseline_path),
            }
            qwen_result = {
                "source": "qwen3_asr:Qwen3-ASR-1.7B",
                "segments": [{"start": 0, "end": 1, "text": "Qwen 影子转写"}],
                "metadata": {"backend": "qwen3_asr", "segment_count_raw": 8},
            }
            settings = SimpleNamespace(cache_dir=cache)
            health = {"status": "ready", "model": {"loaded": True, "model_id": "Qwen/Qwen3-ASR-1.7B"}}

            with patch("dso.features.asr_shadow.ensure_data_dirs", return_value=settings), patch(
                "dso.features.asr_shadow.get_video", return_value=video
            ), patch("dso.features.asr_shadow.qwen3_asr_health", return_value=health), patch(
                "dso.features.asr_shadow.transcribe_audio_file", return_value=qwen_result
            ) as transcribe:
                first = run_qwen3_asr_shadow(video_id)
                second = run_qwen3_asr_shadow(video_id)
                status = qwen3_asr_shadow_status(video_id)

            shadow_path = transcript_root / "shadow" / "qwen3_asr" / "transcript.json"
            shadow_payload = json.loads(shadow_path.read_text(encoding="utf-8"))
            self.assertEqual(first["status"], "ready")
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(transcribe.call_count, 1)
            self.assertEqual(shadow_payload["role"], "shadow")
            self.assertEqual(shadow_payload["segments"][0]["text"], "Qwen 影子转写")
            self.assertFalse(shadow_payload["metadata"]["auto_promote"])
            self.assertTrue(status["artifact"]["available"])
            self.assertEqual(
                json.loads(baseline_path.read_text(encoding="utf-8")),
                baseline_payload,
            )

    def test_unloaded_service_waits_without_replacing_whisper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            video_id = "video_waiting"
            transcript_root = cache / video_id / "transcript"
            transcript_root.mkdir(parents=True)
            (transcript_root / "audio.wav").write_bytes(b"audio")
            baseline_path = transcript_root / "transcript.json"
            baseline_path.write_text(
                json.dumps({"source": "whisper_cpp:small", "segments": [{"text": "保留"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            video = {
                "id": video_id,
                "input_mode": "program",
                "file_path": str(root / "program.mp4"),
                "transcript_path": str(baseline_path),
            }
            settings = SimpleNamespace(cache_dir=cache)
            health = {"status": "available", "model": {"loaded": False, "last_error": ""}}

            with patch("dso.features.asr_shadow.ensure_data_dirs", return_value=settings), patch(
                "dso.features.asr_shadow.get_video", return_value=video
            ), patch("dso.features.asr_shadow.qwen3_asr_health", return_value=health), patch(
                "dso.features.asr_shadow.transcribe_audio_file"
            ) as transcribe:
                result = run_qwen3_asr_shadow(video_id)

            self.assertEqual(result["status"], "waiting_model_switch")
            self.assertTrue(result["active_transcript_preserved"])
            self.assertEqual(result["baseline"]["source"], "whisper_cpp:small")
            transcribe.assert_not_called()
            self.assertFalse((transcript_root / "shadow" / "qwen3_asr" / "transcript.json").exists())

    def test_preferred_backend_falls_back_to_whisper_when_qwen_fails(self) -> None:
        qwen_failure = ([], "qwen3_asr_failed:Qwen3ASRError")
        whisper_success = ([{"start": 0, "end": 1, "text": "Whisper 回退"}], "whisper_cpp:base")

        with patch("dso.features.asr._try_qwen3_asr", return_value=qwen_failure) as qwen, patch(
            "dso.features.asr.whisper_cpp_ready", return_value=True
        ), patch("dso.features.asr._try_whisper_cpp", return_value=whisper_success) as whisper:
            segments, source = _try_configured_asr(
                Path("/tmp/audio.wav"),
                Path("/tmp/transcript"),
                "base",
                backend="qwen3_asr_preferred",
            )

        qwen.assert_called_once()
        whisper.assert_called_once()
        self.assertEqual(source, "whisper_cpp:base")
        self.assertEqual(segments[0]["text"], "Whisper 回退")

    def test_cached_qwen_primary_is_preserved_when_runtime_is_temporarily_unloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "transcript.json"
            stored_key = {
                "audio_sha256": "same",
                "backend_preference": "qwen3_asr_preferred",
                "active_backend": "qwen3_asr",
                "qwen3_asr": {"model": "Qwen/Qwen3-ASR-1.7B"},
            }
            current_key = {
                **stored_key,
                "active_backend": "whisper_cpp_fallback",
            }
            path.write_text(
                json.dumps(
                    {
                        "source": "qwen3_asr:Qwen3-ASR-1.7B",
                        "segments": [{"text": "保留 Qwen 主转写"}],
                        "metadata": {"cache_key": stored_key},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            cached = _cached_transcript(path, current_key, force=False)

            self.assertIsNotNone(cached)
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(cached["segments"][0]["text"], "保留 Qwen 主转写")

    def test_failed_primary_and_fallback_preserve_same_audio_transcript(self) -> None:
        previous = {
            "source": "whisper_cpp:small",
            "segments": [{"start": 0, "end": 1, "text": "保留旧转写"}],
            "metadata": {"cache_key": {"audio_sha256": "same", "active_backend": "whisper_cpp"}},
        }
        cache_key = {
            "audio_sha256": "same",
            "backend_preference": "qwen3_asr_preferred",
            "active_backend": "qwen3_asr",
        }

        preserved = _preserve_previous_transcript(
            previous,
            cache_key=cache_key,
            failed_source="missing_faster_whisper",
            routing={"primary": {"preferred_backend": "qwen3_asr"}},
        )

        self.assertIsNotNone(preserved)
        self.assertEqual(preserved["segments"][0]["text"], "保留旧转写")
        self.assertTrue(preserved["metadata"]["stale_fallback"])
        self.assertTrue(preserved["metadata"]["routing"]["fallback_used"])
        self.assertTrue(preserved["metadata"]["routing"]["preserved_previous_transcript"])
        self.assertEqual(preserved["metadata"]["failed_attempt"]["source"], "missing_faster_whisper")

        changed_audio = _preserve_previous_transcript(
            previous,
            cache_key={**cache_key, "audio_sha256": "changed"},
            failed_source="missing_faster_whisper",
            routing={},
        )
        self.assertIsNone(changed_audio)


if __name__ == "__main__":
    unittest.main()
