<template>
  <section id="videos" class="panel">
    <div class="panel-head">
      <div>
        <h2 class="panel-title"><Icon :name="state.entryMode === 'precut' ? 'list-video' : 'video'" />{{ state.entryMode === "precut" ? "已切短片排名" : "完整节目切片" }}</h2>
        <p class="panel-subtitle">{{ state.entryMode === "precut" ? "批量导入、边界锁定、统一排名与审核" : "节目理解、候选召回、统一排名与审核" }}</p>
      </div>
      <div v-if="state.entryMode === 'program'" class="toolbar-actions">
        <button id="refresh-btn" type="button" :disabled="state.busyKey === 'refresh-videos'" @click="withBusy('refresh-videos', refreshVideos)">
          <span v-if="state.busyKey === 'refresh-videos'" class="spinner"></span>
          <Icon v-else name="refresh-cw" />刷新
        </button>
        <button
          id="run-selected-btn"
          class="primary"
          type="button"
          :disabled="!selectedProgramVideo || Boolean(state.busyKey) || sliceActive"
          @click="runSelected"
        >
          <span v-if="state.busyKey === 'run-selected' || selectedSliceActive" class="spinner"></span>
          <Icon v-else name="wand-sparkles" />智能切片
        </button>
      </div>
    </div>
    <div class="panel-body">
      <div class="entry-mode-tabs" role="tablist" aria-label="素材入口">
        <button type="button" role="tab" :aria-selected="state.entryMode === 'precut'" :class="{ active: state.entryMode === 'precut' }" @click="selectEntryMode('precut')">
          <Icon name="list-video" /><span><strong>已切短片</strong><small>保留原边界，批量排名</small></span>
        </button>
        <button type="button" role="tab" :aria-selected="state.entryMode === 'program'" :class="{ active: state.entryMode === 'program' }" @click="selectEntryMode('program')">
          <Icon name="scissors" /><span><strong>完整节目</strong><small>召回候选，再统一排名</small></span>
        </button>
      </div>

      <PrecutBatchWorkbench v-if="state.entryMode === 'precut'" />
      <template v-else>
      <section
        v-if="sliceProgressVisible"
        class="slice-progress-card"
        :class="`is-${state.sliceProgress.status}`"
        data-testid="slice-progress"
        role="status"
        aria-live="polite"
      >
        <div class="slice-progress-head">
          <div>
            <span class="slice-progress-kicker">{{ progressStatusLabel }} · {{ progressVideoTitle }}</span>
            <strong>{{ state.sliceProgress.stageLabel }}</strong>
            <p>{{ state.sliceProgress.detail }}</p>
          </div>
          <strong class="slice-progress-percent">{{ Math.round(state.sliceProgress.percent) }}%</strong>
        </div>
        <div
          class="slice-progress-track"
          role="progressbar"
          aria-label="智能切片进度"
          :aria-valuenow="Math.round(state.sliceProgress.percent)"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <span :style="{ width: `${state.sliceProgress.percent}%` }"></span>
        </div>
        <div class="slice-progress-stages" aria-label="智能切片处理阶段">
          <span v-for="(stage, index) in progressStages" :key="stage" :class="progressStageClass(index)">
            <b>{{ index + 1 }}</b><small>{{ stage }}</small>
          </span>
        </div>
        <div class="slice-progress-meta">
          <span><Icon name="file-clock" />已耗时 <strong>{{ formatDuration(state.sliceProgress.elapsedSeconds) }}</strong></span>
          <span><Icon name="gauge" />{{ remainingTimeLabel }}</span>
          <span>阶段 <strong>{{ displayedStage }}/{{ state.sliceProgress.totalStages }}</strong></span>
        </div>
      </section>

      <div class="workbench-focus" aria-live="polite">
        <div class="focus-next">
          <span class="meta">下一步任务</span>
          <strong>{{ workflowGuide.title }}</strong>
          <p>{{ workflowGuide.copy }}</p>
        </div>
        <div class="focus-current">
          <span class="meta">当前节目</span>
          <strong>{{ selectedProgramVideo?.title || "未选择节目" }}</strong>
          <p>{{ selectedProgramVideo ? `${selectedProgramVideo.account_id || "未设置账号"} / ${fmtSeconds(selectedProgramVideo.duration_seconds)} / ${statusLabel(selectedProgramVideo.status)}` : "从左侧导入节目，或在下方列表选择已有节目。" }}</p>
        </div>
        <button type="button" class="primary" :data-guide-action="workflowGuide.action" @click="handleGuideAction(workflowGuide.action)">
          <Icon name="arrow-right" />{{ workflowGuide.actionLabel }}
        </button>
      </div>

      <div class="stats">
        <div class="stat"><span>节目</span><strong id="stat-videos">{{ state.stats.videos }}</strong></div>
        <div class="stat"><span>候选</span><strong id="stat-segments">{{ state.stats.segments }}</strong></div>
        <div class="stat"><span>导出</span><strong id="stat-exports">{{ state.stats.exports }}</strong></div>
        <div class="stat"><span>训练样本</span><strong id="stat-samples">{{ state.stats.training_samples }}</strong></div>
      </div>

      <QualitySentinel />

      <div class="toolbar" style="margin-bottom:10px;">
        <div class="filters">
          <input id="video-search" v-model="state.videoSearch" placeholder="搜索节目名称 / 账号" aria-label="搜索节目" />
          <select id="account-filter" v-model="state.accountFilter" aria-label="账号筛选">
            <option value="">全部账号</option>
            <option v-for="account in accountOptions" :key="account" :value="account">{{ account }}</option>
          </select>
          <select id="status-filter" v-model="state.statusFilter" aria-label="状态筛选">
            <option value="">全部状态</option>
            <option v-for="status in statusOptions" :key="status" :value="status">{{ status }}</option>
          </select>
        </div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>节目名称</th>
              <th>账号</th>
              <th>时长</th>
              <th>分辨率</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="video-rows">
            <tr
              v-for="video in filteredProgramVideos"
              :key="video.id"
              class="video-row"
              :class="{ selected: video.id === state.selectedVideoId }"
              :data-video-id="video.id"
            >
              <td>
                <button type="button" class="video-select-btn" :aria-pressed="video.id === state.selectedVideoId" @click="selectVideo(video.id)">
                  <span class="video-title">{{ video.title }}</span><code>{{ video.id }}</code>
                </button>
              </td>
              <td>{{ video.account_id }}</td>
              <td>{{ fmtSeconds(video.duration_seconds) }}</td>
              <td>{{ Number(video.width || 0) }}x{{ Number(video.height || 0) }}</td>
              <td><span class="status" :class="statusClass(video.status)">{{ statusLabel(video.status) }}</span></td>
              <td>
                <div class="row-actions">
                  <button class="icon-only" title="提取" aria-label="提取节目素材" data-action="extract" :data-video-id="video.id" :disabled="Boolean(state.busyKey) || sliceActive" @click="runVideoStep(video.id, 'extract')"><Icon name="scan-line" /></button>
                  <button class="icon-only" title="生成候选" aria-label="生成节目候选" data-action="segments" :data-video-id="video.id" :disabled="Boolean(state.busyKey) || sliceActive" @click="runVideoStep(video.id, 'segments')"><Icon name="list-video" /></button>
                  <button class="icon-only" title="评分" aria-label="为节目候选评分" data-action="score" :data-video-id="video.id" :disabled="Boolean(state.busyKey) || sliceActive" @click="runVideoStep(video.id, 'score')"><Icon name="star" /></button>
                  <button class="primary" data-action="run-all" :data-video-id="video.id" :disabled="Boolean(state.busyKey) || sliceActive" @click="runWholeVideo(video.id)">
                    <span v-if="state.busyKey === `run-all-${video.id}` || isSliceActive(video.id)" class="spinner"></span>
                    <Icon v-else name="wand-sparkles" />智能切片
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="!filteredProgramVideos.length">
              <td colspan="6">
                <div class="empty"><Icon name="video" /><strong>暂无节目</strong><span>先从上方流程区导入素材，再进入候选审核。</span></div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Icon from "./Icon.vue";
import PrecutBatchWorkbench from "./PrecutBatchWorkbench.vue";
import QualitySentinel from "./QualitySentinel.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { VideoRow } from "../types";
import { fmtSeconds } from "../utils";

const {
  state,
  filteredVideos,
  accountOptions,
  statusOptions,
  refreshVideos,
  loadQuality,
  runStep,
  runAll,
  selectedVideo,
  workflowGuide,
  handleGuideAction,
  withBusy,
  toast
} = useDashboardContext();

const filteredProgramVideos = computed(() => filteredVideos.value.filter(video => video.input_mode !== "precut"));
const selectedProgramVideo = computed(() => selectedVideo.value?.input_mode === "precut" ? null : selectedVideo.value);
const progressStages = ["转写分析", "候选生成", "评分排序", "Omni 复排"];
const sliceActive = computed(() => ["running", "refining"].includes(state.sliceProgress.status));
const selectedSliceActive = computed(() => Boolean(selectedProgramVideo.value && isSliceActive(selectedProgramVideo.value.id)));
const sliceProgressVisible = computed(() => state.sliceProgress.status !== "idle" && Boolean(state.sliceProgress.videoId));
const progressVideoTitle = computed(() => state.videos.find(video => video.id === state.sliceProgress.videoId)?.title || state.sliceProgress.videoId);
const displayedStage = computed(() => state.sliceProgress.status === "completed"
  ? state.sliceProgress.totalStages
  : Math.min(state.sliceProgress.totalStages, state.sliceProgress.stageIndex + 1));
const progressStatusLabel = computed(() => ({
  running: "正在智能切片",
  refining: "候选已就绪",
  completed: "处理完成",
  failed: "处理失败",
  idle: "等待开始"
}[state.sliceProgress.status]));
const remainingTimeLabel = computed(() => {
  if (state.sliceProgress.status === "completed") return `总耗时 ${formatDuration(state.sliceProgress.elapsedSeconds)}`;
  if (state.sliceProgress.status === "failed") return "已停止，可检查提示后重试";
  if (state.sliceProgress.status === "refining") return "剩余时间由 GPU 队列决定";
  const remaining = state.sliceProgress.estimatedRemainingSeconds;
  return remaining == null ? "正在估算剩余时间" : `预计剩余 ${formatDuration(remaining)}`;
});

function selectEntryMode(mode: "precut" | "program"): void {
  state.entryMode = mode;
  if (mode === "program" && !selectedProgramVideo.value && filteredProgramVideos.value.length) {
    state.selectedVideoId = filteredProgramVideos.value[0].id;
  }
}

function isSliceActive(videoId: string): boolean {
  return state.sliceProgress.videoId === videoId && sliceActive.value;
}

function progressStageClass(index: number): string {
  if (state.sliceProgress.status === "completed") return "done";
  if (index < state.sliceProgress.stageIndex) return "done";
  if (index === state.sliceProgress.stageIndex) return state.sliceProgress.status === "failed" ? "failed" : "active";
  return "pending";
}

function formatDuration(value: number | null): string {
  const totalSeconds = Math.max(0, Math.round(Number(value || 0)));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function statusClass(status?: string): string {
  if (status === "scored" || status === "extracted") return "ok";
  if (status === "ingested") return "neutral";
  return "warn";
}

function statusLabel(status?: string): string {
  const labels: Record<string, string> = {
    ingested: "已导入",
    extracted: "已提取",
    segmented: "已生成候选",
    scored: "已评分",
    transcribed: "已转写",
    processing: "处理中",
    failed: "处理失败"
  };
  return labels[String(status || "")] || status || "待处理";
}

function selectVideo(videoId: string): void {
  loadQuality(videoId, true).catch(error => toast(error.message));
}

async function runSelected(): Promise<void> {
  if (!selectedProgramVideo.value) {
    toast("请选择节目");
    return;
  }
  await withBusy("run-selected", () => runAll(selectedProgramVideo.value!.id));
}

async function runVideoStep(videoId: string, step: "extract" | "segments" | "score"): Promise<void> {
  await withBusy(`${step}-${videoId}`, () => runStep(videoId, step));
}

async function runWholeVideo(videoId: VideoRow["id"]): Promise<void> {
  await withBusy(`run-all-${videoId}`, () => runAll(videoId));
}
</script>
