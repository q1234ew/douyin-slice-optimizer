<template>
  <div class="quality-sentinel" :class="{ compact }">
    <div v-if="!state.selectedVideoId || !selectedVideo" class="quality-top">
      <div class="quality-score gate warn"><span>导出决策</span><strong>-</strong><em>未选择</em></div>
      <div class="quality-score warn"><span>质量健康分</span><strong>-</strong><em>未选择</em></div>
      <div class="quality-main"><strong>发布前质量哨兵</strong><span>选择节目后展示 ASR、候选队列和导出前风险。</span></div>
    </div>

    <div v-else-if="state.qualityLoading" class="quality-top">
      <div class="quality-score gate warn"><span>导出决策</span><strong>...</strong><em>检测中</em></div>
      <div class="quality-score warn"><span>质量健康分</span><strong>...</strong><em>检测中</em></div>
      <div class="quality-main"><strong>{{ selectedVideo.title || "当前节目" }}</strong><span><span class="spinner"></span> 正在读取 ASR 和候选队列质量信号</span></div>
    </div>

    <div v-else-if="!report" class="quality-top">
      <div class="quality-score gate warn"><span>导出决策</span><strong>-</strong><em>暂无</em></div>
      <div class="quality-score warn"><span>质量健康分</span><strong>-</strong><em>暂无</em></div>
      <div class="quality-main"><strong>{{ selectedVideo.title || "当前节目" }}</strong><span>质量洞察暂不可用，仍可继续查看候选和推荐模拟。</span></div>
    </div>

    <template v-else>
      <div class="quality-summary">
        <div class="quality-score gate" :class="gateClass">
          <span>当前结论</span>
          <strong>{{ gateStatusLabel(gateStatus) }}</strong>
          <em>{{ primaryAction.label || gate?.label || gateStatusLabel(gateStatus) }}</em>
        </div>
        <div class="quality-score" :class="level">
          <span>质量分</span>
          <strong>{{ score.toFixed(0) }}</strong>
          <em>{{ healthLevelLabel(level) }}</em>
        </div>
        <div class="quality-main quality-brief">
          <span class="quality-kicker">导出前检查</span>
          <strong>{{ selectedVideo.title || report.video_title || "当前节目" }}</strong>
          <p>{{ keyRiskLine }}</p>
          <div class="quality-route-brief">
            <Icon name="route" />
            <span>{{ asrRoutingSummary }}</span>
          </div>
          <div class="quality-actions">
            <button type="button" class="primary-action" @click="handlePrimaryReview">
              <Icon :name="primaryReview.icon" />{{ primaryReview.label }}
            </button>
            <button type="button" :disabled="state.busyKey === 'quality-refresh'" :data-refresh-quality="state.selectedVideoId" @click="refresh">
              <span v-if="state.busyKey === 'quality-refresh'" class="spinner"></span>
              <Icon v-else name="refresh-cw" />刷新
            </button>
          </div>
        </div>
      </div>

      <div class="quality-metrics">
        <div v-for="metric in metricCards" :key="metric.label" class="quality-metric" :class="metric.tone">
          <span>{{ metric.label }}</span>
          <strong>{{ metric.value }}</strong>
          <em>{{ metric.caption }}</em>
        </div>
      </div>

      <div class="quality-bottom">
        <div class="quality-list quality-panel">
          <div class="quality-panel-head">
            <div>
              <strong>需要处理的风险</strong>
              <span>{{ issues.length ? `${issues.length} 类风险会影响导出判断` : "当前没有明显阻断项" }}</span>
            </div>
          </div>
          <div v-if="!issues.length" class="quality-row calm"><Icon name="shield-check" /><span>暂无明显质量风险</span></div>
          <div v-for="issue in issues.slice(0, 3)" :key="`${issue.label}-${issue.evidence}`" class="quality-row">
            <span class="status" :class="issueStatus(issue.severity)">{{ issue.label || "质量提示" }}</span>
            <span>{{ clipText(issue.evidence || issue.recommendation || "", 110) }}</span>
          </div>
        </div>

        <div class="quality-list quality-panel">
          <div class="quality-panel-head">
            <div>
              <strong>建议动作</strong>
              <span>按顺序处理，避免直接批量导出</span>
            </div>
          </div>
          <div v-if="!actionItems.length" class="quality-row calm"><Icon name="check-circle-2" /><span>可进入标题、封面和导出审核。</span></div>
          <div v-for="item in actionItems" :key="item.key" class="quality-row">
            <Icon :name="item.icon" />
            <span>{{ clipText(item.text, 118) }}</span>
          </div>
        </div>

        <div class="quality-list quality-panel">
          <div class="quality-panel-head">
            <div>
              <strong>待处理片段</strong>
              <span>{{ reviewQueue.length ? "点击进入候选详情定位处理" : "暂无片段级复核队列" }}</span>
            </div>
          </div>
          <div v-if="!reviewQueue.length" class="quality-row calm"><Icon name="shield-check" /><span>当前没有片段级复核项。</span></div>
          <div v-for="item in reviewQueue" :key="item.key" class="quality-row">
            <span class="status" :class="item.tone">{{ item.label }}</span>
            <span>{{ clipText(item.text, 96) }}</span>
            <button
              v-if="item.segmentId"
              type="button"
              aria-label="打开候选详情"
              :data-open-quality-segment="item.segmentId"
              @click="openSegmentInCandidates(item.segmentId, item.section)"
            >
              <Icon name="panel-right-open" />
            </button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { InspectorSectionName } from "../types";
import {
  clipText,
  decisionStatusClass,
  fmtSeconds,
  gateAction,
  gateLevelClass,
  gateStatusLabel,
  healthLevelLabel
} from "../utils";

defineProps<{
  compact?: boolean;
}>();

type MetricCard = {
  label: string;
  value: string;
  caption: string;
  tone?: string;
};

type ActionItem = {
  key: string;
  text: string;
  icon: string;
};

type ReviewQueueItem = {
  key: string;
  label: string;
  text: string;
  tone: string;
  segmentId?: string;
  section: InspectorSectionName;
};

const { state, selectedVideo, loadQuality, openSegmentInCandidates, handleGuideAction, withBusy } = useDashboardContext();

const report = computed(() => state.quality);
const health = computed(() => report.value?.health || {});
const gate = computed(() => report.value?.gate || {});
const transcript = computed(() => report.value?.transcript || {});
const queue = computed(() => report.value?.queue || {});
const asrRouting = computed(() => report.value?.asr_routing || {});
const issues = computed(() => Array.isArray(report.value?.issues) ? report.value?.issues || [] : []);
const actions = computed(() => Array.isArray(report.value?.actions) ? report.value?.actions || [] : []);
const watchlist = computed(() => Array.isArray(report.value?.watchlist) ? report.value?.watchlist || [] : []);
const simulationActions = computed(() => Array.isArray(report.value?.simulation?.actions) ? report.value?.simulation?.actions || [] : []);
const simulationDecisions = computed(() => Array.isArray(report.value?.simulation?.decisions) ? report.value?.simulation?.decisions || [] : []);
const verifyRoutes = computed(() => Array.isArray(asrRouting.value.verify_queue) ? asrRouting.value.verify_queue || [] : []);
const englishPreserveRoutes = computed(() => Array.isArray(asrRouting.value.english_preserve_queue) ? asrRouting.value.english_preserve_queue || [] : []);
const asrRouteItems = computed(() => [...verifyRoutes.value, ...englishPreserveRoutes.value]);
const level = computed(() => health.value.level || "warn");
const score = computed(() => Number(health.value.score || 0));
const gateStatus = computed(() => gate.value.status || (level.value === "good" ? "allow" : "review"));
const gateClass = computed(() => gateLevelClass(gate.value));
const primaryAction = computed(() => gateAction(gate.value));
const gateSummary = computed(() => {
  const reasons = Array.isArray(gate.value.reasons) ? gate.value.reasons : [];
  const topReason = reasons[0] || {};
  return gate.value.summary || topReason.label || health.value.top_issue || "暂无明显质量风险";
});
const backendLabel = computed(() => `${transcript.value.backend || "-"}${transcript.value.whisper_cpp_vad_enabled ? "+VAD" : ""}`);
const asrRoutingSummary = computed(() => {
  const videoRoute = asrRouting.value.video || {};
  const action = String(asrRouting.value.next_action || videoRoute.decision || "keep_current");
  const profile = String(videoRoute.recommended_profile || "-");
  const model = String(videoRoute.recommended_model || "-");
  const verifyCount = Number(asrRouting.value.verify_count || 0);
  const englishCount = Number(asrRouting.value.english_preserve_count || 0);
  if (action === "rerun_full_video_quality") return `全片建议 ${profile}/${model}，Top 候选 ${verifyCount} 条进入 verify 队列。`;
  if (action === "verify_top_candidates") return `当前全片可保留，Top 候选 ${verifyCount} 条建议候选级 verify。`;
  if (action === "preserve_quality_for_english") return `英文场景 ${englishCount} 条保留 quality/small，不自动覆盖。`;
  return `当前 ASR 路由为 ${profile}/${model}，暂无额外升级建议。`;
});
const keyRiskLine = computed(() => {
  const issueCount = issues.value.length;
  const reviewCount = watchlist.value.length + Number(asrRouting.value.verify_count || 0);
  if (!issueCount && !reviewCount) return gateSummary.value || "当前无高优先级风险，可继续候选审核。";
  const parts = [];
  if (issueCount) parts.push(`${issueCount} 类质量风险`);
  if (reviewCount) parts.push(`${reviewCount} 个复核信号`);
  return `${parts.join("，")}。${gateSummary.value}`;
});
const primaryReview = computed(() => {
  if (watchlist.value.length || Number(asrRouting.value.verify_count || 0)) {
    return { label: "处理 ASR 复核", action: "candidate:asr", icon: "scan-text" };
  }
  if (simulationDecisions.value.length) {
    return { label: "查看候选风险", action: "candidate:decision", icon: "panel-right-open" };
  }
  return { label: "进入候选审核", action: "candidates", icon: "panel-right-open" };
});
const metricCards = computed<MetricCard[]>(() => {
  const repetition = Number(transcript.value.repetition_noise_count || 0);
  const adRead = Number(transcript.value.ad_read_count || 0);
  const verifyCount = Number(asrRouting.value.verify_count || 0);
  const closedLoop = Number(queue.value.closed_loop_count || 0);
  const topK = Number(queue.value.top_k || 0);
  return [
    {
      label: "ASR 状态",
      value: backendLabel.value,
      caption: transcript.value.whisper_cpp_vad_enabled ? "已启用 VAD" : "未启用 VAD",
      tone: repetition ? "warn" : "good"
    },
    {
      label: "候选复核",
      value: String(watchlist.value.length),
      caption: verifyCount ? `${verifyCount} 条建议 verify` : "无候选级 ASR 队列",
      tone: watchlist.value.length || verifyCount ? "warn" : "good"
    },
    {
      label: "重复文本",
      value: String(repetition),
      caption: "疑似 ASR 幻觉/重复",
      tone: repetition ? "risk" : "good"
    },
    {
      label: "广告/导流",
      value: String(adRead),
      caption: "需剪除或降权",
      tone: adRead ? "warn" : "good"
    },
    {
      label: "Top 闭环",
      value: `${closedLoop}/${topK}`,
      caption: "结构完整候选",
      tone: closedLoop ? "good" : "warn"
    },
    {
      label: "字幕规模",
      value: String(Number(transcript.value.segment_count || 0)),
      caption: "用于上下文判断",
      tone: "neutral"
    }
  ];
});
const actionItems = computed<ActionItem[]>(() => {
  const items: ActionItem[] = actions.value.slice(0, 3).map((text, index) => ({
    key: `action-${index}-${text}`,
    text,
    icon: "check-circle-2"
  }));
  simulationActions.value.slice(0, 1).forEach((text, index) => {
    items.push({ key: `simulation-${index}-${text}`, text, icon: "radar" });
  });
  return items;
});
const reviewQueue = computed<ReviewQueueItem[]>(() => {
  const items: ReviewQueueItem[] = [];
  asrRouteItems.value.slice(0, 2).forEach((item, index) => {
    items.push({
      key: `asr-${index}-${item.segment_id || item.recommended_profile || "route"}`,
      label: item.decision === "verify_candidate" ? "ASR 复核" : "ASR 保留",
      text: (item.reasons || []).join(" / ") || item.evidence || asrRoutingSummary.value,
      tone: item.decision === "verify_candidate" ? "warn" : "neutral",
      segmentId: item.segment_id,
      section: "asr"
    });
  });
  simulationDecisions.value.slice(0, 2).forEach((item, index) => {
    items.push({
      key: `decision-${index}-${item.segment_id || item.label || "simulation"}`,
      label: item.label || "推荐联动",
      text: item.title || item.reason || "查看候选风险说明",
      tone: decisionStatusClass(item.severity),
      segmentId: item.segment_id,
      section: "decision"
    });
  });
  watchlist.value.slice(0, 3).forEach((item, index) => {
    const flags = Array.isArray(item.flags) ? item.flags.join(" / ") : "";
    items.push({
      key: `watch-${index}-${item.segment_id || flags}`,
      label: "上下文确认",
      text: `${fmtSeconds(item.time_range?.start_time)} - ${fmtSeconds(item.time_range?.end_time)} ${flags}`.trim(),
      tone: "neutral",
      segmentId: item.segment_id,
      section: "asr"
    });
  });
  return items.slice(0, 5);
});

function issueStatus(severity?: string): string {
  if (severity === "risk") return "risk";
  if (severity === "info") return "neutral";
  return "warn";
}

async function refresh(): Promise<void> {
  if (!state.selectedVideoId) return;
  await withBusy("quality-refresh", () => loadQuality(state.selectedVideoId, true));
}

async function handlePrimaryReview(): Promise<void> {
  await handleGuideAction(primaryReview.value.action);
}
</script>
