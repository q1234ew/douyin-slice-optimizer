<template>
  <component :is="component" class="icon" aria-hidden="true" />
</template>

<script setup lang="ts">
import { computed } from "vue";
import {
  Activity,
  ArrowRight,
  AudioLines,
  Ban,
  Blocks,
  Captions,
  Check,
  CheckCircle2,
  CircleAlert,
  CircleHelp,
  ClipboardCheck,
  Copy,
  CopyPlus,
  Cpu,
  Database,
  Download,
  ExternalLink,
  FileClock,
  FileSpreadsheet,
  Flag,
  FlaskConical,
  Gauge,
  GraduationCap,
  History,
  Image,
  Info,
  LayoutDashboard,
  Lightbulb,
  LineChart,
  Link,
  ListVideo,
  PanelRight,
  PanelRightOpen,
  Play,
  PlaySquare,
  QrCode,
  Radar,
  RadioTower,
  RefreshCcw,
  RefreshCw,
  Route,
  ScanLine,
  ScanText,
  Scissors,
  Server,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Star,
  Text,
  Upload,
  UploadCloud,
  UserCircle,
  Users,
  Video,
  WandSparkles,
  Workflow
} from "@lucide/vue";

const props = defineProps<{
  name: string;
}>();

function toPascalCase(value: string): string {
  return value
    .split("-")
    .map(part => part ? `${part[0].toUpperCase()}${part.slice(1)}` : "")
    .join("");
}

const iconMap = {
  Activity,
  ArrowRight,
  AudioLines,
  Ban,
  Blocks,
  Captions,
  Check,
  CheckCircle2,
  CircleAlert,
  CircleHelp,
  ClipboardCheck,
  Copy,
  CopyPlus,
  Cpu,
  Database,
  Download,
  ExternalLink,
  FileClock,
  FileSpreadsheet,
  Flag,
  FlaskConical,
  Gauge,
  GraduationCap,
  History,
  Image,
  Info,
  LayoutDashboard,
  Lightbulb,
  LineChart,
  Link,
  ListVideo,
  PanelRight,
  PanelRightOpen,
  Play,
  PlaySquare,
  QrCode,
  Radar,
  RadioTower,
  RefreshCcw,
  RefreshCw,
  Route,
  ScanLine,
  ScanText,
  Scissors,
  Server,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Star,
  Text,
  Upload,
  UploadCloud,
  UserCircle,
  Users,
  Video,
  WandSparkles,
  Workflow
};

const component = computed(() => iconMap[toPascalCase(props.name) as keyof typeof iconMap] || CircleHelp);
</script>
