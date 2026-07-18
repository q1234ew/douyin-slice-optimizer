<template>
  <a href="#workbench" class="skip-link">跳到主内容</a>
  <TopBar />
  <main id="workbench" class="app-grid" tabindex="-1">
    <WorkflowSidebar />
    <section class="workspace">
      <ProgramWorkbench />
      <FeedbackView />
      <CandidateWorkbench />
      <SimulationView />
    </section>
    <InspectorPanel />
  </main>
  <ToastMessage />
</template>

<script setup lang="ts">
import { onMounted, provide } from "vue";
import CandidateWorkbench from "./components/CandidateWorkbench.vue";
import FeedbackView from "./components/FeedbackView.vue";
import InspectorPanel from "./components/InspectorPanel.vue";
import ProgramWorkbench from "./components/ProgramWorkbench.vue";
import SimulationView from "./components/SimulationView.vue";
import ToastMessage from "./components/ToastMessage.vue";
import TopBar from "./components/TopBar.vue";
import WorkflowSidebar from "./components/WorkflowSidebar.vue";
import { dashboardKey } from "./composables/dashboardContext";
import { useDashboard } from "./composables/useDashboard";

const dashboard = useDashboard();
provide(dashboardKey, dashboard);

onMounted(() => {
  const { refreshVideos, loadPrecutBatches } = dashboard;
  refreshVideos().catch(() => {});
  loadPrecutBatches(false).catch(() => {});
});
</script>
