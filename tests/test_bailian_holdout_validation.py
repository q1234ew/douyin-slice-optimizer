from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dso.api.main import app
from dso.learning.bailian_failure_attribution import (
    run_bailian_holdout_failure_attribution,
)
from dso.learning.bailian_evidence_quality import (
    _build_or_reuse_evidence_pack,
    _reference_coverage_plan,
    _stratified_retrieval_profiles,
    _window_plan,
    run_bailian_evidence_quality_reconstruction,
)
from dso.learning.bailian_holdout_validation import (
    HARD_BATCH_CAP_CNY,
    _assert_blind_payload,
    evaluate_bailian_holdout_validation,
    freeze_bailian_holdout_validation,
)
from dso.providers.contracts import stable_json_sha256


class BailianHoldoutValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DSO_ROOT"] = self.tmp.name
        self.manifest, self.baseline, self.ablation, self.rerank, self.vectors = (
            _synthetic_frozen_inputs()
        )

    def tearDown(self) -> None:
        os.environ.pop("DSO_ROOT", None)
        self.tmp.cleanup()

    def test_freeze_is_blind_immutable_and_uses_fixed_10_cny_cap(self) -> None:
        persisted = {}

        def load_stage(_benchmark_id: str, stage: str):
            return {"ablation": self.ablation, "rerank": self.rerank}.get(stage)

        with (
            patch(
                "dso.learning.bailian_holdout_validation.load_multimodal_vector_manifest",
                return_value=self.manifest,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._local_vector_report",
                return_value=(self.baseline, "frozen_sidecar"),
            ),
            patch(
                "dso.learning.bailian_holdout_validation._load_stage_report",
                side_effect=load_stage,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._cloud_records",
                return_value={},
            ),
            patch(
                "dso.learning.bailian_holdout_validation._vectors_for_modality",
                return_value=self.vectors,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._persist_stage_report",
                side_effect=lambda _manifest, stage, report: persisted.setdefault(stage, report),
            ),
        ):
            result = freeze_bailian_holdout_validation("holdout-test-r1")

        self.assertEqual(result["split_policy"]["calibration_pair_count"], 40)
        self.assertEqual(result["split_policy"]["holdout_pair_count"], 20)
        self.assertEqual(result["split_policy"]["sample_overlap_count"], 0)
        self.assertEqual(
            result["fixed_configuration"]["hard_batch_cap_cny"],
            format(HARD_BATCH_CAP_CNY, "f"),
        )
        self.assertEqual(result["fixed_configuration"]["cloud_weight"], 0.15)
        self.assertEqual(len(result["reference_sample_ids"]), 20)
        self.assertEqual(len(result["calibration_predictions"]), 40)
        self.assertEqual(len(result["holdout_baseline"]), 20)
        _assert_blind_payload(result)
        self.assertNotIn("proxy_choice", str(result))
        self.assertIn("holdout-config", persisted)

    def test_blind_payload_rejects_outcomes_at_any_depth(self) -> None:
        with self.assertRaisesRegex(ValueError, "forbidden outcome fields"):
            _assert_blind_payload({"predictions": [{"proxy_choice": "left"}]})

    def test_evaluation_unlocks_only_after_prediction_sha(self) -> None:
        config = self._freeze_config()
        outcome_by_task = {
            str(item["task_id"]): str(item["proxy_choice"])
            for item in self.baseline["pair_results"]
        }
        prediction_rows = []
        for item in config["holdout_baseline"]:
            task_id = str(item["task_id"])
            final_delta = 1.0 if outcome_by_task[task_id] == "left" else -1.0
            prediction_rows.append(
                {
                    "task_id": task_id,
                    "left_sample_id": item["left_sample_id"],
                    "right_sample_id": item["right_sample_id"],
                    "v2_4_delta": item["v2_4_delta"],
                    "embedding_delta": final_delta,
                    "rerank_delta": final_delta,
                    "cloud_delta": final_delta,
                    "final_delta": final_delta,
                    "predicted_choice": outcome_by_task[task_id],
                }
            )
        prediction_core = {
            "contract_version": config["contract_version"],
            "status": "predictions_frozen",
            "admission_status": "research_only",
            "benchmark_id": self.manifest["benchmark_id"],
            "manifest_sha256": self.manifest["manifest_sha256"],
            "config_sha256": config["config_sha256"],
            "batch_id": "d12b-test",
            "pair_count": 20,
            "predictions": prediction_rows,
            "coverage": {},
            "budget": {
                "hard_batch_cap_cny": "10.00",
                "effective_cost_cny": "0.25",
                "network_request_count": 40,
            },
            "labels_locked": True,
            "blind_payload_verified": True,
            "automatic_promotion": False,
            "production_weight_changed": False,
        }
        prediction = {
            **prediction_core,
            "prediction_sha256": stable_json_sha256(prediction_core),
            "generated_at": "2026-07-19T00:00:00+00:00",
        }
        persisted = {}

        def load_stage(_benchmark_id: str, stage: str):
            return {
                "holdout-config": config,
                "holdout-predictions": prediction,
            }.get(stage)

        with (
            patch(
                "dso.learning.bailian_holdout_validation.load_multimodal_vector_manifest",
                return_value=self.manifest,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._local_vector_report",
                return_value=(self.baseline, "frozen_sidecar"),
            ),
            patch(
                "dso.learning.bailian_holdout_validation._load_stage_report",
                side_effect=load_stage,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._persist_stage_report",
                side_effect=lambda _manifest, stage, report: persisted.setdefault(stage, report),
            ),
        ):
            report = evaluate_bailian_holdout_validation("holdout-test-r1")

        self.assertEqual(report["holdout_primary"]["evaluable_pair_count"], 20)
        self.assertEqual(report["holdout_primary"]["balanced_pairwise_accuracy"], 1.0)
        self.assertTrue(report["labels_unlocked_after_prediction_sha"])
        self.assertFalse(report["production_weight_changed"])
        self.assertEqual(report["budget"]["hard_batch_cap_cny"], "10.00")
        self.assertIn("holdout-evaluation", persisted)

    def test_web_route_dispatches_holdout_actions(self) -> None:
        client = TestClient(app)
        with patch(
            "dso.api.main.freeze_bailian_holdout_validation",
            return_value={"status": "frozen", "hard_batch_cap_cny": "10.00"},
        ):
            response = client.post(
                "/learning/multimodal-vector-experiment/cloud/holdout/freeze",
                json={"benchmark_id": "holdout-test-r1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "frozen")

    def test_cache_only_failure_attribution_is_reproducible(self) -> None:
        config, prediction, evaluation = self._evaluated_artifacts()
        persisted = {}

        def load_stage(_benchmark_id: str, stage: str):
            return {
                "holdout-config": config,
                "holdout-predictions": prediction,
                "holdout-evaluation": evaluation,
                "holdout-rerank": self.rerank,
            }.get(stage)

        with (
            patch(
                "dso.learning.bailian_failure_attribution.load_multimodal_vector_manifest",
                return_value=self.manifest,
            ),
            patch(
                "dso.learning.bailian_failure_attribution._load_stage_report",
                side_effect=load_stage,
            ),
            patch(
                "dso.learning.bailian_failure_attribution._cloud_records",
                return_value={},
            ),
            patch(
                "dso.learning.bailian_failure_attribution._vectors_for_modality",
                return_value=self.vectors,
            ),
            patch(
                "dso.learning.bailian_failure_attribution._persist_stage_report",
                side_effect=lambda _manifest, stage, report: persisted.setdefault(stage, report),
            ),
        ):
            report = run_bailian_holdout_failure_attribution("holdout-test-r1")

        self.assertEqual(report["analysis_scope"]["pair_count"], 20)
        self.assertEqual(report["network_request_count"], 0)
        self.assertEqual(report["effective_cost_cny"], "0")
        self.assertTrue(report["cache_only"])
        self.assertEqual(report["component_comparison"]["fusion_embedding"]["pair_count"], 20)
        self.assertIn("holdout-failure-attribution", persisted)
        self.assertTrue(report["source_artifacts"]["blind_prediction_verified"])

    def test_failure_attribution_rejects_tampered_prediction(self) -> None:
        config, prediction, evaluation = self._evaluated_artifacts()
        prediction = {**prediction, "prediction_sha256": "tampered"}

        def load_stage(_benchmark_id: str, stage: str):
            return {
                "holdout-config": config,
                "holdout-predictions": prediction,
                "holdout-evaluation": evaluation,
                "holdout-rerank": self.rerank,
            }.get(stage)

        with (
            patch(
                "dso.learning.bailian_failure_attribution.load_multimodal_vector_manifest",
                return_value=self.manifest,
            ),
            patch(
                "dso.learning.bailian_failure_attribution._load_stage_report",
                side_effect=load_stage,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "prediction checksum mismatch"):
                run_bailian_holdout_failure_attribution("holdout-test-r1")

    def test_web_route_runs_cache_only_failure_attribution(self) -> None:
        client = TestClient(app)
        with patch(
            "dso.api.main.run_bailian_holdout_failure_attribution",
            return_value={"status": "ready", "network_request_count": 0},
        ):
            response = client.post(
                "/learning/multimodal-vector-experiment/cloud/holdout-attribution",
                json={"benchmark_id": "holdout-test-r1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["network_request_count"], 0)

    def test_evidence_quality_builds_three_distinct_windows_and_reuses_pack(self) -> None:
        root = Path(self.tmp.name)
        video_path = root / "data" / "douyin_media_assets" / "test" / "video.mp4"
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(b"video")
        manifest = {
            "benchmark_id": "holdout-test-r1",
            "manifest_sha256": "a" * 64,
        }
        sample = {
            "sample_id": "sample-window-test",
            "account_id": "test",
            "duration_seconds": 60.0,
            "title": "test",
            "media": {
                "video": {
                    "path": str(video_path.relative_to(root)),
                    "sha256": "video-sha",
                }
            },
        }

        def fake_extract(_video_path: Path, output_path: Path, _timestamp: float) -> bool:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"jpeg")
            return True

        with (
            patch(
                "dso.learning.bailian_evidence_quality._extract_frame",
                side_effect=fake_extract,
            ),
            patch(
                "dso.learning.bailian_evidence_quality.jpeg_dimensions",
                return_value=(640, 360),
            ),
        ):
            first = _build_or_reuse_evidence_pack(manifest, sample, force=False)
            second = _build_or_reuse_evidence_pack(manifest, sample, force=False)

        self.assertEqual(first["status"], "ready")
        self.assertEqual([item["role"] for item in first["windows"]], ["hook", "middle", "payoff"])
        self.assertEqual(len({item["frame_seconds"] for item in first["windows"]}), 3)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(_window_plan(60.0, 15.0)), 3)

    def test_stratified_retrieval_prefers_same_account_for_each_label(self) -> None:
        samples = {
            "query": {"account_id": "a", "performance_label": "high", "semantic": {"content_category": "performance_clip"}},
            "high-a": {"account_id": "a", "performance_label": "high", "semantic": {"content_category": "performance_clip"}},
            "low-a": {"account_id": "a", "performance_label": "low", "semantic": {"content_category": "performance_clip"}},
            "high-global": {"account_id": "b", "performance_label": "high", "semantic": {"content_category": "performance_clip"}},
            "low-global": {"account_id": "b", "performance_label": "low", "semantic": {"content_category": "performance_clip"}},
        }
        vectors = {
            "query": [1.0, 0.0],
            "high-a": [0.8, 0.2],
            "low-a": [0.2, 0.8],
            "high-global": [1.0, 0.0],
            "low-global": [0.0, 1.0],
        }
        profiles = _stratified_retrieval_profiles(
            ["query"],
            ["high-a", "low-a", "high-global", "low-global"],
            samples,
            vectors,
            neighbors_per_label=1,
        )
        selected = profiles["query"]["top_matches"]
        self.assertEqual({item["sample_id"] for item in selected}, {"high-a", "low-a"})
        self.assertTrue(profiles["query"]["balanced_same_account_available"])

    def test_reference_plan_reports_manifest_coverage_ceiling(self) -> None:
        samples = {
            "q-a": {"account_id": "a"},
            "q-b": {"account_id": "b"},
            "a-high": {"account_id": "a", "performance_label": "high"},
            "a-low": {"account_id": "a", "performance_label": "low"},
            "b-high": {"account_id": "b", "performance_label": "high"},
        }
        plan = _reference_coverage_plan(
            ["q-a", "q-b"],
            ["a-high", "a-low", "b-high"],
            ["a-high", "b-high"],
            samples,
        )
        self.assertEqual(plan["cached_balanced_same_account_coverage"], 0.0)
        self.assertEqual(plan["manifest_balanced_same_account_ceiling"], 0.5)
        self.assertEqual(plan["unrecoverable_accounts"], ["b"])
        self.assertEqual(plan["recommended_reference_ids"], ["a-low"])

    def test_evidence_quality_run_is_cache_only_and_never_promotes(self) -> None:
        config, prediction, evaluation = self._evaluated_artifacts()
        persisted = {}

        def load_stage(_benchmark_id: str, stage: str):
            return {
                "holdout-config": config,
                "holdout-predictions": prediction,
                "holdout-evaluation": evaluation,
            }.get(stage)

        def ready_pack(_manifest: dict, sample: dict, *, force: bool) -> dict:
            del force
            return {
                "sample_id": sample["sample_id"],
                "status": "ready",
                "cache_hit": True,
                "windows": [
                    {"role": role, "status": "ready", "frame_seconds": index * 10.0}
                    for index, role in enumerate(("hook", "middle", "payoff"), start=1)
                ],
            }

        with (
            patch(
                "dso.learning.bailian_evidence_quality.load_multimodal_vector_manifest",
                return_value=self.manifest,
            ),
            patch(
                "dso.learning.bailian_evidence_quality._load_stage_report",
                side_effect=load_stage,
            ),
            patch(
                "dso.learning.bailian_evidence_quality._build_or_reuse_evidence_pack",
                side_effect=ready_pack,
            ),
            patch(
                "dso.learning.bailian_evidence_quality._cloud_records",
                return_value={},
            ),
            patch(
                "dso.learning.bailian_evidence_quality._vectors_for_modality",
                return_value=self.vectors,
            ),
            patch(
                "dso.learning.bailian_evidence_quality._persist_stage_report",
                side_effect=lambda _manifest, stage, report: persisted.setdefault(stage, report),
            ),
        ):
            report = run_bailian_evidence_quality_reconstruction("holdout-test-r1")

        self.assertEqual(report["network_request_count"], 0)
        self.assertEqual(report["effective_cost_cny"], "0")
        self.assertEqual(report["evidence_pack"]["summary"]["ready_count"], 40)
        self.assertFalse(report["evidence_gate"]["passed"])
        self.assertFalse(report["production_weight_changed"])
        self.assertIn("evidence-quality-reconstruction", persisted)

    def test_web_route_runs_evidence_quality_reconstruction(self) -> None:
        client = TestClient(app)
        with patch(
            "dso.api.main.run_bailian_evidence_quality_reconstruction",
            return_value={"status": "ready_for_embedding_rebuild", "network_request_count": 0},
        ):
            response = client.post(
                "/learning/multimodal-vector-experiment/cloud/evidence-quality/rebuild",
                json={"benchmark_id": "holdout-test-r1", "scope": "holdout", "limit": 40},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["network_request_count"], 0)

    def _freeze_config(self) -> dict:
        def load_stage(_benchmark_id: str, stage: str):
            return {"ablation": self.ablation, "rerank": self.rerank}.get(stage)

        with (
            patch(
                "dso.learning.bailian_holdout_validation.load_multimodal_vector_manifest",
                return_value=self.manifest,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._local_vector_report",
                return_value=(self.baseline, "frozen_sidecar"),
            ),
            patch(
                "dso.learning.bailian_holdout_validation._load_stage_report",
                side_effect=load_stage,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._cloud_records",
                return_value={},
            ),
            patch(
                "dso.learning.bailian_holdout_validation._vectors_for_modality",
                return_value=self.vectors,
            ),
            patch("dso.learning.bailian_holdout_validation._persist_stage_report"),
        ):
            return freeze_bailian_holdout_validation("holdout-test-r1")

    def _evaluated_artifacts(self) -> tuple[dict, dict, dict]:
        config = self._freeze_config()
        outcome_by_task = {
            str(item["task_id"]): str(item["proxy_choice"])
            for item in self.baseline["pair_results"]
        }
        prediction_rows = []
        for item in config["holdout_baseline"]:
            task_id = str(item["task_id"])
            delta = 1.0 if outcome_by_task[task_id] == "left" else -1.0
            prediction_rows.append(
                {
                    "task_id": task_id,
                    "left_sample_id": item["left_sample_id"],
                    "right_sample_id": item["right_sample_id"],
                    "v2_4_delta": item["v2_4_delta"],
                    "embedding_delta": delta,
                    "rerank_delta": delta,
                    "cloud_delta": delta,
                    "final_delta": delta,
                    "predicted_choice": outcome_by_task[task_id],
                }
            )
        prediction_core = {
            "contract_version": config["contract_version"],
            "status": "predictions_frozen",
            "admission_status": "research_only",
            "benchmark_id": self.manifest["benchmark_id"],
            "manifest_sha256": self.manifest["manifest_sha256"],
            "config_sha256": config["config_sha256"],
            "batch_id": "d12c0-test",
            "pair_count": 20,
            "predictions": prediction_rows,
            "coverage": {},
            "budget": {
                "hard_batch_cap_cny": "10.00",
                "effective_cost_cny": "0.25",
                "network_request_count": 40,
            },
            "labels_locked": True,
            "blind_payload_verified": True,
            "automatic_promotion": False,
            "production_weight_changed": False,
        }
        prediction = {
            **prediction_core,
            "prediction_sha256": stable_json_sha256(prediction_core),
            "generated_at": "2026-07-19T00:00:00+00:00",
        }

        def load_stage(_benchmark_id: str, stage: str):
            return {
                "holdout-config": config,
                "holdout-predictions": prediction,
            }.get(stage)

        with (
            patch(
                "dso.learning.bailian_holdout_validation.load_multimodal_vector_manifest",
                return_value=self.manifest,
            ),
            patch(
                "dso.learning.bailian_holdout_validation._local_vector_report",
                return_value=(self.baseline, "frozen_sidecar"),
            ),
            patch(
                "dso.learning.bailian_holdout_validation._load_stage_report",
                side_effect=load_stage,
            ),
            patch("dso.learning.bailian_holdout_validation._persist_stage_report"),
        ):
            evaluation = evaluate_bailian_holdout_validation("holdout-test-r1")
        return config, prediction, evaluation


def _synthetic_frozen_inputs() -> tuple[dict, dict, dict, dict, dict[str, list[float]]]:
    benchmark_id = "holdout-test-r1"
    manifest_sha = "a" * 64
    tasks = []
    samples = {}
    pair_results = []
    vectors = {}
    rerank_items = []
    evaluation_ids = []
    for index in range(60):
        task_id = f"pair-{index + 1:03d}"
        left_id = f"sample-left-{index + 1:03d}"
        right_id = f"sample-right-{index + 1:03d}"
        tasks.append(
            {
                "task_id": task_id,
                "left_sample_id": left_id,
                "right_sample_id": right_id,
            }
        )
        evaluation_ids.extend((left_id, right_id))
        outcome = "left" if index % 2 == 0 else "right"
        v24_choice = outcome if index % 3 else ("right" if outcome == "left" else "left")
        v24_delta = 1.0 if v24_choice == "left" else -1.0
        pair_results.append(
            {
                "task_id": task_id,
                "proxy_choice": outcome,
                "predictions": {"research_ranker_v2_4": v24_choice},
                "score_deltas": {"research_ranker_v2_4": v24_delta},
            }
        )
        for sample_id, is_preferred in (
            (left_id, outcome == "left"),
            (right_id, outcome == "right"),
        ):
            samples[sample_id] = {
                "sample_id": sample_id,
                "account_id": "yuhuan" if index >= 40 else f"account-{index % 5}",
                "normalized_reward": 1.0 if is_preferred else 0.0,
                "title": sample_id,
                "semantic": {"content_category": "performance_clip"},
            }
            vectors[sample_id] = [1.0, 0.0] if is_preferred else [0.0, 1.0]
            rerank_items.append(
                {"sample_id": sample_id, "score": 80.0 if is_preferred else 20.0}
            )

    reference_ids = []
    for index in range(10):
        for label, vector in (("high", [1.0, 0.0]), ("low", [0.0, 1.0])):
            sample_id = f"reference-{label}-{index:02d}"
            reference_ids.append(sample_id)
            samples[sample_id] = {
                "sample_id": sample_id,
                "account_id": "reference",
                "performance_label": label,
                "title": sample_id,
                "semantic": {"content_category": "performance_clip"},
            }
            vectors[sample_id] = vector

    manifest = {
        "benchmark_id": benchmark_id,
        "manifest_sha256": manifest_sha,
        "tasks": tasks,
        "samples": samples,
        "evaluation_sample_ids": evaluation_ids,
        "reference_sample_ids": reference_ids,
    }
    baseline = {
        "benchmark_id": benchmark_id,
        "manifest_sha256": manifest_sha,
        "pair_results": pair_results,
    }
    ablation = {
        "benchmark_id": benchmark_id,
        "manifest_sha256": manifest_sha,
        "generated_at": "2026-07-19T00:00:00+00:00",
    }
    rerank = {
        "benchmark_id": benchmark_id,
        "manifest_sha256": manifest_sha,
        "items": rerank_items[:80],
        "generated_at": "2026-07-19T00:00:00+00:00",
    }
    return manifest, baseline, ablation, rerank, vectors


if __name__ == "__main__":
    unittest.main()
