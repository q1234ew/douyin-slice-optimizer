<template>
  <section id="candidates" class="panel candidate-workbench">
    <div class="panel-head">
      <div>
        <h2 class="panel-title"><Icon name="list-video" />候选列表</h2>
        <p id="candidate-source" class="panel-subtitle">{{ selectedVideo ? `来自：${selectedVideo.title}` : "未选择节目" }}</p>
      </div>
      <select id="candidate-video-select" :value="state.selectedVideoId" aria-label="候选节目" @change="changeVideo">
        <option v-for="video in state.videos" :key="video.id" :value="video.id">{{ video.title }}</option>
      </select>
    </div>

    <QualitySentinel compact />

    <div id="candidate-review-brief" class="review-brief" aria-live="polite">
      <div class="review-brief-main">
        <strong>{{ clipText(selectedTitle, 44) }}</strong>
        <span>按混合分从上到下复核：先看时间轴信号、Omni 多窗口证据和历史先验，再决定人工通过或导出预览。</span>
      </div>
      <div class="review-brief-stat"><span>Top 候选</span><strong>{{ rows.length }}</strong></div>
      <div class="review-brief-stat"><span>待导出</span><strong>{{ pendingExport }}</strong></div>
      <div class="review-brief-stat"><span>已导出</span><strong>{{ exported }}</strong></div>
      <div class="review-brief-stat"><span>风险复核</span><strong>{{ blocked }}</strong></div>
      <div class="review-brief-actions">
        <button v-if="selectedSegment && !selectedSegment.latest_export?.export_url" type="button" class="primary" :data-export-segment="selectedSegment.id" @click="exportSelected">
          <Icon name="download" />导出选中
        </button>
        <button v-else-if="exported" type="button" class="primary" data-guide-action="feedback" @click="handleGuideAction('feedback')">
          <Icon name="database" />导入表现数据
        </button>
        <button v-else type="button" class="primary" data-guide-action="process" @click="handleGuideAction('process')">
          <Icon name="wand-sparkles" />处理选中
        </button>
        <button v-if="selectedSegment" type="button" data-guide-action="candidate:history" @click="handleGuideAction('candidate:history')"><Icon name="history" />判断依据</button>
        <button v-else type="button" data-guide-action="candidates" @click="handleGuideAction('candidates')"><Icon name="clipboard-check" />开始复核</button>
      </div>
    </div>

    <div id="candidate-list" class="candidate-list">
      <div v-if="state.moduleStatus.suggestions.error" class="module-error">
        <Icon name="circle-alert" /><span>{{ state.moduleStatus.suggestions.error }}</span>
      </div>
      <div v-if="!rows.length" class="empty">
        <Icon name="list-video" /><strong>暂无候选片段</strong><span>选择节目后点击“处理选中”，系统会生成 Top 候选、历史先验和候选审核入口。</span>
      </div>
      <article
        v-for="(row, index) in rows"
        :key="row.id"
        class="candidate-card"
        :class="{ selected: row.id === state.selectedSegmentId }"
        :data-segment-id="row.id"
      >
        <button class="candidate-select" type="button" :aria-label="`查看候选 ${index + 1}：${firstTitle(row)}`" @click="selectCandidate(row.id)">
          <span class="rank">{{ index + 1 }}</span>
          <span class="candidate-main">
            <span class="candidate-time">{{ fmtTimeRange(row) }}</span>
            <strong class="candidate-title">{{ firstTitle(row) }}</strong>
            <span class="candidate-copy">{{ clipText(row.score_explanation || row.short_video_structure || row.summary, 82) }}</span>
            <span class="candidate-model-meta" :class="omniTone(row)">{{ omniLabel(row) }}</span>
          </span>
        </button>
        <div class="candidate-score">
          <strong>{{ displayScore(row).toFixed(1) }}</strong>
          <span>{{ row.omni_status === "ready" ? "混合分" : "综合分" }}</span>
          <div class="score-meter"><span :style="{ width: `${Math.max(0, Math.min(100, displayScore(row)))}%` }"></span></div>
        </div>
        <div class="candidate-status">
          <span class="status" :class="cardStatus(row).tone">{{ cardStatus(row).label }}</span>
          <span v-if="qualityFlagsFor(row.id).length" class="candidate-risk-note">{{ clipText(qualityFlagsFor(row.id)[0], 24) }}</span>
        </div>
        <div class="candidate-actions">
          <button type="button" :class="{ primary: row.id === state.selectedSegmentId }" @click="selectCandidate(row.id, row.latest_export || null)">
            <Icon :name="row.latest_export?.export_url ? 'play' : 'panel-right-open'" />{{ row.latest_export?.export_url ? "查看预览" : row.id === state.selectedSegmentId ? "正在复核" : "查看详情" }}
          </button>
        </div>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Icon from "./Icon.vue";
import QualitySentinel from "./QualitySentinel.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { CandidateRow } from "../types";
import { clipText, fmtTimeRange, reviewStatusClass } from "../utils";

const {
  state,
  selectedVideo,
  selectedSegment,
  loadSuggestions,
  selectCandidate,
  handleGuideAction,
  exportSegment,
  qualityFlagsFor,
  withBusy,
  toast
} = useDashboardContext();

const rows = computed(() => state.suggestions || []);
const exported = computed(() => rows.value.filter(row => row.review_status === "exported" || row.latest_export?.export_url).length);
const blocked = computed(() => rows.value.filter(row => row.review_status === "blocked" || qualityFlagsFor(row.id).length).length);
const pendingExport = computed(() => Math.max(0, rows.value.length - exported.value));
const selectedTitle = computed(() => selectedSegment.value ? firstTitle(selectedSegment.value) : "等待候选");

function firstTitle(row: CandidateRow): string {
  const titles = Array.isArray(row.title_suggestions) ? row.title_suggestions : [];
  return titles[0] || row.summary || "候选片段";
}

function displayScore(row: CandidateRow): number {
  return Number(row.hybrid_score || row.ranker_score || row.final_score || 0);
}

function omniLabel(row: CandidateRow): string {
  if (row.omni_status === "ready") {
    const analysis = row.omni_analysis && typeof row.omni_analysis === "object" ? row.omni_analysis : {};
    const windows = Number(analysis.window_count || 0);
    return `Omni ${windows || 1} 窗复排 · 置信 ${Math.round(Number(row.omni_confidence || 0) * 100)}%`;
  }
  if (String(row.omni_status || "").startsWith("fallback_")) return "Omni 未就绪 · 已自动规则回退";
  if (row.omni_status === "not_selected") return "规则预排 · 未进入 Omni 池";
  if (row.omni_status === "error") return "Omni 单条失败 · 已自动回退";
  return `多信号边界 · 置信 ${Math.round(Number(row.boundary_confidence || 0) * 100)}%`;
}

function omniTone(row: CandidateRow): string {
  if (row.omni_status === "ready") return "ready";
  if (row.omni_status === "error") return "error";
  return "fallback";
}

function cardStatus(row: CandidateRow): { label: string; tone: string } {
  if (qualityFlagsFor(row.id).length) return { label: "质量复核", tone: "warn" };
  if (row.latest_export?.export_url || row.review_status === "exported") return { label: "已导出", tone: "ok" };
  if (row.review_status === "approved") return { label: "已通过", tone: "ok" };
  if (row.review_status === "blocked") return { label: "暂缓", tone: "risk" };
  return { label: row.review_status_label || "待审核", tone: reviewStatusClass(row.review_status) };
}

function changeVideo(event: Event): void {
  const target = event.target as HTMLSelectElement;
  loadSuggestions(target.value).catch(error => toast(error.message));
}

async function exportRow(segmentId: string): Promise<void> {
  await withBusy(`export-${segmentId}`, () => exportSegment(segmentId));
}

async function exportSelected(): Promise<void> {
  if (!selectedSegment.value) return;
  await exportRow(selectedSegment.value.id);
}
</script>
