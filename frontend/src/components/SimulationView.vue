<template>
  <section id="simulation" class="panel">
    <div class="panel-head">
      <div>
        <h2 class="panel-title"><Icon name="radar" />推荐链路模拟器</h2>
        <p id="simulation-source" class="panel-subtitle">{{ selectedVideo ? `来自：${selectedVideo.title} / 推荐链路影子模拟` : "选择节目后模拟冷启动、首轮留存、扩量和重排瓶颈" }}</p>
      </div>
      <div class="toolbar-actions">
        <select id="simulation-video-select" :value="state.selectedVideoId" aria-label="模拟节目" @change="changeVideo">
          <option v-for="video in state.videos" :key="video.id" :value="video.id">{{ video.title }}</option>
        </select>
        <button id="simulation-refresh-btn" type="button" :disabled="!state.selectedVideoId || state.busyKey === 'simulation-refresh'" @click="refresh">
          <span v-if="state.busyKey === 'simulation-refresh'" class="spinner"></span>
          <Icon v-else name="refresh-cw" />刷新模拟
        </button>
      </div>
    </div>
    <div class="panel-body">
      <QualitySentinel compact />
      <div id="simulation-summary" class="simulation-summary">
        <div class="stat"><span>模拟均分</span><strong>{{ Number(summary.avg_score || 0).toFixed(1) }}</strong></div>
        <div class="stat"><span>高潜候选</span><strong>{{ Number(summary.high_potential_count || 0) }}</strong></div>
        <div class="stat"><span>主要瓶颈</span><strong style="font-size:16px;">{{ summary.top_bottleneck || "暂无" }}</strong></div>
        <div class="stat"><span>主阶段</span><strong style="font-size:16px;">{{ summary.top_stage || "暂无" }}</strong></div>
      </div>
      <div id="simulation-list" class="simulation-grid">
        <div v-if="!rows.length" class="empty recovery-empty">
          <Icon name="radar" />
          <strong>暂无模拟结果</strong>
          <span>{{ simulationEmptyText }}</span>
          <div class="empty-actions">
            <button type="button" data-guide-action="process" @click="handleGuideAction('process')"><Icon name="wand-sparkles" />节目处理</button>
            <button type="button" data-guide-action="candidates" @click="handleGuideAction('candidates')"><Icon name="list-video" />候选审核</button>
          </div>
        </div>
        <article v-for="row in rows" :key="row.segment_id" class="simulation-card">
          <div class="rank">{{ Number(row.simulation_rank || 0) }}</div>
          <div class="simulation-main">
            <div class="candidate-time">{{ fmtSeconds(row.time_range?.start_time) }} - {{ fmtSeconds(row.time_range?.end_time) }} ({{ Math.round(Number(row.time_range?.duration_seconds || 0)) }}s)</div>
            <div class="simulation-title">{{ row.title || "候选片段" }}</div>
            <div class="tags">
              <span class="tag">{{ row.predicted_stage || "待模拟" }}</span>
              <span class="tag blue">{{ row.music_slice_type || "短视频切片" }}</span>
              <span class="tag warn">瓶颈：{{ row.bottleneck?.label || "-" }}</span>
              <span v-if="decision(row.segment_id)" class="tag" :class="decisionStatusClass(decision(row.segment_id)?.severity)">质量联动：{{ decision(row.segment_id)?.label || "待判断" }}</span>
            </div>
            <div class="stage-flow">
              <div v-for="stage in row.stage_flow || []" :key="stage.label" class="stage" :class="stage.status || 'warn'">
                <div class="stage-head"><span>{{ stage.label }}</span><strong>{{ Math.max(0, Math.min(100, Number(stage.score || 0))).toFixed(0) }}</strong></div>
                <div class="bar"><span :style="{ width: `${Math.max(0, Math.min(100, Number(stage.score || 0)))}%` }"></span></div>
              </div>
            </div>
            <div class="simulation-footer">
              <div>
                <div class="meta">冷启动人群</div>
                <div class="tags"><span v-for="cluster in row.audience_clusters || []" :key="cluster" class="tag blue">{{ cluster }}</span></div>
              </div>
              <div>
                <div class="meta">动作建议</div>
                <ol class="simulation-actions"><li v-for="action in mergedActions(row)" :key="action">{{ action }}</li></ol>
              </div>
            </div>
          </div>
          <div class="simulation-score">
            <strong>{{ Number(row.simulated_score || 0).toFixed(1) }}</strong>
            <span>模拟分</span>
            <div class="score-meter"><span :style="{ width: `${Math.max(0, Math.min(100, Number(row.simulated_score || 0)))}%` }"></span></div>
            <div class="meta" style="margin-top:8px;">原评分 {{ Number(row.final_score || 0).toFixed(1) }}</div>
            <button type="button" style="margin-top:10px;" :data-open-sim-segment="row.segment_id || ''" @click="openSegmentInCandidates(row.segment_id)"><Icon name="panel-right-open" />候选详情</button>
          </div>
        </article>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Icon from "./Icon.vue";
import QualitySentinel from "./QualitySentinel.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { SimulationRow } from "../types";
import { decisionStatusClass, fmtSeconds } from "../utils";

const { state, selectedVideo, loadSimulation, openSegmentInCandidates, simulationDecisionFor, handleGuideAction, withBusy, toast } = useDashboardContext();

const rows = computed(() => state.simulations || []);
const summary = computed(() => state.simulationSummary || {});
const simulationEmptyText = computed(() => {
  if (state.moduleStatus.simulation.error) return state.moduleStatus.simulation.error;
  if (!state.selectedVideoId) return "先选择节目，再生成候选和评分。";
  if (!state.suggestions.length) return "当前节目还没有 Top 候选，先回到节目处理生成候选并评分。";
  return "候选已存在，刷新推荐模拟后会显示冷启动、扩量和重排瓶颈。";
});

function changeVideo(event: Event): void {
  const target = event.target as HTMLSelectElement;
  loadSimulation(target.value).catch(error => toast(error.message));
}

async function refresh(): Promise<void> {
  if (!state.selectedVideoId) {
    toast("请选择节目");
    return;
  }
  await withBusy("simulation-refresh", () => loadSimulation(state.selectedVideoId));
}

function decision(segmentId?: string) {
  return simulationDecisionFor(segmentId);
}

function mergedActions(row: SimulationRow): string[] {
  const linkedDecision = decision(row.segment_id);
  const decisionAction = linkedDecision?.action || "";
  const actions = Array.isArray(row.actions) ? row.actions : [];
  return decisionAction ? [decisionAction, ...actions.filter(action => action !== decisionAction)].slice(0, 4) : actions;
}
</script>
