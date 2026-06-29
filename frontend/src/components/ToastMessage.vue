<template>
  <div id="toast" class="toast" :class="{ show: Boolean(state.toastMessage) }" role="status" aria-live="polite" @click="dismissToast">
    {{ state.toastMessage }}
  </div>
</template>

<script setup lang="ts">
import { useDashboardContext } from "../composables/dashboardContext";

const { state, dismissToast } = useDashboardContext();
</script>
