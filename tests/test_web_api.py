from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - exercised by environments without web deps
    TestClient = None

from dso.db.session import connect, init_db
from dso.scoring.scorer import score_segment
from dso.versions import (
    ARTIFACT_MANIFEST_VERSION,
    BACKTEST_VERSION,
    DOUYIN_HISTORY_VERSION,
    FEEDBACK_INSIGHTS_VERSION,
    FEEDBACK_STATE_VERSION,
    INTEREST_CLOCK_VERSION,
    MEMORY_BANK_VERSION,
    METRICS_IMPORT_VERSION,
    PLATFORM_SYNC_VERSION,
    QUALITY_GATE_VERSION,
    QUALITY_INSIGHTS_VERSION,
    RESEARCH_LABEL_VERSION,
    SEMANTIC_FEATURE_VERSION,
    VARIANT_EXPERIMENT_VERSION,
)


@unittest.skipIf(TestClient is None, "FastAPI/TestClient dependencies are not installed")
class WebApiTest(unittest.TestCase):
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
        os.environ.pop("DSO_RIGHTS_MODE", None)
        for name in ["DSO_DOUYIN_CLIENT_KEY", "DSO_DOUYIN_CLIENT_SECRET", "DSO_DOUYIN_REDIRECT_URI", "DSO_DOUYIN_SCOPES"]:
            os.environ.pop(name, None)

    def test_dashboard_home_renders_gate_and_feedback_summary(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Douyin Slice Optimizer", response.text)
        self.assertIn('meta name="dso-frontend" content="vue3-vite-typescript"', response.text)
        self.assertIn('id="dso-initial-state"', response.text)
        asset_match = re.search(r'src="([^"]+/assets/index-[^"]+\.js)"', response.text)
        self.assertIsNotNone(asset_match)
        asset_response = self.client.get(asset_match.group(1))
        self.assertEqual(asset_response.status_code, 200)
        self.assertIn("javascript", asset_response.headers["content-type"])
        self.assertIn("研究学习", asset_response.text)
        self.assertIn("研究样本、历史先验、校准回测与平台账号", asset_response.text)
        self.assertIn("semantic-calibration-queue", asset_response.text)
        self.assertIn("memory-build-btn", asset_response.text)
        self.assertIn("backtest-btn", asset_response.text)
        self.assertIn("qwen-embedding-btn", asset_response.text)

    def test_static_dashboard_directory_serves_index(self) -> None:
        response = self.client.get("/static/dashboard/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Douyin Slice Optimizer", response.text)
        self.assertIn("/static/dashboard/assets/", response.text)

    def test_qwen_omni_candidate_analyze_endpoint_is_shadow_only(self) -> None:
        segment = _insert_segment()
        with patch(
            "dso.api.main.analyze_candidate_with_qwen_omni",
            return_value={
                "contract_version": "qwen2_5_omni_7b_gptq_int4.shadow_v1",
                "status": "ready",
                "entity_id": segment["id"],
                "writes_labels": False,
                "production_weight": False,
            },
        ):
            response = self.client.post(
                f"/segments/{segment['id']}/qwen-omni/analyze",
                json={"max_clip_seconds": 15, "load_model": False},
            )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertFalse(payload["writes_labels"])
        self.assertFalse(payload["production_weight"])

    def test_quality_endpoint_returns_read_only_gate(self) -> None:
        segment = _insert_segment()
        _write_transcript(
            self.root / "transcript_clean.json",
            segment["source_video_id"],
            [
                {"index": 0, "start": 0, "end": 8, "text": "导师点评这次改编很突破"},
                {"index": 1, "start": 8, "end": 18, "text": "副歌高音爆发 全场观众欢呼"},
            ],
        )
        score_segment(segment["id"])

        response = self.client.get(f"/videos/{segment['source_video_id']}/quality?top_k=5")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["gate"]["status"], "allow")
        self.assertEqual(payload["gate"]["enforcement"], "read_only")
        self.assertEqual(payload["gate"]["version"], QUALITY_GATE_VERSION)
        self.assertEqual(payload["contract_version"], QUALITY_INSIGHTS_VERSION)
        self.assertEqual(payload["gate"]["signals"]["rights_mode"], "trusted_sample")
        self.assertEqual(payload["gate"]["primary_action"]["kind"], "export_preview")
        self.assertIn("导出", payload["gate"]["summary"])
        self.assertEqual(payload["query"]["top_k"], 5)
        self.assertIn("simulation", payload)

    def test_suggestions_include_derived_review_status(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])

        response = self.client.get(f"/videos/{segment['source_video_id']}/suggestions?top_k=5")
        row = response.json()["suggestions"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(row["review_status"], "candidate")
        self.assertEqual(row["review_status_label"], "待审核")
        self.assertEqual(row["review_status_source"], "api.derived.v1")

        with connect() as conn:
            conn.execute("UPDATE candidate_segments SET status = 'corrected' WHERE id = ?", [segment["id"]])
            conn.commit()

        response = self.client.get(f"/videos/{segment['source_video_id']}/suggestions?top_k=5")
        row = response.json()["suggestions"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(row["review_status"], "needs_review")
        self.assertEqual(row["review_status_label"], "需复核")
        self.assertEqual(row["workflow_status"], "review")
        self.assertIn("修正", row["review_status_reason"])

    def test_blocked_candidate_status_takes_priority_over_existing_export(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO slice_variants
                (id, candidate_segment_id, title, cover_time, subtitle_style, export_path, variant_notes, hypothesis, changed_variable, publish_window, status, predicted_score, created_at, updated_at)
                VALUES ('variant_exported', ?, '已导出标题', 20, 'lyrics_and_dialogue', '/tmp/exported.mp4', '', '', '', '', 'exported', 80, ?, ?)
                """,
                [segment["id"], "2026-06-23T00:00:00+00:00", "2026-06-23T00:00:00+00:00"],
            )
            conn.execute("UPDATE candidate_segments SET status = 'blocked' WHERE id = ?", [segment["id"]])
            conn.commit()

        response = self.client.get(f"/videos/{segment['source_video_id']}/suggestions?top_k=5")
        row = response.json()["suggestions"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(row["review_status"], "blocked")
        self.assertIn("已有导出", row["review_status_reason"])

    def test_review_manifest_and_variant_api_support_v04_workbench(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])

        review = self.client.post(
            f"/segments/{segment['id']}/review",
            json={"status": "approved", "reason": "人工确认", "operator": "tester"},
        ).json()
        suggestions = self.client.get(f"/videos/{segment['source_video_id']}/suggestions?top_k=5").json()
        manifest = self.client.get(f"/videos/{segment['source_video_id']}/manifest").json()
        variant = self.client.post(
            f"/segments/{segment['id']}/variants",
            json={
                "title": "标题 A",
                "hypothesis": "标题变化提升留存",
                "changed_variable": "title",
                "publish_window": "24h",
            },
        ).json()
        experiment = self.client.post(
            f"/variants/{variant['id']}/experiments",
            json={"experiment_group": "A", "changed_variable": "title", "publish_window": "24h"},
        ).json()
        changes = self.client.get(f"/segments/{segment['id']}/changes").json()

        self.assertEqual(review["review_status"], "approved")
        self.assertEqual(review["segment"]["review_status"], "approved")
        self.assertEqual(suggestions["suggestions"][0]["review_status"], "approved")
        self.assertEqual(manifest["contract_version"], ARTIFACT_MANIFEST_VERSION)
        self.assertIn("scores", {item["step"] for item in manifest["steps"]})
        self.assertEqual(variant["contract_version"], VARIANT_EXPERIMENT_VERSION)
        self.assertEqual(variant["changed_variable"], "title")
        self.assertEqual(experiment["contract_version"], VARIANT_EXPERIMENT_VERSION)
        self.assertTrue(changes["review_events"])
        self.assertTrue(changes["changes"])

    def test_platform_mapping_and_mock_metric_contract_are_local_only(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        variant = self.client.post(
            f"/segments/{segment['id']}/variants",
            json={"title": "平台标题", "changed_variable": "title"},
        ).json()
        mapping = self.client.post(
            "/platform/mappings",
            json={"platform": "douyin", "platform_item_id": "aweme_api_1", "slice_variant_id": variant["id"]},
        ).json()
        mapped = self.client.post(
            "/platform/mock-map",
            json={"platform": "douyin", "aweme_id": "aweme_api_1", "play_count": "100", "like_count": "8"},
        ).json()
        mappings = self.client.get("/platform/mappings?platform=douyin").json()

        self.assertEqual(mapping["candidate_segment_id"], segment["id"])
        self.assertEqual(mapped["mapped_row"]["platform_item_id"], "aweme_api_1")
        self.assertEqual(mapped["mapped_row"]["views"], "100")
        self.assertIn("mock", mapped["contract"]["sample_sources"])
        self.assertEqual(mappings["count"], 1)

    def test_douyin_sync_api_imports_mock_feedback_for_mapped_item(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        variant = self.client.post(
            f"/segments/{segment['id']}/variants",
            json={"title": "回流标题", "changed_variable": "title"},
        ).json()
        account = self.client.post(
            "/platform/accounts",
            json={"platform": "douyin", "account_id": "main", "display_name": "测试账号"},
        ).json()
        self.client.post(
            "/platform/mappings",
            json={"platform": "douyin", "platform_item_id": "aweme_sync_api", "slice_variant_id": variant["id"]},
        )

        response = self.client.post(
            "/platform/douyin/sync",
            json={"account_id": "main", "source": "mock", "windows": ["6h"]},
        )
        payload = response.json()
        runs = self.client.get("/platform/sync-runs?account_id=main&platform=douyin").json()
        summary = self.client.get("/platform/douyin/summary?account_id=main").json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(account["auth_status"], "mock_ready")
        self.assertEqual(payload["contract_version"], PLATFORM_SYNC_VERSION)
        self.assertEqual(payload["import_result"]["row_summary"]["linked_rows"], 1)
        self.assertEqual(payload["import_result"]["training_samples"], 1)
        self.assertEqual(runs["count"], 1)
        self.assertEqual(summary["metrics"]["count"], 1)

    def test_douyin_oauth_start_api_returns_scan_url_and_status(self) -> None:
        os.environ["DSO_DOUYIN_CLIENT_KEY"] = "client_key_demo"
        os.environ["DSO_DOUYIN_REDIRECT_URI"] = "https://example.com/platform/douyin/oauth/callback"

        response = self.client.post(
            "/platform/douyin/oauth/start",
            json={"account_id": "main", "scopes": ["user_info", "posting.behavior"]},
        )
        payload = response.json()
        status = self.client.get(f"/platform/douyin/oauth/status?account_id=main&state={payload['state']}").json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "waiting_scan")
        self.assertIn("open.douyin.com/platform/oauth/connect", payload["auth_url"])
        self.assertEqual(payload["config"]["ready_for_qr_login"], True)
        self.assertEqual(status["session"]["status"], "waiting_scan")

    def test_metrics_import_api_reports_linkage_and_updates_insights(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        csv_path = self.root / "metrics_api.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["candidate_segment_id", "window_name", "views", "impressions", "avg_watch_ratio"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "candidate_segment_id": segment["id"],
                    "window_name": "24h",
                    "views": "1000",
                    "impressions": "2200",
                    "avg_watch_ratio": "82%",
                }
            )
            writer.writerow(
                {
                    "candidate_segment_id": "seg_missing",
                    "window_name": "24h",
                    "views": "500",
                    "impressions": "1200",
                    "avg_watch_ratio": "40%",
                }
            )

        with csv_path.open("rb") as handle:
            response = self.client.post(
                "/metrics/import",
                files={"file": ("metrics_api.csv", handle, "text/csv")},
            )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["contract_version"], METRICS_IMPORT_VERSION)
        self.assertEqual(payload["row_summary"]["linked_rows"], 1)
        self.assertEqual(payload["row_summary"]["unlinked_rows"], 1)
        self.assertEqual(payload["training_eligibility"]["eligible_rows"], 1)

        insights = self.client.get("/accounts/main/insights").json()
        samples = self.client.get("/training-samples?account_id=main").json()
        baselines = self.client.get("/accounts/main/baselines").json()
        rebuild = self.client.post("/feedback/rebuild?account_id=main").json()
        self.assertEqual(insights["sample_count"], 1)
        self.assertEqual(insights["contract_version"], FEEDBACK_INSIGHTS_VERSION)
        self.assertEqual(insights["account_id"], "main")
        self.assertEqual(samples["count"], 1)
        self.assertEqual(samples["contract_version"], FEEDBACK_STATE_VERSION)
        self.assertEqual(samples["query"]["account_id"], "main")
        self.assertEqual(baselines["contract_version"], FEEDBACK_STATE_VERSION)
        self.assertEqual(baselines["account_id"], "main")
        self.assertEqual(rebuild["contract_version"], FEEDBACK_STATE_VERSION)
        self.assertEqual(rebuild["feedback_state"]["rebuilt_training_samples"], 1)

    def test_learning_api_supports_feedback_panel(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        csv_path = self.root / "learning_api.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "candidate_segment_id",
                    "window_name",
                    "collected_at",
                    "views",
                    "impressions",
                    "avg_watch_ratio",
                    "completion_rate",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "candidate_segment_id": segment["id"],
                    "window_name": "24h",
                    "collected_at": "2026-06-23T20:00:00+00:00",
                    "views": "1600",
                    "impressions": "2400",
                    "avg_watch_ratio": "78%",
                    "completion_rate": "61%",
                }
            )
        with csv_path.open("rb") as handle:
            self.client.post(
                "/metrics/import",
                files={"file": ("learning_api.csv", handle, "text/csv")},
            )

        memory = self.client.post("/learning/memory/build", json={"account_id": "main"}).json()
        clock = self.client.post("/accounts/main/interest-clock/rebuild").json()
        recommended = self.client.get("/accounts/main/interest-clock?limit=2").json()
        report = self.client.post("/learning/backtest", json={"account_id": "main", "k": 3}).json()
        reports = self.client.get("/learning/backtest?account_id=main&limit=1").json()

        self.assertEqual(memory["contract_version"], MEMORY_BANK_VERSION)
        self.assertEqual(memory["total_candidates"], 1)
        self.assertEqual(clock["contract_version"], INTEREST_CLOCK_VERSION)
        self.assertTrue(clock["top_windows"])
        self.assertEqual(recommended["contract_version"], INTEREST_CLOCK_VERSION)
        self.assertTrue(recommended["recommendations"])
        self.assertEqual(report["contract_version"], BACKTEST_VERSION)
        self.assertEqual(report["metrics"]["sample_count"], 1)
        self.assertEqual(report["metrics"]["strategy"], "research_ranker_v2_4")
        self.assertIn("weight_config", report["metrics"])
        self.assertIn("semantic_gap_analysis", report["metrics"])
        self.assertEqual(reports["reports"][0]["contract_version"], BACKTEST_VERSION)
        self.assertEqual(reports["reports"][0]["metrics"]["sample_count"], 1)
        self.assertTrue(reports["reports"][0]["top_rows"])

    def test_learning_api_imports_douyin_clean_history(self) -> None:
        clean_dir = self.root / "data" / "douyin_capture" / "sixuweilive" / "clean_20260628T010000_appleevents_api"
        raw_dir = self.root / "data" / "douyin_capture" / "sixuweilive" / "raw_20260628T010000_appleevents_api"
        clean_dir.mkdir(parents=True)
        raw_dir.mkdir(parents=True)
        (clean_dir / "douyin_visible_works_dedup_latest.json").write_text(
            json.dumps(
                [
                    {
                        "aweme_id": "2001",
                        "normalized_title": "歌手舞台高能切片",
                        "best_visible_count_number": 88000,
                        "content_category": "performance_highlight",
                        "hook_type": "music_burst",
                        "slice_structure": "climax_first",
                        "program_name": "歌手2026",
                        "artist_names": ["歌手A"],
                        "tags": ["歌手2026", "live"],
                    },
                    {
                        "aweme_id": "2002",
                        "normalized_title": "普通花絮切片",
                        "best_visible_count_number": 1200,
                        "content_category": "behind_scene",
                        "hook_type": "daily_moment",
                        "slice_structure": "linear",
                        "program_name": "歌手2026",
                        "artist_names": ["歌手B"],
                        "tags": ["歌手2026", "花絮"],
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (raw_dir / "sixuweilive_post_api_works.json").write_text(
            json.dumps(
                [
                    {"aweme_id": "2001", "digg_count": 88000, "comment_count": 600, "share_count": 240, "collect_count": 900, "duration": 41000, "create_time": 1782604800},
                    {"aweme_id": "2002", "digg_count": 1200, "comment_count": 5, "share_count": 1, "collect_count": 2, "duration": 23000, "create_time": 1782608400},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        imported = self.client.post(
            "/learning/historical-samples/import",
            json={
                "source_type": "douyin_clean",
                "account_id": "sixuweilive",
                "clean_dir": str(clean_dir),
                "dataset_id": "sixuweilive_20260628",
                "force": True,
            },
        ).json()
        baselines = self.client.get(
            "/learning/douyin-history/baselines?account_id=sixuweilive&dataset_id=sixuweilive_20260628&min_count=1"
        ).json()
        summary = self.client.get("/learning/historical-samples/summary?account_id=sixuweilive").json()
        datasets = self.client.get("/learning/datasets?account_id=sixuweilive").json()
        coverage = self.client.get(
            "/learning/research/coverage?account_id=sixuweilive&dataset_id=sixuweilive_20260628"
        ).json()
        queue = self.client.get(
            "/learning/semantic-calibration/queue?account_id=sixuweilive&dataset_id=sixuweilive_20260628&limit=2&label=high&min_priority=1"
        ).json()
        dataset = next(item for item in datasets["datasets"] if item["id"] == "sixuweilive_20260628")
        sample_id = queue["samples"][0]["id"]
        patched = self.client.patch(
            f"/learning/historical-samples/{sample_id}/labels",
            json={
                "content_category": "performance_highlight",
                "hook_type": "music_burst",
                "slice_structure": "climax_first",
                "artist_names": ["歌手A"],
                "song_title": "测试歌",
                "tags": ["歌手2026", "live"],
            },
        ).json()
        reopened = self.client.post(
            f"/learning/historical-samples/{sample_id}/calibration/reopen",
            json={"classification_confidence": "low", "operator": "tester", "reason": "api reopen test"},
        ).json()
        reopened_queue = self.client.get(
            "/learning/semantic-calibration/queue?account_id=sixuweilive&dataset_id=sixuweilive_20260628&limit=2&label=high&min_priority=1"
        ).json()
        labels = self.client.post(
            "/learning/research-labels/rebuild",
            json={"account_id": "sixuweilive", "dataset_id": "sixuweilive_20260628", "min_baseline_samples": 2},
        ).json()
        tuning = self.client.post(
            "/learning/ranker-tuning/run",
            json={"account_id": "sixuweilive", "k": 1, "holdout_policy": "time", "max_trials": 2},
        ).json()
        multimodal_plan = self.client.post(
            "/learning/multimodal/collection-plan",
            json={"account_id": "sixuweilive", "dataset_id": "sixuweilive_20260628", "limit": 1},
        ).json()
        multimodal_validation = self.client.post(
            "/learning/multimodal-validation/run",
            json={"account_id": "sixuweilive", "dataset_id": "sixuweilive_20260628", "limit": 10, "min_samples": 1},
        ).json()
        multimodal_feature = self.client.post(
            "/learning/multimodal-feature-experiment/run",
            json={"account_id": "sixuweilive", "dataset_id": "sixuweilive_20260628", "limit": 10, "min_feature_samples": 1},
        ).json()
        with patch(
            "dso.api.main.build_qwen_embedding_index",
            return_value={
                "contract_version": "qwen3_vl_embedding.evidence_v1",
                "status": "ready",
                "created": 1,
                "reused": 1,
                "skipped": 0,
                "failed": 0,
                "coverage": {"ready_rate": 1.0},
                "service_status": {"status": "ready"},
            },
        ), patch(
            "dso.api.main.run_qwen_embedding_evidence",
            return_value={
                "contract_version": "qwen3_vl_embedding.evidence_v1",
                "status": "ready",
                "embedding_coverage": {"text_ready_count": 2},
                "similar_evidence_summary": {"sample_count": 1},
            },
        ):
            qwen_build = self.client.post(
                "/learning/qwen-embeddings/build",
                json={"account_id": "sixuweilive", "dataset_id": "sixuweilive_20260628", "modality": "text", "limit": 2},
            ).json()
            qwen_evidence = self.client.post(
                "/learning/qwen-embedding-evidence/run",
                json={"account_id": "sixuweilive", "dataset_id": "sixuweilive_20260628", "modality": "text", "limit": 2},
            ).json()
        with patch(
            "dso.api.main.qwen_omni_status",
            return_value={"contract_version": "qwen2_5_omni_7b_gptq_int4.shadow_v1", "status": "model_switch_required"},
        ), patch(
            "dso.api.main.run_qwen_omni_shadow",
            return_value={"contract_version": "qwen2_5_omni_7b_gptq_int4.shadow_v1", "status": "ready", "analyzed_count": 1},
        ):
            omni_status = self.client.get("/learning/qwen-omni/status").json()
            omni_shadow = self.client.post(
                "/learning/qwen-omni/shadow-run",
                json={"account_id": "sixuweilive", "dataset_id": "sixuweilive_20260628", "limit": 1},
            ).json()

        self.assertEqual(imported["contract_version"], DOUYIN_HISTORY_VERSION)
        self.assertEqual(imported["inserted"], 2)
        self.assertEqual(imported["source_row_count"], 2)
        self.assertEqual(imported["source_unique_count"], 2)
        self.assertEqual(imported["stored_sample_count"], 2)
        self.assertEqual(imported["label_counts"]["high"], 1)
        self.assertEqual(summary["stored_sample_count"], 2)
        self.assertEqual(summary["trainable_sample_count"], 2)
        self.assertEqual(summary["metric_coverage"]["likes"]["rate"], 1.0)
        self.assertEqual(summary["metric_coverage"]["favorites"]["rate"], 1.0)
        self.assertEqual(summary["metric_coverage"]["comments"]["rate"], 1.0)
        self.assertEqual(summary["metric_coverage"]["shares"]["rate"], 1.0)
        self.assertEqual(summary["play_missing_rate"], 1.0)
        self.assertEqual(datasets["stored_sample_count"], 2)
        self.assertEqual(dataset["stored_sample_count"], 2)
        self.assertEqual(dataset["trainable_sample_count"], 2)
        self.assertEqual(dataset["play_missing_count"], 2)
        self.assertEqual(dataset["interaction_coverage"]["likes"]["rate"], 1.0)
        self.assertEqual(coverage["status"], "ready")
        self.assertEqual(coverage["semantic_feature_version"], SEMANTIC_FEATURE_VERSION)
        self.assertEqual(coverage["research_label_version"], RESEARCH_LABEL_VERSION)
        self.assertEqual(coverage["coverage"]["content_category"]["rate"], 1.0)
        self.assertEqual(baselines["sample_count"], 2)
        self.assertTrue(any(item["dimension"] == "program_name" for item in baselines["top_signals"]))
        self.assertEqual(queue["filters"]["label"], "high")
        self.assertEqual(queue["filters"]["strategy"], "research_ranker_v2_4")
        self.assertIn("suggested_fields", queue["samples"][0])
        self.assertIn("recommended_fields", queue["samples"][0])
        self.assertIn("queue_reason", queue["samples"][0])
        self.assertIn("impact_reason", queue["samples"][0])
        self.assertEqual(patched["sample"]["classification_confidence"], "manual_verified")
        self.assertEqual(reopened["status"], "reopened")
        self.assertEqual(reopened["sample"]["classification_confidence"], "low")
        self.assertTrue(any(item["id"] == sample_id for item in reopened_queue["samples"]))
        self.assertEqual(labels["research_label_version"], RESEARCH_LABEL_VERSION)
        self.assertEqual(tuning["strategy"], "research_ranker_v2_4")
        self.assertTrue(tuning["trials"])
        self.assertIn(multimodal_plan["status"], {"ready", "empty"})
        self.assertGreaterEqual(multimodal_plan["sample_count"], 0)
        self.assertIn("plan_path", multimodal_plan)
        self.assertIn("asset_readiness", multimodal_validation)
        self.assertIn("promotion_gate", multimodal_validation)
        self.assertIn("feature_coverage", multimodal_feature)
        self.assertIn("strategy_comparison", multimodal_feature)
        self.assertEqual(qwen_build["status"], "ready")
        self.assertIn("embedding_coverage", qwen_evidence)
        self.assertEqual(omni_status["status"], "model_switch_required")
        self.assertEqual(omni_shadow["analyzed_count"], 1)
        self.assertIn("promotion_gate", tuning)


def _insert_segment() -> dict:
    now = "2026-06-23T00:00:00+00:00"
    video_id = "video_demo"
    segment_id = "seg_demo"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO source_videos
            (id, account_id, title, original_path, file_path, duration_seconds, width, height, fps, audio_streams, status, transcript_path, created_at, updated_at)
            VALUES (?, 'main', 'demo', '/tmp/demo.mp4', '/tmp/demo.mp4', 120, 1920, 1080, 25, 1, 'ingested', NULL, ?, ?)
            """,
            [video_id, now, now],
        )
        conn.execute(
            """
            INSERT INTO candidate_segments
            (id, source_video_id, performance_id, start_time, end_time, duration_seconds, transcript, summary, primary_topic, song_section_type,
             music_slice_type, emotion_type, short_video_structure, musical_moment, program_context, comment_trigger, cover_time, status, created_at)
            VALUES (?, ?, NULL, 10, 42, 32, '导师说这次改编第一次突破 副歌高音很强', 'demo summary', '音乐综艺', 'climax_candidate',
             '节目叙事到音乐爆点型', '热血', '节目上下文 -> 歌曲爆点 -> 现场反应', '副歌/高音/强节奏候选',
             '含节目叙事或导师/赛制信息', '可讨论这段改编/表现是否完成突破', 24, 'candidate', ?)
            """,
            [segment_id, video_id, now],
        )
        conn.commit()
    return {
        "id": segment_id,
        "source_video_id": video_id,
        "duration_seconds": 32.0,
        "start_time": 10.0,
        "end_time": 42.0,
    }


def _write_transcript(path: Path, video_id: str, segments: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "source": "whisper_cpp:base",
                "metadata": {
                    "backend": "whisper_cpp",
                    "segment_count_raw": len(segments),
                    "segment_count_processed": len(segments),
                    "postprocess_version": "test",
                    "cache_key": {
                        "whisper_cpp": {
                            "model_name": "base",
                            "vad_enabled": True,
                            "vad_model": "/tmp/ggml-silero.bin",
                            "extra_args": None,
                        }
                    },
                },
                "segments": segments,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with connect() as conn:
        conn.execute(
            "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
            [str(path), video_id],
        )
        conn.commit()
