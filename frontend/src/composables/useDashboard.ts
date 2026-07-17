import { computed, reactive, type ComputedRef } from "vue";
import { api, jsonBody } from "../api";
import type {
  AccountInsights,
  BacktestList,
  BacktestReport,
  BaselineRow,
  CalibrationDraft,
  CandidateRow,
  DashboardInitialState,
  DashboardStats,
  DouyinHistoryBaselines,
  DouyinOAuthStatus,
  DouyinSummary,
  FeedbackSectionName,
  HistoricalSampleImportResult,
  HistoricalSampleSummary,
  InspectorSectionName,
  InterestClockResult,
  LearningDataset,
  LearningDatasetList,
  Manifest,
  MaterialCalibrationReplay,
  MaterialConfusionQueue,
  MaterialEvidenceStatus,
  MaterialGoldDraft,
  MaterialGoldQueue,
  MaterialGoldSample,
  MaterialResolverReport,
  MaterialWindowDraft,
  MemoryBuildResult,
  ModuleStatus,
  ModuleStatusKey,
  MultimodalCollectionPlan,
  MultimodalFeatureExperimentResult,
  MultimodalValidationResult,
  PreviewState,
  PrototypeBankResult,
  QwenEmbeddingBuildResult,
  QwenEmbeddingEvidenceResult,
  QualityReport,
  RankerTuningResult,
  RuntimeDiagnostics,
  SegmentHistoryResult,
  SemanticCalibrationQueue,
  SemanticCalibrationSample,
  SemanticFeatureBackfillResult,
  SemanticFeatureExperiment,
  SimulationRow,
  SimulationSummary,
  SliceStructureEvaluation,
  TrainingSample,
  VariantRow,
  VideoRow,
  VisualWindowExperiment,
  VisualWindowReviewSample,
  VisualWindowScoutReport,
  VisualWindowScoutStatus,
  ViewName
} from "../types";
import {
  previewStateFromRow,
  qualityFlagsForSegment,
  readInitialState,
  simulationDecisionForSegment
} from "../utils";

const defaultStats: DashboardStats = {
  videos: 0,
  segments: 0,
  exports: 0,
  training_samples: 0
};

const moduleKeys: ModuleStatusKey[] = [
  "videos",
  "quality",
  "suggestions",
  "history",
  "feedback",
  "simulation",
  "douyin",
  "runtime"
];

function defaultModuleStatus(): Record<ModuleStatusKey, ModuleStatus> {
  return moduleKeys.reduce((acc, key) => {
    acc[key] = { loading: false, error: "" };
    return acc;
  }, {} as Record<ModuleStatusKey, ModuleStatus>);
}

export interface DashboardStore {
  state: DashboardState;
  selectedVideo: ComputedRef<VideoRow | null>;
  selectedSegment: ComputedRef<CandidateRow | null>;
  filteredVideos: ComputedRef<VideoRow[]>;
  accountOptions: ComputedRef<string[]>;
  statusOptions: ComputedRef<string[]>;
  workflowGuide: ComputedRef<WorkflowGuideItem>;
  toast: (message: string) => void;
  dismissToast: () => void;
  setView: (view: ViewName) => void;
  loadRuntime: () => Promise<void>;
  loadStats: () => Promise<void>;
  loadQuality: (videoId: string, notify?: boolean) => Promise<void>;
  loadManifest: (videoId: string) => Promise<void>;
  refreshVideos: () => Promise<void>;
  loadSuggestions: (videoId: string, notify?: boolean) => Promise<void>;
  loadSegmentHistory: (segmentId: string) => Promise<void>;
  loadSimulation: (videoId: string, notify?: boolean) => Promise<void>;
  runStep: (videoId: string, step: "extract" | "segments" | "score") => Promise<void>;
  runAll: (videoId: string) => Promise<void>;
  loadFeedback: () => Promise<void>;
  loadLearningDatasets: () => Promise<void>;
  handleGuideAction: (action: string) => Promise<void>;
  selectCandidate: (segmentId: string, variant?: VariantRow | null, section?: InspectorSectionName) => void;
  openSegmentInCandidates: (segmentId?: string, section?: InspectorSectionName) => Promise<void>;
  uploadVideo: (form: FormData) => Promise<void>;
  importMetrics: (form: FormData) => Promise<void>;
  syncDouyinMock: () => Promise<void>;
  syncDouyinFile: (form: FormData) => Promise<void>;
  startDouyinLogin: () => Promise<void>;
  rebuildFeedback: () => Promise<void>;
  buildMemoryBank: () => Promise<void>;
  rebuildInterestClock: () => Promise<void>;
  runBacktest: () => Promise<void>;
  backfillSemanticFeatures: () => Promise<void>;
  runSemanticFeatureExperiment: () => Promise<void>;
  runSliceStructureEvaluation: () => Promise<void>;
  buildMultimodalCollectionPlan: () => Promise<void>;
  runMultimodalValidation: () => Promise<void>;
  runMultimodalFeatureExperiment: () => Promise<void>;
  runQwenEmbeddingResearch: () => Promise<void>;
  loadCalibrationWorkspace: (notify?: boolean) => Promise<void>;
  loadMaterialGoldQueue: (notify?: boolean) => Promise<void>;
  loadMaterialConfusionQueue: (notify?: boolean) => Promise<void>;
  loadMaterialEvidenceStatus: (notify?: boolean) => Promise<void>;
  runMaterialEvidenceSmoke: () => Promise<void>;
  runMaterialResolverShadow: () => Promise<void>;
  loadVisualWindowScoutStatus: (notify?: boolean) => Promise<void>;
  runVisualWindowScout: () => Promise<void>;
  runVisualWindowExperiment: () => Promise<void>;
  saveMaterialWindowAnnotation: (sampleId: string, windowId: string) => Promise<void>;
  saveMaterialGoldAnnotation: (sampleId: string) => Promise<void>;
  reopenMaterialGoldAnnotation: (sampleId: string) => Promise<void>;
  runMaterialCalibrationReplay: () => Promise<void>;
  loadSemanticCalibrationQueue: (notify?: boolean) => Promise<void>;
  saveCalibrationLabels: (sampleId: string) => Promise<void>;
  reopenCalibrationSample: (sampleId: string) => Promise<void>;
  rebuildCalibrationEvidence: () => Promise<void>;
  importHistoricalSamples: () => Promise<void>;
  buildPrototypeBank: () => Promise<void>;
  exportSegment: (segmentId: string) => Promise<void>;
  reviewSegment: (segmentId: string, status: string) => Promise<void>;
  verifyAsr: (segmentId: string) => Promise<void>;
  createVariant: (segmentId: string) => Promise<void>;
  bindPlatform: (segmentId: string, platformItemId: string) => Promise<void>;
  copyText: (text: string) => Promise<void>;
  qualityFlagsFor: (segmentId?: string) => string[];
  simulationDecisionFor: (segmentId?: string) => ReturnType<typeof simulationDecisionForSegment>;
  withBusy: <T>(key: string, task: () => Promise<T>) => Promise<T | undefined>;
}

export interface DashboardState {
  videos: VideoRow[];
  stats: DashboardStats;
  selectedVideoId: string;
  selectedSegmentId: string;
  suggestions: CandidateRow[];
  simulations: SimulationRow[];
  simulationSummary: SimulationSummary;
  quality: QualityReport | null;
  qualityLoading: boolean;
  trainingSamples: TrainingSample[];
  baselines: BaselineRow[];
  accountInsights: AccountInsights | null;
  douyinSummary: DouyinSummary | null;
  douyinOAuth: DouyinOAuthStatus | null;
  memoryBuild: MemoryBuildResult | null;
  interestClock: InterestClockResult | null;
  backtestReports: BacktestReport[];
  historicalImport: HistoricalSampleImportResult | null;
  historicalSummary: HistoricalSampleSummary | null;
  semanticCalibrationQueue: SemanticCalibrationQueue | null;
  semanticFeatureExperiment: SemanticFeatureExperiment | null;
  semanticFeatureBackfill: SemanticFeatureBackfillResult | null;
  sliceStructureEvaluation: SliceStructureEvaluation | null;
  multimodalCollectionPlan: MultimodalCollectionPlan | null;
  multimodalValidation: MultimodalValidationResult | null;
  multimodalFeatureExperiment: MultimodalFeatureExperimentResult | null;
  qwenEmbeddingBuild: QwenEmbeddingBuildResult | null;
  qwenEmbeddingEvidence: QwenEmbeddingEvidenceResult | null;
  materialGoldQueue: MaterialGoldQueue | null;
  materialConfusionQueue: MaterialConfusionQueue | null;
  materialEvidenceStatus: MaterialEvidenceStatus | null;
  materialResolverReport: MaterialResolverReport | null;
  visualWindowScoutStatus: VisualWindowScoutStatus | null;
  visualWindowScoutReport: VisualWindowScoutReport | null;
  visualWindowExperiment: VisualWindowExperiment | null;
  materialCalibrationReplay: MaterialCalibrationReplay | null;
  calibrationDrafts: Record<string, CalibrationDraft>;
  materialGoldDrafts: Record<string, MaterialGoldDraft>;
  materialWindowDrafts: Record<string, MaterialWindowDraft>;
  rankerTuning: RankerTuningResult | null;
  prototypeBank: PrototypeBankResult | null;
  historyBaselines: DouyinHistoryBaselines | null;
  segmentHistory: Record<string, SegmentHistoryResult>;
  learningDatasets: LearningDataset[];
  runtime: RuntimeDiagnostics | null;
  manifest: Manifest | null;
  preview: PreviewState | null;
  view: ViewName;
  feedbackSection: FeedbackSectionName;
  inspectorSection: InspectorSectionName;
  expandedWorkflow: boolean;
  moduleStatus: Record<ModuleStatusKey, ModuleStatus>;
  videoSearch: string;
  accountFilter: string;
  statusFilter: string;
  feedbackAccount: string;
  feedbackDataset: string;
  metricsResult: string;
  douyinSyncResult: string;
  learningResult: string;
  toastMessage: string;
  busyKey: string;
}

export interface WorkflowGuideItem {
  step: number;
  key: string;
  status: string;
  statusClass: string;
  title: string;
  copy: string;
  action: string;
  actionLabel: string;
  micro: string;
  progress: number;
}

export function useDashboard(): DashboardStore {
  const initial = readInitialState<DashboardInitialState>({});
  const state = reactive<DashboardState>({
    videos: initial.videos || [],
    stats: { ...defaultStats, ...(initial.stats || {}) },
    selectedVideoId: "",
    selectedSegmentId: "",
    suggestions: [],
    simulations: [],
    simulationSummary: {},
    quality: null,
    qualityLoading: false,
    trainingSamples: [],
    baselines: [],
    accountInsights: null,
    douyinSummary: null,
    douyinOAuth: null,
    memoryBuild: null,
    interestClock: null,
    backtestReports: [],
    historicalImport: null,
    historicalSummary: null,
    semanticCalibrationQueue: null,
    semanticFeatureExperiment: null,
    semanticFeatureBackfill: null,
    sliceStructureEvaluation: null,
    multimodalCollectionPlan: null,
    multimodalValidation: null,
    multimodalFeatureExperiment: null,
    qwenEmbeddingBuild: null,
    qwenEmbeddingEvidence: null,
    materialGoldQueue: null,
    materialConfusionQueue: null,
    materialEvidenceStatus: null,
    materialResolverReport: null,
    visualWindowScoutStatus: null,
    visualWindowScoutReport: null,
    visualWindowExperiment: null,
    materialCalibrationReplay: null,
    calibrationDrafts: {},
    materialGoldDrafts: {},
    materialWindowDrafts: {},
    rankerTuning: null,
    prototypeBank: null,
    historyBaselines: null,
    segmentHistory: {},
    learningDatasets: [],
    runtime: null,
    manifest: null,
    preview: null,
    view: "workbench",
    feedbackSection: "overview",
    inspectorSection: "decision",
    expandedWorkflow: false,
    moduleStatus: defaultModuleStatus(),
    videoSearch: "",
    accountFilter: "",
    statusFilter: "",
    feedbackAccount: "",
    feedbackDataset: "all",
    metricsResult: "等待导入",
    douyinSyncResult: "等待同步",
    learningResult: "等待学习评估",
    toastMessage: "",
    busyKey: ""
  });

  let toastTimer: number | undefined;
  let feedbackLoadSeq = 0;
  let feedbackLoadKey = "";
  let feedbackLoadPromise: Promise<void> | null = null;
  let calibrationLoadKey = "";
  let calibrationWorkspaceKey = "";
  let calibrationLoadPromise: Promise<void> | null = null;

  const selectedVideo = computed(() => state.videos.find(video => video.id === state.selectedVideoId) || null);
  const selectedSegment = computed(() => state.suggestions.find(row => row.id === state.selectedSegmentId) || null);
  const accountOptions = computed(() => Array.from(new Set(state.videos.map(video => video.account_id).filter(Boolean))) as string[]);
  const statusOptions = computed(() => Array.from(new Set(state.videos.map(video => video.status).filter(Boolean))) as string[]);
  const filteredVideos = computed(() => {
    const query = state.videoSearch.trim().toLowerCase();
    return state.videos.filter(video => {
      const haystack = `${video.title || ""} ${video.account_id || ""}`.toLowerCase();
      return (!query || haystack.includes(query))
        && (!state.accountFilter || video.account_id === state.accountFilter)
        && (!state.statusFilter || video.status === state.statusFilter);
    });
  });

  const workflowGuide = computed<WorkflowGuideItem>(() => {
    const videoCount = statCount("videos");
    const exportCount = statCount("exports");
    const sampleCount = statCount("training_samples");
    const selectedStatus = selectedVideo.value?.status || "";
    const hasCandidates = Boolean(state.suggestions.length)
      || ["scored", "segmented"].includes(selectedStatus)
      || (state.videos.length === 1 && statCount("segments") > 0);
    let item: Omit<WorkflowGuideItem, "progress">;

    if (!videoCount) {
      item = {
        step: 1,
        key: "upload",
        status: "待开始",
        statusClass: "neutral",
        title: "先导入一期节目",
        copy: "填写账号与标题，上传视频后会出现在节目管理列表。",
        action: "upload",
        actionLabel: "填写导入信息",
        micro: "入口在本面板下方"
      };
    } else if (!hasCandidates) {
      item = {
        step: 2,
        key: "process",
        status: "下一步",
        statusClass: "warn",
        title: "处理选中节目",
        copy: "一键完成提取、候选生成和评分，得到可审核的 Top 列表。",
        action: "process",
        actionLabel: "处理选中节目",
        micro: selectedVideo.value?.title || "先选择节目"
      };
    } else if (!exportCount) {
      item = {
        step: 3,
        key: "review",
        status: "下一步",
        statusClass: "warn",
        title: "审核候选并导出",
        copy: "进入候选审核，查看质量 Gate、标题建议和 9:16 导出入口。",
        action: "candidates",
        actionLabel: "进入候选审核",
        micro: selectedVideo.value?.title || "候选已生成"
      };
    } else if (!sampleCount) {
      item = {
        step: 4,
        key: "feedback",
        status: "待研究",
        statusClass: "neutral",
        title: "查看历史先验",
        copy: "基于已发布研究样本查看账号基线、互动热度和离线回测。",
        action: "feedback",
        actionLabel: "查看研究样本",
        micro: `${exportCount} 条候选待对照`
      };
    } else {
      item = {
        step: 5,
        key: "learn",
        status: "持续优化",
        statusClass: "ok",
        title: "查看复盘建议",
        copy: "查看账号基线和表现摘要，用于下一轮切片策略。",
        action: "feedback",
        actionLabel: "查看复盘摘要",
        micro: `${sampleCount} 条训练样本`
      };
    }
    return { ...item, progress: Math.max(20, Math.min(100, item.step * 20)) };
  });

  function statCount(key: keyof DashboardStats): number {
    return Number(state.stats[key] || 0);
  }

  function percentText(value: number): string {
    if (!Number.isFinite(value)) return "-";
    return `${(Math.max(0, Math.min(1, value)) * 100).toFixed(0)}%`;
  }

  function toast(message: string): void {
    state.toastMessage = message;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      state.toastMessage = "";
    }, 2800);
  }

  function dismissToast(): void {
    state.toastMessage = "";
    window.clearTimeout(toastTimer);
  }

  function errorText(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
  }

  function patchModuleStatus(key: ModuleStatusKey, patch: Partial<ModuleStatus>): void {
    state.moduleStatus[key] = {
      ...(state.moduleStatus[key] || { loading: false, error: "" }),
      ...patch
    };
  }

  function beginModule(key: ModuleStatusKey): void {
    patchModuleStatus(key, { loading: true, error: "" });
  }

  function finishModule(key: ModuleStatusKey): void {
    patchModuleStatus(key, { loading: false, error: "", lastUpdated: new Date().toISOString() });
  }

  function failModule(key: ModuleStatusKey, error: unknown, fallback: string): void {
    patchModuleStatus(key, { loading: false, error: errorText(error, fallback) });
  }

  async function withBusy<T>(key: string, task: () => Promise<T>): Promise<T | undefined> {
    if (state.busyKey) return undefined;
    state.busyKey = key;
    try {
      return await task();
    } finally {
      state.busyKey = "";
    }
  }

  function setView(view: ViewName): void {
    state.view = view || "workbench";
    document.body.dataset.view = state.view;
    if (state.view === "feedback") {
      loadFeedback().catch(() => undefined);
      if (state.feedbackSection === "calibration") {
        loadCalibrationWorkspace(false).catch(() => undefined);
      }
      if (!state.runtime && !state.moduleStatus.runtime.loading) {
        loadRuntime().catch(() => undefined);
      }
    }
    if (state.view === "simulation" && state.selectedVideoId) {
      loadSimulation(state.selectedVideoId, false).catch(() => undefined);
    }
  }

  async function loadRuntime(): Promise<void> {
    beginModule("runtime");
    try {
      state.runtime = await api<RuntimeDiagnostics>("/runtime");
      finishModule("runtime");
    } catch (error) {
      state.runtime = { asr: { note: error instanceof Error ? error.message : "运行环境检测失败" } };
      failModule("runtime", error, "运行环境检测失败");
    }
  }

  async function loadStats(): Promise<void> {
    state.stats = { ...defaultStats, ...(await api<DashboardStats>("/stats")) };
  }

  async function loadQuality(videoId: string, notify = false): Promise<void> {
    if (!videoId) {
      state.quality = null;
      state.qualityLoading = false;
      return;
    }
    state.selectedVideoId = videoId;
    state.qualityLoading = true;
    beginModule("quality");
    try {
      state.quality = await api<QualityReport>(`/videos/${encodeURIComponent(videoId)}/quality?top_k=30`);
      loadManifest(videoId).catch(() => {});
      finishModule("quality");
    } catch (error) {
      state.quality = null;
      failModule("quality", error, "质量洞察暂不可用");
      if (notify) patchModuleStatus("quality", { error: errorText(error, "质量洞察暂不可用") });
    } finally {
      state.qualityLoading = false;
    }
  }

  async function loadManifest(videoId: string): Promise<void> {
    if (!videoId) {
      state.manifest = null;
      return;
    }
    state.manifest = await api<Manifest>(`/videos/${encodeURIComponent(videoId)}/manifest`);
  }

  async function refreshVideos(): Promise<void> {
    beginModule("videos");
    try {
      const [data] = await Promise.all([
        api<{ videos?: VideoRow[] }>("/videos"),
        loadStats()
      ]);
      state.videos = data.videos || [];
      if (!state.selectedVideoId && state.videos.length) {
        state.selectedVideoId = state.videos[0].id;
      }
      finishModule("videos");
      if (state.selectedVideoId) {
        await loadQuality(state.selectedVideoId, false);
        if (state.view === "simulation") await loadSimulation(state.selectedVideoId, false);
      }
    } catch (error) {
      failModule("videos", error, "节目数据暂不可用");
      throw error;
    }
  }

  async function loadSuggestions(videoId: string, notify = true): Promise<void> {
    if (!videoId) {
      state.suggestions = [];
      state.selectedSegmentId = "";
      state.preview = null;
      return;
    }
    state.selectedVideoId = videoId;
    state.selectedSegmentId = "";
    state.preview = null;
    if (state.quality?.video_id !== videoId) {
      loadQuality(videoId, false).catch(() => {});
    }
    beginModule("suggestions");
    try {
      const data = await api<{ suggestions?: CandidateRow[] }>(`/videos/${encodeURIComponent(videoId)}/suggestions?top_k=10`);
      state.suggestions = data.suggestions || [];
      if (state.suggestions.length) {
        selectCandidate(state.suggestions[0].id);
      }
      finishModule("suggestions");
    } catch (error) {
      state.suggestions = [];
      failModule("suggestions", error, "暂无评分结果，请先完成候选生成和评分");
      if (notify) patchModuleStatus("suggestions", { error: "暂无评分结果，请先完成候选生成和评分" });
    }
  }

  async function loadSimulation(videoId: string, notify = true): Promise<void> {
    if (!videoId) {
      state.simulations = [];
      state.simulationSummary = {};
      return;
    }
    state.selectedVideoId = videoId;
    if (state.quality?.video_id !== videoId) {
      loadQuality(videoId, false).catch(() => {});
    }
    beginModule("simulation");
    try {
      const data = await api<{ simulations?: SimulationRow[]; summary?: SimulationSummary }>(`/videos/${encodeURIComponent(videoId)}/simulation?top_k=10`);
      state.simulations = data.simulations || [];
      state.simulationSummary = data.summary || {};
      finishModule("simulation");
    } catch (error) {
      state.simulations = [];
      state.simulationSummary = {};
      failModule("simulation", error, "暂无模拟结果，请先生成候选并评分");
      if (notify) patchModuleStatus("simulation", { error: "暂无模拟结果，请先生成候选并评分" });
    }
  }

  async function runStep(videoId: string, step: "extract" | "segments" | "score"): Promise<void> {
    const labels = { extract: "提取完成", segments: "候选生成完成", score: "评分完成" };
    await api(`/videos/${encodeURIComponent(videoId)}/${step}`, { method: "POST" });
    toast(labels[step] || "处理完成");
    await refreshVideos();
  }

  async function runAll(videoId: string): Promise<void> {
    toast("开始处理选中节目");
    await api(`/videos/${encodeURIComponent(videoId)}/extract`, { method: "POST" });
    await api(`/videos/${encodeURIComponent(videoId)}/segments`, { method: "POST" });
    await api(`/videos/${encodeURIComponent(videoId)}/score`, { method: "POST" });
    toast("候选与评分已更新");
    await refreshVideos();
  }

  function currentFeedbackLoadKey(): string {
    return `${state.feedbackAccount.trim()}::${state.feedbackDataset || "all"}`;
  }

  function currentCalibrationScopeKey(): string {
    return `${state.feedbackAccount.trim()}::${state.feedbackDataset || "all"}`;
  }

  function hasCalibrationWorkspaceData(): boolean {
    return Boolean(
      state.semanticCalibrationQueue
      || state.materialGoldQueue
      || state.materialConfusionQueue
      || state.materialEvidenceStatus
      || state.visualWindowScoutStatus
    );
  }

  function clearCalibrationWorkspace(): void {
    state.semanticCalibrationQueue = null;
    state.materialGoldQueue = null;
    state.materialConfusionQueue = null;
    state.materialEvidenceStatus = null;
    state.visualWindowScoutStatus = null;
    state.calibrationDrafts = {};
    state.materialGoldDrafts = {};
    state.materialWindowDrafts = {};
  }

  function loadFeedback(): Promise<void> {
    const requestedKey = currentFeedbackLoadKey();
    if (feedbackLoadPromise && feedbackLoadKey === requestedKey) {
      return feedbackLoadPromise;
    }

    const requestSeq = ++feedbackLoadSeq;
    feedbackLoadKey = requestedKey;
    const request = performFeedbackLoad(requestSeq);
    const trackedRequest = request.finally(() => {
      if (feedbackLoadPromise === trackedRequest) {
        feedbackLoadPromise = null;
      }
    });
    feedbackLoadPromise = trackedRequest;
    return trackedRequest;
  }

  async function performFeedbackLoad(requestSeq: number): Promise<void> {
    beginModule("feedback");
    beginModule("history");
    beginModule("douyin");
    const account = state.feedbackAccount.trim();
    const accountPath = account || "all";
    const accountQuery = encodeURIComponent(account);
    const dataset = state.feedbackDataset || "all";
    const historyDataset = dataset === "all" ? "" : dataset;
    const requestedCalibrationScope = `${account}::${dataset}`;
    if (calibrationWorkspaceKey && calibrationWorkspaceKey !== requestedCalibrationScope) {
      calibrationWorkspaceKey = "";
      clearCalibrationWorkspace();
    }

    const learningDatasetsRequest = loadLearningDatasets();
    const samplesRequest = api<{ training_samples?: TrainingSample[] }>(`/training-samples?account_id=${accountQuery}&limit=50`);
    const historyBaselinesRequest = api<DouyinHistoryBaselines>(`/learning/douyin-history/baselines?account_id=${accountQuery}&dataset_id=${encodeURIComponent(historyDataset)}&min_count=1&limit=80`);
    const accountBaselinesRequest = account
      ? api<{ baselines?: BaselineRow[] }>(`/accounts/${encodeURIComponent(account)}/baselines`)
      : Promise.resolve<{ baselines?: BaselineRow[] }>({ baselines: [] });
    const accountInsightsRequest = account
      ? api<AccountInsights>(`/accounts/${encodeURIComponent(account)}/insights`)
      : Promise.resolve<AccountInsights | null>(null);
    const evaluationRequest = Promise.allSettled([
      api<InterestClockResult>(`/accounts/${encodeURIComponent(accountPath)}/interest-clock?limit=5`),
      api<BacktestList>(`/learning/backtest?account_id=${accountQuery}&limit=1&compact=true`),
      api<PrototypeBankResult>(`/accounts/${encodeURIComponent(accountPath)}/prototypes?limit=5&source=visible_capture&dataset_id=${encodeURIComponent(dataset)}`)
    ]);
    const douyinRequest = account
      ? Promise.all([
        api<DouyinSummary>(`/platform/douyin/summary?account_id=${encodeURIComponent(account)}`),
        api<DouyinOAuthStatus>(`/platform/douyin/oauth/status?account_id=${encodeURIComponent(account)}`)
      ])
      : Promise.resolve<[DouyinSummary, DouyinOAuthStatus]>([
        { mappings: [], runs: [], metrics: { count: 0, unlinked: 0 } },
        { account: { auth_status: "not_connected" }, token: {}, config: { ready_for_qr_login: false, missing: [] } }
      ]);

    samplesRequest.catch(() => undefined);
    historyBaselinesRequest.catch(() => undefined);
    accountBaselinesRequest.catch(() => undefined);
    accountInsightsRequest.catch(() => undefined);
    douyinRequest.catch(() => undefined);

    try {
      await learningDatasetsRequest;
    } catch (error) {
      if (requestSeq !== feedbackLoadSeq) return;
      state.learningDatasets = [];
      state.historicalSummary = null;
      failModule("feedback", error, "研究数据集暂不可用");
    }
    if (requestSeq !== feedbackLoadSeq) return;
    feedbackLoadKey = currentFeedbackLoadKey();

    let historicalSummary: HistoricalSampleSummary | null = state.historicalSummary;
    let historyBaselines: DouyinHistoryBaselines | null = null;

    try {
      const samples = await samplesRequest;
      if (!historicalSummary) {
        historicalSummary = await api<HistoricalSampleSummary>(`/learning/historical-samples/summary?account_id=${accountQuery}`);
      }
      if (requestSeq !== feedbackLoadSeq) return;
      state.trainingSamples = samples.training_samples || [];
      state.historicalSummary = historicalSummary;
      finishModule("feedback");
    } catch (error) {
      if (requestSeq !== feedbackLoadSeq) return;
      state.trainingSamples = [];
      failModule("feedback", error, "研究样本摘要暂不可用");
    }

    try {
      const [historyResult, baselines, insights, evaluations] = await Promise.all([
        historyBaselinesRequest,
        accountBaselinesRequest,
        accountInsightsRequest,
        evaluationRequest
      ]);
      if (requestSeq !== feedbackLoadSeq) return;
      historyBaselines = historyResult;
      state.historyBaselines = historyBaselines || null;
      state.baselines = baselines.baselines || [];
      state.accountInsights = insights || { sample_count: historyBaselines.sample_count || historicalSummary?.sample_count || 0 };

      const [clock, backtests, prototypes] = evaluations;
      state.interestClock = clock.status === "fulfilled" ? clock.value || null : null;
      state.backtestReports = backtests.status === "fulfilled" ? backtests.value.reports || [] : [];
      state.prototypeBank = prototypes.status === "fulfilled" ? prototypes.value || null : null;
      const rejected = [clock, backtests, prototypes].find(result => result.status === "rejected");
      if (rejected && rejected.status === "rejected") {
        state.learningResult = `部分研究评估暂不可用：${errorText(rejected.reason, "接口失败")}`;
        failModule("history", rejected.reason, "部分研究评估暂不可用");
      } else {
        finishModule("history");
      }
    } catch (error) {
      if (requestSeq !== feedbackLoadSeq) return;
      state.historyBaselines = null;
      state.baselines = [];
      state.accountInsights = null;
      state.interestClock = null;
      state.backtestReports = [];
      state.prototypeBank = null;
      state.learningResult = error instanceof Error ? `学习评估暂不可用：${error.message}` : "学习评估暂不可用";
      failModule("history", error, "历史先验与回测暂不可用");
    }

    try {
      const [douyin, oauth] = await douyinRequest;
      if (requestSeq !== feedbackLoadSeq) return;
      state.douyinSummary = douyin || null;
      state.douyinOAuth = oauth || null;
      finishModule("douyin");
    } catch (error) {
      if (requestSeq !== feedbackLoadSeq) return;
      state.douyinSummary = { mappings: [], runs: [], metrics: { count: 0, unlinked: 0 } };
      state.douyinOAuth = { account: { auth_status: "not_connected" }, token: {}, config: { ready_for_qr_login: false, missing: [] } };
      failModule("douyin", error, "平台账号状态暂不可用");
    }
  }

  function calibrationQueuePath(limit = 8): string {
    const params = new URLSearchParams();
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    if (account) params.set("account_id", account);
    if (dataset) params.set("dataset_id", dataset);
    params.set("limit", String(limit));
    params.set("min_priority", "0");
    params.set("queue_type", "mixed");
    params.set("strategy", "research_ranker_v2_4");
    params.set("min_disagreement", "0");
    return `/learning/semantic-calibration/queue?${params.toString()}`;
  }

  function materialGoldQueuePath(limit = 6): string {
    const params = new URLSearchParams();
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    if (account) params.set("account_id", account);
    if (dataset) params.set("dataset_id", dataset);
    params.set("limit", String(limit));
    return `/learning/material-gold-set/queue?${params.toString()}`;
  }

  function materialConfusionQueuePath(limit = 80): string {
    const params = new URLSearchParams();
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    if (account) params.set("account_id", account);
    if (dataset) params.set("dataset_id", dataset);
    params.set("limit", String(limit));
    params.set("local_media_only", "true");
    return `/learning/material-confusions/queue?${params.toString()}`;
  }

  function materialEvidenceStatusPath(limit = 80): string {
    const params = new URLSearchParams();
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    if (account) params.set("account_id", account);
    if (dataset) params.set("dataset_id", dataset);
    params.set("limit", String(limit));
    params.set("include_reviewed", "true");
    return `/learning/material-evidence/status?${params.toString()}`;
  }

  function visualWindowStatusPath(limit = 60, summaryOnly = false): string {
    const params = new URLSearchParams();
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    if (account) params.set("account_id", account);
    if (dataset) params.set("dataset_id", dataset);
    params.set("limit", String(limit));
    if (summaryOnly) params.set("summary_only", "true");
    return `/learning/visual-window-scout/status?${params.toString()}`;
  }

  function normalizeCalibrationQueue(queue: SemanticCalibrationQueue | null | undefined): SemanticCalibrationQueue | null {
    if (!queue) return null;
    const samples = calibrationSamples(queue);
    const recentlySaved = Array.isArray(queue.recently_saved_samples) ? queue.recently_saved_samples : [];
    return { ...queue, samples, recently_saved_samples: recentlySaved };
  }

  function calibrationSamples(queue: SemanticCalibrationQueue | null | undefined): SemanticCalibrationSample[] {
    if (!queue) return [];
    if (Array.isArray(queue.samples)) return queue.samples;
    if (Array.isArray(queue.queue)) return queue.queue;
    return [];
  }

  function sampleKey(sample: SemanticCalibrationSample): string {
    return String(sample.sample_id || sample.id || "");
  }

  function textValue(value: unknown): string {
    if (Array.isArray(value)) return value.map(item => String(item || "").trim()).filter(Boolean).join(", ");
    return String(value || "");
  }

  function draftFromSample(sample: SemanticCalibrationSample): CalibrationDraft {
    return {
      content_category: textValue(sample.content_category),
      hook_type: textValue(sample.hook_type),
      slice_structure: textValue(sample.slice_structure),
      artist_names: textValue(sample.artist_names),
      song_title: textValue(sample.song_title),
      tags: textValue(sample.tags)
    };
  }

  function syncCalibrationDrafts(samples: SemanticCalibrationSample[]): void {
    const next: Record<string, CalibrationDraft> = {};
    for (const sample of samples) {
      const key = sampleKey(sample);
      if (!key) continue;
      next[key] = state.calibrationDrafts[key] || draftFromSample(sample);
    }
    state.calibrationDrafts = next;
  }

  function materialGoldDraftFromSample(sample: MaterialGoldSample): MaterialGoldDraft {
    return {
      domain_category: textValue(sample.domain_category) || "unknown",
      material_type: textValue(sample.material_type) || "unknown",
      program_context: textValue(sample.program_context) || "unknown",
      presentation_style: textValue(sample.presentation_style) || "unknown",
      review_note: "人工确认素材形态"
    };
  }

  function syncMaterialGoldDrafts(samples: MaterialGoldSample[]): void {
    const next: Record<string, MaterialGoldDraft> = {};
    for (const sample of samples) {
      const key = sampleKey(sample);
      if (!key) continue;
      next[key] = state.materialGoldDrafts[key] || materialGoldDraftFromSample(sample);
    }
    state.materialGoldDrafts = next;
  }

  function materialWindowDraftFromSample(sample: VisualWindowReviewSample): MaterialWindowDraft {
    const annotation = sample.annotation || {};
    return {
      scene_form: textValue(annotation.scene_form || sample.predicted_scene_form) || "unknown",
      program_context_mode: textValue(annotation.program_context_mode) || "unknown",
      selection_quality: textValue(annotation.selection_quality) || "uncertain",
      review_note: textValue(annotation.review_note) || "人工确认视觉候选窗"
    };
  }

  function syncMaterialWindowDrafts(samples: VisualWindowReviewSample[]): void {
    const next: Record<string, MaterialWindowDraft> = {};
    for (const sample of samples) {
      const key = String(sample.window_id || "");
      if (!key) continue;
      next[key] = state.materialWindowDrafts[key] || materialWindowDraftFromSample(sample);
    }
    state.materialWindowDrafts = next;
  }

  function calibrationQueueCounts(queue: SemanticCalibrationQueue | null | undefined): { visible: number; total: number; pending: number; saved: number } {
    const samples = calibrationSamples(queue);
    const summary = queue?.batch_summary && typeof queue.batch_summary === "object"
      ? queue.batch_summary as Record<string, unknown>
      : {};
    const total = Number(queue?.total_candidates ?? queue?.count ?? samples.length);
    return {
      visible: samples.length,
      total: Number.isFinite(total) ? total : samples.length,
      pending: Number(summary.pending_count ?? total ?? samples.length) || 0,
      saved: Number(summary.saved_count ?? 0) || 0
    };
  }

  function calibrationQueueStatusText(queue: SemanticCalibrationQueue | null | undefined): string {
    const counts = calibrationQueueCounts(queue);
    if (!queue) return "等待刷新";
    if (counts.pending <= 0) {
      return counts.saved > 0
        ? `当前筛选已完成 / 已保存 ${counts.saved} 条`
        : "当前筛选暂无待校准样本";
    }
    return `显示 ${counts.visible}/${counts.pending} 条 / 已保存 ${counts.saved} 条`;
  }

  async function loadLearningDatasets(): Promise<void> {
    const params = new URLSearchParams();
    params.set("account_id", state.feedbackAccount.trim());
    params.set("compact", "true");
    const result = await api<LearningDatasetList>(`/learning/datasets?${params.toString()}`);
    state.learningDatasets = result.datasets || [];
    state.historicalSummary = result.historical_summary || null;
    if (!state.feedbackDataset && state.learningDatasets.length) {
      state.feedbackDataset = state.learningDatasets[0].id || "all";
    }
    if (state.feedbackDataset === "all" && !state.learningDatasets.some(item => item.id === "all") && state.learningDatasets[0]?.id) {
      state.feedbackDataset = state.learningDatasets[0].id;
    }
  }

  function scrollToPanel(id: string): void {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function feedbackSectionFromAction(action: string): FeedbackSectionName {
    const section = action.split(":")[1] as FeedbackSectionName | undefined;
    return section && ["overview", "samples", "calibration", "platform", "runtime"].includes(section) ? section : "overview";
  }

  function inspectorSectionFromAction(action: string): InspectorSectionName {
    const section = action.split(":")[1] as InspectorSectionName | undefined;
    return section && ["decision", "asr", "history", "packaging"].includes(section) ? section : "decision";
  }

  function focusUploadForm(): void {
    state.expandedWorkflow = true;
    setView("workbench");
    scrollToPanel("workbench");
    window.setTimeout(() => {
      const target = document.getElementById("upload-title")
        || document.getElementById("upload-account")
        || document.getElementById("upload-file");
      target?.focus();
    }, 120);
  }

  async function handleGuideAction(action: string): Promise<void> {
    if (action.startsWith("feedback:")) {
      state.feedbackSection = feedbackSectionFromAction(action);
      setView("feedback");
      window.setTimeout(() => scrollToPanel(`feedback-${state.feedbackSection}`), 80);
      return;
    }
    if (action.startsWith("candidate:")) {
      state.inspectorSection = inspectorSectionFromAction(action);
      if (!state.selectedVideoId) {
        toast("请先选择节目");
        return;
      }
      setView("candidates");
      scrollToPanel("workbench");
      if (!state.suggestions.length) await loadSuggestions(state.selectedVideoId, false);
      return;
    }
    if (action === "upload") {
      focusUploadForm();
      return;
    }
    if (action === "process") {
      if (!state.selectedVideoId) {
        toast("请先导入或选择节目");
        focusUploadForm();
        return;
      }
      setView("workbench");
      scrollToPanel("videos");
      await withBusy("run-selected", () => runAll(state.selectedVideoId));
      return;
    }
    if (action === "candidates") {
      if (!state.selectedVideoId) {
        toast("请先选择节目");
        return;
      }
      setView("candidates");
      scrollToPanel("workbench");
      if (!state.suggestions.length) await loadSuggestions(state.selectedVideoId, false);
      return;
    }
    if (action === "feedback") {
      state.feedbackSection = "overview";
      setView("feedback");
      scrollToPanel("feedback");
      return;
    }
    if (action === "simulation") {
      if (!state.selectedVideoId) {
        toast("请先选择节目");
        return;
      }
      setView("simulation");
      scrollToPanel("simulation");
    }
  }

  function selectCandidate(segmentId: string, variant: VariantRow | null = null, section: InspectorSectionName = "decision"): void {
    state.selectedSegmentId = segmentId;
    state.inspectorSection = section;
    const row = state.suggestions.find(item => item.id === segmentId);
    state.preview = row ? previewStateFromRow(row, variant || row.latest_export || null) : null;
    if (row) {
      loadSegmentHistory(segmentId).catch(() => undefined);
    }
  }

  async function loadSegmentHistory(segmentId: string): Promise<void> {
    if (!segmentId) return;
    const account = state.feedbackAccount.trim();
    const suffix = account ? `?account_id=${encodeURIComponent(account)}&limit=6` : "?limit=6";
    beginModule("history");
    try {
      const result = await api<SegmentHistoryResult>(`/segments/${encodeURIComponent(segmentId)}/history${suffix}`);
      state.segmentHistory = { ...state.segmentHistory, [segmentId]: result };
      finishModule("history");
    } catch (error) {
      failModule("history", error, "候选历史先验暂不可用");
    }
  }

  async function openSegmentInCandidates(segmentId?: string, section: InspectorSectionName = "decision"): Promise<void> {
    if (!segmentId || !state.selectedVideoId) return;
    await loadSuggestions(state.selectedVideoId, false);
    setView("candidates");
    selectCandidate(segmentId, null, section);
  }

  async function uploadVideo(form: FormData): Promise<void> {
    await api("/videos", { method: "POST", body: form });
    toast("节目已导入");
    await refreshVideos();
  }

  async function importMetrics(form: FormData): Promise<void> {
    const result = await api<{
      imported?: number;
      training_samples?: number;
      row_summary?: { linked_rows?: number; unlinked_rows?: number };
    }>("/metrics/import", { method: "POST", body: form });
    const summary = result.row_summary || {};
    const linked = Number(summary.linked_rows ?? result.training_samples ?? 0);
    const unlinked = Number(summary.unlinked_rows || 0);
    state.metricsResult = `导入 ${result.imported || 0} 条，链接 ${linked} 条，未链接 ${unlinked} 条，训练样本 ${result.training_samples || 0} 条`;
    await loadStats();
    await loadFeedback();
    toast("指标已导入");
  }

  async function syncDouyinMock(): Promise<void> {
    const account = state.feedbackAccount || "main";
    const result = await api<{
      pulled_rows?: number;
      import_result?: {
        training_samples?: number;
        row_summary?: { linked_rows?: number };
      };
    }>("/platform/douyin/sync", jsonBody({ account_id: account, source: "mock" }));
    const summary = result.import_result?.row_summary || {};
    state.douyinSyncResult = `同步 ${Number(result.pulled_rows || 0)} 行，链接 ${Number(summary.linked_rows || 0)} 行，训练样本 ${Number(result.import_result?.training_samples || 0)} 条`;
    await loadStats();
    await loadFeedback();
    toast("抖音 mock 数据已同步");
  }

  async function syncDouyinFile(form: FormData): Promise<void> {
    const result = await api<{
      pulled_rows?: number;
      import_result?: { row_summary?: { linked_rows?: number; unlinked_rows?: number } };
    }>("/platform/douyin/sync-file", { method: "POST", body: form });
    const summary = result.import_result?.row_summary || {};
    state.douyinSyncResult = `文件同步 ${Number(result.pulled_rows || 0)} 行，链接 ${Number(summary.linked_rows || 0)} 行，未链接 ${Number(summary.unlinked_rows || 0)} 行`;
    await loadStats();
    await loadFeedback();
    toast("抖音文件数据已同步");
  }

  async function startDouyinLogin(): Promise<void> {
    const account = state.feedbackAccount || "main";
    const result = await api<DouyinOAuthStatus>("/platform/douyin/oauth/start", jsonBody({ account_id: account }));
    state.douyinOAuth = {
      ...(state.douyinOAuth || {}),
      session: result.session,
      config: result.config,
      state: result.state,
      auth_url: result.auth_url
    };
    if (result.auth_url) {
      window.open(result.auth_url, "_blank", "noopener");
      state.douyinSyncResult = `已打开抖音官方扫码授权页，授权完成后回到研究学习刷新状态。state=${result.state || ""}`;
    } else {
      const missing = Array.isArray(result.config?.missing) ? result.config.missing.join(", ") : "OAuth 配置";
      state.douyinSyncResult = `扫码登录未就绪：缺少 ${missing}`;
    }
  }

  async function rebuildFeedback(): Promise<void> {
    const account = state.feedbackAccount || "";
    const path = `/feedback/rebuild${account ? `?account_id=${encodeURIComponent(account)}` : ""}`;
    const result = await api<{ baselines?: number; training_samples?: number }>(path, { method: "POST" });
    state.metricsResult = `基线 ${result.baselines || 0} 条，训练样本 ${result.training_samples || 0} 条`;
    calibrationWorkspaceKey = "";
    clearCalibrationWorkspace();
    await loadStats();
    await loadFeedback();
    if (state.feedbackSection === "calibration") {
      await loadCalibrationWorkspace(false);
    }
    toast("反馈状态已重算");
  }

  async function buildMemoryBank(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const result = await api<MemoryBuildResult>("/learning/memory/build", jsonBody({ account_id: account || null }));
    state.memoryBuild = result;
    state.learningResult = `记忆库 ${Number(result.created || 0)} 新建 / ${Number(result.reused || 0)} 复用 / 候选 ${Number(result.total_candidates || 0)} 条`;
    toast("记忆库已更新");
  }

  async function rebuildInterestClock(): Promise<void> {
    const account = state.feedbackAccount.trim() || "all";
    const result = await api<InterestClockResult>(`/accounts/${encodeURIComponent(account)}/interest-clock/rebuild`, { method: "POST" });
    state.interestClock = result;
    const top = (result.top_windows || result.suggestions || [])[0] || {};
    state.learningResult = top.publish_hour !== undefined
      ? `时间建议 ${result.status || "ready"} / ${top.publish_hour}:00 / score ${Number(top.suggested_score || 0).toFixed(1)}`
      : `时间建议 ${result.status || "insufficient_history"}`;
    toast("发布时间建议已更新");
  }

  async function runBacktest(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const report = await api<BacktestReport>("/learning/backtest", jsonBody({ account_id: account || null, k: 10, strategy: "research_ranker_v2_4", holdout_policy: "time" }));
    state.backtestReports = [report, ...state.backtestReports].slice(0, 3);
    const metrics = report.metrics || {};
    const gate = metrics.promotion_gate || {};
    state.learningResult = `回测 ${report.status || "ready"} / lift ${Number(metrics.topk_lift_vs_random || 0).toFixed(2)}x / 高互动 ${Number(metrics.high_interaction_hit_rate || 0).toFixed(2)} / ${Boolean(gate.passed) ? "可提升权重" : "研究证据"}`;
    toast("离线回测已生成");
  }

  async function backfillSemanticFeatures(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const result = await api<SemanticFeatureBackfillResult>("/learning/semantic-features/backfill", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 0,
      force: true
    }));
    state.semanticFeatureBackfill = result;
    const structureRate = Number(result.coverage?.slice_structure?.rate || 0);
    const entityRate = Number(result.coverage?.entity_signal?.rate || 0);
    state.learningResult = `语义回填 ${result.status || "ready"} / 更新 ${Number(result.updated || 0)} / 结构 ${percentText(structureRate)} / 实体 ${percentText(entityRate)}`;
    await loadLearningDatasets();
    toast("语义特征已回填");
  }

  async function runSemanticFeatureExperiment(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const result = await api<SemanticFeatureExperiment>("/learning/semantic-feature-experiment/run", jsonBody({
      account_id: account || null,
      k: 10,
      holdout_policy: "time",
      include_field_masks: false
    }));
    state.semanticFeatureExperiment = result;
    const metrics = result.base_metrics || {};
    const noisy = result.diagnosis?.possibly_noisy_fields || [];
    state.learningResult = `语义实验 ${result.status || "ready"} / lift ${Number(metrics.topk_lift_vs_random || 0).toFixed(2)}x / 噪声字段 ${noisy.length}`;
    toast("语义特征实验已生成");
  }

  async function runSliceStructureEvaluation(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const result = await api<SliceStructureEvaluation>("/learning/slice-structure/evaluate", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 0,
      min_confidence: 0
    }));
    state.sliceStructureEvaluation = result;
    const coverage = result.coverage || {};
    const evaluatorRate = Number(coverage.evaluator_known_rate || 0);
    const conflictRate = Number(coverage.conflict_rate || 0);
    const queueCount = Array.isArray(result.review_queue) ? result.review_queue.length : 0;
    state.learningResult = `结构评估 ${result.status || "ready"} / 可判定 ${percentText(evaluatorRate)} / 冲突 ${percentText(conflictRate)} / 待复核 ${queueCount}`;
    toast("切片结构评估已生成");
  }

  async function buildMultimodalCollectionPlan(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const result = await api<MultimodalCollectionPlan>("/learning/multimodal/collection-plan", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 300,
      stage: "beta_d1",
      include_ready: false
    }));
    state.multimodalCollectionPlan = result;
    const summary = result.summary || {};
    const missing = summary.missing_assets && typeof summary.missing_assets === "object"
      ? Object.entries(summary.missing_assets as Record<string, unknown>).slice(0, 2).map(([key, value]) => `${key} ${Number(value || 0)}`).join(" / ")
      : "";
    state.learningResult = `多模态采集计划 ${result.status || "ready"} / 待采 ${Number(result.sample_count || 0)} / 候选 ${Number(result.candidate_count || 0)}${missing ? ` / 缺 ${missing}` : ""}`;
    toast("多模态采集计划已生成");
  }

  async function runMultimodalValidation(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const result = await api<MultimodalValidationResult>("/learning/multimodal-validation/run", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 300,
      k: 10,
      min_samples: 100,
      min_asset_coverage: 0.7
    }));
    state.multimodalValidation = result;
    const readiness = result.asset_readiness || {};
    const coverage = readiness.coverage || {};
    const readyRate = Number(coverage.ready_for_multimodal?.rate || 0);
    const proxy = result.proxy_signal_experiment || {};
    const gate = result.promotion_gate || {};
    state.learningResult = `多模态验证 ${result.status || "research_only"} / 素材 ${percentText(readyRate)} / 代理 lift ${Number(proxy.lift_delta || 0).toFixed(2)} / ${gate.decision || "research_only"}`;
    toast("多模态验证已生成");
  }

  async function runMultimodalFeatureExperiment(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const result = await api<MultimodalFeatureExperimentResult>("/learning/multimodal-feature-experiment/run", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 300,
      k: 10,
      min_feature_samples: 60,
      audio_window_seconds: 10
    }));
    state.multimodalFeatureExperiment = result;
    const coverage = result.feature_coverage || {};
    const strategies = result.strategy_comparison || {};
    const combined = strategies.semantic_plus_audio_visual || {};
    const readyRate = Number(coverage.feature_ready_rate || 0);
    const liftDelta = Number(combined.lift_delta_vs_semantic || 0);
    const gate = result.promotion_gate || {};
    state.learningResult = `真实特征实验 ${result.status || "research_only"} / 覆盖 ${percentText(readyRate)} / 语义增益 ${liftDelta >= 0 ? "+" : ""}${liftDelta.toFixed(2)} / ${gate.decision || "research_only"}`;
    toast("真实多模态特征实验已生成");
  }

  async function runQwenEmbeddingResearch(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const build = await api<QwenEmbeddingBuildResult>("/learning/qwen-embeddings/build", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      entity_type: "historical_sample",
      modality: "text",
      limit: 300,
      force: false
    }));
    state.qwenEmbeddingBuild = build;
    const evidence = await api<QwenEmbeddingEvidenceResult>("/learning/qwen-embedding-evidence/run", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 300,
      k: 10,
      modality: "text"
    }));
    state.qwenEmbeddingEvidence = evidence;
    const report = await api<BacktestReport>("/learning/backtest", jsonBody({
      account_id: account || null,
      k: 10,
      strategy: "ranker_plus_text_embedding",
      holdout_policy: "time"
    }));
    state.backtestReports = [report, ...state.backtestReports].slice(0, 3);
    const coverage = build.coverage || {};
    const metrics = report.metrics || {};
    const gap = metrics.embedding_strategy_gap && typeof metrics.embedding_strategy_gap === "object"
      ? metrics.embedding_strategy_gap as Record<string, unknown>
      : {};
    const selected = gap.selected && typeof gap.selected === "object" ? gap.selected as Record<string, unknown> : {};
    const delta = Number(selected.topk_lift_delta_vs_v2_4 || 0);
    state.learningResult = `Qwen 索引 ${build.status || "ready"} / ready ${percentText(Number(coverage.ready_rate || 0))} / 回测 lift ${Number(metrics.topk_lift_vs_random || 0).toFixed(2)}x / 较 v2.4 ${delta >= 0 ? "+" : ""}${delta.toFixed(2)}`;
    toast("Qwen embedding 索引与研究回测已生成");
  }

  function loadCalibrationWorkspace(notify = false): Promise<void> {
    const requestedKey = currentCalibrationScopeKey();
    if (calibrationWorkspaceKey === requestedKey && hasCalibrationWorkspaceData()) {
      return Promise.resolve();
    }
    if (calibrationLoadPromise && calibrationLoadKey === requestedKey) {
      return calibrationLoadPromise;
    }

    calibrationLoadKey = requestedKey;
    const request = performCalibrationWorkspaceLoad(requestedKey, notify);
    const trackedRequest = request.finally(() => {
      if (calibrationLoadPromise === trackedRequest) {
        calibrationLoadPromise = null;
      }
    });
    calibrationLoadPromise = trackedRequest;
    return trackedRequest;
  }

  async function performCalibrationWorkspaceLoad(requestedKey: string, notify: boolean): Promise<void> {
    beginModule("history");
    const [calibration, materialGold, materialConfusion, materialEvidence, visualWindows] = await Promise.allSettled([
      api<SemanticCalibrationQueue>(calibrationQueuePath(8)),
      api<MaterialGoldQueue>(materialGoldQueuePath(6)),
      api<MaterialConfusionQueue>(materialConfusionQueuePath(80)),
      api<MaterialEvidenceStatus>(materialEvidenceStatusPath(80)),
      api<VisualWindowScoutStatus>(visualWindowStatusPath(60, true))
    ]);
    if (requestedKey !== currentCalibrationScopeKey()) return;

    state.semanticCalibrationQueue = calibration.status === "fulfilled" ? normalizeCalibrationQueue(calibration.value) : null;
    syncCalibrationDrafts(calibrationSamples(state.semanticCalibrationQueue));
    state.materialGoldQueue = materialGold.status === "fulfilled" ? materialGold.value || null : null;
    syncMaterialGoldDrafts(state.materialGoldQueue?.samples || []);
    state.materialConfusionQueue = materialConfusion.status === "fulfilled" ? materialConfusion.value || null : null;
    state.materialEvidenceStatus = materialEvidence.status === "fulfilled" ? materialEvidence.value || null : null;
    state.visualWindowScoutStatus = visualWindows.status === "fulfilled" ? visualWindows.value || null : null;
    syncMaterialWindowDrafts(state.visualWindowScoutStatus?.review_queue?.samples || []);
    calibrationWorkspaceKey = requestedKey;

    const rejected = [calibration, materialGold, materialConfusion, materialEvidence, visualWindows]
      .find(result => result.status === "rejected");
    if (rejected && rejected.status === "rejected") {
      state.learningResult = `部分校准数据暂不可用：${errorText(rejected.reason, "接口失败")}`;
      failModule("history", rejected.reason, "部分校准数据暂不可用");
      return;
    }
    finishModule("history");
    if (notify) toast("评测与校准数据已加载");
  }

  async function loadMaterialGoldQueue(notify = true): Promise<void> {
    beginModule("history");
    try {
      const queue = await api<MaterialGoldQueue>(materialGoldQueuePath(12));
      state.materialGoldQueue = queue || null;
      syncMaterialGoldDrafts(queue.samples || []);
      const summary = queue.batch_summary || {};
      state.learningResult = `素材形态审核：待确认 ${Number(summary.pending_count || 0)} / 已确认 ${Number(summary.confirmed_count || 0)}`;
      finishModule("history");
      if (notify) toast("素材形态审核队列已刷新");
    } catch (error) {
      failModule("history", error, "素材形态审核队列刷新失败");
      throw error;
    }
  }

  async function loadMaterialConfusionQueue(notify = true): Promise<void> {
    beginModule("history");
    try {
      const queue = await api<MaterialConfusionQueue>(materialConfusionQueuePath(80));
      state.materialConfusionQueue = queue || null;
      const summary = queue.batch_summary || {};
      state.learningResult = `定向错判队列：${Number(summary.selected_count || queue.count || 0)} 条 / ${Number(summary.account_count || 0)} 个账号 / 媒体就绪 ${percentText(Number(summary.local_media_ready_rate || 0))}`;
      finishModule("history");
      if (notify) toast("定向错判队列已刷新");
    } catch (error) {
      failModule("history", error, "定向错判队列刷新失败");
      throw error;
    }
  }

  async function loadMaterialEvidenceStatus(notify = true): Promise<void> {
    beginModule("history");
    try {
      const status = await api<MaterialEvidenceStatus>(materialEvidenceStatusPath(80));
      state.materialEvidenceStatus = status || null;
      const summary = status.batch_summary || {};
      state.learningResult = `D10-B 证据：${Number(summary.evidence_ready_count || 0)}/${Number(summary.selected_count || 0)} / 多窗口 ${Number(summary.multi_window_ready_count || 0)} / ASR ${Number(summary.asr_ready_count || 0)} / OCR ${Number(summary.ocr_ready_count || 0)}`;
      finishModule("history");
      if (notify) toast("D10-B 证据状态已刷新");
    } catch (error) {
      failModule("history", error, "D10-B 证据状态刷新失败");
      throw error;
    }
  }

  async function runMaterialEvidenceSmoke(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const report = await api<MaterialEvidenceStatus>("/learning/material-evidence/extract", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 3,
      window_seconds: 8,
      run_asr: true,
      run_ocr: true,
      run_omni: true,
      load_model: false,
      force: false,
      include_reviewed: true
    }));
    const coverage = report.coverage || {};
    state.learningResult = `D10-B Smoke ${report.status || "ready"} / ${Number(report.sample_count || 0)} 条 / 多窗口 ${Number(coverage.multi_window_ready_count || 0)} / ASR ${Number(coverage.asr_ready_count || 0)} / OCR ${Number(coverage.ocr_ready_count || 0)}`;
    await loadMaterialEvidenceStatus(false);
    toast("D10-B 三窗口证据 Smoke 已完成");
  }

  async function runMaterialResolverShadow(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const report = await api<MaterialResolverReport>("/learning/material-resolver/shadow", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 80,
      include_reviewed: true
    }));
    state.materialResolverReport = report;
    const summary = report.summary || {};
    state.learningResult = `Resolver Shadow / Gold 入队 ${percentText(Number(summary.gold_queue_coverage || 0))} / 证据 ${percentText(Number(summary.gold_evidence_coverage || 0))} / unknown 弃权 ${percentText(Number(summary.unknown_abstention_rate || 0))} / ${report.status || "research_only"}`;
    await loadMaterialEvidenceStatus(false);
    toast("D10-B Resolver Shadow 已生成");
  }

  async function loadVisualWindowScoutStatus(notify = true): Promise<void> {
    beginModule("history");
    try {
      const status = await api<VisualWindowScoutStatus>(visualWindowStatusPath(60));
      state.visualWindowScoutStatus = status || null;
      syncMaterialWindowDrafts(status.review_queue?.samples || []);
      const readiness = status.media_readiness || {};
      const annotations = status.annotation_summary || {};
      state.learningResult = `D11 视觉候选窗：可扫描 ${Number(readiness.eligible_count || 0)} / 窗口 Gold ${Number(annotations.confirmed_count || 0)} / ${status.status || "not_started"}`;
      finishModule("history");
      if (notify) toast("D11 视觉候选窗状态已刷新");
    } catch (error) {
      failModule("history", error, "D11 视觉候选窗状态刷新失败");
      throw error;
    }
  }

  async function runVisualWindowScout(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const report = await api<VisualWindowScoutReport>("/learning/visual-window-scout/build", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      limit: 5,
      window_seconds: 15,
      stride_seconds: 5,
      max_windows_per_sample: 3,
      scan_scenes: true,
      load_model: false,
      force: false
    }));
    state.visualWindowScoutReport = report;
    await loadVisualWindowScoutStatus(false);
    state.learningResult = `D11 扫描 ${Number(report.sample_count || 0)} 条 / ${Number(report.candidate_count || 0)} 个候选窗 / embedding ${Number(report.embedding_ready_count || 0)} / ${report.status || "research_only"}`;
    toast("D11 五条视觉窗口扫描已完成");
  }

  async function saveMaterialWindowAnnotation(sampleId: string, windowId: string): Promise<void> {
    const sample = (state.visualWindowScoutStatus?.review_queue?.samples || []).find(item => String(item.window_id || "") === windowId);
    const draft = state.materialWindowDrafts[windowId];
    if (!sample || !draft) {
      toast("未找到可保存的视觉候选窗");
      return;
    }
    await api(`/learning/material-window-gold/${encodeURIComponent(sampleId)}`, {
      ...jsonBody({
        start_seconds: Number(sample.start_seconds || 0),
        end_seconds: Number(sample.end_seconds || 0),
        scene_form: draft.scene_form,
        program_context_mode: draft.program_context_mode,
        selection_quality: draft.selection_quality,
        review_note: draft.review_note,
        operator: "workbench"
      }),
      method: "PATCH"
    });
    await loadVisualWindowScoutStatus(false);
    state.learningResult = `D11 窗口 Gold 已保存 / ${draft.scene_form} / ${draft.selection_quality}`;
    toast("视觉候选窗标注已保存");
  }

  async function runVisualWindowExperiment(): Promise<void> {
    const report = await api<VisualWindowExperiment>("/learning/visual-window-scout/experiment", jsonBody({}));
    state.visualWindowExperiment = report;
    const gate = report.promotion_gate && typeof report.promotion_gate === "object" ? report.promotion_gate as Record<string, unknown> : {};
    const observed = gate.observed && typeof gate.observed === "object" ? gate.observed as Record<string, unknown> : {};
    state.learningResult = `D11 冻结对比 / Fusion Recall@2 ${percentText(Number(observed.fusion_recall_at_2 || 0))} / 样本 ${Number(observed.evaluated_samples || 0)} / ${report.status || "research_only"}`;
    toast("D11 fixed/text/visual/fusion 对比已生成");
  }

  async function saveMaterialGoldAnnotation(sampleId: string): Promise<void> {
    const id = String(sampleId || "").trim();
    const draft = state.materialGoldDrafts[id];
    if (!id || !draft) {
      toast("未找到可保存的素材形态样本");
      return;
    }
    await api(`/learning/material-gold-set/${encodeURIComponent(id)}`, {
      ...jsonBody({
        domain_category: draft.domain_category,
        material_type: draft.material_type,
        program_context: draft.program_context,
        presentation_style: draft.presentation_style,
        review_note: draft.review_note,
        operator: "workbench"
      }),
      method: "PATCH"
    });
    await loadMaterialGoldQueue(false);
    await loadMaterialConfusionQueue(false);
    const summary = state.materialGoldQueue?.batch_summary || {};
    state.learningResult = `素材形态已确认 / 剩余 ${Number(summary.pending_count || 0)} / 已完成 ${Number(summary.confirmed_count || 0)}`;
    toast("素材形态人工确认已保存");
  }

  async function reopenMaterialGoldAnnotation(sampleId: string): Promise<void> {
    const id = String(sampleId || "").trim();
    if (!id) return;
    await api(`/learning/material-gold-set/${encodeURIComponent(id)}/reopen`, jsonBody({
      operator: "workbench",
      reason: "workbench reopen material gold annotation"
    }));
    await loadMaterialGoldQueue(false);
    state.learningResult = "素材形态标注已重新打开";
    toast("样本已回到待审核状态");
  }

  async function runMaterialCalibrationReplay(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const replay = await api<MaterialCalibrationReplay>("/learning/material-gold-set/replay", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      k: 30,
      holdout_policy: "time"
    }));
    state.materialCalibrationReplay = replay;
    state.materialGoldQueue = replay.queue || state.materialGoldQueue;
    syncMaterialGoldDrafts(state.materialGoldQueue?.samples || []);
    const metrics = replay.metrics || {};
    const gate = (metrics.omni_material_v29_report || metrics.omni_material_v28_report) && typeof (metrics.omni_material_v29_report || metrics.omni_material_v28_report) === "object"
      ? (metrics.promotion_gate || {})
      : {};
    const quality = metrics.omni_material_calibration || {};
    const auditQuality = metrics.omni_material_calibration_holdout || quality;
    const split = metrics.omni_material_gold_split || {};
    state.learningResult = `v2.9 回放 / Gold ${Number(quality.confirmed_count || 0)} / 校准 ${Number(split.calibration_count || 0)} + 审计 ${Number(split.audit_count || 0)} / 严格 ${percentText(Number(auditQuality.material_type_accuracy || 0))} / 规范形态 ${percentText(Number(auditQuality.canonical_material_type_accuracy ?? auditQuality.material_type_accuracy ?? 0))} / ${String(gate.status || "research_only")}`;
    toast("v2.9 素材形态层级回放已完成");
  }

  async function loadSemanticCalibrationQueue(notify = true): Promise<void> {
    beginModule("history");
    try {
      const queue = await api<SemanticCalibrationQueue>(calibrationQueuePath(12));
      state.semanticCalibrationQueue = normalizeCalibrationQueue(queue);
      syncCalibrationDrafts(calibrationSamples(state.semanticCalibrationQueue));
      state.learningResult = `语义校准队列：${calibrationQueueStatusText(state.semanticCalibrationQueue)}`;
      finishModule("history");
      if (notify) toast(`语义校准队列已刷新：${calibrationQueueStatusText(state.semanticCalibrationQueue)}`);
    } catch (error) {
      failModule("history", error, "语义校准队列刷新失败");
      throw error;
    }
  }

  function splitMultiValue(value: string): string[] {
    return value
      .split(/[,，、/]/)
      .map(item => item.trim())
      .filter(Boolean);
  }

  async function saveCalibrationLabels(sampleId: string): Promise<void> {
    const id = String(sampleId || "").trim();
    const draft = state.calibrationDrafts[id];
    if (!id || !draft) {
      toast("未找到可保存的校准样本");
      return;
    }
    const currentSample = calibrationSamples(state.semanticCalibrationQueue).find(sample => sampleKey(sample) === id);
    const sampleLabel = textValue(currentSample?.title || currentSample?.song_title || id);
    await api(`/learning/historical-samples/${encodeURIComponent(id)}/labels`, {
      ...jsonBody({
        content_category: draft.content_category,
        hook_type: draft.hook_type,
        slice_structure: draft.slice_structure,
        artist_names: splitMultiValue(draft.artist_names),
        song_title: draft.song_title,
        tags: splitMultiValue(draft.tags),
        operator: "workbench",
        reason: "manual semantic calibration from workbench"
      }),
      method: "PATCH"
    });
    await loadSemanticCalibrationQueue(false);
    const counts = calibrationQueueCounts(state.semanticCalibrationQueue);
    state.learningResult = counts.pending > 0
      ? `已保存“${sampleLabel}”，队列已刷新：剩余 ${counts.pending} 条，当前显示 ${counts.visible} 条，已保存 ${counts.saved} 条`
      : `已保存“${sampleLabel}”，当前筛选下语义校准队列已完成，已保存 ${counts.saved} 条`;
    toast(counts.pending > 0 ? `人工标签已保存，队列已刷新：剩余 ${counts.pending} 条` : "人工标签已保存，当前队列已完成");
  }

  async function reopenCalibrationSample(sampleId: string): Promise<void> {
    const id = String(sampleId || "").trim();
    if (!id) {
      toast("未找到可重新打开的样本");
      return;
    }
    const savedSamples = state.semanticCalibrationQueue?.recently_saved_samples || [];
    const sample = savedSamples.find(item => sampleKey(item) === id);
    const sampleLabel = textValue(sample?.title || sample?.song_title || id);
    await api(`/learning/historical-samples/${encodeURIComponent(id)}/calibration/reopen`, jsonBody({
      classification_confidence: "low",
      operator: "workbench",
      reason: "reopen semantic calibration from workbench"
    }));
    await loadSemanticCalibrationQueue(false);
    const counts = calibrationQueueCounts(state.semanticCalibrationQueue);
    state.learningResult = `已重新打开“${sampleLabel}”，队列已刷新：剩余 ${counts.pending} 条，当前显示 ${counts.visible} 条`;
    toast("已重新打开校准，样本会回到待校准队列");
  }

  async function rebuildCalibrationEvidence(): Promise<void> {
    const account = state.feedbackAccount.trim();
    const dataset = state.feedbackDataset === "all" ? "" : state.feedbackDataset;
    const labelResult = await api<{ updated?: number }>("/learning/research-labels/rebuild", jsonBody({
      account_id: account || null,
      dataset_id: dataset || null,
      min_baseline_samples: 20
    }));
    const prototypeDataset = state.feedbackDataset || "all";
    const prototypes = await api<PrototypeBankResult>("/learning/prototypes/build", jsonBody({
      account_id: account || "all",
      source: "visible_capture",
      dataset_id: prototypeDataset,
      limit: 100,
      force: true
    }));
    const tuning = await api<RankerTuningResult>("/learning/ranker-tuning/run", jsonBody({
      account_id: account || null,
      k: 10,
      holdout_policy: "time",
      max_trials: 12
    }));
    const report = await api<BacktestReport>("/learning/backtest", jsonBody({
      account_id: account || null,
      k: 10,
      strategy: "research_ranker_v2_4",
      holdout_policy: "time"
    }));
    state.prototypeBank = prototypes;
    state.rankerTuning = tuning;
    state.backtestReports = [report, ...state.backtestReports].slice(0, 3);
    const metrics = report.metrics || {};
    const gate = metrics.promotion_gate || {};
    state.learningResult = `重建 ${Number(labelResult.updated || 0)} 条标签 / v2.4 lift ${Number(metrics.topk_lift_vs_random || 0).toFixed(2)}x / ${Boolean(gate.passed) ? "通过门控" : "研究证据"}`;
    await loadSemanticCalibrationQueue(false);
    toast("标签、原型库和 v2.4 回测已更新");
  }

  async function importHistoricalSamples(): Promise<void> {
    const account = state.feedbackAccount.trim();
    if (!account) {
      const summary = await api<HistoricalSampleSummary>("/learning/historical-samples/summary?account_id=");
      state.historicalSummary = summary;
      state.learningResult = `全量历史样本 ${Number(summary.sample_count || 0).toLocaleString("zh-CN")} 条，重复组 ${Number(summary.duplicate_item_group_count || 0)}`;
      toast("全量研究样本已刷新");
      return;
    }
    const dataset = state.feedbackDataset || "all";
    const result = await api<HistoricalSampleImportResult>("/learning/historical-samples/import", jsonBody({ account_id: account, dataset_id: dataset, force: true }));
    state.historicalImport = result;
    const summary = result.summary || await api<HistoricalSampleSummary>(`/learning/historical-samples/summary?account_id=${encodeURIComponent(account)}`);
    state.historicalSummary = summary;
    state.learningResult = `历史入库 ${result.status || "ready"} / 新增 ${Number(result.inserted || 0)} / 更新 ${Number(result.updated || 0)} / 有效 ${Number(result.valid_rows || 0)} / 库内 ${Number(result.sample_count || 0)}`;
    await loadLearningDatasets();
    toast("研究样本已入库");
  }

  async function buildPrototypeBank(): Promise<void> {
    const account = state.feedbackAccount.trim() || "all";
    const dataset = state.feedbackDataset || "default";
    const result = await api<PrototypeBankResult>("/learning/prototypes/build", jsonBody({ account_id: account, source: "visible_capture", dataset_id: dataset, limit: 100, force: true }));
    state.prototypeBank = result;
    const top = (result.prototypes || [])[0] || {};
    state.learningResult = top.prototype_name
      ? `原型库 ${result.status || "ready"} / ${top.prototype_name} / 样本 ${Number(result.sample_count || 0)}`
      : `原型库 ${result.status || "empty"} / 样本 ${Number(result.sample_count || 0)}`;
    toast("高互动原型库已更新");
  }

  async function exportSegment(segmentId: string): Promise<void> {
    const result = await api<VariantRow>(`/segments/${encodeURIComponent(segmentId)}/export`, { method: "POST" });
    const row = state.suggestions.find(item => item.id === segmentId);
    if (row) {
      row.latest_export = result;
      state.selectedSegmentId = segmentId;
      state.preview = previewStateFromRow(row, result);
    }
    toast("导出完成");
    await loadStats();
  }

  async function reviewSegment(segmentId: string, status: string): Promise<void> {
    const reason = status === "approved"
      ? "人工确认可进入导出预览"
      : (status === "blocked" ? "人工暂缓，需补充授权或质量复核" : "人工要求复核");
    const result = await api<{ segment?: CandidateRow }>(`/segments/${encodeURIComponent(segmentId)}/review`, jsonBody({ status, reason, operator: "local" }));
    const index = state.suggestions.findIndex(item => item.id === segmentId);
    if (index >= 0 && result.segment) {
      state.suggestions[index] = result.segment;
      if (state.selectedSegmentId === segmentId) selectCandidate(segmentId, result.segment.latest_export || null);
    }
    toast("复核状态已更新");
  }

  async function verifyAsr(segmentId: string): Promise<void> {
    const result = await api<{ record?: CandidateRow["latest_asr_verification"] } & CandidateRow["latest_asr_verification"]>(
      `/segments/${encodeURIComponent(segmentId)}/asr/verify`,
      jsonBody({ profile: "verify" })
    );
    const row = state.suggestions.find(item => item.id === segmentId);
    if (row) row.latest_asr_verification = result?.record || result || null;
    toast("ASR verify 已生成");
    if (state.selectedVideoId) await loadManifest(state.selectedVideoId);
  }

  async function createVariant(segmentId: string): Promise<void> {
    const row = state.suggestions.find(item => item.id === segmentId);
    const titles = Array.isArray(row?.title_suggestions) ? row.title_suggestions : [];
    const variant = await api<VariantRow>(`/segments/${encodeURIComponent(segmentId)}/variants`, jsonBody({
      title: titles[1] || titles[0] || row?.summary || "A/B 标题版本",
      hypothesis: "对比标题/封面包装对首轮留存的影响",
      changed_variable: "title",
      publish_window: "manual"
    }));
    if (row) {
      row.variants = [variant, ...(row.variants || [])];
      row.variant_count = row.variants.length;
    }
    toast("Variant 已创建");
  }

  async function bindPlatform(segmentId: string, platformItemId: string): Promise<void> {
    const row = state.suggestions.find(item => item.id === segmentId);
    const firstVariant = Array.isArray(row?.variants) ? row.variants[0] : null;
    const mapping = await api(`/platform/mappings`, jsonBody({
      account_id: state.feedbackAccount || "main",
      platform: "douyin",
      platform_item_id: platformItemId,
      candidate_segment_id: segmentId,
      slice_variant_id: firstVariant?.id || ""
    }));
    if (row) row.platform_mappings = [mapping, ...(row.platform_mappings || [])];
    toast("平台 item 已绑定");
    await loadFeedback();
  }

  async function copyText(text: string): Promise<void> {
    await navigator.clipboard?.writeText(text || "");
    toast("已复制");
  }

  function qualityFlagsFor(segmentId?: string): string[] {
    return qualityFlagsForSegment(state.quality, segmentId);
  }

  function simulationDecisionFor(segmentId?: string) {
    return simulationDecisionForSegment(state.quality, segmentId);
  }

  if (state.videos.length) {
    state.selectedVideoId = state.videos[0].id;
  }
  document.body.dataset.view = state.view;

  return {
    state,
    selectedVideo,
    selectedSegment,
    filteredVideos,
    accountOptions,
    statusOptions,
    workflowGuide,
    toast,
    dismissToast,
    setView,
    loadRuntime,
    loadStats,
    loadQuality,
    loadManifest,
    refreshVideos,
    loadSuggestions,
    loadSegmentHistory,
    loadSimulation,
    runStep,
    runAll,
    loadFeedback,
    loadLearningDatasets,
    handleGuideAction,
    selectCandidate,
    openSegmentInCandidates,
    uploadVideo,
    importMetrics,
    syncDouyinMock,
    syncDouyinFile,
    startDouyinLogin,
    rebuildFeedback,
    buildMemoryBank,
    rebuildInterestClock,
    runBacktest,
    backfillSemanticFeatures,
    runSemanticFeatureExperiment,
    runSliceStructureEvaluation,
    buildMultimodalCollectionPlan,
    runMultimodalValidation,
    runMultimodalFeatureExperiment,
    runQwenEmbeddingResearch,
    loadCalibrationWorkspace,
    loadMaterialGoldQueue,
    loadMaterialConfusionQueue,
    loadMaterialEvidenceStatus,
    runMaterialEvidenceSmoke,
    runMaterialResolverShadow,
    loadVisualWindowScoutStatus,
    runVisualWindowScout,
    runVisualWindowExperiment,
    saveMaterialWindowAnnotation,
    saveMaterialGoldAnnotation,
    reopenMaterialGoldAnnotation,
    runMaterialCalibrationReplay,
    loadSemanticCalibrationQueue,
    saveCalibrationLabels,
    reopenCalibrationSample,
    rebuildCalibrationEvidence,
    importHistoricalSamples,
    buildPrototypeBank,
    exportSegment,
    reviewSegment,
    verifyAsr,
    createVariant,
    bindPlatform,
    copyText,
    qualityFlagsFor,
    simulationDecisionFor,
    withBusy
  };
}
