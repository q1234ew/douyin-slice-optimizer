export type ViewName = "workbench" | "candidates" | "simulation" | "feedback";
export type FeedbackSectionName = "overview" | "samples" | "calibration" | "platform" | "runtime";
export type InspectorSectionName = "decision" | "asr" | "history" | "packaging";
export type ModuleStatusKey =
  | "videos"
  | "quality"
  | "suggestions"
  | "history"
  | "feedback"
  | "simulation"
  | "douyin"
  | "runtime";

export interface ModuleStatus {
  loading: boolean;
  error: string;
  lastUpdated?: string;
}

export interface DashboardStats {
  videos: number;
  segments: number;
  exports: number;
  training_samples: number;
}

export interface VideoRow {
  id: string;
  account_id?: string;
  title?: string;
  duration_seconds?: number;
  width?: number;
  height?: number;
  status?: string;
  [key: string]: unknown;
}

export type MaterialEntryMode = "precut" | "program";

export interface PrecutBatchSummary {
  id: string;
  account_id?: string;
  title?: string;
  status?: string;
  item_count?: number;
  created_count?: number;
  reused_count?: number;
  failed_count?: number;
  processed_count?: number;
  contract_version?: string;
  error_summary?: string;
  created_at?: string;
  updated_at?: string;
  progress?: {
    settled_count?: number;
    total_count?: number;
    ratio?: number;
  };
}

export interface PrecutBatchItem extends CandidateRow {
  batch_id?: string;
  candidate_segment_id?: string;
  status?: string;
  batch_rank?: number;
  position?: number;
  source_name?: string;
  title?: string;
  content_hash?: string;
  size_bytes?: number;
  ingest_disposition?: string;
  source_duration_seconds?: number;
  input_mode?: string;
  candidate_status?: string;
  boundary_locked?: number | boolean;
  boundary_invariant?: boolean;
  candidate_origin?: string;
  candidate_contract_version?: string;
  effective_score?: number | null;
  processing_notes?: Array<Record<string, unknown>>;
  error?: string;
}

export interface PrecutBatchDetail {
  contract_version?: string;
  candidate_contract_version?: string;
  batch_id: string;
  status?: string;
  batch: PrecutBatchSummary;
  items?: PrecutBatchItem[];
  rankings?: PrecutBatchItem[];
  summary?: {
    item_count?: number;
    created_count?: number;
    reused_count?: number;
    failed_count?: number;
    processed_count?: number;
    ranked_count?: number;
    boundary_locked_count?: number;
  };
}

export interface PrecutBatchList {
  contract_version?: string;
  count?: number;
  batches?: PrecutBatchSummary[];
}

export interface VariantRow {
  id?: string;
  status?: string;
  title?: string;
  hypothesis?: string;
  changed_variable?: string;
  publish_window?: string;
  export_path?: string;
  export_url?: string;
  cover_path?: string;
  cover_url?: string;
  [key: string]: unknown;
}

export interface CandidateRow {
  id: string;
  source_video_id?: string;
  start_time?: number;
  end_time?: number;
  duration_seconds?: number;
  transcript?: string;
  summary?: string;
  title_suggestions?: string[];
  cover_time?: number;
  final_score?: number;
  ranker_score?: number;
  ranker_version?: string;
  effective_score?: number | null;
  production_score?: number | null;
  production_ranking_strategy?: string;
  production_ranking_version?: string;
  production_score_field?: string;
  production_status?: string;
  research_score?: number | null;
  research_ranking_strategy?: string;
  research_ranker_version?: string;
  research_promotion_status?: string;
  research_score_delta_vs_production?: number | null;
  ranking_scope?: "production" | "research" | string;
  ranking_policy_reason?: string;
  hybrid_score?: number;
  hybrid_rank?: number;
  hybrid_ranker_version?: string;
  omni_score?: number;
  omni_confidence?: number;
  omni_status?: string;
  omni_analysis?: Record<string, unknown>;
  generation_signals?: Record<string, unknown>;
  boundary_strategy?: string;
  boundary_confidence?: number;
  learning_signals?: SegmentHistoryResult;
  score_explanation?: string;
  cover_suggestion?: string;
  risk_notes?: string[];
  music_slice_type?: string;
  emotion_type?: string;
  short_video_structure?: string;
  program_context?: string;
  comment_trigger?: string;
  review_status?: string;
  review_status_label?: string;
  review_status_reason?: string;
  latest_export?: VariantRow | null;
  variants?: VariantRow[];
  platform_mappings?: PlatformMapping[];
  latest_asr_verification?: AsrVerification | null;
  feedback_summary?: FeedbackSummary;
  review_events?: ReviewEvent[];
  [key: string]: unknown;
}

export interface PreviewState {
  segmentId: string;
  title: string;
  timeRange: string;
  duration: string;
  score: string;
  type: string;
  url: string;
  coverUrl: string;
  exportPath: string;
}

export interface QualityIssue {
  severity?: string;
  label?: string;
  evidence?: string;
  recommendation?: string;
  [key: string]: unknown;
}

export interface QualityWatchItem {
  segment_id?: string;
  flags?: string[];
  time_range?: {
    start_time?: number;
    end_time?: number;
  };
}

export interface QualityDecision {
  segment_id?: string;
  severity?: string;
  label?: string;
  title?: string;
  reason?: string;
  action?: string;
}

export interface AsrRoutingCandidate {
  segment_id?: string;
  decision?: string;
  recommended_profile?: string;
  recommended_model?: string;
  reasons?: string[];
  reason_keys?: string[];
  preserve_quality_result?: boolean;
  evidence?: string;
  time_range?: {
    start_time?: number;
    end_time?: number;
    duration_seconds?: number;
  };
}

export interface AsrRoutingReport {
  contract_version?: string;
  next_action?: string;
  verify_count?: number;
  english_preserve_count?: number;
  video?: {
    decision?: string;
    recommended_profile?: string;
    recommended_model?: string;
    reasons?: string[];
    [key: string]: unknown;
  };
  candidates?: AsrRoutingCandidate[];
  verify_queue?: AsrRoutingCandidate[];
  english_preserve_queue?: AsrRoutingCandidate[];
  [key: string]: unknown;
}

export interface QualityReport {
  health?: {
    score?: number;
    level?: string;
    top_issue?: string;
  };
  gate?: {
    status?: string;
    severity?: string;
    label?: string;
    summary?: string;
    primary_action?: ActionHint;
    reasons?: Array<{ label?: string; [key: string]: unknown }>;
    signals?: { rights_mode?: string; [key: string]: unknown };
  };
  transcript?: {
    source?: string;
    backend?: string;
    whisper_cpp_vad_enabled?: boolean;
    segment_count?: number;
    repetition_noise_count?: number;
    ad_read_count?: number;
  };
  queue?: {
    closed_loop_count?: number;
    top_k?: number;
  };
  simulation?: {
    decisions?: QualityDecision[];
    actions?: string[];
  };
  asr_routing?: AsrRoutingReport;
  issues?: QualityIssue[];
  actions?: string[];
  watchlist?: QualityWatchItem[];
  video_title?: string;
  [key: string]: unknown;
}

export interface ActionHint {
  kind?: string;
  label?: string;
  description?: string;
}

export interface SimulationRow {
  segment_id?: string;
  simulation_rank?: number;
  title?: string;
  predicted_stage?: string;
  music_slice_type?: string;
  simulated_score?: number;
  final_score?: number;
  bottleneck?: { label?: string };
  time_range?: {
    start_time?: number;
    end_time?: number;
    duration_seconds?: number;
  };
  audience_clusters?: string[];
  actions?: string[];
  stage_flow?: Array<{
    label?: string;
    score?: number;
    status?: string;
  }>;
  [key: string]: unknown;
}

export interface SimulationSummary {
  avg_score?: number;
  high_potential_count?: number;
  top_bottleneck?: string;
  top_stage?: string;
}

export interface TrainingSample {
  dataset_id?: string;
  dataset?: LearningDataset;
  music_slice_type?: string;
  label_window?: string;
  train_split?: string;
  normalized_reward?: number;
  reward_proxy?: number;
  [key: string]: unknown;
}

export interface BaselineRow {
  metric_name?: string;
  content_type?: string;
  duration_bucket?: string;
  publish_hour?: number;
  sample_count?: number;
  p75_value?: number;
  p90_value?: number;
  [key: string]: unknown;
}

export interface AccountInsights {
  sample_count?: number;
  top_signals?: Record<string, {
    name?: string;
    count?: number;
    reward_proxy?: number;
    play_conversion_rate?: number;
  }>;
  [key: string]: unknown;
}

export interface PlatformMapping {
  candidate_segment_id?: string;
  platform_item_id?: string;
  sync_status?: string;
  last_metrics_at?: string;
  last_synced_at?: string;
  [key: string]: unknown;
}

export interface DouyinSummary {
  mappings?: PlatformMapping[];
  runs?: Array<{
    id?: string;
    source?: string;
    imported_metrics?: number;
    linked_rows?: number;
  }>;
  metrics?: {
    count?: number;
    unlinked?: number;
  };
  [key: string]: unknown;
}

export interface DouyinOAuthStatus {
  account?: {
    auth_status?: string;
    [key: string]: unknown;
  };
  token?: {
    stored?: boolean;
    open_id?: string;
    access_token_expires_at?: string;
    [key: string]: unknown;
  };
  config?: {
    ready_for_qr_login?: boolean;
    missing?: string[];
    [key: string]: unknown;
  };
  session?: Record<string, unknown>;
  state?: string;
  auth_url?: string;
  [key: string]: unknown;
}

export interface MemoryBuildResult {
  contract_version?: string;
  status?: string;
  account_id?: string;
  model_name?: string;
  vector_dim?: number;
  created?: number;
  reused?: number;
  total_candidates?: number;
  [key: string]: unknown;
}

export interface HistoricalSampleImportResult {
  contract_version?: string;
  status?: string;
  account_id?: string;
  dataset_id?: string;
  dataset_name?: string;
  source_raw_count?: number;
  source_raw_rows?: number;
  source_unique_count?: number;
  source_unique_rows?: number;
  raw_rows?: number;
  valid_rows?: number;
  unique_count?: number;
  inserted?: number;
  updated?: number;
  skipped?: number;
  sample_count?: number;
  historical_count?: number;
  training_ready_count?: number;
  trainable_count?: number;
  datasets?: HistoricalSampleImportResult[];
  summary?: HistoricalSampleSummary;
  [key: string]: unknown;
}

export interface HistoricalSampleSummary {
  contract_version?: string;
  status?: string;
  account_id?: string;
  source_raw_count?: number;
  source_raw_rows?: number;
  source_unique_count?: number;
  source_unique_rows?: number;
  raw_rows?: number;
  valid_rows?: number;
  unique_count?: number;
  count?: number;
  sample_count?: number;
  historical_count?: number;
  training_ready_count?: number;
  trainable_count?: number;
  duplicate_item_group_count?: number;
  play_missing_rate?: number;
  likes_coverage_rate?: number;
  favorites_coverage_rate?: number;
  comments_coverage_rate?: number;
  shares_coverage_rate?: number;
  datasets?: LearningDataset[];
  account_quality?: AccountQuality[];
  [key: string]: unknown;
}

export interface AccountQuality {
  account_id?: string;
  account_display_name?: string;
  account_tier?: string;
  account_quality_grade?: string;
  sample_count?: number;
  stored_sample_count?: number;
  formal_sample_count?: number;
  deduped_sample_count?: number;
  trainable_sample_count?: number;
  duplicate_item_group_count?: number;
  likes_coverage_rate?: number;
  favorites_coverage_rate?: number;
  comments_coverage_rate?: number;
  shares_coverage_rate?: number;
  play_missing_rate?: number;
  reward_p50?: number;
  reward_p75?: number;
  confidence?: string;
  confidence_label?: string;
  confidence_reason?: string;
  [key: string]: unknown;
}

export interface DouyinHistorySignal {
  dimension?: string;
  name?: string;
  sample_count?: number;
  avg_reward?: number;
  median_reward?: number;
  p75_reward?: number;
  high_count?: number;
  low_count?: number;
  [key: string]: unknown;
}

export interface DouyinHistoryBaselines {
  contract_version?: string;
  status?: string;
  account_id?: string;
  dataset_id?: string;
  sample_count?: number;
  avg_reward?: number;
  median_reward?: number;
  p75_reward?: number;
  top_signals?: DouyinHistorySignal[];
  groups?: DouyinHistorySignal[];
  [key: string]: unknown;
}

export interface SegmentHistoryMatch {
  matched_segment_id?: string;
  historical_sample_id?: string;
  matched_sample_id?: string;
  platform_item_id?: string;
  matched_platform_item_id?: string;
  account_id?: string;
  account_display_name?: string;
  dataset_id?: string;
  title?: string;
  url?: string;
  similarity?: number;
  reward_proxy?: number;
  normalized_reward?: number;
  sample_source?: string;
  performance_label?: string;
  match_type?: string;
  content_category?: string;
  hook_type?: string;
  slice_structure?: string;
  artist_names?: string;
  song_title?: string;
  [key: string]: unknown;
}

export interface SegmentHistoryResult {
  contract_version?: string;
  status?: string;
  segment_id?: string;
  account_id?: string;
  history_source?: string;
  match_scope?: string;
  fallback_reason?: string;
  sample_count?: number;
  matched_count?: number;
  similar_high_perf_score?: number;
  similar_low_perf_risk?: number;
  similar_high_samples?: SegmentHistoryMatch[];
  similar_low_samples?: SegmentHistoryMatch[];
  matched_high_samples?: SegmentHistoryMatch[];
  matched_low_samples?: SegmentHistoryMatch[];
  account_baseline_position?: Record<string, unknown>;
  prototype_hits?: SegmentHistoryMatch[];
  prototype_summary?: string;
  low_interaction_risk_library?: SegmentHistoryMatch[];
  risk_summary?: string;
  component_scores?: Record<string, number>;
  evidence_quality?: Record<string, unknown>;
  ranker_reason?: string;
  ranker_advice?: RankerAdvice;
  research_ranker_version?: string;
  confidence?: number;
  confidence_label?: string;
  history_uncertainty?: number;
  matches?: SegmentHistoryMatch[];
  [key: string]: unknown;
}

export interface RankerAdvice {
  action?: string;
  label?: string;
  reason?: string;
  severity?: string;
  [key: string]: unknown;
}

export interface InterestClockRecommendation {
  account_id?: string;
  content_type?: string;
  duration_bucket?: string;
  publish_hour?: number;
  suggested_score?: number;
  confidence?: number;
  sample_count?: number;
  [key: string]: unknown;
}

export interface InterestClockResult {
  contract_version?: string;
  status?: string;
  account_id?: string;
  generated_at?: string;
  sample_count?: number;
  group_count?: number;
  query?: Record<string, unknown>;
  recommendations?: InterestClockRecommendation[];
  top_windows?: InterestClockRecommendation[];
  suggestions?: InterestClockRecommendation[];
  [key: string]: unknown;
}

export interface BacktestMetrics {
  sample_count?: number;
  k?: number;
  strategy?: string;
  ndcg_at_k?: number;
  topk_hit_rate?: number;
  topk_lift_vs_random?: number;
  high_interaction_hit_rate?: number;
  low_interaction_avoidance_rate?: number;
  calibration_mae?: number;
  closed_loop_rate?: number;
  low_exposure_uncertain_rate?: number;
  sample_source?: string;
  holdout_policy?: string;
  holdout_policy_key?: string;
  research_label_version?: string;
  research_ranker_version?: string;
  strategy_comparison?: Record<string, Record<string, unknown>>;
  component_ablation?: Record<string, Record<string, unknown>>;
  per_account_metrics?: Record<string, unknown>[];
  promotion_gate?: Record<string, unknown>;
  weight_config?: Record<string, unknown>;
  baseline_gap?: Record<string, unknown>;
  semantic_gap_analysis?: Record<string, unknown>;
  diagnostic_samples?: Record<string, unknown>;
  leakage_guard_summary?: Record<string, unknown>;
  next_calibration_queue?: Record<string, unknown>[];
  calibration_summary?: Record<string, unknown>;
  scorer_version?: string;
  [key: string]: unknown;
}

export interface BacktestReport {
  id?: string;
  contract_version?: string;
  status?: string;
  account_id?: string;
  report_name?: string;
  generated_at?: string;
  created_at?: string;
  query?: Record<string, unknown>;
  metrics?: BacktestMetrics;
  top_rows?: Record<string, unknown>[];
  [key: string]: unknown;
}

export interface BacktestList {
  contract_version?: string;
  account_id?: string;
  count?: number;
  reports?: BacktestReport[];
  [key: string]: unknown;
}

export interface SemanticCalibrationSample {
  sample_id?: string;
  id?: string;
  account_id?: string;
  account_display_name?: string;
  dataset_id?: string;
  title?: string;
  content_category?: string;
  hook_type?: string;
  slice_structure?: string;
  artist_names?: string;
  song_title?: string;
  tags?: string[] | string;
  performance_label?: string;
  label_reason?: string;
  classification_confidence?: string;
  priority_score?: number;
  priority?: number;
  needs?: string[];
  missing_fields?: string[];
  suggested_fields?: string[];
  recommended_fields?: string[];
  recommended_field_guides?: AnnotationFieldGuide[];
  impact_reason?: string;
  queue_reason?: string;
  queue_type?: string;
  disagreement_score?: number;
  risk_score?: number;
  baseline_strategy_score?: number;
  ranker_strategy_score?: number;
  semantic_unknown_reason?: string;
  manual_verified?: boolean;
  reward_proxy?: number;
  normalized_reward?: number;
  published_at?: string;
  [key: string]: unknown;
}

export interface AnnotationFieldGuide {
  field?: string;
  label_zh?: string;
  short_label_zh?: string;
  description_zh?: string;
  annotation_hint_zh?: string;
  allowed_values?: Array<{ value?: string; label_zh?: string; label?: string }>;
  [key: string]: unknown;
}

export interface SemanticCalibrationQueue {
  contract_version?: string;
  status?: string;
  count?: number;
  total_candidates?: number;
  samples?: SemanticCalibrationSample[];
  queue?: SemanticCalibrationSample[];
  recently_saved_samples?: SemanticCalibrationSample[];
  filters?: Record<string, unknown>;
  batch_summary?: Record<string, unknown>;
  semantic_label_catalog?: Record<string, unknown>;
  annotation_field_guides?: Record<string, AnnotationFieldGuide>;
  [key: string]: unknown;
}

export interface CalibrationDraft {
  content_category: string;
  hook_type: string;
  slice_structure: string;
  artist_names: string;
  song_title: string;
  tags: string;
}

export interface MaterialGoldAnnotation {
  id?: string;
  sample_id?: string;
  account_id?: string;
  dataset_id?: string;
  domain_category?: string;
  material_type?: string;
  program_context?: string;
  presentation_style?: string;
  review_status?: string;
  operator?: string;
  review_note?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface MaterialGoldSample extends SemanticCalibrationSample {
  domain_category?: string;
  material_type?: string;
  program_context?: string;
  presentation_style?: string;
  material_conflict?: boolean;
  score_delta_vs_v2_4?: number;
  platform_url?: string;
  material_label_verified?: boolean;
  duplicate_group_size?: number;
  collapsed_variant_count?: number;
  annotation?: MaterialGoldAnnotation | null;
}

export interface MaterialGoldQueue {
  contract_version?: string;
  status?: string;
  mode?: string;
  count?: number;
  total_candidates?: number;
  samples?: MaterialGoldSample[];
  recently_confirmed_samples?: MaterialGoldAnnotation[];
  batch_summary?: Record<string, unknown>;
  annotation_field_guides?: Record<string, AnnotationFieldGuide>;
  source_backtest?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface MaterialConfusionSample extends MaterialGoldSample {
  confusion_pair?: string;
  confusion_pair_label_zh?: string;
  pair_definition?: {
    left?: string;
    right?: string;
    left_cues?: string[];
    right_cues?: string[];
    dynamic?: boolean;
  };
  queue_reason?: string;
  candidate_material_types?: string[];
  candidate_material_labels_zh?: string[];
  candidate_context_fields?: string[];
  omni_raw_material_type?: string;
  omni_canonical_material_type?: string;
  omni_highlight_signal?: string;
  omni_program_context?: string;
  taxonomy_derivation_reason?: string;
  cue_evidence?: {
    left_hits?: string[];
    right_hits?: string[];
    left_type?: string;
    right_type?: string;
  };
  assets?: {
    video?: boolean;
    audio?: boolean;
    transcript?: boolean;
    ocr?: boolean;
    visual?: boolean;
    ready_for_evidence?: boolean;
    paths?: Record<string, string[]>;
  };
}

export interface MaterialConfusionQueue {
  contract_version?: string;
  queue_version?: string;
  status?: string;
  mode?: string;
  count?: number;
  total_candidates?: number;
  taxonomy?: Record<string, unknown>;
  confusion_pairs?: Array<Record<string, unknown>>;
  batch_summary?: Record<string, unknown>;
  samples?: MaterialConfusionSample[];
  [key: string]: unknown;
}

export interface MaterialEvidenceSample {
  sample_id?: string;
  account_id?: string;
  title?: string;
  confusion_pair?: string;
  status?: string;
  source_has_audio?: boolean;
  component_summary?: {
    planned_window_count?: number;
    executed_window_count?: number;
    asr_ready_windows?: number;
    asr_low_information_windows?: number;
    asr_audio_missing_windows?: number;
    ocr_ready_windows?: number;
    omni_ready_windows?: number;
    omni_legacy_response_windows?: number;
  };
  multi_window_prediction?: string;
  multi_window_confidence?: number;
  resolver_strategies?: Record<string, Record<string, unknown>>;
  [key: string]: unknown;
}

export interface MaterialEvidenceStatus {
  contract_version?: string;
  resolver_version?: string;
  status?: string;
  queue_count?: number;
  batch_summary?: Record<string, unknown>;
  coverage?: Record<string, unknown>;
  samples?: MaterialEvidenceSample[];
  latest_resolver_summary?: Record<string, unknown>;
  recommendations?: string[];
  [key: string]: unknown;
}

export interface MaterialResolverReport {
  contract_version?: string;
  resolver_version?: string;
  status?: string;
  summary?: Record<string, unknown>;
  strategy_comparison?: Record<string, Record<string, unknown>>;
  cached_eval_strategy_comparison?: Record<string, Record<string, unknown>>;
  disagreement_samples?: Array<Record<string, unknown>>;
  promotion_gate?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface MaterialWindowAnnotation {
  id?: string;
  window_id?: string;
  sample_id?: string;
  start_seconds?: number;
  end_seconds?: number;
  scene_form?: string;
  program_context_mode?: string;
  selection_quality?: string;
  review_status?: string;
  review_note?: string;
  [key: string]: unknown;
}

export interface VisualWindowReviewSample {
  sample_id?: string;
  window_id?: string;
  window_key?: string;
  account_id?: string;
  title?: string;
  platform_url?: string;
  gold_material_type?: string;
  audio_source?: string;
  start_seconds?: number;
  end_seconds?: number;
  frame_urls?: string[];
  selected_by?: string[];
  embedding_status?: string;
  predicted_scene_form?: string;
  visual_score?: number;
  text_score?: number;
  fusion_score?: number;
  annotation?: MaterialWindowAnnotation | null;
  [key: string]: unknown;
}

export interface VisualWindowScoutStatus {
  contract_version?: string;
  build_id?: string;
  status?: string;
  mode?: string;
  media_readiness?: Record<string, unknown>;
  annotation_summary?: Record<string, unknown>;
  prototype_summary?: Record<string, unknown>;
  latest_build?: Record<string, unknown>;
  batch_progress?: Record<string, unknown>;
  build_history?: Array<Record<string, unknown>>;
  review_queue?: {
    status?: string;
    count?: number;
    pending_count?: number;
    samples?: VisualWindowReviewSample[];
  };
  scene_form_options?: Array<Record<string, unknown>>;
  program_context_options?: Array<Record<string, unknown>>;
  selection_quality_options?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface VisualWindowScoutReport extends VisualWindowScoutStatus {
  lifecycle?: string;
  resumed_pending_batch?: boolean;
  selection_summary?: Record<string, unknown>;
  manifest_summary?: Record<string, unknown>;
  sample_count?: number;
  candidate_count?: number;
  embedding_ready_count?: number;
  build_counts?: Record<string, number>;
  samples?: Array<Record<string, unknown>>;
  omni_top2_queue?: Array<Record<string, unknown>>;
  service_status?: Record<string, unknown>;
}

export interface VisualWindowExperiment {
  contract_version?: string;
  experiment_id?: string;
  build_id?: string;
  build_ids?: string[];
  source_build_count?: number;
  evaluation_scope?: string;
  status?: string;
  strategy_comparison?: Record<string, Record<string, unknown>>;
  paired_comparison?: Record<string, Record<string, unknown>>;
  evaluation_summary?: Record<string, unknown>;
  leakage_guard_summary?: Record<string, unknown>;
  build_manifest_verification?: Record<string, unknown>;
  embedding_coverage_summary?: Record<string, unknown>;
  promotion_gate?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface MaterialWindowDraft {
  scene_form: string;
  program_context_mode: string;
  selection_quality: string;
  review_note: string;
}

export interface MaterialGoldDraft {
  domain_category: string;
  material_type: string;
  program_context: string;
  presentation_style: string;
  review_note: string;
}

export interface MaterialCalibrationReplay {
  contract_version?: string;
  status?: string;
  mode?: string;
  metrics?: {
    strategy?: string;
    strategy_comparison?: Record<string, Record<string, unknown>>;
    promotion_gate?: Record<string, unknown>;
    omni_material_calibration?: Record<string, unknown>;
    omni_material_calibration_holdout?: Record<string, unknown>;
    omni_material_gold_split?: Record<string, unknown>;
    omni_material_router_profiles?: Record<string, unknown>[];
    omni_material_taxonomy_router_profiles?: Record<string, unknown>[];
    omni_material_v28_report?: Record<string, unknown>;
    omni_material_v28_gate?: Record<string, unknown>;
    omni_material_v29_report?: Record<string, unknown>;
    omni_material_v29_gate?: Record<string, unknown>;
    [key: string]: unknown;
  };
  queue?: MaterialGoldQueue;
  [key: string]: unknown;
}

export interface RankerTuningResult {
  contract_version?: string;
  status?: string;
  best?: Record<string, unknown>;
  trials?: Record<string, unknown>[];
  metrics?: BacktestMetrics;
  strategy_comparison?: Record<string, Record<string, unknown>>;
  per_account_metrics?: Record<string, unknown>[];
  promotion_gate?: Record<string, unknown>;
  baseline_gap?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface SemanticFeatureCoverageItem {
  count?: number;
  total?: number;
  rate?: number;
  sample_count?: number;
  [key: string]: unknown;
}

export interface SemanticFeatureExperiment {
  contract_version?: string;
  status?: string;
  account_id?: string;
  strategy?: string;
  sample_count?: number;
  k?: number;
  holdout_policy?: string;
  coverage?: Record<string, SemanticFeatureCoverageItem>;
  base_metrics?: BacktestMetrics;
  strategy_comparison?: Record<string, Record<string, unknown>>;
  field_mask_ablation?: Record<string, unknown>[];
  diagnosis?: {
    promotion_gap_to_1_85?: number;
    strongest_positive_evidence_fields?: Record<string, unknown>[];
    possibly_noisy_fields?: Record<string, unknown>[];
    low_coverage_core_fields?: string[];
    recommendation?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export interface SemanticFeatureBackfillResult {
  contract_version?: string;
  status?: string;
  semantic_feature_version?: string;
  scanned?: number;
  updated?: number;
  unchanged?: number;
  skipped_current?: number;
  manual_verified_seen?: number;
  coverage?: Record<string, SemanticFeatureCoverageItem>;
  [key: string]: unknown;
}

export interface SliceStructureEvaluation {
  contract_version?: string;
  evaluator_version?: string;
  status?: string;
  sample_count?: number;
  evaluated_count?: number;
  coverage?: {
    total?: number;
    current_known_rate?: number;
    evaluator_known_rate?: number;
    trusted_rate?: number;
    high_confidence_rate?: number;
    agreement_rate?: number;
    conflict_rate?: number;
    conflict_count?: number;
    [key: string]: unknown;
  };
  structure_distribution?: Array<Record<string, unknown>>;
  issues?: Array<Record<string, unknown>>;
  review_queue?: Array<Record<string, unknown>>;
  recommendations?: string[];
  [key: string]: unknown;
}

export interface MultimodalCollectionPlan {
  contract_version?: string;
  validation_version?: string;
  plan_type?: string;
  status?: string;
  sample_count?: number;
  candidate_count?: number;
  plan_path?: string;
  next_command?: string;
  summary?: Record<string, unknown>;
  samples?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface MultimodalValidationResult {
  contract_version?: string;
  validation_version?: string;
  status?: string;
  validation_mode?: string;
  sample_count?: number;
  evaluated_count?: number;
  asset_readiness?: {
    coverage?: Record<string, { count?: number; total?: number; rate?: number; [key: string]: unknown }>;
    by_label?: Record<string, Record<string, unknown>>;
    [key: string]: unknown;
  };
  proxy_signal_experiment?: {
    baseline?: Record<string, unknown>;
    multimodal_proxy?: Record<string, unknown>;
    lift_delta?: number;
    high_hit_delta?: number;
    low_avoidance_delta?: number;
    feature_group_ablation?: Array<Record<string, unknown>>;
    useful_signal_groups?: string[];
    [key: string]: unknown;
  };
  promotion_gate?: Record<string, unknown>;
  review_queue?: Array<Record<string, unknown>>;
  recommendations?: string[];
  [key: string]: unknown;
}

export interface MultimodalFeatureExperimentResult {
  contract_version?: string;
  validation_version?: string;
  feature_version?: string;
  status?: string;
  validation_mode?: string;
  sample_count?: number;
  feature_ready_count?: number;
  audio_ready_count?: number;
  visual_ready_count?: number;
  feature_coverage?: Record<string, unknown>;
  strategy_comparison?: Record<string, Record<string, unknown>>;
  feature_diagnostics?: Record<string, unknown>;
  promotion_gate?: Record<string, unknown>;
  recommendations?: string[];
  [key: string]: unknown;
}

export interface QwenEmbeddingBuildResult {
  contract_version?: string;
  status?: string;
  embedding_model?: string;
  embedding_version?: string;
  entity_type?: string;
  account_id?: string;
  dataset_id?: string;
  modality?: string;
  sample_count?: number;
  created?: number;
  reused?: number;
  skipped?: number;
  failed?: number;
  coverage?: Record<string, unknown>;
  service_status?: Record<string, unknown>;
  errors?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface QwenEmbeddingEvidenceResult {
  contract_version?: string;
  status?: string;
  embedding_model?: string;
  account_id?: string;
  dataset_id?: string;
  modality?: string;
  sample_count?: number;
  embedding_coverage?: Record<string, unknown>;
  build_summary?: Record<string, unknown>;
  similar_evidence_summary?: Record<string, unknown>;
  low_interaction_risk_samples?: Array<Record<string, unknown>>;
  evidence_samples?: Array<Record<string, unknown>>;
  recommendations?: string[];
  [key: string]: unknown;
}

export interface PrototypeExample {
  title?: string;
  views?: number;
  score?: number;
  platform_item_id?: string;
  url?: string;
  source_kind?: string;
  [key: string]: unknown;
}

export interface PrototypeBankItem {
  contract_version?: string;
  account_id?: string;
  dataset_id?: string;
  dataset_name?: string;
  prototype_key?: string;
  prototype_name?: string;
  source?: string;
  sample_count?: number;
  median_views?: number;
  p75_views?: number;
  max_views?: number;
  avg_score?: number;
  confidence?: number;
  keywords?: string[];
  examples?: PrototypeExample[];
  parameters?: {
    duration_seconds_range?: number[];
    opening_hook?: string;
    title_patterns?: string[];
    cover_focus?: string;
    publish_hours?: number[];
    absolute_level?: {
      code?: string;
      label?: string;
      views?: number;
      min_views?: number;
      max_views?: number | null;
      basis?: string;
      [key: string]: unknown;
    };
    max_absolute_level?: {
      code?: string;
      label?: string;
      views?: number;
      [key: string]: unknown;
    };
	    account_lift?: {
	      account_median_views?: number;
	      account_p75_views?: number;
	      median_lift?: number;
	      p75_lift?: number;
	      max_vs_account_p75?: number;
	      label?: string;
	      [key: string]: unknown;
	    };
	    performance_metric?: {
	      basis?: string;
	      label?: string;
	      median?: number;
	      p75?: number;
	      max?: number;
	      account_median?: number;
	      account_p75?: number;
	      median_lift?: number;
	      p75_lift?: number;
	      max_vs_account_p75?: number;
	      lift_label?: string;
	      play_count_missing?: boolean;
	      [key: string]: unknown;
	    };
    stability?: {
      key?: string;
      label?: string;
      rank?: number;
      reasons?: string[];
      [key: string]: unknown;
    };
    decision_label?: string;
    [key: string]: unknown;
  };
  updated_at?: string;
  [key: string]: unknown;
}

export interface PrototypeBankResult {
  contract_version?: string;
  status?: string;
  account_id?: string;
  dataset_id?: string;
  dataset_name?: string;
  dataset?: LearningDataset;
  source?: string;
  generated_at?: string;
  source_raw_count?: number;
  source_unique_count?: number;
  sample_count?: number;
  historical_count?: number;
  training_ready_count?: number;
  trainable_count?: number;
  prototype_count?: number;
  count?: number;
  account_distribution?: {
    sample_count?: number;
    median_views?: number;
    p75_views?: number;
    p90_views?: number;
    max_views?: number;
    [key: string]: unknown;
  };
  source_summary?: Record<string, unknown>;
  prototypes?: PrototypeBankItem[];
  next_actions?: string[];
  [key: string]: unknown;
}

export interface LearningDataset {
  id: string;
  name?: string;
  display_name?: string;
  account_id?: string;
  account_display_name?: string;
  account_tier?: string;
  program_key?: string;
  kind?: string;
  source_paths?: string[];
  source_raw_count?: number;
  source_raw_rows?: number;
  source_rows?: number;
  file_rows?: number;
  raw_rows?: number;
  raw_count?: number;
  total_rows?: number;
  source_unique_count?: number;
  source_unique_rows?: number;
  unique_rows?: number;
  deduped_count?: number;
  deduped_rows?: number;
  sample_count?: number;
  unique_count?: number;
  stored_sample_count?: number;
  formal_sample_count?: number;
  deduped_sample_count?: number;
  historical_count?: number;
  historical_sample_count?: number;
  stored_count?: number;
  stored_samples?: number;
  imported_samples?: number;
  training_ready_count?: number;
  training_ready_samples?: number;
  trainable_count?: number;
  trainable_samples?: number;
  training_sample_count?: number;
  training_samples?: number;
  eligible_count?: number;
  usable_count?: number;
  max_views?: number;
  latest_at?: string;
  [key: string]: unknown;
}

export interface LearningDatasetList {
  contract_version?: string;
  count?: number;
  datasets?: LearningDataset[];
  historical_summary?: HistoricalSampleSummary;
  [key: string]: unknown;
}

export interface RuntimeDiagnostics {
  ffmpeg?: { available?: boolean };
  ffprobe?: { available?: boolean };
  asr?: {
    status?: string;
    backend?: string;
    note?: string;
    default_model?: string;
    faster_whisper_installed?: boolean;
    whisper_cpp?: {
      ready?: boolean;
      binary?: string;
    };
    profile_plan?: {
      profiles?: Array<{
        profile?: string;
        model?: string;
        model_exists?: boolean;
        vad_enabled?: boolean;
        purpose?: string;
      }>;
    };
  };
  [key: string]: unknown;
}

export interface Manifest {
  completion_ratio?: number;
  next_action?: { label?: string };
  steps?: Array<{
    step?: string;
    status?: string;
  }>;
  [key: string]: unknown;
}

export interface ReviewEvent {
  review_status?: string;
  reason?: string;
  created_at?: string;
}

export interface AsrVerification {
  id?: string;
  difference_score?: number;
  model_name?: string;
  profile?: string;
}

export interface FeedbackSummary {
  sample_count?: number;
  best_reward?: number;
  best_normalized_reward?: number;
  latest_at?: string;
}

export interface DashboardInitialState {
  stats?: Partial<DashboardStats>;
  videos?: VideoRow[];
}
