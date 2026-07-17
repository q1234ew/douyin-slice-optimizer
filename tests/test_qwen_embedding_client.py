from __future__ import annotations

import os
import tempfile
import unittest

from dso.db.session import connect, init_db
from dso.learning.qwen_embeddings import (
    QWEN_EMBEDDING_DIM,
    QWEN_EMBEDDING_MODEL,
    QwenEmbeddingClient,
    quarantine_invalid_qwen_embedding_records,
)


class _StubClient(QwenEmbeddingClient):
    def __init__(self, responses: dict[tuple[str, str], dict]) -> None:
        self.service_url = "mock"
        self.timeout_seconds = 1.0
        self._requests_session = None
        self.responses = responses

    def _json_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        del payload
        return self.responses[(method, path)]


class QwenEmbeddingClientTests(unittest.TestCase):
    def test_health_requires_target_model_to_be_loaded(self) -> None:
        omni = _StubClient(
            {
                ("GET", "/health"): {
                    "status": "ready",
                    "model": {
                        "loaded": True,
                        "model_id": "Qwen/Qwen2.5-Omni-7B-GPTQ-Int4",
                        "backend": "qwen_omni",
                    },
                }
            }
        )
        unloaded = _StubClient(
            {
                ("GET", "/health"): {
                    "status": "ready",
                    "model": {
                        "loaded": False,
                        "model_id": QWEN_EMBEDDING_MODEL,
                        "backend": "sentence_transformers",
                    },
                }
            }
        )
        ready = _StubClient(
            {
                ("GET", "/health"): {
                    "status": "ready",
                    "model": {
                        "loaded": True,
                        "model_id": QWEN_EMBEDDING_MODEL,
                        "backend": "sentence_transformers",
                    },
                }
            }
        )

        self.assertEqual(omni.health()["status"], "model_switch_required")
        self.assertEqual(unloaded.health()["status"], "model_not_loaded")
        self.assertEqual(ready.health()["status"], "ready")

    def test_fallback_and_wrong_dimensions_are_rejected(self) -> None:
        fallback = _StubClient(
            {
                ("POST", "/embed/text"): {
                    "status": "fallback",
                    "embedding_dim": 64,
                    "embeddings": [[0.0] * 64],
                }
            }
        )
        wrong_dimension = _StubClient(
            {
                ("POST", "/embed/text"): {
                    "status": "model",
                    "embedding_dim": 64,
                    "embeddings": [[0.0] * 64],
                }
            }
        )

        with self.assertRaisesRegex(RuntimeError, "fallback_rejected"):
            fallback.embed_text("test")
        with self.assertRaisesRegex(RuntimeError, "unexpected_dimension"):
            wrong_dimension.embed_text("test")

    def test_model_response_accepts_2048_dimensions(self) -> None:
        client = _StubClient(
            {
                ("POST", "/embed/text"): {
                    "status": "model",
                    "embedding_dim": QWEN_EMBEDDING_DIM,
                    "embeddings": [[0.0] * QWEN_EMBEDDING_DIM],
                }
            }
        )

        self.assertEqual(len(client.embed_text("test")), QWEN_EMBEDDING_DIM)

    def test_invalid_ready_records_are_quarantined(self) -> None:
        previous_root = os.environ.get("DSO_ROOT")
        with tempfile.TemporaryDirectory() as root:
            os.environ["DSO_ROOT"] = root
            try:
                init_db()
                with connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO embedding_records (
                            id, entity_type, entity_id, modality, model_name, vector_dim,
                            status, created_at, updated_at
                        ) VALUES ('bad', 'historical_sample', 'sample', 'visual', ?, 64,
                                  'ready', '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z')
                        """,
                        [QWEN_EMBEDDING_MODEL],
                    )
                    conn.commit()

                self.assertEqual(quarantine_invalid_qwen_embedding_records(), 1)
                with connect() as conn:
                    row = conn.execute("SELECT status, error FROM embedding_records WHERE id = 'bad'").fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error"], "invalid_qwen_embedding_dimension")
            finally:
                if previous_root is None:
                    os.environ.pop("DSO_ROOT", None)
                else:
                    os.environ["DSO_ROOT"] = previous_root


if __name__ == "__main__":
    unittest.main()
