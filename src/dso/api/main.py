from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from dso.artifacts import record_artifact, video_manifest, write_artifact_json
from dso.api.dashboard import dashboard_static_dir, render_dashboard
from dso.config import ensure_data_dirs
from dso.corrections.editor import (
    create_performance,
    delete_performance,
    list_performances,
    update_candidate_segment,
    update_performance,
)
from dso.db.session import connect, fetch_one
from dso.db.session import init_db
from dso.features.asr import transcribe_video
from dso.features.asr_verify import latest_asr_verification, list_asr_verifications, verify_candidate_asr
from dso.features.audio import extract_audio_features
from dso.feedback.douyin import douyin_sync_contract, douyin_sync_summary, register_douyin_account, sync_douyin_feedback
from dso.feedback.douyin_auth import (
    complete_douyin_qr_login,
    douyin_oauth_config,
    douyin_oauth_status,
    start_douyin_qr_login,
)
from dso.feedback.importer import account_baselines, account_insights, import_metrics, list_training_samples, rebuild_feedback_state
from dso.feedback.platform import (
    create_platform_mapping,
    list_platform_accounts,
    list_platform_mappings,
    list_platform_sync_runs,
    map_platform_metric_row,
    platform_metric_contract,
    upsert_platform_account,
)
from dso.media.ingest import ingest_video, list_videos
from dso.learning.backtest import backtest_rule_ranker, list_backtest_reports, run_ranker_tuning
from dso.learning.historical_samples import (
    douyin_history_baselines,
    export_douyin_history_assets,
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
from dso.learning.prototypes import build_prototype_bank, list_capture_datasets, list_prototype_bank, match_segment_prototypes
from dso.quality.insights import quality_insights
from dso.review import list_change_events, list_review_events, mark_candidate_review
from dso.runtime import runtime_diagnostics
from dso.scoring.scorer import sanitize_title_suggestions, score_video, suggestions
from dso.segments.generator import generate_segments
from dso.simulation.recommender import simulate_segment, simulate_video
from dso.utils import utc_now
from dso.variants.exporter import (
    create_experiment,
    create_variant,
    export_segment,
    export_preflight,
    list_experiments,
    list_variants,
    update_variant,
)
from dso.versions import FEEDBACK_STATE_VERSION, QUALITY_GATE_VERSION, SCORER_VERSION, SEGMENTER_VERSION

try:
    from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
except Exception as exc:  # pragma: no cover
    raise RuntimeError("FastAPI is required for the web API. Install with: pip install -e .") from exc


app = FastAPI(title="Douyin Slice Optimizer", version="0.1.0")
_dashboard_static_dir = dashboard_static_dir()
if _dashboard_static_dir.is_dir():
    app.mount("/static/dashboard", StaticFiles(directory=_dashboard_static_dir, html=True), name="dashboard_static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return render_dashboard(_dashboard_stats(), list_videos())


@app.get("/stats")
def stats() -> dict:
    return _dashboard_stats()


@app.get("/runtime")
def runtime() -> dict:
    return runtime_diagnostics()


@app.get("/videos")
def videos() -> dict:
    return {"videos": list_videos()}


@app.post("/videos")
async def create_video(
    account_id: str = Form("main"),
    title: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    suffix = Path(file.filename or "upload.mp4").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return ingest_video(tmp_path, account_id=account_id, title=title)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/videos/{video_id}/extract")
def extract(video_id: str) -> dict:
    transcript = transcribe_video(video_id)
    audio = extract_audio_features(video_id)
    settings = ensure_data_dirs()
    transcript_path = settings.cache_dir / video_id / "transcript" / "transcript.json"
    record_artifact(
        video_id,
        step="transcript",
        artifact_type="transcript",
        artifact_path=transcript_path,
        version=str((transcript.get("metadata") or {}).get("postprocess_version") or ""),
        summary={"source": transcript["source"], "segments": len(transcript["segments"])},
    )
    record_artifact(
        video_id,
        step="audio",
        artifact_type="audio_features",
        artifact_path=audio.get("wav_path") or "",
        summary={"peaks": len(audio["peaks"]), "frames": len(audio["frames"])},
    )
    return {"transcript_source": transcript["source"], "segments": len(transcript["segments"]), "audio_peaks": len(audio["peaks"])}


@app.post("/videos/{video_id}/segments")
def segments(video_id: str, top_k: int = 30) -> dict:
    rows = generate_segments(video_id, top_k=top_k)
    record_artifact(
        video_id,
        step="candidates",
        artifact_type="db_rows",
        version=SEGMENTER_VERSION,
        summary={"count": len(rows), "top_k": top_k},
    )
    return {"count": len(rows), "segments": rows}


@app.post("/videos/{video_id}/score")
def score(video_id: str) -> dict:
    rows = score_video(video_id)
    record_artifact(
        video_id,
        step="scores",
        artifact_type="db_rows",
        version=SCORER_VERSION,
        summary={"count": len(rows)},
    )
    return {"count": len(rows), "scores": rows}


@app.get("/videos/{video_id}/suggestions")
def get_suggestions(video_id: str, top_k: int = 10) -> dict:
    rows = [_attach_segment_variants(row) for row in suggestions(video_id, top_k=top_k)]
    return {"suggestions": rows}


@app.get("/videos/{video_id}/simulation")
def get_video_simulation(video_id: str, top_k: int = 10) -> dict:
    return simulate_video(video_id, top_k=top_k)


@app.get("/videos/{video_id}/quality")
def get_video_quality(video_id: str, top_k: int = 30) -> dict:
    try:
        report = quality_insights(video_id, top_k=top_k)
        write_artifact_json(
            video_id,
            step="quality",
            filename="quality_report.json",
            data=report,
            artifact_type="quality_report",
            version=QUALITY_GATE_VERSION,
            summary={
                "gate_status": (report.get("gate") or {}).get("status"),
                "health_score": (report.get("health") or {}).get("score"),
                "issues": len(report.get("issues") or []),
            },
        )
        return report
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/videos/{video_id}/manifest")
def get_video_manifest(video_id: str) -> dict:
    try:
        return video_manifest(video_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/videos/{video_id}/performances")
def get_performances(video_id: str) -> dict:
    try:
        rows = list_performances(video_id)
        return {"count": len(rows), "performances": rows}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/videos/{video_id}/performances")
def post_performance(video_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return create_performance(video_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/performances/{performance_id}")
def patch_performance(performance_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return update_performance(performance_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/performances/{performance_id}")
def remove_performance(performance_id: str) -> dict:
    try:
        return delete_performance(performance_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/segments/{segment_id}/correction")
def patch_candidate_segment(segment_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return update_candidate_segment(segment_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/segments/{segment_id}/review")
def review_segment(segment_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        result = mark_candidate_review(
            segment_id,
            payload.get("status") or payload.get("review_status") or "review",
            reason=payload.get("reason") or "",
            operator=payload.get("operator") or "local",
        )
        with connect() as conn:
            row = fetch_one(
                conn,
                """
                SELECT c.*, s.final_score, s.score_explanation, s.title_suggestions,
                       s.cover_suggestion, s.risk_notes, s.rights_risk_score, s.low_originality_score,
                       s.ranker_score, s.ranker_version, s.learning_signals_json
                FROM candidate_segments c
                LEFT JOIN slice_scores s ON s.candidate_segment_id = c.id
                WHERE c.id = ?
                """,
                [segment_id],
            )
        return {**result, "segment": _attach_segment_variants(row) if row else None}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/segments/{segment_id}/changes")
def segment_changes(segment_id: str) -> dict:
    return {
        "segment_id": segment_id,
        "review_events": list_review_events(segment_id)["events"],
        "changes": list_change_events(segment_id=segment_id)["changes"],
    }


@app.get("/segments/{segment_id}/history")
def segment_history(segment_id: str, account_id: str | None = None, limit: int = 8) -> dict:
    try:
        return calibrate_segment_history(segment_id, account_id=account_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/segments/{segment_id}/prototypes")
def segment_prototype_matches(
    segment_id: str,
    account_id: str | None = None,
    source: str = "external",
    dataset_id: str | None = None,
    limit: int = 5,
) -> dict:
    try:
        return match_segment_prototypes(segment_id, account_id=account_id, source=source, dataset_id=dataset_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/segments/{segment_id}/asr/verify")
def verify_segment_asr(segment_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return verify_candidate_asr(
            segment_id,
            asr_profile=payload.get("asr_profile") or payload.get("profile") or "verify",
            model_size=payload.get("model_size") or payload.get("model"),
            backend=payload.get("backend"),
            force=bool(payload.get("force")),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/segments/{segment_id}/asr/verify")
def segment_asr_verifications(segment_id: str) -> dict:
    return list_asr_verifications(segment_id)


@app.get("/segments/{segment_id}/simulation")
def get_segment_simulation(segment_id: str) -> dict:
    try:
        return simulate_segment(segment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/segments/{segment_id}/export")
def export(segment_id: str, force: bool = False) -> dict:
    try:
        return _attach_export_urls(export_segment(segment_id, force=force))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/segments/{segment_id}/export/preflight")
def segment_export_preflight(segment_id: str, variant_id: str | None = None) -> dict:
    try:
        return export_preflight(segment_id, variant_id=variant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/segments/{segment_id}/variants")
def variants(segment_id: str) -> dict:
    rows = [_attach_export_urls(row) for row in list_variants(segment_id)]
    return {"count": len(rows), "variants": rows}


@app.post("/segments/{segment_id}/variants")
def post_variant(segment_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        options = dict(payload)
        title = options.pop("title", None)
        row = create_variant(segment_id, title=title, **options)
        return _attach_export_urls(row)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/variants/{variant_id}")
def patch_variant(variant_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return _attach_export_urls(update_variant(variant_id, payload))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/variants/{variant_id}/experiments")
def post_experiment(variant_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return create_experiment(variant_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/variants/{variant_id}/experiments")
def variant_experiments(variant_id: str) -> dict:
    rows = list_experiments(variant_id)
    return {"count": len(rows), "experiments": rows}


@app.get("/exports/{export_path:path}")
def exported_file(export_path: str) -> FileResponse:
    settings = ensure_data_dirs()
    base = settings.exports_dir.resolve()
    target = (base / export_path).resolve()
    if not target.is_file() or base not in target.parents:
        raise HTTPException(status_code=404, detail="export not found")
    return FileResponse(target)


@app.post("/metrics/import")
async def metrics_import(file: UploadFile = File(...), sample_source: str = "csv") -> dict:
    suffix = Path(file.filename or "metrics.csv").suffix.lower() or ".csv"
    if suffix == ".xslx":
        suffix = ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return import_metrics(tmp_path, sample_source=sample_source)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/platform/accounts")
def platform_accounts(account_id: str | None = None, platform: str | None = None) -> dict:
    rows = list_platform_accounts(account_id=account_id, platform=platform)
    return {
        "contract": douyin_sync_contract(),
        "count": len(rows),
        "accounts": rows,
    }


@app.post("/platform/accounts")
def post_platform_account(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        if (payload.get("platform") or "douyin") == "douyin":
            return register_douyin_account(payload.get("account_id") or "main", payload)
        return upsert_platform_account(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/platform/mappings")
def platform_mappings(
    account_id: str | None = None,
    platform: str | None = None,
    candidate_segment_id: str | None = None,
    slice_variant_id: str | None = None,
    experiment_id: str | None = None,
) -> dict:
    rows = list_platform_mappings(
        account_id=account_id,
        platform=platform,
        candidate_segment_id=candidate_segment_id,
        slice_variant_id=slice_variant_id,
        experiment_id=experiment_id,
    )
    return {
        "contract": platform_metric_contract(),
        "count": len(rows),
        "mappings": rows,
    }


@app.post("/platform/mappings")
def post_platform_mapping(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return create_platform_mapping(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/platform/mock-map")
def platform_mock_map(payload: dict = Body(default_factory=dict)) -> dict:
    return {
        "contract": platform_metric_contract(),
        "mapped_row": map_platform_metric_row(payload, sample_source=payload.get("sample_source") or "mock"),
    }


@app.get("/platform/sync-runs")
def platform_sync_runs(account_id: str | None = None, platform: str | None = None, limit: int = 20) -> dict:
    rows = list_platform_sync_runs(account_id=account_id, platform=platform, limit=limit)
    return {"contract": douyin_sync_contract(), "count": len(rows), "sync_runs": rows}


@app.get("/platform/douyin/contract")
def douyin_contract() -> dict:
    return douyin_sync_contract()


@app.get("/platform/douyin/oauth/config")
def get_douyin_oauth_config() -> dict:
    return douyin_oauth_config()


@app.post("/platform/douyin/oauth/start")
def post_douyin_oauth_start(payload: dict = Body(default_factory=dict)) -> dict:
    return start_douyin_qr_login(
        payload.get("account_id") or "main",
        scopes=payload.get("scopes") or payload.get("scope"),
        redirect_uri=payload.get("redirect_uri"),
    )


@app.get("/platform/douyin/oauth/status")
def get_douyin_oauth_status(account_id: str = "main", state: str | None = None) -> dict:
    return douyin_oauth_status(account_id=account_id, state=state)


@app.get("/platform/douyin/oauth/callback", response_class=HTMLResponse)
def get_douyin_oauth_callback(code: str = "", state: str = "", error: str = "", error_description: str = "") -> str:
    if error:
        return _oauth_callback_html("授权失败", f"{error}: {error_description}")
    try:
        result = complete_douyin_qr_login(code, state, exchange=True)
        return _oauth_callback_html("授权完成", f"账号状态：{_esc(result.get('status'))}。可以回到工作台继续同步。")
    except Exception as exc:
        return _oauth_callback_html("授权待处理", _esc(str(exc)))


@app.post("/platform/douyin/oauth/callback")
def post_douyin_oauth_callback(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return complete_douyin_qr_login(
            payload.get("code") or "",
            payload.get("state") or "",
            exchange=bool(payload.get("exchange", True)),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/platform/douyin/summary")
def get_douyin_summary(account_id: str = "main") -> dict:
    return douyin_sync_summary(account_id)


@app.post("/platform/douyin/sync")
def post_douyin_sync(payload: dict = Body(default_factory=dict)) -> dict:
    result = sync_douyin_feedback(
        payload.get("account_id") or "main",
        source=payload.get("source") or "mock",
        payload=payload,
        windows=_parse_windows(payload.get("windows")),
        sync_mode=payload.get("sync_mode") or "manual",
    )
    if result.get("status") == "failed":
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/platform/douyin/sync-file")
async def post_douyin_sync_file(
    file: UploadFile = File(...),
    account_id: str = Form("main"),
    source: str = Form("csv"),
    windows: str = Form(""),
) -> dict:
    suffix = Path(file.filename or "douyin_metrics.csv").suffix.lower() or ".csv"
    if suffix == ".xslx":
        suffix = ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        result = sync_douyin_feedback(
            account_id,
            source=source,
            source_path=tmp_path,
            windows=_parse_windows(windows),
            sync_mode="manual_file",
        )
        if result.get("status") == "failed":
            raise HTTPException(status_code=400, detail=result)
        return result
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/accounts/{account_id}/insights")
def insights(account_id: str) -> dict:
    return account_insights(account_id)


@app.get("/accounts/{account_id}/baselines")
def baselines(account_id: str) -> dict:
    rows = account_baselines(account_id)
    return {
        "contract_version": FEEDBACK_STATE_VERSION,
        "status": "ready" if rows else "empty",
        "generated_at": utc_now(),
        "account_id": account_id,
        "query": {"account_id": account_id},
        "count": len(rows),
        "baselines": rows,
    }


@app.get("/training-samples")
def training_samples(account_id: str | None = None, limit: int = 50) -> dict:
    rows = list_training_samples(account_id=account_id, limit=limit)
    return {
        "contract_version": FEEDBACK_STATE_VERSION,
        "status": "ready" if rows else "empty",
        "generated_at": utc_now(),
        "account_id": account_id or "all",
        "query": {"account_id": account_id or "all", "limit": limit},
        "count": len(rows),
        "training_samples": rows,
    }


@app.post("/feedback/rebuild")
def rebuild_feedback(account_id: str | None = None) -> dict:
    return rebuild_feedback_state(account_id)


@app.post("/learning/memory/build")
def post_memory_build(payload: dict = Body(default_factory=dict)) -> dict:
    return build_text_memory_bank(account_id=payload.get("account_id"), force=bool(payload.get("force")))


@app.get("/accounts/{account_id}/interest-clock")
def get_interest_clock(account_id: str, content_type: str | None = None, duration_seconds: float | None = None, limit: int = 5) -> dict:
    return recommend_publish_hours(account_id, content_type=content_type, duration_seconds=duration_seconds, limit=limit)


@app.post("/accounts/{account_id}/interest-clock/rebuild")
def post_interest_clock(account_id: str) -> dict:
    return build_interest_clock(account_id)


@app.post("/learning/backtest")
def post_backtest(payload: dict = Body(default_factory=dict)) -> dict:
    return backtest_rule_ranker(
        account_id=payload.get("account_id"),
        k=int(payload.get("k") or 10),
        strategy=payload.get("strategy") or "research_ranker_v2_2",
        holdout_policy=payload.get("holdout_policy") or "time",
        label_version=payload.get("label_version"),
    )


@app.get("/learning/backtest")
def get_backtests(account_id: str | None = None, limit: int = 10) -> dict:
    return list_backtest_reports(account_id=account_id, limit=limit)


@app.get("/learning/datasets")
def get_learning_datasets(account_id: str | None = None) -> dict:
    catalog = list_capture_datasets()
    history = historical_sample_summary(account_id=account_id)
    datasets = _attach_history_summary_to_datasets(catalog.get("datasets") or [], history)
    enriched = {
        **catalog,
        "count": len(datasets),
        "datasets": datasets,
        "historical_summary": history,
    }
    for key in [
        "stored_sample_count",
        "formal_sample_count",
        "deduped_sample_count",
        "trainable_sample_count",
        "metric_coverage_sample_count",
        "metric_coverage",
        "interaction_coverage",
        "likes_coverage_rate",
        "favorites_coverage_rate",
        "comments_coverage_rate",
        "shares_coverage_rate",
        "play_missing_count",
        "play_missing_rate",
        "duplicate_item_group_count",
        "duplicate_item_groups",
    ]:
        enriched[key] = history.get(key)
    return enriched


@app.get("/learning/research/coverage")
def get_research_coverage(account_id: str | None = None, dataset_id: str | None = None) -> dict:
    return research_field_coverage(account_id=account_id, dataset_id=dataset_id)


@app.get("/learning/semantic-calibration/queue")
def get_semantic_calibration_queue(
    account_id: str | None = None,
    dataset_id: str | None = None,
    limit: int = 50,
    min_priority: float = 0,
    label: str | None = None,
    queue_type: str = "mixed",
    strategy: str = "research_ranker_v2_2",
    min_disagreement: float = 0,
) -> dict:
    return semantic_calibration_queue(
        account_id=account_id,
        dataset_id=dataset_id,
        limit=limit,
        min_priority=min_priority,
        label=label,
        queue_type=queue_type,
        strategy=strategy,
        min_disagreement=min_disagreement,
    )


@app.patch("/learning/historical-samples/{sample_id}/labels")
def patch_historical_sample_labels(sample_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return update_historical_sample_labels(sample_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/historical-samples/{sample_id}/calibration/reopen")
def post_reopen_historical_sample_calibration(sample_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return reopen_historical_sample_calibration(sample_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/research-labels/rebuild")
def post_research_labels_rebuild(payload: dict = Body(default_factory=dict)) -> dict:
    return rebuild_research_labels(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        min_baseline_samples=int(payload.get("min_baseline_samples") or 20),
    )


@app.post("/learning/ranker-tuning/run")
def post_ranker_tuning(payload: dict = Body(default_factory=dict)) -> dict:
    return run_ranker_tuning(
        account_id=payload.get("account_id"),
        k=int(payload.get("k") or 10),
        holdout_policy=payload.get("holdout_policy") or "time",
        max_trials=int(payload.get("max_trials") or 12),
        label_version=payload.get("label_version"),
    )


def _attach_history_summary_to_datasets(datasets: list[dict], history: dict) -> list[dict]:
    history_by_dataset = {
        item.get("dataset_id") or item.get("id"): item
        for item in history.get("datasets") or []
        if item.get("dataset_id") or item.get("id")
    }
    seen: set[str] = set()
    enriched = []
    for dataset in datasets:
        item = _dataset_with_history_fields(dataset, history if dataset.get("id") == "all" else history_by_dataset.get(dataset.get("id")))
        seen.add(item.get("id") or "")
        enriched.append(item)
    for dataset_id, item in history_by_dataset.items():
        if dataset_id in seen:
            continue
        enriched.append(_historical_only_dataset(item))
    return enriched


def _dataset_with_history_fields(dataset: dict, history_item: dict | None) -> dict:
    item = dict(dataset)
    if history_item:
        item["account_id"] = item.get("account_id") or history_item.get("account_id") or history_item.get("program_key") or ""
        item["account_display_name"] = item.get("account_display_name") or history_item.get("account_display_name") or ""
        item["account_tier"] = item.get("account_tier") or history_item.get("account_tier") or ""
        item["display_name"] = item.get("display_name") or history_item.get("display_name") or history_item.get("name") or item.get("name") or item.get("id")
    item["source_row_count"] = int(item.get("raw_rows") or 0)
    item["source_unique_count"] = int(item.get("unique_count") or 0)
    item["source_dedup_count"] = int(item.get("unique_count") or 0)
    if not history_item:
        item["historical_summary"] = None
        item["stored_sample_count"] = 0
        item["formal_sample_count"] = 0
        item["deduped_sample_count"] = 0
        item["trainable_sample_count"] = 0
        item["duplicate_item_group_count"] = 0
        item["duplicate_item_groups"] = []
        item["metric_coverage"] = {}
        item["interaction_coverage"] = {}
        item["play_missing_count"] = 0
        item["play_missing_rate"] = 0.0
        return item
    item["historical_summary"] = history_item
    lineage = history_item.get("data_lineage") or {}
    source_row_count = int(lineage.get("source_row_count") or history_item.get("source_row_count") or item.get("source_row_count") or 0)
    source_unique_count = int(lineage.get("source_unique_count") or history_item.get("source_unique_count") or item.get("source_unique_count") or 0)
    source_dedup_count = int(lineage.get("source_dedup_count") or history_item.get("source_dedup_count") or source_unique_count)
    stored_sample_count = int(history_item.get("stored_sample_count") or history_item.get("sample_count") or 0)
    item["source_row_count"] = source_row_count
    item["source_unique_count"] = source_unique_count
    item["source_dedup_count"] = source_dedup_count
    item["raw_rows"] = source_row_count
    item["unique_count"] = source_unique_count
    item["sample_count"] = stored_sample_count
    if lineage.get("source_paths"):
        item["source_paths"] = lineage.get("source_paths") or []
    for key in [
        "stored_sample_count",
        "formal_sample_count",
        "deduped_sample_count",
        "trainable_sample_count",
        "metric_coverage_sample_count",
        "metric_coverage",
        "interaction_coverage",
        "likes_coverage_rate",
        "favorites_coverage_rate",
        "comments_coverage_rate",
        "shares_coverage_rate",
        "play_missing_count",
        "play_missing_rate",
        "duplicate_item_group_count",
        "duplicate_item_groups",
    ]:
        item[key] = history_item.get(key)
    return item


def _historical_only_dataset(history_item: dict) -> dict:
    lineage = history_item.get("data_lineage") or {}
    dataset = {
        "id": history_item.get("dataset_id") or history_item.get("id") or "default",
        "dataset_id": history_item.get("dataset_id") or history_item.get("id") or "default",
        "name": history_item.get("name") or history_item.get("dataset_id") or "default",
        "display_name": history_item.get("display_name") or history_item.get("name") or history_item.get("dataset_id") or "default",
        "account_id": history_item.get("account_id") or "",
        "account_display_name": history_item.get("account_display_name") or history_item.get("account_id") or "",
        "account_tier": history_item.get("account_tier") or "",
        "program_key": history_item.get("program_key") or "",
        "kind": "historical_capture",
        "source_paths": lineage.get("source_paths") or [],
        "raw_rows": int(lineage.get("source_row_count") or 0),
        "sample_count": int(lineage.get("source_row_count") or 0),
        "unique_count": int(lineage.get("source_unique_count") or 0),
        "max_views": int(history_item.get("max_views") or 0),
        "latest_at": history_item.get("latest_at") or "",
    }
    return _dataset_with_history_fields(dataset, history_item)


@app.post("/learning/historical-samples/import")
def post_historical_samples_import(payload: dict = Body(default_factory=dict)) -> dict:
    if (payload.get("source_type") or payload.get("source_kind")) == "douyin_clean":
        clean_dir = payload.get("clean_dir") or payload.get("source_path")
        if not clean_dir:
            raise HTTPException(status_code=400, detail="clean_dir or source_path is required for douyin_clean import")
        return import_douyin_history(
            account_id=payload.get("account_id") or "main",
            clean_dir=clean_dir,
            raw_dir=payload.get("raw_dir"),
            dataset_id=payload.get("dataset_id"),
            dataset_name=payload.get("dataset_name"),
            output_dir=payload.get("output_dir"),
            force=bool(payload.get("force")),
        )
    return import_historical_samples(
        account_id=payload.get("account_id") or "main",
        dataset_id=payload.get("dataset_id"),
        source_path=payload.get("source_path"),
        force=bool(payload.get("force")),
    )


@app.post("/learning/douyin-history/import")
def post_douyin_history_import(payload: dict = Body(default_factory=dict)) -> dict:
    clean_dir = payload.get("clean_dir") or payload.get("source_path")
    if not clean_dir:
        raise HTTPException(status_code=400, detail="clean_dir or source_path is required")
    return import_douyin_history(
        account_id=payload.get("account_id") or "main",
        clean_dir=clean_dir,
        raw_dir=payload.get("raw_dir"),
        dataset_id=payload.get("dataset_id"),
        dataset_name=payload.get("dataset_name"),
        output_dir=payload.get("output_dir"),
        force=bool(payload.get("force")),
    )


@app.get("/learning/douyin-history/baselines")
def get_douyin_history_baselines(
    account_id: str | None = None,
    dataset_id: str | None = None,
    min_count: int = 2,
    limit: int = 80,
) -> dict:
    return douyin_history_baselines(account_id=account_id, dataset_id=dataset_id, min_count=min_count, limit=limit)


@app.post("/learning/douyin-history/export")
def post_douyin_history_export(payload: dict = Body(default_factory=dict)) -> dict:
    return {
        "outputs": export_douyin_history_assets(
            account_id=payload.get("account_id"),
            dataset_id=payload.get("dataset_id"),
            output_dir=payload.get("output_dir") or "outputs/douyin_history_assets",
        )
    }


@app.get("/learning/historical-samples")
def get_historical_samples(account_id: str | None = "main", dataset_id: str | None = None, limit: int = 50) -> dict:
    return list_historical_samples(account_id=account_id, dataset_id=dataset_id, limit=limit)


@app.get("/learning/historical-samples/summary")
def get_historical_samples_summary(account_id: str | None = "main") -> dict:
    return historical_sample_summary(account_id=account_id)


@app.post("/learning/prototypes/build")
def post_prototype_build(payload: dict = Body(default_factory=dict)) -> dict:
    return build_prototype_bank(
        account_id=payload.get("account_id") or "main",
        source=payload.get("source") or "external",
        dataset_id=payload.get("dataset_id"),
        source_path=payload.get("source_path"),
        limit=int(payload.get("limit") or 80),
        min_views=int(payload.get("min_views") or 0),
        force=bool(payload.get("force")),
    )


@app.get("/accounts/{account_id}/prototypes")
def get_prototypes(account_id: str, source: str = "external", dataset_id: str | None = None, limit: int = 20) -> dict:
    return list_prototype_bank(account_id, source=source, dataset_id=dataset_id, limit=limit)


def _esc(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _dashboard_stats() -> dict:
    with connect() as conn:
        videos_count = fetch_one(conn, "SELECT COUNT(*) AS count FROM source_videos")["count"]
        segments_count = fetch_one(conn, "SELECT COUNT(*) AS count FROM candidate_segments")["count"]
        exports_count = fetch_one(conn, "SELECT COUNT(*) AS count FROM slice_variants WHERE export_path IS NOT NULL AND export_path != ''")["count"]
        samples_count = fetch_one(conn, "SELECT COUNT(*) AS count FROM training_samples")["count"]
    return {
        "videos": int(videos_count or 0),
        "segments": int(segments_count or 0),
        "exports": int(exports_count or 0),
        "training_samples": int(samples_count or 0),
    }


def _attach_segment_variants(row: dict) -> dict:
    row = _parse_segment_json_fields(dict(row))
    variants = [_attach_export_urls(item) for item in list_variants(row["id"])]
    exported = [item for item in variants if item.get("export_path")]
    row["variants"] = variants
    row["variant_count"] = len(variants)
    row["latest_export"] = exported[0] if exported else None
    row["platform_mappings"] = list_platform_mappings(candidate_segment_id=row["id"], platform="douyin")
    row["latest_asr_verification"] = latest_asr_verification(row["id"])
    row["feedback_summary"] = _candidate_feedback_summary(row["id"])
    row["review_events"] = list_review_events(row["id"], limit=5)["events"]
    row.update(_candidate_review_contract(row, exported=bool(exported)))
    return row


def _parse_segment_json_fields(row: dict) -> dict:
    for key, default in [("title_suggestions", []), ("risk_notes", [])]:
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except Exception:
                row[key] = default
        elif value is None:
            row[key] = default
    row["title_suggestions"] = sanitize_title_suggestions(row.get("title_suggestions") or [])
    signals = row.pop("learning_signals_json", None)
    if isinstance(signals, str):
        try:
            row["learning_signals"] = json.loads(signals)
        except Exception:
            row["learning_signals"] = {}
    elif signals is not None:
        row["learning_signals"] = signals
    return row


def _candidate_review_contract(row: dict, *, exported: bool = False) -> dict:
    status = str(row.get("status") or "candidate")
    rights_risk = float(row.get("rights_risk_score") or 0)
    low_originality = float(row.get("low_originality_score") or 0)
    reasons: list[str] = []

    if status in {"blocked", "rejected"} or rights_risk >= 80 or low_originality >= 80:
        review_status = "blocked"
        if status in {"blocked", "rejected"}:
            reasons.append("人工状态已标记暂缓")
        if rights_risk >= 80:
            reasons.append("授权风险进入阻断区")
        if low_originality >= 80:
            reasons.append("低原创风险进入阻断区")
        if exported:
            reasons.append("已有导出文件，但当前风险状态优先级更高")
    elif exported:
        review_status = "exported"
        reasons.append("已有导出预览文件")
    elif status in {"approved", "ready"}:
        review_status = "approved"
        reasons.append("人工状态已标记通过")
    elif status in {"corrected", "review", "needs_review"} or rights_risk >= 50 or low_originality >= 45:
        review_status = "needs_review"
        if status == "corrected":
            reasons.append("候选已修正，需要复核通过")
        elif status in {"review", "needs_review"}:
            reasons.append("人工状态要求复核")
        if rights_risk >= 50:
            reasons.append("授权风险需要复核")
        if low_originality >= 45:
            reasons.append("低原创风险需要复核")
    else:
        review_status = "candidate"
        reasons.append("原始候选，等待人工扫读")

    labels = {
        "candidate": "待审核",
        "needs_review": "需复核",
        "approved": "已通过",
        "blocked": "暂缓导出",
        "exported": "已导出",
    }
    return {
        "review_status": review_status,
        "review_status_label": labels[review_status],
        "review_status_reason": "；".join(reasons),
        "review_status_source": "api.derived.v1",
        "workflow_status": "review" if review_status == "needs_review" else review_status,
    }


def _candidate_feedback_summary(segment_id: str) -> dict:
    with connect() as conn:
        row = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS count,
                   MAX(reward_proxy) AS best_reward,
                   MAX(normalized_reward) AS best_normalized_reward,
                   MAX(created_at) AS latest_at
            FROM training_samples
            WHERE candidate_segment_id = ?
            """,
            [segment_id],
        )
        latest = fetch_one(
            conn,
            """
            SELECT label_window, reward_proxy, normalized_reward, train_split, created_at
            FROM training_samples
            WHERE candidate_segment_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [segment_id],
        )
    return {
        "sample_count": int((row or {}).get("count") or 0),
        "best_reward": float((row or {}).get("best_reward") or 0),
        "best_normalized_reward": float((row or {}).get("best_normalized_reward") or 0),
        "latest_at": (row or {}).get("latest_at") or "",
        "latest_sample": latest,
    }


def _attach_export_urls(row: dict) -> dict:
    for key in ["export_path", "subtitle_path", "cover_path"]:
        value = row.get(key)
        if value:
            row[key.replace("_path", "_url")] = _export_url(value)
    return row


def _export_url(path_value: str) -> str | None:
    settings = ensure_data_dirs()
    try:
        rel = Path(path_value).resolve().relative_to(settings.exports_dir.resolve()).as_posix()
    except Exception:
        return None
    return "/exports/" + quote(rel, safe="/")


def _parse_windows(value: object) -> list[str] | None:
    if value in (None, ""):
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _oauth_callback_html(title: str, message: str) -> str:
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Douyin OAuth</title>"
        "<body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:32px;line-height:1.5'>"
        f"<h1>{_esc(title)}</h1><p>{message}</p>"
        "<p>可以关闭此页，回到 Douyin Slice Optimizer 工作台。</p>"
        "</body>"
    )
