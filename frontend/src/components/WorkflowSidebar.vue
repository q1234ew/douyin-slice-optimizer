<template>
  <aside class="side">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2 class="panel-title"><Icon name="workflow" />工作流程</h2>
        </div>
      </div>
      <div class="panel-body workflow">
        <div id="workflow-guide" class="workflow-guide" aria-live="polite">
          <div class="guide-kicker">
            <span><Icon name="route" />流程导览</span>
            <span class="status" :class="workflowGuide.statusClass">第 {{ workflowGuide.step }} 步 · {{ workflowGuide.status }}</span>
          </div>
          <h3 class="guide-title">{{ workflowGuide.title }}</h3>
          <p class="guide-copy">{{ workflowGuide.copy }}</p>
          <div class="guide-footer">
            <div class="guide-progress" aria-hidden="true"><span :style="{ width: `${workflowGuide.progress}%` }"></span></div>
            <div class="guide-actions">
              <span class="guide-micro">{{ workflowGuide.micro }}</span>
              <button type="button" class="primary" :data-guide-action="workflowGuide.action" @click="handleGuideAction(workflowGuide.action)">
                <Icon name="arrow-right" />{{ workflowGuide.actionLabel }}
              </button>
            </div>
          </div>
        </div>

        <div id="workflow-steps" class="flow-steps" aria-label="流程导览">
          <button
            v-for="step in workflowSteps"
            :key="step.key"
            type="button"
            class="flow-step"
            :class="{ done: step.index < workflowGuide.step, active: step.index === workflowGuide.step }"
            :aria-current="step.index === workflowGuide.step ? 'step' : 'false'"
            :data-step-index="step.index"
            :data-workflow-step="step.key"
            :data-guide-action="step.action"
            @click="handleGuideAction(step.action)"
          >
            <span class="flow-step-number">{{ step.index }}</span>
            <span class="flow-step-main"><strong>{{ step.title }}</strong><span>{{ step.subtitle }}</span></span>
          </button>
        </div>

        <div class="workflow-upload">
          <button type="button" class="workflow-disclosure" :aria-expanded="showUploadForm ? 'true' : 'false'" @click="state.expandedWorkflow = !state.expandedWorkflow">
            <span class="step-title"><span class="step-number">+</span>导入节目</span>
            <span class="meta">{{ showUploadForm ? "收起" : "需要新节目时展开" }}</span>
          </button>
          <form v-if="showUploadForm" id="upload-form" class="dropzone" @submit.prevent="submitUpload">
            <input id="upload-account" v-model="uploadAccount" name="account_id" aria-label="账号" />
            <input id="upload-title" v-model="uploadTitle" name="title" placeholder="节目标题" aria-label="节目标题" required />
            <input id="upload-file" ref="uploadFile" type="file" name="file" accept="video/*" aria-label="视频文件" required />
            <button class="primary" type="submit" :disabled="state.busyKey === 'upload-video'">
              <span v-if="state.busyKey === 'upload-video'" class="spinner"></span>
              <Icon v-else name="upload-cloud" />{{ state.busyKey === "upload-video" ? "处理中" : "导入节目" }}
            </button>
          </form>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><Icon name="server" />存储与状态</h2>
      </div>
      <div class="panel-body">
        <div class="storage-row"><span>节目总数</span><strong id="stat-videos-side">{{ state.stats.videos }}</strong></div>
        <div class="storage-row"><span>候选片段</span><strong id="stat-segments-side">{{ state.stats.segments }}</strong></div>
        <div class="storage-row"><span>累计导出</span><strong id="stat-exports-side">{{ state.stats.exports }}</strong></div>
        <div class="storage-row"><span>训练样本</span><strong id="stat-samples-side">{{ state.stats.training_samples }}</strong></div>
      </div>
    </section>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";

const { state, workflowGuide, handleGuideAction, uploadVideo, withBusy, toast } = useDashboardContext();

const uploadAccount = ref("main");
const uploadTitle = ref("");
const uploadFile = ref<HTMLInputElement | null>(null);
const showUploadForm = computed(() => state.expandedWorkflow || !state.stats.videos);

const workflowSteps = [
  { index: 1, key: "upload", action: "upload", title: "导入节目", subtitle: "建立素材档案" },
  { index: 2, key: "process", action: "process", title: "处理评分", subtitle: "生成 Top 候选" },
  { index: 3, key: "review", action: "candidates", title: "候选审核", subtitle: "复核质量 Gate" },
  { index: 4, key: "feedback", action: "feedback:overview", title: "历史先验", subtitle: "研究样本" },
  { index: 5, key: "learn", action: "feedback:calibration", title: "复盘学习", subtitle: "回测与校准" }
];

async function submitUpload(): Promise<void> {
  const file = uploadFile.value?.files?.[0];
  if (!file) {
    toast("请选择视频文件");
    return;
  }
  const form = new FormData();
  form.append("account_id", uploadAccount.value || "main");
  form.append("title", uploadTitle.value);
  form.append("file", file);
  await withBusy("upload-video", () => uploadVideo(form));
  uploadTitle.value = "";
  uploadAccount.value = "main";
  if (uploadFile.value) uploadFile.value.value = "";
}
</script>
