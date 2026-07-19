from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from dso.api.main import app
from dso.db.session import connect, init_db, insert_row
from dso.learning.multimodal_vector_value import (
    freeze_multimodal_vector_experiment,
    load_multimodal_vector_manifest,
    multimodal_vector_embedding_request,
    multimodal_vector_experiment_status,
    multimodal_vector_media_path,
    run_multimodal_vector_comparison,
    save_multimodal_vector_review,
    verify_multimodal_vector_manifest,
)
from dso.learning.bailian_vector_chain import (
    _outcome_proxy_comparison,
    run_bailian_vector_chain,
)
from dso.learning.bailian_cached_ablation import (
    _diverse_top_configurations,
    run_bailian_cached_ablation,
)
from dso.learning.qwen_embeddings import build_qwen_embedding_index
from dso.providers.aliyun_bailian import (
    BAILIAN_EMBEDDING_MODEL,
    BAILIAN_RERANK_MODEL,
)
from dso.providers.budget import Money
from dso.providers.contracts import (
    ProviderDataPermissionRecord,
    ProviderDescriptor,
    ProviderLifecycleStatus,
    ProviderModelRef,
)


class _FakeEmbeddingClient:
    def health(self) -> dict:
        return {"status": "ready", "service_url": "mock"}

    def load(self) -> dict:
        return self.health()

    def embed_text(self, text: str) -> list[float]:
        return _unit_vector(0 if "high" in text else 1 if "low" in text else 2)

    def embed_image(self, image_path: Path) -> list[float]:
        return _unit_vector(3 if "high" in image_path.name else 4)

    def embed_video_frames(self, frame_paths: list[Path]) -> list[float]:
        return self.embed_image(frame_paths[0])


class _FakeCloudProvider:
    def __init__(self, model_id: str) -> None:
        request_type = (
            "multimodal_embedding"
            if model_id == BAILIAN_EMBEDDING_MODEL
            else "multimodal_rerank"
            if model_id == BAILIAN_RERANK_MODEL
            else "pairwise_judge"
        )
        self.descriptor = ProviderDescriptor(
            identity=ProviderModelRef(
                provider_id="aliyun_bailian",
                model_id=model_id,
                api_version="fake-api.v1",
                prompt_version="fake-prompt.v1",
            ),
            lifecycle_status=ProviderLifecycleStatus.VALIDATE,
            request_types=(request_type,),
        )

    def estimate_max_cost(self, request) -> Money:
        from decimal import Decimal

        return Money(Decimal("0"), "CNY")


class _FakeCloudRunner:
    def execute(self, request, **kwargs):
        if request.request_type == "multimodal_embedding":
            summary = str(request.payload.get("summary") or "")
            index = 0 if "high" in summary else 1 if "low" in summary else 2
            output = {
                "embeddings": [
                    {
                        "index": 0,
                        "type": "fusion" if request.parameters.get("enable_fusion") else "vl",
                        "embedding": _unit_vector(index, dim=2560),
                    }
                ]
            }
        elif request.request_type == "multimodal_rerank":
            output = {
                "results": [
                    {
                        "index": index,
                        "sample_id": document["sample_id"],
                        "relevance_score": 0.9 if "high" in document["text"] else 0.1,
                    }
                    for index, document in enumerate(request.payload["documents"])
                ]
            }
        else:
            output = {
                "choice": "left",
                "confidence": 0.8,
                "reasons": ["synthetic"],
                "risk_flags": [],
            }
        return SimpleNamespace(
            status="shadow_succeeded",
            provider_output=output,
            policy_code="allowed",
            estimated_cost="0",
            network_request_count=1,
            cache_hit=False,
        )


def _fake_cloud_runtime_builder(*, batch_id: str, model_id: str):
    return SimpleNamespace(
        provider=_FakeCloudProvider(model_id),
        runner=_FakeCloudRunner(),
        data_permission=ProviderDataPermissionRecord(),
        batch_id=batch_id,
    )


def _unexpected_cloud_runtime_builder(*, batch_id: str, model_id: str):
    raise AssertionError(f"cloud runtime must not be built: {batch_id}/{model_id}")


class MultimodalVectorValueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)

    def test_cached_ablation_summary_prioritizes_component_diversity(self) -> None:
        rows = [
            {"strategy": "fusion-a", "component": "fusion"},
            {"strategy": "fusion-b", "component": "fusion"},
            {"strategy": "text-a", "component": "text"},
            {"strategy": "rerank-a", "component": "rerank"},
        ]
        selected = _diverse_top_configurations(rows, limit=3)
        self.assertEqual(
            [row["strategy"] for row in selected],
            ["fusion-a", "text-a", "rerank-a"],
        )

    def test_outcome_gate_uses_balanced_accuracy_when_pair_orientation_is_skewed(self) -> None:
        pair_results = []
        for index in range(20):
            outcome = "left" if index < 15 else "right"
            cloud_match = index < 6 or index >= 15
            v24_match = index < 12 or 15 <= index < 18
            pair_results.append(
                {
                    "outcome_proxy_status": "comparable",
                    "outcome_proxy_choice": outcome,
                    "cloud_matches_outcome_proxy": cloud_match,
                    "v2_4_matches_outcome_proxy": v24_match,
                }
            )

        result = _outcome_proxy_comparison(pair_results)

        self.assertEqual(result["outcome_choice_distribution"], {"left": 15, "right": 5})
        self.assertEqual(result["cloud_pairwise_accuracy"], 0.55)
        self.assertEqual(result["v2_4_pairwise_accuracy"], 0.75)
        self.assertEqual(result["cloud_balanced_pairwise_accuracy"], 0.7)
        self.assertEqual(result["v2_4_balanced_pairwise_accuracy"], 0.7)
        self.assertEqual(result["accuracy_delta_vs_v2_4"], 0.0)
        self.assertEqual(result["raw_accuracy_delta_vs_v2_4"], -0.2)
        self.assertFalse(result["early_stop"])
        self.assertEqual(result["decision"], "continue_to_minimum_pair_count")

    def test_frozen_pairwise_experiment_is_targeted_blind_and_research_only(self) -> None:
        rows = [
            ("gold_high", "7000000000000000001", "high", 96, "account_a"),
            ("gold_low", "7000000000000000002", "low", 12, "account_b"),
            ("control_low", "7000000000000000003", "low", 18, "account_a"),
            ("control_high", "7000000000000000004", "high", 91, "account_b"),
            ("reference_high", "7000000000000000005", "high", 89, "account_c"),
            ("reference_low", "7000000000000000006", "low", 14, "account_c"),
            ("unselected_mid", "7000000000000000007", "mid", 50, "account_d"),
        ]
        for sample_id, item_id, label, reward, account in rows:
            self._insert_sample(sample_id, item_id, label, reward, account)
        self._insert_gold("gold_high", "account_a")
        self._insert_gold("gold_low", "account_b")

        frozen = freeze_multimodal_vector_experiment(
            "vector-value-test-r1",
            pair_count=2,
            reference_per_label=1,
        )
        manifest = load_multimodal_vector_manifest("vector-value-test-r1")

        self.assertEqual(frozen["counts"]["task_count"], 2)
        self.assertEqual(frozen["counts"]["evaluation_sample_count"], 4)
        self.assertEqual(frozen["counts"]["reference_sample_count"], 2)
        self.assertFalse(set(manifest["evaluation_sample_ids"]) & set(manifest["reference_sample_ids"]))
        self.assertTrue(verify_multimodal_vector_manifest("vector-value-test-r1", deep=True)["passed"])

        status = multimodal_vector_experiment_status("vector-value-test-r1")
        current_task = status["current_task"]
        self.assertNotIn("sample_id", current_task["left"])
        self.assertNotIn("performance_label", current_task["left"])
        self.assertTrue(current_task["labels_hidden"])
        self.assertTrue(multimodal_vector_media_path("vector-value-test-r1", current_task["task_id"], "left").is_file())
        client = TestClient(app)
        api_status = client.get(
            "/learning/multimodal-vector-experiment/status?benchmark_id=vector-value-test-r1"
        )
        self.assertEqual(api_status.status_code, 200)
        self.assertTrue(api_status.json()["current_task"]["labels_hidden"])
        cloud_status = client.get(
            "/learning/multimodal-vector-experiment/cloud/status?benchmark_id=vector-value-test-r1"
        )
        self.assertEqual(cloud_status.status_code, 200)
        self.assertEqual(cloud_status.json()["admission_status"], "research_only")
        self.assertEqual(cloud_status.json()["embedding_coverage"]["sample_count"], 6)
        self.assertEqual(cloud_status.json()["outcome_proxy_comparison"], {})
        self.assertFalse(cloud_status.json()["production_weight_changed"])
        media = client.get(
            f"/learning/multimodal-vector-experiment/media/vector-value-test-r1/{current_task['task_id']}/left"
        )
        self.assertEqual(media.status_code, 200)
        self.assertTrue(media.content.startswith(b"video:"))

        request = multimodal_vector_embedding_request("vector-value-test-r1")
        self.assertEqual(len(request["entity_ids"]), 6)
        build = build_qwen_embedding_index(
            entity_type="historical_sample",
            entity_ids=request["entity_ids"],
            modality="all",
            limit=20,
            client=_FakeEmbeddingClient(),
        )
        self.assertEqual(build["sample_count"], 6)
        self.assertEqual(build["created"], 12)
        with connect() as conn:
            unselected = conn.execute(
                "SELECT COUNT(*) AS count FROM embedding_records WHERE entity_id = 'unselected_mid'"
            ).fetchone()["count"]
        self.assertEqual(unselected, 0)

        for index, task in enumerate(manifest["tasks"]):
            saved = save_multimodal_vector_review(
                "vector-value-test-r1",
                task["task_id"],
                {
                    "choice": "left" if index == 0 else "right",
                    "confidence": "high",
                    "reason_tags": ["hook_clarity", "not_allowed"],
                },
            )
            self.assertEqual(saved["reason_tags"], ["hook_clarity"])
            self.assertFalse(saved["production_weight"])

        report = run_multimodal_vector_comparison("vector-value-test-r1")
        self.assertEqual(report["status"], "needs_blind_review")
        self.assertEqual(report["admission_status"], "research_only")
        self.assertIn("research_ranker_v2_4", report["strategy_comparison"])
        self.assertIn("ranker_plus_text_visual_embedding", report["strategy_comparison"])
        self.assertFalse(report["promotion_gate"]["passed"])
        cloud = run_bailian_vector_chain(
            "vector-value-test-r1",
            stage="full",
            limit=0,
            top_n=2,
            judge_limit=2,
            runtime_builder=_fake_cloud_runtime_builder,
        )
        self.assertEqual(cloud["status"], "completed")
        self.assertEqual(cloud["results"]["embeddings"]["created"], 12)
        self.assertEqual(cloud["results"]["rerank"]["completed_count"], 4)
        self.assertEqual(cloud["results"]["rerank"]["baseline_comparison"]["status"], "ready")
        self.assertEqual(cloud["results"]["rerank"]["reference_pool"]["high_count"], 1)
        self.assertEqual(cloud["results"]["rerank"]["reference_pool"]["low_count"], 1)
        outcome_proxy = cloud["results"]["rerank"]["outcome_proxy_comparison"]
        self.assertEqual(outcome_proxy["evaluable_pair_count"], 2)
        self.assertEqual(outcome_proxy["status"], "insufficient_sample")
        self.assertFalse(outcome_proxy["passed"])
        self.assertFalse(outcome_proxy["views_available"])
        self.assertFalse(outcome_proxy["automatic_promotion"])
        self.assertTrue(
            all(
                item["outcome_proxy_choice"] in {"left", "right", "tie"}
                and item["cloud_outcome_choice"] in {"left", "right", "tie"}
                and isinstance(item["cloud_matches_outcome_proxy"], bool)
                and isinstance(item["v2_4_matches_outcome_proxy"], bool)
                for item in cloud["results"]["rerank"]["pair_results"]
            )
        )
        self.assertEqual(
            cloud["results"]["judge"]["selected_count"],
            cloud["results"]["rerank"]["baseline_comparison"]["choice_disagreement_count"],
        )
        if cloud["results"]["judge"]["selected_count"]:
            self.assertTrue(cloud["results"]["judge"]["blind_to_ranker_choices"])
            self.assertEqual(
                cloud["results"]["judge"]["judge_input_version"],
                "dso-bailian-pairwise-input.v2",
            )
        self.assertFalse(cloud["production_weight_changed"])

        ablation = run_bailian_cached_ablation("vector-value-test-r1")
        self.assertEqual(ablation["status"], "insufficient_cache")
        self.assertEqual(ablation["admission_status"], "research_only")
        self.assertTrue(ablation["cache_policy"]["cache_only"])
        self.assertFalse(ablation["cache_policy"]["network_runtime_constructed"])
        self.assertEqual(ablation["cache_policy"]["network_request_count"], 0)
        self.assertEqual(ablation["cache_policy"]["effective_cost_cny"], "0")
        self.assertEqual(ablation["best_incremental_configuration"]["evaluable_pair_count"], 2)
        self.assertFalse(ablation["expansion_gate"]["passed"])
        self.assertFalse(ablation["production_weight_changed"])
        ablation_api = client.post(
            "/learning/multimodal-vector-experiment/cloud/ablation",
            json={"benchmark_id": "vector-value-test-r1"},
        )
        self.assertEqual(ablation_api.status_code, 200)
        self.assertEqual(ablation_api.json()["cache_policy"]["network_request_count"], 0)
        cloud_status_after_ablation = client.get(
            "/learning/multimodal-vector-experiment/cloud/status?benchmark_id=vector-value-test-r1"
        )
        self.assertTrue(cloud_status_after_ablation.json()["reports"]["ablation"])
        self.assertEqual(
            cloud_status_after_ablation.json()["cached_ablation"]["status"],
            "insufficient_cache",
        )

        report_path = (
            self.root
            / "outputs"
            / "multimodal_vector_value"
            / "vector-value-test-r1"
            / "latest.json"
        )
        sidecar_path = self.root / "benchmarks" / "vector-value-test-r1.baseline.json"
        sidecar_path.write_bytes(report_path.read_bytes())
        report_path.unlink()
        sidecar_rerank = run_bailian_vector_chain(
            "vector-value-test-r1",
            stage="rerank",
            limit=2,
            top_n=2,
            runtime_builder=_fake_cloud_runtime_builder,
        )
        self.assertEqual(
            sidecar_rerank["results"]["rerank"]["baseline_comparison"]["source"],
            "frozen_sidecar",
        )
        self.assertEqual(
            sidecar_rerank["results"]["rerank"]["baseline_comparison"]["status"],
            "ready",
        )
        self.assertEqual(len(sidecar_rerank["results"]["rerank"]["pair_results"]), 1)

        sidecar_path.unlink()
        missing_rerank = run_bailian_vector_chain(
            "vector-value-test-r1",
            stage="rerank",
            limit=0,
            top_n=2,
            runtime_builder=_fake_cloud_runtime_builder,
        )
        self.assertEqual(
            missing_rerank["results"]["rerank"]["baseline_comparison"]["status"],
            "missing",
        )
        self.assertEqual(
            missing_rerank["results"]["rerank"]["outcome_proxy_comparison"]["evaluable_pair_count"],
            0,
        )
        self.assertEqual(missing_rerank["results"]["rerank"]["disagreement_queue"], [])
        missing_judge = run_bailian_vector_chain(
            "vector-value-test-r1",
            stage="judge",
            judge_limit=2,
            runtime_builder=_unexpected_cloud_runtime_builder,
        )
        self.assertEqual(missing_judge["results"]["judge"]["status"], "not_ready")
        self.assertEqual(
            missing_judge["results"]["judge"]["not_ready_reason"], "baseline_missing"
        )
        self.assertEqual(missing_judge["results"]["judge"]["selected_count"], 0)
        with connect() as conn:
            gold_count = conn.execute("SELECT COUNT(*) AS count FROM material_gold_annotations").fetchone()["count"]
        self.assertEqual(gold_count, 2)

    def _insert_sample(self, sample_id: str, item_id: str, label: str, reward: float, account: str) -> None:
        now = "2026-07-19T00:00:00+00:00"
        with connect() as conn:
            insert_row(
                conn,
                "historical_capture_samples",
                {
                    "id": sample_id,
                    "account_id": account,
                    "dataset_id": "vector_test",
                    "platform_item_id": item_id,
                    "sample_key": item_id,
                    "title": f"{label} unique sample {sample_id}",
                    "reward_proxy": reward,
                    "normalized_reward": reward,
                    "performance_label": label,
                    "duration_seconds": 45 if "high" in sample_id else 48,
                    "content_category": "performance_highlight",
                    "hook_type": "live_stage",
                    "slice_structure": "setup_to_payoff",
                    "artist_names": "test_artist",
                    "song_title": f"song_{sample_id}",
                    "tags": label,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.commit()
        asset_root = self.root / "data" / "douyin_media_assets" / account / "vector_test"
        video = asset_root / "videos" / f"{item_id}.mp4"
        cover = asset_root / "covers" / f"{item_id}_{label}.jpg"
        video.parent.mkdir(parents=True, exist_ok=True)
        cover.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(f"video:{sample_id}".encode("utf-8"))
        cover.write_bytes(bytes.fromhex("ffd8ffc0000b080001000101011100ffd9"))

    def _insert_gold(self, sample_id: str, account: str) -> None:
        now = "2026-07-19T00:00:00+00:00"
        with connect() as conn:
            insert_row(
                conn,
                "material_gold_annotations",
                {
                    "id": f"gold_{sample_id}",
                    "sample_id": sample_id,
                    "account_id": account,
                    "dataset_id": "vector_test",
                    "domain_category": "music_variety",
                    "material_type": "performance_clip",
                    "program_context": "program_clip",
                    "presentation_style": "stage_performance",
                    "review_status": "confirmed",
                    "operator": "tester",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.commit()


def _unit_vector(index: int, *, dim: int = 2048) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


if __name__ == "__main__":
    unittest.main()
