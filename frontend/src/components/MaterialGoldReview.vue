<template>
  <section id="material-gold-review" class="material-focus-review">
    <div class="material-flow-head">
      <div>
        <span class="section-kicker">Beta-D-9 / Material Taxonomy</span>
        <h4>素材形态人工确认</h4>
      </div>
      <div class="material-head-actions">
        <button class="icon-only" type="button" title="刷新审核队列" aria-label="刷新审核队列" :disabled="state.busyKey === 'material-gold-refresh'" @click="withBusy('material-gold-refresh', loadMaterialGoldQueue)">
          <span v-if="state.busyKey === 'material-gold-refresh'" class="spinner"></span>
          <Icon v-else name="refresh-cw" />
        </button>
        <button type="button" :disabled="state.busyKey === 'material-gold-replay'" @click="withBusy('material-gold-replay', runMaterialCalibrationReplay)">
          <span v-if="state.busyKey === 'material-gold-replay'" class="spinner"></span>
          <Icon v-else name="radar" />回放 v2.9
        </button>
      </div>
    </div>

    <ol class="material-stepper" aria-label="素材形态审核步骤">
      <li class="done"><span>1</span><strong>选择样本</strong><em>{{ pendingCount }} 条待确认</em></li>
      <li :class="{ active: Boolean(currentSample), done: confirmedCount > 0 }"><span>2</span><strong>核对四项字段</strong><em>不确定时选未知</em></li>
      <li :class="{ active: confirmedCount >= 12, done: replayReady }"><span>3</span><strong>保存并回放</strong><em>{{ confirmedCount >= 12 ? '已达到试标线' : `还差 ${Math.max(0, 12 - confirmedCount)} 条` }}</em></li>
    </ol>

    <div class="material-progress-row">
      <div class="material-progress-copy">
        <span>首轮目标</span>
        <strong>{{ confirmedCount }}/60</strong>
      </div>
      <div class="material-progress-track" role="progressbar" :aria-valuenow="confirmedCount" aria-valuemin="0" aria-valuemax="60">
        <span :style="{ width: `${progress}%` }"></span>
      </div>
      <span class="status" :class="confirmedCount >= 12 ? 'ok' : 'neutral'">{{ confirmedCount >= 12 ? '可开始回放' : '试标中' }}</span>
    </div>

    <div v-if="collapsedDuplicateCount" class="quality-row material-duplicate-summary">
      <Icon name="copy" />
      <span>已折叠 {{ collapsedDuplicateCount }} 条同账号同标题变体，避免重复占用 Gold Set 名额。</span>
    </div>

    <div v-if="replayText" class="quality-row material-replay-summary">
      <span class="status" :class="replayClass">v2.9</span>
      <span>{{ replayText }}</span>
    </div>

    <details v-if="taxonomyMismatchSamples.length" class="material-confirmed-details">
      <summary>真正错判 {{ taxonomyMismatchSamples.length }} 条（另有 {{ coarseMatchCount }} 条仅缺细粒度）</summary>
      <div class="material-confirmed-list">
        <div v-for="item in taxonomyMismatchSamples" :key="`taxonomy-mismatch-${String(item.sample_id || '')}`">
          <span>{{ clipText(String(item.title || item.sample_id || ''), 42) }}</span>
          <em>{{ valueLabel('material_type', item.gold_material_type) }} → {{ valueLabel('material_type', item.omni_material_type) }}</em>
        </div>
      </div>
    </details>

    <div v-if="currentSample" class="material-review-workspace">
      <aside class="material-queue-rail" aria-label="待审核样本队列">
        <div class="material-queue-title">
          <strong>待审核</strong>
          <span>{{ activeIndex + 1 }}/{{ samples.length }}</span>
        </div>
        <button
          v-for="(sample, index) in samples.slice(0, 8)"
          :key="sampleKey(sample)"
          type="button"
          :class="{ active: index === activeIndex }"
          :aria-current="index === activeIndex ? 'true' : undefined"
          @click="activeIndex = index"
        >
          <span>{{ index + 1 }}</span>
          <div>
            <strong>{{ clipText(sample.title || sampleKey(sample), 30) }}</strong>
            <em>{{ sample.account_id || 'unknown' }} / {{ performanceLabel(sample.performance_label) }}</em>
          </div>
        </button>
      </aside>

      <article class="material-review-editor">
        <header class="material-sample-head">
          <div>
            <div class="material-sample-meta">
              <span class="status" :class="performanceClass(currentSample.performance_label)">{{ performanceLabel(currentSample.performance_label) }}</span>
              <span>{{ currentSample.account_id || 'unknown' }}</span>
              <span>优先级 {{ Number(currentSample.priority_score || 0).toFixed(1) }}</span>
              <span v-if="currentSample.material_conflict" class="material-conflict">字段冲突</span>
              <span v-if="Number(currentSample.collapsed_variant_count || 0) > 0" class="material-duplicate">同标题变体 {{ currentSample.duplicate_group_size }} 条，已折叠</span>
            </div>
            <h5>{{ currentSample.title || sampleKey(currentSample) }}</h5>
          </div>
          <a v-if="currentSample.platform_url" class="button-link" :href="currentSample.platform_url" target="_blank" rel="noreferrer"><Icon name="external-link" />原视频</a>
        </header>

        <div class="material-model-evidence">
          <span>Omni 原判</span>
          <strong>{{ valueLabel('domain_category', currentSample.domain_category) }}</strong>
          <strong>{{ valueLabel('material_type', currentSample.material_type) }}</strong>
          <strong>{{ valueLabel('presentation_style', currentSample.presentation_style) }}</strong>
          <em>调分 {{ signedNumber(currentSample.score_delta_vs_v2_4) }}</em>
        </div>

        <form v-if="currentDraft" class="material-focused-form" @submit.prevent="saveCurrent">
          <label v-for="field in selectFields" :key="`${sampleKey(currentSample)}-${field}`">
            <span>
              {{ fieldName(field) }}
              <Icon name="circle-help" :title="fieldDescription(field)" />
            </span>
            <select v-model="currentDraft[field]">
              <option v-for="option in fieldOptions(field)" :key="option.value" :value="option.value">{{ option.label }}</option>
            </select>
            <em>Omni：{{ valueLabel(field, currentSample[field]) }}</em>
          </label>
          <label>
            <span>节目语境 <Icon name="circle-help" :title="fieldDescription('program_context')" /></span>
            <input v-model="currentDraft.program_context" type="text" placeholder="unknown" />
            <em>Omni：{{ currentSample.program_context || 'unknown' }}</em>
          </label>
          <label class="material-note-field">
            <span>审核备注</span>
            <input v-model="currentDraft.review_note" type="text" placeholder="可选：记录判断依据或争议点" />
          </label>

          <footer class="material-editor-actions">
            <div class="material-pager">
              <button class="icon-only" type="button" title="上一条" aria-label="上一条" :disabled="activeIndex <= 0" @click="previousSample"><Icon name="chevron-left" /></button>
              <button class="icon-only" type="button" title="下一条" aria-label="下一条" :disabled="activeIndex >= samples.length - 1" @click="nextSample"><Icon name="chevron-right" /></button>
              <span>不确定的字段保留“未知”</span>
            </div>
            <button class="primary" type="submit" :disabled="state.busyKey === saveBusyKey">
              <span v-if="state.busyKey === saveBusyKey" class="spinner"></span>
              <Icon v-else name="check" />保存并进入下一条
            </button>
          </footer>
        </form>
      </article>
    </div>

    <div v-else class="material-review-empty">
      <Icon name="badge-check" />
      <strong>{{ emptyTitle }}</strong>
      <span>{{ emptyText }}</span>
      <button v-if="queueStatus === 'needs_backtest'" type="button" @click="withBusy('material-gold-replay', runMaterialCalibrationReplay)"><Icon name="radar" />生成审核队列</button>
    </div>

    <details v-if="recentlyConfirmed.length" class="material-confirmed-details">
      <summary>最近已确认 {{ recentlyConfirmed.length }} 条</summary>
      <div class="material-confirmed-list">
        <div v-for="item in recentlyConfirmed" :key="`material-confirmed-${item.sample_id}`">
          <span>{{ item.account_id || 'unknown' }} / {{ valueLabel('material_type', item.material_type) }}</span>
          <button type="button" :disabled="state.busyKey === `material-gold-reopen-${item.sample_id}`" @click="withBusy(`material-gold-reopen-${item.sample_id}`, () => reopenMaterialGoldAnnotation(String(item.sample_id || '')))">
            <Icon name="refresh-ccw" />重新审核
          </button>
        </div>
      </div>
    </details>

    <section class="material-confusion-panel" aria-labelledby="material-confusion-title">
      <header class="material-confusion-head">
        <div>
          <span class="section-kicker">Beta-D-10A / Confusion Queue</span>
          <h5 id="material-confusion-title">定向错判队列</h5>
        </div>
        <button class="icon-only" type="button" title="刷新定向错判队列" aria-label="刷新定向错判队列" :disabled="state.busyKey === 'material-confusion-refresh'" @click="withBusy('material-confusion-refresh', loadMaterialConfusionQueue)">
          <span v-if="state.busyKey === 'material-confusion-refresh'" class="spinner"></span>
          <Icon v-else name="refresh-cw" />
        </button>
      </header>

      <div class="material-confusion-stats">
        <span><strong>{{ confusionSamples.length }}</strong> 定向样本</span>
        <span><strong>{{ Number(confusionSummary.account_count || 0) }}</strong> 个账号</span>
        <span><strong>{{ percent(Number(confusionSummary.local_media_ready_rate || 0)) }}</strong> 媒体就绪</span>
        <span><strong>{{ Number(knownConfusions.severe_mismatch_count || 0) }}</strong> 已知严重错判</span>
      </div>

      <div class="material-confusion-tabs" role="tablist" aria-label="错判类型">
        <button type="button" :class="{ active: selectedConfusionPair === 'all' }" @click="selectedConfusionPair = 'all'">全部 {{ confusionSamples.length }}</button>
        <button
          v-for="pair in confusionPairs"
          :key="String(pair.key || '')"
          type="button"
          :class="{ active: selectedConfusionPair === String(pair.key || '') }"
          @click="selectedConfusionPair = String(pair.key || '')"
        >
          {{ String(pair.label_zh || pair.key || '') }} {{ confusionPairCount(String(pair.key || '')) }}
        </button>
      </div>

      <div v-if="visibleConfusionSamples.length" class="material-confusion-list">
        <article v-for="item in visibleConfusionSamples" :key="`material-confusion-${String(item.sample_id || '')}`">
          <div class="material-confusion-row-main">
            <div class="material-sample-meta">
              <span>{{ item.confusion_pair_label_zh || item.confusion_pair }}</span>
              <span>{{ item.account_id || 'unknown' }}</span>
              <span>优先级 {{ Number(item.priority_score || 0).toFixed(1) }}</span>
            </div>
            <strong>{{ item.title || item.sample_id }}</strong>
            <div class="material-confusion-evidence">
              <span>Omni {{ valueLabel('material_type', item.omni_raw_material_type) }}</span>
              <span>规范 {{ valueLabel('material_type', item.omni_canonical_material_type) }}</span>
              <span v-if="confusionCueText(item)">{{ confusionCueText(item) }}</span>
            </div>
          </div>
          <div class="material-confusion-row-actions">
            <span v-if="evidenceFor(item).status && evidenceFor(item).status !== 'missing'" class="status" :class="evidenceFor(item).status === 'ready' ? 'ok' : 'neutral'">
              {{ evidenceFor(item).status === 'ready' ? '证据就绪' : '证据部分' }}
            </span>
            <span class="status" :class="item.assets?.ready_for_evidence ? 'ok' : 'neutral'">{{ item.assets?.ready_for_evidence ? '媒体就绪' : '仅文本' }}</span>
            <a v-if="item.platform_url" class="icon-only" :href="item.platform_url" target="_blank" rel="noreferrer" title="打开原视频" aria-label="打开原视频"><Icon name="external-link" /></a>
          </div>
        </article>
      </div>
      <div v-else class="material-review-empty compact">
        <Icon name="badge-check" />
        <strong>当前筛选暂无样本</strong>
      </div>

      <section class="material-evidence-panel" aria-labelledby="material-evidence-title">
        <header class="material-evidence-head">
          <div>
            <span class="section-kicker">Beta-D-10B / Evidence Resolver</span>
            <h5 id="material-evidence-title">三窗口证据与 Resolver Shadow</h5>
          </div>
          <div class="material-evidence-actions">
            <button type="button" :disabled="state.busyKey === 'material-evidence-smoke'" @click="withBusy('material-evidence-smoke', runMaterialEvidenceSmoke)">
              <span v-if="state.busyKey === 'material-evidence-smoke'" class="spinner"></span>
              <Icon v-else name="scan-search" />3 条 Smoke
            </button>
            <button type="button" :disabled="state.busyKey === 'material-resolver-shadow'" @click="withBusy('material-resolver-shadow', runMaterialResolverShadow)">
              <span v-if="state.busyKey === 'material-resolver-shadow'" class="spinner"></span>
              <Icon v-else name="route" />Resolver 回放
            </button>
          </div>
        </header>

        <div class="material-evidence-stats">
          <span><strong>{{ Number(evidenceSummary.evidence_ready_count || 0) }}</strong>/{{ Number(evidenceSummary.selected_count || 0) }} 证据就绪</span>
          <span><strong>{{ Number(evidenceSummary.multi_window_ready_count || 0) }}</strong> 多窗口</span>
          <span><strong>{{ Number(evidenceSummary.asr_ready_count || 0) }}</strong> ASR</span>
          <span><strong>{{ Number(evidenceSummary.ocr_ready_count || 0) }}</strong> OCR</span>
          <span><strong>{{ Number(evidenceSummary.audio_source_count || 0) }}</strong> 有音轨</span>
        </div>

        <div v-if="resolverStrategyRows.length" class="material-resolver-grid">
          <article v-for="strategy in resolverStrategyRows" :key="strategy.key">
            <strong>{{ strategy.label }}</strong>
            <span>覆盖 {{ percent(strategy.coverage) }}</span>
            <span v-if="strategy.goldCount">规范准确 {{ percent(strategy.accuracy) }}</span>
            <span v-else>等待第二批 Gold</span>
          </article>
        </div>

        <div class="quality-row material-evidence-gate">
          <span class="status" :class="resolverReport?.status === 'eligible_for_ranker_ablation' ? 'ok' : 'neutral'">{{ resolverReport?.status || 'not_started' }}</span>
          <span>分歧 {{ Number(resolverSummary.disagreement_count || latestResolverSummary.disagreement_count || 0) }} / Gold 证据 {{ Number(resolverSummary.cached_gold_evaluable_count || latestResolverSummary.cached_gold_evaluable_count || 0) }}/{{ Number(resolverSummary.gold_evaluable_count || latestResolverSummary.gold_evaluable_count || 0) }}</span>
        </div>
      </section>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { AnnotationFieldGuide, MaterialConfusionSample, MaterialEvidenceSample, MaterialGoldAnnotation, MaterialGoldSample } from "../types";
import { clipText } from "../utils";
import Icon from "./Icon.vue";

const {
  state,
  loadMaterialGoldQueue,
  loadMaterialConfusionQueue,
  runMaterialEvidenceSmoke,
  runMaterialResolverShadow,
  saveMaterialGoldAnnotation,
  reopenMaterialGoldAnnotation,
  runMaterialCalibrationReplay,
  withBusy
} = useDashboardContext();

const activeIndex = ref(0);
const selectedConfusionPair = ref("all");
const selectFields = ["domain_category", "material_type", "presentation_style"] as const;
const queue = computed(() => state.materialGoldQueue || {});
const samples = computed<MaterialGoldSample[]>(() => Array.isArray(queue.value.samples) ? queue.value.samples || [] : []);
const recentlyConfirmed = computed<MaterialGoldAnnotation[]>(() => Array.isArray(queue.value.recently_confirmed_samples) ? queue.value.recently_confirmed_samples || [] : []);
const summary = computed<Record<string, unknown>>(() => queue.value.batch_summary && typeof queue.value.batch_summary === "object" ? queue.value.batch_summary as Record<string, unknown> : {});
const pendingCount = computed(() => Number(summary.value.pending_count ?? queue.value.total_candidates ?? samples.value.length));
const confirmedCount = computed(() => Number(summary.value.confirmed_count || 0));
const collapsedDuplicateCount = computed(() => Number(summary.value.collapsed_duplicate_count || 0));
const progress = computed(() => Math.max(0, Math.min(100, confirmedCount.value / 60 * 100)));
const queueStatus = computed(() => String(queue.value.status || ""));
const currentSample = computed(() => samples.value[activeIndex.value] || samples.value[0] || null);
const currentDraft = computed(() => currentSample.value ? state.materialGoldDrafts[sampleKey(currentSample.value)] || null : null);
const saveBusyKey = computed(() => currentSample.value ? `material-gold-save-${sampleKey(currentSample.value)}` : "material-gold-save");

const replayMetrics = computed<Record<string, unknown>>(() => state.materialCalibrationReplay?.metrics || {});
const replayQuality = computed<Record<string, unknown>>(() => {
  const value = replayMetrics.value.omni_material_calibration;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const replayAuditQuality = computed<Record<string, unknown>>(() => {
  const value = replayMetrics.value.omni_material_calibration_holdout;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const replayGoldSplit = computed<Record<string, unknown>>(() => {
  const value = replayMetrics.value.omni_material_gold_split;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const replayGate = computed<Record<string, unknown>>(() => {
  const value = replayMetrics.value.omni_material_v29_gate || replayMetrics.value.omni_material_v28_gate;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const taxonomyMismatchSamples = computed<Record<string, unknown>[]>(() => {
  const value = replayAuditQuality.value.taxonomy_mismatch_samples;
  return Array.isArray(value) ? value.filter(item => item && typeof item === "object") as Record<string, unknown>[] : [];
});
const coarseMatchCount = computed(() => {
  const relations = replayAuditQuality.value.taxonomy_relation_counts;
  return relations && typeof relations === "object" ? Number((relations as Record<string, unknown>).coarse_match || 0) : 0;
});
const confusionQueue = computed(() => state.materialConfusionQueue || {});
const confusionSummary = computed<Record<string, unknown>>(() => confusionQueue.value.batch_summary && typeof confusionQueue.value.batch_summary === "object" ? confusionQueue.value.batch_summary as Record<string, unknown> : {});
const knownConfusions = computed<Record<string, unknown>>(() => confusionSummary.value.known_gold_confusions && typeof confusionSummary.value.known_gold_confusions === "object" ? confusionSummary.value.known_gold_confusions as Record<string, unknown> : {});
const confusionSamples = computed<MaterialConfusionSample[]>(() => Array.isArray(confusionQueue.value.samples) ? confusionQueue.value.samples || [] : []);
const confusionPairs = computed<Record<string, unknown>[]>(() => Array.isArray(confusionQueue.value.confusion_pairs) ? confusionQueue.value.confusion_pairs.filter(item => item && typeof item === "object") as Record<string, unknown>[] : []);
const evidenceStatus = computed(() => state.materialEvidenceStatus || {});
const evidenceSummary = computed<Record<string, unknown>>(() => evidenceStatus.value.batch_summary && typeof evidenceStatus.value.batch_summary === "object" ? evidenceStatus.value.batch_summary as Record<string, unknown> : {});
const evidenceSamples = computed<MaterialEvidenceSample[]>(() => Array.isArray(evidenceStatus.value.samples) ? evidenceStatus.value.samples || [] : []);
const resolverReport = computed(() => state.materialResolverReport);
const resolverSummary = computed<Record<string, unknown>>(() => resolverReport.value?.summary && typeof resolverReport.value.summary === "object" ? resolverReport.value.summary as Record<string, unknown> : {});
const latestResolverSummary = computed<Record<string, unknown>>(() => evidenceStatus.value.latest_resolver_summary && typeof evidenceStatus.value.latest_resolver_summary === "object" ? evidenceStatus.value.latest_resolver_summary as Record<string, unknown> : {});
const resolverStrategyRows = computed(() => {
  const comparison = resolverReport.value?.cached_eval_strategy_comparison || resolverReport.value?.strategy_comparison || {};
  const labels: Record<string, string> = {
    title_only: "Title only",
    omni_only: "Omni 单窗",
    asr_ocr: "ASR + OCR",
    multi_window: "多窗口 Resolver"
  };
  return Object.keys(labels).filter(key => comparison[key]).map(key => ({
    key,
    label: labels[key],
    coverage: Number(comparison[key]?.coverage || 0),
    accuracy: Number(comparison[key]?.canonical_accuracy || 0),
    goldCount: Number(comparison[key]?.gold_evaluable_count || 0)
  }));
});
const visibleConfusionSamples = computed(() => confusionSamples.value
  .filter(item => selectedConfusionPair.value === "all" || item.confusion_pair === selectedConfusionPair.value)
  .slice(0, 12));
const replayTop30 = computed<Record<string, unknown>>(() => {
  const report = replayMetrics.value.omni_material_v29_report || replayMetrics.value.omni_material_v28_report;
  if (!report || typeof report !== "object") return {};
  const topk = (report as Record<string, unknown>).topk;
  const row = topk && typeof topk === "object" ? (topk as Record<string, unknown>)["30"] : null;
  return row && typeof row === "object" ? row as Record<string, unknown> : {};
});
const replayText = computed(() => {
  if (!Object.keys(replayQuality.value).length && !Object.keys(replayTop30.value).length) return "";
  const auditQuality = Object.keys(replayAuditQuality.value).length ? replayAuditQuality.value : replayQuality.value;
  const splitText = Object.keys(replayGoldSplit.value).length
    ? ` / 校准 ${Number(replayGoldSplit.value.calibration_count || 0)} + 独立审计 ${Number(replayGoldSplit.value.audit_count || 0)}`
    : "";
  const lift = Number(replayTop30.value.v2_9_lift_delta_vs_v2_4 ?? replayTop30.value.v2_8_lift_delta_vs_v2_4 ?? 0);
  const highHit = Number(replayTop30.value.v2_9_high_hit_delta_vs_v2_4 ?? replayTop30.value.v2_8_high_hit_delta_vs_v2_4 ?? 0);
  return `Gold ${Number(replayQuality.value.confirmed_count || 0)}（有效 ${Number(replayQuality.value.effective_unique_count ?? replayQuality.value.confirmed_count ?? 0)}）${splitText} / 严格 ${percent(Number(auditQuality.material_type_accuracy || 0))} / 规范形态 ${percent(Number(auditQuality.canonical_material_type_accuracy ?? auditQuality.material_type_accuracy ?? 0))} / Top30 lift ${signedNumber(lift)} / 高互动 ${signedNumber(highHit)} / ${String(replayGate.value.status || 'research_only')}`;
});
const replayClass = computed(() => {
  return replayGate.value.research_gate_passed === true ? "ok" : "neutral";
});
const replayReady = computed(() => Boolean(state.materialCalibrationReplay));
const emptyTitle = computed(() => queueStatus.value === "complete" ? "本批素材审核已完成" : "暂无审核样本");
const emptyText = computed(() => queueStatus.value === "complete" ? "运行 v2.9 回放，对照严格标签、规范形态和排序变化。" : "生成回放后会自动创建高影响审核队列。");

watch(() => samples.value.length, (length) => {
  if (!length) activeIndex.value = 0;
  else if (activeIndex.value >= length) activeIndex.value = length - 1;
});

function sampleKey(sample: MaterialGoldSample): string {
  return String(sample.sample_id || sample.id || "");
}

function confusionPairCount(pair: string): number {
  return confusionSamples.value.filter(item => item.confusion_pair === pair).length;
}

function confusionCueText(sample: MaterialConfusionSample): string {
  const left = Array.isArray(sample.cue_evidence?.left_hits) ? sample.cue_evidence?.left_hits || [] : [];
  const right = Array.isArray(sample.cue_evidence?.right_hits) ? sample.cue_evidence?.right_hits || [] : [];
  return [...left, ...right].slice(0, 4).join(" / ");
}

function evidenceFor(sample: MaterialConfusionSample): MaterialEvidenceSample {
  const id = String(sample.sample_id || sample.id || "");
  return evidenceSamples.value.find(item => String(item.sample_id || "") === id) || { status: "missing" };
}

function fieldGuide(field: string): AnnotationFieldGuide | null {
  const guides = queue.value.annotation_field_guides;
  const value = guides && typeof guides === "object" ? guides[field] : null;
  return value || null;
}

function fieldName(field: string): string {
  const guide = fieldGuide(field);
  return String(guide?.label_zh || guide?.short_label_zh || field);
}

function fieldDescription(field: string): string {
  const guide = fieldGuide(field);
  return [guide?.description_zh, guide?.annotation_hint_zh].map(item => String(item || "").trim()).filter(Boolean).join(" ");
}

function fieldOptions(field: string): Array<{ value: string; label: string }> {
  const values = fieldGuide(field)?.allowed_values;
  if (!Array.isArray(values) || !values.length) return [{ value: "unknown", label: "未知 / unknown" }];
  return values.map(item => {
    const value = String(item.value || "unknown");
    return { value, label: `${String(item.label_zh || item.label || value)} / ${value}` };
  });
}

function valueLabel(field: string, value?: unknown): string {
  const normalized = String(value || "unknown");
  const option = fieldOptions(field).find(item => item.value === normalized);
  return option ? option.label.split(" / ")[0] : normalized;
}

function signedNumber(value?: unknown): string {
  const number = Number(value || 0);
  return `${number >= 0 ? "+" : ""}${number.toFixed(3)}`;
}

function percent(value?: number): string {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function performanceLabel(value?: unknown): string {
  if (value === "high") return "高互动";
  if (value === "low") return "低互动";
  if (value === "mid") return "中位";
  return "待标注";
}

function performanceClass(value?: unknown): string {
  if (value === "high") return "ok";
  if (value === "low") return "warn";
  return "neutral";
}

function previousSample(): void {
  activeIndex.value = Math.max(0, activeIndex.value - 1);
}

function nextSample(): void {
  activeIndex.value = Math.min(samples.value.length - 1, activeIndex.value + 1);
}

async function saveCurrent(): Promise<void> {
  if (!currentSample.value) return;
  await saveMaterialGoldAnnotation(sampleKey(currentSample.value));
}
</script>
