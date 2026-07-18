<template>
  <section class="precut-workbench" aria-labelledby="precut-title">
    <div class="precut-toolbar">
      <div>
        <h3 id="precut-title"><Icon name="list-video" />批量短片排名</h3>
        <p>原始时间边界锁定 · 内容哈希去重 · 统一候选排序</p>
      </div>
      <div class="toolbar-actions">
        <select v-model="selectedBatchId" aria-label="选择已切短片批次" @change="selectBatch">
          <option value="">最近批次</option>
          <option v-for="batch in state.precutBatches" :key="batch.id" :value="batch.id">
            {{ batch.title || batch.id }} · {{ statusLabel(batch.status) }}
          </option>
        </select>
        <button class="icon-only" type="button" title="刷新批次" aria-label="刷新批次" :disabled="Boolean(state.busyKey)" @click="refreshBatches">
          <Icon name="refresh-cw" />
        </button>
      </div>
    </div>

    <form class="precut-intake" @submit.prevent="submitBatch">
      <label>
        <span>目标账号</span>
        <input v-model="accountId" name="account_id" aria-label="目标账号" />
      </label>
      <label>
        <span>批次名称</span>
        <input v-model="batchTitle" name="batch_title" placeholder="可选" aria-label="批次名称" />
      </label>
      <label class="precut-file-field">
        <span>已切短片</span>
        <input ref="fileInput" type="file" multiple accept="video/*" aria-label="选择多个已切短片" required @change="readFileCount" />
      </label>
      <button class="primary" type="submit" :disabled="!selectedFileCount || state.busyKey === 'precut-upload'">
        <span v-if="state.busyKey === 'precut-upload'" class="spinner"></span>
        <Icon v-else name="upload-cloud" />{{ selectedFileCount ? `导入并排名 ${selectedFileCount} 条` : "选择短片" }}
      </button>
    </form>

    <div v-if="detail" class="precut-batch-status" aria-live="polite">
      <div class="precut-batch-head">
        <div>
          <span class="meta">{{ detail.batch.account_id || "main" }} · {{ detail.contract_version || "precut_batch.v1" }}</span>
          <strong>{{ detail.batch.title || detail.batch_id }}</strong>
        </div>
        <div class="toolbar-actions">
          <span class="status" :class="statusClass(detail.status)">{{ statusLabel(detail.status) }}</span>
          <button
            v-if="detail.status !== 'processing' && detail.status !== 'queued'"
            type="button"
            :disabled="Boolean(state.busyKey)"
            @click="retryBatch"
          >
            <Icon name="refresh-cw" />{{ detail.status === "completed" ? "重新处理" : "继续处理" }}
          </button>
        </div>
      </div>
      <div class="precut-progress" aria-label="批次处理进度">
        <span :style="{ width: `${progressPercent}%` }"></span>
      </div>
      <div class="precut-stats">
        <span><small>总数</small><strong>{{ Number(detail.summary?.item_count || 0) }}</strong></span>
        <span><small>已排名</small><strong>{{ Number(detail.summary?.ranked_count || 0) }}</strong></span>
        <span><small>内容复用</small><strong>{{ Number(detail.summary?.reused_count || 0) }}</strong></span>
        <span><small>边界锁定</small><strong>{{ Number(detail.summary?.boundary_locked_count || 0) }}</strong></span>
        <span><small>失败</small><strong>{{ Number(detail.summary?.failed_count || 0) }}</strong></span>
      </div>
      <p v-if="detail.batch.error_summary" class="inline-alert error">{{ detail.batch.error_summary }}</p>
    </div>

    <div v-if="displayRows.length" class="table-wrap precut-rankings">
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>短片</th>
            <th>边界</th>
            <th>统一排序分</th>
            <th>证据</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in displayRows" :key="item.id || `${item.batch_id}-${item.position}`">
            <td><strong class="precut-rank">{{ item.batch_rank || "-" }}</strong></td>
            <td>
              <strong class="precut-item-title">{{ item.title || item.source_name }}</strong>
              <span class="meta">{{ item.source_name }} · {{ fmtSeconds(item.duration_seconds) }}</span>
            </td>
            <td>
              <span class="boundary-lock" :class="{ ready: item.boundary_invariant }">
                <Icon :name="item.boundary_invariant ? 'shield-check' : 'circle-alert'" />
                {{ item.boundary_invariant ? "0 秒至片尾" : "待核验" }}
              </span>
            </td>
            <td><strong class="precut-score">{{ scoreText(item.effective_score) }}</strong></td>
            <td><span class="meta">{{ evidenceLabel(item) }}</span></td>
            <td><span class="status" :class="itemStatusClass(item.status)">{{ itemStatusLabel(item.status, item.candidate_status) }}</span></td>
            <td>
              <button
                type="button"
                :disabled="!item.candidate_segment_id || !item.source_video_id || item.effective_score == null"
                @click="openCandidate(item)"
              >
                <Icon name="panel-right-open" />审核
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="empty precut-empty">
      <Icon name="list-video" />
      <strong>尚无批量短片</strong>
      <span>选择多个已剪好的视频，系统会保持每条原始时间边界。</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { PrecutBatchItem } from "../types";
import { fmtSeconds } from "../utils";

const {
  state,
  loadPrecutBatches,
  loadPrecutBatch,
  createPrecutBatch,
  processPrecutBatch,
  loadSuggestions,
  selectCandidate,
  setView,
  withBusy,
  toast
} = useDashboardContext();

const accountId = ref("main");
const batchTitle = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
const selectedFileCount = ref(0);
const selectedBatchId = ref("");
const detail = computed(() => state.selectedPrecutBatch);
const progressPercent = computed(() => Math.round(Number(detail.value?.batch.progress?.ratio || 0) * 100));
const displayRows = computed(() => {
  const rankings = detail.value?.rankings || [];
  const rankedIds = new Set(rankings.map(item => item.id));
  const remainder = (detail.value?.items || []).filter(item => !rankedIds.has(item.id));
  return [...rankings, ...remainder];
});

watch(
  () => state.selectedPrecutBatch?.batch_id,
  value => { selectedBatchId.value = value || ""; },
  { immediate: true }
);

onMounted(() => {
  if (!state.precutBatches.length) loadPrecutBatches(false).catch(() => undefined);
});

function readFileCount(): void {
  selectedFileCount.value = fileInput.value?.files?.length || 0;
}

async function submitBatch(): Promise<void> {
  const files = Array.from(fileInput.value?.files || []);
  if (!files.length) {
    toast("请选择至少一个短片文件");
    return;
  }
  const form = new FormData();
  form.append("account_id", accountId.value.trim() || "main");
  form.append("batch_title", batchTitle.value.trim());
  form.append("process", "true");
  form.append("asr_profile", "fast");
  files.forEach(file => form.append("files", file));
  await withBusy("precut-upload", () => createPrecutBatch(form));
  batchTitle.value = "";
  selectedFileCount.value = 0;
  if (fileInput.value) fileInput.value.value = "";
}

async function selectBatch(): Promise<void> {
  if (!selectedBatchId.value) return;
  await withBusy("precut-select", () => loadPrecutBatch(selectedBatchId.value));
}

async function refreshBatches(): Promise<void> {
  await withBusy("precut-refresh", () => loadPrecutBatches(true));
}

async function retryBatch(): Promise<void> {
  if (!detail.value?.batch_id) return;
  const force = detail.value.status === "completed";
  await withBusy("precut-process", () => processPrecutBatch(detail.value!.batch_id, force));
}

async function openCandidate(item: PrecutBatchItem): Promise<void> {
  if (!item.source_video_id || !item.candidate_segment_id) return;
  state.selectedVideoId = item.source_video_id;
  await loadSuggestions(item.source_video_id, false);
  setView("candidates");
  selectCandidate(item.candidate_segment_id, null, "decision");
}

function statusLabel(status?: string): string {
  return {
    ready: "待处理",
    queued: "排队中",
    processing: "处理中",
    completed: "排名完成",
    partial_failed: "部分完成",
    failed: "处理失败"
  }[String(status || "")] || status || "待处理";
}

function statusClass(status?: string): string {
  if (status === "completed") return "ok";
  if (status === "failed") return "risk";
  if (status === "partial_failed" || status === "processing" || status === "queued") return "warn";
  return "neutral";
}

function itemStatusLabel(status?: string, candidateStatus?: string): string {
  if (status === "scored") {
    return candidateStatus === "approved" ? "已通过" : (candidateStatus === "blocked" ? "已暂缓" : "待审核");
  }
  return {
    ready: "待处理",
    reused: "等待复用",
    processing: "处理中",
    failed: "失败"
  }[String(status || "")] || status || "待处理";
}

function itemStatusClass(status?: string): string {
  if (status === "scored") return "ok";
  if (status === "failed") return "risk";
  if (status === "processing") return "warn";
  return "neutral";
}

function scoreText(value?: number | null): string {
  return value == null ? "-" : Number(value).toFixed(1);
}

function evidenceLabel(item: PrecutBatchItem): string {
  if (item.error) return item.error;
  const signals = item.learning_signals || {};
  const label = String(signals.confidence_label || "");
  if (label === "high") return "历史证据高";
  if (label === "medium") return "历史证据中";
  if (label === "low") return "历史证据低";
  return item.effective_score == null ? "等待评分" : "规则与语义证据";
}
</script>
