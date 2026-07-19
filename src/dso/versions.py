from __future__ import annotations


QUALITY_INSIGHTS_VERSION = "quality_insights.v1"
QUALITY_GATE_VERSION = "quality_gate.v1"
METRICS_IMPORT_VERSION = "metrics_import.v1"
FEEDBACK_INSIGHTS_VERSION = "feedback_insights.v1"
FEEDBACK_STATE_VERSION = "feedback_state.v1"
REVIEW_CONTRACT_VERSION = "review_contract.v1"
CHANGE_LOG_VERSION = "change_log.v1"
ARTIFACT_MANIFEST_VERSION = "artifact_manifest.v1"
VIDEO_DOWNLOAD_CONTRACT_VERSION = "video_download.v1"
STANDARD_CANDIDATE_VERSION = "standard_candidate.v1"
PRECUT_BATCH_VERSION = "precut_batch.v1"
PRODUCTION_RANKING_POLICY_VERSION = "production_ranking_policy.v1"
MODEL_SCHEDULER_VERSION = "model_scheduler.v1"
ASR_PROFILE_PLAN_VERSION = "asr_profile_plan.v1"
ASR_MODEL_ROUTING_VERSION = "asr_model_routing.v1"
QWEN3_ASR_SHADOW_VERSION = "qwen3_asr_shadow.v1"
QWEN3_ASR_VERSION = "qwen3_asr.program_v1"
ASR_VERIFY_VERSION = "asr_verify.v1"
VARIANT_EXPERIMENT_VERSION = "variant_experiment.v1"
PLATFORM_SYNC_VERSION = "platform_sync.v1"
MEMORY_BANK_VERSION = "memory_bank.v1"
HISTORY_CALIBRATION_VERSION = "history_calibration.v1"
INTEREST_CLOCK_VERSION = "interest_clock.v1"
BACKTEST_VERSION = "backtest.v2.4"
PROTOTYPE_BANK_VERSION = "prototype_bank.v1"
HISTORICAL_CAPTURE_VERSION = "historical_capture.v1"
DOUYIN_HISTORY_VERSION = "douyin_history.v1"
SEMANTIC_FEATURE_VERSION = "semantic_features.research_v3"
SLICE_STRUCTURE_EVALUATOR_VERSION = "slice_structure_evaluator.v1"
MULTIMODAL_VALIDATION_VERSION = "multimodal_validation.v1"
MULTIMODAL_FEATURE_VERSION = "multimodal_features.lightweight_v1"
QWEN_EMBEDDING_VERSION = "qwen3_vl_embedding.evidence_v1"
MULTIMODAL_VECTOR_VALUE_VERSION = "multimodal_vector_value.v1"
QWEN_OMNI_VERSION = "qwen2_5_omni_7b_gptq_int4.shadow_v1"
OMNI_SLICE_RANKER_VERSION = "omni_slice_ranker.hybrid_v1"
HYBRID_SLICE_PIPELINE_VERSION = "hybrid_slice_pipeline.v1"
MATERIAL_EVIDENCE_VERSION = "material_evidence.d10b.v1"
MATERIAL_RESOLVER_VERSION = "material_confusion_resolver.shadow_v1"
BENCHMARK_MANIFEST_VERSION = "benchmark_manifest.v1"
RESEARCH_LABEL_VERSION = "research_labels.visible_engagement_v2"
RESEARCH_RANKER_VERSION = "historical_research_ranker.v2.4"
SEGMENTER_VERSION = "music_variety_segmenter.v2_timeline"
SCORER_VERSION = "music_variety_rules.v1"


def component_versions() -> dict[str, str]:
    return {
        "quality_insights": QUALITY_INSIGHTS_VERSION,
        "quality_gate": QUALITY_GATE_VERSION,
        "metrics_import": METRICS_IMPORT_VERSION,
        "feedback_insights": FEEDBACK_INSIGHTS_VERSION,
        "feedback_state": FEEDBACK_STATE_VERSION,
        "review_contract": REVIEW_CONTRACT_VERSION,
        "change_log": CHANGE_LOG_VERSION,
        "artifact_manifest": ARTIFACT_MANIFEST_VERSION,
        "video_download": VIDEO_DOWNLOAD_CONTRACT_VERSION,
        "standard_candidate": STANDARD_CANDIDATE_VERSION,
        "precut_batch": PRECUT_BATCH_VERSION,
        "production_ranking_policy": PRODUCTION_RANKING_POLICY_VERSION,
        "model_scheduler": MODEL_SCHEDULER_VERSION,
        "asr_profile_plan": ASR_PROFILE_PLAN_VERSION,
        "asr_model_routing": ASR_MODEL_ROUTING_VERSION,
        "qwen3_asr_shadow": QWEN3_ASR_SHADOW_VERSION,
        "qwen3_asr": QWEN3_ASR_VERSION,
        "asr_verify": ASR_VERIFY_VERSION,
        "variant_experiment": VARIANT_EXPERIMENT_VERSION,
        "platform_sync": PLATFORM_SYNC_VERSION,
        "memory_bank": MEMORY_BANK_VERSION,
        "history_calibration": HISTORY_CALIBRATION_VERSION,
        "interest_clock": INTEREST_CLOCK_VERSION,
        "backtest": BACKTEST_VERSION,
        "prototype_bank": PROTOTYPE_BANK_VERSION,
        "historical_capture": HISTORICAL_CAPTURE_VERSION,
        "douyin_history": DOUYIN_HISTORY_VERSION,
        "semantic_features": SEMANTIC_FEATURE_VERSION,
        "slice_structure_evaluator": SLICE_STRUCTURE_EVALUATOR_VERSION,
        "multimodal_validation": MULTIMODAL_VALIDATION_VERSION,
        "multimodal_features": MULTIMODAL_FEATURE_VERSION,
        "qwen_embeddings": QWEN_EMBEDDING_VERSION,
        "multimodal_vector_value": MULTIMODAL_VECTOR_VALUE_VERSION,
        "qwen_omni": QWEN_OMNI_VERSION,
        "omni_slice_ranker": OMNI_SLICE_RANKER_VERSION,
        "hybrid_slice_pipeline": HYBRID_SLICE_PIPELINE_VERSION,
        "material_evidence": MATERIAL_EVIDENCE_VERSION,
        "material_resolver": MATERIAL_RESOLVER_VERSION,
        "benchmark_manifest": BENCHMARK_MANIFEST_VERSION,
        "research_labels": RESEARCH_LABEL_VERSION,
        "research_ranker": RESEARCH_RANKER_VERSION,
        "segmenter": SEGMENTER_VERSION,
        "scorer": SCORER_VERSION,
    }
