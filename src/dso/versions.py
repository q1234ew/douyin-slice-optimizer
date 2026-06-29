from __future__ import annotations


QUALITY_INSIGHTS_VERSION = "quality_insights.v1"
QUALITY_GATE_VERSION = "quality_gate.v1"
METRICS_IMPORT_VERSION = "metrics_import.v1"
FEEDBACK_INSIGHTS_VERSION = "feedback_insights.v1"
FEEDBACK_STATE_VERSION = "feedback_state.v1"
REVIEW_CONTRACT_VERSION = "review_contract.v1"
CHANGE_LOG_VERSION = "change_log.v1"
ARTIFACT_MANIFEST_VERSION = "artifact_manifest.v1"
ASR_PROFILE_PLAN_VERSION = "asr_profile_plan.v1"
ASR_MODEL_ROUTING_VERSION = "asr_model_routing.v1"
ASR_VERIFY_VERSION = "asr_verify.v1"
VARIANT_EXPERIMENT_VERSION = "variant_experiment.v1"
PLATFORM_SYNC_VERSION = "platform_sync.v1"
MEMORY_BANK_VERSION = "memory_bank.v1"
HISTORY_CALIBRATION_VERSION = "history_calibration.v1"
INTEREST_CLOCK_VERSION = "interest_clock.v1"
BACKTEST_VERSION = "backtest.v2.2"
PROTOTYPE_BANK_VERSION = "prototype_bank.v1"
HISTORICAL_CAPTURE_VERSION = "historical_capture.v1"
DOUYIN_HISTORY_VERSION = "douyin_history.v1"
SEMANTIC_FEATURE_VERSION = "semantic_features.research_v2"
RESEARCH_LABEL_VERSION = "research_labels.visible_engagement_v2"
RESEARCH_RANKER_VERSION = "historical_research_ranker.v2.2"
SEGMENTER_VERSION = "music_variety_segmenter.v1"
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
        "asr_profile_plan": ASR_PROFILE_PLAN_VERSION,
        "asr_model_routing": ASR_MODEL_ROUTING_VERSION,
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
        "research_labels": RESEARCH_LABEL_VERSION,
        "research_ranker": RESEARCH_RANKER_VERSION,
        "segmenter": SEGMENTER_VERSION,
        "scorer": SCORER_VERSION,
    }
