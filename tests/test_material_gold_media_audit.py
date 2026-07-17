from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import audit_material_gold_media as media_audit  # noqa: E402


class MaterialGoldMediaAuditTests(unittest.TestCase):
    def test_missing_audio_is_not_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            video = Path(temp_value) / "sample.mp4"
            video.touch()
            with patch.object(
                media_audit,
                "probe_video",
                return_value={"duration_seconds": 100.0, "audio_streams": 0},
            ):
                result = media_audit._audit_row(
                    self._row(),
                    {"video": [str(video)]},
                    "reaction_compilation",
                    duration_tolerance=0.15,
                )

        self.assertEqual(result["media_readiness"], "not_evaluable")
        self.assertIn("audio_missing", result["exclusion_reasons"])

    def test_matching_external_audio_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            video = Path(temp_value) / "sample.mp4"
            audio = Path(temp_value) / "sample.wav"
            video.touch()
            audio.touch()

            def fake_probe(path: Path) -> dict:
                if Path(path).suffix == ".wav":
                    return {"duration_seconds": 99.0, "audio_streams": 1}
                return {"duration_seconds": 100.0, "audio_streams": 0}

            with patch.object(media_audit, "probe_video", side_effect=fake_probe):
                result = media_audit._audit_row(
                    self._row(),
                    {"video": [str(video)], "audio": [str(audio)]},
                    "reaction_compilation",
                    duration_tolerance=0.15,
                )

        self.assertEqual(result["media_readiness"], "ready_for_selector")
        self.assertEqual(result["audio_source"], "external_audio")
        self.assertEqual(result["exclusion_reasons"], [])

    @staticmethod
    def _row() -> dict:
        return {
            "sample_id": "sample_1",
            "account_id": "account_1",
            "dataset_id": "dataset_1",
            "platform_item_id": "item_1",
            "title": "test",
            "expected_duration_seconds": 100.0,
            "gold_material_type": "compilation",
            "program_context": "unknown",
            "presentation_style": "listicle",
        }


if __name__ == "__main__":
    unittest.main()
