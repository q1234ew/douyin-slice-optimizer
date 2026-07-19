from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

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
from dso.features.asr_shadow import qwen3_asr_shadow_status, run_qwen3_asr_shadow
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
from dso.precut import (
    MAX_BATCH_ITEMS,
    create_precut_batch,
    get_precut_batch,
    list_precut_batches,
    process_precut_batch,
    queue_precut_batch,
)
from dso.providers.admin_config import ProviderAdminConfigError, save_provider_connection_config
from dso.providers.service import provider_admin_status, public_model_status, run_fake_provider_smoke
from dso.learning.backtest import backtest_rule_ranker, list_backtest_reports, run_ranker_tuning, semantic_feature_experiment
from dso.learning.benchmark_manifest import load_benchmark_manifest, run_frozen_benchmark, verify_benchmark_manifest
from dso.learning.historical_samples import (
    douyin_history_baselines,
    export_douyin_history_assets,
    import_douyin_history,
    import_historical_samples,
    backfill_semantic_features,
    historical_sample_summary,
    list_historical_samples,
    omni_calibration_replay,
    rebuild_research_labels,
    research_field_coverage,
    reopen_historical_sample_calibration,
    semantic_calibration_queue,
    update_historical_sample_labels,
)
from dso.learning.interest_clock import build_interest_clock, recommend_publish_hours
from dso.learning.material_calibration import (
    material_gold_set_queue,
    reopen_material_gold_annotation,
    run_material_calibration_replay,
    update_material_gold_annotation,
)
from dso.learning.material_confusion import material_confusion_queue, material_taxonomy_contract
from dso.learning.material_evidence import (
    material_evidence_status,
    run_material_evidence_batch,
    run_material_resolver_shadow,
)
from dso.learning.visual_window_scout import (
    DEFAULT_D11B_BATCH_SIZE,
    build_visual_window_scout,
    load_visual_window_build,
    load_visual_window_build_manifest,
    run_visual_window_experiment,
    update_material_window_annotation,
    visual_window_frame_path,
    visual_window_scout_status,
)
from dso.learning.memory import build_text_memory_bank, calibrate_segment_history
from dso.learning.multimodal_validation import (
    DEFAULT_MULTIMODAL_COLLECTION_TARGET,
    build_multimodal_collection_plan,
    collect_multimodal_assets,
    resolve_multimodal_storage_limit_bytes,
    run_multimodal_feature_experiment,
    run_multimodal_validation,
)
from dso.learning.prototypes import build_prototype_bank, list_capture_datasets, list_prototype_bank, match_segment_prototypes
from dso.learning.qwen_embeddings import (
    build_qwen_embedding_index,
    qwen_embedding_evidence_for_segment,
    run_qwen_embedding_evidence,
)
from dso.learning.multimodal_vector_value import (
    DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    freeze_multimodal_vector_experiment,
    multimodal_vector_embedding_request,
    multimodal_vector_experiment_status,
    multimodal_vector_media_path,
    run_multimodal_vector_comparison,
    save_multimodal_vector_review,
    verify_multimodal_vector_manifest,
)
from dso.learning.bailian_vector_chain import (
    bailian_vector_chain_status,
    run_bailian_vector_chain,
)
from dso.learning.bailian_cached_ablation import run_bailian_cached_ablation
from dso.learning.bailian_failure_attribution import (
    run_bailian_holdout_failure_attribution,
)
from dso.learning.bailian_evidence_quality import (
    run_bailian_evidence_quality_reconstruction,
)
from dso.learning.bailian_holdout_validation import (
    evaluate_bailian_holdout_validation,
    freeze_bailian_holdout_validation,
    run_bailian_holdout_prediction,
)
from dso.learning.qwen_omni import analyze_candidate_with_qwen_omni, qwen_omni_status, run_qwen_omni_media_batch, run_qwen_omni_shadow
from dso.learning.omni_slice_ranker import rerank_video_candidates_with_omni, run_hybrid_slice_pipeline
from dso.learning.slice_structure_evaluator import evaluate_slice_structure
from dso.quality.insights import quality_insights
from dso.review import list_change_events, list_review_events, mark_candidate_review
from dso.runtime import runtime_diagnostics
from dso.scoring.ranking_policy import attach_ranking_policy, production_ranking_contract
from dso.scoring.scorer import sanitize_title_suggestions, score_video, suggestions
from dso.scheduler.db import init_scheduler_db
from dso.scheduler.asr import submit_qwen3_asr_job
from dso.scheduler.repository import InvalidJobTransition, JobNotFound, ModelJobRepository
from dso.scheduler.service import (
    model_scheduler_enabled,
    scheduler_resources,
    scheduler_status,
    submit_embedding_build_job,
    submit_omni_rerank_job,
    wait_for_model_job,
)
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
from dso.versions import (
    FEEDBACK_STATE_VERSION,
    HYBRID_SLICE_PIPELINE_VERSION,
    QUALITY_GATE_VERSION,
    SCORER_VERSION,
    SEGMENTER_VERSION,
)

try:
    from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
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
    if model_scheduler_enabled():
        init_scheduler_db()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return render_dashboard(_dashboard_stats(), list_videos())


@app.get("/stats")
def stats() -> dict:
    return _dashboard_stats()


@app.get("/runtime")
def runtime() -> dict:
    return runtime_diagnostics()


@app.get("/providers/status")
def providers_status() -> dict:
    return public_model_status()


def _provider_config_submission_security(request: Request) -> tuple[bool, str]:
    """Allow secrets only through HTTPS proxying or a direct loopback tunnel."""

    client_host = (request.client.host if request.client else "").strip().lower()
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        return False, "API Key 只能通过 HTTPS 或 SSH 本地端口转发提交"
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if forwarded_proto:
        if forwarded_proto == "https":
            return True, "当前连接由可信本机反向代理以 HTTPS 转发"
        return False, "当前为公网 HTTP，禁止提交 API Key"
    has_proxy_headers = bool(
        request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    )
    host = (request.url.hostname or "").strip().lower()
    if not has_proxy_headers and host in {"127.0.0.1", "::1", "localhost"}:
        return True, "当前通过 SSH 本地端口转发访问"
    return False, "API Key 只能通过 HTTPS 或 SSH 本地端口转发提交"


def _same_origin_request(request: Request) -> bool:
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return True
    parsed = urlsplit(origin)
    host = request.headers.get("host", "").strip().lower()
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    scheme = forwarded_proto or request.url.scheme
    return parsed.scheme.lower() == scheme.lower() and parsed.netloc.lower() == host


@app.get("/providers/config")
def providers_config(request: Request, response: Response) -> dict:
    allowed, reason = _provider_config_submission_security(request)
    response.headers["Cache-Control"] = "no-store"
    return provider_admin_status(
        secure_submission_allowed=allowed,
        secure_submission_reason=reason,
    )


@app.post("/providers/config")
def update_providers_config(
    request: Request,
    response: Response,
    payload: dict = Body(...),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="仅接受 application/json")
    allowed, reason = _provider_config_submission_security(request)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    if not _same_origin_request(request):
        raise HTTPException(status_code=403, detail="拒绝跨站提交 Provider 配置")
    try:
        save_provider_connection_config(payload)
    except ProviderAdminConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **provider_admin_status(
            secure_submission_allowed=allowed,
            secure_submission_reason=reason,
        ),
        "saved": True,
    }


@app.get("/ranking/policy")
def ranking_policy() -> dict:
    return production_ranking_contract()


@app.get("/model-scheduler/status")
def model_scheduler_runtime_status() -> dict:
    return scheduler_status()


@app.get("/model-scheduler/resources")
def model_scheduler_resource_status() -> dict:
    return scheduler_resources()


@app.get("/model-jobs/{job_id}")
def model_job_status(job_id: str) -> dict:
    try:
        return ModelJobRepository().get(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=f"model job not found: {job_id}") from exc


@app.get("/model-jobs/{job_id}/events")
def model_job_events(job_id: str, after: int = 0, limit: int = 200) -> dict:
    try:
        events = ModelJobRepository().events(job_id, after=after, limit=limit)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=f"model job not found: {job_id}") from exc
    return {"job_id": job_id, "events": events}


@app.post("/model-jobs/{job_id}/cancel")
def cancel_model_job(job_id: str) -> dict:
    try:
        return ModelJobRepository().cancel(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=f"model job not found: {job_id}") from exc


@app.post("/model-jobs/{job_id}/retry")
def retry_model_job(job_id: str) -> dict:
    try:
        outcome = ModelJobRepository().retry(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=f"model job not found: {job_id}") from exc
    except InvalidJobTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**outcome.job, "deduplicated": outcome.deduplicated, "cache_hit": outcome.cache_hit}


@app.post("/providers/fake-smoke")
def providers_fake_smoke(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return run_fake_provider_smoke(
            text=str(payload.get("text") or "G3 provider contract smoke"),
            repeat=int(payload.get("repeat") or 2),
            batch_id=payload.get("batch_id"),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.get("/precut-batches")
def precut_batches(limit: int = 20) -> dict:
    return list_precut_batches(limit=limit)


@app.get("/precut-batches/{batch_id}")
def precut_batch(batch_id: str) -> dict:
    try:
        return get_precut_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/precut-batches")
async def create_precut_batch_endpoint(
    background_tasks: BackgroundTasks,
    account_id: str = Form("main"),
    batch_title: str = Form(""),
    process: bool = Form(True),
    asr_profile: str = Form("fast"),
    files: list[UploadFile] = File(...),
) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="at least one precut video is required")
    if len(files) > MAX_BATCH_ITEMS:
        raise HTTPException(status_code=400, detail=f"a precut batch supports at most {MAX_BATCH_ITEMS} files")

    source_names: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="dso-precut-") as temp_dir:
            temp_paths: list[Path] = []
            for index, upload in enumerate(files):
                source_name = Path(upload.filename or f"clip-{index + 1}.mp4").name
                suffix = Path(source_name).suffix or ".mp4"
                stored_name = source_name if Path(source_name).suffix else f"{source_name}{suffix}"
                temp_path = Path(temp_dir) / f"{index:03d}-{stored_name}"
                with temp_path.open("wb") as handle:
                    while chunk := await upload.read(1024 * 1024):
                        handle.write(chunk)
                await upload.close()
                temp_paths.append(temp_path)
                source_names.append(source_name)
            result = create_precut_batch(
                temp_paths,
                account_id=account_id,
                title=batch_title,
                source_names=source_names,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    processable = int(result.get("summary", {}).get("item_count") or 0) - int(
        result.get("summary", {}).get("failed_count") or 0
    )
    if process and processable > 0 and result.get("status") != "completed":
        result = queue_precut_batch(result["batch_id"])
        background_tasks.add_task(
            process_precut_batch,
            result["batch_id"],
            force=False,
            asr_profile=asr_profile or "fast",
        )
    return result


@app.post("/precut-batches/{batch_id}/process")
def process_precut_batch_endpoint(
    batch_id: str,
    background_tasks: BackgroundTasks,
    payload: dict = Body(default_factory=dict),
) -> dict:
    try:
        force = bool(payload.get("force", False))
        asr_profile = str(payload.get("asr_profile") or "fast")
        if bool(payload.get("wait", False)):
            return process_precut_batch(batch_id, force=force, asr_profile=asr_profile)
        result = queue_precut_batch(batch_id)
        background_tasks.add_task(
            process_precut_batch,
            batch_id,
            force=force,
            asr_profile=asr_profile,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/videos/{video_id}/extract")
def extract(video_id: str, response: Response, payload: dict = Body(default_factory=dict)) -> dict:
    if model_scheduler_enabled():
        scheduled = submit_qwen3_asr_job(video_id, force=bool(payload.get("force", False)), role="primary")
        audio = extract_audio_features(video_id)
        response.status_code = 200 if scheduled.get("status") in {"cached", "empty"} else 202
        return {
            **scheduled,
            "audio_peaks": len(audio["peaks"]),
            "asr_selected_backend": "qwen3_asr_scheduled",
            "asr_fallback_used": False,
        }
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
    routing = (transcript.get("metadata") or {}).get("routing") or {}
    return {
        "transcript_source": transcript["source"],
        "segments": len(transcript["segments"]),
        "audio_peaks": len(audio["peaks"]),
        "asr_primary": routing.get("primary") or {},
        "asr_selected_backend": routing.get("selected_backend") or (transcript.get("metadata") or {}).get("backend") or "",
        "asr_fallback_used": bool(routing.get("fallback_used", False)),
        "asr_shadow": routing.get("shadow") or {},
    }


@app.get("/videos/{video_id}/asr/shadow")
def get_qwen3_asr_shadow(video_id: str) -> dict:
    try:
        return qwen3_asr_shadow_status(video_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/videos/{video_id}/asr/shadow")
def post_qwen3_asr_shadow(video_id: str, response: Response, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        if model_scheduler_enabled():
            scheduled = submit_qwen3_asr_job(video_id, force=bool(payload.get("force", False)), role="shadow")
            response.status_code = 200 if scheduled.get("status") in {"cached", "empty"} else 202
            return scheduled
        return run_qwen3_asr_shadow(video_id, force=bool(payload.get("force", False)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/videos/{video_id}/segments")
def segments(video_id: str, top_k: int = 30) -> dict:
    try:
        rows = generate_segments(video_id, top_k=top_k)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@app.post("/videos/{video_id}/hybrid-slice")
def hybrid_slice(video_id: str, response: Response, payload: dict = Body(default_factory=dict)) -> dict:
    if model_scheduler_enabled():
        candidate_limit = int(payload.get("candidate_limit") or 3)
        top_k = int(payload.get("top_k") or 10)
        recall_count = max(30, candidate_limit * 4, top_k * 3)
        segments = generate_segments(video_id, top_k=recall_count)
        scores = score_video(video_id)
        scheduled = submit_omni_rerank_job(
            video_id,
            candidate_limit=candidate_limit,
            max_clip_seconds=float(payload.get("max_clip_seconds") or 6.0),
            omni_weight=float(payload.get("omni_weight") or 0.15),
            load_model=bool(payload.get("load_model", False)),
            force=bool(payload.get("force", False)),
        )
        response.status_code = 200 if scheduled.get("status") in {"cached", "empty"} else 202
        result = {
            "contract_version": HYBRID_SLICE_PIPELINE_VERSION,
            "status": scheduled.get("status") or "accepted",
            "video_id": video_id,
            "pipeline": {
                "recall": "timeline_signal_segmenter",
                "pre_rank": "current_rules",
                "rerank": "model_scheduler.v1/qwen_omni_multi_window_research",
                "fallback": "current_rules",
            },
            "counts": {
                "recalled": len(segments),
                "scored": len(scores),
                "preselected": int(((scheduled.get("model_job") or {}).get("progress") or {}).get("total_items") or 0),
                "omni_applied": int((((scheduled.get("model_job") or {}).get("result_summary") or {}).get("omni_applied_count") or 0)),
            },
            "baseline": scheduled.get("baseline") or {},
            "model_job": scheduled.get("model_job"),
            "production_weight": False,
            "research_only": True,
        }
        record_artifact(
            video_id,
            step="hybrid_ranking",
            artifact_type="model_job",
            version=HYBRID_SLICE_PIPELINE_VERSION,
            summary={"status": result["status"], **result["counts"]},
        )
        return result
    try:
        result = run_hybrid_slice_pipeline(
            video_id,
            top_k=int(payload.get("top_k") or 10),
            candidate_limit=int(payload.get("candidate_limit") or 3),
            max_clip_seconds=float(payload.get("max_clip_seconds") or 6.0),
            omni_weight=float(payload.get("omni_weight") or 0.15),
            load_model=bool(payload.get("load_model", False)),
            force=bool(payload.get("force", False)),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_artifact(
        video_id,
        step="hybrid_ranking",
        artifact_type="omni_multi_window_rerank",
        version=HYBRID_SLICE_PIPELINE_VERSION,
        summary={
            "status": result.get("status") or "fallback",
            **(result.get("counts") or {}),
        },
    )
    return result


@app.post("/videos/{video_id}/omni-rerank")
def omni_rerank(video_id: str, response: Response, payload: dict = Body(default_factory=dict)) -> dict:
    if model_scheduler_enabled():
        mode = str(payload.get("mode") or "async").strip().lower()
        if mode not in {"async", "wait"}:
            raise HTTPException(status_code=400, detail="mode must be async or wait when model scheduler is enabled")
        result = submit_omni_rerank_job(
            video_id,
            candidate_limit=int(payload.get("candidate_limit") or 3),
            max_clip_seconds=float(payload.get("max_clip_seconds") or 6.0),
            omni_weight=float(payload.get("omni_weight") or 0.15),
            load_model=bool(payload.get("load_model", False)),
            force=bool(payload.get("force", False)),
        )
        job = result.get("model_job") if isinstance(result.get("model_job"), dict) else None
        if mode == "wait" and job and job.get("status") not in {"succeeded", "degraded", "failed", "cancelled", "cancelled_partial", "expired"}:
            job = wait_for_model_job(
                str(job["job_id"]),
                timeout_seconds=float(payload.get("wait_timeout_seconds") or 10.0),
            )
            result["model_job"] = job
            if job.get("status") in {"succeeded", "degraded", "failed", "cancelled", "cancelled_partial", "expired"}:
                result["status"] = str(job.get("status"))
        response.status_code = 200 if result.get("status") in {"cached", "empty", "succeeded", "degraded", "failed", "cancelled", "cancelled_partial", "expired"} else 202
        record_artifact(
            video_id,
            step="omni_rerank",
            artifact_type="model_job",
            version=HYBRID_SLICE_PIPELINE_VERSION,
            summary={
                "status": result.get("status") or "accepted",
                "job_id": str((result.get("model_job") or {}).get("job_id") or ""),
                "deduplicated": bool((result.get("model_job") or {}).get("deduplicated")),
            },
        )
        return result
    result = rerank_video_candidates_with_omni(
        video_id,
        candidate_limit=int(payload.get("candidate_limit") or 3),
        max_clip_seconds=float(payload.get("max_clip_seconds") or 6.0),
        omni_weight=float(payload.get("omni_weight") or 0.15),
        load_model=bool(payload.get("load_model", False)),
        force=bool(payload.get("force", False)),
    )
    record_artifact(
        video_id,
        step="omni_rerank",
        artifact_type="omni_multi_window_rerank",
        version=str(result.get("contract_version") or HYBRID_SLICE_PIPELINE_VERSION),
        summary={
            "status": result.get("status") or "fallback",
            "preselected": result.get("preselected_count") or 0,
            "omni_applied": result.get("omni_applied_count") or 0,
        },
    )
    return result


@app.get("/videos/{video_id}/suggestions")
def get_suggestions(video_id: str, top_k: int = 10, ranking_scope: str = "production") -> dict:
    try:
        rows = [
            _attach_segment_variants(row)
            for row in suggestions(video_id, top_k=top_k, ranking_scope=ranking_scope)
        ]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ranking_policy": production_ranking_contract(), "ranking_scope": ranking_scope, "suggestions": rows}


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
        history = calibrate_segment_history(segment_id, account_id=account_id, limit=limit)
        embedding = qwen_embedding_evidence_for_segment(segment_id, account_id=account_id, limit=limit, modality="all")
        return {**history, "embedding_evidence": embedding}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/segments/{segment_id}/qwen-omni/analyze")
def segment_qwen_omni_analyze(segment_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return analyze_candidate_with_qwen_omni(
            segment_id,
            account_id=payload.get("account_id"),
            max_clip_seconds=float(payload.get("max_clip_seconds") or 15.0),
            load_model=bool(payload.get("load_model", False)),
        )
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
        strategy=payload.get("strategy") or "research_ranker_v2_4",
        holdout_policy=payload.get("holdout_policy") or "time",
        label_version=payload.get("label_version"),
    )


@app.get("/learning/backtest")
def get_backtests(account_id: str | None = None, limit: int = 10, compact: bool = False) -> dict:
    return list_backtest_reports(account_id=account_id, limit=limit, compact=compact)


@app.get("/learning/benchmark-manifest/{benchmark_id}")
def get_benchmark_manifest(benchmark_id: str, verify: bool = True) -> dict:
    try:
        manifest = load_benchmark_manifest(benchmark_id)
        return {
            "status": "ready",
            "manifest": manifest,
            "verification": verify_benchmark_manifest(benchmark_id) if verify else None,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/benchmark/run")
def post_benchmark_run(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return run_frozen_benchmark(
            str(payload.get("benchmark_id") or "dso-v1-beta-d10-ab-20260715-r1"),
            allow_drift=bool(payload.get("allow_drift", False)),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/learning/omni-calibration/replay")
def post_omni_calibration_replay(payload: dict = Body(default_factory=dict)) -> dict:
    return omni_calibration_replay(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 50),
        k=int(payload.get("k") or 10),
        holdout_policy=payload.get("holdout_policy") or "time",
    )


@app.get("/learning/datasets")
def get_learning_datasets(account_id: str | None = None, compact: bool = False) -> dict:
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
    return _compact_learning_datasets(enriched) if compact else enriched


@app.get("/learning/research/coverage")
def get_research_coverage(account_id: str | None = None, dataset_id: str | None = None) -> dict:
    return research_field_coverage(account_id=account_id, dataset_id=dataset_id)


@app.post("/learning/semantic-features/backfill")
def post_semantic_features_backfill(payload: dict = Body(default_factory=dict)) -> dict:
    return backfill_semantic_features(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 0),
        force=bool(payload.get("force", False)),
    )


@app.get("/learning/semantic-calibration/queue")
def get_semantic_calibration_queue(
    account_id: str | None = None,
    dataset_id: str | None = None,
    limit: int = 50,
    min_priority: float = 0,
    label: str | None = None,
    queue_type: str = "mixed",
    strategy: str = "research_ranker_v2_4",
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


@app.get("/learning/material-gold-set/queue")
def get_material_gold_set_queue(
    account_id: str | None = None,
    dataset_id: str | None = None,
    limit: int = 12,
    include_reviewed: bool = False,
) -> dict:
    return material_gold_set_queue(
        account_id=account_id,
        dataset_id=dataset_id,
        limit=limit,
        include_reviewed=include_reviewed,
    )


@app.get("/learning/material-taxonomy")
def get_material_taxonomy() -> dict:
    return material_taxonomy_contract()


@app.get("/learning/material-confusions/queue")
def get_material_confusion_queue(
    account_id: str | None = None,
    dataset_id: str | None = None,
    confusion_pair: str | None = None,
    limit: int = 80,
    local_media_only: bool = True,
    include_reviewed: bool = False,
) -> dict:
    try:
        return material_confusion_queue(
            account_id=account_id,
            dataset_id=dataset_id,
            confusion_pair=confusion_pair,
            limit=limit,
            local_media_only=local_media_only,
            include_reviewed=include_reviewed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/material-evidence/status")
def get_material_evidence_status(
    account_id: str | None = None,
    dataset_id: str | None = None,
    confusion_pair: str | None = None,
    limit: int = 80,
    include_reviewed: bool = True,
) -> dict:
    try:
        return material_evidence_status(
            account_id=account_id,
            dataset_id=dataset_id,
            confusion_pair=confusion_pair,
            limit=limit,
            include_reviewed=include_reviewed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/material-evidence/extract")
def post_material_evidence_extract(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return run_material_evidence_batch(
            account_id=payload.get("account_id"),
            dataset_id=payload.get("dataset_id"),
            confusion_pair=payload.get("confusion_pair"),
            limit=int(payload.get("limit") or 10),
            window_seconds=float(payload.get("window_seconds") or 8.0),
            run_asr=bool(payload.get("run_asr", True)),
            run_ocr=bool(payload.get("run_ocr", True)),
            run_omni=bool(payload.get("run_omni", True)),
            load_model=bool(payload.get("load_model", False)),
            force=bool(payload.get("force", False)),
            include_reviewed=bool(payload.get("include_reviewed", True)),
            sample_ids=payload.get("sample_ids") if isinstance(payload.get("sample_ids"), list) else None,
            output_path=payload.get("output_path"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/material-resolver/shadow")
def post_material_resolver_shadow(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return run_material_resolver_shadow(
            account_id=payload.get("account_id"),
            dataset_id=payload.get("dataset_id"),
            confusion_pair=payload.get("confusion_pair"),
            limit=int(payload.get("limit") or 80),
            include_reviewed=bool(payload.get("include_reviewed", True)),
            output_path=payload.get("output_path"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/visual-window-scout/status")
def get_visual_window_scout_status(
    account_id: str | None = None,
    dataset_id: str | None = None,
    limit: int = 60,
    summary_only: bool = False,
    build_id: str | None = None,
) -> dict:
    try:
        return visual_window_scout_status(
            account_id=account_id,
            dataset_id=dataset_id,
            limit=limit,
            summary_only=summary_only,
            build_id=build_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/visual-window-scout/build")
def post_visual_window_scout_build(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return build_visual_window_scout(
            account_id=payload.get("account_id"),
            dataset_id=payload.get("dataset_id"),
            sample_ids=payload.get("sample_ids") if isinstance(payload.get("sample_ids"), list) else None,
            limit=int(payload.get("limit") or DEFAULT_D11B_BATCH_SIZE),
            window_seconds=float(payload.get("window_seconds") or 15.0),
            stride_seconds=float(payload.get("stride_seconds") or 5.0),
            max_windows_per_sample=int(payload.get("max_windows_per_sample") or 3),
            force=bool(payload.get("force", False)),
            load_model=bool(payload.get("load_model", False)),
            scan_scenes=bool(payload.get("scan_scenes", True)),
            frame_cache_limit_bytes=int(payload.get("frame_cache_limit_bytes") or 512 * 1024 * 1024),
            batch_mode=str(payload.get("batch_mode") or "next"),
            exclude_reviewed=bool(payload.get("exclude_reviewed", True)),
            resume_pending=bool(payload.get("resume_pending", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/learning/material-window-gold/{sample_id}")
def patch_material_window_gold(sample_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return update_material_window_annotation(sample_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/visual-window-scout/builds/{build_id}")
def get_visual_window_scout_build(build_id: str) -> dict:
    try:
        return load_visual_window_build(build_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/visual-window-scout/builds/{build_id}/manifest")
def get_visual_window_scout_build_manifest(build_id: str) -> dict:
    try:
        return load_visual_window_build_manifest(build_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/visual-window-scout/experiment")
def post_visual_window_scout_experiment(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return run_visual_window_experiment(
            build_id=payload.get("build_id"),
            build_ids=payload.get("build_ids") if isinstance(payload.get("build_ids"), list) else None,
            scope=str(payload.get("scope") or "cumulative"),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/visual-window-scout/frames/{sample_id}/{window_key}/{frame_name}")
def get_visual_window_scout_frame(sample_id: str, window_key: str, frame_name: str) -> FileResponse:
    try:
        path = visual_window_frame_path(sample_id, window_key, frame_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="window frame not found") from exc
    return FileResponse(path)


@app.patch("/learning/material-gold-set/{sample_id}")
def patch_material_gold_set(sample_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return update_material_gold_annotation(sample_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/material-gold-set/{sample_id}/reopen")
def post_reopen_material_gold_set(sample_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return reopen_material_gold_annotation(sample_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/material-gold-set/replay")
def post_material_gold_set_replay(payload: dict = Body(default_factory=dict)) -> dict:
    return run_material_calibration_replay(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        k=int(payload.get("k") or 30),
        holdout_policy=payload.get("holdout_policy") or "time",
    )


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


@app.post("/learning/semantic-feature-experiment/run")
def post_semantic_feature_experiment(payload: dict = Body(default_factory=dict)) -> dict:
    return semantic_feature_experiment(
        account_id=payload.get("account_id"),
        k=int(payload.get("k") or 10),
        holdout_policy=payload.get("holdout_policy") or "time",
        label_version=payload.get("label_version"),
        include_field_masks=bool(payload.get("include_field_masks", True)),
    )


@app.post("/learning/slice-structure/evaluate")
def post_slice_structure_evaluate(payload: dict = Body(default_factory=dict)) -> dict:
    return evaluate_slice_structure(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 0),
        min_confidence=float(payload.get("min_confidence") or 0.0),
    )


@app.post("/learning/multimodal/collection-plan")
def post_multimodal_collection_plan(payload: dict = Body(default_factory=dict)) -> dict:
    return build_multimodal_collection_plan(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or DEFAULT_MULTIMODAL_COLLECTION_TARGET),
        stage=payload.get("stage") or "beta_d1",
        output_path=payload.get("output_path"),
        include_ready=bool(payload.get("include_ready", False)),
    )


@app.post("/learning/multimodal/collect")
def post_multimodal_collect(payload: dict = Body(default_factory=dict)) -> dict:
    return collect_multimodal_assets(
        plan_path=payload.get("plan_path"),
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 30),
        stage=payload.get("stage") or "beta_d1",
        output_root=payload.get("output_root"),
        report_dir=payload.get("report_dir"),
        run_id=payload.get("run_id") or "",
        page_delay_seconds=int(payload.get("page_delay_seconds") or 14),
        extra_wait_seconds=int(payload.get("extra_wait_seconds") or 5),
        extract_audio=bool(payload.get("extract_audio", True)),
        dry_run=bool(payload.get("dry_run", True)),
        max_storage_bytes=resolve_multimodal_storage_limit_bytes(
            max_storage_bytes=payload.get("max_storage_bytes"),
            max_storage_gb=payload.get("max_storage_gb"),
        ),
    )


@app.post("/learning/multimodal-validation/run")
def post_multimodal_validation(payload: dict = Body(default_factory=dict)) -> dict:
    return run_multimodal_validation(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 300),
        k=int(payload.get("k") or 10),
        min_samples=int(payload.get("min_samples") or 100),
        min_asset_coverage=float(payload.get("min_asset_coverage") or 0.7),
    )


@app.post("/learning/multimodal-feature-experiment/run")
def post_multimodal_feature_experiment(payload: dict = Body(default_factory=dict)) -> dict:
    return run_multimodal_feature_experiment(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 300),
        k=int(payload.get("k") or 10),
        min_feature_samples=int(payload.get("min_feature_samples") or 60),
        audio_window_seconds=float(payload.get("audio_window_seconds") or 10.0),
        force=bool(payload.get("force", False)),
    )


@app.post("/learning/qwen-embeddings/build")
def post_qwen_embeddings_build(response: Response, payload: dict = Body(default_factory=dict)) -> dict:
    if model_scheduler_enabled():
        scheduled = submit_embedding_build_job(
            account_id=payload.get("account_id"),
            dataset_id=payload.get("dataset_id"),
            entity_type=payload.get("entity_type") or "historical_sample",
            entity_ids=payload.get("entity_ids") if isinstance(payload.get("entity_ids"), list) else None,
            modality=payload.get("modality") or "text",
            limit=int(payload.get("limit") or 300),
            force=bool(payload.get("force", False)),
        )
        response.status_code = 200 if scheduled.get("status") in {"cached", "empty"} else 202
        return scheduled
    return build_qwen_embedding_index(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        entity_type=payload.get("entity_type") or "historical_sample",
        entity_ids=payload.get("entity_ids") if isinstance(payload.get("entity_ids"), list) else None,
        modality=payload.get("modality") or "text",
        limit=int(payload.get("limit") or 300),
        force=bool(payload.get("force", False)),
    )


@app.post("/learning/qwen-embedding-evidence/run")
def post_qwen_embedding_evidence(payload: dict = Body(default_factory=dict)) -> dict:
    return run_qwen_embedding_evidence(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 300),
        k=int(payload.get("k") or 10),
        modality=payload.get("modality") or "all",
    )


@app.get("/learning/multimodal-vector-experiment/status")
def get_multimodal_vector_experiment_status(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    reviewer_id: str = "local",
) -> dict:
    try:
        return multimodal_vector_experiment_status(benchmark_id, reviewer_id=reviewer_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/freeze")
def post_multimodal_vector_experiment_freeze(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return freeze_multimodal_vector_experiment(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
            pair_count=int(payload.get("pair_count") or 60),
            reference_per_label=int(payload.get("reference_per_label") or 60),
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/multimodal-vector-experiment/verify")
def get_multimodal_vector_experiment_verify(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
    deep: bool = False,
) -> dict:
    try:
        return verify_multimodal_vector_manifest(benchmark_id, deep=deep)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/embeddings")
def post_multimodal_vector_experiment_embeddings(response: Response, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        request = multimodal_vector_embedding_request(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID
        )
        if not model_scheduler_enabled():
            raise HTTPException(
                status_code=409,
                detail="multimodal vector embedding build requires the persistent model scheduler",
            )
        scheduled = submit_embedding_build_job(
            entity_type="historical_sample",
            entity_ids=request["entity_ids"],
            modality="all",
            limit=len(request["entity_ids"]),
            force=bool(payload.get("force", False)),
        )
        response.status_code = 200 if scheduled.get("status") in {"cached", "empty"} else 202
        return {
            "benchmark_id": request["benchmark_id"],
            "manifest_sha256": request["manifest_sha256"],
            "target_sample_count": len(request["entity_ids"]),
            **scheduled,
        }
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/compare")
def post_multimodal_vector_experiment_compare(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return run_multimodal_vector_comparison(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
            reviewer_id=payload.get("reviewer_id") or "local",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/multimodal-vector-experiment/cloud/status")
def get_multimodal_vector_cloud_status(
    benchmark_id: str = DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
) -> dict:
    try:
        return bailian_vector_chain_status(benchmark_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/cloud/run")
def post_multimodal_vector_cloud_run(payload: dict = Body(default_factory=dict)) -> dict:
    stage = str(payload.get("stage") or "smoke").strip().lower()
    limit = int(payload.get("limit") if payload.get("limit") is not None else 10)
    if stage == "full":
        raise HTTPException(
            status_code=409,
            detail="full cloud chain is a resumable CLI operation; use bounded web batches to keep health checks responsive",
        )
    maximum = 10 if stage == "smoke" else 40
    if not 1 <= limit <= maximum:
        raise HTTPException(
            status_code=400,
            detail=f"web {stage} batches require limit between 1 and {maximum}",
        )
    try:
        return run_bailian_vector_chain(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
            stage=stage,
            limit=limit,
            top_n=int(payload.get("top_n") or 20),
            judge_limit=int(payload.get("judge_limit") or 20),
            force=bool(payload.get("force", False)),
            batch_id=payload.get("batch_id"),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/cloud/ablation")
def post_multimodal_vector_cloud_ablation(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return run_bailian_cached_ablation(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/cloud/holdout/{action}")
def post_multimodal_vector_cloud_holdout(
    action: str, payload: dict = Body(default_factory=dict)
) -> dict:
    benchmark_id = payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID
    try:
        if action == "freeze":
            return freeze_bailian_holdout_validation(benchmark_id)
        if action == "predict":
            return run_bailian_holdout_prediction(benchmark_id)
        if action == "evaluate":
            return evaluate_bailian_holdout_validation(benchmark_id)
        raise HTTPException(status_code=404, detail=f"unsupported holdout action: {action}")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/cloud/holdout-attribution")
def post_multimodal_vector_cloud_holdout_attribution(
    payload: dict = Body(default_factory=dict),
) -> dict:
    try:
        return run_bailian_holdout_failure_attribution(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/cloud/evidence-quality/rebuild")
def post_multimodal_vector_cloud_evidence_quality(
    payload: dict = Body(default_factory=dict),
) -> dict:
    try:
        return run_bailian_evidence_quality_reconstruction(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
            scope=payload.get("scope") or "holdout",
            limit=int(payload.get("limit") or 40),
            force=bool(payload.get("force", False)),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/learning/multimodal-vector-experiment/reviews/{task_id}")
def post_multimodal_vector_experiment_review(task_id: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return save_multimodal_vector_review(
            payload.get("benchmark_id") or DEFAULT_MULTIMODAL_VECTOR_BENCHMARK_ID,
            task_id,
            payload,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/multimodal-vector-experiment/media/{benchmark_id}/{task_id}/{side}")
def get_multimodal_vector_experiment_media(benchmark_id: str, task_id: str, side: str) -> FileResponse:
    try:
        path = multimodal_vector_media_path(benchmark_id, task_id, side)
        return FileResponse(path, filename=path.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/qwen-omni/status")
def get_qwen_omni_status() -> dict:
    return qwen_omni_status()


@app.post("/learning/qwen-omni/shadow-run")
def post_qwen_omni_shadow_run(payload: dict = Body(default_factory=dict)) -> dict:
    return run_qwen_omni_shadow(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 20),
        max_clip_seconds=float(payload.get("max_clip_seconds") or 15.0),
        load_model=bool(payload.get("load_model", False)),
        use_media=bool(payload.get("use_media", False)),
        allow_windowed_clips=bool(payload.get("allow_windowed_clips", False)),
        visual_ready_only=bool(payload.get("visual_ready_only", False)),
    )


@app.post("/learning/qwen-omni/media-batch")
def post_qwen_omni_media_batch(payload: dict = Body(default_factory=dict)) -> dict:
    return run_qwen_omni_media_batch(
        account_id=payload.get("account_id"),
        dataset_id=payload.get("dataset_id"),
        limit=int(payload.get("limit") or 20),
        max_clip_seconds=float(payload.get("max_clip_seconds") or 8.0),
        load_model=bool(payload.get("load_model", False)),
        force=bool(payload.get("force", False)),
        output_path=payload.get("output_path"),
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


def _compact_learning_datasets(payload: dict) -> dict:
    result = dict(payload)
    result["datasets"] = [_compact_learning_dataset(item) for item in payload.get("datasets") or []]
    result["historical_summary"] = _compact_history_summary(payload.get("historical_summary") or {})
    result.pop("duplicate_item_groups", None)
    return result


def _compact_learning_dataset(dataset: dict) -> dict:
    item = dict(dataset)
    item.pop("historical_summary", None)
    item.pop("duplicate_item_groups", None)
    item.pop("source_paths", None)
    lineage = dict(item.get("data_lineage") or {})
    lineage.pop("source_paths", None)
    if lineage:
        item["data_lineage"] = lineage
    return item


def _compact_history_summary(history: dict) -> dict:
    item = dict(history)
    item.pop("duplicate_item_groups", None)
    item["datasets"] = [_compact_learning_dataset(dataset) for dataset in history.get("datasets") or []]
    lineage = dict(item.get("data_lineage") or {})
    lineage.pop("source_paths", None)
    if lineage:
        item["data_lineage"] = lineage
    return item


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
    return douyin_history_baselines(
        account_id=account_id,
        dataset_id=dataset_id,
        min_count=min_count,
        limit=limit,
        include_groups=False,
    )


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
    row = attach_ranking_policy(_parse_segment_json_fields(dict(row)), ranking_scope=row.get("ranking_scope"))
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
    for source, target in [
        ("omni_analysis_json", "omni_analysis"),
        ("generation_signals_json", "generation_signals"),
    ]:
        value = row.pop(source, None)
        if isinstance(value, str):
            try:
                row[target] = json.loads(value)
            except Exception:
                row[target] = {}
        elif value is not None:
            row[target] = value
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
