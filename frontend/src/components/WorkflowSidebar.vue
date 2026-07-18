<template>
  <aside class="side" aria-label="剪辑工作流">
    <section class="panel workflow-rail-panel">
      <div class="workflow-rail">
        <nav class="workspace-page-nav" aria-label="剪辑工作台页面">
          <button
            v-for="page in workspacePages"
            :key="page.key"
            type="button"
            :class="{ active: activePage === page.key }"
            :aria-current="activePage === page.key ? 'page' : undefined"
            :data-workspace-page="page.key"
            @click="navigatePage(page.key)"
          >
            <Icon :name="page.icon" />
            <span><strong>{{ page.title }}</strong><small>{{ page.subtitle }}</small></span>
          </button>
        </nav>

        <div class="workflow-progress-summary" aria-label="当前页面进度" role="status">
          <div><span>当前页面</span><strong>{{ currentStage.step }} / 3</strong></div>
          <div class="guide-progress" aria-hidden="true"><span :style="{ width: `${currentStage.progress}%` }"></span></div>
          <small>{{ currentStage.label }} · {{ currentStage.status }}</small>
        </div>

        <button v-if="showContextAction" type="button" class="workflow-rail-action" :data-guide-action="workflowGuide.action" @click="handleGuideAction(workflowGuide.action)">
          <span><small>下一步</small><strong>{{ workflowGuide.title }}</strong></span>
          <Icon name="arrow-right" />
        </button>
      </div>

      <div v-if="showUploadTools" class="workflow-upload rail-upload">
        <button type="button" class="workflow-disclosure" :aria-expanded="showUploadForm ? 'true' : 'false'" @click="state.expandedWorkflow = !state.expandedWorkflow">
          <span class="step-title"><span class="step-number">+</span>导入素材</span>
          <span class="meta">{{ showUploadForm ? "收起" : "添加新节目" }}</span>
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
    </section>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";

const { state, workflowGuide, setView, handleGuideAction, uploadVideo, withBusy, toast } = useDashboardContext();

const uploadAccount = ref("main");
const uploadTitle = ref("");
const uploadFile = ref<HTMLInputElement | null>(null);
const showUploadForm = computed(() => state.expandedWorkflow || !state.stats.videos);
const showUploadTools = computed(() => state.entryMode === "program" && state.view === "workbench" && (state.expandedWorkflow || !state.stats.videos));
const showContextAction = computed(() => state.entryMode === "program" && state.view !== "candidates" && !showUploadTools.value);
const activePage = computed(() => {
  if (state.view === "candidates" && state.inspectorSection === "packaging") return "publish";
  if (state.view === "candidates" || state.view === "simulation") return "review";
  return "materials";
});
const currentStage = computed(() => {
  if (activePage.value === "publish") {
    return { step: 3, progress: 100, label: "导出质检", status: "发布准备" };
  }
  if (activePage.value === "review") {
    return { step: 2, progress: 200 / 3, label: "候选复核", status: "审核中" };
  }
  return { step: 1, progress: 100 / 3, label: "素材处理", status: "素材与节目" };
});

const workspacePages = [
  { key: "materials", title: "素材与节目", subtitle: "导入、转写、评分", icon: "video" },
  { key: "review", title: "候选审核", subtitle: "复核与判断依据", icon: "list-video" },
  { key: "publish", title: "发布准备", subtitle: "包装、预览、导出", icon: "upload" }
] as const;

type WorkspacePage = typeof workspacePages[number]["key"];

function navigatePage(page: WorkspacePage): void {
  if (page === "review") {
    state.inspectorSection = "decision";
    handleGuideAction("candidates").catch(() => undefined);
    return;
  }
  if (page === "publish") {
    handleGuideAction("candidate:packaging").catch(() => undefined);
    return;
  }
  setView("workbench");
  window.setTimeout(() => document.getElementById("workbench")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
}

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
