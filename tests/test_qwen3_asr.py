from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from dso.features.asr import active_asr_backend
from dso.features.qwen3_asr import (
    _is_context_echo,
    _offset_segment,
    _wav_chunks,
    qwen3_asr_cache_config,
    transcribe_wav,
)


class Qwen3ASRTests(unittest.TestCase):
    def test_safe_defaults_use_short_chunks_without_context(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = qwen3_asr_cache_config()

        self.assertEqual(config["chunk_seconds"], 60.0)
        self.assertEqual(config["context"], "")
        self.assertEqual(config["boundary_search_seconds"], 5.0)
        self.assertTrue(config["retry_enabled"])

    def test_requested_backend_reports_loaded_remote_service(self) -> None:
        health = {"status": "ready", "model": {"loaded": True}}
        with patch("dso.features.asr.qwen3_asr_health", return_value=health):
            self.assertEqual(active_asr_backend("qwen3_asr"), "qwen3_asr")

    def test_wav_chunking_and_overlap_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            audio = root / "audio.wav"
            with wave.open(str(audio), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(10)
                handle.writeframes(b"\x00\x00" * 100)

            chunks = list(_wav_chunks(audio, root / "chunks", chunk_seconds=4.0, overlap_seconds=1.0))

            self.assertEqual(len(chunks), 3)
            self.assertEqual(chunks[0]["start"], 0.0)
            self.assertEqual(chunks[1]["start"], 3.0)
            self.assertEqual(chunks[2]["start"], 6.0)
            kept = _offset_segment(
                {"start": 0.6, "end": 1.0, "text": "保留"},
                chunks[1],
            )
            dropped = _offset_segment(
                {"start": 0.1, "end": 0.3, "text": "重复"},
                chunks[1],
            )
            self.assertEqual(kept["start"], 3.6)
            self.assertIsNone(dropped)

    def test_chunk_boundary_moves_to_low_energy_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            audio = root / "audio.wav"
            frames = []
            for index in range(1200):
                value = 0 if 570 <= index < 600 else 10000
                frames.append(int(value).to_bytes(2, byteorder="little", signed=True))
            with wave.open(str(audio), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(100)
                handle.writeframes(b"".join(frames))

            chunks = list(
                _wav_chunks(
                    audio,
                    root / "chunks",
                    chunk_seconds=6.0,
                    overlap_seconds=1.0,
                    boundary_search_seconds=1.0,
                )
            )

            self.assertGreaterEqual(chunks[0]["end"], 5.7)
            self.assertLess(chunks[0]["end"], 6.0)
            self.assertAlmostEqual(chunks[1]["start"], chunks[0]["end"] - 1.0, places=2)

    def test_context_echo_detection_requires_near_full_prompt_replay(self) -> None:
        context = "歌手2025，陈楚生，张韶涵，范玮琪，竞演，听审，补位歌手。"
        self.assertTrue(_is_context_echo("歌手2025 陈楚生 张韶涵 范玮琪 竞演 听审 补位歌手", context))
        self.assertFalse(_is_context_echo("掌声送给今晚精彩的八位歌手", context))

    def test_empty_active_chunk_is_recovered_with_smaller_windows(self) -> None:
        config = self._config(chunk_seconds=40.0, retry_chunk_seconds=20.0)
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            audio = self._write_active_audio(root / "audio.wav", duration_seconds=40)

            def upload(path: Path, **_kwargs) -> dict:
                if path.parent.name.startswith("retry-"):
                    text = "这是缩短窗口后恢复的完整关键口播内容"
                    return {
                        "status": "ready",
                        "language": "Chinese",
                        "text": text,
                        "segments": [{"start": 1.0, "end": 3.0, "text": text}],
                        "elapsed_seconds": 1.0,
                    }
                return {
                    "status": "empty",
                    "language": "Chinese",
                    "text": "",
                    "segments": [],
                    "elapsed_seconds": 2.0,
                }

            health = {"status": "ready", "model": {"loaded": True, "model_id": "Qwen3-ASR-test"}}
            with (
                patch("dso.features.qwen3_asr.qwen3_asr_health", return_value=health),
                patch("dso.features.qwen3_asr.qwen3_asr_cache_config", return_value=config),
                patch("dso.features.qwen3_asr._upload_audio", side_effect=upload),
            ):
                segments, metadata = transcribe_wav(audio, root / "work")

        self.assertEqual(len(segments), 2)
        self.assertEqual(metadata["quality_status"], "ready")
        self.assertEqual(metadata["recovered_chunk_count"], 1)
        chunk = metadata["chunks"][0]
        self.assertEqual(chunk["selected_strategy"], "split_retry")
        self.assertEqual(chunk["quality_status"], "recovered")
        self.assertIn("empty_active_audio", chunk["recovery_reasons"])
        self.assertEqual(chunk["attempt_count"], 3)

    def test_slow_sparse_chunk_is_recovered_with_smaller_windows(self) -> None:
        config = self._config(chunk_seconds=40.0, retry_chunk_seconds=20.0)
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            audio = self._write_active_audio(root / "audio.wav", duration_seconds=40)

            def upload(path: Path, **_kwargs) -> dict:
                if path.parent.name.startswith("retry-"):
                    text = "缩短窗口以后识别出了原先被歌词覆盖的关键主持人口播"
                    elapsed = 1.0
                else:
                    text = "继续加油飞"
                    elapsed = 20.0
                return {
                    "status": "ready",
                    "language": "Chinese",
                    "text": text,
                    "segments": [{"start": 1.0, "end": 3.0, "text": text}],
                    "elapsed_seconds": elapsed,
                }

            health = {"status": "ready", "model": {"loaded": True, "model_id": "Qwen3-ASR-test"}}
            with (
                patch("dso.features.qwen3_asr.qwen3_asr_health", return_value=health),
                patch("dso.features.qwen3_asr.qwen3_asr_cache_config", return_value=config),
                patch("dso.features.qwen3_asr._upload_audio", side_effect=upload),
            ):
                segments, metadata = transcribe_wav(audio, root / "work")

        self.assertEqual(len(segments), 2)
        self.assertTrue(all("关键主持人口播" in segment["text"] for segment in segments))
        chunk = metadata["chunks"][0]
        self.assertEqual(chunk["quality_status"], "recovered")
        self.assertIn("sparse_active_audio", chunk["recovery_reasons"])

    def test_context_echo_retries_same_chunk_without_context(self) -> None:
        context = "歌手2025，陈楚生，张韶涵，范玮琪，竞演，听审，补位歌手。"
        config = self._config(chunk_seconds=20.0, retry_chunk_seconds=20.0, context=context)
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            audio = self._write_active_audio(root / "audio.wav", duration_seconds=20)

            def upload(_path: Path, *, context: str, **_kwargs) -> dict:
                text = context or "来掌声送给今晚精彩的八位歌手现在开始投票"
                return {
                    "status": "ready",
                    "language": "Chinese",
                    "text": text,
                    "segments": [{"start": 1.0, "end": 5.0, "text": text}],
                    "elapsed_seconds": 1.0,
                }

            health = {"status": "ready", "model": {"loaded": True, "model_id": "Qwen3-ASR-test"}}
            with (
                patch("dso.features.qwen3_asr.qwen3_asr_health", return_value=health),
                patch("dso.features.qwen3_asr.qwen3_asr_cache_config", return_value=config),
                patch("dso.features.qwen3_asr._upload_audio", side_effect=upload),
            ):
                segments, metadata = transcribe_wav(audio, root / "work")

        self.assertIn("开始投票", segments[0]["text"])
        chunk = metadata["chunks"][0]
        self.assertEqual(chunk["selected_strategy"], "no_context_retry")
        self.assertEqual(chunk["quality_status"], "recovered")
        self.assertEqual(chunk["attempt_count"], 2)
        self.assertIn("context_echo", chunk["recovery_reasons"])

    @staticmethod
    def _write_active_audio(path: Path, *, duration_seconds: int) -> Path:
        rate = 100
        sample = int(8000).to_bytes(2, byteorder="little", signed=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(sample * rate * duration_seconds)
        return path

    @staticmethod
    def _config(**overrides) -> dict:
        config = {
            "service_url": "http://test.invalid:8002",
            "model": "Qwen3-ASR-test",
            "language": "Chinese",
            "context": "",
            "chunk_seconds": 60.0,
            "overlap_seconds": 0.0,
            "boundary_search_seconds": 0.0,
            "retry_enabled": True,
            "retry_chunk_seconds": 30.0,
            "retry_min_rms": 0.002,
            "retry_min_text_density": 0.5,
            "retry_slow_seconds": 12.0,
            "timestamps": True,
        }
        config.update(overrides)
        return config


if __name__ == "__main__":
    unittest.main()
