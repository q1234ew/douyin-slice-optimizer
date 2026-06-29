import { inject, type InjectionKey } from "vue";
import type { DashboardStore } from "./useDashboard";

export const dashboardKey: InjectionKey<DashboardStore> = Symbol("dashboard");

export function useDashboardContext(): DashboardStore {
  const dashboard = inject(dashboardKey);
  if (!dashboard) {
    throw new Error("Dashboard context is not available");
  }
  return dashboard;
}
