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
      <button class="topbar-account-btn" type="button" :title="`当前账号：${activeAccountLabel}`" aria-label="抖音已连接，打开研究中心的平台连接" data-douyin-account-entry @click="openDouyinAccount">
        <span class="dot"></span>抖音已连接
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

const activeAccountLabel = computed(() => {
  const id = state.feedbackAccount.trim();
  if (!id) return "全部账号";
  const quality = state.historicalSummary?.account_quality?.find(item => item.account_id === id);
  if (quality?.account_display_name) return quality.account_display_name;
  const dataset = state.learningDatasets.find(item => item.account_id === id || item.program_key === id);
  return dataset?.account_display_name || id;
});

function openDouyinAccount(): void {
  handleGuideAction("feedback:platform").catch(() => undefined);
}
</script>
