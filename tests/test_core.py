from __future__ import annotations

import csv
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from html import escape
from unittest.mock import patch

from dso.api.dashboard import render_dashboard
from dso.artifacts import video_manifest
from dso.config import ensure_data_dirs
from dso.collectors.douyin_accounts import build_account_library, clean_account_api_works
from dso.collectors.douyin_classification import classify_published_work
from dso.collectors.douyin_media import collect_douyin_media
from dso.collectors.douyin_visible import clean_visible_snapshots
from dso.corrections.editor import create_performance, list_performances, update_candidate_segment, update_performance
from dso.db.session import connect, init_db
from dso.cli import _argparse_main, cmd_doctor, cmd_web
from dso.features.asr import _parse_whisper_cpp_json, post_process_segments, transcribe_video
from dso.features.asr_contract import asr_profile_plan
from dso.features.asr_verify import verify_candidate_asr
from dso.features.asr_profile import resolve_asr_model_list, resolve_asr_model_size
from dso.features.asr_routing import route_candidate_asr, route_video_asr
from dso.features.whisper_cpp import whisper_cpp_binary, whisper_cpp_language, whisper_cpp_model, whisper_cpp_ready
from dso.feedback.douyin import douyin_sync_summary, register_douyin_account, sync_douyin_feedback
from dso.feedback.douyin_auth import complete_douyin_qr_login, douyin_oauth_status, start_douyin_qr_login
from dso.feedback.importer import account_baselines, account_insights, import_metrics, list_training_samples
from dso.feedback.platform import create_platform_mapping, map_platform_metric_row
from dso.feedback.reward import compute_reward_proxy, feedback_signal_rates
from dso.learning.backtest import (
    RESEARCH_RANKER_V23_STRATEGY,
    RESEARCH_RANKER_V24_STRATEGY,
    _apply_v23_diversity,
    _score_v22_from_components,
    _v24_reliable_signal_row,
    backtest_rule_ranker,
    list_backtest_reports,
    run_ranker_tuning,
    semantic_feature_experiment,
)
from dso.learning.historical_samples import (
    douyin_history_baselines,
    backfill_semantic_features,
    import_douyin_history,
    import_historical_samples,
    historical_sample_summary,
    list_historical_samples,
    rebuild_research_labels,
    research_field_coverage,
    reopen_historical_sample_calibration,
    semantic_calibration_queue,
    update_historical_sample_labels,
)
from dso.learning.interest_clock import build_interest_clock, recommend_publish_hours
from dso.learning.memory import build_text_memory_bank, calibrate_segment_history
from dso.learning.multimodal_validation import (
    build_multimodal_collection_plan,
    collect_multimodal_assets,
    run_multimodal_feature_experiment,
    run_multimodal_validation,
)
from dso.learning.prototypes import build_prototype_bank, list_capture_datasets, list_prototype_bank, match_segment_prototypes
from dso.learning.slice_structure_evaluator import evaluate_slice_structure, evaluate_slice_structure_row
from dso.media.ffmpeg import probe_video
from dso.media.ingest import ingest_video
from dso.quality.insights import _has_repetition_noise, quality_insights
from dso.review import list_change_events, list_review_events, mark_candidate_review
from dso.runtime import runtime_diagnostics
from dso.scoring.rights import rights_risk_for_segment, set_rights
from dso.scoring.scorer import score_segment, suggestions
from dso.segments.generator import _candidate_row, _dedupe_and_rank, _from_transcript, generate_segments
from dso.simulation.recommender import simulate_segment, simulate_video
from dso.text.zh_hans import to_zh_hans
from dso.utils import run_cmd
from dso.variants.exporter import _overlapping_transcript, create_experiment, create_variant, export_preflight, export_segment, list_experiments, update_variant
from dso.versions import (
    FEEDBACK_INSIGHTS_VERSION,
    FEEDBACK_STATE_VERSION,
    METRICS_IMPORT_VERSION,
    PLATFORM_SYNC_VERSION,
    BACKTEST_VERSION,
    HISTORY_CALIBRATION_VERSION,
    INTEREST_CLOCK_VERSION,
    MEMORY_BANK_VERSION,
    PROTOTYPE_BANK_VERSION,
    QUALITY_GATE_VERSION,
    QUALITY_INSIGHTS_VERSION,
    RESEARCH_LABEL_VERSION,
    RESEARCH_RANKER_VERSION,
    SCORER_VERSION,
    SEGMENTER_VERSION,
    SEMANTIC_FEATURE_VERSION,
    MULTIMODAL_VALIDATION_VERSION,
    MULTIMODAL_FEATURE_VERSION,
    DOUYIN_HISTORY_VERSION,
)


class CoreWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["DSO_ROOT"] = str(self.root)
        os.environ.pop("DSO_RIGHTS_MODE", None)
        init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DSO_ROOT", None)
        os.environ.pop("DSO_RIGHTS_MODE", None)
        for name in [
            "DSO_ASR_BACKEND",
            "DSO_WHISPER_CPP_BIN",
            "DSO_WHISPER_CPP_MODEL",
            "DSO_WHISPER_CPP_MODEL_NAME",
            "DSO_WHISPER_MODEL",
            "DSO_ASR_PROFILE",
            "DSO_ASR_ROUTING",
            "DSO_WHISPER_CPP_EXTRA_ARGS",
            "DSO_WHISPER_LANGUAGE",
            "DSO_WHISPER_PROMPT",
            "DSO_WHISPER_HOTWORDS",
            "DSO_DOUYIN_CLIENT_KEY",
            "DSO_DOUYIN_CLIENT_SECRET",
            "DSO_DOUYIN_REDIRECT_URI",
            "DSO_DOUYIN_SCOPES",
        ]:
            os.environ.pop(name, None)

    def test_rights_defaults_to_trusted_sample_mode(self) -> None:
        segment = _insert_segment()
        risk, notes, allowed = rights_risk_for_segment(segment)
        self.assertTrue(allowed)
        self.assertEqual(risk, 0)
        self.assertIn("sample", notes[0])

    def test_strict_rights_rule_blocks_missing_and_allows_cleared(self) -> None:
        os.environ["DSO_RIGHTS_MODE"] = "strict"
        segment = _insert_segment()
        risk, notes, allowed = rights_risk_for_segment(segment)
        self.assertFalse(allowed)
        self.assertGreaterEqual(risk, 90)

        set_rights(
            "source_video",
            segment["source_video_id"],
            program="cleared",
            song="cleared",
            performance="cleared",
            artist="cleared",
            platforms="douyin",
            duration=60,
        )
        risk, notes, allowed = rights_risk_for_segment(segment)
        self.assertTrue(allowed)
        self.assertLess(risk, 50)

    def test_score_formula_uses_trusted_sample_without_rights_penalty(self) -> None:
        segment = _insert_segment()
        scored = score_segment(segment["id"])
        self.assertEqual(scored["rights_risk_score"], 0)
        self.assertIn("sample", scored["score_explanation"])

    def test_manual_performance_correction_upserts_song(self) -> None:
        segment = _insert_segment()

        performance = create_performance(
            segment["source_video_id"],
            {
                "song_title": "测试歌曲",
                "performer_name": "测试歌手",
                "start_time": 8,
                "end_time": 58,
                "stage_type": "竞演舞台",
                "arrangement_notes": "副歌前有导师点评铺垫",
            },
        )
        updated = update_performance(
            performance["id"],
            {
                "song_title": "测试歌曲",
                "performer_name": "测试歌手 A",
                "start_time": 9,
                "end_time": 60,
            },
        )
        rows = list_performances(segment["source_video_id"])

        self.assertEqual(updated["song_title"], "测试歌曲")
        self.assertEqual(updated["performer_name"], "测试歌手 A")
        self.assertEqual(updated["start_time"], 9)
        self.assertEqual(len(rows), 1)

    def test_candidate_correction_updates_boundaries_and_rescores(self) -> None:
        segment = _insert_segment()
        performance = create_performance(
            segment["source_video_id"],
            {
                "song_title": "爆点测试歌",
                "performer_name": "竞演歌手",
                "start_time": 0,
                "end_time": 90,
            },
        )

        corrected = update_candidate_segment(
            segment["id"],
            {
                "performance_id": performance["id"],
                "start_time": 12,
                "end_time": 50,
                "cover_time": 30,
                "transcript": "导师宣布晋级悬念后，副歌高音爆发，全场观众起立欢呼",
                "music_slice_type": "赛制悬念到音乐爆点型",
                "short_video_structure": "赛制悬念 -> 副歌高音 -> 现场反应",
                "program_context": "导师评价与晋级悬念已明确",
                "comment_trigger": "可讨论这次高音是否改变结果",
            },
        )

        self.assertEqual(corrected["performance_id"], performance["id"])
        self.assertEqual(corrected["status"], "corrected")
        self.assertEqual(corrected["duration_seconds"], 38)
        self.assertGreater(corrected["final_score"], 0)
        with connect() as conn:
            score = conn.execute(
                "SELECT final_score FROM slice_scores WHERE candidate_segment_id = ?",
                [segment["id"]],
            ).fetchone()
        self.assertIsNotNone(score)

    def test_candidate_review_and_change_log_track_manual_decisions(self) -> None:
        segment = _insert_segment()
        corrected = update_candidate_segment(
            segment["id"],
            {
                "start_time": 12,
                "end_time": 48,
                "cover_time": 28,
                "reason": "收紧到副歌前后",
                "operator": "tester",
            },
        )
        review = mark_candidate_review(segment["id"], "approved", reason="字幕与授权已确认", operator="tester")
        changes = list_change_events(segment_id=segment["id"])

        self.assertEqual(corrected["status"], "corrected")
        self.assertEqual(review["review_status"], "approved")
        self.assertGreaterEqual(changes["count"], 2)
        fields = {
            field
            for item in changes["changes"]
            for field in item["diff"].keys()
        }
        self.assertIn("start_time", fields)
        self.assertIn("status", fields)

    def test_repeated_review_status_with_same_reason_is_idempotent(self) -> None:
        segment = _insert_segment()

        first = mark_candidate_review(segment["id"], "blocked", reason="授权待确认", operator="tester")
        duplicate = mark_candidate_review(segment["id"], "blocked", reason="授权待确认", operator="tester")
        events = list_review_events(segment["id"])

        self.assertEqual(first["status"], "updated")
        self.assertEqual(duplicate["status"], "unchanged")
        self.assertEqual(events["count"], 1)
        self.assertEqual(events["events"][0]["review_status"], "blocked")

    def test_generator_builds_music_variety_arc_candidate(self) -> None:
        transcript = [
            {"start": 0, "end": 5, "text": "导师点评说这次选择会影响晋级"},
            {"start": 6, "end": 11, "text": "他说这首歌写给妈妈 是一路坚持的故事"},
            {"start": 18, "end": 24, "text": "副歌高音转调爆发 情绪直接推上去"},
            {"start": 25, "end": 31, "text": "全场观众起立欢呼 导师反应很激动"},
            {"start": 34, "end": 39, "text": "主持人继续串场"},
        ]
        frames = [{"time": second, "energy": 0.35} for second in range(0, 45)]

        candidates = _dedupe_and_rank(_from_transcript("video_demo", transcript, frames, 60))
        best = candidates[0]

        self.assertEqual(best["music_slice_type"], "综艺叙事爆点闭环型")
        self.assertIn("音乐爆点", best["short_video_structure"])
        self.assertIn("现场反应", best["short_video_structure"])
        self.assertIn("导师评价", best["program_context"])
        self.assertIn("现场反应", best["comment_trigger"])

    def test_generate_segments_returns_segmenter_version(self) -> None:
        segment = _insert_segment()
        transcript_path = self.root / "segmenter_transcript.json"
        transcript = [
            {"start": 0, "end": 5, "text": "导师点评说这次改编会影响晋级"},
            {"start": 6, "end": 11, "text": "歌手讲到一路坚持的故事"},
            {"start": 18, "end": 24, "text": "副歌高音转调爆发 情绪直接推上去"},
            {"start": 25, "end": 31, "text": "全场观众起立欢呼 导师反应很激动"},
        ]
        transcript_path.write_text(json.dumps({"segments": transcript}, ensure_ascii=False), encoding="utf-8")
        frames = [{"time": second, "energy": 0.35} for second in range(0, 60)]
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        with patch(
            "dso.segments.generator.extract_audio_features",
            return_value={"frames": frames, "peaks": []},
        ):
            rows = generate_segments(segment["source_video_id"], top_k=5)

        self.assertTrue(rows)
        self.assertEqual(rows[0]["segmenter_version"], SEGMENTER_VERSION)

    def test_dedupe_rank_penalizes_sponsor_read_candidates(self) -> None:
        clean = _candidate_fixture(
            "clean",
            0,
            "导师点评这次改编很突破 副歌高音爆发 全场欢呼",
            "含导师评价等节目上下文",
        )
        ad = _candidate_fixture(
            "ad",
            80,
            "歌手2025超级合作伙伴vivo提醒您 王老吉销量第一 怕上火喝王老吉",
            "疑似品牌/广告口播密集，建议只作为上下文补充",
        )

        ranked = _dedupe_and_rank([ad, clean])

        self.assertEqual(ranked[0]["id"], "clean")

    def test_generator_varies_structure_moment_and_tags_by_transcript(self) -> None:
        rows = [
            _candidate_row("video_demo", 0, 24, "到歌手2025的 第一个舞台 他选择了自己的代表", 0.36),
            _candidate_row("video_demo", 30, 54, "这首歌已经发行了21年 你是不是也会想起自己最初的梦想", 0.34),
            _candidate_row("video_demo", 60, 84, "本场最终的胜负排名会不会改变 结果马上公布", 0.32),
            _candidate_row("video_demo", 90, 114, "我个人建议给我们的合声老师加薪 他们已经完全不输给乐队", 0.38),
        ]

        self.assertGreaterEqual(len({row["short_video_structure"] for row in rows}), 4)
        self.assertGreaterEqual(len({row["musical_moment"] for row in rows}), 4)
        self.assertGreaterEqual(len({row["music_slice_type"] for row in rows}), 4)
        self.assertGreaterEqual(len({row["emotion_type"] for row in rows}), 4)
        self.assertIn("首秀", rows[0]["short_video_structure"] + rows[0]["music_slice_type"])
        self.assertIn("歌曲记忆", rows[1]["short_video_structure"] + rows[1]["music_slice_type"])
        self.assertIn("赛制", rows[2]["short_video_structure"] + rows[2]["music_slice_type"])
        self.assertIn("导师评价", rows[3]["short_video_structure"] + rows[3]["music_slice_type"])

    def test_parse_whisper_cpp_json_offsets_and_timestamps(self) -> None:
        path = self.root / "whisper_cpp.json"
        path.write_text(
            json.dumps(
                {
                    "transcription": [
                        {
                            "offsets": {"from": 1200, "to": 3450},
                            "text": "第一句真实字幕",
                        },
                        {
                            "timestamps": {"from": "00:00:04,000", "to": "00:00:06,500"},
                            "text": "第二句真实字幕",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        segments = _parse_whisper_cpp_json(path)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start"], 1.2)
        self.assertEqual(segments[0]["end"], 3.45)
        self.assertEqual(segments[1]["start"], 4.0)
        self.assertEqual(segments[1]["end"], 6.5)

    def test_whisper_cpp_uses_project_local_backend_by_default(self) -> None:
        binary = self.root / "tools/whisper.cpp/build/bin/whisper-cli"
        model = self.root / "data/models/whisper.cpp/ggml-base.bin"
        binary.parent.mkdir(parents=True, exist_ok=True)
        model.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        model.write_text("model", encoding="utf-8")

        self.assertEqual(whisper_cpp_binary(), str(binary.resolve()))
        self.assertEqual(whisper_cpp_model(), str(model.resolve()))
        self.assertEqual(whisper_cpp_language(), "zh")
        self.assertTrue(whisper_cpp_ready())

    def test_asr_profiles_resolve_quality_and_compare_models(self) -> None:
        self.assertEqual(resolve_asr_model_size(profile="fast"), "base")
        self.assertEqual(resolve_asr_model_size(profile="quality"), "small")
        self.assertEqual(resolve_asr_model_size(profile="verify"), "large-v3-turbo-q5_0")
        self.assertEqual(resolve_asr_model_list(profile="compare"), ["base", "small"])
        plan = asr_profile_plan()
        self.assertEqual(plan["profiles_by_name"]["fast"]["model"], "base")
        self.assertEqual(plan["profiles_by_name"]["quality"]["model"], "small")
        self.assertEqual(plan["profiles_by_name"]["verify"]["model"], "large-v3-turbo-q5_0")

        os.environ["DSO_ASR_PROFILE"] = "quality"
        self.assertEqual(resolve_asr_model_size(), "small")

        os.environ["DSO_WHISPER_MODEL"] = "base"
        self.assertEqual(resolve_asr_model_size(profile="quality"), "base")

    def test_asr_routing_sends_chinese_risk_to_verify_and_preserves_english_quality(self) -> None:
        risky_segment = {
            "id": "seg_risky",
            "duration_seconds": 54,
            "transcript": (
                "陈楚生和张韶涵听完范玮琪这次竞演后，导师讨论排名和晋级结果，"
                "他说这是第一次把妈妈和一路坚持的故事唱进副歌。"
            ),
            "music_slice_type": "赛制悬念到音乐爆点型",
            "short_video_structure": "导师评价 -> 排名悬念 -> 副歌高音",
            "program_context": "含导师评价、竞演排名和晋级上下文",
            "comment_trigger": "可讨论这次排名是否公平",
        }
        english_segment = {
            "id": "seg_english",
            "duration_seconds": 32,
            "transcript": (
                "Grace introduces the English song The Show and says keep proving yourself, "
                "keep stepping it up and bring something fresh to the stage."
            ),
            "music_slice_type": "英文歌手介绍",
            "program_context": "英文歌名和英文介绍需要保留 small 结果人工复核",
        }

        risky_route = route_candidate_asr(
            risky_segment,
            transcript_summary={"profile": "quality", "model_size": "small"},
            issues=[{"key": "asr_repetition_noise"}],
            requested_profile="auto",
        )
        english_route = route_candidate_asr(
            english_segment,
            transcript_summary={"profile": "quality", "model_size": "small"},
            issues=[],
            requested_profile="auto",
        )

        self.assertEqual(risky_route["decision"], "verify_candidate")
        self.assertEqual(risky_route["recommended_profile"], "verify")
        self.assertIn("asr_quality_risk", risky_route["reason_keys"])
        self.assertIn("person_name_dense", risky_route["reason_keys"])
        self.assertTrue(risky_route["preserve_quality_result"])
        self.assertEqual(english_route["decision"], "keep_quality_for_english")
        self.assertEqual(english_route["recommended_profile"], "quality")
        self.assertEqual(english_route["recommended_model"], "small")
        self.assertTrue(english_route["preserve_quality_result"])

    def test_asr_video_routing_recommends_quality_for_base_quality_risk(self) -> None:
        route = route_video_asr(
            {"status": "transcribed"},
            transcript_summary={
                "source": "whisper_cpp:base",
                "path": "/tmp/transcript.json",
                "segment_count": 12,
                "profile": "fast",
                "model_size": "base",
            },
            issues=[{"key": "asr_repetition_noise"}],
        )

        self.assertEqual(route["decision"], "rerun_full_video_quality")
        self.assertEqual(route["recommended_profile"], "quality")
        self.assertEqual(route["recommended_model"], "small")
        self.assertIn("base_quality_risk", route["reason_keys"])

    def test_post_process_segments_fixes_hotwords_and_marks_ads(self) -> None:
        segments = post_process_segments(
            [
                {"start": 0, "end": 0.8, "text": " 王老级 "},
                {"start": 1.0, "end": 1.4, "text": "提醒您合作伙伴白确灵"},
                {"start": 2.0, "end": 3.2, "text": "這次我壓力很大 你們這個節目太會搞事情"},
                {"start": 4.0, "end": 5.2, "text": "最初的夢想學律想起的時候 現場掌聲很多"},
            ]
        )

        self.assertEqual(segments[0]["text"], "王老吉")
        self.assertEqual(segments[1]["text"], "提醒您合作伙伴白雀羚")
        self.assertEqual(segments[1]["tags"], ["ad_read"])
        self.assertEqual(segments[2]["text"], "这次我压力很大 你们这个节目太会搞事情")
        self.assertEqual(segments[3]["text"], "最初的梦想旋律想起的时候 现场掌声很多")

    def test_transcribe_video_reuses_matching_asr_cache(self) -> None:
        segment = _insert_segment()

        def fake_extract(_video_path: Path, wav_path: Path) -> Path:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"same audio")
            return wav_path

        with patch("dso.features.asr.extract_audio", side_effect=fake_extract), patch(
            "dso.features.asr._try_configured_asr",
            return_value=([{"start": 0, "end": 1, "text": "导师点评"}], "fake_asr"),
        ) as asr_mock:
            first = transcribe_video(segment["source_video_id"])
            second = transcribe_video(segment["source_video_id"])

        self.assertFalse((first.get("metadata") or {}).get("cache_hit"))
        self.assertTrue(second["cache_hit"])
        self.assertEqual(asr_mock.call_count, 1)

    def test_transcribe_video_quality_profile_uses_small_model(self) -> None:
        segment = _insert_segment()
        seen_models = []

        def fake_extract(_video_path: Path, wav_path: Path) -> Path:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"quality profile audio")
            return wav_path

        def fake_asr(_audio_path: Path, _transcript_dir: Path, model_size: str, *, backend: str | None = None) -> tuple[list[dict], str]:
            seen_models.append(model_size)
            return ([{"start": 0, "end": 1, "text": "导师点评"}], f"fake_asr:{model_size}")

        with patch("dso.features.asr.extract_audio", side_effect=fake_extract), patch(
            "dso.features.asr._try_configured_asr",
            side_effect=fake_asr,
        ):
            result = transcribe_video(segment["source_video_id"], asr_profile="quality", force=True)

        self.assertEqual(seen_models, ["small"])
        self.assertEqual(result["source"], "fake_asr:small")
        self.assertEqual(result["metadata"]["profile"], "quality")
        self.assertEqual(result["metadata"]["model_size"], "small")

    def test_verify_candidate_asr_writes_comparison_artifact(self) -> None:
        segment = _insert_segment()
        transcript_path = self.root / "verify_source.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "segments": [
                        {"start": 8, "end": 16, "text": "导师说这次改编第一次突破"},
                        {"start": 16, "end": 30, "text": "副歌高音很强"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ? WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        def fake_extract(_video_path: Path, wav_path: Path, _start: float, _end: float) -> Path:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"fake wav")
            return wav_path

        with patch("dso.features.asr_verify._extract_segment_audio", side_effect=fake_extract), patch(
            "dso.features.asr_verify.transcribe_audio_file",
            return_value={
                "source": "fake_asr:verify",
                "segments": [{"start": 0, "end": 5, "text": "导师说这次改编首次突破 副歌高音很强"}],
                "metadata": {"backend": "fake", "profile": "verify", "model_size": "large-v3-turbo-q5_0"},
            },
        ):
            result = verify_candidate_asr(segment["id"], backend="fake", force=True)

        self.assertEqual(result["contract_version"], "asr_verify.v1")
        self.assertEqual(result["profile"], "verify")
        self.assertTrue(Path(result["verified"]["path"]).is_file())
        self.assertTrue(Path(result["record"]["artifact_path"]).is_file())
        self.assertGreater(result["difference_score"], 0)

    def test_verify_candidate_asr_auto_profile_preserves_english_quality(self) -> None:
        segment = _insert_segment()
        with connect() as conn:
            conn.execute(
                """
                UPDATE candidate_segments
                SET transcript = ?,
                    music_slice_type = '英文歌手介绍',
                    program_context = '英文歌名和英文介绍需要保留 small 结果人工复核'
                WHERE id = ?
                """,
                [
                    (
                        "Grace introduces the English song The Show and says keep proving yourself, "
                        "keep stepping it up and bring something fresh to the stage."
                    ),
                    segment["id"],
                ],
            )
            conn.commit()

        def fake_extract(_video_path: Path, wav_path: Path, _start: float, _end: float) -> Path:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"fake wav")
            return wav_path

        seen = {}

        def fake_transcribe(
            _audio_path: Path,
            _transcript_dir: Path,
            *,
            model_size: str | None = None,
            asr_profile: str | None = None,
            backend: str | None = None,
            routing_context: dict | None = None,
        ) -> dict:
            seen.update(
                {
                    "model_size": model_size,
                    "asr_profile": asr_profile,
                    "backend": backend,
                    "routing_context": routing_context,
                }
            )
            return {
                "source": "fake_asr:quality",
                "segments": [{"start": 0, "end": 5, "text": "Grace introduces The Show"}],
                "metadata": {"backend": "fake", "profile": asr_profile, "model_size": model_size},
            }

        with patch("dso.features.asr_verify._extract_segment_audio", side_effect=fake_extract), patch(
            "dso.features.asr_verify.transcribe_audio_file",
            side_effect=fake_transcribe,
        ):
            result = verify_candidate_asr(segment["id"], asr_profile="auto", backend="fake", force=True)

        self.assertEqual(seen["asr_profile"], "quality")
        self.assertEqual(seen["model_size"], "small")
        self.assertEqual(result["profile"], "quality")
        self.assertEqual(result["model_name"], "small")
        self.assertEqual(result["routing"]["decision"], "keep_quality_for_english")
        self.assertTrue(result["routing"]["preserve_quality_result"])

    def test_score_explanation_surfaces_recommendation_proxy_signals(self) -> None:
        segment = _insert_segment()
        scored = score_segment(segment["id"])

        self.assertIn("首5秒留存", scored["score_explanation"])
        self.assertIn("上下文完整度", scored["score_explanation"])
        self.assertIn("低原创/负反馈风险", scored["score_explanation"])
        self.assertEqual(scored["scorer_version"], SCORER_VERSION)
        self.assertIn(SCORER_VERSION, scored["score_explanation"])
        self.assertLess(scored["low_originality_score"], 25)

    def test_score_copy_uses_segment_specific_transcript(self) -> None:
        segment = _insert_segment()
        now = "2026-06-23T00:00:00+00:00"
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO candidate_segments
                (id, source_video_id, performance_id, start_time, end_time, duration_seconds, transcript, summary, primary_topic, song_section_type,
                 music_slice_type, emotion_type, short_video_structure, musical_moment, program_context, comment_trigger, cover_time, status, created_at)
                VALUES ('seg_nervous', ?, NULL, 60, 84, 24, '第一次參加這麼棒的節目 說不緊張是騙人的', 'nervous demo', '音乐综艺', 'context_or_build',
                 '节目叙事型', '舞台表现', '铺垫信息 -> 舞台表现 -> 结果/反应', '歌曲铺垫/情绪段候选',
                 '含首次登台/舞台压力等节目上下文', '可讨论第一次登台的紧张感是否让后续舞台更有代入感', 70, 'candidate', ?)
                """,
                [segment["source_video_id"], now],
            )
            conn.commit()

        scored_a = score_segment(segment["id"])
        scored_b = score_segment("seg_nervous")
        title_a = json.loads(scored_a["title_suggestions"])[0]
        title_b = json.loads(scored_b["title_suggestions"])[0]

        self.assertNotEqual(title_a, title_b)
        self.assertIn("片段看点", scored_b["score_explanation"])
        self.assertIn("第一次", title_b + scored_b["score_explanation"])

    def test_title_suggestions_do_not_include_evaluation_prompt(self) -> None:
        segment = _insert_segment()
        with connect() as conn:
            conn.execute(
                """
                UPDATE candidate_segments
                SET transcript = '最初的梦想学律想起的时候 我们现场好多朋友跟她一起合唱',
                    music_slice_type = '歌词共鸣型',
                    emotion_type = '遗憾',
                    short_video_structure = '歌词共鸣 -> 舞台特写 -> 评论触发',
                    musical_moment = '歌曲情绪段候选',
                    program_context = '含歌词共鸣和现场合唱上下文',
                    comment_trigger = '可讨论这首歌为什么让人想起青春'
                WHERE id = ?
                """,
                [segment["id"]],
            )
            conn.commit()

        scored = score_segment(segment["id"])
        titles = json.loads(scored["title_suggestions"])

        self.assertTrue(titles)
        self.assertFalse(any("为什么值得" in title or "单独切出来" in title for title in titles))
        self.assertTrue(any("看点" in title or "遗憾" in title or "歌词" in title for title in titles))

        with connect() as conn:
            conn.execute(
                "UPDATE slice_scores SET title_suggestions = ? WHERE candidate_segment_id = ?",
                [json.dumps(["最初的梦想，这段为什么值得单独切出来"], ensure_ascii=False), segment["id"]],
            )
            conn.commit()

        rows = suggestions(segment["source_video_id"], top_k=1)
        self.assertEqual(rows[0]["title_suggestions"], ["最初的梦想"])

    def test_recommendation_simulator_builds_stage_flow(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])

        single = simulate_segment(segment["id"])
        self.assertEqual(single["segment_id"], segment["id"])
        self.assertIn("simulated_score", single)
        self.assertEqual(len(single["stage_flow"]), 6)
        self.assertIn("bottleneck", single)
        self.assertTrue(single["audience_clusters"])
        self.assertTrue(single["actions"])

        video = simulate_video(segment["source_video_id"], top_k=1)
        self.assertEqual(video["count"], 1)
        self.assertEqual(video["simulations"][0]["segment_id"], segment["id"])
        self.assertIn("avg_score", video["summary"])

    def test_quality_insights_flags_asr_and_queue_risks(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        transcript_path = self.root / "transcript.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "source": "whisper_cpp:base",
                    "metadata": {
                        "backend": "whisper_cpp",
                        "segment_count_raw": 3,
                        "segment_count_processed": 3,
                        "postprocess_version": "test",
                        "cache_key": {"whisper_cpp": {"extra_args": None}},
                    },
                    "segments": [
                        {"index": 0, "start": 0, "end": 12, "text": "我爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱你"},
                        {"index": 1, "start": 12, "end": 20, "text": "歌手2025超级合作伙伴vivo提醒您"},
                        {"index": 2, "start": 20, "end": 30, "text": "导师点评这次改编很突破"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                """
                UPDATE candidate_segments
                SET transcript = transcript || ' 超级合作伙伴vivo提醒您'
                WHERE id = ?
                """,
                [segment["id"]],
            )
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        report = quality_insights(segment["source_video_id"], top_k=5)
        issue_keys = {issue["key"] for issue in report["issues"]}

        self.assertEqual(report["transcript"]["source"], "whisper_cpp:base")
        self.assertEqual(report["transcript"]["backend"], "whisper_cpp")
        self.assertGreaterEqual(report["transcript"]["repetition_noise_count"], 1)
        self.assertGreaterEqual(report["queue"]["sponsor_risk_count"], 1)
        self.assertIn("asr_repetition_noise", issue_keys)
        self.assertIn("whisper_cpp_base_no_vad", issue_keys)
        self.assertIn("sponsor_risk", issue_keys)
        self.assertEqual(report["gate"]["status"], "review")
        self.assertIn("asr_repetition_noise", {reason["key"] for reason in report["gate"]["reasons"]})
        self.assertIn("sponsor_risk", report["gate"]["review_issue_keys"])
        self.assertFalse(report["gate"]["blocking_issue_keys"])
        self.assertTrue(report["watchlist"])
        self.assertTrue(report["actions"])
        self.assertTrue(report["simulation"]["available"])
        self.assertIn("review", {item["decision"] for item in report["simulation"]["decisions"]})
        self.assertEqual(report["asr_routing"]["video"]["decision"], "rerun_full_video_quality")
        self.assertEqual(report["asr_routing"]["video"]["recommended_profile"], "quality")
        self.assertGreaterEqual(report["asr_routing"]["verify_count"], 1)
        self.assertTrue(report["asr_routing"]["verify_queue"])

    def test_quality_repetition_noise_does_not_penalize_english_singer_context(self) -> None:
        english_context = (
            "The rankings are temporary, you have to keep proving yourself, keep stepping it up "
            "and just keep getting creative. It's important to bring something fresh to the stage."
        )
        repeated_noise = "thank you " * 6

        self.assertFalse(_has_repetition_noise(english_context))
        self.assertTrue(_has_repetition_noise(repeated_noise))

    def test_golden_quality_fixture_locks_release_gate_signals(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        transcript_path = self.root / "golden_quality_transcript.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "source": "whisper_cpp:base",
                    "metadata": {
                        "backend": "whisper_cpp",
                        "segment_count_raw": 3,
                        "segment_count_processed": 3,
                        "postprocess_version": "golden",
                        "cache_key": {"whisper_cpp": {"model_name": "base", "extra_args": None}},
                    },
                    "segments": [
                        {"index": 0, "start": 0, "end": 10, "text": "我爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱你"},
                        {"index": 1, "start": 10, "end": 18, "text": "歌手2025超级合作伙伴vivo提醒您"},
                        {"index": 2, "start": 18, "end": 30, "text": "副歌高音爆发 全场观众起立欢呼"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                """
                UPDATE candidate_segments
                SET transcript = transcript || ' 超级合作伙伴vivo提醒您'
                WHERE id = ?
                """,
                [segment["id"]],
            )
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        report = quality_insights(segment["source_video_id"], top_k=5)
        issue_keys = {issue["key"] for issue in report["issues"]}

        self.assertEqual(report["contract_version"], QUALITY_INSIGHTS_VERSION)
        self.assertEqual(report["component_versions"]["quality_gate"], QUALITY_GATE_VERSION)
        self.assertEqual(report["component_versions"]["segmenter"], SEGMENTER_VERSION)
        self.assertEqual(report["component_versions"]["scorer"], SCORER_VERSION)
        self.assertEqual(report["gate"]["signals"]["rights_mode"], "trusted_sample")
        self.assertGreaterEqual(report["transcript"]["repetition_noise_count"], 1)
        self.assertGreaterEqual(report["transcript"]["ad_read_count"], 1)
        self.assertGreaterEqual(report["queue"]["sponsor_risk_count"], 1)
        self.assertGreaterEqual(report["queue"]["closed_loop_count"], 1)
        self.assertIn("asr_repetition_noise", issue_keys)
        self.assertIn("sponsor_risk", issue_keys)
        self.assertEqual(report["gate"]["status"], "review")
        self.assertIn("review", {item["decision"] for item in report["simulation"]["decisions"]})

    def test_quality_insights_flags_transcript_ad_reads_even_when_top_queue_is_clean(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        transcript_path = self.root / "transcript_ad_read_only.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "source": "whisper_cpp:base",
                    "metadata": {
                        "backend": "whisper_cpp",
                        "segment_count_raw": 2,
                        "segment_count_processed": 2,
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
                    "segments": [
                        {"index": 0, "start": 0, "end": 8, "text": "歌手2025超级合作伙伴vivo提醒您"},
                        {"index": 1, "start": 8, "end": 16, "text": "导师点评这次改编很突破"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        report = quality_insights(segment["source_video_id"], top_k=5)
        issue_keys = {issue["key"] for issue in report["issues"]}

        self.assertGreaterEqual(report["transcript"]["ad_read_count"], 1)
        self.assertEqual(report["queue"]["sponsor_risk_count"], 0)
        self.assertIn("transcript_ad_reads", issue_keys)
        self.assertNotIn("sponsor_risk", issue_keys)
        self.assertEqual(report["gate"]["status"], "review")
        self.assertIn("transcript_ad_reads", report["gate"]["review_issue_keys"])
        self.assertIn("抽查字幕", " ".join(report["actions"]))

    def test_quality_insights_recognizes_whisper_cpp_vad_metadata(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        transcript_path = self.root / "transcript_vad.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "source": "whisper_cpp:base",
                    "metadata": {
                        "backend": "whisper_cpp",
                        "segment_count_raw": 2,
                        "segment_count_processed": 2,
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
                    "segments": [
                        {"index": 0, "start": 0, "end": 8, "text": "我爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱爱你"},
                        {"index": 1, "start": 8, "end": 16, "text": "导师点评这次改编很突破"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        report = quality_insights(segment["source_video_id"], top_k=5)
        issue_keys = {issue["key"] for issue in report["issues"]}

        self.assertTrue(report["transcript"]["whisper_cpp_vad_enabled"])
        self.assertEqual(report["transcript"]["whisper_cpp_vad_model"], "/tmp/ggml-silero.bin")
        self.assertNotIn("whisper_cpp_base_no_vad", issue_keys)
        self.assertIn("asr_repetition_noise", issue_keys)
        self.assertEqual(report["gate"]["status"], "review")
        self.assertIn("asr_repetition_noise", report["gate"]["review_issue_keys"])
        self.assertNotEqual(report["health"]["level"], "good")
        self.assertIn("已启用 VAD", " ".join(report["actions"]))

    def test_quality_insights_links_stable_high_potential_to_export_preview(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        transcript_path = self.root / "transcript_clean.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "source": "whisper_cpp:base",
                    "metadata": {
                        "backend": "whisper_cpp",
                        "segment_count_raw": 2,
                        "segment_count_processed": 2,
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
                    "segments": [
                        {"index": 0, "start": 0, "end": 8, "text": "导师点评这次改编很突破"},
                        {"index": 1, "start": 8, "end": 18, "text": "副歌高音爆发 全场观众欢呼"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        report = quality_insights(segment["source_video_id"], top_k=5)
        decisions = report["simulation"]["decisions"]

        self.assertEqual(report["health"]["level"], "good")
        self.assertEqual(report["gate"]["status"], "allow")
        self.assertEqual(report["gate"]["severity"], "ok")
        self.assertEqual(report["gate"]["version"], QUALITY_GATE_VERSION)
        self.assertEqual(report["contract_version"], QUALITY_INSIGHTS_VERSION)
        self.assertEqual(report["query"]["top_k"], 5)
        self.assertEqual(report["query"]["simulation_top_k"], 5)
        self.assertEqual(report["component_versions"]["quality_gate"], QUALITY_GATE_VERSION)
        self.assertEqual(report["component_versions"]["segmenter"], SEGMENTER_VERSION)
        self.assertEqual(report["component_versions"]["scorer"], SCORER_VERSION)
        self.assertEqual(report["gate"]["signals"]["rights_mode"], "trusted_sample")
        self.assertIn("导出", report["gate"]["summary"])
        self.assertEqual(report["gate"]["primary_action"]["kind"], "export_preview")
        self.assertIn("export_preview", report["gate"]["allowed_actions"])
        self.assertEqual(report["gate"]["blocking_issue_keys"], [])
        self.assertTrue(report["simulation"]["available"])
        self.assertEqual(decisions[0]["segment_id"], segment["id"])
        self.assertEqual(decisions[0]["decision"], "export_preview")
        self.assertGreaterEqual(report["simulation"]["summary"]["ready_to_export_count"], 1)
        self.assertIn("优先导出", " ".join(report["simulation"]["actions"]))

    def test_quality_gate_blocks_high_rights_risk_without_changing_export_logic(self) -> None:
        os.environ["DSO_RIGHTS_MODE"] = "strict"
        segment = _insert_segment()
        transcript_path = self.root / "transcript_rights_block.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "source": "whisper_cpp:base",
                    "metadata": {
                        "backend": "whisper_cpp",
                        "segment_count_raw": 2,
                        "segment_count_processed": 2,
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
                    "segments": [
                        {"index": 0, "start": 0, "end": 8, "text": "导师点评这次改编很突破"},
                        {"index": 1, "start": 8, "end": 18, "text": "副歌高音爆发 全场观众欢呼"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        scored = score_segment(segment["id"])
        report = quality_insights(segment["source_video_id"], top_k=5)

        self.assertGreaterEqual(scored["rights_risk_score"], 80)
        self.assertEqual(report["gate"]["status"], "block")
        self.assertEqual(report["gate"]["severity"], "risk")
        self.assertIn("rights_risk_block", report["gate"]["blocking_issue_keys"])
        self.assertGreaterEqual(report["queue"]["max_rights_risk_score"], 80)
        self.assertEqual(report["gate"]["enforcement"], "read_only")

    def test_audio_only_candidate_gets_low_originality_penalty(self) -> None:
        _insert_segment()
        audio_only = _insert_audio_only_segment()
        scored = score_segment(audio_only["id"])

        self.assertGreater(scored["low_originality_score"], 45)
        self.assertLess(scored["short_video_hook_score"], 60)

    def test_reward_proxy_uses_short_video_feedback_signals(self) -> None:
        metrics = {
            "views": 500,
            "impressions": 1000,
            "avg_watch_ratio": 0.7,
            "five_second_retention": 0.8,
            "completion_rate": 0.55,
            "likes": 20,
            "comments": 5,
            "favorites": 10,
            "shares": 5,
            "follows": 5,
            "negative_feedback": 2,
        }
        rates = feedback_signal_rates(metrics)
        self.assertEqual(rates["play_conversion_rate"], 0.5)
        self.assertEqual(rates["engagement_rate"], 0.08)
        self.assertEqual(rates["follow_rate"], 0.01)
        self.assertEqual(rates["negative_feedback_rate"], 0.004)

        reward, components = compute_reward_proxy(metrics)
        worse_reward, _components = compute_reward_proxy({**metrics, "negative_feedback": 30})
        self.assertGreater(reward, worse_reward)
        self.assertEqual(components["play_conversion_rate"], 0.5)
        self.assertEqual(components["engagement_rate"], 0.08)

    def test_metrics_import(self) -> None:
        csv_path = self.root / "metrics.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["candidate_segment_id", "views", "avg_watch_ratio", "completion_rate", "likes"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "candidate_segment_id": "seg_demo",
                    "views": "1000",
                    "avg_watch_ratio": "0.82",
                    "completion_rate": "0.61",
                    "likes": "80",
                }
            )
        result = import_metrics(csv_path)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["contract_version"], METRICS_IMPORT_VERSION)
        self.assertEqual(result["status"], "import_completed_with_warnings")
        self.assertEqual(result["row_summary"]["unlinked_rows"], 1)
        self.assertEqual(result["training_eligibility"]["eligible_rows"], 0)
        self.assertIn("avg_watch_ratio", result["input_contract"]["ratio_fields"])
        self.assertEqual(result["feedback_state"]["rebuilt_training_samples"], 0)
        self.assertFalse(result["row_issues"][0]["training_eligible"])

    def test_account_insights_empty_response_has_stable_contract(self) -> None:
        insights = account_insights("main")

        self.assertEqual(insights["contract_version"], FEEDBACK_INSIGHTS_VERSION)
        self.assertEqual(insights["status"], "empty")
        self.assertEqual(insights["account_id"], "main")
        self.assertEqual(insights["sample_count"], 0)
        self.assertIn("reward_proxy", insights["metric_notes"])
        self.assertEqual(insights["top_signals"]["slice_type"], None)
        self.assertEqual(insights["rankings"]["slice_type"], [])
        self.assertEqual(insights["by_slice_type"], {})

    def test_metrics_import_builds_snapshots_training_samples_and_baselines(self) -> None:
        segment = _insert_segment()
        csv_path = self.root / "metrics_with_segment.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "candidate_segment_id",
                    "window_name",
                    "hours_since_publish",
                    "views",
                    "impressions",
                    "avg_watch_ratio",
                    "five_second_retention",
                    "completion_rate",
                    "rewatch_rate",
                    "likes",
                    "comments",
                    "favorites",
                    "shares",
                    "follows",
                    "negative_feedback",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "candidate_segment_id": segment["id"],
                    "window_name": "24h",
                    "hours_since_publish": "24",
                    "views": "1000",
                    "impressions": "2200",
                    "avg_watch_ratio": "82%",
                    "five_second_retention": "91%",
                    "completion_rate": "61%",
                    "rewatch_rate": "12%",
                    "likes": "80",
                    "comments": "35",
                    "favorites": "42",
                    "shares": "18",
                    "follows": "9",
                    "negative_feedback": "3",
                }
            )
        result = import_metrics(csv_path)
        self.assertEqual(result["snapshots"], 1)
        self.assertEqual(result["training_samples"], 1)
        self.assertGreater(result["baselines"], 0)
        self.assertEqual(result["status"], "import_completed")
        self.assertEqual(result["row_summary"]["linked_rows"], 1)
        self.assertEqual(result["row_summary"]["unlinked_rows"], 0)
        self.assertEqual(result["training_eligibility"]["eligible_rows"], 1)
        self.assertEqual(result["feedback_state"]["rebuilt_training_samples"], 1)
        self.assertGreater(result["feedback_state"]["rebuilt_baselines"], 0)

        samples = list_training_samples(account_id="main")
        self.assertEqual(len(samples), 1)
        self.assertGreater(samples[0]["reward_proxy"], 0)
        baselines = account_baselines("main")
        self.assertTrue(any(row["metric_name"] == "reward_proxy" for row in baselines))
        self.assertTrue(any(row["metric_name"] == "play_conversion_rate" for row in baselines))

        with connect() as conn:
            row = conn.execute("SELECT avg_watch_ratio, reward_proxy FROM performance_metrics").fetchone()
        self.assertAlmostEqual(row["avg_watch_ratio"], 0.82)
        self.assertGreater(row["reward_proxy"], 0)

        insights = account_insights("main")
        self.assertEqual(insights["contract_version"], FEEDBACK_INSIGHTS_VERSION)
        self.assertEqual(insights["status"], "ready")
        self.assertEqual(insights["account_id"], "main")
        self.assertEqual(insights["sample_count"], 1)
        self.assertIn("by_structure", insights)
        self.assertIn("program_context_hook", insights["by_hook_type"])
        self.assertEqual(insights["by_slice_type"]["节目叙事到音乐爆点型"]["play_conversion_rate"], 0.4545)
        self.assertEqual(insights["top_signals"]["duration_bucket"]["name"], "medium")

    def test_metrics_import_accepts_xlsx_rows(self) -> None:
        segment = _insert_segment()
        xlsx_path = self.root / "metrics_with_segment.xlsx"
        _write_xlsx_rows(
            xlsx_path,
            "指标",
            [
                ["candidate_segment_id", "window_name", "views", "impressions", "avg_watch_ratio", "completion_rate", "likes"],
                [segment["id"], "24h", "1000", "2200", "82%", "61%", "80"],
            ],
        )

        result = import_metrics(xlsx_path)

        self.assertEqual(result["status"], "import_completed")
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["row_summary"]["linked_rows"], 1)
        self.assertEqual(result["training_samples"], 1)
        self.assertIn("xlsx", result["input_contract"]["file_formats"])

    def test_metrics_import_reports_mixed_linked_and_unlinked_rows(self) -> None:
        segment = _insert_segment()
        csv_path = self.root / "metrics_mixed.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["candidate_segment_id", "views", "impressions", "avg_watch_ratio", "completion_rate"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "candidate_segment_id": segment["id"],
                    "views": "1000",
                    "impressions": "2000",
                    "avg_watch_ratio": "70%",
                    "completion_rate": "55%",
                }
            )
            writer.writerow(
                {
                    "candidate_segment_id": "seg_missing",
                    "views": "500",
                    "impressions": "1200",
                    "avg_watch_ratio": "40%",
                    "completion_rate": "20%",
                }
            )

        result = import_metrics(csv_path)

        self.assertEqual(result["status"], "import_completed_with_warnings")
        self.assertEqual(result["row_summary"]["total_rows"], 2)
        self.assertEqual(result["row_summary"]["imported_metrics"], 2)
        self.assertEqual(result["row_summary"]["created_snapshots"], 2)
        self.assertEqual(result["row_summary"]["linked_rows"], 1)
        self.assertEqual(result["row_summary"]["unlinked_rows"], 1)
        self.assertEqual(result["training_eligibility"]["eligible_rows"], 1)
        self.assertEqual(result["training_eligibility"]["ineligible_rows"], 1)
        self.assertEqual(result["feedback_state"]["rebuilt_training_samples"], 1)
        self.assertEqual(result["row_issues"][0]["row_number"], 3)
        self.assertEqual(result["row_issues"][0]["link_status"], "unlinked")
        self.assertEqual(result["row_issues"][0]["identifiers"]["candidate_segment_id"], "seg_missing")
        self.assertEqual(len(list_training_samples(account_id="main")), 1)

    def test_video_manifest_summarizes_pipeline_artifacts(self) -> None:
        segment = _insert_segment()
        transcript_path = self.root / "manifest_transcript.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "source": "sidecar_srt",
                    "metadata": {"postprocess_version": "test"},
                    "segments": [{"start": 0, "end": 5, "text": "导师点评"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ?, status = 'transcribed' WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()
        score_segment(segment["id"])

        manifest = video_manifest(segment["source_video_id"])
        steps = {item["step"]: item for item in manifest["steps"]}

        self.assertEqual(manifest["contract_version"], "artifact_manifest.v1")
        self.assertEqual(steps["transcript"]["status"], "ready")
        self.assertEqual(steps["candidates"]["summary"]["count"], 1)
        self.assertEqual(steps["scores"]["summary"]["count"], 1)
        self.assertIn("exports", steps)

    def test_variant_experiment_records_hypothesis_and_changed_variable(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        variant = create_variant(
            segment["id"],
            title="标题 A",
            hypothesis="更直接的标题提升首5秒留存",
            changed_variable="title",
            publish_window="24h evening",
        )
        updated = update_variant(
            variant["id"],
            {
                "title": "标题 B",
                "changed_variable": "cover_time",
                "reason": "测试封面时间",
            },
        )
        experiment = create_experiment(
            updated["id"],
            {
                "experiment_group": "A",
                "hypothesis": "封面变化提升点击",
                "changed_variable": "cover_time",
                "publish_window": "24h evening",
            },
        )
        experiments = list_experiments(updated["id"])

        self.assertEqual(variant["contract_version"], "variant_experiment.v1")
        self.assertEqual(updated["title"], "标题 B")
        self.assertEqual(updated["changed_variable"], "cover_time")
        self.assertEqual(experiment["hypothesis"], "封面变化提升点击")
        self.assertEqual(len(experiments), 1)

    def test_platform_mapping_links_mock_metrics_to_training_sample(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        variant = create_variant(segment["id"], title="平台映射标题", changed_variable="title")
        experiment = create_experiment(variant["id"], {"experiment_group": "platform"})
        mapping = create_platform_mapping(
            {
                "platform": "douyin",
                "platform_item_id": "aweme_123",
                "slice_variant_id": variant["id"],
                "experiment_id": experiment["id"],
            }
        )
        mapped = map_platform_metric_row(
            {
                "platform": "douyin",
                "aweme_id": "aweme_123",
                "play_count": "1200",
                "show_count": "2400",
                "avg_play_duration": "18",
                "play_finish_rate": "52%",
                "like_count": "90",
            }
        )
        csv_path = self.root / "mock_platform_metrics.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(mapped.keys()))
            writer.writeheader()
            writer.writerow(mapped)

        result = import_metrics(csv_path, sample_source="mock")
        samples = list_training_samples(account_id="main")

        self.assertEqual(mapping["candidate_segment_id"], segment["id"])
        self.assertEqual(result["row_summary"]["linked_rows"], 1)
        self.assertEqual(result["training_samples"], 1)
        self.assertEqual(samples[0]["sample_source"], "mock")

    def test_douyin_mock_sync_uses_existing_mapping_and_imports_windows(self) -> None:
        segment = _insert_segment()
        target = _insert_extra_segment(
            "seg_mock_target",
            "赛制铺垫后观众突然起立 副歌爆发",
            "赛制悬念到音乐爆点型",
        )
        score_segment(segment["id"])
        score_segment(target["id"])
        variant = create_variant(segment["id"], title="回流标题", changed_variable="title")
        experiment = create_experiment(variant["id"], {"experiment_group": "sync"})
        register_douyin_account("main", {"display_name": "测试账号"})
        create_platform_mapping(
            {
                "platform": "douyin",
                "platform_item_id": "aweme_sync_1",
                "slice_variant_id": variant["id"],
                "experiment_id": experiment["id"],
                "platform_title": "已发布切片",
            }
        )

        result = sync_douyin_feedback("main", source="mock", windows=["6h", "24h"])
        summary = douyin_sync_summary("main")
        samples = list_training_samples(account_id="main")
        clock = build_interest_clock("main")
        history = calibrate_segment_history(target["id"], account_id="main")

        self.assertEqual(result["contract_version"], PLATFORM_SYNC_VERSION)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["pulled_rows"], 2)
        self.assertEqual(result["mapping_summary"]["mapped_items"], 1)
        self.assertEqual(result["import_result"]["row_summary"]["linked_rows"], 2)
        self.assertEqual(result["import_result"]["training_samples"], 2)
        self.assertEqual(summary["metrics"]["count"], 2)
        self.assertTrue(summary["runs"])
        self.assertEqual({sample["sample_source"] for sample in samples}, {"mock"})
        self.assertEqual(clock["sample_count"], 0)
        self.assertEqual(clock["status"], "insufficient_history")
        self.assertEqual(history["sample_count"], 0)
        self.assertEqual(history["status"], "insufficient_history")

    def test_douyin_api_payload_creates_mapping_and_rebuilds_feedback(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        variant = create_variant(segment["id"], title="API 回流标题", changed_variable="cover")
        payload = {
            "source": "api",
            "rows": [
                {
                    "platform": "douyin",
                    "aweme_id": "aweme_payload_1",
                    "slice_variant_id": variant["id"],
                    "window_name": "24h",
                    "play_count": "3200",
                    "show_count": "6000",
                    "avg_play_duration": "20",
                    "play_finish_rate": "66%",
                    "like_count": "180",
                    "share_count": "22",
                }
            ],
        }

        result = sync_douyin_feedback("main", source="api", payload=payload, windows=["24h"])
        mappings = douyin_sync_summary("main")["mappings"]
        samples = list_training_samples(account_id="main")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["import_result"]["row_summary"]["linked_rows"], 1)
        self.assertEqual(mappings[0]["platform_item_id"], "aweme_payload_1")
        self.assertEqual(mappings[0]["candidate_segment_id"], segment["id"])
        self.assertEqual(samples[0]["sample_source"], "api")

    def test_douyin_csv_sync_is_counted_in_summary(self) -> None:
        result = sync_douyin_feedback(
            "main",
            source="csv",
            payload=[
                {
                    "platform": "douyin",
                    "aweme_id": "aweme_csv_visible_1",
                    "window_name": "visible_snapshot",
                    "play_count": "818",
                }
            ],
            windows=["final"],
        )
        summary = douyin_sync_summary("main")

        self.assertEqual(result["status"], "completed_with_warnings")
        self.assertEqual(result["import_result"]["row_summary"]["unlinked_rows"], 1)
        self.assertEqual(summary["metrics"]["count"], 1)
        self.assertEqual(summary["metrics"]["unlinked"], 1)
        self.assertEqual(summary["mappings"][0]["platform_item_id"], "aweme_csv_visible_1")

    def test_douyin_xlsx_sync_reads_visible_collection_sheet(self) -> None:
        xlsx_path = self.root / "tianci_douyin_visible_collection_latest.xlsx"
        _write_xlsx_rows(
            xlsx_path,
            "作品去重",
            [
                ["排名", "可见计数", "计数数值", "视频ID文本", "视频URL", "内容类别", "钩子类型", "标题", "话题标签"],
                [
                    "1",
                    "23.8万",
                    "",
                    "ID:7655575210669722907",
                    "https://www.douyin.com/video/7655575210669722907",
                    "performance_clip",
                    "high_note",
                    "黄子弘凡孙楠坠落太有力量了 #黄子弘凡孙楠合唱坠落",
                    "#黄子弘凡孙楠合唱坠落|#天赐的声音",
                ],
            ],
        )

        result = sync_douyin_feedback("main", source="xlsx", source_path=xlsx_path)
        summary = douyin_sync_summary("main")

        self.assertEqual(result["status"], "completed_with_warnings")
        self.assertEqual(result["pulled_rows"], 1)
        self.assertEqual(result["import_result"]["row_summary"]["unlinked_rows"], 1)
        self.assertEqual(summary["mappings"][0]["platform_item_id"], "7655575210669722907")
        with connect() as conn:
            row = conn.execute("SELECT views, platform_item_id, sample_source FROM performance_metrics").fetchone()
        self.assertEqual(row["views"], 238000)
        self.assertEqual(row["platform_item_id"], "7655575210669722907")
        self.assertEqual(row["sample_source"], "csv")

    def test_douyin_visible_clean_recovers_counts_ids_and_dedupes(self) -> None:
        capture_dir = self.root / "douyin_capture"
        capture_dir.mkdir()
        snapshot = {
            "observed_at": "2026-06-27T13:37:21Z",
            "page": {"title": "抖音", "url": "https://www.douyin.com/follow"},
            "account": {
                "nickname": "天赐的声音",
                "profile_url": "https://www.douyin.com/user/account_demo",
                "followers_visible": "1110.3万",
                "likes_received_visible": "8.3亿",
            },
            "current_video": {
                "aweme_ids_visible": ["7655912998237687046", "7655558170642812206"],
                "hashtag_links": [
                    {
                        "text": "#刘珂矣",
                        "href": "https://www.douyin.com/search/x?aweme_id=7655558170642812206&source=pc_click_hashtag_feed",
                    },
                    {
                        "text": "相关搜索 ： 刘珂矣音乐作品",
                        "href": "https://www.douyin.com/search/y?aweme_id=7655558170642812206&source=related_search_anchor_v2",
                    },
                ],
                "visible_metric_numbers_unlabeled": ["818", "21"],
            },
            "visible_works": [
                {
                    "visible_count": None,
                    "title_tags_text": "共创 2.4万 王铮亮&amp;欧阳娜娜《阳光下的星星》 #欧阳娜娜 #王铮亮",
                    "tags": ["#欧阳娜娜", "#王铮亮"],
                },
                {
                    "visible_count": "1.1万",
                    "title_tags_text": "王铮亮&欧阳娜娜《阳光下的星星》 #欧阳娜娜 #王铮亮",
                    "tags": ["#欧阳娜娜", "#王铮亮"],
                },
                {
                    "href": "/video/7655942685882060068",
                    "visible_count": None,
                    "title_tags_text": "置顶 5727 《依兰爱情故事》金志文无伴奏原声 #天赐的声音 #金志文 《依兰爱情故事》金志文无伴奏原声 #天赐的声音 #金志文",
                    "tags": ["#天赐的声音", "#金志文"],
                },
            ],
        }
        (capture_dir / "douyin_follow_visible_20260627T133721Z.json").write_text(
            json.dumps(snapshot, ensure_ascii=False),
            encoding="utf-8",
        )

        result = clean_visible_snapshots(capture_dir, capture_dir)

        self.assertEqual(result.current_videos[0]["current_aweme_id"], "7655558170642812206")
        work_rows = [row for row in result.clean_records if row["record_type"] == "visible_work_card"]
        self.assertEqual(work_rows[0]["visible_count"], "2.4万")
        self.assertEqual(work_rows[0]["visible_count_number"], 24000)
        self.assertIn("王铮亮&欧阳娜娜", work_rows[0]["normalized_title"])
        self.assertEqual(len(result.dedup_works), 2)
        sunlight = next(item for item in result.dedup_works if "阳光下的星星" in item["normalized_title"])
        self.assertEqual(sunlight["best_visible_count"], "2.4万")
        pinned = next(item for item in result.dedup_works if item["aweme_id"] == "7655942685882060068")
        self.assertEqual(pinned["best_visible_count"], "5727")
        self.assertTrue(pinned["is_pinned_visible"])
        self.assertEqual(result.quality_report["work_card_count_deduped"], 2)
        self.assertTrue(Path(result.paths["dedup_works_csv"]).exists())

    def test_douyin_media_collect_dry_run_writes_account_scoped_report(self) -> None:
        plan_path = self.root / "media_plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "samples": [
                        {
                            "sample_id": "smoke_tianci_high_001",
                            "collection_order": 2,
                            "account_id": "tianci",
                            "dataset_id": "tianci_20260628",
                            "performance_label": "high",
                            "aweme_id": "7655575210669722907",
                            "source_url": "https://www.douyin.com/video/7655575210669722907",
                            "title": "高互动样本",
                            "stage": "smoke_v1",
                        },
                        {
                            "sample_id": "smoke_sixuweilive_mid_001",
                            "collection_order": 1,
                            "account_id": "sixuweilive",
                            "dataset_id": "sixuweilive_20260628",
                            "performance_label": "mid",
                            "aweme_id": "7656046228765994171",
                            "source_url": "https://www.douyin.com/video/7656046228765994171",
                            "title": "中互动样本",
                            "stage": "smoke_v1",
                        },
                        {
                            "sample_id": "pilot_tianci_low_001",
                            "collection_order": 3,
                            "account_id": "tianci",
                            "dataset_id": "tianci_20260628",
                            "performance_label": "low",
                            "aweme_id": "7650000000000000000",
                            "source_url": "https://www.douyin.com/video/7650000000000000000",
                            "title": "试点样本",
                            "stage": "pilot_v1",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = collect_douyin_media(
            plan_path,
            stage="smoke_v1",
            output_root=self.root / "media_assets",
            report_dir=self.root / "reports",
            run_id="test_run",
            dry_run=True,
        )
        report = json.loads(Path(result["report_json"]).read_text(encoding="utf-8"))

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["planned"], 2)
        self.assertEqual(result["success"], 0)
        self.assertTrue(Path(result["report_md"]).exists())
        self.assertEqual([row["collection_order"] for row in report["results"]], [1, 2])
        self.assertIn("/sixuweilive/test_run/videos/7656046228765994171.mp4", report["results"][0]["video_path"])
        self.assertIn("/tianci/test_run/videos/7655575210669722907.mp4", report["results"][1]["video_path"])
        self.assertEqual(report["summary"]["by_account"]["sixuweilive"]["total"], 1)
        self.assertEqual(report["summary"]["by_account"]["tianci"]["total"], 1)

    def test_douyin_account_library_defaults_missing_tier_to_x(self) -> None:
        input_path = self.root / "accounts.json"
        output_path = self.root / "data" / "douyin_capture" / "account_library.json"
        input_path.write_text(
            json.dumps(
                [
                    {
                        "key": "tianci",
                        "account": "天赐的声音",
                        "sec_uid": "sec_tianci",
                        "unique_id": "tiancideshen11",
                        "follower_count": 10622421,
                        "works": "330",
                        "account_type": "program_official",
                        "program_key": "tianci",
                        "source_kind": "following_api",
                        "collection_depth_limit": 1000,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = build_account_library(input_path, output_path=output_path, observed_at="2026-06-28T20:30:00Z")
        account = result.accounts[0]

        self.assertEqual(account["account_key"], "tianci")
        self.assertEqual(account["account_tier"], "X")
        self.assertEqual(account["tier"], "X")
        self.assertEqual(account["profile_url"], "https://www.douyin.com/user/sec_tianci")
        self.assertEqual(account["aweme_count"], 330)
        self.assertEqual(account["unique_id"], "tiancideshen11")
        self.assertEqual(account["follower_count"], 10622421)
        self.assertEqual(account["account_type"], "program_official")
        self.assertEqual(account["program_key"], "tianci")
        self.assertEqual(account["source_kind"], "following_api")
        self.assertEqual(account["collection_depth_limit"], 1000)
        self.assertIn("missing_account_tier_defaulted_x", account["quality_flags"])
        self.assertTrue(output_path.exists())

    def test_douyin_account_api_works_clean_dedupes_and_reports_quality(self) -> None:
        account_library = self.root / "account_library.json"
        raw_works = self.root / "raw_works.json"
        account_library.write_text(
            json.dumps(
                [
                    {
                        "account_key": "tianci",
                        "account_tier": "A",
                        "nickname": "天赐的声音",
                        "profile_url": "https://www.douyin.com/user/sec_tianci",
                        "sec_uid": "sec_tianci",
                        "user_id": "uid_tianci",
                        "aweme_count": 330,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        raw_works.write_text(
            json.dumps(
                [
                    {
                        "aweme_id": "1001",
                        "desc": "黄霄雲清唱《有我呢》#天赐的声音 #黄霄雲",
                        "create_time": 1782531286,
                        "video_url": "https://www.douyin.com/video/1001",
                        "digg_count": 250,
                        "comment_count": 23,
                        "share_count": 7,
                        "collect_count": 11,
                        "play_count": 0,
                        "duration": 30806,
                        "author_nickname": "天赐的声音",
                        "author_sec_uid": "sec_tianci",
                        "author_uid": "uid_tianci",
                    },
                    {
                        "aweme_id": "1001",
                        "desc": "黄霄雲清唱《有我呢》#天赐的声音 #黄霄雲",
                        "create_time": 1782531286,
                        "digg_count": 255,
                        "comment_count": 23,
                        "share_count": 7,
                        "collect_count": 11,
                        "play_count": 0,
                        "duration": 30806,
                        "author_nickname": "天赐的声音",
                        "author_sec_uid": "sec_tianci",
                        "author_uid": "uid_tianci",
                    },
                    {
                        "aweme_id": "1002",
                        "desc": "黄子弘凡孙楠坠落太有力量了 #天赐的声音",
                        "create_time": 1782452507,
                        "statistics": {
                            "digg_count": 500,
                            "comment_count": 88,
                            "share_count": 12,
                            "collect_count": 34,
                            "play_count": 12000,
                        },
                        "duration": 40299,
                        "author": {
                            "nickname": "天赐的声音",
                            "sec_uid": "sec_tianci",
                            "uid": "uid_tianci",
                        },
                    },
                    {
                        "aweme_id": "bad_author",
                        "desc": "错配作者作品",
                        "digg_count": 999,
                        "comment_count": 1,
                        "share_count": 1,
                        "collect_count": 1,
                        "play_count": 99,
                        "author_nickname": "其他账号",
                        "author_sec_uid": "wrong_sec_uid",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = clean_account_api_works(
            account_library=account_library,
            account_key="tianci",
            raw_works=raw_works,
            output_root=self.root / "data" / "douyin_capture",
            run_id="20260628T203000_test",
            observed_at="2026-06-28T20:30:00Z",
        )
        report = result.quality_report

        self.assertEqual(report["raw_rows"], 4)
        self.assertEqual(report["accepted_rows"], 3)
        self.assertEqual(report["dedup_rows"], 2)
        self.assertEqual(report["author_mismatch_rejected"], 1)
        self.assertEqual(report["duplicate_ratio"], 0.3333)
        self.assertEqual(report["required_metric_coverage"]["likes"]["rate"], 1.0)
        self.assertEqual(report["required_metric_coverage"]["favorites"]["rate"], 1.0)
        self.assertEqual(report["play_count_missing_rate"], 0.5)
        self.assertEqual(len(result.clean_works), 2)
        first = next(row for row in result.clean_works if row["aweme_id"] == "1001")
        self.assertEqual(first["title"], "黄霄雲清唱《有我呢》#天赐的声音 #黄霄雲")
        self.assertIn("#黄霄雲", first["tags"])
        self.assertEqual(first["likes"], 255)
        self.assertEqual(first["favorites"], 11)
        self.assertEqual(first["comments"], 23)
        self.assertEqual(first["shares"], 7)
        self.assertEqual(first["play_count"], 0)
        self.assertTrue(first["play_count_missing"])
        self.assertNotEqual(first["likes"], first["play_count"])
        self.assertEqual(first["duration"], 30806)
        self.assertEqual(first["duration_seconds"], 30.806)
        self.assertTrue(Path(result.paths["clean_works_json"]).exists())
        self.assertTrue(Path(result.paths["quality_report_account"]).exists())

    def test_douyin_account_api_works_marks_missing_play_count_and_metric_coverage(self) -> None:
        account_library = [
            {
                "account_key": "sixuweilive",
                "account_tier": "B",
                "nickname": "思绪未live",
                "sec_uid": "sec_sixu",
            }
        ]
        raw_works = [
            {
                "aweme_id": "2001",
                "desc": "只返回互动指标的作品 #思绪未live",
                "digg_count": 88,
                "share_count": 4,
                "collect_count": 9,
                "duration": 12000,
                "author_nickname": "思绪未live",
                "author_sec_uid": "sec_sixu",
            }
        ]

        result = clean_account_api_works(
            account_library=account_library,
            account_key="sixuweilive",
            raw_works=raw_works,
            output_root=self.root / "data" / "douyin_capture",
            run_id="20260628T203100_test",
        )
        clean = result.clean_works[0]
        report = result.quality_report

        self.assertIsNone(clean["play_count"])
        self.assertTrue(clean["play_count_missing"])
        self.assertIn("missing_play_count", clean["metric_quality_flags"])
        self.assertEqual(clean["likes"], 88)
        self.assertNotEqual(clean["likes"], clean["play_count"])
        self.assertEqual(report["required_metric_coverage"]["comments"]["rate"], 0.0)
        self.assertEqual(report["required_metric_coverage"]["likes"]["rate"], 1.0)
        self.assertEqual(report["play_count_missing_rate"], 1.0)
        self.assertLess(report["quality_score"], 100)

    def test_douyin_qr_login_builds_official_authorize_url(self) -> None:
        os.environ["DSO_DOUYIN_CLIENT_KEY"] = "client_key_demo"
        os.environ["DSO_DOUYIN_REDIRECT_URI"] = "https://example.com/platform/douyin/oauth/callback"

        result = start_douyin_qr_login("main", scopes="user_info,posting.behavior", state="state_demo")
        status = douyin_oauth_status("main", state="state_demo")

        self.assertEqual(result["status"], "waiting_scan")
        self.assertIn("https://open.douyin.com/platform/oauth/connect/", result["auth_url"])
        self.assertIn("client_key=client_key_demo", result["auth_url"])
        self.assertIn("response_type=code", result["auth_url"])
        self.assertIn("state=state_demo", result["auth_url"])
        self.assertEqual(result["config"]["ready_for_qr_login"], True)
        self.assertEqual(status["session"]["status"], "waiting_scan")

    def test_douyin_qr_login_exchange_stores_token_outside_sqlite(self) -> None:
        os.environ["DSO_DOUYIN_CLIENT_KEY"] = "client_key_demo"
        os.environ["DSO_DOUYIN_CLIENT_SECRET"] = "client_secret_demo"
        os.environ["DSO_DOUYIN_REDIRECT_URI"] = "https://example.com/platform/douyin/oauth/callback"
        start_douyin_qr_login("main", scopes="user_info", state="state_token")
        token_payload = {
            "data": {
                "access_token": "access_demo",
                "refresh_token": "refresh_demo",
                "open_id": "open_demo",
                "scope": "user_info",
                "expires_in": 3600,
                "refresh_expires_in": 7200,
            }
        }

        with patch("dso.feedback.douyin_auth._exchange_access_token", return_value=token_payload):
            result = complete_douyin_qr_login("code_demo", "state_token")
        status = douyin_oauth_status("main", state="state_token")
        token_path = Path(result["token_path"])
        token_data = json.loads(token_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["open_id"], "open_demo")
        self.assertEqual(status["account"]["auth_status"], "connected")
        self.assertEqual(status["account"]["token_status"], "stored_local_file")
        self.assertTrue(status["token"]["stored"])
        self.assertIn("douyin:main", token_data)
        self.assertEqual(token_data["douyin:main"]["access_token"], "access_demo")

    def test_memory_bank_and_history_calibration_find_similar_training_sample(self) -> None:
        target = _insert_segment()
        high = _insert_extra_segment(
            "seg_high_history",
            "导师说这次改编第一次突破 副歌高音爆发 全场观众欢呼",
            "节目叙事到音乐爆点型",
        )
        low = _insert_extra_segment(
            "seg_low_history",
            "品牌福利口播 关注直播间 下单领取优惠券",
            "广告口播型",
        )
        for segment_id in [target["id"], high["id"], low["id"]]:
            score_segment(segment_id)
        csv_path = self.root / "history_metrics.csv"
        _write_metric_rows(
            csv_path,
            [
                {"candidate_segment_id": high["id"], "window_name": "24h", "views": "5000", "impressions": "7000", "avg_watch_ratio": "88%", "completion_rate": "72%"},
                {"candidate_segment_id": low["id"], "window_name": "24h", "views": "200", "impressions": "2000", "avg_watch_ratio": "18%", "completion_rate": "12%", "negative_feedback": "20"},
            ],
        )
        import_metrics(csv_path)

        memory = build_text_memory_bank(account_id="main")
        history = calibrate_segment_history(target["id"], account_id="main", limit=5)

        self.assertEqual(memory["contract_version"], MEMORY_BANK_VERSION)
        self.assertEqual(history["contract_version"], HISTORY_CALIBRATION_VERSION)
        self.assertEqual(history["sample_count"], 2)
        self.assertGreater(history["similar_high_perf_score"], 0)
        self.assertTrue(any(match["matched_segment_id"] == high["id"] for match in history["matches"]))
        self.assertLess(history["history_uncertainty"], 1)

    def test_interest_clock_and_backtest_use_training_samples(self) -> None:
        first = _insert_segment()
        second = _insert_extra_segment(
            "seg_clock_second",
            "赛制悬念之后副歌转调 全场观众起立",
            "赛制悬念到音乐爆点型",
        )
        third = _insert_extra_segment(
            "seg_clock_third",
            "歌手故事铺垫 后面高音唱出遗憾",
            "歌手故事到音乐爆点型",
        )
        for segment_id in [first["id"], second["id"], third["id"]]:
            score_segment(segment_id)
        csv_path = self.root / "clock_metrics.csv"
        _write_metric_rows(
            csv_path,
            [
                {"candidate_segment_id": first["id"], "window_name": "24h", "collected_at": "2026-06-23T20:00:00+00:00", "views": "2600", "impressions": "4000", "avg_watch_ratio": "75%", "completion_rate": "60%"},
                {"candidate_segment_id": second["id"], "window_name": "24h", "collected_at": "2026-06-23T21:00:00+00:00", "views": "3200", "impressions": "5200", "avg_watch_ratio": "81%", "completion_rate": "68%"},
                {"candidate_segment_id": third["id"], "window_name": "24h", "collected_at": "2026-06-23T10:00:00+00:00", "views": "900", "impressions": "2600", "avg_watch_ratio": "48%", "completion_rate": "32%"},
            ],
        )
        import_metrics(csv_path)

        clock = build_interest_clock("main")
        recommended = recommend_publish_hours("main", limit=3)
        report = backtest_rule_ranker(account_id="main", k=3)
        reports = list_backtest_reports(account_id="main", limit=1)

        self.assertEqual(clock["contract_version"], INTEREST_CLOCK_VERSION)
        self.assertEqual(recommended["contract_version"], INTEREST_CLOCK_VERSION)
        self.assertTrue(clock["top_windows"])
        self.assertTrue(recommended["recommendations"])
        self.assertEqual(report["contract_version"], BACKTEST_VERSION)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["metrics"]["sample_count"], 3)
        self.assertGreaterEqual(report["metrics"]["ndcg_at_k"], 0)
        self.assertLessEqual(report["metrics"]["ndcg_at_k"], 1)
        self.assertEqual(reports["contract_version"], BACKTEST_VERSION)
        self.assertEqual(reports["reports"][0]["contract_version"], BACKTEST_VERSION)
        self.assertEqual(reports["reports"][0]["metrics"]["sample_count"], 3)
        self.assertTrue(reports["reports"][0]["top_rows"])

    def test_prototype_bank_builds_from_visible_capture_csv(self) -> None:
        capture_path = self.root / "data" / "douyin_capture" / "douyin_visible_works_dedup_latest.csv"
        _write_visible_work_rows(
            capture_path,
            [
                {
                    "work_key": "work_regret",
                    "normalized_title": "#欢子黄霄雲是你没选我啊唱尽遗憾 #欢子黄霄雲把遗憾唱得太具体了 #天赐的声音",
                    "tags": "#欢子黄霄雲是你没选我啊唱尽遗憾|#欢子黄霄雲把遗憾唱得太具体了|#欢子|#黄霄雲|#天赐的声音",
                    "hook_type": "emotional_story",
                    "content_category": "performance_clip",
                    "program_name": "天赐的声音",
                    "artist_names": "欢子|黄霄雲",
                    "best_visible_count_number": "120000",
                    "last_observed_at": "2026-06-27T13:40:12+00:00",
                },
                {
                    "work_key": "work_national",
                    "normalized_title": "#刘珂矣天赐国风舞台半壶纱 十里桃花待嫁的年华 #刘珂矣 #半壶纱",
                    "tags": "#刘珂矣天赐国风舞台半壶纱|#刘珂矣|#半壶纱|#天赐的声音",
                    "hook_type": "celebrity_pairing",
                    "content_category": "performance_clip",
                    "program_name": "天赐的声音",
                    "artist_names": "刘珂矣",
                    "song_title": "半壶纱",
                    "best_visible_count_number": "15000",
                    "last_observed_at": "2026-06-27T13:38:48+00:00",
                },
                {
                    "work_key": "work_blast",
                    "normalized_title": "副歌高音转调爆发 全场观众起立欢呼",
                    "tags": "#高音|#转调|#全场欢呼",
                    "hook_type": "music_burst",
                    "content_category": "performance_clip",
                    "best_visible_count_number": "54000",
                    "last_observed_at": "2026-06-27T20:00:00+00:00",
                },
            ],
        )

        result = build_prototype_bank("main", source="external", limit=10, force=True)
        bank = list_prototype_bank("main", limit=10)

        self.assertEqual(result["contract_version"], PROTOTYPE_BANK_VERSION)
        self.assertEqual(result["sample_count"], 3)
        self.assertIn("account_distribution", result)
        self.assertGreater(result["account_distribution"]["p75_views"], 0)
        self.assertGreaterEqual(result["prototype_count"], 2)
        self.assertEqual(bank["contract_version"], PROTOTYPE_BANK_VERSION)
        self.assertEqual(bank["count"], result["prototype_count"])
        names = {item["prototype_name"] for item in result["prototypes"]}
        self.assertIn("遗憾共鸣型", names)
        self.assertIn("国风审美型", names)
        prototype = result["prototypes"][0]
        self.assertIn("absolute_level", prototype["parameters"])
        self.assertIn("account_lift", prototype["parameters"])
        self.assertIn("stability", prototype["parameters"])
        self.assertIn("decision_label", prototype["parameters"])
        self.assertTrue(Path(bank["prototypes"][0]["vector_path"]).is_file())

    def test_prototype_bank_auto_discovers_non_tianci_latest_xlsx(self) -> None:
        xlsx_path = self.root / "outputs" / "douyin_geshou2026_20260627" / "geshou2026_douyin_visible_collection_latest.xlsx"
        _write_xlsx_rows(
            xlsx_path,
            "作品去重",
            [
                ["排名", "可见计数", "计数数值", "视频ID文本", "视频URL", "内容类别", "钩子类型", "切片结构", "艺人", "歌曲", "标题", "话题标签"],
                [
                    "1",
                    "14.0万",
                    "140000",
                    "ID:7655679615205226441",
                    "https://www.douyin.com/video/7655679615205226441",
                    "performance_clip",
                    "unknown",
                    "unknown",
                    "万妮达",
                    "Bad Boy",
                    "万妮达 Bad Boy 唱出清醒自信的飒爽内核 #歌手2026第六期舞台",
                    "#歌手2026|#万妮达",
                ],
                [
                    "2",
                    "5.4万",
                    "54000",
                    "ID:7655683554328522414",
                    "https://www.douyin.com/video/7655683554328522414",
                    "performance_clip",
                    "celebrity_pairing",
                    "unknown",
                    "万妮达|约翰·传奇",
                    "",
                    "约翰传奇选择万妮达守榜 两位歌手同台互动 #歌手2026",
                    "#歌手2026|#万妮达|#约翰传奇",
                ],
            ],
        )

        result = build_prototype_bank("main", source="external", limit=10, force=True)

        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["source_summary"]["by_kind"]["capture_xlsx"], 2)
        self.assertTrue(any(example["source_kind"] == "capture_xlsx" for item in result["prototypes"] for example in item["examples"]))

    def test_prototype_bank_isolated_by_dataset_id(self) -> None:
        tianci_path = self.root / "outputs" / "douyin_tianci_20260627" / "tianci_douyin_visible_collection_latest.xlsx"
        geshou_path = self.root / "outputs" / "douyin_geshou2026_20260627" / "geshou2026_douyin_visible_collection_latest.xlsx"
        _write_xlsx_rows(
            tianci_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签", "钩子类型", "艺人", "歌曲"],
                ["1", "120000", "ID:tianci_regret", "欢子黄霄雲是你没选我啊唱尽遗憾", "#天赐的声音|#没选我", "emotional_story", "欢子|黄霄雲", ""],
                ["2", "30000", "ID:tianci_national", "刘珂矣半壶纱国风舞台", "#天赐的声音|#半壶纱", "unknown", "刘珂矣", "半壶纱"],
            ],
        )
        _write_xlsx_rows(
            geshou_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签", "钩子类型", "艺人", "歌曲"],
                ["1", "140000", "ID:geshou_rank", "歌手2026第六期排名预测 袭榜结果悬念", "#歌手2026|#排名", "unknown", "万妮达", ""],
                ["2", "54000", "ID:geshou_pair", "约翰传奇选择万妮达守榜 两位歌手同台互动", "#歌手2026|#万妮达", "celebrity_pairing", "万妮达|约翰·传奇", ""],
            ],
        )

        datasets = list_capture_datasets()
        dataset_ids = {item["id"] for item in datasets["datasets"]}
        self.assertIn("tianci_20260627", dataset_ids)
        self.assertIn("geshou2026_20260627", dataset_ids)

        import_historical_samples("main", dataset_id="tianci_20260627", force=True)
        import_historical_samples("main", dataset_id="geshou2026_20260627", force=True)
        tianci = build_prototype_bank("main", source="visible_capture", dataset_id="tianci_20260627", limit=10, force=True)
        geshou = build_prototype_bank("main", source="visible_capture", dataset_id="geshou2026_20260627", limit=10, force=True)
        tianci_bank = list_prototype_bank("main", source="visible_capture", dataset_id="tianci_20260627", limit=10)
        geshou_bank = list_prototype_bank("main", source="visible_capture", dataset_id="geshou2026_20260627", limit=10)

        self.assertEqual(tianci["dataset_id"], "tianci_20260627")
        self.assertEqual(geshou["dataset_id"], "geshou2026_20260627")
        self.assertTrue(all(item["dataset_id"] == "tianci_20260627" for item in tianci_bank["prototypes"]))
        self.assertTrue(all(item["dataset_id"] == "geshou2026_20260627" for item in geshou_bank["prototypes"]))
        self.assertNotEqual(
            {example["title"] for item in tianci_bank["prototypes"] for example in item["examples"]},
            {example["title"] for item in geshou_bank["prototypes"] for example in item["examples"]},
        )

    def test_historical_capture_samples_import_and_feed_prototypes(self) -> None:
        xlsx_path = self.root / "outputs" / "douyin_tianci_20260627" / "tianci_douyin_visible_collection_latest.xlsx"
        _write_xlsx_rows(
            xlsx_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签", "钩子类型", "艺人", "歌曲"],
                ["1", "120000", "ID:tianci_regret", "欢子黄霄雲是你没选我啊唱尽遗憾", "#天赐的声音|#没选我", "emotional_story", "欢子|黄霄雲", ""],
                ["2", "54000", "ID:tianci_blast", "副歌高音转调爆发 全场观众起立欢呼", "#高音|#转调", "music_burst", "测试歌手", ""],
                ["3", "30000", "ID:tianci_national", "刘珂矣半壶纱国风舞台", "#天赐的声音|#半壶纱", "unknown", "刘珂矣", "半壶纱"],
            ],
        )

        imported = import_historical_samples("main", dataset_id="tianci_20260627", force=True)
        listed = list_historical_samples("main", dataset_id="tianci_20260627", limit=10)
        summary = historical_sample_summary("main")
        xlsx_path.unlink()
        result = build_prototype_bank("main", source="visible_capture", dataset_id="tianci_20260627", limit=10, force=True)

        self.assertEqual(imported["valid_rows"], 3)
        self.assertEqual(imported["inserted"], 3)
        self.assertEqual(listed["count"], 3)
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["source_summary"]["by_dataset"]["tianci_20260627"], 3)
        self.assertTrue(any(example["source_kind"] == "capture_xlsx" for item in result["prototypes"] for example in item["examples"]))

    def test_historical_sample_summary_reports_lineage_metric_coverage_and_trainable_counts(self) -> None:
        _insert_historical_sample("hist_likes", dataset_id="tianci_20260628", item_id="likes_item", title="点赞有效", likes=12)
        _insert_historical_sample("hist_comment", dataset_id="tianci_20260628", item_id="comment_item", title="评论有效", comments=5)
        _insert_historical_sample("hist_favorite", dataset_id="tianci_20260628", item_id="favorite_item", title="收藏有效", favorites=7)
        _insert_historical_sample(
            "hist_share_title",
            dataset_id="tianci_20260628",
            item_id="",
            sample_key="title:stable-share-title",
            title="标题 key 有效",
            shares=3,
        )
        _insert_historical_sample("hist_mock", dataset_id="tianci_20260628", item_id="mock_item", title="Mock 样本", likes=99, source_kind="mock")

        summary = historical_sample_summary("main")
        dataset = next(item for item in summary["datasets"] if item["dataset_id"] == "tianci_20260628")

        self.assertEqual(summary["sample_count"], 5)
        self.assertEqual(summary["stored_sample_count"], 5)
        self.assertEqual(summary["deduped_sample_count"], 5)
        self.assertEqual(summary["trainable_sample_count"], 4)
        self.assertEqual(summary["metric_coverage_sample_count"], 4)
        self.assertEqual(summary["metric_coverage"]["likes"]["count"], 1)
        self.assertEqual(summary["metric_coverage"]["comments"]["count"], 1)
        self.assertEqual(summary["metric_coverage"]["favorites"]["count"], 1)
        self.assertEqual(summary["metric_coverage"]["shares"]["count"], 1)
        self.assertEqual(summary["likes_coverage_rate"], 0.25)
        self.assertEqual(summary["play_missing_count"], 4)
        self.assertEqual(summary["play_missing_rate"], 1.0)
        self.assertEqual(summary["duplicate_item_group_count"], 0)
        self.assertEqual(summary["duplicate_item_groups"], [])
        self.assertEqual(dataset["trainable_sample_count"], 4)
        self.assertEqual(summary["account_quality"][0]["account_id"], "main")
        self.assertEqual(summary["account_quality"][0]["trainable_sample_count"], 4)
        self.assertEqual(summary["account_quality"][0]["confidence"], "insufficient_history")

    def test_history_calibration_prefers_historical_capture_samples(self) -> None:
        target = _insert_segment()
        _insert_historical_sample(
            "hist_high_match",
            dataset_id="tianci_20260628",
            item_id="hist_high_match",
            title="导师点评改编第一次突破 副歌高音爆发 全场观众欢呼",
            likes=500,
            comments=80,
            favorites=60,
            shares=40,
            reward_proxy=95,
            normalized_reward=95,
            performance_label="high",
        )
        _insert_historical_sample(
            "hist_low_match",
            dataset_id="tianci_20260628",
            item_id="hist_low_match",
            title="品牌福利口播 关注直播间 下单领取优惠券",
            likes=2,
            reward_proxy=12,
            normalized_reward=12,
            performance_label="low",
        )

        history = calibrate_segment_history(target["id"], account_id="main", limit=5)

        self.assertEqual(history["history_source"], "historical_capture_samples")
        self.assertEqual(history["status"], "low_confidence")
        self.assertEqual(history["sample_count"], 2)
        self.assertGreater(history["similar_high_perf_score"], 0)
        self.assertTrue(any(match["performance_label"] == "high" for match in history["matches"]))

    def test_score_segment_uses_published_research_history_prior(self) -> None:
        target = _insert_segment()
        _insert_historical_sample(
            "hist_score_high",
            dataset_id="tianci_20260628",
            item_id="hist_score_high",
            title="导师点评改编第一次突破 副歌高音爆发 全场观众欢呼",
            likes=500,
            comments=80,
            favorites=60,
            shares=40,
            reward_proxy=95,
            normalized_reward=95,
            performance_label="high",
            content_category="节目叙事到音乐爆点型",
            hook_type="high_note",
            slice_structure="节目上下文 -> 歌曲爆点 -> 现场反应",
            tags="导师|副歌|高音",
        )
        _insert_historical_sample(
            "hist_score_low",
            dataset_id="tianci_20260628",
            item_id="hist_score_low",
            title="导师点评改编片段 副歌前插入直播间福利 下单领取优惠券",
            likes=2,
            reward_proxy=12,
            normalized_reward=12,
            performance_label="low",
            content_category="commercial",
            hook_type="ecommerce",
            slice_structure="linear",
            tags="福利|下单",
        )

        scored = score_segment(target["id"])
        signals = scored["learning_signals"]

        self.assertEqual(scored["ranker_version"], RESEARCH_RANKER_VERSION)
        self.assertEqual(signals["history_source"], "published_research_samples")
        self.assertEqual(signals["evidence_label"], "历史研究先验")
        self.assertGreater(signals["history_match_score"], 50)
        self.assertTrue(any(match["performance_label"] == "high" for match in signals["matches"]))
        self.assertTrue(signals["similar_high_samples"])
        self.assertTrue(signals["similar_low_samples"])
        self.assertTrue(signals["prototype_hits"])
        self.assertTrue(signals["low_interaction_risk_library"])
        self.assertEqual(signals["research_ranker_version"], RESEARCH_RANKER_VERSION)
        self.assertIn("component_scores", signals)
        self.assertIn("evidence_quality", signals)
        self.assertIn("ranker_advice", signals)
        self.assertIn(
            signals["ranker_advice"]["action"],
            {
                "recommend_export_preview",
                "needs_context_review",
                "low_evidence_hold",
                "low_interaction_risk_review",
            },
        )
        self.assertTrue(signals["matched_high_samples"])
        self.assertTrue(signals["matched_low_samples"])
        self.assertIn("ranker_reason", signals)
        self.assertIn(signals["confidence_label"], {"low", "medium", "high"})
        self.assertIsNotNone(signals["account_baseline_position"]["percentile"])
        self.assertIn("position_label", signals["account_baseline_position"])
        with connect() as conn:
            row = conn.execute(
                "SELECT ranker_version, learning_signals_json FROM slice_scores WHERE candidate_segment_id = ?",
                [target["id"]],
            ).fetchone()
        self.assertEqual(row["ranker_version"], RESEARCH_RANKER_VERSION)
        stored_signals = json.loads(row["learning_signals_json"])
        self.assertEqual(stored_signals["evidence_label"], "历史研究先验")
        self.assertTrue(stored_signals["prototype_hits"])

    def test_backtest_uses_historical_samples_when_training_samples_missing(self) -> None:
        for index in range(12):
            _insert_historical_sample(
                f"hist_bt_{index}",
                dataset_id="tianci_20260628",
                item_id=f"hist_bt_{index}",
                title=f"历史回测样本 {index} 高音舞台",
                likes=10 + index,
                reward_proxy=20 + index * 3,
                normalized_reward=20 + index * 3,
                performance_label="high" if index >= 8 else "mid",
            )

        report = backtest_rule_ranker(account_id="main", k=5)

        self.assertEqual(report["contract_version"], BACKTEST_VERSION)
        self.assertEqual(report["status"], "low_confidence")
        self.assertEqual(report["metrics"]["sample_source"], "historical_capture_samples")
        self.assertGreater(report["metrics"]["sample_count"], 0)
        self.assertIn("topk_lift_vs_random", report["metrics"])
        self.assertIn("high_interaction_hit_rate", report["metrics"])
        self.assertIn("low_interaction_avoidance_rate", report["metrics"])
        self.assertIn("holdout_policy", report["metrics"])
        self.assertIn("risk_note", report["metrics"])
        self.assertTrue(report["top_rows"])

    def test_rebuild_research_labels_v2_preserves_reward_proxy_and_adds_adjusted_reason(self) -> None:
        for index, reward in enumerate([12, 18, 24, 45, 70, 95]):
            _insert_historical_sample(
                f"hist_label_v2_{index}",
                dataset_id="tianci_20260628",
                item_id=f"hist_label_v2_{index}",
                title=f"标签重建样本 {index}",
                reward_proxy=reward,
                normalized_reward=reward,
                performance_label="mid",
                duration_seconds=18 if index < 3 else 55,
                published_at=f"2026-06-{10 + index:02d}T00:00:00+00:00",
                collected_at="2026-06-28T00:00:00+00:00",
            )

        result = rebuild_research_labels(account_id="main", dataset_id="tianci_20260628", min_baseline_samples=2)

        self.assertEqual(result["research_label_version"], RESEARCH_LABEL_VERSION)
        self.assertEqual(result["updated"], 6)
        self.assertEqual(result["label_counts"]["high"], 2)
        with connect() as conn:
            row = conn.execute(
                "SELECT reward_proxy, normalized_reward, performance_label, label_reason, research_label_version FROM historical_capture_samples WHERE id = ?",
                ["hist_label_v2_5"],
            ).fetchone()
        self.assertEqual(row["reward_proxy"], 95)
        self.assertEqual(row["research_label_version"], RESEARCH_LABEL_VERSION)
        self.assertIn("adjusted_visible_engagement", row["label_reason"])
        self.assertIn("age_bucket=", row["label_reason"])
        self.assertIn("duration_bucket=", row["label_reason"])

    def test_semantic_calibration_queue_and_manual_patch_are_traceable(self) -> None:
        _insert_historical_sample(
            "hist_calibrate_1",
            dataset_id="tianci_20260628",
            item_id="hist_calibrate_1",
            title="导师第一次点评后副歌高音爆发",
            reward_proxy=92,
            normalized_reward=96,
            performance_label="high",
            content_category="",
            hook_type="",
            slice_structure="",
            artist_names="",
            classification_confidence="low",
        )

        queue = semantic_calibration_queue(account_id="main", dataset_id="tianci_20260628", limit=5)
        self.assertEqual(queue["status"], "ready")
        self.assertEqual(queue["samples"][0]["id"], "hist_calibrate_1")
        self.assertTrue(any(item["field"] == "hook_type" for item in queue["samples"][0]["needs"]))
        self.assertIn("hook_type", queue["samples"][0]["suggested_fields"])
        self.assertIn("recommended_fields", queue["samples"][0])
        self.assertIn("queue_reason", queue["samples"][0])
        self.assertIn("risk_score", queue["samples"][0])
        self.assertIn("disagreement_score", queue["samples"][0])
        self.assertIn("impact_reason", queue["samples"][0])
        self.assertFalse(queue["samples"][0]["manual_verified"])
        filtered = semantic_calibration_queue(
            account_id="main",
            dataset_id="tianci_20260628",
            limit=5,
            min_priority=1,
            label="high",
            queue_type="mixed",
            strategy=RESEARCH_RANKER_V24_STRATEGY,
        )
        self.assertEqual(filtered["filters"]["label"], "high")
        self.assertEqual(filtered["filters"]["strategy"], RESEARCH_RANKER_V24_STRATEGY)
        self.assertEqual(filtered["samples"][0]["id"], "hist_calibrate_1")

        updated = update_historical_sample_labels(
            "hist_calibrate_1",
            {
                "content_category": "music_variety",
                "hook_type": "high_note",
                "slice_structure": "setup_to_payoff",
                "artist_names": ["Grace"],
                "operator": "tester",
                "reason": "calibration test",
            },
        )

        self.assertEqual(updated["sample"]["classification_confidence"], "manual_verified")
        self.assertEqual(updated["sample"]["artist_names"], "Grace")
        changes = list_change_events(entity_type="historical_capture_sample", entity_id="hist_calibrate_1")
        self.assertEqual(changes["count"], 1)
        self.assertIn("semantic_label_calibration", changes["changes"][0]["change_type"])
        post_queue = semantic_calibration_queue(account_id="main", dataset_id="tianci_20260628", limit=5)
        self.assertFalse(any(item["id"] == "hist_calibrate_1" for item in post_queue["samples"]))
        self.assertTrue(any(item["id"] == "hist_calibrate_1" for item in post_queue["recently_saved_samples"]))

        reopened = reopen_historical_sample_calibration(
            "hist_calibrate_1",
            {
                "classification_confidence": "low",
                "operator": "tester",
                "reason": "reopen for second pass",
            },
        )
        reopened_queue = semantic_calibration_queue(account_id="main", dataset_id="tianci_20260628", limit=5)
        reopened_changes = list_change_events(entity_type="historical_capture_sample", entity_id="hist_calibrate_1")

        self.assertEqual(reopened["status"], "reopened")
        self.assertEqual(reopened["sample"]["classification_confidence"], "low")
        self.assertTrue(any(item["id"] == "hist_calibrate_1" for item in reopened_queue["samples"]))
        self.assertTrue(any(item["change_type"] == "semantic_calibration_reopened" for item in reopened_changes["changes"]))

    def test_historical_backtest_returns_v2_strategy_comparison_with_time_split(self) -> None:
        for index in range(30):
            high = index % 3 == 0
            _insert_historical_sample(
                f"hist_time_bt_{index}",
                dataset_id="tianci_20260628",
                item_id=f"hist_time_bt_{index}",
                title=("副歌高音爆发 全场欢呼 " if high else "普通铺垫 舞台片段 ") + str(index),
                reward_proxy=80 + index if high else 20 + index * 0.5,
                normalized_reward=90 if high else 35,
                performance_label="high" if high else "mid",
                content_category="节目叙事到音乐爆点型" if high else "节目叙事型",
                hook_type="高音爆发" if high else "铺垫",
                slice_structure="上下文 -> 爆点 -> 反应" if high else "线性铺垫",
                classification_confidence="medium",
                duration_seconds=30 + index,
                published_at=f"2026-05-{index + 1:02d}T00:00:00+00:00",
                collected_at="2026-06-28T00:00:00+00:00",
            )

        report = backtest_rule_ranker(account_id="main", k=5, strategy=RESEARCH_RANKER_V24_STRATEGY, holdout_policy="time")

        self.assertIn("strategy_comparison", report["metrics"])
        self.assertEqual(report["metrics"]["strategy"], RESEARCH_RANKER_V24_STRATEGY)
        self.assertIn(RESEARCH_RANKER_V24_STRATEGY, report["metrics"]["strategy_comparison"])
        self.assertIn("research_ranker_v2_3", report["metrics"]["strategy_comparison"])
        self.assertIn("research_ranker_v2_2", report["metrics"]["strategy_comparison"])
        self.assertIn("research_ranker_v2_1", report["metrics"]["strategy_comparison"])
        self.assertIn("research_ranker_v2", report["metrics"]["strategy_comparison"])
        self.assertIn("semantic_baseline_v2", report["metrics"]["strategy_comparison"])
        self.assertIn("ranker_without_prototypes", report["metrics"]["component_ablation"])
        self.assertIn("promotion_gate", report["metrics"])
        self.assertIn("weight_config", report["metrics"])
        self.assertIn("baseline_gap", report["metrics"])
        self.assertIn("semantic_gap_analysis", report["metrics"])
        self.assertIn("diagnostic_samples", report["metrics"])
        self.assertIn("diversity_summary", report["metrics"])
        self.assertIn("leakage_guard_summary", report["metrics"])
        self.assertIn("next_calibration_queue", report["metrics"])
        self.assertIn("calibration_summary", report["metrics"])
        self.assertEqual(report["metrics"]["promotion_gate"]["strategy"], RESEARCH_RANKER_V24_STRATEGY)
        self.assertEqual(report["metrics"]["holdout_policy_key"], "time")
        self.assertTrue(report["top_rows"])
        self.assertIn("v24_signal_trust", report["top_rows"][0]["component_scores"])

        tuning = run_ranker_tuning(account_id="main", k=5, holdout_policy="time", max_trials=2)
        self.assertEqual(tuning["strategy"], RESEARCH_RANKER_V24_STRATEGY)
        self.assertTrue(tuning["trials"])
        self.assertIn("weight_config", tuning["best"])
        self.assertIn("promotion_gate", tuning)

        experiment = semantic_feature_experiment(account_id="main", k=5, holdout_policy="time", include_field_masks=False)
        self.assertEqual(experiment["status"], "ready")
        self.assertEqual(experiment["strategy"], RESEARCH_RANKER_V24_STRATEGY)
        self.assertIn("coverage", experiment)
        self.assertIn("base_metrics", experiment)
        self.assertIn("diagnosis", experiment)
        self.assertEqual(experiment["field_mask_ablation"], [])

    def test_research_ranker_v24_quarantines_weak_semantic_signals(self) -> None:
        gated = _v24_reliable_signal_row(
            {
                "content_category": "performance_clip",
                "hook_type": "high_note",
                "slice_structure": "setup_to_payoff",
                "structure_confidence": "medium",
                "structure_evidence": "副歌后爆点",
                "artist_names": "Grace",
                "song_title": "Grace 创作的原声",
                "original_sound_owner": "Grace",
                "is_original_sound": 1,
                "entity_signal": "Grace|原创",
                "classification_confidence": "medium",
            }
        )

        self.assertEqual(gated["hook_type"], "unknown")
        self.assertEqual(gated["slice_structure"], "unknown")
        self.assertEqual(gated["song_title"], "")
        self.assertEqual(gated["artist_names"], "Grace")
        self.assertEqual(gated["original_sound_owner"], "Grace")
        self.assertEqual(gated["entity_signal"], "Grace|原创")

        manual = _v24_reliable_signal_row(
            {
                "hook_type": "high_note",
                "slice_structure": "setup_to_payoff",
                "song_title": "真实歌名",
                "classification_confidence": "manual_verified",
            }
        )

        self.assertEqual(manual["hook_type"], "high_note")
        self.assertEqual(manual["slice_structure"], "setup_to_payoff")
        self.assertEqual(manual["song_title"], "真实歌名")

    def test_slice_structure_evaluator_builds_review_queue(self) -> None:
        _insert_historical_sample(
            "hist_structure_eval_1",
            dataset_id="tianci_20260628",
            item_id="hist_structure_eval_1",
            title="一开口高音爆发全场尖叫 Grace 舞台封神",
            reward_proxy=96,
            normalized_reward=98,
            performance_label="high",
            content_category="performance_clip",
            hook_type="high_note",
            slice_structure="unknown",
            classification_confidence="medium",
        )
        _insert_historical_sample(
            "hist_structure_eval_2",
            dataset_id="tianci_20260628",
            item_id="hist_structure_eval_2",
            title="日常记录彩排过程 vlog",
            reward_proxy=22,
            normalized_reward=18,
            performance_label="low",
            content_category="behind_the_scenes",
            hook_type="daily_moment",
            slice_structure="climax_first",
            classification_confidence="medium",
        )

        row_eval = evaluate_slice_structure_row(
            {
                "id": "row_only",
                "title": "一开口高音爆发",
                "slice_structure": "unknown",
                "classification_confidence": "medium",
            }
        )
        report = evaluate_slice_structure(account_id="main", dataset_id="tianci_20260628")

        self.assertEqual(row_eval["suggested_structure"], "climax_first")
        self.assertEqual(report["status"], "ready")
        self.assertGreaterEqual(report["coverage"]["evaluator_known_count"], 2)
        self.assertTrue(any(item["status"] == "suggested_update" for item in report["review_queue"]))
        self.assertTrue(any(item["status"] == "conflict_review" for item in report["review_queue"]))
        self.assertTrue(report["recommendations"])

    def test_multimodal_validation_builds_collection_plan_and_asset_gate(self) -> None:
        ready_id = "7655575210669722907"
        missing_id = "7656046228765994171"
        low_id = "7650000000000000000"
        _insert_historical_sample(
            "hist_mm_ready",
            dataset_id="tianci_20260628",
            item_id=ready_id,
            title="一开口高音爆发全场尖叫 舞台封神",
            reward_proxy=96,
            normalized_reward=98,
            performance_label="high",
            content_category="performance_highlight",
            hook_type="high_note",
            slice_structure="climax_first",
            tags="高音|舞台|全场",
        )
        _insert_historical_sample(
            "hist_mm_missing",
            dataset_id="tianci_20260628",
            item_id=missing_id,
            title="副歌合唱观众泪目 这段值得切",
            reward_proxy=86,
            normalized_reward=88,
            performance_label="high",
            content_category="performance_highlight",
            hook_type="chorus",
            slice_structure="chorus_first",
            tags="副歌|合唱|观众",
        )
        _insert_historical_sample(
            "hist_mm_low",
            dataset_id="tianci_20260628",
            item_id=low_id,
            title="王力宏现场震全场 低互动舞台片段",
            reward_proxy=12,
            normalized_reward=16,
            performance_label="low",
            content_category="performance_highlight",
            hook_type="live_stage",
            slice_structure="linear",
            tags="王力宏|现场|舞台",
        )
        asset_root = self.root / "data" / "douyin_media_assets" / "main" / "beta_d1"
        for folder, suffix in [("videos", ".mp4"), ("covers", ".jpg"), ("audio", ".wav")]:
            target = asset_root / folder / f"{ready_id}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"asset")

        plan = build_multimodal_collection_plan(account_id="main", dataset_id="tianci_20260628", limit=2)
        validation = run_multimodal_validation(account_id="main", dataset_id="tianci_20260628", limit=10, min_samples=1, min_asset_coverage=0.8)
        dry_run = collect_multimodal_assets(plan_path=plan["plan_path"], limit=1, dry_run=True)

        self.assertEqual(plan["validation_version"], MULTIMODAL_VALIDATION_VERSION)
        self.assertTrue(Path(plan["plan_path"]).exists())
        self.assertEqual(plan["sample_count"], 2)
        self.assertTrue(any(item["aweme_id"] == missing_id for item in plan["samples"]))
        self.assertEqual(validation["validation_version"], MULTIMODAL_VALIDATION_VERSION)
        self.assertIn("asset_readiness", validation)
        self.assertLess(validation["asset_readiness"]["coverage"]["ready_for_multimodal"]["rate"], 0.8)
        self.assertEqual(validation["promotion_gate"]["decision"], "collect_assets_first")
        self.assertEqual(dry_run["collection_mode"], "dry_run")
        self.assertEqual(dry_run["planned"], 1)

    def test_multimodal_feature_experiment_extracts_real_audio_features(self) -> None:
        high_id = "7651111111111111111"
        low_id = "7651111111111111112"
        mid_id = "7651111111111111113"
        _insert_historical_sample(
            "hist_mm_feature_high",
            dataset_id="tianci_20260628",
            item_id=high_id,
            title="高音爆发 全场尖叫 舞台高光",
            reward_proxy=96,
            normalized_reward=96,
            performance_label="high",
            content_category="performance_highlight",
            hook_type="high_note",
            slice_structure="climax_first",
            classification_confidence="medium",
        )
        _insert_historical_sample(
            "hist_mm_feature_low",
            dataset_id="tianci_20260628",
            item_id=low_id,
            title="平铺直叙 低互动片段",
            reward_proxy=12,
            normalized_reward=12,
            performance_label="low",
            content_category="performance_highlight",
            hook_type="live_stage",
            slice_structure="linear",
            classification_confidence="medium",
        )
        _insert_historical_sample(
            "hist_mm_feature_mid",
            dataset_id="tianci_20260628",
            item_id=mid_id,
            title="副歌合唱 观众回应",
            reward_proxy=52,
            normalized_reward=52,
            performance_label="mid",
            content_category="performance_highlight",
            hook_type="chorus",
            slice_structure="chorus_first",
            classification_confidence="medium",
        )
        audio_root = self.root / "data" / "douyin_media_assets" / "main" / "beta_d2" / "audio"
        _write_test_wav(audio_root / f"{high_id}.wav", amplitudes=[1800, 6000, 13000, 22000])
        _write_test_wav(audio_root / f"{low_id}.wav", amplitudes=[120, 160, 120, 160])
        _write_test_wav(audio_root / f"{mid_id}.wav", amplitudes=[900, 1300, 1600, 1400])

        result = run_multimodal_feature_experiment(
            account_id="main",
            dataset_id="tianci_20260628",
            limit=10,
            k=1,
            min_feature_samples=1,
            audio_window_seconds=2.0,
            force=True,
        )

        diagnostics = result["feature_diagnostics"]
        self.assertEqual(result["feature_version"], MULTIMODAL_FEATURE_VERSION)
        self.assertEqual(result["feature_ready_count"], 3)
        self.assertEqual(result["audio_ready_count"], 3)
        self.assertIn("semantic_plus_audio", result["strategy_comparison"])
        self.assertIn("semantic_plus_audio_visual", result["strategy_comparison"])
        self.assertIn("promotion_gate", result)
        self.assertGreater(
            diagnostics["by_label"]["high"]["avg_audio_score"],
            diagnostics["by_label"]["low"]["avg_audio_score"],
        )

    def test_research_ranker_v22_uses_semantic_weight_and_positive_evidence(self) -> None:
        base_components = {
            "account_baseline_position": 60.0,
            "high_similarity": 0.0,
            "low_interaction_risk": 0.0,
            "prototype_fit": 0.0,
            "semantic_label_trust": 50.0,
            "long_tail_novelty": 35.0,
            "best_similarity": 0.8,
        }

        unweighted = _score_v22_from_components(
            base_components,
            config={
                "semantic_strong_weight": 1.0,
                "semantic_floor_weight": 1.0,
                "high_similarity_weight": 0.0,
                "low_risk_weight": 0.0,
                "prototype_weight": 0.0,
            },
        )
        damped = _score_v22_from_components(
            base_components,
            config={
                "semantic_strong_weight": 0.5,
                "semantic_floor_weight": 1.0,
                "high_similarity_weight": 0.0,
                "low_risk_weight": 0.0,
                "prototype_weight": 0.0,
            },
        )
        positive = _score_v22_from_components(
            {
                **base_components,
                "account_baseline_position": 50.0,
                "high_similarity": 82.0,
                "prototype_fit": 45.0,
            }
        )

        self.assertLess(damped, unweighted)
        self.assertGreater(positive, 50.0)

    def test_research_ranker_v22_gates_low_interaction_risk(self) -> None:
        strong_evidence = {
            "account_baseline_position": 50.0,
            "high_similarity": 72.0,
            "low_interaction_risk": 40.0,
            "prototype_fit": 0.0,
            "semantic_label_trust": 50.0,
            "long_tail_novelty": 35.0,
            "best_similarity": 0.8,
        }
        risky = {
            **strong_evidence,
            "high_similarity": 20.0,
            "low_interaction_risk": 95.0,
        }

        self.assertGreater(_score_v22_from_components(strong_evidence), 50.0)
        self.assertLess(_score_v22_from_components(risky), _score_v22_from_components(strong_evidence))

    def test_research_ranker_v23_penalizes_near_duplicate_topk(self) -> None:
        rows = [
            {
                "training_sample_id": "dup_a",
                "title": "王铮亮 张远《故乡的云》直拍 #声生不息",
                "song_title": "故乡的云",
                "artist_names": "王铮亮|张远",
                "content_category": "performance_clip",
                "strategy_scores": {RESEARCH_RANKER_V23_STRATEGY: 66.0},
                "component_scores": {},
            },
            {
                "training_sample_id": "dup_b",
                "title": "王铮亮 张远《故乡的云》直拍 #声生不息",
                "song_title": "故乡的云",
                "artist_names": "王铮亮|张远",
                "content_category": "performance_clip",
                "strategy_scores": {RESEARCH_RANKER_V23_STRATEGY: 65.5},
                "component_scores": {},
            },
            {
                "training_sample_id": "unique",
                "title": "单依纯《橄榄树》舞台评价",
                "song_title": "橄榄树",
                "artist_names": "单依纯",
                "content_category": "judge_comment",
                "strategy_scores": {RESEARCH_RANKER_V23_STRATEGY: 64.0},
                "component_scores": {},
            },
        ]

        adjusted = _apply_v23_diversity(rows)
        scores = {
            row["training_sample_id"]: row["strategy_scores"][RESEARCH_RANKER_V23_STRATEGY]
            for row in adjusted
        }
        penalties = {
            row["training_sample_id"]: row["component_scores"].get("v23_diversity_penalty", 0)
            for row in adjusted
        }

        self.assertEqual(penalties["dup_a"], 0)
        self.assertGreater(penalties["dup_b"], 0)
        self.assertLess(scores["dup_b"], scores["unique"])

    def test_douyin_research_classification_filters_noisy_artist_tags(self) -> None:
        classified = classify_published_work(
            title="陶喆终于找到亲传弟子？ 声乐老师reaction陶喆 檀健次《荷塘月色》",
            tags=["#檀健次陶喆", "#陶喆", "#檀健次", "#荷塘月色", "#reaction", "#青年创作者成长计划"],
            aweme_id="noise_1001",
        )

        artists = classified["artist_names"].split("|")
        self.assertIn("陶喆", artists)
        self.assertIn("檀健次", artists)
        self.assertNotIn("reaction", artists)
        self.assertNotIn("青年创作者成长计划", artists)
        self.assertEqual(classified["hook_type"], "reaction")

    def test_douyin_research_classification_does_not_overuse_pairing_for_rank_lists(self) -> None:
        classified = classify_published_work(
            title="#歌手总决赛名单 经过多轮激烈角逐，@陈楚生 米奇·盖顿@Mickey Guyton @单依纯 @李佳薇 七位歌王候选人成功晋级",
            tags=["#歌手2025", "#歌手总决赛名单", "#歌手晋级"],
            aweme_id="rank_1001",
        )

        self.assertNotEqual(classified["hook_type"], "celebrity_pairing")
        self.assertEqual(classified["content_category"], "judge_comment")
        self.assertEqual(classified["slice_structure"], "setup_to_payoff")

    def test_douyin_research_classification_keeps_real_pairing_evidence(self) -> None:
        classified = classify_published_work(
            title="单依纯和汪苏泷合唱《如果爱忘了》声线太默契，现场全场泪目",
            tags=["#单依纯", "#汪苏泷", "#如果爱忘了"],
            aweme_id="pair_1001",
        )

        self.assertEqual(classified["hook_type"], "celebrity_pairing")
        self.assertIn("单依纯", classified["artist_names"])
        self.assertIn("汪苏泷", classified["artist_names"])

    def test_douyin_research_classification_outputs_structure_evidence_and_original_sound_owner(self) -> None:
        classified = classify_published_work(
            title="一开口高音炸场！侯明昊《笼》舞台直拍",
            tags=["#侯明昊", "#笼"],
            aweme_id="structure_1001",
            existing={"music_title": "@歌手2026创作的原声"},
        )

        self.assertEqual(classified["slice_structure"], "climax_first")
        self.assertEqual(classified["structure_confidence"], "high")
        self.assertEqual(classified["structure_evidence"], "一开口")
        self.assertEqual(classified["song_title"], "笼")
        self.assertEqual(classified["original_sound_owner"], "歌手2026")
        self.assertEqual(classified["is_original_sound"], "1")
        self.assertEqual(classified["entity_signal"], "artist:侯明昊")

    def test_douyin_clean_history_import_backfills_research_semantics(self) -> None:
        clean_dir = self.root / "data" / "douyin_capture" / "geshou2026" / "clean_20260628T010000_appleevents_api"
        raw_dir = self.root / "data" / "douyin_capture" / "geshou2026" / "raw_20260628T010000_appleevents_api"
        clean_dir.mkdir(parents=True)
        raw_dir.mkdir(parents=True)
        (clean_dir / "douyin_visible_works_dedup_latest.json").write_text(
            json.dumps(
                [
                    {
                        "aweme_id": "sem_1001",
                        "normalized_title": "歌手2026 万妮达副歌高音爆发 舞台燃炸",
                        "best_visible_count_number": 66000,
                        "tags": ["歌手2026", "万妮达", "副歌"],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (raw_dir / "geshou2026_post_api_works.json").write_text(
            json.dumps(
                [
                    {
                        "aweme_id": "sem_1001",
                        "digg_count": 66000,
                        "comment_count": 320,
                        "share_count": 120,
                        "collect_count": 520,
                        "duration": 39000,
                        "create_time": 1782604800,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        import_douyin_history("geshou2026", clean_dir, dataset_id="geshou2026_20260628", force=True)
        listed = list_historical_samples("geshou2026", dataset_id="geshou2026_20260628", limit=5)
        coverage = research_field_coverage(account_id="geshou2026", dataset_id="geshou2026_20260628")

        sample = listed["samples"][0]
        self.assertEqual(sample["content_category"], "performance_clip")
        self.assertEqual(sample["hook_type"], "high_note")
        self.assertEqual(sample["slice_structure"], "pure_highlight")
        self.assertEqual(sample["structure_confidence"], "medium")
        self.assertEqual(sample["structure_evidence"], "副歌")
        self.assertEqual(sample["program_name"], "歌手2026")
        self.assertIn("万妮达", sample["artist_names"])
        self.assertEqual(sample["semantic_feature_version"], SEMANTIC_FEATURE_VERSION)
        self.assertEqual(sample["research_label_version"], RESEARCH_LABEL_VERSION)
        self.assertEqual(sample["classification_confidence"], "high")
        self.assertEqual(coverage["status"], "ready")
        self.assertEqual(coverage["coverage"]["content_category"]["rate"], 1.0)
        self.assertEqual(coverage["coverage"]["hook_type"]["rate"], 1.0)
        self.assertIn("artist_names", coverage["usable_dimensions"])

    def test_semantic_features_backfill_updates_unknown_structure_without_manual_label(self) -> None:
        _insert_historical_sample(
            "hist_sem_backfill_1",
            dataset_id="geshou2026_20260628",
            item_id="hist_sem_backfill_1",
            title="一开口高音炸场！侯明昊《笼》舞台直拍",
            reward_proxy=80,
            normalized_reward=90,
            performance_label="high",
            content_category="unknown",
            hook_type="unknown",
            slice_structure="unknown",
            artist_names="",
            song_title="@歌手2026创作的原声",
            tags="侯明昊|笼",
            classification_confidence="medium",
        )

        result = backfill_semantic_features(account_id="main", dataset_id="geshou2026_20260628", force=True)
        sample = list_historical_samples("main", dataset_id="geshou2026_20260628", limit=1)["samples"][0]

        self.assertEqual(result["semantic_feature_version"], SEMANTIC_FEATURE_VERSION)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(sample["slice_structure"], "climax_first")
        self.assertEqual(sample["structure_confidence"], "high")
        self.assertEqual(sample["structure_evidence"], "一开口")
        self.assertEqual(sample["song_title"], "笼")
        self.assertEqual(sample["original_sound_owner"], "歌手2026")
        self.assertTrue(sample["is_original_sound"])
        self.assertEqual(sample["entity_signal"], "artist:侯明昊")

    def test_douyin_clean_history_import_labels_baselines_and_prototypes(self) -> None:
        clean_dir = self.root / "data" / "douyin_capture" / "tianci" / "clean_20260628T000000_appleevents_api"
        raw_dir = self.root / "data" / "douyin_capture" / "tianci" / "raw_20260628T000000_appleevents_api"
        clean_dir.mkdir(parents=True)
        raw_dir.mkdir(parents=True)
        (clean_dir / "douyin_visible_works_dedup_latest.json").write_text(
            json.dumps(
                [
                    {
                        "account_key": "tianci",
                        "aweme_id": "1001",
                        "normalized_title": "高音转调现场全场沸腾",
                        "best_visible_count_number": 120000,
                        "content_category": "performance_highlight",
                        "hook_type": "music_burst",
                        "slice_structure": "climax_first",
                        "program_name": "天赐的声音",
                        "artist_names": ["歌手A"],
                        "song_title": "测试歌",
                        "tags": ["天赐的声音", "高音"],
                        "last_observed_at": "2026-06-28T00:00:00+00:00",
                        "video_url": "https://www.douyin.com/video/1001",
                    },
                    {
                        "account_key": "tianci",
                        "aweme_id": "1002",
                        "normalized_title": "导师点评改编思路",
                        "best_visible_count_number": 32000,
                        "content_category": "commentary",
                        "hook_type": "expert_comment",
                        "slice_structure": "context_first",
                        "program_name": "天赐的声音",
                        "artist_names": ["歌手B"],
                        "tags": ["天赐的声音", "点评"],
                        "last_observed_at": "2026-06-28T00:00:00+00:00",
                    },
                    {
                        "account_key": "tianci",
                        "aweme_id": "1003",
                        "normalized_title": "后台花絮轻松互动",
                        "best_visible_count_number": 900,
                        "content_category": "behind_scene",
                        "hook_type": "daily_moment",
                        "slice_structure": "linear",
                        "program_name": "天赐的声音",
                        "artist_names": ["歌手C"],
                        "tags": ["天赐的声音", "花絮"],
                        "last_observed_at": "2026-06-28T00:00:00+00:00",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (clean_dir / "douyin_collection_quality_latest.json").write_text(
            json.dumps(
                {
                    "quality_grade": "A",
                    "quality_score": 0.97,
                    "work_card_count_deduped": 3,
                    "work_card_count_raw": 3,
                    "estimated_duplicate_ratio": 0,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (raw_dir / "tianci_post_api_works.json").write_text(
            json.dumps(
                [
                    {"aweme_id": "1001", "desc": "高音转调现场全场沸腾", "digg_count": 120000, "comment_count": 1800, "share_count": 900, "collect_count": 2400, "duration": 45000, "create_time": 1782604800},
                    {"aweme_id": "1002", "desc": "导师点评改编思路", "digg_count": 32000, "comment_count": 180, "share_count": 80, "collect_count": 260, "duration": 32000, "create_time": 1782608400},
                    {"aweme_id": "1003", "desc": "后台花絮轻松互动", "digg_count": 900, "comment_count": 3, "share_count": 1, "collect_count": 2, "duration": 18000, "create_time": 1782612000},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        output_dir = self.root / "outputs" / "douyin_history_assets"
        imported = import_douyin_history(
            "tianci",
            clean_dir,
            dataset_id="tianci_20260628",
            output_dir=output_dir,
            force=True,
        )
        listed = list_historical_samples("tianci", dataset_id="tianci_20260628", limit=10)
        baselines = douyin_history_baselines("tianci", dataset_id="tianci_20260628", min_count=1)
        summary = historical_sample_summary("tianci")
        prototypes = build_prototype_bank("tianci", source="visible_capture", dataset_id="tianci_20260628", limit=10, force=True)

        self.assertEqual(imported["contract_version"], DOUYIN_HISTORY_VERSION)
        self.assertEqual(imported["inserted"], 3)
        self.assertEqual(imported["source_row_count"], 3)
        self.assertEqual(imported["source_unique_count"], 3)
        self.assertEqual(imported["stored_sample_count"], 3)
        self.assertEqual(imported["label_counts"]["high"], 1)
        self.assertEqual(imported["label_counts"]["low"], 1)
        self.assertEqual(listed["count"], 3)
        self.assertTrue(any(sample["performance_label"] == "high" for sample in listed["samples"]))
        self.assertTrue(all(sample["reward_proxy"] > 0 for sample in listed["samples"]))
        self.assertEqual(summary["source_row_count"], 3)
        self.assertEqual(summary["source_unique_count"], 3)
        self.assertEqual(summary["stored_sample_count"], 3)
        self.assertEqual(summary["trainable_sample_count"], 3)
        self.assertEqual(summary["metric_coverage"]["likes"]["rate"], 1.0)
        self.assertEqual(summary["metric_coverage"]["favorites"]["rate"], 1.0)
        self.assertEqual(summary["metric_coverage"]["comments"]["rate"], 1.0)
        self.assertEqual(summary["metric_coverage"]["shares"]["rate"], 1.0)
        self.assertEqual(summary["play_missing_count"], 3)
        self.assertEqual(summary["play_missing_rate"], 1.0)
        self.assertEqual(baselines["sample_count"], 3)
        self.assertEqual(baselines["label_counts"]["mid"], 1)
        self.assertTrue(any(item["dimension"] == "hook_type" for item in baselines["top_signals"]))
        self.assertEqual(prototypes["sample_count"], 3)
        self.assertEqual(prototypes["account_distribution"]["performance_basis"], "reward_proxy")
        self.assertGreater(prototypes["account_distribution"]["p75_performance"], 0)
        self.assertTrue(prototypes["prototypes"])
        top_prototype = prototypes["prototypes"][0]
        self.assertEqual(top_prototype["parameters"]["performance_metric"]["basis"], "reward_proxy")
        self.assertGreater(top_prototype["parameters"]["performance_metric"]["p75"], 0)
        self.assertTrue(top_prototype["parameters"]["absolute_level"]["code"].startswith("I"))
        self.assertTrue((output_dir / "history_samples_latest.json").exists())
        self.assertTrue((output_dir / "account_baselines_latest.json").exists())

    def test_historical_capture_import_all_keeps_dataset_isolation(self) -> None:
        tianci_path = self.root / "outputs" / "douyin_tianci_20260627" / "tianci_douyin_visible_collection_latest.xlsx"
        geshou_path = self.root / "outputs" / "douyin_geshou2026_20260627" / "geshou2026_douyin_visible_collection_latest.xlsx"
        _write_xlsx_rows(
            tianci_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签", "钩子类型"],
                ["1", "120000", "ID:tianci_regret", "欢子黄霄雲是你没选我啊唱尽遗憾", "#天赐的声音|#没选我", "emotional_story"],
            ],
        )
        _write_xlsx_rows(
            geshou_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签", "钩子类型"],
                ["1", "140000", "ID:geshou_rank", "歌手2026第六期排名预测 袭榜结果悬念", "#歌手2026|#排名", "unknown"],
            ],
        )

        imported = import_historical_samples("main", dataset_id="all", force=True)
        summary = historical_sample_summary("main")

        self.assertEqual(imported["valid_rows"], 2)
        self.assertEqual(imported["sample_count"], 2)
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual({item["dataset_id"] for item in summary["datasets"]}, {"tianci_20260627", "geshou2026_20260627"})
        with connect() as conn:
            all_rows = conn.execute(
                "SELECT COUNT(*) AS count FROM historical_capture_samples WHERE dataset_id = 'all'"
            ).fetchone()["count"]
        self.assertEqual(all_rows, 0)

    def test_historical_capture_import_all_dedupes_video_across_batches(self) -> None:
        old_path = self.root / "outputs" / "douyin_tianci_20260627" / "tianci_douyin_visible_collection_latest.xlsx"
        new_path = self.root / "outputs" / "douyin_tianci_20260628" / "tianci_douyin_visible_collection_latest.xlsx"
        _write_xlsx_rows(
            old_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签"],
                ["1", "120000", "7650000000000000001", "旧批次重复视频", "#天赐的声音"],
            ],
        )
        _write_xlsx_rows(
            new_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签"],
                ["1", "180000", "7650000000000000001", "新批次重复视频", "#天赐的声音"],
            ],
        )

        imported = import_historical_samples("main", dataset_id="all", force=True)
        summary = historical_sample_summary("main")

        self.assertEqual(imported["valid_rows"], 2)
        self.assertEqual(imported["sample_count"], 1)
        self.assertEqual(imported["deduped"], 1)
        self.assertEqual(summary["sample_count"], 1)
        with connect() as conn:
            row = conn.execute(
                """
                SELECT dataset_id, title, views
                FROM historical_capture_samples
                WHERE platform_item_id = '7650000000000000001'
                """
            ).fetchone()
        self.assertEqual(row["dataset_id"], "tianci_20260628")
        self.assertEqual(row["title"], "新批次重复视频")
        self.assertEqual(row["views"], 180000)

    def test_historical_capture_dedup_prefers_douyin_clean_raw_metrics_without_fake_views(self) -> None:
        item_id = "7650000000000000099"
        xlsx_path = self.root / "outputs" / "douyin_tianci_20260627" / "tianci_douyin_visible_collection_latest.xlsx"
        _write_xlsx_rows(
            xlsx_path,
            "作品去重",
            [
                ["排名", "计数数值", "视频ID文本", "标题", "话题标签"],
                ["1", "88000", item_id, "xlsx 可见计数作品", "#天赐的声音"],
            ],
        )
        import_historical_samples("main", dataset_id="tianci_20260627", force=True)

        clean_dir = self.root / "data" / "douyin_capture" / "main" / "clean_20260628T000000_appleevents_api"
        raw_dir = self.root / "data" / "douyin_capture" / "main" / "raw_20260628T000000_appleevents_api"
        clean_dir.mkdir(parents=True)
        raw_dir.mkdir(parents=True)
        (clean_dir / "douyin_visible_works_dedup_latest.json").write_text(
            json.dumps(
                [
                    {
                        "aweme_id": item_id,
                        "normalized_title": "json raw 指标作品",
                        "best_visible_count_number": 88000,
                        "content_category": "performance_highlight",
                        "hook_type": "music_burst",
                        "last_observed_at": "2026-06-28T00:00:00+00:00",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (raw_dir / "main_post_api_works.json").write_text(
            json.dumps(
                [
                    {
                        "aweme_id": item_id,
                        "digg_count": 4321,
                        "comment_count": 98,
                        "collect_count": 76,
                        "share_count": 54,
                        "duration": 45000,
                        "create_time": 1782604800,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        imported = import_douyin_history("main", clean_dir, dataset_id="tianci_20260628", force=True)
        listed = list_historical_samples("main", limit=10)

        self.assertEqual(imported["inserted"], 0)
        self.assertEqual(imported["updated"], 1)
        self.assertEqual(imported["deduped"], 1)
        self.assertEqual(imported["sample_count"], 1)
        self.assertEqual(listed["count"], 1)
        self.assertTrue(listed["samples"][0]["play_count_missing"])
        self.assertEqual(listed["samples"][0]["metric_source"], "raw_api")
        self.assertEqual(listed["samples"][0]["metric_window"], "lifetime_at_capture")
        with connect() as conn:
            row = conn.execute(
                """
                SELECT source_kind, dataset_id, views, likes, comments, favorites, shares, reward_proxy, raw_json,
                       COUNT(*) OVER () AS total
                FROM historical_capture_samples
                WHERE account_id = 'main' AND platform = 'douyin' AND platform_item_id = ?
                """,
                [item_id],
            ).fetchone()
        raw = json.loads(row["raw_json"])
        self.assertEqual(row["total"], 1)
        self.assertEqual(row["source_kind"], "douyin_clean_json")
        self.assertEqual(row["dataset_id"], "tianci_20260628")
        self.assertEqual(row["views"], 0)
        self.assertEqual(row["likes"], 4321)
        self.assertNotEqual(row["likes"], row["views"])
        self.assertEqual(row["comments"], 98)
        self.assertEqual(row["favorites"], 76)
        self.assertEqual(row["shares"], 54)
        self.assertGreater(row["reward_proxy"], 0)
        self.assertTrue(raw["metric_quality"]["play_count_missing"])
        self.assertEqual(raw["metric_quality"]["metric_source"], "raw_api")
        self.assertEqual(raw["metric_quality"]["metric_window"], "lifetime_at_capture")

    def test_prototype_bank_matches_local_candidate_segment(self) -> None:
        _insert_segment()
        target = _insert_extra_segment(
            "seg_regret_target",
            "欢子黄霄雲这一句是你没选我啊 把遗憾和青春错过唱得太具体",
            "遗憾情绪副歌型",
        )
        capture_path = self.root / "data" / "douyin_capture" / "douyin_visible_works_dedup_latest.csv"
        _write_visible_work_rows(
            capture_path,
            [
                {
                    "work_key": "work_regret",
                    "normalized_title": "#欢子黄霄雲是你没选我啊唱尽遗憾 #欢子黄霄雲把遗憾唱得太具体了",
                    "tags": "#欢子黄霄雲是你没选我啊唱尽遗憾|#欢子|#黄霄雲",
                    "hook_type": "emotional_story",
                    "artist_names": "欢子|黄霄雲",
                    "best_visible_count_number": "120000",
                    "last_observed_at": "2026-06-27T13:40:12+00:00",
                },
                {
                    "work_key": "work_blast",
                    "normalized_title": "副歌高音转调爆发 全场观众起立欢呼",
                    "tags": "#高音|#转调|#全场欢呼",
                    "hook_type": "music_burst",
                    "best_visible_count_number": "54000",
                    "last_observed_at": "2026-06-27T20:00:00+00:00",
                },
            ],
        )
        build_prototype_bank("main", source="external", limit=10, force=True)

        matched = match_segment_prototypes(target["id"], account_id="main", limit=3)

        self.assertEqual(matched["contract_version"], PROTOTYPE_BANK_VERSION)
        self.assertTrue(matched["matches"])
        self.assertEqual(matched["matches"][0]["prototype_name"], "遗憾共鸣型")
        self.assertGreater(matched["matches"][0]["fit_score"], 0)

    def test_ffprobe_if_ffmpeg_available(self) -> None:
        video_path = self.root / "demo.mp4"
        try:
            run_cmd(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=320x180:rate=25",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000",
                    "-t",
                    "2",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(video_path),
                ]
            )
        except Exception:
            self.skipTest("ffmpeg unavailable")
        metadata = probe_video(video_path)
        self.assertGreater(metadata["duration_seconds"], 0)
        self.assertEqual(metadata["width"], 320)

    def test_overlapping_transcript_clips_to_segment_boundaries(self) -> None:
        transcript_path = self.root / "transcript.json"
        transcript_path.write_text(
            """
            {
              "segments": [
                {"start": 5, "end": 15, "text": "before"},
                {"start": 15, "end": 25, "text": "across"},
                {"start": 25, "end": 30, "text": "after"}
              ]
            }
            """,
            encoding="utf-8",
        )
        rows = _overlapping_transcript({"transcript_path": str(transcript_path)}, 10, 20)
        self.assertEqual(
            [(row["start"], row["end"], row["text"]) for row in rows],
            [(10, 15.0, "before"), (15.0, 20, "across")],
        )

    def test_export_allows_trusted_sample_and_writes_clipped_subtitles(self) -> None:
        os.environ["DSO_RIGHTS_MODE"] = "trusted_sample"
        segment = _insert_segment()
        transcript_path = self.root / "transcript.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "segments": [
                        {"start": 4, "end": 8, "text": "outside-before"},
                        {"start": 5, "end": 15, "text": "lead-in"},
                        {"start": 20, "end": 30, "text": "middle"},
                        {"start": 40, "end": 50, "text": "tail"},
                        {"start": 43, "end": 50, "text": "outside-after"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE source_videos SET transcript_path = ? WHERE id = ?",
                [str(transcript_path), segment["source_video_id"]],
            )
            conn.commit()

        def fake_export(_video_path: Path, output_path: Path, start: float, end: float, subtitle_path: Path) -> None:
            self.assertEqual(start, 10.0)
            self.assertEqual(end, 42.0)
            self.assertTrue(subtitle_path.is_file())
            output_path.write_bytes(b"fake mp4")

        def fake_cover(_video_path: Path, cover_path: Path, _time: float) -> None:
            cover_path.write_bytes(b"fake jpg")

        with patch("dso.variants.exporter.export_vertical_clip", side_effect=fake_export) as export_mock, patch(
            "dso.variants.exporter.extract_frame", side_effect=fake_cover
        ) as cover_mock:
            result = export_segment(segment["id"])

        self.assertEqual(result["rights_risk"], 0.0)
        self.assertEqual(result["rights_mode"], "trusted_sample")
        self.assertIn("sample", " ".join(result["rights_notes"]))
        self.assertEqual(result["component_versions"]["segmenter"], SEGMENTER_VERSION)
        self.assertEqual(result["component_versions"]["scorer"], SCORER_VERSION)
        self.assertTrue(Path(result["export_path"]).is_file())
        self.assertTrue(Path(result["cover_path"]).is_file())
        subtitle_text = Path(result["subtitle_path"]).read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:05,000", subtitle_text)
        self.assertIn("lead-in", subtitle_text)
        self.assertIn("00:00:10,000 --> 00:00:20,000", subtitle_text)
        self.assertIn("middle", subtitle_text)
        self.assertIn("00:00:30,000 --> 00:00:32,000", subtitle_text)
        self.assertIn("tail", subtitle_text)
        self.assertNotIn("outside-before", subtitle_text)
        self.assertNotIn("outside-after", subtitle_text)
        export_mock.assert_called_once()
        cover_mock.assert_called_once()

    def test_export_preflight_blocks_manually_blocked_candidate(self) -> None:
        segment = _insert_segment()
        score_segment(segment["id"])
        mark_candidate_review(segment["id"], "blocked", reason="授权待确认", operator="tester")

        preflight = export_preflight(segment["id"])

        self.assertEqual(preflight["status"], "block")
        self.assertFalse(preflight["can_export"])
        self.assertIn("manual_blocked", {reason["key"] for reason in preflight["reasons"]})
        with self.assertRaises(PermissionError):
            export_segment(segment["id"])

    def test_runtime_diagnostics_and_cli_doctor_are_json_serializable(self) -> None:
        diagnostics = runtime_diagnostics()
        doctor = cmd_doctor()

        json.dumps(diagnostics, ensure_ascii=False)
        json.dumps(doctor, ensure_ascii=False)
        self.assertEqual(diagnostics["rights_mode"], "trusted_sample")
        self.assertIn("ffmpeg", diagnostics)
        self.assertIn("ffprobe", diagnostics)
        self.assertIn("asr", diagnostics)
        self.assertIn("profile_plan", diagnostics["asr"])
        self.assertEqual(diagnostics["asr"]["profile_plan"]["profiles_by_name"]["verify"]["model"], "large-v3-turbo-q5_0")
        self.assertTrue(all(isinstance(value, str) for value in diagnostics["paths"].values()))
        self.assertEqual(doctor["paths"]["db_path"], diagnostics["paths"]["db_path"])

    def test_zh_hans_normalizes_common_asr_traditional_characters(self) -> None:
        self.assertEqual(to_zh_hans("我們有兩個人"), "我们有两个人")

    def test_dashboard_renders_vue_shell_and_initial_state(self) -> None:
        html = render_dashboard({"videos": 0, "segments": 0, "exports": 0, "training_samples": 0}, [])

        self.assertIn('meta name="dso-frontend" content="vue3-vite-typescript"', html)
        self.assertIn('id="dso-initial-state"', html)
        self.assertIn('"training_samples": 0', html)
        self.assertIn('<div id="app"></div>', html)
        self.assertIn("/static/dashboard/assets/", html)
        self.assertNotIn("__DSO_INITIAL_STATE__", html)

    def test_dashboard_initial_state_escapes_script_end_tags(self) -> None:
        html = render_dashboard(
            {"videos": 1, "segments": 0, "exports": 0, "training_samples": 0},
            [{"id": "video_demo", "title": "</script><p>bad</p>"}],
        )

        self.assertIn("<\\/script><p>bad<\\/p>", html)
        self.assertNotIn("</script><p>bad</p>", html)

    def test_web_command_reports_missing_dependencies_before_starting_server(self) -> None:
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name in {"fastapi", "uvicorn"}:
                raise ModuleNotFoundError(name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(RuntimeError, "Web UI requires FastAPI and Uvicorn"):
                cmd_web()

    def test_argparse_web_command_exits_with_clear_dependency_error(self) -> None:
        real_import = __import__
        stderr = io.StringIO()

        def fake_import(name, *args, **kwargs):
            if name in {"fastapi", "uvicorn"}:
                raise ModuleNotFoundError(name)
            return real_import(name, *args, **kwargs)

        with patch.object(sys, "argv", ["dso", "web"]), patch("sys.stderr", stderr), patch(
            "builtins.__import__", side_effect=fake_import
        ):
            with self.assertRaises(SystemExit) as raised:
                _argparse_main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Web UI requires FastAPI and Uvicorn", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


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


def _insert_extra_segment(segment_id: str, transcript: str, music_slice_type: str) -> dict:
    now = "2026-06-23T00:00:00+00:00"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO candidate_segments
            (id, source_video_id, performance_id, start_time, end_time, duration_seconds, transcript, summary, primary_topic, song_section_type,
             music_slice_type, emotion_type, short_video_structure, musical_moment, program_context, comment_trigger, cover_time, status, created_at)
            VALUES (?, 'video_demo', NULL, 50, 82, 32, ?, 'extra summary', '音乐综艺', 'climax_candidate',
             ?, '热血', '节目上下文 -> 歌曲爆点 -> 现场反应', '副歌/高音/强节奏候选',
             '含节目叙事或导师/赛制信息', '可讨论这段改编/表现是否完成突破', 64, 'candidate', ?)
            """,
            [segment_id, transcript, music_slice_type, now],
        )
        conn.commit()
    return {
        "id": segment_id,
        "source_video_id": "video_demo",
        "duration_seconds": 32.0,
        "start_time": 50.0,
        "end_time": 82.0,
    }


def _write_metric_rows(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_visible_work_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _insert_historical_sample(
    row_id: str,
    *,
    account_id: str = "main",
    dataset_id: str,
    item_id: str,
    title: str,
    sample_key: str | None = None,
    source_kind: str = "douyin_clean_json",
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    favorites: int = 0,
    shares: int = 0,
    reward_proxy: float = 0,
    normalized_reward: float = 0,
    performance_label: str = "",
    content_category: str = "",
    hook_type: str = "",
    slice_structure: str = "",
    program_name: str = "",
    artist_names: str = "",
    song_title: str = "",
    tags: str = "",
    duration_seconds: float = 0,
    classification_confidence: str = "",
    published_at: str = "",
    collected_at: str = "",
    semantic_feature_version: str = SEMANTIC_FEATURE_VERSION,
    research_label_version: str = RESEARCH_LABEL_VERSION,
) -> None:
    now = "2026-06-28T00:00:00+00:00"
    raw_json = json.dumps({"metric_availability": {"views": views > 0}}, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO historical_capture_samples
             (id, account_id, dataset_id, dataset_name, source_kind, platform, platform_item_id, sample_key, title,
             views, likes, comments, favorites, shares, reward_proxy, normalized_reward, performance_label,
             content_category, hook_type, slice_structure, program_name, artist_names, song_title, tags,
             duration_seconds, classification_confidence, published_at, collected_at,
             semantic_feature_version, research_label_version, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'douyin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row_id,
                account_id,
                dataset_id,
                dataset_id,
                source_kind,
                item_id,
                sample_key or (f"item:{item_id}" if item_id else ""),
                title,
                views,
                likes,
                comments,
                favorites,
                shares,
                reward_proxy,
                normalized_reward,
                performance_label,
                content_category,
                hook_type,
                slice_structure,
                program_name,
                artist_names,
                song_title,
                tags,
                duration_seconds,
                classification_confidence,
                published_at,
                collected_at,
                semantic_feature_version,
                research_label_version,
                raw_json,
                now,
                now,
            ],
        )
        conn.commit()


def _write_test_wav(path: Path, amplitudes: list[int], *, sample_rate: int = 16000, seconds_per_level: float = 0.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames: list[bytes] = []
    samples_per_level = max(1, int(sample_rate * seconds_per_level))
    for amp in amplitudes:
        amplitude = max(0, min(32000, int(amp)))
        for index in range(samples_per_level):
            value = amplitude if index % 2 == 0 else -amplitude
            frames.append(struct.pack("<h", value))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(frames))


def _write_xlsx_rows(path: Path, sheet_name: str, rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{_xlsx_col(col_index)}{row_index}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value), quote=False)}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def _xlsx_col(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _candidate_fixture(segment_id: str, start: float, transcript: str, program_context: str) -> dict:
    duration = 32.0
    return {
        "id": segment_id,
        "source_video_id": "video_demo",
        "start_time": start,
        "end_time": start + duration,
        "duration_seconds": duration,
        "transcript": transcript,
        "summary": transcript,
        "program_context": program_context,
        "comment_trigger": "可讨论这段表现",
        "short_video_structure": "节目上下文 -> 歌曲爆点 -> 现场反应",
        "musical_moment": "副歌/高音音乐爆点候选",
    }


def _insert_audio_only_segment() -> dict:
    now = "2026-06-23T00:00:00+00:00"
    video_id = "video_demo"
    segment_id = "seg_audio_only"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO candidate_segments
            (id, source_video_id, performance_id, start_time, end_time, duration_seconds, transcript, summary, primary_topic, song_section_type,
             music_slice_type, emotion_type, short_video_structure, musical_moment, program_context, comment_trigger, cover_time, status, created_at)
            VALUES (?, ?, NULL, 50, 82, 32, '音乐/舞台高能候选片段', 'pure audio peak', '音乐综艺', 'climax_candidate',
             '直入听觉爆点型', '舞台表现', '听觉爆点 -> 情绪延展 -> 评论触发', '强节奏/能量峰值音乐爆点候选',
             '节目上下文需人工确认', '可讨论副歌、高音或改编记忆点', 64, 'candidate', ?)
            """,
            [segment_id, video_id, now],
        )
        conn.commit()
    return {
        "id": segment_id,
        "source_video_id": video_id,
        "duration_seconds": 32.0,
        "start_time": 50.0,
        "end_time": 82.0,
    }


if __name__ == "__main__":
    unittest.main()
