<template>
  <aside id="inspector" class="panel inspector">
    <div class="panel-head">
      <div>
        <h2 class="panel-title"><Icon name="play-square" />当前候选概览</h2>
        <p class="panel-subtitle">决策、预览与证据在同一处完成</p>
      </div>
    </div>

    <div v-if="row" class="inspector-decision-summary">
      <div>
        <span class="candidate-time">{{ state.preview?.timeRange || "当前候选" }}</span>
        <strong>{{ titles[0] || row.summary || "候选片段" }}</strong>
        <div class="inspector-decision-meta">
          <span>综合分 {{ Number(row.final_score || 0).toFixed(1) }}</span>
          <span class="status" :class="decisionStatusClass(advisorySeverity)">{{ advisoryTitle }}</span>
        </div>
      </div>
      <button class="primary" type="button" :data-primary-decision="row.id" :disabled="Boolean(state.busyKey)" @click="runPrimaryDecision">
        <Icon :name="decisionCta.icon" />{{ decisionCta.label }}
      </button>
    </div>

    <div id="preview-panel" class="preview-panel">
      <div v-if="!state.preview" class="preview-shell">
        <div class="preview-empty"><Icon name="play-square" /><strong>在线预览</strong><span>选择候选后查看时段、评分和导出预览。</span></div>
      </div>
      <template v-else>
        <template v-if="!state.preview.url">
          <div class="preview-shell preview-shell-empty"><div class="preview-empty"><Icon name="play-square" /><strong>尚未导出</strong><span>点击下方按钮生成 9:16 MP4 后即可在线播放。</span></div></div>
        </template>
        <template v-else>
          <div class="preview-shell">
            <video controls preload="metadata" :poster="state.preview.coverUrl || ''" :src="state.preview.url"></video>
          </div>
          <div class="preview-actions">
            <a class="button-link" :href="state.preview.url" target="_blank" rel="noopener"><Icon name="external-link" />打开</a>
            <button type="button" :data-copy="state.preview.exportPath || state.preview.url" @click="copyText(state.preview.exportPath || state.preview.url)"><Icon name="copy" />路径</button>
          </div>
        </template>
      </template>
    </div>

    <div id="detail-panel" class="detail-panel">
      <div v-if="!row" class="empty"><Icon name="panel-right" /><strong>等待候选</strong><span>选中候选后，这里按决策、字幕/ASR、历史先验、包装/平台分区展示。</span></div>
      <template v-else>
        <div class="inspector-tabs" role="tablist" aria-label="候选详情分区">
          <button
            v-for="tab in inspectorTabs"
            :key="tab.key"
            type="button"
            role="tab"
            :id="`inspector-tab-${tab.key}`"
            :aria-selected="state.inspectorSection === tab.key ? 'true' : 'false'"
            :aria-controls="`inspector-panel-${tab.key}`"
            :tabindex="state.inspectorSection === tab.key ? 0 : -1"
            :class="{ active: state.inspectorSection === tab.key }"
            @click="state.inspectorSection = tab.key"
          >
            <Icon :name="tab.icon" />{{ tab.label }}
          </button>
        </div>

        <template v-if="state.inspectorSection === 'decision'">
          <div class="decision-card">
            <div>
              <span class="meta">候选决策</span>
              <strong>{{ decisionCta.title }}</strong>
              <p>{{ decisionCta.detail }}</p>
            </div>
            <button class="primary" type="button" :data-primary-decision="row.id" @click="runPrimaryDecision">
              <Icon :name="decisionCta.icon" />{{ decisionCta.label }}
            </button>
          </div>

          <div class="detail-section">
            <div class="detail-title"><Icon name="history" />历史先验摘要</div>
            <div v-if="history" class="detail-metrics">
              <span>账号基线<strong>{{ baselinePercent || "-" }}</strong></span>
              <span>高互动匹配<strong>{{ Number(history.similar_high_perf_score || 0).toFixed(1) }}</strong></span>
              <span>低互动风险<strong>{{ Number(history.similar_low_perf_risk || 0).toFixed(1) }}</strong></span>
              <span>置信<strong>{{ confidenceText(history.confidence_label) }}</strong></span>
            </div>
            <div v-if="history" class="meta">{{ historyStatusText }}</div>
            <div v-else class="meta">暂无历史先验，样本不足时不会输出确定性权重。</div>
            <div class="detail-actions">
              <button type="button" @click="state.inspectorSection = 'history'"><Icon name="panel-right-open" />查看完整解释</button>
            </div>
          </div>

          <div class="detail-section">
            <div class="detail-title"><Icon name="activity" />评分拆解</div>
            <div class="score-breakdown">
              <div class="score-chip"><span>综合分</span><strong>{{ Number(row.final_score || 0).toFixed(1) }}</strong></div>
              <div class="score-chip"><span>首5秒</span><strong>{{ signals.hook }}</strong></div>
              <div class="score-chip"><span>音乐爆点</span><strong>{{ signals.music }}</strong></div>
              <div class="score-chip"><span>上下文</span><strong>{{ signals.context }}</strong></div>
              <div class="score-chip"><span>评论触发</span><strong>{{ signals.comment }}</strong></div>
              <div class="score-chip"><span>低原创风险</span><strong>{{ signals.originality }}</strong></div>
            </div>
          </div>

          <div class="detail-section">
            <div class="detail-title"><Icon name="clipboard-check" />人工复核</div>
            <div class="review-current">
              <div>
                <span class="meta">当前结论</span>
                <strong>{{ row.review_status_label || reviewStatusLabel(row.review_status) }}</strong>
                <p>{{ row.review_status_reason || latestReviewReason || "等待人工扫读字幕、上下文和历史先验。" }}</p>
              </div>
              <span class="status" :class="reviewStatusClass(reviewClassStatus(row.review_status))">{{ reviewStatusLabel(row.review_status) }}</span>
            </div>
            <div class="detail-actions secondary-actions review-actions">
              <button
                type="button"
                data-review-status="approved"
                :data-review-segment="row.id"
                :class="{ active: isCurrentReview('approved') }"
                :disabled="isCurrentReview('approved') || isReviewBusy('approved')"
                @click="review('approved')"
              >
                <Icon name="check" />通过
              </button>
              <button
                type="button"
                data-review-status="review"
                :data-review-segment="row.id"
                :class="{ active: isCurrentReview('review') }"
                :disabled="isCurrentReview('review') || isReviewBusy('review')"
                @click="review('review')"
              >
                <Icon name="circle-alert" />需复核
              </button>
              <button
                type="button"
                data-review-status="blocked"
                :data-review-segment="row.id"
                :class="{ active: isCurrentReview('blocked') }"
                :disabled="isCurrentReview('blocked') || isReviewBusy('blocked')"
                @click="review('blocked')"
              >
                <Icon name="ban" />暂缓
              </button>
            </div>
            <div v-if="latestReviewEvent" class="review-latest">
              <span class="meta">最近记录</span>
              <span class="status" :class="reviewStatusClass(reviewClassStatus(latestReviewEvent.review_status))">{{ reviewStatusLabel(latestReviewEvent.review_status) }}</span>
              <span>{{ clipText(latestReviewReason || latestReviewEvent.created_at || "", 84) }}</span>
            </div>
            <button v-if="olderReviewEvents.length" type="button" class="review-history-toggle" @click="showReviewHistory = !showReviewHistory">
              <Icon name="history" />{{ showReviewHistory ? "收起历史记录" : `查看历史记录 ${olderReviewEvents.length} 条` }}
            </button>
            <div v-if="showReviewHistory" class="review-history">
              <div v-for="event in olderReviewEvents" :key="`${event.review_status}-${event.created_at}`" class="quality-row">
                <span class="status" :class="reviewStatusClass(reviewClassStatus(event.review_status))">{{ reviewStatusLabel(event.review_status) }}</span>
                <span>{{ clipText(event.reason || event.created_at || "", 84) }}</span>
              </div>
            </div>
            <div v-if="!reviewEvents.length" class="meta">暂无人工复核记录</div>
          </div>

          <div class="detail-section">
            <div class="detail-title"><Icon name="file-clock" />运行清单</div>
            <template v-if="manifestSteps.length">
              <div class="manifest-mini">
                <span v-for="step in manifestSteps" :key="step.step" :class="step.status || 'missing'">{{ step.step }}</span>
              </div>
              <div class="meta">完成度 {{ Math.round(Number(state.manifest?.completion_ratio || 0) * 100) }}% / 下一步 {{ state.manifest?.next_action?.label || "-" }}</div>
            </template>
            <div v-else class="meta">运行清单暂未加载</div>
          </div>

          <div class="detail-section"><div class="detail-title"><Icon name="sparkles" />推荐理由</div><div>{{ row.score_explanation || "" }}</div></div>
        </template>

        <template v-else-if="state.inspectorSection === 'asr'">
          <div v-if="qualityFlags.length" class="module-warning">
            <Icon name="circle-alert" /><span>{{ qualityFlags.join(" / ") }}</span>
          </div>
          <div class="detail-section"><div class="detail-title"><Icon name="captions" />字幕预览</div><div class="caption-box">{{ row.transcript || "暂无字幕" }}</div></div>
          <div class="detail-section">
            <div class="detail-title"><Icon name="audio-lines" />ASR 二次验证</div>
            <div v-if="row.latest_asr_verification?.id" class="quality-row">
              <span class="status" :class="Number(row.latest_asr_verification.difference_score || 0) >= 0.35 ? 'warn' : 'ok'">差异 {{ Number(row.latest_asr_verification.difference_score || 0).toFixed(2) }}</span>
              <span>{{ clipText(row.latest_asr_verification.model_name || row.latest_asr_verification.profile || "verify", 54) }}</span>
            </div>
            <div v-else class="meta">尚未对该候选做 verify 模型二次转写。</div>
            <div class="detail-actions"><button type="button" :data-verify-asr="row.id" @click="verify"><Icon name="scan-text" />verify 转写</button></div>
          </div>
        </template>

        <template v-else-if="state.inspectorSection === 'history'">
          <div id="inspector-panel-history" class="detail-section simulation-compare" role="tabpanel" aria-labelledby="inspector-tab-history">
            <div class="simulation-compare-head">
              <div>
                <div class="detail-title"><Icon name="radar" />推荐模拟</div>
                <div class="meta">在当前候选上下文中比较推荐阶段和主要瓶颈。</div>
              </div>
              <button type="button" :disabled="!state.selectedVideoId || state.busyKey === 'inspector-simulation'" @click="refreshSimulation">
                <span v-if="state.busyKey === 'inspector-simulation'" class="spinner"></span>
                <Icon v-else name="refresh-cw" />比较模拟
              </button>
            </div>
            <div v-if="currentSimulation" class="simulation-compare-grid">
              <span>当前评分<strong>{{ Number(row.final_score || 0).toFixed(1) }}</strong></span>
              <span>模拟评分<strong>{{ Number(currentSimulation.simulated_score || 0).toFixed(1) }}</strong></span>
              <span>推荐阶段<strong>{{ currentSimulation.predicted_stage || state.simulationSummary.top_stage || "待判断" }}</strong></span>
              <span>主要瓶颈<strong>{{ currentSimulation.bottleneck?.label || state.simulationSummary.top_bottleneck || "暂无" }}</strong></span>
            </div>
            <div v-else class="simulation-inline-empty">{{ state.moduleStatus.simulation.error || "点击“比较模拟”读取当前候选的推荐链路结果。" }}</div>
          </div>
          <div v-if="state.moduleStatus.history.error" class="module-error">
            <Icon name="circle-alert" /><span>{{ state.moduleStatus.history.error }}</span>
          </div>
          <div class="detail-section">
            <div class="detail-title"><Icon name="history" />历史相似</div>
            <div v-if="history" class="detail-metrics">
              <span>历史样本<strong>{{ Number(history.sample_count || 0) }}</strong></span>
              <span>高互动匹配<strong>{{ Number(history.similar_high_perf_score || 0).toFixed(1) }}</strong></span>
              <span>低互动风险<strong>{{ Number(history.similar_low_perf_risk || 0).toFixed(1) }}</strong></span>
              <span v-if="baselinePercent">账号基线<strong>{{ baselinePercent }}</strong></span>
              <span v-if="history.confidence_label">置信<strong>{{ confidenceText(history.confidence_label) }}</strong></span>
            </div>
            <div v-if="history" class="meta" style="margin-top:6px;">{{ historyStatusText }}</div>
            <div v-if="rankerReason" class="quality-row" style="margin-top:8px;">
              <span class="status neutral">排序器</span>
              <span>{{ clipText(rankerReason, 116) }}</span>
            </div>
            <div v-if="rankerAdviceText" class="quality-row" style="margin-top:8px;">
              <span class="status" :class="rankerAdviceClass">{{ rankerAdviceLabel }}</span>
              <span>{{ clipText(rankerAdviceText, 116) }}</span>
            </div>
            <div v-if="semanticGapText" class="quality-row" style="margin-top:8px;">
              <span class="status neutral">基线差异</span>
              <span>{{ clipText(semanticGapText, 116) }}</span>
            </div>
            <div v-if="embeddingEvidenceText" class="quality-row" style="margin-top:8px;">
              <span class="status neutral">相似历史证据</span>
              <span>{{ clipText(embeddingEvidenceText, 116) }}</span>
            </div>
            <div v-if="embeddingEvidence.embedding_ranker_reason" class="quality-row" style="margin-top:8px;">
              <span class="status neutral">Qwen</span>
              <span>{{ clipText(String(embeddingEvidence.embedding_ranker_reason || ''), 116) }}</span>
            </div>
            <div v-if="componentRows.length" class="detail-metrics" style="margin-top:8px;">
              <span v-for="item in componentRows" :key="item.key">{{ item.label }}<strong>{{ item.value }}</strong></span>
            </div>
            <template v-if="embeddingEvidenceRows.length">
              <div v-for="item in embeddingEvidenceRows" :key="`${item.label}-${item.historical_sample_id || item.platform_item_id || item.title}`" class="quality-row">
                <span class="status" :class="String(item.status || 'neutral')">{{ item.label }}</span>
                <span>{{ clipText(`${item.title || item.platform_item_id || '历史样本'} / 相似 ${Number(item.similarity || 0).toFixed(2)} / 热度 ${Number(item.normalized_reward || item.reward_proxy || 0).toFixed(1)}`, 116) }}</span>
              </div>
            </template>
            <template v-if="prototypeHits.length">
              <div v-for="item in prototypeHits.slice(0, 3)" :key="String(item.prototype_key || item.prototype_name)" class="quality-row">
                <span class="status ok">原型</span>
                <span>{{ clipText(`${item.prototype_name || "高互动原型"} / fit ${Number(item.fit_score || 0).toFixed(1)} / 置信 ${Number(item.confidence || 0).toFixed(2)}`, 96) }}</span>
              </div>
            </template>
            <template v-if="riskHits.length">
              <div v-for="item in riskHits.slice(0, 2)" :key="String(item.risk_key || item.risk_name)" class="quality-row">
                <span class="status warn">风险</span>
                <span>{{ clipText(`${item.risk_name || "低互动风险"} / risk ${Number(item.risk_score || 0).toFixed(1)} / 置信 ${Number(item.confidence || 0).toFixed(2)}`, 96) }}</span>
              </div>
            </template>
            <template v-if="historyMatches.length">
              <div v-for="item in historyMatches.slice(0, 6)" :key="item.historical_sample_id || item.matched_sample_id || item.matched_segment_id || item.title" class="quality-row">
                <span class="status" :class="matchStatusClass(item.match_type)">{{ matchLabel(item.match_type) }}</span>
                <span>{{ clipText(`${item.title || item.platform_item_id || "历史样本"} / ${item.account_display_name || item.account_id || "账号"} / 相似 ${Number(item.similarity || 0).toFixed(2)} / 热度 ${Number(item.normalized_reward || item.reward_proxy || 0).toFixed(1)}`, 116) }}</span>
              </div>
            </template>
            <div v-else class="meta">暂无可用历史相似样本，样本不足时不会输出确定性权重。</div>
          </div>
        </template>

        <template v-else>
          <div class="detail-section">
            <div class="detail-title"><Icon name="text" />标题建议</div>
            <ol v-if="titles.length" class="title-list"><li v-for="title in titles" :key="title">{{ title }}</li></ol>
            <div v-else class="meta">暂无标题建议</div>
          </div>
          <div class="detail-section"><div class="detail-title"><Icon name="image" />封面建议</div><div>{{ row.cover_suggestion || "优先选择歌手强表情、导师反应或舞台高潮帧" }}</div></div>
          <div class="detail-section">
            <div class="detail-title"><Icon name="flask-conical" />Variant 实验</div>
            <template v-if="variants.length">
              <div v-for="variant in variants.slice(0, 3)" :key="variant.id || variant.title" class="quality-row">
                <span class="status neutral">{{ variant.status || "draft" }}</span>
                <span>{{ clipText(`${variant.changed_variable || "default"} / ${variant.hypothesis || variant.title || ""}`, 70) }}</span>
              </div>
            </template>
            <div v-else class="meta">暂无 A/B variant，点击下方按钮创建默认实验版本</div>
            <div class="detail-actions"><button type="button" :data-create-variant="row.id" @click="create"><Icon name="copy-plus" />创建版本</button></div>
          </div>
          <div class="detail-section">
            <div class="detail-title"><Icon name="radio-tower" />平台映射</div>
            <template v-if="mappings.length">
              <div v-for="item in mappings.slice(0, 3)" :key="item.platform_item_id" class="quality-row">
                <span class="status ok">{{ item.sync_status || "linked" }}</span>
                <span>{{ item.platform_item_id || "" }} / {{ item.last_metrics_at || item.last_synced_at || "未同步" }}</span>
              </div>
            </template>
            <div v-else class="meta">暂无平台 item 绑定，有授权平台 item 时先绑定 aweme_id 再同步指标。</div>
            <div class="detail-actions"><button type="button" :data-bind-platform="row.id" @click="bind"><Icon name="link" />绑定 item</button></div>
          </div>
          <div class="detail-section">
            <div class="detail-title"><Icon name="database" />授权反馈样本</div>
            <div v-if="feedback.sample_count" class="detail-metrics">
              <span>样本<strong>{{ Number(feedback.sample_count || 0) }}</strong></span>
              <span>最佳互动热度<strong>{{ Number(feedback.best_reward || 0).toFixed(1) }}</strong></span>
              <span>归一化<strong>{{ Number(feedback.best_normalized_reward || 0).toFixed(1) }}</strong></span>
            </div>
            <div v-else class="meta">暂无该候选的授权反馈样本</div>
          </div>
          <div class="detail-section"><div class="detail-title"><Icon name="shield-alert" />风险提示</div><div v-for="note in riskNotes" :key="note">{{ note }}</div></div>
          <div class="detail-section">
            <div class="detail-title"><Icon name="blocks" />内容结构</div>
            <div>{{ row.short_video_structure || row.summary || "" }}</div>
            <div class="meta" style="margin-top:6px;">{{ row.program_context || "" }} / {{ row.comment_trigger || "" }}</div>
          </div>
        </template>
      </template>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import Icon from "./Icon.vue";
import type { ReviewEvent, SegmentHistoryResult } from "../types";
import { useDashboardContext } from "../composables/dashboardContext";
import {
  clipText,
  decisionStatusClass,
  gateAction,
  reviewStatusClass,
  scoreSignals,
  simulationDecisionForSegment
} from "../utils";

const {
  state,
  selectedSegment,
  exportSegment,
  reviewSegment,
  verifyAsr,
  createVariant,
  bindPlatform,
  loadSimulation,
  copyText,
  qualityFlagsFor,
  withBusy,
  toast
} = useDashboardContext();

const row = selectedSegment;
const showReviewHistory = ref(false);
const inspectorTabs = [
  { key: "decision", label: "决策", icon: "clipboard-check" },
  { key: "asr", label: "字幕", icon: "captions" },
  { key: "history", label: "判断依据", icon: "history" },
  { key: "packaging", label: "发布", icon: "text" }
] as const;
const titles = computed(() => Array.isArray(row.value?.title_suggestions) ? row.value?.title_suggestions || [] : []);
const riskNotes = computed(() => {
  const notes = Array.isArray(row.value?.risk_notes) ? row.value?.risk_notes || [] : [];
  return notes.length ? notes : ["合格 sample 数据，可进入评分与导出流程"];
});
const variants = computed(() => Array.isArray(row.value?.variants) ? row.value?.variants || [] : []);
const mappings = computed(() => Array.isArray(row.value?.platform_mappings) ? row.value?.platform_mappings || [] : []);
const reviewEvents = computed(() => Array.isArray(row.value?.review_events) ? row.value?.review_events || [] : []);
const latestReviewEvent = computed<ReviewEvent | null>(() => reviewEvents.value[0] || null);
const olderReviewEvents = computed(() => reviewEvents.value.slice(1));
const latestReviewReason = computed(() => String(latestReviewEvent.value?.reason || ""));
const feedback = computed(() => row.value?.feedback_summary || {});
const researchHistory = computed<SegmentHistoryResult | null>(() => {
  const signals = row.value?.learning_signals;
  return signals && typeof signals === "object" ? signals : null;
});
const history = computed<SegmentHistoryResult | null>(() => {
  const apiHistory = row.value?.id ? state.segmentHistory[row.value.id] || null : null;
  if (!researchHistory.value) return apiHistory;
  return {
    ...(apiHistory || {}),
    ...researchHistory.value,
    matches: researchHistory.value.matches || apiHistory?.matches || []
  };
});
const historyMatches = computed(() => Array.isArray(history.value?.matches) ? history.value?.matches || [] : []);
const prototypeHits = computed(() => Array.isArray(history.value?.prototype_hits) ? history.value?.prototype_hits || [] : []);
const riskHits = computed(() => Array.isArray(history.value?.low_interaction_risk_library) ? history.value?.low_interaction_risk_library || [] : []);
const baseline = computed(() => history.value?.account_baseline_position || {});
const componentScores = computed<Record<string, unknown>>(() => {
  const scores = history.value?.component_scores;
  return scores && typeof scores === "object" ? scores as Record<string, unknown> : {};
});
const embeddingEvidence = computed<Record<string, unknown>>(() => {
  const value = history.value?.embedding_evidence;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const embeddingQuality = computed<Record<string, unknown>>(() => {
  const value = embeddingEvidence.value.embedding_evidence_quality;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const embeddingEvidenceText = computed(() => {
  if (!Object.keys(embeddingEvidence.value).length) return "";
  const textScore = Number(embeddingEvidence.value.text_similarity_score || 0);
  const visualScore = Number(embeddingEvidence.value.visual_similarity_score || 0);
  const quality = Number(embeddingQuality.value.score || 0);
  const scope = String(embeddingEvidence.value.embedding_scope || "account");
  return `文本 ${textScore.toFixed(2)} / 视觉 ${visualScore.toFixed(2)} / 质量 ${quality.toFixed(2)} / ${scope}`;
});
const embeddingEvidenceRows = computed(() => {
  const groups: Array<[string, string, string]> = [
    ["matched_text_high_samples", "文本高互动", "ok"],
    ["matched_text_low_samples", "文本低风险", "warn"],
    ["matched_visual_high_samples", "视觉高互动", "ok"],
    ["matched_visual_low_samples", "视觉低风险", "warn"]
  ];
  const rows: Array<Record<string, unknown>> = [];
  for (const [key, label, status] of groups) {
    const values = embeddingEvidence.value[key];
    if (!Array.isArray(values)) continue;
    for (const item of values.slice(0, 2)) {
      rows.push({ ...(item as Record<string, unknown>), label, status });
    }
  }
  return rows.slice(0, 6);
});
const componentRows = computed(() => ["high_similarity", "low_interaction_risk", "account_baseline_position", "prototype_fit", "semantic_label_trust"].map((key) => ({
  key,
  label: componentLabel(key),
  value: Number(componentScores.value[key] || 0).toFixed(key === "semantic_label_trust" ? 0 : 1)
})).filter((item) => Number(item.value) > 0));
const rankerReason = computed(() => String(history.value?.ranker_reason || ""));
const rankerAdvice = computed<Record<string, unknown>>(() => {
  const advice = history.value?.ranker_advice;
  return advice && typeof advice === "object" ? advice as Record<string, unknown> : {};
});
const rankerAdviceLabel = computed(() => String(rankerAdvice.value.label || adviceLabel(String(rankerAdvice.value.action || ""))));
const rankerAdviceText = computed(() => String(rankerAdvice.value.reason || ""));
const semanticGapText = computed(() => {
  const value = history.value?.semantic_gap_reason;
  if (!value || typeof value !== "object") return "";
  const reason = value as Record<string, unknown>;
  const delta = Number(reason.delta || 0);
  const text = String(reason.reason || "");
  return `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} / ${text}`;
});
const rankerAdviceClass = computed(() => {
  const actionName = String(rankerAdvice.value.action || "");
  if (actionName === "recommend_export_preview") return "ok";
  if (actionName === "low_interaction_risk_review" || actionName === "low_evidence_hold") return "warn";
  return "neutral";
});
const baselinePercent = computed(() => {
  const value = Number(baseline.value.percentile);
  return Number.isFinite(value) ? `${Math.round(value * 100)}%` : "";
});
const baselineCompactText = computed(() => {
  const confidence = confidenceText(history.value?.confidence_label);
  return baselinePercent.value ? `${baselinePercent.value} / ${confidence}置信` : `${confidence}置信`;
});
const topPrototypeName = computed(() => clipText(prototypeHits.value[0]?.prototype_name || "未命中", 22));
const topRiskName = computed(() => clipText(riskHits.value[0]?.risk_name || "未命中", 22));
const manifestSteps = computed(() => Array.isArray(state.manifest?.steps) ? state.manifest?.steps || [] : []);
const signals = computed(() => row.value ? scoreSignals(row.value) : scoreSignals({ id: "" }));
const qualityFlags = computed(() => row.value ? qualityFlagsFor(row.value.id) : []);
const gate = computed(() => state.quality?.gate || {});
const action = computed(() => gateAction(gate.value));
const linkedDecision = computed(() => simulationDecisionForSegment(state.quality, state.preview?.segmentId));
const currentSimulation = computed(() => state.simulations.find(item => item.segment_id === row.value?.id) || null);
const advisorySeverity = computed(() => linkedDecision.value?.severity || gate.value.severity || "warn");
const advisoryTitle = computed(() => linkedDecision.value?.label || action.value.label || "导出决策");
const advisoryDetail = computed(() => linkedDecision.value?.action || action.value.description || gate.value.summary || "Gate 为只读提示，不会自动阻断导出。");
const historyStatusText = computed(() => {
  if (!history.value) return "";
  const scope = history.value.match_scope || "account";
  const source = history.value.history_source === "published_research_samples" || history.value.history_source === "historical_capture_samples" ? "研究样本" : "授权反馈样本";
  const confidence = history.value.status === "ready" ? "可用于趋势判断" : (history.value.status === "low_confidence" ? "低置信趋势" : "样本不足");
  const baselineText = baseline.value.position_text ? ` / ${baseline.value.position_text}` : "";
  const uncertainty = history.value.history_uncertainty === undefined ? "" : ` / 不确定性 ${Number(history.value.history_uncertainty || 0).toFixed(2)}`;
  return `${source} / ${scope} / ${confidence}${baselineText}${uncertainty}`;
});
const decisionCta = computed(() => {
  const hasAsrRisk = qualityFlags.value.some(flag => /ASR|字幕|重复|幻觉/.test(flag));
  if (hasAsrRisk && !row.value?.latest_asr_verification?.id) {
    return {
      title: "先复核字幕质量",
      detail: "当前候选命中 ASR 或字幕风险，先做 verify 转写再决定是否导出。",
      label: "先复核 ASR",
      icon: "scan-text",
      action: "verify"
    };
  }
  if (row.value?.review_status !== "approved" && row.value?.review_status !== "exported") {
    return {
      title: "等待人工判断",
      detail: "扫读字幕、上下文和历史先验后，先把候选标记为通过或需复核。",
      label: "人工通过",
      icon: "check",
      action: "approve"
    };
  }
  if (!state.preview?.url) {
    return {
      title: "可生成预览",
      detail: "人工已通过或质量风险较低，可以生成 9:16 预览继续检查包装效果。",
      label: "导出预览",
      icon: "download",
      action: "export"
    };
  }
  return {
    title: "已生成预览",
    detail: "可继续查看历史研究解释，或在包装/平台分区绑定平台 item。",
    label: "查看研究解释",
    icon: "history",
    action: "history"
  };
});

watch(() => row.value?.id, () => {
  showReviewHistory.value = false;
});

async function refreshSimulation(): Promise<void> {
  if (!state.selectedVideoId) return;
  await withBusy("inspector-simulation", () => loadSimulation(state.selectedVideoId));
}

async function runPrimaryDecision(): Promise<void> {
  if (!row.value) return;
  if (decisionCta.value.action === "verify") {
    state.inspectorSection = "asr";
    await verify();
    return;
  }
  if (decisionCta.value.action === "approve") {
    await review("approved");
    return;
  }
  if (decisionCta.value.action === "export") {
    await exportSelected();
    return;
  }
  state.inspectorSection = "history";
}

async function exportSelected(): Promise<void> {
  if (!state.preview?.segmentId) return;
  await withBusy(`export-${state.preview.segmentId}`, () => exportSegment(state.preview!.segmentId));
}

async function review(status: string): Promise<void> {
  if (!row.value) return;
  if (isCurrentReview(status)) {
    toast("复核结论未变化");
    return;
  }
  await withBusy(`review-${row.value.id}-${status}`, () => reviewSegment(row.value!.id, status));
}

function normalizedReviewStatus(status?: string): string {
  const value = String(status || "").trim();
  if (value === "needs_review" || value === "need_review" || value === "corrected") return "review";
  if (value === "ready") return "approved";
  if (value === "rejected") return "blocked";
  return value || "candidate";
}

function reviewStatusLabel(status?: string): string {
  const value = normalizedReviewStatus(status);
  if (value === "approved") return "通过";
  if (value === "review") return "需复核";
  if (value === "blocked") return "暂缓";
  if (value === "exported") return "已导出";
  return "待审核";
}

function reviewClassStatus(status?: string): string {
  const value = normalizedReviewStatus(status);
  return value === "review" ? "needs_review" : value;
}

function isCurrentReview(status: string): boolean {
  return normalizedReviewStatus(row.value?.review_status) === normalizedReviewStatus(status);
}

function isReviewBusy(status: string): boolean {
  return Boolean(row.value?.id && state.busyKey === `review-${row.value.id}-${status}`);
}

async function verify(): Promise<void> {
  if (!row.value) return;
  await withBusy(`verify-${row.value.id}`, () => verifyAsr(row.value!.id));
}

async function create(): Promise<void> {
  if (!row.value) return;
  await withBusy(`variant-${row.value.id}`, () => createVariant(row.value!.id));
}

async function bind(): Promise<void> {
  if (!row.value) return;
  const platformItemId = window.prompt("输入抖音 item_id / aweme_id");
  if (!platformItemId) return;
  await withBusy(`bind-${row.value.id}`, () => bindPlatform(row.value!.id, platformItemId)).catch(error => toast(error.message));
}

function matchLabel(value?: string): string {
  if (value === "high") return "高互动";
  if (value === "low") return "低互动";
  return "相似";
}

function matchStatusClass(value?: string): string {
  if (value === "high") return "ok";
  if (value === "low") return "warn";
  return "neutral";
}

function componentLabel(value: string): string {
  const labels: Record<string, string> = {
    high_similarity: "高互动",
    low_interaction_risk: "低互动风险",
    account_baseline_position: "账号基线",
    prototype_fit: "原型",
    semantic_label_trust: "可信"
  };
  return labels[value] || value;
}

function adviceLabel(value: string): string {
  const labels: Record<string, string> = {
    recommend_export_preview: "建议预览",
    needs_context_review: "上下文复核",
    low_evidence_hold: "低证据暂缓",
    low_interaction_risk_review: "低互动复核"
  };
  return labels[value] || "排序建议";
}

function confidenceText(value?: unknown): string {
  if (value === "high") return "高";
  if (value === "medium") return "中";
  if (value === "low") return "低";
  return String(value || "-");
}
</script>
