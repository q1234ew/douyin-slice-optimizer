import type { ActionHint, CandidateRow, QualityDecision, QualityReport, VariantRow } from "./types";

const ZH_HANS_PHRASES: Array<[string, string]> = [
  ["音樂", "音乐"],
  ["綜藝", "综艺"],
  ["節目", "节目"],
  ["現場", "现场"],
  ["全場", "全场"],
  ["掌聲", "掌声"],
  ["觀眾", "观众"],
  ["導師", "导师"],
  ["評委", "评委"],
  ["點評", "点评"],
  ["建議", "建议"],
  ["夢想", "梦想"],
  ["廣告", "广告"],
  ["直播間", "直播间"],
  ["掃碼", "扫码"],
  ["互動", "互动"],
  ["帳號", "账号"],
  ["標題", "标题"],
  ["風險", "风险"],
  ["複核", "复核"],
  ["轉寫", "转写"],
  ["導出", "导出"]
];

const ZH_HANS_CHARS: Record<string, string> = {
  "這": "这",
  "個": "个",
  "們": "们",
  "兩": "两",
  "會": "会",
  "為": "为",
  "來": "来",
  "說": "说",
  "聽": "听",
  "還": "还",
  "場": "场",
  "聲": "声",
  "夢": "梦",
  "愛": "爱",
  "時": "时",
  "壓": "压",
  "從": "从",
  "現": "现",
  "沒": "没",
  "過": "过",
  "開": "开",
  "眾": "众",
  "歡": "欢",
  "間": "间",
  "節": "节",
  "關": "关",
  "係": "系",
  "別": "别",
  "張": "张",
  "歐": "欧",
  "國": "国",
  "華": "华",
  "話": "话",
  "該": "该",
  "評": "评",
  "論": "论",
  "讓": "让",
  "請": "请",
  "謝": "谢",
  "誰": "谁",
  "點": "点",
  "選": "选",
  "進": "进",
  "後": "后",
  "對": "对",
  "單": "单",
  "團": "团",
  "帶": "带",
  "動": "动",
  "師": "师",
  "業": "业",
  "輸": "输",
  "給": "给",
  "問": "问",
  "麼": "么",
  "覺": "觉",
  "應": "应",
  "態": "态",
  "長": "长",
  "幾": "几",
  "條": "条",
  "裡": "里",
  "裏": "里",
  "邊": "边",
  "盡": "尽",
  "與": "与",
  "參": "参",
  "雙": "双",
  "兒": "儿",
  "氣": "气",
  "網": "网",
  "雲": "云",
  "樂": "乐",
  "風": "风",
  "險": "险",
  "驗": "验",
  "標": "标",
  "題": "题",
  "歷": "历",
  "據": "据",
  "庫": "库",
  "樣": "样",
  "質": "质",
  "號": "号",
  "碼": "码",
  "錄": "录",
  "轉": "转",
  "寫": "写",
  "導": "导",
  "審": "审",
  "發": "发",
  "佈": "布",
  "觀": "观",
  "紅": "红",
  "綠": "绿",
  "藍": "蓝",
  "黃": "黄",
  "萬": "万",
  "億": "亿",
  "體": "体",
  "讀": "读",
  "處": "处",
  "級": "级",
  "權": "权"
};

export function toZhHans(value: unknown): string {
  let text = String(value || "");
  for (const [wrong, right] of ZH_HANS_PHRASES) {
    text = text.split(wrong).join(right);
  }
  return Array.from(text, char => ZH_HANS_CHARS[char] || char).join("");
}

export function readInitialState<T>(fallback: T): T {
  const node = document.getElementById("dso-initial-state");
  const raw = node?.textContent?.trim();
  if (!raw || raw === "__DSO_INITIAL_STATE__") {
    return fallback;
  }
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function fmtSeconds(value: unknown): string {
  const seconds = Number(value || 0);
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${rest}`;
}

export function fmtTimeRange(row: CandidateRow): string {
  return `${fmtSeconds(row.start_time)} - ${fmtSeconds(row.end_time)} (${Math.round(Number(row.duration_seconds || 0))}s)`;
}

export function clipText(value: unknown, maxLength = 92): string {
  const text = toZhHans(value).trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}...` : text;
}

export function scoreSignals(row: CandidateRow): Record<string, string> {
  const text = row.score_explanation || "";
  const find = (pattern: RegExp) => {
    const match = text.match(pattern);
    return match ? Number(match[1]).toFixed(0) : "-";
  };
  return {
    hook: find(/首5秒留存\s*([0-9.]+)/),
    music: find(/音乐爆点\s*([0-9.]+)/),
    context: find(/上下文完整度\s*([0-9.]+)/),
    comment: find(/互动评论触发\s*([0-9.]+)/),
    originality: find(/低原创\/负反馈风险\s*([0-9.]+)/)
  };
}

export function healthStatusClass(level?: string): string {
  if (level === "good") return "ok";
  if (level === "risk") return "risk";
  return "warn";
}

export function healthLevelLabel(level?: string): string {
  if (level === "good") return "稳定";
  if (level === "risk") return "高风险";
  return "需复核";
}

export function gateLevelClass(gate?: QualityReport["gate"]): string {
  const status = gate?.status || "";
  const severity = gate?.severity || "";
  if (status === "block" || severity === "risk") return "risk";
  if (status === "allow" || severity === "ok") return "good";
  return "warn";
}

export function gateStatusLabel(status?: string): string {
  if (status === "allow") return "放行";
  if (status === "block") return "暂缓";
  return "复核";
}

export function gateAction(gate?: QualityReport["gate"]): ActionHint {
  const action = gate?.primary_action || {};
  const status = gate?.status || "";
  if (action.kind) return action;
  if (status === "allow") return { kind: "export_preview", label: "导出预览", description: "选择高分候选生成 9:16 MP4，进入人工终审。" };
  if (status === "block") return { kind: "open_review_queue", label: "处理阻断项", description: "先处理质量、授权或流程缺口，再刷新 Gate。" };
  return { kind: "open_review_queue", label: "先复核", description: "检查字幕、上下文、授权和广告口播，通过后再导出预览。" };
}

export function decisionStatusClass(severity?: string): string {
  if (severity === "ok") return "ok";
  if (severity === "risk") return "risk";
  if (severity === "info" || severity === "neutral") return "neutral";
  return "warn";
}

export function reviewStatusClass(status?: string): string {
  if (status === "approved" || status === "exported") return "ok";
  if (status === "blocked") return "risk";
  if (status === "needs_review") return "warn";
  return "neutral";
}

export function simulationDecisionForSegment(report: QualityReport | null, segmentId?: string): QualityDecision | null {
  const decisions = Array.isArray(report?.simulation?.decisions) ? report?.simulation?.decisions : [];
  return decisions.find(item => item.segment_id === segmentId) || null;
}

export function qualityFlagsForSegment(report: QualityReport | null, segmentId?: string): string[] {
  const watchlist = Array.isArray(report?.watchlist) ? report?.watchlist : [];
  const item = watchlist.find(row => row.segment_id === segmentId);
  return item && Array.isArray(item.flags) ? item.flags : [];
}

export function previewStateFromRow(row: CandidateRow, variant?: VariantRow | null) {
  const titles = Array.isArray(row.title_suggestions) ? row.title_suggestions : [];
  return {
    segmentId: row.id,
    title: titles[0] || row.summary || "候选片段",
    timeRange: fmtTimeRange(row),
    duration: `${Math.round(Number(row.duration_seconds || 0))}s`,
    score: Number(row.final_score || 0).toFixed(1),
    type: row.music_slice_type || "短视频切片",
    url: variant?.export_url || "",
    coverUrl: variant?.cover_url || "",
    exportPath: variant?.export_path || ""
  };
}
