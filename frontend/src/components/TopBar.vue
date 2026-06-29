<template>
  <header class="topbar">
    <div class="brand">
      <div class="mark"><Icon name="scissors" /></div>
      <h1>音乐综艺切片优化系统</h1>
      <span class="status-pill"><Icon name="sparkles" />Vue 3</span>
    </div>
    <nav class="nav" aria-label="主导航" role="tablist">
      <button
        v-for="tab in tabs"
        :key="tab.view"
        :class="{ active: state.view === tab.view }"
        type="button"
        role="tab"
        :aria-selected="state.view === tab.view ? 'true' : 'false'"
        :data-view-tab="tab.view"
        @click="setView(tab.view)"
      >
        <Icon :name="tab.icon" />{{ tab.label }}
      </button>
    </nav>
    <div class="top-right">
      <button class="topbar-account-btn" type="button" aria-label="打开研究学习里的平台账号分区" data-douyin-account-entry @click="openDouyinAccount">
        <Icon name="radio-tower" />抖音账号
      </button>
      <span class="db-chip"><span class="dot"></span>SQLite</span>
      <span class="db-chip"><Icon name="user-circle" /><span id="active-account-chip">{{ activeAccountLabel }}</span></span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { ViewName } from "../types";

const { state, setView, handleGuideAction } = useDashboardContext();

const tabs: Array<{ view: ViewName; label: string; icon: string }> = [
  { view: "workbench", label: "节目处理", icon: "layout-dashboard" },
  { view: "candidates", label: "候选审核", icon: "play-square" },
  { view: "simulation", label: "推荐模拟", icon: "radar" },
  { view: "feedback", label: "研究学习", icon: "database" }
];

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
