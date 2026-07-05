from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dso.artifacts import video_manifest
from dso.collectors.douyin_accounts import build_account_library, clean_account_api_works
from dso.collectors.douyin_media import collect_douyin_media
from dso.collectors.douyin_visible import clean_visible_snapshots
from dso.db.session import init_db
from dso.features.asr import transcribe_video
from dso.features.asr_bench import benchmark_asr
from dso.features.asr_verify import verify_candidate_asr
from dso.features.asr_profile import normalize_asr_profile, resolve_asr_model_list, resolve_asr_model_size
from dso.features.whisper_cpp import setup_whisper_cpp
from dso.features.audio import extract_audio_features
from dso.feedback.douyin import douyin_sync_summary, register_douyin_account, sync_douyin_feedback
from dso.feedback.douyin_auth import complete_douyin_qr_login, douyin_oauth_status, start_douyin_qr_login
from dso.feedback.importer import account_baselines, account_insights, import_metrics, list_training_samples, rebuild_feedback_state
from dso.learning.backtest import backtest_rule_ranker, list_backtest_reports, run_ranker_tuning, semantic_feature_experiment
from dso.learning.historical_samples import (
    douyin_history_baselines,
    export_douyin_history_assets,
    import_douyin_history,
    import_historical_samples,
    backfill_semantic_features,
    historical_sample_summary,
    list_historical_samples,
    rebuild_research_labels,
    research_field_coverage,
    reopen_historical_sample_calibration,
    semantic_calibration_queue,
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
from dso.learning.qwen_embeddings import build_qwen_embedding_index, run_qwen_embedding_evidence
from dso.learning.qwen_omni import analyze_candidate_with_qwen_omni, qwen_omni_status, run_qwen_omni_shadow
from dso.learning.slice_structure_evaluator import evaluate_slice_structure
from dso.media.ingest import ingest_video, list_videos
from dso.review import mark_candidate_review
from dso.runtime import runtime_diagnostics
from dso.scoring.rights import set_rights
from dso.scoring.scorer import score_video, suggestions
from dso.segments.generator import generate_segments
from dso.variants.exporter import create_variant, export_segment


def cmd_init() -> dict:
    path = init_db()
    return {"db_path": str(path)}


def cmd_ingest(video_path: str, account: str, title: str) -> dict:
    init_db()
    return ingest_video(video_path, account_id=account, title=title)


def cmd_rights_set(
    asset_type: str,
    asset_id: str,
    program: str,
    song: str,
    performance: str,
    artist: str,
    platforms: str,
    duration: float | None,
    accounts: str,
) -> dict:
    init_db()
    return set_rights(
        asset_type,
        asset_id,
        program=program,
        song=song,
        performance=performance,
        artist=artist,
        platforms=platforms,
        duration=duration,
        accounts=accounts,
    )


def cmd_extract(
    video_id: str,
    force_asr: bool = False,
    asr_profile: str | None = None,
    asr_model: str | None = None,
    asr_backend: str | None = None,
) -> dict:
    init_db()
    profile_name = normalize_asr_profile(asr_profile)
    model_size = resolve_asr_model_size(asr_model, profile=profile_name)
    transcript = transcribe_video(
        video_id,
        model_size=model_size,
        asr_profile=profile_name,
        backend=asr_backend,
        force=force_asr,
    )
    audio = extract_audio_features(video_id)
    return {
        "video_id": video_id,
        "asr_profile": profile_name,
        "asr_model": model_size,
        "asr_backend": asr_backend or "auto",
        "transcript_source": transcript["source"],
        "transcript_segments": len(transcript["segments"]),
        "transcript_cache_hit": bool(transcript.get("cache_hit") or (transcript.get("metadata") or {}).get("cache_hit")),
        "audio_peaks": len(audio["peaks"]),
    }


def cmd_generate_segments(video_id: str, top_k: int) -> dict:
    init_db()
    rows = generate_segments(video_id, top_k=top_k)
    return {"video_id": video_id, "count": len(rows), "segments": rows}


def cmd_score(video_id: str) -> dict:
    init_db()
    rows = score_video(video_id)
    return {"video_id": video_id, "count": len(rows), "scores": rows}


def cmd_suggest(video_id: str, top_k: int) -> dict:
    init_db()
    return {"video_id": video_id, "suggestions": suggestions(video_id, top_k=top_k)}


def cmd_manifest(video_id: str) -> dict:
    init_db()
    return video_manifest(video_id)


def cmd_review_segment(segment_id: str, status: str, reason: str = "", operator: str = "local") -> dict:
    init_db()
    return mark_candidate_review(segment_id, status, reason=reason, operator=operator)


def cmd_verify_asr(segment_id: str, profile: str = "verify", model: str | None = None, backend: str | None = None, force: bool = False) -> dict:
    init_db()
    return verify_candidate_asr(segment_id, asr_profile=profile, model_size=model, backend=backend, force=force)


def cmd_export(segment_id: str, title: str | None = None) -> dict:
    init_db()
    if title:
        create_variant(segment_id, title=title)
    return export_segment(segment_id)


def cmd_import_metrics(csv_path: str) -> dict:
    init_db()
    return import_metrics(csv_path)


def cmd_insights(account: str | None) -> dict:
    init_db()
    return account_insights(account)


def cmd_rebuild_feedback(account: str | None) -> dict:
    init_db()
    return rebuild_feedback_state(account)


def cmd_douyin_account(account: str, display_name: str = "", platform_account_id: str = "") -> dict:
    init_db()
    return register_douyin_account(
        account,
        {
            "display_name": display_name,
            "platform_account_id": platform_account_id,
            "auth_status": "mock_ready",
            "token_status": "not_stored",
        },
    )


def cmd_douyin_sync(account: str, source: str = "mock", path: str | None = None, windows: str = "") -> dict:
    init_db()
    parsed_windows = [item.strip() for item in windows.split(",") if item.strip()] if windows else None
    return sync_douyin_feedback(account, source=source, source_path=path, windows=parsed_windows)


def cmd_douyin_summary(account: str) -> dict:
    init_db()
    return douyin_sync_summary(account)


def cmd_douyin_login_url(account: str, scopes: str = "", redirect_uri: str | None = None) -> dict:
    init_db()
    return start_douyin_qr_login(account, scopes=scopes or None, redirect_uri=redirect_uri)


def cmd_douyin_auth_code(code: str, state: str, exchange: bool = True) -> dict:
    init_db()
    return complete_douyin_qr_login(code, state, exchange=exchange)


def cmd_douyin_auth_status(account: str, state: str | None = None) -> dict:
    init_db()
    return douyin_oauth_status(account_id=account, state=state)


def cmd_douyin_visible_clean(input_dir: str | None = None, output_dir: str | None = None) -> dict:
    result = clean_visible_snapshots(input_dir=input_dir, output_dir=output_dir)
    report = result.quality_report
    return {
        "pipeline": report["pipeline_version"],
        "quality_grade": report["quality_grade"],
        "quality_score": report["quality_score"],
        "snapshot_count": report["snapshot_count"],
        "record_count": report["record_count"],
        "work_card_count_raw": report["work_card_count_raw"],
        "work_card_count_deduped": report["work_card_count_deduped"],
        "estimated_duplicate_ratio": report["estimated_duplicate_ratio"],
        "accounts": report["accounts"],
        "paths": result.paths,
        "recommendations": report["recommendations"],
    }


def cmd_douyin_account_library(
    input_path: str,
    output_path: str | None = None,
    observed_at: str | None = None,
    source_method: str = "manual_account_library",
) -> dict:
    result = build_account_library(
        input_path,
        output_path=output_path,
        observed_at=observed_at,
        source_method=source_method,
    )
    return {
        "pipeline": "douyin_account_library_v1",
        "account_count": len(result.accounts),
        "accounts": result.accounts,
        "paths": result.paths,
    }


def cmd_douyin_account_works_clean(
    account_library: str,
    account: str,
    raw_works: str,
    output_root: str | None = None,
    run_id: str | None = None,
    rejected_author_mismatch: str | None = None,
    source_method: str = "appleevents_api_json",
    observed_at: str | None = None,
) -> dict:
    result = clean_account_api_works(
        account_library=account_library,
        account_key=account,
        raw_works=raw_works,
        output_root=output_root,
        run_id=run_id,
        rejected_author_mismatch=rejected_author_mismatch,
        source_method=source_method,
        observed_at=observed_at,
    )
    report = result.quality_report
    return {
        "pipeline": report["pipeline_version"],
        "account_key": result.account_key,
        "run_id": report["run_id"],
        "raw_rows": report["raw_rows"],
        "dedup_rows": report["dedup_rows"],
        "author_mismatch_rejected": report["author_mismatch_rejected"],
        "duplicate_ratio": report["duplicate_ratio"],
        "play_count_missing_rate": report["play_count_missing_rate"],
        "quality_grade": report["quality_grade"],
        "quality_score": report["quality_score"],
        "paths": result.paths,
        "recommendations": report["recommendations"],
    }


def cmd_douyin_media_collect(
    plan_path: str,
    stage: str | None = "smoke_v1",
    account: str | None = None,
    limit: int = 0,
    output_root: str | None = None,
    report_dir: str | None = None,
    run_id: str = "20260629_test_v1",
    page_delay_seconds: int = 14,
    extra_wait_seconds: int = 5,
    extract_audio: bool = True,
    dry_run: bool = False,
    max_storage_gb: float = 0.0,
) -> dict:
    return collect_douyin_media(
        plan_path,
        stage=stage,
        account=account,
        limit=limit,
        output_root=output_root,
        report_dir=report_dir,
        run_id=run_id,
        page_delay_seconds=page_delay_seconds,
        extra_wait_seconds=extra_wait_seconds,
        extract_audio=extract_audio,
        dry_run=dry_run,
        max_storage_bytes=int(float(max_storage_gb or 0.0) * 1024 * 1024 * 1024),
    )


def cmd_memory_build(account: str | None, force: bool = False) -> dict:
    init_db()
    return build_text_memory_bank(account_id=account, force=force)


def cmd_history(segment_id: str, account: str | None, limit: int) -> dict:
    init_db()
    return calibrate_segment_history(segment_id, account_id=account, limit=limit)


def cmd_interest_clock(account: str, content_type: str | None, duration: float | None, limit: int, rebuild: bool = False) -> dict:
    init_db()
    if rebuild:
        build_interest_clock(account)
    return recommend_publish_hours(account, content_type=content_type, duration_seconds=duration, limit=limit)


def cmd_backtest(account: str | None, k: int, strategy: str = "research_ranker_v2_4", holdout_policy: str = "time", label_version: str | None = None) -> dict:
    init_db()
    return backtest_rule_ranker(account_id=account, k=k, strategy=strategy, holdout_policy=holdout_policy, label_version=label_version)


def cmd_ranker_tuning(account: str | None, k: int, holdout_policy: str, max_trials: int, label_version: str | None = None) -> dict:
    init_db()
    return run_ranker_tuning(
        account_id=account,
        k=k,
        holdout_policy=holdout_policy,
        max_trials=max_trials,
        label_version=label_version,
    )


def cmd_semantic_feature_experiment(
    account: str | None,
    k: int,
    holdout_policy: str,
    label_version: str | None = None,
    include_field_masks: bool = True,
) -> dict:
    init_db()
    return semantic_feature_experiment(
        account_id=account,
        k=k,
        holdout_policy=holdout_policy,
        label_version=label_version,
        include_field_masks=include_field_masks,
    )


def cmd_slice_structure_evaluate(account: str | None, dataset: str | None, limit: int, min_confidence: float = 0.0) -> dict:
    init_db()
    return evaluate_slice_structure(
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        min_confidence=min_confidence,
    )


def cmd_multimodal_collection_plan(
    account: str | None,
    dataset: str | None,
    limit: int,
    stage: str = "beta_d1",
    output_path: str | None = None,
    include_ready: bool = False,
) -> dict:
    init_db()
    return build_multimodal_collection_plan(
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        stage=stage,
        output_path=output_path,
        include_ready=include_ready,
    )


def cmd_multimodal_collect(
    plan_path: str | None,
    account: str | None,
    dataset: str | None,
    limit: int,
    stage: str = "beta_d1",
    output_root: str | None = None,
    report_dir: str | None = None,
    run_id: str = "",
    page_delay_seconds: int = 14,
    extra_wait_seconds: int = 5,
    extract_audio: bool = True,
    dry_run: bool = True,
    max_storage_gb: float = 3.0,
) -> dict:
    init_db()
    return collect_multimodal_assets(
        plan_path=plan_path,
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        stage=stage,
        output_root=output_root,
        report_dir=report_dir,
        run_id=run_id,
        page_delay_seconds=page_delay_seconds,
        extra_wait_seconds=extra_wait_seconds,
        extract_audio=extract_audio,
        dry_run=dry_run,
        max_storage_bytes=int(float(max_storage_gb or 0.0) * 1024 * 1024 * 1024),
    )


def cmd_multimodal_validation(account: str | None, dataset: str | None, limit: int, k: int, min_samples: int, min_asset_coverage: float) -> dict:
    init_db()
    return run_multimodal_validation(
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        k=k,
        min_samples=min_samples,
        min_asset_coverage=min_asset_coverage,
    )


def cmd_multimodal_feature_experiment(
    account: str | None,
    dataset: str | None,
    limit: int,
    k: int,
    min_feature_samples: int,
    audio_window_seconds: float,
    force: bool = False,
) -> dict:
    init_db()
    return run_multimodal_feature_experiment(
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        k=k,
        min_feature_samples=min_feature_samples,
        audio_window_seconds=audio_window_seconds,
        force=force,
    )


def cmd_qwen_embeddings_build(
    account: str | None,
    dataset: str | None,
    entity_type: str,
    modality: str,
    limit: int,
    force: bool = False,
) -> dict:
    init_db()
    return build_qwen_embedding_index(
        account_id=account,
        dataset_id=dataset,
        entity_type=entity_type,
        modality=modality,
        limit=limit,
        force=force,
    )


def cmd_qwen_embedding_evidence(
    account: str | None,
    dataset: str | None,
    limit: int,
    k: int,
    modality: str,
) -> dict:
    init_db()
    return run_qwen_embedding_evidence(
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        k=k,
        modality=modality,
    )


def cmd_qwen_omni_status() -> dict:
    init_db()
    return qwen_omni_status()


def cmd_qwen_omni_analyze(segment_id: str, account: str | None, max_clip_seconds: float, load_model: bool = False) -> dict:
    init_db()
    return analyze_candidate_with_qwen_omni(
        segment_id,
        account_id=account,
        max_clip_seconds=max_clip_seconds,
        load_model=load_model,
    )


def cmd_qwen_omni_shadow_run(
    account: str | None,
    dataset: str | None,
    limit: int,
    max_clip_seconds: float,
    load_model: bool = False,
) -> dict:
    init_db()
    return run_qwen_omni_shadow(
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        max_clip_seconds=max_clip_seconds,
        load_model=load_model,
    )


def cmd_backtest_reports(account: str | None, limit: int) -> dict:
    init_db()
    return list_backtest_reports(account_id=account, limit=limit)


def cmd_datasets() -> dict:
    init_db()
    return list_capture_datasets()


def cmd_historical_import(account: str, dataset: str | None, source_path: str | None, force: bool = False) -> dict:
    init_db()
    return import_historical_samples(account_id=account, dataset_id=dataset, source_path=source_path, force=force)


def cmd_douyin_history_import(
    account: str,
    clean_dir: str,
    raw_dir: str | None,
    dataset: str | None,
    dataset_name: str | None,
    output_dir: str | None,
    force: bool = False,
) -> dict:
    init_db()
    return import_douyin_history(
        account_id=account,
        clean_dir=clean_dir,
        raw_dir=raw_dir,
        dataset_id=dataset,
        dataset_name=dataset_name,
        output_dir=output_dir,
        force=force,
    )


def cmd_douyin_history_baselines(
    account: str | None,
    dataset: str | None,
    output_dir: str | None,
    min_count: int,
    limit: int,
) -> dict:
    init_db()
    result = douyin_history_baselines(account_id=account, dataset_id=dataset, min_count=min_count, limit=limit)
    if output_dir:
        result["outputs"] = export_douyin_history_assets(account_id=account, dataset_id=dataset, output_dir=output_dir)
    return result


def cmd_historical_samples(account: str | None, dataset: str | None, limit: int) -> dict:
    init_db()
    return list_historical_samples(account_id=account, dataset_id=dataset, limit=limit)


def cmd_historical_summary(account: str | None) -> dict:
    init_db()
    return historical_sample_summary(account_id=account)


def cmd_research_coverage(account: str | None, dataset: str | None) -> dict:
    init_db()
    return research_field_coverage(account_id=account, dataset_id=dataset)


def cmd_semantic_features_backfill(account: str | None, dataset: str | None, limit: int, force: bool = False) -> dict:
    init_db()
    return backfill_semantic_features(account_id=account, dataset_id=dataset, limit=limit, force=force)


def cmd_semantic_calibration_queue(
    account: str | None,
    dataset: str | None,
    limit: int,
    min_priority: float = 0.0,
    label: str | None = None,
    queue_type: str = "mixed",
    strategy: str = "research_ranker_v2_4",
    min_disagreement: float = 0.0,
) -> dict:
    init_db()
    return semantic_calibration_queue(
        account_id=account,
        dataset_id=dataset,
        limit=limit,
        min_priority=min_priority,
        label=label,
        queue_type=queue_type,
        strategy=strategy,
        min_disagreement=min_disagreement,
    )


def cmd_semantic_calibration_reopen(
    sample_id: str,
    confidence: str = "low",
    operator: str = "local",
    reason: str = "reopen semantic calibration",
) -> dict:
    init_db()
    return reopen_historical_sample_calibration(
        sample_id,
        {
            "classification_confidence": confidence,
            "operator": operator,
            "reason": reason,
        },
    )


def cmd_research_labels_rebuild(account: str | None, dataset: str | None, min_baseline_samples: int) -> dict:
    init_db()
    return rebuild_research_labels(account_id=account, dataset_id=dataset, min_baseline_samples=min_baseline_samples)


def cmd_prototype_build(account: str, source: str, source_path: str | None, dataset: str | None, limit: int, min_views: int, force: bool = False) -> dict:
    init_db()
    return build_prototype_bank(
        account_id=account,
        source=source,
        dataset_id=dataset,
        source_path=source_path,
        limit=limit,
        min_views=min_views,
        force=force,
    )


def cmd_prototypes(account: str, source: str, dataset: str | None, limit: int) -> dict:
    init_db()
    return list_prototype_bank(account, source=source, dataset_id=dataset, limit=limit)


def cmd_prototype_match(segment_id: str, account: str | None, source: str, dataset: str | None, limit: int) -> dict:
    init_db()
    return match_segment_prototypes(segment_id, account_id=account, source=source, dataset_id=dataset, limit=limit)


def cmd_training_samples(account: str | None, limit: int) -> dict:
    init_db()
    rows = list_training_samples(account_id=account, limit=limit)
    return {"count": len(rows), "training_samples": rows}


def cmd_baselines(account: str | None) -> dict:
    init_db()
    rows = account_baselines(account)
    return {"count": len(rows), "baselines": rows}


def cmd_doctor() -> dict:
    init_db()
    return runtime_diagnostics()


def cmd_web(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    missing = []
    for module_name in ["fastapi", "uvicorn"]:
        try:
            __import__(module_name)
        except Exception:
            missing.append(module_name)
    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(
            "Web UI requires FastAPI and Uvicorn. "
            'Install the project dependencies with: python3 -m pip install -e ".[dev]". '
            f"Missing: {missing_text}"
        )

    import uvicorn  # type: ignore

    init_db()
    uvicorn.run("dso.api.main:app", host=host, port=port, reload=reload)


def cmd_setup_asr(
    model: str | None = None,
    force: bool = False,
    vad_model: str = "silero-v6.2.0",
    profile: str | None = None,
) -> dict:
    init_db()
    profile_name = normalize_asr_profile(profile, allow_compare=True)
    models = resolve_asr_model_list(model, profile=profile_name)
    results = [setup_whisper_cpp(model=item, force=force, vad_model=vad_model) for item in models]
    if len(results) == 1:
        return {**results[0], "profile": profile_name, "models": models}
    return {
        "ready": all(result.get("ready") for result in results),
        "profile": profile_name,
        "models": models,
        "results": results,
        "actions": [action for result in results for action in result.get("actions", [])],
    }


def cmd_bench_asr(
    input_path: str,
    backend: str = "auto",
    models: str | None = None,
    profile: str | None = None,
    output_dir: str | None = None,
    duration_seconds: float | None = None,
) -> dict:
    init_db()
    return benchmark_asr(
        input_path,
        backend=backend,
        models=models,
        profile=profile,
        output_dir=output_dir,
        duration_seconds=duration_seconds,
    )


def main() -> None:
    try:
        import typer  # type: ignore
    except Exception:
        _argparse_main()
        return
    _typer_main(typer)


def _typer_main(typer_module: Any) -> None:
    app = typer_module.Typer(help="Douyin Slice Optimizer MVP")
    rights_app = typer_module.Typer(help="Rights clearance commands")
    app.add_typer(rights_app, name="rights")

    @app.command("init")
    def init_command() -> None:
        _print(cmd_init())

    @app.command("videos")
    def videos_command() -> None:
        init_db()
        _print({"videos": list_videos()})

    @app.command("doctor")
    def doctor_command() -> None:
        _print(cmd_doctor())

    @app.command("setup-asr")
    def setup_asr_command(
        model: str | None = typer_module.Option(None, "--model"),
        profile: str | None = typer_module.Option(None, "--profile"),
        vad_model: str = typer_module.Option("silero-v6.2.0", "--vad-model"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_setup_asr(model, force, vad_model, profile))

    @app.command("bench-asr")
    def bench_asr_command(
        input_path: str,
        backend: str = typer_module.Option("auto", "--backend"),
        models: str | None = typer_module.Option(None, "--models"),
        profile: str | None = typer_module.Option(None, "--profile"),
        output_dir: str | None = typer_module.Option(None, "--output-dir"),
        duration_seconds: float | None = typer_module.Option(None, "--duration-seconds"),
    ) -> None:
        _print(cmd_bench_asr(input_path, backend, models, profile, output_dir, duration_seconds))

    @app.command("ingest")
    def ingest_command(
        video_path: str,
        account: str = typer_module.Option("main", "--account"),
        title: str = typer_module.Option(..., "--title"),
    ) -> None:
        _print(cmd_ingest(video_path, account, title))

    @rights_app.command("set")
    def rights_set_command(
        asset_type: str,
        asset_id: str,
        program: str = typer_module.Option("cleared", "--program"),
        song: str = typer_module.Option("cleared", "--song"),
        performance: str = typer_module.Option("cleared", "--performance"),
        artist: str = typer_module.Option("cleared", "--artist"),
        platforms: str = typer_module.Option("douyin", "--platforms"),
        duration: float | None = typer_module.Option(None, "--duration"),
        accounts: str = typer_module.Option("", "--accounts"),
    ) -> None:
        _print(cmd_rights_set(asset_type, asset_id, program, song, performance, artist, platforms, duration, accounts))

    @app.command("extract")
    def extract_command(
        video_id: str,
        force_asr: bool = typer_module.Option(False, "--force-asr"),
        asr_profile: str | None = typer_module.Option(None, "--asr-profile"),
        asr_model: str | None = typer_module.Option(None, "--asr-model"),
        asr_backend: str | None = typer_module.Option(None, "--asr-backend"),
    ) -> None:
        _print(cmd_extract(video_id, force_asr, asr_profile, asr_model, asr_backend))

    @app.command("generate-segments")
    def generate_segments_command(video_id: str, top_k: int = typer_module.Option(30, "--top-k")) -> None:
        _print(cmd_generate_segments(video_id, top_k))

    @app.command("score")
    def score_command(video_id: str) -> None:
        _print(cmd_score(video_id))

    @app.command("suggest")
    def suggest_command(video_id: str, top_k: int = typer_module.Option(10, "--top-k")) -> None:
        _print(cmd_suggest(video_id, top_k))

    @app.command("manifest")
    def manifest_command(video_id: str) -> None:
        _print(cmd_manifest(video_id))

    @app.command("review-segment")
    def review_segment_command(
        segment_id: str,
        status: str = typer_module.Option("review", "--status"),
        reason: str = typer_module.Option("", "--reason"),
        operator: str = typer_module.Option("local", "--operator"),
    ) -> None:
        _print(cmd_review_segment(segment_id, status, reason, operator))

    @app.command("verify-asr")
    def verify_asr_command(
        segment_id: str,
        profile: str = typer_module.Option("verify", "--profile"),
        model: str | None = typer_module.Option(None, "--model"),
        backend: str | None = typer_module.Option(None, "--backend"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_verify_asr(segment_id, profile, model, backend, force))

    @app.command("export")
    def export_command(segment_id: str, title: str | None = typer_module.Option(None, "--title")) -> None:
        _print(cmd_export(segment_id, title))

    @app.command("import-metrics")
    def import_metrics_command(csv_path: str) -> None:
        _print(cmd_import_metrics(csv_path))

    @app.command("insights")
    def insights_command(account: str | None = typer_module.Option(None, "--account")) -> None:
        _print(cmd_insights(account))

    @app.command("rebuild-feedback")
    def rebuild_feedback_command(account: str | None = typer_module.Option(None, "--account")) -> None:
        _print(cmd_rebuild_feedback(account))

    @app.command("douyin-account")
    def douyin_account_command(
        account: str = typer_module.Option("main", "--account"),
        display_name: str = typer_module.Option("", "--display-name"),
        platform_account_id: str = typer_module.Option("", "--platform-account-id"),
    ) -> None:
        _print(cmd_douyin_account(account, display_name, platform_account_id))

    @app.command("douyin-sync")
    def douyin_sync_command(
        account: str = typer_module.Option("main", "--account"),
        source: str = typer_module.Option("mock", "--source"),
        path: str | None = typer_module.Option(None, "--path"),
        windows: str = typer_module.Option("", "--windows"),
    ) -> None:
        _print(cmd_douyin_sync(account, source, path, windows))

    @app.command("douyin-summary")
    def douyin_summary_command(account: str = typer_module.Option("main", "--account")) -> None:
        _print(cmd_douyin_summary(account))

    @app.command("douyin-login-url")
    def douyin_login_url_command(
        account: str = typer_module.Option("main", "--account"),
        scopes: str = typer_module.Option("", "--scopes"),
        redirect_uri: str | None = typer_module.Option(None, "--redirect-uri"),
    ) -> None:
        _print(cmd_douyin_login_url(account, scopes, redirect_uri))

    @app.command("douyin-auth-code")
    def douyin_auth_code_command(
        code: str,
        state: str,
        exchange: bool = typer_module.Option(True, "--exchange/--no-exchange"),
    ) -> None:
        _print(cmd_douyin_auth_code(code, state, exchange))

    @app.command("douyin-auth-status")
    def douyin_auth_status_command(
        account: str = typer_module.Option("main", "--account"),
        state: str | None = typer_module.Option(None, "--state"),
    ) -> None:
        _print(cmd_douyin_auth_status(account, state))

    @app.command("douyin-visible-clean")
    def douyin_visible_clean_command(
        input_dir: str | None = typer_module.Option(None, "--input-dir"),
        output_dir: str | None = typer_module.Option(None, "--output-dir"),
    ) -> None:
        _print(cmd_douyin_visible_clean(input_dir, output_dir))

    @app.command("douyin-account-library")
    def douyin_account_library_command(
        input_path: str = typer_module.Argument(...),
        output_path: str | None = typer_module.Option(None, "--output-path"),
        observed_at: str | None = typer_module.Option(None, "--observed-at"),
        source_method: str = typer_module.Option("manual_account_library", "--source-method"),
    ) -> None:
        _print(cmd_douyin_account_library(input_path, output_path, observed_at, source_method))

    @app.command("douyin-account-works-clean")
    def douyin_account_works_clean_command(
        account_library: str = typer_module.Option(..., "--account-library"),
        account: str = typer_module.Option(..., "--account"),
        raw_works: str = typer_module.Option(..., "--raw-works"),
        output_root: str | None = typer_module.Option(None, "--output-root"),
        run_id: str | None = typer_module.Option(None, "--run-id"),
        rejected_author_mismatch: str | None = typer_module.Option(None, "--rejected-author-mismatch"),
        source_method: str = typer_module.Option("appleevents_api_json", "--source-method"),
        observed_at: str | None = typer_module.Option(None, "--observed-at"),
    ) -> None:
        _print(
            cmd_douyin_account_works_clean(
                account_library,
                account,
                raw_works,
                output_root,
                run_id,
                rejected_author_mismatch,
                source_method,
                observed_at,
            )
        )

    @app.command("douyin-media-collect")
    def douyin_media_collect_command(
        plan_path: str = typer_module.Argument(...),
        stage: str | None = typer_module.Option("smoke_v1", "--stage"),
        account: str | None = typer_module.Option(None, "--account"),
        limit: int = typer_module.Option(0, "--limit"),
        output_root: str | None = typer_module.Option(None, "--output-root"),
        report_dir: str | None = typer_module.Option(None, "--report-dir"),
        run_id: str = typer_module.Option("20260629_test_v1", "--run-id"),
        page_delay_seconds: int = typer_module.Option(14, "--page-delay-seconds"),
        extra_wait_seconds: int = typer_module.Option(5, "--extra-wait-seconds"),
        extract_audio: bool = typer_module.Option(True, "--extract-audio/--no-extract-audio"),
        dry_run: bool = typer_module.Option(False, "--dry-run"),
        max_storage_gb: float = typer_module.Option(0.0, "--max-storage-gb"),
    ) -> None:
        _print(
            cmd_douyin_media_collect(
                plan_path,
                stage,
                account,
                limit,
                output_root,
                report_dir,
                run_id,
                page_delay_seconds,
                extra_wait_seconds,
                extract_audio,
                dry_run,
                max_storage_gb,
            )
        )

    @app.command("memory-build")
    def memory_build_command(
        account: str | None = typer_module.Option(None, "--account"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_memory_build(account, force))

    @app.command("history")
    def history_command(
        segment_id: str,
        account: str | None = typer_module.Option(None, "--account"),
        limit: int = typer_module.Option(8, "--limit"),
    ) -> None:
        _print(cmd_history(segment_id, account, limit))

    @app.command("interest-clock")
    def interest_clock_command(
        account: str = typer_module.Option("main", "--account"),
        content_type: str | None = typer_module.Option(None, "--content-type"),
        duration: float | None = typer_module.Option(None, "--duration"),
        limit: int = typer_module.Option(5, "--limit"),
        rebuild: bool = typer_module.Option(False, "--rebuild"),
    ) -> None:
        _print(cmd_interest_clock(account, content_type, duration, limit, rebuild))

    @app.command("backtest")
    def backtest_command(
        account: str | None = typer_module.Option(None, "--account"),
        k: int = typer_module.Option(10, "--k"),
        strategy: str = typer_module.Option("research_ranker_v2_4", "--strategy"),
        holdout_policy: str = typer_module.Option("time", "--holdout-policy"),
        label_version: str | None = typer_module.Option(None, "--label-version"),
    ) -> None:
        _print(cmd_backtest(account, k, strategy, holdout_policy, label_version))

    @app.command("ranker-tuning-run")
    def ranker_tuning_command(
        account: str | None = typer_module.Option(None, "--account"),
        k: int = typer_module.Option(10, "--k"),
        holdout_policy: str = typer_module.Option("time", "--holdout-policy"),
        max_trials: int = typer_module.Option(12, "--max-trials"),
        label_version: str | None = typer_module.Option(None, "--label-version"),
    ) -> None:
        _print(cmd_ranker_tuning(account, k, holdout_policy, max_trials, label_version))

    @app.command("semantic-feature-experiment")
    def semantic_feature_experiment_command(
        account: str | None = typer_module.Option(None, "--account"),
        k: int = typer_module.Option(10, "--k"),
        holdout_policy: str = typer_module.Option("time", "--holdout-policy"),
        label_version: str | None = typer_module.Option(None, "--label-version"),
        skip_field_masks: bool = typer_module.Option(False, "--skip-field-masks"),
    ) -> None:
        _print(cmd_semantic_feature_experiment(account, k, holdout_policy, label_version, not skip_field_masks))

    @app.command("semantic-features-backfill")
    def semantic_features_backfill_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(0, "--limit"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_semantic_features_backfill(account, dataset, limit, force))

    @app.command("slice-structure-evaluate")
    def slice_structure_evaluate_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(0, "--limit"),
        min_confidence: float = typer_module.Option(0.0, "--min-confidence"),
    ) -> None:
        _print(cmd_slice_structure_evaluate(account, dataset, limit, min_confidence))

    @app.command("multimodal-collection-plan")
    def multimodal_collection_plan_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(120, "--limit"),
        stage: str = typer_module.Option("beta_d1", "--stage"),
        output_path: str | None = typer_module.Option(None, "--output-path"),
        include_ready: bool = typer_module.Option(False, "--include-ready"),
    ) -> None:
        _print(cmd_multimodal_collection_plan(account, dataset, limit, stage, output_path, include_ready))

    @app.command("multimodal-collect")
    def multimodal_collect_command(
        plan_path: str | None = typer_module.Option(None, "--plan-path"),
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(30, "--limit"),
        stage: str = typer_module.Option("beta_d1", "--stage"),
        output_root: str | None = typer_module.Option(None, "--output-root"),
        report_dir: str | None = typer_module.Option(None, "--report-dir"),
        run_id: str = typer_module.Option("", "--run-id"),
        page_delay_seconds: int = typer_module.Option(14, "--page-delay-seconds"),
        extra_wait_seconds: int = typer_module.Option(5, "--extra-wait-seconds"),
        extract_audio: bool = typer_module.Option(True, "--extract-audio/--no-extract-audio"),
        dry_run: bool = typer_module.Option(True, "--dry-run/--download"),
        max_storage_gb: float = typer_module.Option(3.0, "--max-storage-gb"),
    ) -> None:
        _print(
            cmd_multimodal_collect(
                plan_path,
                account,
                dataset,
                limit,
                stage,
                output_root,
                report_dir,
                run_id,
                page_delay_seconds,
                extra_wait_seconds,
                extract_audio,
                dry_run,
                max_storage_gb,
            )
        )

    @app.command("multimodal-validation")
    def multimodal_validation_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(300, "--limit"),
        k: int = typer_module.Option(10, "--k"),
        min_samples: int = typer_module.Option(100, "--min-samples"),
        min_asset_coverage: float = typer_module.Option(0.7, "--min-asset-coverage"),
    ) -> None:
        _print(cmd_multimodal_validation(account, dataset, limit, k, min_samples, min_asset_coverage))

    @app.command("multimodal-feature-experiment")
    def multimodal_feature_experiment_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(300, "--limit"),
        k: int = typer_module.Option(10, "--k"),
        min_feature_samples: int = typer_module.Option(60, "--min-feature-samples"),
        audio_window_seconds: float = typer_module.Option(10.0, "--audio-window-seconds"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_multimodal_feature_experiment(account, dataset, limit, k, min_feature_samples, audio_window_seconds, force))

    @app.command("qwen-embeddings-build")
    def qwen_embeddings_build_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        entity_type: str = typer_module.Option("historical_sample", "--entity-type"),
        modality: str = typer_module.Option("text", "--modality"),
        limit: int = typer_module.Option(300, "--limit"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_qwen_embeddings_build(account, dataset, entity_type, modality, limit, force))

    @app.command("qwen-embedding-evidence")
    def qwen_embedding_evidence_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(300, "--limit"),
        k: int = typer_module.Option(10, "--k"),
        modality: str = typer_module.Option("all", "--modality"),
    ) -> None:
        _print(cmd_qwen_embedding_evidence(account, dataset, limit, k, modality))

    @app.command("qwen-omni-status")
    def qwen_omni_status_command() -> None:
        _print(cmd_qwen_omni_status())

    @app.command("qwen-omni-analyze")
    def qwen_omni_analyze_command(
        segment_id: str,
        account: str | None = typer_module.Option(None, "--account"),
        max_clip_seconds: float = typer_module.Option(15.0, "--max-clip-seconds"),
        load_model: bool = typer_module.Option(False, "--load-model"),
    ) -> None:
        _print(cmd_qwen_omni_analyze(segment_id, account, max_clip_seconds, load_model))

    @app.command("qwen-omni-shadow-run")
    def qwen_omni_shadow_run_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(20, "--limit"),
        max_clip_seconds: float = typer_module.Option(15.0, "--max-clip-seconds"),
        load_model: bool = typer_module.Option(False, "--load-model"),
    ) -> None:
        _print(cmd_qwen_omni_shadow_run(account, dataset, limit, max_clip_seconds, load_model))

    @app.command("backtest-reports")
    def backtest_reports_command(
        account: str | None = typer_module.Option(None, "--account"),
        limit: int = typer_module.Option(10, "--limit"),
    ) -> None:
        _print(cmd_backtest_reports(account, limit))

    @app.command("prototype-build")
    def prototype_build_command(
        account: str = typer_module.Option("main", "--account"),
        source: str = typer_module.Option("external", "--source"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        source_path: str | None = typer_module.Option(None, "--source-path"),
        limit: int = typer_module.Option(80, "--limit"),
        min_views: int = typer_module.Option(0, "--min-views"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_prototype_build(account, source, source_path, dataset, limit, min_views, force))

    @app.command("prototypes")
    def prototypes_command(
        account: str = typer_module.Option("main", "--account"),
        source: str = typer_module.Option("external", "--source"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(20, "--limit"),
    ) -> None:
        _print(cmd_prototypes(account, source, dataset, limit))

    @app.command("datasets")
    def datasets_command() -> None:
        _print(cmd_datasets())

    @app.command("historical-import")
    def historical_import_command(
        account: str = typer_module.Option("main", "--account"),
        dataset: str | None = typer_module.Option("all", "--dataset"),
        source_path: str | None = typer_module.Option(None, "--source-path"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_historical_import(account, dataset, source_path, force))

    @app.command("douyin-history-import")
    def douyin_history_import_command(
        account: str = typer_module.Option("main", "--account"),
        clean_dir: str = typer_module.Option(..., "--clean-dir"),
        raw_dir: str | None = typer_module.Option(None, "--raw-dir"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        dataset_name: str | None = typer_module.Option(None, "--dataset-name"),
        output_dir: str | None = typer_module.Option(None, "--output-dir"),
        force: bool = typer_module.Option(False, "--force"),
    ) -> None:
        _print(cmd_douyin_history_import(account, clean_dir, raw_dir, dataset, dataset_name, output_dir, force))

    @app.command("douyin-history-baselines")
    def douyin_history_baselines_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        output_dir: str | None = typer_module.Option(None, "--output-dir"),
        min_count: int = typer_module.Option(2, "--min-count"),
        limit: int = typer_module.Option(80, "--limit"),
    ) -> None:
        _print(cmd_douyin_history_baselines(account, dataset, output_dir, min_count, limit))

    @app.command("historical-samples")
    def historical_samples_command(
        account: str | None = typer_module.Option("main", "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(20, "--limit"),
    ) -> None:
        _print(cmd_historical_samples(account, dataset, limit))

    @app.command("historical-summary")
    def historical_summary_command(account: str | None = typer_module.Option("main", "--account")) -> None:
        _print(cmd_historical_summary(account))

    @app.command("research-coverage")
    def research_coverage_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
    ) -> None:
        _print(cmd_research_coverage(account, dataset))

    @app.command("semantic-calibration-queue")
    def semantic_calibration_queue_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(50, "--limit"),
        min_priority: float = typer_module.Option(0.0, "--min-priority"),
        label: str | None = typer_module.Option(None, "--label"),
        queue_type: str = typer_module.Option("mixed", "--queue-type"),
        strategy: str = typer_module.Option("research_ranker_v2_4", "--strategy"),
        min_disagreement: float = typer_module.Option(0.0, "--min-disagreement"),
    ) -> None:
        _print(cmd_semantic_calibration_queue(account, dataset, limit, min_priority, label, queue_type, strategy, min_disagreement))

    @app.command("semantic-calibration-reopen")
    def semantic_calibration_reopen_command(
        sample_id: str,
        confidence: str = typer_module.Option("low", "--confidence"),
        operator: str = typer_module.Option("local", "--operator"),
        reason: str = typer_module.Option("reopen semantic calibration", "--reason"),
    ) -> None:
        _print(cmd_semantic_calibration_reopen(sample_id, confidence, operator, reason))

    @app.command("research-labels-rebuild")
    def research_labels_rebuild_command(
        account: str | None = typer_module.Option(None, "--account"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        min_baseline_samples: int = typer_module.Option(20, "--min-baseline-samples"),
    ) -> None:
        _print(cmd_research_labels_rebuild(account, dataset, min_baseline_samples))

    @app.command("prototype-match")
    def prototype_match_command(
        segment_id: str,
        account: str | None = typer_module.Option(None, "--account"),
        source: str = typer_module.Option("external", "--source"),
        dataset: str | None = typer_module.Option(None, "--dataset"),
        limit: int = typer_module.Option(5, "--limit"),
    ) -> None:
        _print(cmd_prototype_match(segment_id, account, source, dataset, limit))

    @app.command("training-samples")
    def training_samples_command(account: str | None = typer_module.Option(None, "--account"), limit: int = typer_module.Option(50, "--limit")) -> None:
        _print(cmd_training_samples(account, limit))

    @app.command("baselines")
    def baselines_command(account: str | None = typer_module.Option(None, "--account")) -> None:
        _print(cmd_baselines(account))

    @app.command("web")
    def web_command(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
        try:
            cmd_web(host=host, port=port, reload=reload)
        except RuntimeError as exc:
            typer_module.echo(f"Error: {exc}", err=True)
            raise typer_module.Exit(1)

    app()


def _argparse_main() -> None:
    parser = argparse.ArgumentParser(prog="dso", description="Douyin Slice Optimizer MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("videos")
    sub.add_parser("doctor")
    setup_asr = sub.add_parser("setup-asr")
    setup_asr.add_argument("--model")
    setup_asr.add_argument("--profile")
    setup_asr.add_argument("--vad-model", default="silero-v6.2.0")
    setup_asr.add_argument("--force", action="store_true")
    bench_asr = sub.add_parser("bench-asr")
    bench_asr.add_argument("input_path")
    bench_asr.add_argument("--backend", default="auto")
    bench_asr.add_argument("--models")
    bench_asr.add_argument("--profile")
    bench_asr.add_argument("--output-dir")
    bench_asr.add_argument("--duration-seconds", type=float)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("video_path")
    ingest.add_argument("--account", default="main")
    ingest.add_argument("--title", required=True)

    rights = sub.add_parser("rights")
    rights_sub = rights.add_subparsers(dest="rights_command", required=True)
    rights_set = rights_sub.add_parser("set")
    rights_set.add_argument("asset_type")
    rights_set.add_argument("asset_id")
    rights_set.add_argument("--program", default="cleared")
    rights_set.add_argument("--song", default="cleared")
    rights_set.add_argument("--performance", default="cleared")
    rights_set.add_argument("--artist", default="cleared")
    rights_set.add_argument("--platforms", default="douyin")
    rights_set.add_argument("--duration", type=float)
    rights_set.add_argument("--accounts", default="")

    extract = sub.add_parser("extract")
    extract.add_argument("video_id")
    extract.add_argument("--force-asr", action="store_true")
    extract.add_argument("--asr-profile")
    extract.add_argument("--asr-model")
    extract.add_argument("--asr-backend")
    gen = sub.add_parser("generate-segments")
    gen.add_argument("video_id")
    gen.add_argument("--top-k", type=int, default=30)
    score = sub.add_parser("score")
    score.add_argument("video_id")
    suggest = sub.add_parser("suggest")
    suggest.add_argument("video_id")
    suggest.add_argument("--top-k", type=int, default=10)
    manifest = sub.add_parser("manifest")
    manifest.add_argument("video_id")
    review = sub.add_parser("review-segment")
    review.add_argument("segment_id")
    review.add_argument("--status", default="review")
    review.add_argument("--reason", default="")
    review.add_argument("--operator", default="local")
    verify_asr = sub.add_parser("verify-asr")
    verify_asr.add_argument("segment_id")
    verify_asr.add_argument("--profile", default="verify")
    verify_asr.add_argument("--model")
    verify_asr.add_argument("--backend")
    verify_asr.add_argument("--force", action="store_true")
    export = sub.add_parser("export")
    export.add_argument("segment_id")
    export.add_argument("--title")
    metrics = sub.add_parser("import-metrics")
    metrics.add_argument("csv_path")
    insights = sub.add_parser("insights")
    insights.add_argument("--account")
    rebuild = sub.add_parser("rebuild-feedback")
    rebuild.add_argument("--account")
    douyin_account = sub.add_parser("douyin-account")
    douyin_account.add_argument("--account", default="main")
    douyin_account.add_argument("--display-name", default="")
    douyin_account.add_argument("--platform-account-id", default="")
    douyin_sync = sub.add_parser("douyin-sync")
    douyin_sync.add_argument("--account", default="main")
    douyin_sync.add_argument("--source", default="mock")
    douyin_sync.add_argument("--path")
    douyin_sync.add_argument("--windows", default="")
    douyin_summary = sub.add_parser("douyin-summary")
    douyin_summary.add_argument("--account", default="main")
    douyin_login = sub.add_parser("douyin-login-url")
    douyin_login.add_argument("--account", default="main")
    douyin_login.add_argument("--scopes", default="")
    douyin_login.add_argument("--redirect-uri")
    douyin_code = sub.add_parser("douyin-auth-code")
    douyin_code.add_argument("code")
    douyin_code.add_argument("state")
    douyin_code.add_argument("--no-exchange", action="store_true")
    douyin_status = sub.add_parser("douyin-auth-status")
    douyin_status.add_argument("--account", default="main")
    douyin_status.add_argument("--state")
    douyin_visible = sub.add_parser("douyin-visible-clean")
    douyin_visible.add_argument("--input-dir")
    douyin_visible.add_argument("--output-dir")
    douyin_account_library = sub.add_parser("douyin-account-library")
    douyin_account_library.add_argument("input_path")
    douyin_account_library.add_argument("--output-path")
    douyin_account_library.add_argument("--observed-at")
    douyin_account_library.add_argument("--source-method", default="manual_account_library")
    douyin_account_works = sub.add_parser("douyin-account-works-clean")
    douyin_account_works.add_argument("--account-library", required=True)
    douyin_account_works.add_argument("--account", required=True)
    douyin_account_works.add_argument("--raw-works", required=True)
    douyin_account_works.add_argument("--output-root")
    douyin_account_works.add_argument("--run-id")
    douyin_account_works.add_argument("--rejected-author-mismatch")
    douyin_account_works.add_argument("--source-method", default="appleevents_api_json")
    douyin_account_works.add_argument("--observed-at")
    douyin_media = sub.add_parser("douyin-media-collect")
    douyin_media.add_argument("plan_path")
    douyin_media.add_argument("--stage", default="smoke_v1")
    douyin_media.add_argument("--account")
    douyin_media.add_argument("--limit", type=int, default=0)
    douyin_media.add_argument("--output-root")
    douyin_media.add_argument("--report-dir")
    douyin_media.add_argument("--run-id", default="20260629_test_v1")
    douyin_media.add_argument("--page-delay-seconds", type=int, default=14)
    douyin_media.add_argument("--extra-wait-seconds", type=int, default=5)
    douyin_media.add_argument("--no-extract-audio", action="store_true")
    douyin_media.add_argument("--dry-run", action="store_true")
    douyin_media.add_argument("--max-storage-gb", type=float, default=0.0)
    memory_build = sub.add_parser("memory-build")
    memory_build.add_argument("--account")
    memory_build.add_argument("--force", action="store_true")
    history = sub.add_parser("history")
    history.add_argument("segment_id")
    history.add_argument("--account")
    history.add_argument("--limit", type=int, default=8)
    interest = sub.add_parser("interest-clock")
    interest.add_argument("--account", default="main")
    interest.add_argument("--content-type")
    interest.add_argument("--duration", type=float)
    interest.add_argument("--limit", type=int, default=5)
    interest.add_argument("--rebuild", action="store_true")
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--account")
    backtest.add_argument("--k", type=int, default=10)
    backtest.add_argument("--strategy", default="research_ranker_v2_4")
    backtest.add_argument("--holdout-policy", default="time")
    backtest.add_argument("--label-version")
    ranker_tuning = sub.add_parser("ranker-tuning-run")
    ranker_tuning.add_argument("--account")
    ranker_tuning.add_argument("--k", type=int, default=10)
    ranker_tuning.add_argument("--holdout-policy", default="time")
    ranker_tuning.add_argument("--max-trials", type=int, default=12)
    ranker_tuning.add_argument("--label-version")
    semantic_experiment = sub.add_parser("semantic-feature-experiment")
    semantic_experiment.add_argument("--account")
    semantic_experiment.add_argument("--k", type=int, default=10)
    semantic_experiment.add_argument("--holdout-policy", default="time")
    semantic_experiment.add_argument("--label-version")
    semantic_experiment.add_argument("--skip-field-masks", action="store_true")
    semantic_backfill = sub.add_parser("semantic-features-backfill")
    semantic_backfill.add_argument("--account")
    semantic_backfill.add_argument("--dataset")
    semantic_backfill.add_argument("--limit", type=int, default=0)
    semantic_backfill.add_argument("--force", action="store_true")
    slice_structure_eval = sub.add_parser("slice-structure-evaluate")
    slice_structure_eval.add_argument("--account")
    slice_structure_eval.add_argument("--dataset")
    slice_structure_eval.add_argument("--limit", type=int, default=0)
    slice_structure_eval.add_argument("--min-confidence", type=float, default=0.0)
    multimodal_plan = sub.add_parser("multimodal-collection-plan")
    multimodal_plan.add_argument("--account")
    multimodal_plan.add_argument("--dataset")
    multimodal_plan.add_argument("--limit", type=int, default=120)
    multimodal_plan.add_argument("--stage", default="beta_d1")
    multimodal_plan.add_argument("--output-path")
    multimodal_plan.add_argument("--include-ready", action="store_true")
    multimodal_collect = sub.add_parser("multimodal-collect")
    multimodal_collect.add_argument("--plan-path")
    multimodal_collect.add_argument("--account")
    multimodal_collect.add_argument("--dataset")
    multimodal_collect.add_argument("--limit", type=int, default=30)
    multimodal_collect.add_argument("--stage", default="beta_d1")
    multimodal_collect.add_argument("--output-root")
    multimodal_collect.add_argument("--report-dir")
    multimodal_collect.add_argument("--run-id", default="")
    multimodal_collect.add_argument("--page-delay-seconds", type=int, default=14)
    multimodal_collect.add_argument("--extra-wait-seconds", type=int, default=5)
    multimodal_collect.add_argument("--no-extract-audio", action="store_true")
    multimodal_collect.add_argument("--download", action="store_true")
    multimodal_collect.add_argument("--max-storage-gb", type=float, default=3.0)
    multimodal_validation = sub.add_parser("multimodal-validation")
    multimodal_validation.add_argument("--account")
    multimodal_validation.add_argument("--dataset")
    multimodal_validation.add_argument("--limit", type=int, default=300)
    multimodal_validation.add_argument("--k", type=int, default=10)
    multimodal_validation.add_argument("--min-samples", type=int, default=100)
    multimodal_validation.add_argument("--min-asset-coverage", type=float, default=0.7)
    multimodal_feature = sub.add_parser("multimodal-feature-experiment")
    multimodal_feature.add_argument("--account")
    multimodal_feature.add_argument("--dataset")
    multimodal_feature.add_argument("--limit", type=int, default=300)
    multimodal_feature.add_argument("--k", type=int, default=10)
    multimodal_feature.add_argument("--min-feature-samples", type=int, default=60)
    multimodal_feature.add_argument("--audio-window-seconds", type=float, default=10.0)
    multimodal_feature.add_argument("--force", action="store_true")
    qwen_embeddings = sub.add_parser("qwen-embeddings-build")
    qwen_embeddings.add_argument("--account")
    qwen_embeddings.add_argument("--dataset")
    qwen_embeddings.add_argument("--entity-type", default="historical_sample")
    qwen_embeddings.add_argument("--modality", default="text")
    qwen_embeddings.add_argument("--limit", type=int, default=300)
    qwen_embeddings.add_argument("--force", action="store_true")
    qwen_evidence = sub.add_parser("qwen-embedding-evidence")
    qwen_evidence.add_argument("--account")
    qwen_evidence.add_argument("--dataset")
    qwen_evidence.add_argument("--limit", type=int, default=300)
    qwen_evidence.add_argument("--k", type=int, default=10)
    qwen_evidence.add_argument("--modality", default="all")
    sub.add_parser("qwen-omni-status")
    qwen_omni_analyze = sub.add_parser("qwen-omni-analyze")
    qwen_omni_analyze.add_argument("segment_id")
    qwen_omni_analyze.add_argument("--account")
    qwen_omni_analyze.add_argument("--max-clip-seconds", type=float, default=15.0)
    qwen_omni_analyze.add_argument("--load-model", action="store_true")
    qwen_omni_shadow = sub.add_parser("qwen-omni-shadow-run")
    qwen_omni_shadow.add_argument("--account")
    qwen_omni_shadow.add_argument("--dataset")
    qwen_omni_shadow.add_argument("--limit", type=int, default=20)
    qwen_omni_shadow.add_argument("--max-clip-seconds", type=float, default=15.0)
    qwen_omni_shadow.add_argument("--load-model", action="store_true")
    backtest_reports = sub.add_parser("backtest-reports")
    backtest_reports.add_argument("--account")
    backtest_reports.add_argument("--limit", type=int, default=10)
    sub.add_parser("datasets")
    historical_import = sub.add_parser("historical-import")
    historical_import.add_argument("--account", default="main")
    historical_import.add_argument("--dataset", default="all")
    historical_import.add_argument("--source-path")
    historical_import.add_argument("--force", action="store_true")
    douyin_history_import = sub.add_parser("douyin-history-import")
    douyin_history_import.add_argument("--account", default="main")
    douyin_history_import.add_argument("--clean-dir", required=True)
    douyin_history_import.add_argument("--raw-dir")
    douyin_history_import.add_argument("--dataset")
    douyin_history_import.add_argument("--dataset-name")
    douyin_history_import.add_argument("--output-dir")
    douyin_history_import.add_argument("--force", action="store_true")
    douyin_history_baselines = sub.add_parser("douyin-history-baselines")
    douyin_history_baselines.add_argument("--account")
    douyin_history_baselines.add_argument("--dataset")
    douyin_history_baselines.add_argument("--output-dir")
    douyin_history_baselines.add_argument("--min-count", type=int, default=2)
    douyin_history_baselines.add_argument("--limit", type=int, default=80)
    historical_samples = sub.add_parser("historical-samples")
    historical_samples.add_argument("--account", default="main")
    historical_samples.add_argument("--dataset")
    historical_samples.add_argument("--limit", type=int, default=20)
    historical_summary = sub.add_parser("historical-summary")
    historical_summary.add_argument("--account", default="main")
    research_coverage = sub.add_parser("research-coverage")
    research_coverage.add_argument("--account")
    research_coverage.add_argument("--dataset")
    calibration_queue = sub.add_parser("semantic-calibration-queue")
    calibration_queue.add_argument("--account")
    calibration_queue.add_argument("--dataset")
    calibration_queue.add_argument("--limit", type=int, default=50)
    calibration_queue.add_argument("--min-priority", type=float, default=0.0)
    calibration_queue.add_argument("--label")
    calibration_queue.add_argument("--queue-type", default="mixed")
    calibration_queue.add_argument("--strategy", default="research_ranker_v2_4")
    calibration_queue.add_argument("--min-disagreement", type=float, default=0.0)
    calibration_reopen = sub.add_parser("semantic-calibration-reopen")
    calibration_reopen.add_argument("sample_id")
    calibration_reopen.add_argument("--confidence", default="low")
    calibration_reopen.add_argument("--operator", default="local")
    calibration_reopen.add_argument("--reason", default="reopen semantic calibration")
    labels_rebuild = sub.add_parser("research-labels-rebuild")
    labels_rebuild.add_argument("--account")
    labels_rebuild.add_argument("--dataset")
    labels_rebuild.add_argument("--min-baseline-samples", type=int, default=20)
    prototype_build = sub.add_parser("prototype-build")
    prototype_build.add_argument("--account", default="main")
    prototype_build.add_argument("--source", default="external")
    prototype_build.add_argument("--dataset")
    prototype_build.add_argument("--source-path")
    prototype_build.add_argument("--limit", type=int, default=80)
    prototype_build.add_argument("--min-views", type=int, default=0)
    prototype_build.add_argument("--force", action="store_true")
    prototypes = sub.add_parser("prototypes")
    prototypes.add_argument("--account", default="main")
    prototypes.add_argument("--source", default="external")
    prototypes.add_argument("--dataset")
    prototypes.add_argument("--limit", type=int, default=20)
    prototype_match = sub.add_parser("prototype-match")
    prototype_match.add_argument("segment_id")
    prototype_match.add_argument("--account")
    prototype_match.add_argument("--source", default="external")
    prototype_match.add_argument("--dataset")
    prototype_match.add_argument("--limit", type=int, default=5)
    training = sub.add_parser("training-samples")
    training.add_argument("--account")
    training.add_argument("--limit", type=int, default=50)
    baselines = sub.add_parser("baselines")
    baselines.add_argument("--account")
    web = sub.add_parser("web")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--reload", action="store_true")

    args = parser.parse_args()
    if args.command == "init":
        _print(cmd_init())
    elif args.command == "videos":
        init_db()
        _print({"videos": list_videos()})
    elif args.command == "doctor":
        _print(cmd_doctor())
    elif args.command == "setup-asr":
        _print(cmd_setup_asr(args.model, args.force, args.vad_model, args.profile))
    elif args.command == "bench-asr":
        _print(cmd_bench_asr(args.input_path, args.backend, args.models, args.profile, args.output_dir, args.duration_seconds))
    elif args.command == "ingest":
        _print(cmd_ingest(args.video_path, args.account, args.title))
    elif args.command == "rights" and args.rights_command == "set":
        _print(cmd_rights_set(args.asset_type, args.asset_id, args.program, args.song, args.performance, args.artist, args.platforms, args.duration, args.accounts))
    elif args.command == "extract":
        _print(cmd_extract(args.video_id, args.force_asr, args.asr_profile, args.asr_model, args.asr_backend))
    elif args.command == "generate-segments":
        _print(cmd_generate_segments(args.video_id, args.top_k))
    elif args.command == "score":
        _print(cmd_score(args.video_id))
    elif args.command == "suggest":
        _print(cmd_suggest(args.video_id, args.top_k))
    elif args.command == "manifest":
        _print(cmd_manifest(args.video_id))
    elif args.command == "review-segment":
        _print(cmd_review_segment(args.segment_id, args.status, args.reason, args.operator))
    elif args.command == "verify-asr":
        _print(cmd_verify_asr(args.segment_id, args.profile, args.model, args.backend, args.force))
    elif args.command == "export":
        _print(cmd_export(args.segment_id, args.title))
    elif args.command == "import-metrics":
        _print(cmd_import_metrics(args.csv_path))
    elif args.command == "insights":
        _print(cmd_insights(args.account))
    elif args.command == "rebuild-feedback":
        _print(cmd_rebuild_feedback(args.account))
    elif args.command == "douyin-account":
        _print(cmd_douyin_account(args.account, args.display_name, args.platform_account_id))
    elif args.command == "douyin-sync":
        _print(cmd_douyin_sync(args.account, args.source, args.path, args.windows))
    elif args.command == "douyin-summary":
        _print(cmd_douyin_summary(args.account))
    elif args.command == "douyin-login-url":
        _print(cmd_douyin_login_url(args.account, args.scopes, args.redirect_uri))
    elif args.command == "douyin-auth-code":
        _print(cmd_douyin_auth_code(args.code, args.state, not args.no_exchange))
    elif args.command == "douyin-auth-status":
        _print(cmd_douyin_auth_status(args.account, args.state))
    elif args.command == "douyin-visible-clean":
        _print(cmd_douyin_visible_clean(args.input_dir, args.output_dir))
    elif args.command == "douyin-account-library":
        _print(cmd_douyin_account_library(args.input_path, args.output_path, args.observed_at, args.source_method))
    elif args.command == "douyin-account-works-clean":
        _print(
            cmd_douyin_account_works_clean(
                args.account_library,
                args.account,
                args.raw_works,
                args.output_root,
                args.run_id,
                args.rejected_author_mismatch,
                args.source_method,
                args.observed_at,
            )
        )
    elif args.command == "douyin-media-collect":
        _print(
            cmd_douyin_media_collect(
                args.plan_path,
                args.stage,
                args.account,
                args.limit,
                args.output_root,
                args.report_dir,
                args.run_id,
                args.page_delay_seconds,
                args.extra_wait_seconds,
                not args.no_extract_audio,
                args.dry_run,
                args.max_storage_gb,
            )
        )
    elif args.command == "memory-build":
        _print(cmd_memory_build(args.account, args.force))
    elif args.command == "history":
        _print(cmd_history(args.segment_id, args.account, args.limit))
    elif args.command == "interest-clock":
        _print(cmd_interest_clock(args.account, args.content_type, args.duration, args.limit, args.rebuild))
    elif args.command == "backtest":
        _print(cmd_backtest(args.account, args.k, args.strategy, args.holdout_policy, args.label_version))
    elif args.command == "ranker-tuning-run":
        _print(cmd_ranker_tuning(args.account, args.k, args.holdout_policy, args.max_trials, args.label_version))
    elif args.command == "semantic-feature-experiment":
        _print(
            cmd_semantic_feature_experiment(
                args.account,
                args.k,
                args.holdout_policy,
                args.label_version,
                include_field_masks=not args.skip_field_masks,
            )
        )
    elif args.command == "semantic-features-backfill":
        _print(cmd_semantic_features_backfill(args.account, args.dataset, args.limit, args.force))
    elif args.command == "slice-structure-evaluate":
        _print(cmd_slice_structure_evaluate(args.account, args.dataset, args.limit, args.min_confidence))
    elif args.command == "multimodal-collection-plan":
        _print(cmd_multimodal_collection_plan(args.account, args.dataset, args.limit, args.stage, args.output_path, args.include_ready))
    elif args.command == "multimodal-collect":
        _print(
            cmd_multimodal_collect(
                args.plan_path,
                args.account,
                args.dataset,
                args.limit,
                args.stage,
                args.output_root,
                args.report_dir,
                args.run_id,
                args.page_delay_seconds,
                args.extra_wait_seconds,
                not args.no_extract_audio,
                not args.download,
                args.max_storage_gb,
            )
        )
    elif args.command == "multimodal-validation":
        _print(cmd_multimodal_validation(args.account, args.dataset, args.limit, args.k, args.min_samples, args.min_asset_coverage))
    elif args.command == "multimodal-feature-experiment":
        _print(
            cmd_multimodal_feature_experiment(
                args.account,
                args.dataset,
                args.limit,
                args.k,
                args.min_feature_samples,
                args.audio_window_seconds,
                args.force,
            )
        )
    elif args.command == "qwen-embeddings-build":
        _print(cmd_qwen_embeddings_build(args.account, args.dataset, args.entity_type, args.modality, args.limit, args.force))
    elif args.command == "qwen-embedding-evidence":
        _print(cmd_qwen_embedding_evidence(args.account, args.dataset, args.limit, args.k, args.modality))
    elif args.command == "qwen-omni-status":
        _print(cmd_qwen_omni_status())
    elif args.command == "qwen-omni-analyze":
        _print(cmd_qwen_omni_analyze(args.segment_id, args.account, args.max_clip_seconds, args.load_model))
    elif args.command == "qwen-omni-shadow-run":
        _print(cmd_qwen_omni_shadow_run(args.account, args.dataset, args.limit, args.max_clip_seconds, args.load_model))
    elif args.command == "backtest-reports":
        _print(cmd_backtest_reports(args.account, args.limit))
    elif args.command == "datasets":
        _print(cmd_datasets())
    elif args.command == "historical-import":
        _print(cmd_historical_import(args.account, args.dataset, args.source_path, args.force))
    elif args.command == "douyin-history-import":
        _print(cmd_douyin_history_import(args.account, args.clean_dir, args.raw_dir, args.dataset, args.dataset_name, args.output_dir, args.force))
    elif args.command == "douyin-history-baselines":
        _print(cmd_douyin_history_baselines(args.account, args.dataset, args.output_dir, args.min_count, args.limit))
    elif args.command == "historical-samples":
        _print(cmd_historical_samples(args.account, args.dataset, args.limit))
    elif args.command == "historical-summary":
        _print(cmd_historical_summary(args.account))
    elif args.command == "research-coverage":
        _print(cmd_research_coverage(args.account, args.dataset))
    elif args.command == "semantic-calibration-queue":
        _print(
            cmd_semantic_calibration_queue(
                args.account,
                args.dataset,
                args.limit,
                args.min_priority,
                args.label,
                args.queue_type,
                args.strategy,
                args.min_disagreement,
            )
        )
    elif args.command == "semantic-calibration-reopen":
        _print(cmd_semantic_calibration_reopen(args.sample_id, args.confidence, args.operator, args.reason))
    elif args.command == "research-labels-rebuild":
        _print(cmd_research_labels_rebuild(args.account, args.dataset, args.min_baseline_samples))
    elif args.command == "prototype-build":
        _print(cmd_prototype_build(args.account, args.source, args.source_path, args.dataset, args.limit, args.min_views, args.force))
    elif args.command == "prototypes":
        _print(cmd_prototypes(args.account, args.source, args.dataset, args.limit))
    elif args.command == "prototype-match":
        _print(cmd_prototype_match(args.segment_id, args.account, args.source, args.dataset, args.limit))
    elif args.command == "training-samples":
        _print(cmd_training_samples(args.account, args.limit))
    elif args.command == "baselines":
        _print(cmd_baselines(args.account))
    elif args.command == "web":
        try:
            cmd_web(args.host, args.port, args.reload)
        except RuntimeError as exc:
            parser.exit(1, f"error: {exc}\n")


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
