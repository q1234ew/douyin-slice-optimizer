<template>
  <header class="topbar">
    <div class="brand">
      <div class="mark"><Icon name="scissors" /></div>
      <h1>音乐综艺切片优化系统</h1>
    </div>
    <nav class="nav mode-nav" aria-label="工作模式">
      <button type="button" :class="{ active: workspaceActive }" :aria-current="workspaceActive ? 'page' : undefined" data-view-tab="workbench" @click="setView('workbench')">
        <Icon name="layout-dashboard" />剪辑工作台
      </button>
      <button type="button" :class="{ active: state.view === 'feedback' }" :aria-current="state.view === 'feedback' ? 'page' : undefined" data-view-tab="feedback" @click="setView('feedback')">
        <Icon name="database" />研究中心
      </button>
    </nav>
    <div class="top-right">
      <button class="topbar-provider-btn" type="button" title="配置公网模型 API" @click="openProviderConfig">
        <Icon name="settings-2" />模型 API
      </button>
      <button class="topbar-account-btn" :class="{ unready: !publishingReady }" type="button" :title="publishingStatusTitle" :aria-label="`${publishingStatusLabel}，打开研究中心的平台连接`" data-douyin-account-entry @click="openDouyinAccount">
        <span class="dot"></span>{{ publishingStatusLabel }}
      </button>
      <span class="db-chip"><span class="dot"></span>本地运行</span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";

const { state, setView, handleGuideAction } = useDashboardContext();

const workspaceActive = computed(() => state.view !== "feedback");

const publishingContext = computed(() => state.douyinSummary?.account_context || {});
const publishingReady = computed(() => publishingContext.value.production_personalization_allowed === true);
const publishingStatusLabel = computed(() => {
  if (publishingContext.value.account_role !== "publishing_target") return "发布账号未指定";
  return publishingReady.value ? "发布账号就绪" : "发布账号冷启动";
});
const publishingStatusTitle = computed(() => {
  if (publishingContext.value.account_role !== "publishing_target") return "研究账号与发布账号已隔离；目标发布账号尚未指定";
  const name = publishingContext.value.display_name || publishingContext.value.platform_account_id || publishingContext.value.account_id || "目标发布账号";
  return `${name}：${publishingReady.value ? "结果数据可用于校准" : "结果数据暂不可用于个性化"}`;
});

function openDouyinAccount(): void {
  handleGuideAction("feedback:platform").catch(() => undefined);
}

function openProviderConfig(): void {
  handleGuideAction("feedback:runtime").catch(() => undefined);
}
</script>
