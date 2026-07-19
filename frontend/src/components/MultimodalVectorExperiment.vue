<template>
  <section id="multimodal-vector-experiment" class="vector-experiment">
    <header class="vector-header">
      <div>
        <span class="vector-kicker">FROZEN SHADOW BENCHMARK</span>
        <h4>多模态向量价值评测</h4>
      </div>
      <button type="button" class="icon-action" :disabled="busy" title="刷新实验状态" @click="loadStatus">
        <Icon name="refresh-cw" />
      </button>
    </header>

    <div v-if="error" class="vector-alert"><Icon name="circle-alert" /><span>{{ error }}</span></div>

    <div v-if="status.status === 'not_frozen'" class="vector-empty">
      <Icon name="database" />
      <strong>实验集尚未冻结</strong>
      <span>当前操作只创建研究 manifest，不修改人工 Gold 或生产排序。</span>
      <button type="button" class="primary" :disabled="busy" @click="freezeBenchmark">
        <span v-if="busy" class="spinner"></span><Icon v-else name="shield-check" />冻结 60 组对照
      </button>
    </div>

    <template v-else>
      <div class="vector-metrics">
        <div><span>盲审进度</span><strong>{{ progress.reviewed_count || 0 }}/{{ progress.task_count || 60 }}</strong></div>
        <div><span>评测文本</span><strong>{{ coverageText('evaluation', 'text') }}</strong></div>
        <div><span>评测视觉</span><strong>{{ coverageText('evaluation', 'visual') }}</strong></div>
        <div><span>参考池双模态</span><strong>{{ coverageText('reference', 'both') }}</strong></div>
        <div><span>准入状态</span><strong class="research-only">研究态</strong></div>
      </div>

      <div class="vector-actions">
        <button type="button" :disabled="busy" @click="buildEmbeddings">
          <span v-if="busyKey === 'embeddings'" class="spinner"></span><Icon v-else name="blocks" />定向构建向量
        </button>
        <button type="button" :disabled="busy" @click="runComparison">
          <span v-if="busyKey === 'compare'" class="spinner"></span><Icon v-else name="radar" />刷新对照结果
        </button>
        <span class="vector-action-status">{{ actionStatus }}</span>
      </div>

      <div class="cloud-chain">
        <div class="cloud-chain-head">
          <div><span>BAILIAN SHADOW</span><strong>云端检索、重排与分歧裁判</strong></div>
          <span class="status" :class="cloudReady ? 'success' : 'neutral'">{{ cloudReady ? '可运行' : '门禁关闭' }}</span>
        </div>
        <div class="cloud-chain-metrics">
          <div><span>文本向量</span><strong>{{ cloudCoverage.text_ready_count || 0 }}/{{ cloudCoverage.sample_count || 240 }}</strong></div>
          <div><span>融合向量</span><strong>{{ cloudCoverage.fusion_ready_count || 0 }}/{{ cloudCoverage.sample_count || 240 }}</strong></div>
          <div><span>客观门禁</span><strong :class="cloudOutcomeGateClass">{{ cloudOutcomeGateText }}</strong></div>
          <div><span>Judge 报告</span><strong>{{ cloudReports.judge ? '已生成' : '待运行' }}</strong></div>
          <div><span>平衡互动代理命中</span><strong>{{ cloudOutcomeAccuracyText }}</strong></div>
          <div><span>相对 v2.4</span><strong :class="cloudOutcomeDeltaClass">{{ cloudOutcomeDeltaText }}</strong></div>
        </div>
        <div class="vector-actions">
          <button type="button" :disabled="busy" @click="runCachedAblation">
            <span v-if="busyKey === 'cloud-ablation'" class="spinner"></span><Icon v-else name="radar" />缓存消融
          </button>
          <button type="button" :disabled="busy" @click="runCloud('preflight', 40)">
            <span v-if="busyKey === 'cloud-preflight'" class="spinner"></span><Icon v-else name="shield-check" />离线预检 40 条
          </button>
          <button type="button" :disabled="busy || !cloudReady" @click="runCloud('smoke', 10)">
            <span v-if="busyKey === 'cloud-smoke'" class="spinner"></span><Icon v-else name="flask-conical" />10 条 Smoke
          </button>
          <button type="button" :disabled="busy || !cloudReady" @click="runCloud('embeddings', 40)">
            <span v-if="busyKey === 'cloud-embeddings'" class="spinner"></span><Icon v-else name="cloud-cog" />构建 40 条
          </button>
          <button type="button" :disabled="busy || !cloudReady" @click="runCloud('rerank', 20)">
            <span v-if="busyKey === 'cloud-rerank'" class="spinner"></span><Icon v-else name="list-filter" />重排 20 条
          </button>
          <button type="button" :disabled="busy || !cloudReady || !cloudReports.rerank" @click="runCloud('judge', 20)">
            <span v-if="busyKey === 'cloud-judge'" class="spinner"></span><Icon v-else name="scale" />裁判高分歧
          </button>
        </div>

        <div v-if="ablationReady" class="ablation-results">
          <div class="strategy-results-head">
            <div><span>D12-A CACHE ONLY</span><strong>信号归因与融合校准</strong></div>
            <span class="status" :class="ablationGatePassed ? 'success' : 'neutral'">{{ ablationGatePassed ? '可扩至 60 对' : '保持 v2.4' }}</span>
          </div>
          <div class="ablation-metrics">
            <div><span>v2.4 基线</span><strong>{{ percent(ablationBaselineAccuracy) }}</strong></div>
            <div><span>最佳缓存配置</span><strong>{{ percent(ablationBestAccuracy) }}</strong></div>
            <div><span>增量</span><strong :class="ablationDelta >= 0 ? 'metric-positive' : 'metric-negative'">{{ percentagePointsPrecise(ablationDelta) }}</strong></div>
            <div><span>95% 区间</span><strong>{{ ablationCiText }}</strong></div>
          </div>
          <div class="ablation-table" role="table" aria-label="缓存消融结果">
            <div class="ablation-row table-head" role="row"><span>配置</span><span>平衡命中</span><span>较 v2.4</span><span>样本</span></div>
            <div v-for="row in ablationRows" :key="row.strategy" class="ablation-row" role="row">
              <strong>{{ row.label }}</strong><span>{{ percent(row.accuracy) }}</span><span :class="row.delta >= 0 ? 'metric-positive' : 'metric-negative'">{{ percentagePointsPrecise(row.delta) }}</span><span>{{ row.count }} 对</span>
            </div>
          </div>
          <div class="gate-line"><span class="status neutral">research_only</span><span>{{ ablationGateText }}</span></div>
        </div>

        <div class="ablation-results holdout-results">
          <div class="strategy-results-head">
            <div><span>D12-B INDEPENDENT HOLDOUT</span><strong>20 对独立留出复验</strong></div>
            <span class="status" :class="holdoutGatePassed ? 'success' : 'neutral'">{{ holdoutStatusText }}</span>
          </div>
          <div class="holdout-steps" aria-label="D12-B 验证步骤">
            <span :class="{ active: holdoutStep !== 'not_started' }"><b>1</b>冻结配置</span>
            <span :class="{ active: ['predictions_frozen', 'evaluated'].includes(holdoutStep) }"><b>2</b>生成盲预测</span>
            <span :class="{ active: holdoutStep === 'evaluated' }"><b>3</b>解锁结果</span>
          </div>
          <div class="ablation-metrics">
            <div><span>留出 v2.4</span><strong>{{ holdoutMetricText(holdoutPrimary.v2_4_balanced_pairwise_accuracy) }}</strong></div>
            <div><span>固定融合</span><strong>{{ holdoutMetricText(holdoutPrimary.balanced_pairwise_accuracy) }}</strong></div>
            <div><span>独立增量</span><strong :class="holdoutDelta >= 0 ? 'metric-positive' : 'metric-negative'">{{ holdoutStep === 'evaluated' ? percentagePointsPrecise(holdoutDelta) : '待解锁' }}</strong></div>
            <div><span>60 对次要指标</span><strong>{{ holdoutMetricText(holdoutCombined.balanced_pairwise_accuracy) }}</strong></div>
            <div><span>云调用费用</span><strong>{{ holdoutCostText }}</strong></div>
            <div><span>账号改善</span><strong>{{ holdoutAccountWinsText }}</strong></div>
          </div>
          <div class="vector-actions">
            <button type="button" :disabled="busy || holdoutStep !== 'not_started'" @click="runHoldout('freeze')">
              <span v-if="busyKey === 'holdout-freeze'" class="spinner"></span><Icon v-else name="shield-check" />冻结配置
            </button>
            <button type="button" :disabled="busy || !cloudReady || holdoutStep !== 'configuration_frozen'" @click="runHoldout('predict')">
              <span v-if="busyKey === 'holdout-predict'" class="spinner"></span><Icon v-else name="cloud-cog" />生成盲预测
            </button>
            <button type="button" :disabled="busy || holdoutStep !== 'predictions_frozen'" @click="runHoldout('evaluate')">
              <span v-if="busyKey === 'holdout-evaluate'" class="spinner"></span><Icon v-else name="scale" />解锁并评估
            </button>
            <button type="button" :disabled="busy || holdoutStep !== 'evaluated'" @click="runFailureAttribution">
              <span v-if="busyKey === 'holdout-attribution'" class="spinner"></span><Icon v-else name="search-check" />零成本失败归因
            </button>
            <button type="button" :disabled="busy || holdoutStep !== 'evaluated'" @click="runEvidenceQualityRebuild">
              <span v-if="busyKey === 'evidence-quality'" class="spinner"></span><Icon v-else name="workflow" />重构三窗口证据
            </button>
          </div>
          <div class="gate-line"><span class="status neutral">research_only</span><span>{{ holdoutGateText }}</span></div>
          <div v-if="failureAttributionReady" class="failure-attribution">
            <div class="strategy-results-head">
              <div><span>D12-C0 CACHE ONLY</span><strong>失败原因诊断</strong></div>
              <span class="status neutral">0 元 · 0 请求</span>
            </div>
            <div class="ablation-metrics">
              <div><span>云信号命中</span><strong>{{ percent(failureCloudAccuracy) }}</strong></div>
              <div><span>最终改变选择</span><strong>{{ failureChoiceChanges }} / 20</strong></div>
              <div><span>Top1 表现标签一致</span><strong>{{ percent(failureLabelMatch) }}</strong></div>
              <div><span>Top1 内容分类一致</span><strong>{{ percent(failureCategoryMatch) }}</strong></div>
              <div><span>视觉源不足 3 张</span><strong>{{ failureThinVisualCount }} / {{ failureVisualSampleCount }}</strong></div>
              <div><span>同账号参考覆盖</span><strong>{{ percent(failureSameAccountCoverage) }}</strong></div>
            </div>
            <div class="attribution-list">
              <div v-for="cause in failureRootCauses" :key="cause.cause">
                <strong>{{ causeLabel(cause.cause) }}</strong><span>{{ actionLabel(cause.action) }}</span>
              </div>
            </div>
          </div>
          <div v-if="evidenceQualityReady" class="failure-attribution">
            <div class="strategy-results-head">
              <div><span>D12-C1 EVIDENCE QUALITY</span><strong>三窗口证据与分层参考池</strong></div>
              <span class="status neutral">0 元 · 0 请求</span>
            </div>
            <div class="ablation-metrics">
              <div><span>三窗口就绪</span><strong>{{ evidenceReadyCount }} / {{ evidenceSampleCount }}</strong></div>
              <div><span>三时点覆盖</span><strong>{{ percent(evidenceThreeFrameRate) }}</strong></div>
              <div><span>缓存参考池</span><strong>{{ evidenceReferenceCount }} 条</strong></div>
              <div><span>分层语境覆盖</span><strong>{{ percent(evidenceContextCoverage) }}</strong></div>
              <div><span>同账号高低齐备</span><strong>{{ percent(evidenceBalancedAccountCoverage) }}</strong></div>
              <div><span>冻结池同账号上限</span><strong>{{ percent(evidenceAccountCeiling) }}</strong></div>
              <div><span>分层文本命中</span><strong>{{ percent(evidenceStratifiedAccuracy) }}</strong></div>
              <div><span>较全局文本</span><strong :class="evidenceStratifiedDelta >= 0 ? 'metric-positive' : 'metric-negative'">{{ percentagePointsPrecise(evidenceStratifiedDelta) }}</strong></div>
              <div><span>新 Fusion 向量</span><strong>{{ evidenceVectorReady }} / {{ evidenceVectorTarget }}</strong></div>
              <div><span>参考池缺口账号</span><strong>{{ evidenceUnrecoverableAccounts }} 个</strong></div>
            </div>
            <div class="gate-line"><span class="status neutral">research_only</span><span>{{ evidenceDecisionText }}</span></div>
          </div>
        </div>
      </div>

      <div v-if="task" class="blind-workbench">
        <div class="blind-prompt">
          <div>
            <span>第 {{ task.position || Number(progress.reviewed_count || 0) + 1 }} / {{ progress.task_count || 60 }} 组</span>
            <strong>编辑偏好辅助判断：哪条更值得进入同条件发布测试？</strong>
          </div>
          <span class="blind-badge"><Icon name="shield-check" />身份与历史表现已隐藏</span>
        </div>

        <div class="video-pair">
          <article class="video-choice" :class="{ selected: choice === 'left' }">
            <div class="video-choice-head"><strong>A</strong><span>{{ durationText(task.left?.duration_seconds) }}</span></div>
            <video :key="`${task.task_id}-left`" :src="task.left?.media_url" controls preload="metadata" playsinline></video>
            <button type="button" :aria-pressed="choice === 'left'" @click="choice = 'left'"><Icon name="check" />选择 A</button>
          </article>
          <article class="video-choice" :class="{ selected: choice === 'right' }">
            <div class="video-choice-head"><strong>B</strong><span>{{ durationText(task.right?.duration_seconds) }}</span></div>
            <video :key="`${task.task_id}-right`" :src="task.right?.media_url" controls preload="metadata" playsinline></video>
            <button type="button" :aria-pressed="choice === 'right'" @click="choice = 'right'"><Icon name="check" />选择 B</button>
          </article>
        </div>

        <div class="review-controls">
          <div class="choice-secondary" role="group" aria-label="其他判断">
            <button type="button" :class="{ active: choice === 'tie' }" @click="choice = 'tie'">两者相当</button>
            <button type="button" :class="{ active: choice === 'abstain' }" @click="choice = 'abstain'">无法判断</button>
          </div>
          <div class="confidence-control" role="group" aria-label="判断置信度">
            <span>把握程度</span>
            <button v-for="item in confidenceOptions" :key="item.value" type="button" :class="{ active: confidence === item.value }" @click="confidence = item.value">{{ item.label }}</button>
          </div>
        </div>

        <div class="reason-tags" role="group" aria-label="判断依据">
          <button v-for="item in reasonOptions" :key="item.value" type="button" :class="{ active: reasonTags.includes(item.value) }" @click="toggleReason(item.value)">{{ item.label }}</button>
        </div>

        <div class="review-submit">
          <span>{{ choice ? choiceLabel : '请选择 A、B、相当或无法判断' }}</span>
          <button type="button" class="primary" :disabled="busy || !choice" @click="saveAndNext">
            <span v-if="busyKey === 'review'" class="spinner"></span><Icon v-else name="arrow-right" />保存并进入下一组
          </button>
        </div>
      </div>

      <div v-else class="vector-empty compact">
        <Icon name="check-circle-2" />
        <strong>本轮 60 组盲审已完成</strong>
        <button type="button" class="primary" :disabled="busy" @click="runComparison"><Icon name="radar" />生成最终对照</button>
      </div>

      <div v-if="strategyRows.length" class="strategy-results">
        <div class="strategy-results-head">
          <strong>当前对照结果</strong>
          <span>{{ latestResult?.status === 'ready' ? '达到最小盲审量' : '等待更多盲审' }}</span>
        </div>
        <div class="strategy-table" role="table" aria-label="向量策略对照">
          <div class="strategy-row table-head" role="row"><span>策略</span><span>人工一致</span><span>历史代理</span><span>严重错判</span></div>
          <div v-for="row in strategyRows" :key="row.key" class="strategy-row" role="row">
            <strong>{{ row.label }}</strong><span>{{ percent(row.human) }}</span><span>{{ percent(row.proxy) }}</span><span>{{ row.severe }}</span>
          </div>
        </div>
        <div class="gate-line"><span class="status neutral">research_only</span><span>{{ gateText }}</span></div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, jsonBody } from "../api";
import Icon from "./Icon.vue";

interface BlindSide { label?: string; duration_seconds?: number; media_url?: string }
interface BlindTask { task_id?: string; position?: number; left?: BlindSide; right?: BlindSide }
interface ExperimentStatus {
  status?: string;
  benchmark_id?: string;
  progress?: Record<string, number>;
  embedding_coverage?: Record<string, Record<string, number>>;
  current_task?: BlindTask | null;
  latest_result?: Record<string, unknown> | null;
  recommended_action?: string;
}
interface CloudStatus {
  status?: string;
  embedding_coverage?: Record<string, number>;
  reports?: Record<string, boolean>;
  outcome_proxy_comparison?: Record<string, unknown>;
  cached_ablation?: Record<string, unknown>;
  holdout_validation?: Record<string, unknown>;
  failure_attribution?: Record<string, unknown>;
  evidence_quality?: Record<string, unknown>;
  configuration_errors?: string[];
}

const benchmarkId = "dso-multimodal-vector-value-20260719-r1";
const status = ref<ExperimentStatus>({ status: "loading" });
const cloudStatus = ref<CloudStatus>({ status: "loading" });
const busyKey = ref("");
const error = ref("");
const actionStatus = ref("冻结样本、向量任务和盲审互相独立");
const choice = ref("");
const confidence = ref("medium");
const reasonTags = ref<string[]>([]);
const confidenceOptions = [
  { value: "low", label: "较低" },
  { value: "medium", label: "一般" },
  { value: "high", label: "明确" }
];
const reasonOptions = [
  { value: "hook_clarity", label: "开头清晰" },
  { value: "payoff_strength", label: "回报更强" },
  { value: "performance_quality", label: "表演质量" },
  { value: "context_completeness", label: "上下文完整" },
  { value: "visual_quality", label: "画面表现" },
  { value: "emotional_value", label: "情绪价值" },
  { value: "hard_to_judge", label: "难以比较" }
];

const busy = computed(() => Boolean(busyKey.value));
const progress = computed(() => status.value.progress || {});
const task = computed(() => status.value.current_task || null);
const latestResult = computed<Record<string, unknown> | null>(() => status.value.latest_result || null);
const cloudReady = computed(() => cloudStatus.value.status === "ready_for_shadow");
const cloudCoverage = computed(() => cloudStatus.value.embedding_coverage || {});
const cloudReports = computed(() => cloudStatus.value.reports || {});
const cloudOutcome = computed<Record<string, unknown>>(() => cloudStatus.value.outcome_proxy_comparison || {});
const cachedAblation = computed<Record<string, unknown>>(() => cloudStatus.value.cached_ablation || {});
const holdoutValidation = computed<Record<string, unknown>>(() => cloudStatus.value.holdout_validation || {});
const failureAttribution = computed<Record<string, unknown>>(() => cloudStatus.value.failure_attribution || {});
const failureAttributionReady = computed(() => failureAttribution.value.status === "ready");
const failureComponents = computed<Record<string, unknown>>(() => objectValue(failureAttribution.value.component_comparison));
const failureCloudComponent = computed<Record<string, unknown>>(() => objectValue(failureComponents.value.cloud_50_50));
const failureDecisions = computed<Record<string, unknown>>(() => objectValue(failureAttribution.value.decision_dynamics));
const failureRetrieval = computed<Record<string, unknown>>(() => objectValue(failureAttribution.value.retrieval_diagnostics));
const failureTextRetrieval = computed<Record<string, unknown>>(() => objectValue(failureRetrieval.value.text));
const failureModality = computed<Record<string, unknown>>(() => objectValue(failureAttribution.value.modality_diagnostics));
const failureVisual = computed<Record<string, unknown>>(() => objectValue(failureModality.value.visual_payload));
const failureCloudAccuracy = computed(() => Number(failureCloudComponent.value.balanced_accuracy || 0));
const failureChoiceChanges = computed(() => Number(failureDecisions.value.fixed_choice_change_count || 0));
const failureLabelMatch = computed(() => Number(failureTextRetrieval.value.top1_performance_label_accuracy || 0));
const failureCategoryMatch = computed(() => Number(failureTextRetrieval.value.top1_content_category_match_rate || 0));
const failureSameAccountCoverage = computed(() => Number(failureTextRetrieval.value.same_account_reference_coverage || 0));
const failureThinVisualCount = computed(() => Number(failureVisual.value.less_than_three_sources_count || 0));
const failureVisualSampleCount = computed(() => Number(failureVisual.value.sample_count || 0));
const failureRootCauses = computed(() => {
  const rows = Array.isArray(failureAttribution.value.root_causes) ? failureAttribution.value.root_causes : [];
  return rows.slice(0, 4).map(item => {
    const row = objectValue(item);
    return { cause: String(row.cause || "unknown"), action: String(row.action || "") };
  });
});
const evidenceQuality = computed<Record<string, unknown>>(() => cloudStatus.value.evidence_quality || {});
const evidenceQualityReady = computed(() => !["", "not_run", "loading"].includes(String(evidenceQuality.value.status || "")));
const evidenceSummary = computed<Record<string, unknown>>(() => objectValue(evidenceQuality.value.evidence_summary));
const evidenceReferenceSummary = computed<Record<string, unknown>>(() => objectValue(evidenceQuality.value.reference_summary));
const evidenceReferencePlan = computed<Record<string, unknown>>(() => objectValue(evidenceQuality.value.reference_coverage_plan));
const evidenceComparison = computed<Record<string, unknown>>(() => objectValue(evidenceQuality.value.cached_retrieval_comparison));
const evidenceStratified = computed<Record<string, unknown>>(() => objectValue(evidenceComparison.value.stratified_text));
const evidenceRebuildPlan = computed<Record<string, unknown>>(() => objectValue(evidenceQuality.value.embedding_rebuild_plan));
const evidenceReadyCount = computed(() => Number(evidenceSummary.value.ready_count || 0));
const evidenceSampleCount = computed(() => Number(evidenceSummary.value.sample_count || 0));
const evidenceThreeFrameRate = computed(() => Number(evidenceSummary.value.three_distinct_temporal_frames_rate || 0));
const evidenceReferenceCount = computed(() => Number(evidenceReferenceSummary.value.available_reference_count || 0));
const evidenceContextCoverage = computed(() => Number(evidenceReferenceSummary.value.account_or_program_or_material_coverage || 0));
const evidenceBalancedAccountCoverage = computed(() => Number(evidenceReferenceSummary.value.balanced_same_account_coverage || 0));
const evidenceAccountCeiling = computed(() => Number(evidenceReferencePlan.value.manifest_balanced_same_account_ceiling || 0));
const evidenceUnrecoverableAccounts = computed(() => Number(evidenceReferencePlan.value.unrecoverable_account_count || 0));
const evidenceStratifiedAccuracy = computed(() => Number(evidenceStratified.value.balanced_accuracy || 0));
const evidenceStratifiedDelta = computed(() => Number(evidenceStratified.value.delta_vs_global_text || 0));
const evidenceVectorReady = computed(() => Number(evidenceRebuildPlan.value.ready_vector_count || 0));
const evidenceVectorTarget = computed(() => Number(evidenceRebuildPlan.value.target_sample_count || 0));
const evidenceDecisionText = computed(() => ({
  complete_three_window_evidence_before_embedding: "先补齐三窗口真实帧，再重建 Fusion 向量。",
  expand_stratified_high_low_reference_coverage: "先补齐账号、节目或素材形态下的高低互动参考。",
  await_explicit_d12c1_embedding_rebuild: "证据包已就绪，等待显式重建 D12-C1 Fusion 向量。",
  keep_v2_4_and_revise_reference_objective: "缓存对照未达门槛，继续保持 v2.4。",
  ready_for_new_independent_holdout: "证据门控已满足，可以冻结新的独立留出集。"
} as Record<string, string>)[String(evidenceQuality.value.decision || "")] || "等待执行证据质量重构。" );
const holdoutStep = computed(() => String(holdoutValidation.value.status || "not_started"));
const holdoutPrimary = computed<Record<string, unknown>>(() => objectValue(holdoutValidation.value.holdout_primary));
const holdoutCombined = computed<Record<string, unknown>>(() => objectValue(holdoutValidation.value.combined_60_secondary));
const holdoutGate = computed<Record<string, unknown>>(() => objectValue(holdoutValidation.value.validation_gate));
const holdoutBudget = computed<Record<string, unknown>>(() => objectValue(holdoutValidation.value.budget));
const holdoutGatePassed = computed(() => holdoutGate.value.passed === true);
const holdoutDelta = computed(() => Number(holdoutPrimary.value.accuracy_delta_vs_v2_4 || 0));
const holdoutStatusText = computed(() => ((): Record<string, string> => ({
  not_started: "待冻结",
  configuration_frozen: "配置已冻结",
  predictions_frozen: "预测已冻结",
  evaluated: holdoutGatePassed.value ? "独立信号通过" : "保持 v2.4"
}))()[holdoutStep.value] || "研究态");
const holdoutCostText = computed(() => {
  if (!holdoutBudget.value.hard_batch_cap_cny) return "上限 10 元";
  return `${Number(holdoutBudget.value.effective_cost_cny || 0).toFixed(3)} / ${Number(holdoutBudget.value.hard_batch_cap_cny || 10).toFixed(0)} 元`;
});
const holdoutAccountWinsText = computed(() => {
  if (holdoutStep.value !== "evaluated") return "待解锁";
  return `${Number(holdoutGate.value.combined_ready_account_win_count || 0)} / ${Number(holdoutGate.value.required_combined_ready_account_win_count || 3)}`;
});
const holdoutGateText = computed(() => {
  if (holdoutStep.value === "not_started") return "先冻结 D12-A 配置与剩余 20 对，单次费用硬上限 10 元。";
  if (holdoutStep.value === "configuration_frozen") return "结果标签保持锁定；下一步只生成并冻结预测。";
  if (holdoutStep.value === "predictions_frozen") return "预测 SHA 已冻结，可以解锁历史互动代理做一次性评估。";
  return `结论：${String(holdoutGate.value.decision || "keep_v2_4")}；不会自动修改生产权重。`;
});
const ablationReady = computed(() => Boolean(cachedAblation.value.status));
const ablationBaseline = computed<Record<string, unknown>>(() => objectValue(cachedAblation.value.baseline));
const ablationBest = computed<Record<string, unknown>>(() => objectValue(cachedAblation.value.best_incremental_configuration));
const ablationGate = computed<Record<string, unknown>>(() => objectValue(cachedAblation.value.expansion_gate));
const ablationBaselineAccuracy = computed(() => Number(ablationBest.value.v2_4_balanced_pairwise_accuracy ?? ablationBaseline.value.balanced_pairwise_accuracy ?? 0));
const ablationBestAccuracy = computed(() => Number(ablationBest.value.balanced_pairwise_accuracy || 0));
const ablationDelta = computed(() => Number(ablationBest.value.accuracy_delta_vs_v2_4 || 0));
const ablationGatePassed = computed(() => ablationGate.value.passed === true);
const ablationCiText = computed(() => {
  const values = Array.isArray(ablationBest.value.bootstrap_delta_ci95) ? ablationBest.value.bootstrap_delta_ci95 : [];
  if (values.length !== 2) return "待计算";
  return `${percentagePointsPrecise(Number(values[0] || 0))}～${percentagePointsPrecise(Number(values[1] || 0))}`;
});
const ablationRows = computed(() => {
  const rows = Array.isArray(cachedAblation.value.top_configurations) ? cachedAblation.value.top_configurations : [];
  return rows.slice(0, 5).map(item => {
    const row = objectValue(item);
    const strategy = String(row.strategy || "unknown");
    return {
      strategy,
      label: strategyLabel(strategy),
      accuracy: Number(row.balanced_pairwise_accuracy || 0),
      delta: Number(row.accuracy_delta_vs_v2_4 || 0),
      count: Number(row.evaluable_pair_count || 0)
    };
  });
});
const ablationGateText = computed(() => {
  const accountWins = Number(ablationGate.value.account_win_count || 0);
  const categoryWins = Number(ablationGate.value.category_win_count || 0);
  if (ablationGatePassed.value) return `通过扩量门槛：${accountWins} 个账号、${categoryWins} 个类别获得改善；D12-B 单次上限 10 元。`;
  return `未通过扩量门槛：账号改善 ${accountWins}/3，类别改善 ${categoryWins}/2；不新增云端请求。`;
});
const cloudOutcomeAccuracyText = computed(() => {
  const count = Number(cloudOutcome.value.evaluable_pair_count || 0);
  if (!count) return "待重排";
  const accuracy = Number(cloudOutcome.value.cloud_balanced_pairwise_accuracy ?? cloudOutcome.value.cloud_pairwise_accuracy ?? 0);
  return `${percent(accuracy)} · ${count} 对`;
});
const cloudOutcomeDeltaText = computed(() => {
  const count = Number(cloudOutcome.value.evaluable_pair_count || 0);
  if (!count) return "待对照";
  return percentagePoints(Number(cloudOutcome.value.accuracy_delta_vs_v2_4 || 0));
});
const cloudOutcomeDeltaClass = computed(() => {
  const count = Number(cloudOutcome.value.evaluable_pair_count || 0);
  if (!count) return "";
  return Number(cloudOutcome.value.accuracy_delta_vs_v2_4 || 0) >= 0 ? "metric-positive" : "metric-negative";
});
const cloudOutcomeGateText = computed(() => {
  const gateStatus = String(cloudOutcome.value.status || "");
  const count = Number(cloudOutcome.value.evaluable_pair_count || 0);
  if (gateStatus === "early_stop") return "停止扩量";
  if (cloudOutcome.value.passed === true) return "研究信号通过";
  if (gateStatus === "ready") return "未通过";
  return count ? `${count}/${Number(cloudOutcome.value.required_pair_count || 40)} 对` : "待重排";
});
const cloudOutcomeGateClass = computed(() => {
  if (cloudOutcome.value.passed === true) return "metric-positive";
  return String(cloudOutcome.value.status || "") === "early_stop" ? "metric-negative" : "";
});
const comparison = computed<Record<string, Record<string, unknown>>>(() => {
  const value = latestResult.value?.strategy_comparison;
  return value && typeof value === "object" ? value as Record<string, Record<string, unknown>> : {};
});
const strategyRows = computed(() => {
  const labels: Record<string, string> = {
    current_rules: "当前规则",
    research_ranker_v2_4: "Ranker v2.4",
    ranker_plus_text_embedding: "+ 文本向量",
    ranker_plus_visual_embedding: "+ 视觉向量",
    ranker_plus_text_visual_embedding: "+ 文本视觉融合"
  };
  return Object.entries(labels).filter(([key]) => comparison.value[key]).map(([key, label]) => ({
    key,
    label,
    human: Number(comparison.value[key]?.human_pairwise_accuracy || 0),
    proxy: Number(comparison.value[key]?.historical_proxy_pairwise_accuracy || 0),
    severe: Number(comparison.value[key]?.high_confidence_severe_error_count || 0)
  }));
});
const gateText = computed(() => {
  const gate = latestResult.value?.promotion_gate;
  const value = gate && typeof gate === "object" ? gate as Record<string, unknown> : {};
  const delta = Number(value.fusion_accuracy_delta_vs_v2_4 || 0);
  return `融合较 v2.4 人工一致 ${delta >= 0 ? "+" : ""}${percent(delta)}；不会自动改变生产权重。`;
});
const choiceLabel = computed(() => ({ left: "已选 A", right: "已选 B", tie: "判断为相当", abstain: "本组弃权" }[choice.value] || ""));

onMounted(async () => {
  await Promise.all([loadStatus(), loadCloudStatus()]);
});

async function loadStatus(): Promise<void> {
  busyKey.value = "status";
  error.value = "";
  try {
    status.value = await api<ExperimentStatus>(`/learning/multimodal-vector-experiment/status?benchmark_id=${encodeURIComponent(benchmarkId)}`);
  } catch (cause) {
    error.value = message(cause);
  } finally {
    busyKey.value = "";
  }
}

async function loadCloudStatus(): Promise<void> {
  try {
    cloudStatus.value = await api<CloudStatus>(`/learning/multimodal-vector-experiment/cloud/status?benchmark_id=${encodeURIComponent(benchmarkId)}`);
  } catch (cause) {
    error.value = message(cause);
  }
}

async function freezeBenchmark(): Promise<void> {
  await runAction("freeze", async () => {
    await api("/learning/multimodal-vector-experiment/freeze", jsonBody({ benchmark_id: benchmarkId, pair_count: 60, reference_per_label: 60 }));
    actionStatus.value = "冻结 manifest 已创建";
  });
}

async function buildEmbeddings(): Promise<void> {
  await runAction("embeddings", async () => {
    const result = await api<Record<string, unknown>>("/learning/multimodal-vector-experiment/embeddings", jsonBody({ benchmark_id: benchmarkId }));
    const job = result.model_job && typeof result.model_job === "object" ? result.model_job as Record<string, unknown> : {};
    actionStatus.value = job.job_id ? `向量任务已入队：${String(job.status || "queued")}` : `向量缓存：${String(result.status || "ready")}`;
  });
}

async function runComparison(): Promise<void> {
  await runAction("compare", async () => {
    const result = await api<Record<string, unknown>>("/learning/multimodal-vector-experiment/compare", jsonBody({ benchmark_id: benchmarkId, reviewer_id: "local" }));
    actionStatus.value = result.status === "ready" ? "对照结果已更新" : "已生成中期结果，继续完成盲审";
  });
}

async function saveAndNext(): Promise<void> {
  if (!task.value?.task_id || !choice.value) return;
  await runAction("review", async () => {
    await api(`/learning/multimodal-vector-experiment/reviews/${encodeURIComponent(task.value?.task_id || "")}`, jsonBody({
      benchmark_id: benchmarkId,
      reviewer_id: "local",
      choice: choice.value,
      confidence: confidence.value,
      reason_tags: reasonTags.value
    }));
    choice.value = "";
    confidence.value = "medium";
    reasonTags.value = [];
    actionStatus.value = "已保存盲审判断";
  });
}

async function runCloud(stage: string, limit: number): Promise<void> {
  await runAction(`cloud-${stage}`, async () => {
    const result = await api<Record<string, unknown>>("/learning/multimodal-vector-experiment/cloud/run", jsonBody({
      benchmark_id: benchmarkId,
      stage,
      limit,
      top_n: stage === "smoke" ? 10 : 20,
      judge_limit: stage === "smoke" ? 5 : 20
    }));
    actionStatus.value = `云端 ${stage}：${String(result.status || "completed")}`;
    await loadCloudStatus();
  });
}

async function runCachedAblation(): Promise<void> {
  await runAction("cloud-ablation", async () => {
    const result = await api<Record<string, unknown>>("/learning/multimodal-vector-experiment/cloud/ablation", jsonBody({ benchmark_id: benchmarkId }));
    const gate = objectValue(result.expansion_gate);
    actionStatus.value = gate.passed === true ? "缓存消融通过扩量门槛" : "缓存消融完成，继续保持 v2.4";
    await loadCloudStatus();
  });
}

async function runHoldout(action: "freeze" | "predict" | "evaluate"): Promise<void> {
  await runAction(`holdout-${action}`, async () => {
    const result = await api<Record<string, unknown>>(`/learning/multimodal-vector-experiment/cloud/holdout/${action}`, jsonBody({ benchmark_id: benchmarkId }));
    actionStatus.value = action === "freeze"
      ? "D12-B 配置与 20 对留出集已冻结"
      : action === "predict"
        ? `盲预测已冻结，费用 ${Number(objectValue(result.budget).effective_cost_cny || 0).toFixed(3)} 元`
        : `留出评估完成：${String(objectValue(result.validation_gate).decision || "keep_v2_4")}`;
    await loadCloudStatus();
  });
}

async function runFailureAttribution(): Promise<void> {
  await runAction("holdout-attribution", async () => {
    const result = await api<Record<string, unknown>>("/learning/multimodal-vector-experiment/cloud/holdout-attribution", jsonBody({ benchmark_id: benchmarkId }));
    actionStatus.value = `零成本归因完成：${String(result.decision || "keep_v2_4")}`;
    await loadCloudStatus();
  });
}

async function runEvidenceQualityRebuild(): Promise<void> {
  await runAction("evidence-quality", async () => {
    const result = await api<Record<string, unknown>>("/learning/multimodal-vector-experiment/cloud/evidence-quality/rebuild", jsonBody({
      benchmark_id: benchmarkId,
      scope: "holdout",
      limit: 40,
      force: false
    }));
    actionStatus.value = `证据质量重构完成：${String(result.decision || "research_only")}`;
    await loadCloudStatus();
  });
}

async function runAction(key: string, action: () => Promise<void>): Promise<void> {
  busyKey.value = key;
  error.value = "";
  try {
    await action();
    status.value = await api<ExperimentStatus>(`/learning/multimodal-vector-experiment/status?benchmark_id=${encodeURIComponent(benchmarkId)}`);
  } catch (cause) {
    error.value = message(cause);
  } finally {
    busyKey.value = "";
  }
}

function coverageText(scope: string, modality: string): string {
  const value = status.value.embedding_coverage?.[scope] || {};
  const count = modality === "both" ? Number(value.text_visual_ready_count || 0) : Number(value[`${modality}_ready_count`] || 0);
  return `${count}/${Number(value.sample_count || 0)}`;
}

function toggleReason(value: string): void {
  reasonTags.value = reasonTags.value.includes(value)
    ? reasonTags.value.filter(item => item !== value)
    : [...reasonTags.value, value];
}

function durationText(value?: number): string {
  const seconds = Number(value || 0);
  const minutes = Math.floor(seconds / 60);
  const remain = Math.round(seconds % 60);
  return minutes ? `${minutes}:${String(remain).padStart(2, "0")}` : `${remain}s`;
}

function percent(value: number): string {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function percentagePoints(value: number): string {
  const points = Math.round(Number(value || 0) * 100);
  return `${points >= 0 ? "+" : ""}${points}pp`;
}

function percentagePointsPrecise(value: number): string {
  const points = Number(value || 0) * 100;
  return `${points >= 0 ? "+" : ""}${points.toFixed(1)}pp`;
}

function holdoutMetricText(value: unknown): string {
  return holdoutStep.value === "evaluated" ? percent(Number(value || 0)) : "待解锁";
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function strategyLabel(value: string): string {
  if (value === "cached_rerank_current") return "缓存 Rerank";
  const v24Fusion = value.match(/^v2_4_plus_.+_w(\d+)$/);
  if (v24Fusion) return `v2.4 + 云融合 ${Number(v24Fusion[1])}%`;
  const embeddingRerank = value.match(/^embedding_rerank_w(\d+)$/);
  if (embeddingRerank) return `向量 + Rerank ${Number(embeddingRerank[1])}%`;
  const cosine = value.match(/^(text|fusion)_cosine_ref(\d+)_k(\d+)$/);
  if (cosine) return `${cosine[1] === "text" ? "文本" : "图文"} Cosine · 参考 ${cosine[2]} · K${cosine[3]}`;
  return value;
}

function causeLabel(value: string): string {
  return ({
    cloud_signal_not_outcome_aligned: "云信号没有对齐互动结果",
    retrieval_clusters_semantics_more_than_outcomes: "检索更像内容分类，不像传播结果",
    visual_payload_has_insufficient_temporal_coverage: "视觉输入缺少时间覆盖",
    global_reference_pool_lacks_account_context: "全局参考池缺少账号语境",
    fixed_fusion_is_decision_inactive: "固定融合没有改变选择"
  } as Record<string, string>)[value] || value;
}

function actionLabel(value: string): string {
  return ({
    do_not_raise_cloud_weight_or_expand_judge: "暂不提高云权重，也不扩大 Judge。",
    redesign_reference_objective_and_account_conditioning: "重建传播结果参考目标，并加入账号条件。",
    use_true_hook_middle_payoff_frames_before_retesting_fusion: "补真实 hook/middle/payoff 帧后再测图文融合。",
    build_account_or_program_stratified_references: "建立按账号或节目分层的参考池。",
    improve_signal_quality_before_any_new_weight_gate: "先提升证据质量，再讨论新权重门控。"
  } as Record<string, string>)[value] || value;
}

function message(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause || "操作失败");
}
</script>

<style scoped>
.vector-experiment { display: grid; gap: 14px; padding-top: 4px; }
.vector-header, .blind-prompt, .review-submit, .strategy-results-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.vector-header h4 { margin: 3px 0 0; font-size: 18px; }
.vector-kicker { color: #6db2ff; font-size: 11px; font-weight: 800; letter-spacing: 0; }
.icon-action { width: 42px; height: 42px; padding: 0; display: inline-grid; place-items: center; }
.vector-alert { display: flex; align-items: center; gap: 8px; color: #ffb866; border-left: 3px solid #e48b25; padding: 10px 12px; background: rgba(228, 139, 37, .08); }
.vector-empty { min-height: 220px; display: grid; justify-items: center; align-content: center; gap: 10px; color: var(--muted); text-align: center; border: 1px dashed var(--border); }
.vector-empty.compact { min-height: 150px; }
.vector-empty strong { color: var(--text); }
.vector-metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-block: 1px solid var(--border); }
.vector-metrics > div { min-width: 0; padding: 12px 14px; border-right: 1px solid var(--border); }
.vector-metrics > div:last-child { border-right: 0; }
.vector-metrics span, .vector-metrics strong { display: block; }
.vector-metrics span { color: var(--muted); font-size: 12px; }
.vector-metrics strong { margin-top: 4px; font-size: 18px; }
.research-only { color: #e8b44d; }
.vector-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.vector-actions button, .review-submit button, .vector-empty button { display: inline-flex; align-items: center; gap: 7px; }
.vector-action-status { color: var(--muted); font-size: 12px; margin-left: 4px; }
.cloud-chain { display: grid; gap: 10px; padding-block: 14px; border-block: 1px solid var(--border); }
.cloud-chain-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.cloud-chain-head > div { display: grid; gap: 3px; }
.cloud-chain-head > div > span { color: #6db2ff; font-size: 10px; font-weight: 800; }
.cloud-chain-head strong { font-size: 14px; }
.cloud-chain-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--border); }
.cloud-chain-metrics > div { min-width: 0; padding: 9px 11px; border-right: 1px solid var(--border); }
.cloud-chain-metrics > div:last-child { border-right: 0; }
.cloud-chain-metrics span, .cloud-chain-metrics strong { display: block; }
.cloud-chain-metrics span { color: var(--muted); font-size: 11px; }
.cloud-chain-metrics strong { margin-top: 3px; font-size: 14px; }
.ablation-results { display: grid; gap: 10px; padding-top: 12px; border-top: 1px solid var(--border); }
.failure-attribution { display: grid; gap: 10px; padding-top: 12px; border-top: 1px solid var(--border); }
.attribution-list { display: grid; border: 1px solid var(--border); }
.attribution-list > div { display: grid; grid-template-columns: minmax(180px, .8fr) minmax(240px, 1.2fr); gap: 12px; padding: 9px 11px; border-top: 1px solid var(--border); }
.attribution-list > div:first-child { border-top: 0; }
.attribution-list span { color: var(--muted); }
.holdout-results { margin-top: 2px; }
.holdout-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--border); }
.holdout-steps span { display: flex; align-items: center; gap: 7px; min-width: 0; padding: 8px 10px; color: var(--muted); border-right: 1px solid var(--border); font-size: 11px; }
.holdout-steps span:last-child { border-right: 0; }
.holdout-steps span.active { color: #74d7a4; background: rgba(57, 180, 116, .08); }
.holdout-steps b { width: 20px; height: 20px; display: inline-grid; place-items: center; flex: 0 0 auto; border: 1px solid currentColor; border-radius: 50%; }
.ablation-results .strategy-results-head > div { display: grid; gap: 3px; }
.ablation-results .strategy-results-head > div > span { color: #6db2ff; font-size: 10px; font-weight: 800; }
.ablation-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--border); }
.ablation-metrics > div { min-width: 0; padding: 9px 11px; border-right: 1px solid var(--border); }
.ablation-metrics > div:last-child { border-right: 0; }
.ablation-metrics span, .ablation-metrics strong { display: block; }
.ablation-metrics span { color: var(--muted); font-size: 11px; }
.ablation-metrics strong { margin-top: 3px; font-size: 14px; overflow-wrap: anywhere; }
.ablation-table { display: grid; border: 1px solid var(--border); }
.ablation-row { display: grid; grid-template-columns: minmax(180px, 2fr) repeat(3, minmax(72px, .7fr)); gap: 10px; align-items: center; padding: 8px 10px; border-top: 1px solid var(--border); font-size: 12px; }
.ablation-row:first-child { border-top: 0; }
.ablation-row.table-head { color: var(--muted); font-size: 11px; background: rgba(255, 255, 255, .025); }
.ablation-row strong { min-width: 0; overflow-wrap: anywhere; }
.metric-positive { color: #74d7a4; }
.metric-negative { color: #ff9f8f; }
.blind-workbench { border-top: 1px solid var(--border); padding-top: 14px; display: grid; gap: 14px; }
.blind-prompt strong, .blind-prompt span { display: block; }
.blind-prompt strong { margin-top: 3px; font-size: 16px; }
.blind-prompt > div > span { color: var(--muted); font-size: 12px; }
.blind-badge { display: inline-flex !important; align-items: center; gap: 6px; color: #74d7a4; font-size: 12px; }
.video-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.video-choice { min-width: 0; border: 1px solid var(--border); background: rgba(6, 14, 27, .32); padding: 10px; display: grid; gap: 9px; transition: border-color .18s ease, box-shadow .18s ease; }
.video-choice.selected { border-color: #4f8df5; box-shadow: inset 0 0 0 1px rgba(79, 141, 245, .45); }
.video-choice-head { display: flex; justify-content: space-between; align-items: center; }
.video-choice-head strong { width: 28px; height: 28px; display: grid; place-items: center; background: #24344c; }
.video-choice-head span { color: var(--muted); font-size: 12px; }
.video-choice video { width: 100%; aspect-ratio: 9 / 12; max-height: 420px; object-fit: contain; background: #02060d; }
.video-choice button { justify-content: center; min-height: 42px; display: inline-flex; align-items: center; gap: 7px; }
.video-choice.selected button { color: white; border-color: #4f8df5; background: rgba(79, 141, 245, .18); }
.review-controls { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.choice-secondary, .confidence-control { display: flex; align-items: center; gap: 6px; }
.choice-secondary button, .confidence-control button, .reason-tags button { min-height: 34px; padding: 6px 10px; font-size: 12px; }
.choice-secondary button.active, .confidence-control button.active, .reason-tags button.active { border-color: #56a4ff; color: #a9d3ff; background: rgba(63, 139, 232, .13); }
.confidence-control > span { color: var(--muted); font-size: 12px; margin-right: 3px; }
.reason-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.review-submit { border-top: 1px solid var(--border); padding-top: 12px; }
.review-submit > span { color: var(--muted); font-size: 12px; }
.strategy-results { border-top: 1px solid var(--border); padding-top: 14px; display: grid; gap: 10px; }
.strategy-results-head span { color: var(--muted); font-size: 12px; }
.strategy-table { display: grid; border: 1px solid var(--border); }
.strategy-row { display: grid; grid-template-columns: minmax(150px, 1.6fr) repeat(3, minmax(80px, 1fr)); gap: 10px; align-items: center; padding: 9px 12px; border-top: 1px solid var(--border); }
.strategy-row:first-child { border-top: 0; }
.strategy-row.table-head { color: var(--muted); font-size: 11px; background: rgba(255, 255, 255, .025); }
.gate-line { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
@media (max-width: 900px) {
  .vector-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .cloud-chain-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ablation-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .cloud-chain-metrics > div { border-bottom: 1px solid var(--border); }
  .vector-metrics > div { border-bottom: 1px solid var(--border); }
  .video-pair { grid-template-columns: 1fr; }
  .video-choice video { aspect-ratio: 16 / 10; max-height: 360px; }
}
@media (max-width: 560px) {
  .vector-header, .blind-prompt, .review-submit { align-items: flex-start; }
  .blind-prompt, .review-submit { flex-direction: column; }
  .vector-metrics { grid-template-columns: 1fr; }
  .ablation-metrics { grid-template-columns: 1fr; }
  .ablation-row { grid-template-columns: minmax(100px, 1.4fr) repeat(3, minmax(54px, .7fr)); gap: 6px; font-size: 10px; padding-inline: 7px; }
  .strategy-row { grid-template-columns: minmax(110px, 1.4fr) repeat(3, minmax(58px, .8fr)); font-size: 11px; padding-inline: 8px; }
}
</style>
