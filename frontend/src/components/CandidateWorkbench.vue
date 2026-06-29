<template>
  <section id="candidates" class="panel candidate-workbench">
    <div class="panel-head">
      <div>
        <h2 class="panel-title"><Icon name="list-video" />候选审核</h2>
        <p id="candidate-source" class="panel-subtitle">{{ selectedVideo ? `来自：${selectedVideo.title}` : "未选择节目" }}</p>
      </div>
      <select id="candidate-video-select" :value="state.selectedVideoId" aria-label="候选节目" @change="changeVideo">
        <option v-for="video in state.videos" :key="video.id" :value="video.id">{{ video.title }}</option>
      </select>
    </div>

    <div id="candidate-review-brief" class="review-brief" aria-live="polite">
      <div class="review-brief-main">
        <strong>{{ clipText(selectedTitle, 44) }}</strong>
        <span>按综合分从上到下复核：先看质量与历史先验，再决定人工通过或导出预览。</span>
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
        <button v-if="selectedSegment" type="button" data-guide-action="candidate:history" @click="handleGuideAction('candidate:history')"><Icon name="history" />历史先验</button>
        <button v-else type="button" data-guide-action="simulation" @click="handleGuideAction('simulation')"><Icon name="radar" />推荐模拟</button>
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
        tabindex="0"
        role="button"
        :aria-label="`候选片段 ${index + 1}`"
        :data-segment-id="row.id"
        @click="selectCandidate(row.id)"
        @keydown.enter.prevent="selectCandidate(row.id)"
        @keydown.space.prevent="selectCandidate(row.id)"
      >
        <div class="rank">{{ index + 1 }}</div>
        <div class="candidate-main">
          <div class="candidate-time">{{ fmtTimeRange(row) }}</div>
          <div class="candidate-title">{{ firstTitle(row) }}</div>
          <div class="candidate-copy">{{ clipText(row.transcript || row.summary, 96) }}</div>
        </div>
        <div class="candidate-signal">
          <div class="meta">结构 / 爆点</div>
          <div class="candidate-copy">{{ clipText(row.short_video_structure || row.summary, 74) }}</div>
          <div class="tags">
            <span class="tag">{{ row.music_slice_type || "短视频切片" }}</span>
            <span class="tag blue">{{ row.emotion_type || "情绪" }}</span>
          </div>
        </div>
        <div class="candidate-score">
          <strong>{{ Number(row.final_score || 0).toFixed(1) }}</strong>
          <span>综合分</span>
          <div class="score-meter"><span :style="{ width: `${Math.max(0, Math.min(100, Number(row.final_score || 0)))}%` }"></span></div>
        </div>
        <div class="candidate-status">
          <span class="status" :class="reviewStatusClass(row.review_status)">{{ row.review_status_label || "待审核" }}</span>
          <div class="meta">{{ clipText(row.review_status_reason || "等待人工扫读", 36) }}</div>
          <template v-if="qualityFlagsFor(row.id).length">
            <span class="status warn">质量复核</span>
            <div class="meta">{{ clipText(qualityFlagsFor(row.id).join(" / "), 32) }}</div>
          </template>
          <span v-if="row.review_status !== 'exported'" class="status" :class="row.latest_export ? 'ok' : 'warn'">{{ row.latest_export ? "已导出" : "待导出" }}</span>
          <div class="meta">封面 {{ row.cover_time ? fmtSeconds(row.cover_time) : "-" }}</div>
        </div>
        <div class="candidate-actions" @click.stop>
          <div class="mini-actions">
            <button class="icon-only" title="字幕" aria-label="复制字幕" :data-copy="row.transcript || ''" @click="copyText(row.transcript || '')"><Icon name="captions" /></button>
            <button class="icon-only" title="标题" aria-label="复制标题建议" :data-copy="titleText(row)" @click="copyText(titleText(row))"><Icon name="text" /></button>
            <button v-if="row.latest_export?.export_url" class="icon-only" title="预览" aria-label="打开导出预览" :data-preview-segment="row.id" @click="selectCandidate(row.id, row.latest_export)"><Icon name="play" /></button>
          </div>
          <button class="primary" :data-export-segment="row.id" :disabled="Boolean(state.busyKey)" @click="exportRow(row.id)">
            <span v-if="state.busyKey === `export-${row.id}`" class="spinner"></span>
            <Icon v-else :name="row.latest_export ? 'refresh-cw' : 'download'" />{{ row.latest_export ? "重导" : "导出" }}
          </button>
        </div>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { CandidateRow } from "../types";
import { clipText, fmtSeconds, fmtTimeRange, reviewStatusClass } from "../utils";

const {
  state,
  selectedVideo,
  selectedSegment,
  loadSuggestions,
  selectCandidate,
  handleGuideAction,
  exportSegment,
  copyText,
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

function titleText(row: CandidateRow): string {
  const titles = Array.isArray(row.title_suggestions) ? row.title_suggestions : [];
  return titles.join("\n");
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
