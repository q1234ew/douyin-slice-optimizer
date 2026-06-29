<template>
  <section id="feedback" class="panel feedback-panel">
    <div class="panel-head">
      <div>
        <h2 class="panel-title"><Icon name="database" />研究学习</h2>
        <p class="panel-subtitle">研究样本、历史先验、校准回测与平台账号</p>
      </div>
      <div class="toolbar-actions">
        <label class="account-switcher" for="feedback-account">
          <span>账号</span>
          <select id="feedback-account" v-model="state.feedbackAccount" aria-label="反馈账号" @change="onFeedbackAccountChange">
            <option value="">全部账号</option>
            <option v-for="account in feedbackAccountOptions" :key="account.id" :value="account.id">
              {{ account.label }}
            </option>
          </select>
        </label>
        <label class="account-switcher" for="feedback-dataset">
          <span>数据集</span>
          <select id="feedback-dataset" v-model="state.feedbackDataset" aria-label="学习数据集" @change="loadFeedbackSafe">
            <option v-for="dataset in feedbackDatasetOptions" :key="dataset.id" :value="dataset.id">
              {{ datasetOptionLabel(dataset) }}
            </option>
          </select>
        </label>
        <button id="rebuild-feedback-btn" type="button" :disabled="state.busyKey === 'rebuild-feedback'" @click="withBusy('rebuild-feedback', rebuildFeedback)">
          <span v-if="state.busyKey === 'rebuild-feedback'" class="spinner"></span>
          <Icon v-else name="refresh-ccw" />刷新数据
        </button>
      </div>
    </div>
    <div class="panel-body">
      <div class="feedback-tabs" role="tablist" aria-label="研究学习分区">
        <button
          v-for="section in feedbackSections"
          :key="section.key"
          type="button"
          role="tab"
          :aria-selected="state.feedbackSection === section.key ? 'true' : 'false'"
          :class="{ active: state.feedbackSection === section.key }"
          @click="state.feedbackSection = section.key"
        >
          <Icon :name="section.icon" />{{ section.label }}
        </button>
      </div>

      <div v-if="moduleAlerts.length" class="feedback-alerts" aria-live="polite">
        <div v-for="item in moduleAlerts" :key="item.key" class="module-error">
          <Icon name="circle-alert" /><span>{{ item.label }}：{{ item.error }}</span>
        </div>
      </div>

      <div v-if="state.feedbackSection === 'overview'" id="feedback-overview" class="research-overview-strip">
        <div>
          <span>样本覆盖</span>
          <strong>{{ countLabel(learningCounts.historical) }}</strong>
          <em>{{ interactionCoverageText }} 四项互动覆盖</em>
        </div>
        <div>
          <span>账号质量</span>
          <strong>{{ accountQualityRows.length || "-" }}</strong>
          <em>{{ accountQualityRows.length ? "账号基线可浏览" : "等待研究样本入库" }}</em>
        </div>
        <div>
          <span>最近回测</span>
          <strong>{{ ndcgText }}</strong>
          <em>{{ latestBacktest ? backtestText : "暂无离线回测" }}</em>
        </div>
        <div>
          <span>平台账号</span>
          <strong>{{ authStatusLabel }}</strong>
          <em>{{ authText }}</em>
        </div>
      </div>

      <div class="feedback-grid">
        <div v-show="state.feedbackSection === 'overview' || state.feedbackSection === 'samples'" class="feedback-block lineage-block">
          <h3><Icon name="database" />数据口径</h3>
          <div class="inner">
            <div class="feedback-overview">
              <div class="overview-card">
                <span>正式历史样本</span>
                <strong>{{ countLabel(learningCounts.historical) }}</strong>
              </div>
              <div class="overview-card">
                <span>源去重</span>
                <strong>{{ countLabel(learningCounts.sourceUnique) }}</strong>
              </div>
              <div class="overview-card">
                <span>可训练样本</span>
                <strong>{{ countLabel(learningCounts.trainingReady) }}</strong>
              </div>
              <div class="overview-card">
                <span>重复视频组</span>
                <strong>{{ countLabel(historicalSummary.duplicate_item_group_count) }}</strong>
              </div>
              <div class="overview-card">
                <span>四项互动覆盖</span>
                <strong>{{ interactionCoverageText }}</strong>
              </div>
              <div class="overview-card">
                <span>播放量缺失</span>
                <strong>{{ percent(historicalSummary.play_missing_rate) }}</strong>
              </div>
            </div>
            <div class="quality-row lineage-note">
              <Icon name="info" />
              <span>{{ lineageStatusText }}</span>
            </div>
          </div>
        </div>

        <div v-show="state.feedbackSection === 'overview' || state.feedbackSection === 'samples'" class="feedback-block account-quality-block">
          <h3><Icon name="users" />账号级数据质量</h3>
          <div class="inner">
            <div v-if="accountQualityRows.length" id="account-quality-summary" class="account-quality-list">
              <div v-for="item in accountQualityRows" :key="item.account_id || 'all'" class="account-quality-row">
                <div class="account-quality-name">
                  <strong>{{ accountDisplayName(item.account_id, item.account_display_name) }}</strong>
                  <span>ID {{ item.account_id || "all" }}{{ item.account_tier ? ` / ${item.account_tier}类` : "" }}</span>
                </div>
                <div class="account-quality-metrics">
                  <span>样本<strong>{{ countLabel(item.trainable_sample_count || item.sample_count) }}</strong></span>
                  <span>互动覆盖<strong>{{ accountCoverageText(item) }}</strong></span>
                  <span>播放缺失<strong>{{ percent(item.play_missing_rate) }}</strong></span>
                  <span>P75<strong>{{ compactNumber(item.reward_p75) }}</strong></span>
                </div>
                <span class="status" :class="item.confidence === 'ready' ? 'ok' : 'warn'">{{ item.confidence_label || confidenceLabel(item.confidence) }}</span>
              </div>
            </div>
            <div v-else class="empty"><Icon name="users" /><strong>暂无账号质量</strong><span>正式样本入库后会显示账号级样本数、互动覆盖和置信状态。</span></div>
          </div>
        </div>

        <div id="feedback-runtime" v-show="state.feedbackSection === 'runtime'" class="feedback-block runtime-block">
          <h3><Icon name="cpu" />运行环境</h3>
          <div class="inner">
            <div id="runtime-status" class="runtime-grid">
              <div class="runtime-item"><span>FFmpeg</span><strong><span class="status" :class="runtime.ffmpeg?.available ? 'ok' : 'warn'">{{ runtime.ffmpeg?.available ? "可用" : "缺失" }}</span></strong></div>
              <div class="runtime-item"><span>FFprobe</span><strong><span class="status" :class="runtime.ffprobe?.available ? 'ok' : 'warn'">{{ runtime.ffprobe?.available ? "可用" : "缺失" }}</span></strong></div>
              <div class="runtime-item"><span>ASR</span><strong><span class="status" :class="asr.status === 'ready' ? 'ok' : 'warn'">{{ backendLabel }}</span></strong></div>
              <div class="runtime-item"><span>加速</span><strong>{{ accelLabel }}</strong></div>
              <div class="runtime-item" style="grid-column:1/-1"><span>说明</span><strong>{{ asr.note || "等待运行环境检测" }}</strong></div>
              <div v-for="profile in profiles" :key="profile.profile" class="runtime-item">
                <span>{{ profile.profile }} · {{ profile.model }}</span>
                <strong>{{ profile.model_exists ? "模型就绪" : "待配置" }} / VAD {{ profile.vad_enabled ? "on" : "off" }}</strong>
                <div class="meta">{{ clipText(profile.purpose || "", 68) }}</div>
              </div>
            </div>
          </div>
        </div>

        <div v-show="state.feedbackSection === 'platform'" class="feedback-block metrics-import-block">
          <span id="feedback-platform" class="section-anchor"></span>
          <h3><Icon name="file-spreadsheet" />授权指标导入</h3>
          <div class="inner">
            <form id="metrics-form" class="form-grid" @submit.prevent="submitMetrics">
              <input id="metrics-file" ref="metricsFile" type="file" name="file" accept=".csv,.xlsx,.xslx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" aria-label="指标文件" required />
              <button class="primary" type="submit" :disabled="state.busyKey === 'metrics-import'">
                <span v-if="state.busyKey === 'metrics-import'" class="spinner"></span>
                <Icon v-else name="upload" />导入文件
              </button>
            </form>
            <div id="metrics-result" class="meta" style="margin-top:10px;">{{ state.metricsResult }}</div>
          </div>
        </div>

        <div id="douyin-account" v-show="state.feedbackSection === 'platform'" class="feedback-block douyin-sync-block">
          <h3><Icon name="radio-tower" />抖音账号</h3>
          <div class="inner">
            <div class="douyin-account-card">
              <div class="douyin-account-main">
                <span>当前账号</span>
                <strong>{{ feedbackAccountLabel }}</strong>
                <div class="meta">{{ douyinAccountMeta }}</div>
              </div>
              <span class="status" :class="authClass">{{ authStatusLabel }}</span>
            </div>
            <div class="quality-row account-status-row">
              <Icon :name="authStatus === 'connected' ? 'shield-check' : 'shield-alert'" />
              <span>{{ authText }}</span>
            </div>
            <div class="sync-actions">
              <button id="douyin-login-btn" class="primary" type="button" :disabled="state.busyKey === 'douyin-login'" @click="withBusy('douyin-login', startDouyinLogin)"><Icon name="qr-code" />连接抖音账号</button>
              <button id="douyin-mock-sync-btn" type="button" :disabled="state.busyKey === 'douyin-mock'" @click="withBusy('douyin-mock', syncDouyinMock)"><Icon name="refresh-cw" />Mock 同步</button>
              <form id="douyin-sync-form" class="inline-file-form" @submit.prevent="submitDouyinFile">
                <input id="douyin-sync-file" ref="douyinFile" type="file" name="file" accept=".csv,.xlsx,.xslx,.json,application/json,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" aria-label="抖音授权数据文件" />
                <button type="submit" :disabled="state.busyKey === 'douyin-file'"><Icon name="upload-cloud" />同步文件</button>
              </form>
            </div>
            <div id="douyin-sync-result" class="meta" style="margin-top:10px;">{{ state.douyinSyncResult }}</div>
            <div id="douyin-sync-summary">
              <div class="detail-metrics">
                <span>平台映射<strong>{{ mappings.length }}</strong></span>
                <span>授权指标<strong>{{ Number(metrics.count || 0) }}</strong></span>
                <span>未链接<strong>{{ Number(metrics.unlinked || 0) }}</strong></span>
              </div>
              <div class="meta">{{ runText }}</div>
              <template v-if="mappings.length">
                <div v-for="item in mappings.slice(0, 4)" :key="item.platform_item_id" class="sample-row">
                  <div><strong>{{ item.platform_item_id || "未绑定 item" }}</strong><div class="meta">{{ item.sync_status || "linked" }} / {{ item.last_metrics_at || item.last_synced_at || "待同步" }}</div></div>
                  <span class="status" :class="item.candidate_segment_id ? 'ok' : 'warn'">{{ item.candidate_segment_id ? "已链接" : "未链接" }}</span>
                </div>
              </template>
              <div v-else class="empty"><Icon name="radio-tower" /><strong>暂无平台映射</strong><span>先在候选详情绑定抖音 item_id，再运行 Mock 或文件同步。</span></div>
            </div>
          </div>
        </div>

        <div v-show="state.feedbackSection === 'overview'" class="feedback-block insights-block">
          <h3><Icon name="line-chart" />复盘摘要</h3>
          <div class="inner">
            <div id="insights-summary">
              <div v-if="!sampleCount" class="empty"><Icon name="line-chart" /><strong>暂无复盘信号</strong><span>刷新已发布研究样本或导入授权指标后，这里会汇总下一轮剪辑方向。</span></div>
              <template v-else>
                <div class="metric-total">{{ countLabel(sampleCount) }}</div>
                <div class="meta">{{ sampleCount >= 300 ? "正式历史样本，可用于账号趋势和原型发现" : "样本量偏少，仅作方向参考" }}</div>
                <div class="insight-grid">
                  <div v-for="item in signalCards" :key="item.key" class="insight-card">
                    <span>{{ item.label }}</span>
                    <strong>{{ item.name }}</strong>
                    <div class="meta">n={{ item.count }} / 互动热度 {{ item.reward }} / 转化 {{ item.conversion }}%</div>
                  </div>
                </div>
                <div class="quality-row" style="margin-top:8px;"><Icon name="lightbulb" /><span>{{ nextInsight }}</span></div>
              </template>
            </div>
          </div>
        </div>

        <div id="feedback-calibration" v-show="state.feedbackSection === 'calibration'" class="feedback-block learning-block">
          <h3><Icon name="file-clock" />V1 Beta-C 学习评估</h3>
          <div class="inner">
            <div class="sync-actions">
              <button id="memory-build-btn" type="button" :disabled="state.busyKey === 'memory-build'" @click="withBusy('memory-build', buildMemoryBank)">
                <span v-if="state.busyKey === 'memory-build'" class="spinner"></span>
                <Icon v-else name="graduation-cap" />重建记忆库
              </button>
              <button id="interest-clock-btn" type="button" :disabled="state.busyKey === 'interest-clock'" @click="withBusy('interest-clock', rebuildInterestClock)">
                <span v-if="state.busyKey === 'interest-clock'" class="spinner"></span>
                <Icon v-else name="file-clock" />刷新发布时间
              </button>
              <button id="historical-import-btn" type="button" :disabled="state.busyKey === 'historical-import'" @click="withBusy('historical-import', importHistoricalSamples)">
                <span v-if="state.busyKey === 'historical-import'" class="spinner"></span>
                <Icon v-else name="database" />刷新历史样本
              </button>
              <button id="prototype-bank-btn" type="button" :disabled="state.busyKey === 'prototype-bank'" @click="withBusy('prototype-bank', buildPrototypeBank)">
                <span v-if="state.busyKey === 'prototype-bank'" class="spinner"></span>
                <Icon v-else name="sparkles" />重建原型库
              </button>
              <button id="backtest-btn" class="primary" type="button" :disabled="state.busyKey === 'backtest'" @click="withBusy('backtest', runBacktest)">
                <span v-if="state.busyKey === 'backtest'" class="spinner"></span>
                <Icon v-else name="radar" />运行回测
              </button>
            </div>
            <div id="learning-result" class="meta" style="margin-top:10px;">{{ state.learningResult }}</div>
            <div class="detail-metrics" style="margin-top:8px;">
              <span>源文件行数<strong>{{ countLabel(learningCounts.sourceRaw) }}</strong></span>
              <span>源去重<strong>{{ countLabel(learningCounts.sourceUnique) }}</strong></span>
              <span>入库样本<strong>{{ countLabel(learningCounts.historical) }}</strong></span>
              <span>可训练样本<strong>{{ countLabel(learningCounts.trainingReady) }}</strong></span>
              <span>记忆候选<strong>{{ memoryTotal }}</strong></span>
              <span>最佳小时<strong>{{ topHour }}</strong></span>
              <span>原型<strong>{{ prototypeTotal }}</strong></span>
              <span>NDCG@K<strong>{{ ndcgText }}</strong></span>
            </div>
            <div class="learning-count-note">{{ learningCountCaption }}</div>
            <div class="calibration-toolbar">
              <div>
                <strong>语义校准队列</strong>
                <span>{{ calibrationQueueText }}</span>
              </div>
              <button type="button" :disabled="state.busyKey === 'calibration-refresh'" @click="withBusy('calibration-refresh', loadSemanticCalibrationQueue)">
                <span v-if="state.busyKey === 'calibration-refresh'" class="spinner"></span>
                <Icon v-else name="refresh-cw" />刷新队列
              </button>
              <button class="primary" type="button" :disabled="state.busyKey === 'calibration-rebuild'" @click="withBusy('calibration-rebuild', rebuildCalibrationEvidence)">
                <span v-if="state.busyKey === 'calibration-rebuild'" class="spinner"></span>
                <Icon v-else name="radar" />重建标签与回测
              </button>
            </div>
            <div v-if="calibrationBatchText" class="quality-row" style="margin-top:8px;">
              <span class="status neutral">批次</span>
              <span>{{ calibrationBatchText }}</span>
            </div>
            <div v-if="calibrationSamples.length" id="semantic-calibration-queue" class="calibration-list">
              <div v-for="sample in calibrationSamples.slice(0, 6)" :key="sampleId(sample)" class="calibration-row">
                <div class="calibration-row-head">
                  <div>
                    <strong>{{ clipText(sample.title || sample.song_title || sampleId(sample), 62) }}</strong>
                    <div class="meta">{{ accountDisplayName(sample.account_id, sample.account_display_name) }} / {{ sample.dataset_id || "default" }} / 优先级 {{ calibrationPriority(sample) }}</div>
                  </div>
                  <span class="status" :class="performanceClass(sample.performance_label)">{{ performanceLabel(sample.performance_label) }}</span>
                </div>
                <div class="meta calibration-reason">{{ calibrationReason(sample) }}</div>
                <div class="calibration-tags">
                  <span v-for="field in calibrationFields(sample)" :key="`${sampleId(sample)}-${field}`">{{ fieldLabel(field) }}</span>
                  <span v-if="sample.queue_reason">{{ queueReasonLabel(sample.queue_reason) }}</span>
                  <span v-if="Number(sample.risk_score || 0)">risk {{ Number(sample.risk_score || 0).toFixed(0) }}</span>
                  <span v-if="Number(sample.disagreement_score || 0)">gap {{ Number(sample.disagreement_score || 0).toFixed(0) }}</span>
                  <span v-if="sample.manual_verified" class="verified">manual</span>
                </div>
                <div v-if="state.calibrationDrafts[sampleId(sample)]" class="calibration-fields">
                  <label>
                    <span>类别</span>
                    <select v-model="state.calibrationDrafts[sampleId(sample)].content_category">
                      <option v-for="option in semanticOptions('content_category')" :key="option.value" :value="option.value">{{ option.label }}</option>
                    </select>
                  </label>
                  <label>
                    <span>Hook</span>
                    <select v-model="state.calibrationDrafts[sampleId(sample)].hook_type">
                      <option v-for="option in semanticOptions('hook_type')" :key="option.value" :value="option.value">{{ option.label }}</option>
                    </select>
                  </label>
                  <label>
                    <span>结构</span>
                    <select v-model="state.calibrationDrafts[sampleId(sample)].slice_structure">
                      <option v-for="option in semanticOptions('slice_structure')" :key="option.value" :value="option.value">{{ option.label }}</option>
                    </select>
                  </label>
                  <label><span>艺人</span><input v-model="state.calibrationDrafts[sampleId(sample)].artist_names" type="text" /></label>
                  <label><span>歌名</span><input v-model="state.calibrationDrafts[sampleId(sample)].song_title" type="text" /></label>
                  <label><span>标签</span><input v-model="state.calibrationDrafts[sampleId(sample)].tags" type="text" /></label>
                </div>
                <div class="calibration-actions">
                  <button type="button" :disabled="state.busyKey === `calibration-save-${sampleId(sample)}`" @click="withBusy(`calibration-save-${sampleId(sample)}`, () => saveCalibrationLabels(sampleId(sample)))">
                    <span v-if="state.busyKey === `calibration-save-${sampleId(sample)}`" class="spinner"></span>
                    <Icon v-else name="check" />保存人工标签
                  </button>
                </div>
              </div>
            </div>
            <div v-else class="quality-row" style="margin-top:8px;">
              <span class="status neutral">校准</span>
              <span>{{ calibrationEmptyText }}</span>
            </div>
            <div v-if="recentlySavedSamples.length" class="calibration-toolbar saved-calibration-toolbar">
              <div>
                <strong>最近已保存</strong>
                <span>误保存或需要二次校准时，可把样本重新放回待校准队列。</span>
              </div>
            </div>
            <div v-if="recentlySavedSamples.length" id="recently-saved-calibration" class="calibration-list">
              <div v-for="sample in recentlySavedSamples.slice(0, 4)" :key="`saved-${sampleId(sample)}`" class="calibration-row">
                <div class="calibration-row-head">
                  <div>
                    <strong>{{ clipText(sample.title || sample.song_title || sampleId(sample), 62) }}</strong>
                    <div class="meta">{{ accountDisplayName(sample.account_id, sample.account_display_name) }} / {{ sample.dataset_id || "default" }} / {{ sample.updated_at || sample.collected_at || "已保存" }}</div>
                  </div>
                  <span class="status ok">manual</span>
                </div>
                <div class="meta calibration-reason">{{ clipText(savedSampleSummary(sample), 116) }}</div>
                <div class="calibration-actions">
                  <button type="button" :disabled="state.busyKey === `calibration-reopen-${sampleId(sample)}`" @click="withBusy(`calibration-reopen-${sampleId(sample)}`, () => reopenCalibrationSample(sampleId(sample)))">
                    <span v-if="state.busyKey === `calibration-reopen-${sampleId(sample)}`" class="spinner"></span>
                    <Icon v-else name="refresh-ccw" />重新打开校准
                  </button>
                </div>
              </div>
            </div>
            <div v-if="topPrototypes.length" id="prototype-bank-summary" class="insight-grid">
              <div v-for="item in topPrototypes.slice(0, 3)" :key="item.prototype_key || item.prototype_name" class="insight-card">
                <span>{{ prototypeLevel(item) }} / n={{ Number(item.sample_count || 0) }}</span>
                <strong>{{ item.prototype_name || "未知原型" }}</strong>
                <div class="meta" :title="prototypeBenchmarkTip(item)">{{ prototypeBenchmarkLine(item) }} / {{ prototypeStability(item) }}</div>
                <div class="meta">{{ prototypeKeywords(item) }}</div>
              </div>
            </div>
            <div v-if="prototypeText" class="quality-row" style="margin-top:8px;">
              <Icon name="sparkles" /><span>{{ prototypeText }}</span>
            </div>
            <div id="interest-clock-summary" class="insight-grid">
              <div v-for="item in recommendations.slice(0, 4)" :key="`${item.content_type}-${item.duration_bucket}-${item.publish_hour}`" class="insight-card">
                <span>{{ item.content_type || "all" }} / {{ item.duration_bucket || "any" }}</span>
                <strong>{{ Number(item.publish_hour ?? -1) >= 0 ? `${item.publish_hour}:00` : "待学习" }}</strong>
                <div class="meta">score {{ Number(item.suggested_score || 0).toFixed(1) }} / conf {{ Number(item.confidence || 0).toFixed(2) }} / n={{ Number(item.sample_count || 0) }}</div>
              </div>
            </div>
            <div v-if="latestBacktest" id="backtest-summary" class="quality-row" style="margin-top:8px;">
              <span class="status" :class="backtestClass">{{ latestBacktest.status || "ready" }}</span>
              <span>{{ backtestText }}</span>
            </div>
            <div v-if="v21ReportText" class="quality-row" style="margin-top:8px;">
              <span class="status neutral">v2.2</span>
              <span>{{ v21ReportText }}</span>
            </div>
            <div v-if="semanticGapText" class="quality-row" style="margin-top:8px;">
              <span class="status" :class="semanticGapClass">{{ semanticGapStatus }}</span>
              <span>{{ semanticGapText }}</span>
            </div>
            <div v-if="backtestStrategyRows.length" class="insight-grid" style="margin-top:8px;">
              <div v-for="item in backtestStrategyRows" :key="item.strategy" class="insight-card">
                <span>{{ item.label }} / n={{ item.sampleCount }}</span>
                <strong>{{ item.lift }}x</strong>
                <div class="meta">高互动 {{ item.highHit }} / 避低 {{ item.lowAvoid }}</div>
              </div>
            </div>
            <div v-if="diagnosticRows.length" class="calibration-list" style="margin-top:8px;">
              <div v-for="item in diagnosticRows" :key="`${item.group}-${item.sampleId}`" class="calibration-row">
                <div class="calibration-row-head">
                  <div>
                    <strong>{{ clipText(item.title || item.sampleId, 54) }}</strong>
                    <div class="meta">{{ item.groupLabel }} / {{ item.accountId || "all" }} / gap {{ item.gap }}</div>
                  </div>
                  <span class="status" :class="performanceClass(item.performanceLabel)">{{ performanceLabel(item.performanceLabel) }}</span>
                </div>
                <div class="meta calibration-reason">{{ item.reason }}</div>
              </div>
            </div>
            <div v-if="promotionGateText" class="quality-row" style="margin-top:8px;">
              <span class="status" :class="promotionGateClass">{{ promotionGateStatus }}</span>
              <span>{{ promotionGateText }}</span>
            </div>
            <div v-if="!recommendations.length && !latestBacktest && !topPrototypes.length" class="empty"><Icon name="file-clock" /><strong>暂无学习评估</strong><span>先刷新已发布研究样本，再生成记忆库、时间建议、原型库和离线回测。</span></div>
          </div>
        </div>

        <div id="feedback-samples" v-show="state.feedbackSection === 'samples'" class="feedback-block training-block">
          <h3><Icon name="graduation-cap" />训练样本</h3>
          <div class="inner">
            <div class="metric-total" id="training-count">{{ countLabel(learningCounts.trainingReady) }}</div>
            <div class="meta training-count-meta">可训练样本，当前列表展示 {{ countLabel(state.trainingSamples.length) }} 条</div>
            <div class="detail-metrics training-counts">
              <span>源文件行数<strong>{{ countLabel(learningCounts.sourceRaw) }}</strong></span>
              <span>源去重<strong>{{ countLabel(learningCounts.sourceUnique) }}</strong></span>
              <span>入库样本<strong>{{ countLabel(learningCounts.historical) }}</strong></span>
            </div>
            <div id="training-list">
              <template v-if="state.trainingSamples.length">
                <div v-for="sample in state.trainingSamples.slice(0, 5)" :key="`${sample.music_slice_type}-${sample.label_window}`" class="sample-row">
                  <div>
                    <strong>{{ sample.music_slice_type || "unknown" }}</strong>
                    <div class="meta">{{ sample.label_window || "" }} / {{ sample.train_split || "" }}</div>
                    <div class="bar"><span :style="{ width: `${Math.min(100, sampleReward(sample))}%` }"></span></div>
                  </div>
                  <strong>{{ sampleReward(sample).toFixed(1) }}</strong>
                </div>
              </template>
              <div v-else class="empty"><Icon name="graduation-cap" /><strong>暂无训练样本</strong><span>导入授权表现 CSV 后，这里会展示可用于排序校准的样本。</span></div>
            </div>
          </div>
        </div>

        <div v-show="state.feedbackSection === 'samples'" class="feedback-block baseline-block">
          <h3><Icon name="gauge" />账号基线</h3>
          <div class="inner">
            <div id="baseline-list">
              <template v-if="historySignalRows.length">
                <div v-for="row in historySignalRows.slice(0, 8)" :key="`${row.dimension}-${row.name}`" class="baseline-row">
                  <div><strong>{{ row.name || "unknown" }}</strong><div class="meta">{{ signalDimensionLabel(row.dimension) }} / n={{ row.sample_count }}</div></div>
                  <div style="text-align:right"><div>互动热度 P75 {{ Number(row.p75_reward || 0).toFixed(1) }}</div><div class="meta">均值 {{ Number(row.avg_reward || 0).toFixed(1) }}</div></div>
                </div>
              </template>
              <template v-else-if="baselineRows.length">
                <div v-for="row in baselineRows" :key="`${row.content_type}-${row.duration_bucket}-${row.publish_hour}`" class="baseline-row">
                  <div><strong>{{ row.content_type || "unknown" }}</strong><div class="meta">{{ row.duration_bucket }} / {{ Number(row.publish_hour ?? -1) >= 0 ? `${row.publish_hour}:00` : "any" }} / n={{ row.sample_count }}</div></div>
                  <div style="text-align:right"><div>互动热度 P75 {{ Number(row.p75_value || 0).toFixed(1) }}</div><div class="meta">P90 {{ Number(row.p90_value || 0).toFixed(1) }}</div></div>
                </div>
              </template>
              <div v-else class="empty"><Icon name="gauge" /><strong>暂无账号基线</strong><span>导入足够研究样本或授权指标后，系统会按类型、时长和发布时间计算账号基线。</span></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import Icon from "./Icon.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { AccountQuality, DouyinHistorySignal, LearningDataset, PrototypeBankItem, SemanticCalibrationSample, TrainingSample } from "../types";
import { clipText } from "../utils";

type CountSource = Record<string, unknown> | null | undefined;

interface LearningCountSummary {
  sourceRaw: number;
  sourceUnique: number;
  historical: number;
  trainingReady: number;
}

interface FeedbackAccountOption {
  id: string;
  label: string;
  tier?: string;
}

const {
  state,
  loadFeedback,
  importMetrics,
  syncDouyinMock,
  syncDouyinFile,
  startDouyinLogin,
  rebuildFeedback,
  buildMemoryBank,
  rebuildInterestClock,
  runBacktest,
  loadSemanticCalibrationQueue,
  saveCalibrationLabels,
  reopenCalibrationSample,
  rebuildCalibrationEvidence,
  importHistoricalSamples,
  buildPrototypeBank,
  withBusy,
  toast
} = useDashboardContext();

const metricsFile = ref<HTMLInputElement | null>(null);
const douyinFile = ref<HTMLInputElement | null>(null);
const feedbackSections = [
  { key: "overview", label: "概览", icon: "layout-dashboard" },
  { key: "samples", label: "研究样本", icon: "database" },
  { key: "calibration", label: "校准与回测", icon: "radar" },
  { key: "platform", label: "平台账号", icon: "radio-tower" },
  { key: "runtime", label: "运行环境", icon: "cpu" }
] as const;
const moduleAlerts = computed(() => [
  { key: "feedback", label: "研究样本", error: state.moduleStatus.feedback.error },
  { key: "history", label: "历史先验", error: state.moduleStatus.history.error },
  { key: "douyin", label: "平台账号", error: state.moduleStatus.douyin.error },
  { key: "runtime", label: "运行环境", error: state.moduleStatus.runtime.error }
].filter(item => item.error));

const runtime = computed(() => state.runtime || {});
const asr = computed(() => runtime.value.asr || {});
const whisperCpp = computed(() => asr.value.whisper_cpp || {});
const profiles = computed(() => Array.isArray(asr.value.profile_plan?.profiles) ? asr.value.profile_plan?.profiles?.slice(0, 3) || [] : []);
const backendLabel = computed(() => asr.value.backend === "whisper_cpp" || whisperCpp.value.ready
  ? "whisper.cpp"
  : (asr.value.faster_whisper_installed ? `faster-whisper ${asr.value.default_model || "base"}` : "占位降级"));
const accelLabel = computed(() => whisperCpp.value.ready ? "Metal/Core ML 可配置" : (whisperCpp.value.binary ? "缺模型" : "未安装 whisper.cpp"));

const oauth = computed(() => state.douyinOAuth || {});
const oauthAccount = computed(() => oauth.value.account || {});
const oauthToken = computed(() => oauth.value.token || {});
const oauthConfig = computed(() => oauth.value.config || {});
const authStatus = computed(() => oauthAccount.value.auth_status || (oauthToken.value.stored ? "connected" : "not_connected"));
const authClass = computed(() => authStatus.value === "connected" ? "ok" : (oauthConfig.value.ready_for_qr_login ? "neutral" : "warn"));
const authStatusLabel = computed(() => {
  const labels: Record<string, string> = {
    connected: "已连接",
    code_received: "已收授权码",
    mock_ready: "Mock 就绪",
    waiting_scan: "等待扫码",
    not_connected: "未连接"
  };
  return labels[authStatus.value] || authStatus.value || "未连接";
});
const authText = computed(() => oauthConfig.value.ready_for_qr_login
  ? `授权 ${authStatus.value}${oauthToken.value.access_token_expires_at ? ` / token 至 ${oauthToken.value.access_token_expires_at.slice(0, 10)}` : ""}`
  : `OAuth 未配置：${Array.isArray(oauthConfig.value.missing) ? oauthConfig.value.missing.join(", ") : "缺少环境变量"}`);
const douyinAccountMeta = computed(() => {
  if (!state.feedbackAccount.trim()) return "全量聚合视图：用于查看 15 个关注账号的历史样本质量";
  const platformId = oauthAccount.value.platform_account_id;
  if (platformId) return `平台 ID ${platformId}`;
  return oauthToken.value.open_id ? `OpenID ${oauthToken.value.open_id}` : `内部 ID ${state.feedbackAccount}，用于平台映射与数据回流`;
});

const summary = computed(() => state.douyinSummary || {});
const mappings = computed(() => Array.isArray(summary.value.mappings) ? summary.value.mappings || [] : []);
const runs = computed(() => Array.isArray(summary.value.runs) ? summary.value.runs || [] : []);
const metrics = computed(() => summary.value.metrics || {});
const runText = computed(() => {
  const latestRun = runs.value[0] || {};
  return latestRun.id ? `最近 ${latestRun.source || ""} / 导入 ${Number(latestRun.imported_metrics || 0)} / 链接 ${Number(latestRun.linked_rows || 0)}` : "暂无同步批次";
});

const insights = computed(() => state.accountInsights || {});
const historyBaselines = computed(() => state.historyBaselines || {});
const historicalSummary = computed(() => state.historicalSummary || {});
const sampleCount = computed(() => Number(insights.value.sample_count || historyBaselines.value.sample_count || historicalSummary.value.sample_count || 0));
const signalCards = computed(() => {
  const top = insights.value.top_signals || {};
  return [
    ["slice_type", "切片类型"],
    ["structure", "内容结构"],
    ["duration_bucket", "时长桶"],
    ["publish_time", "发布时间"]
  ].map(([key, label]) => {
    const item = top[key] || {};
    return {
      key,
      label,
      name: item.name || "暂无",
      count: Number(item.count || 0),
      reward: Number(item.reward_proxy || 0).toFixed(1),
      conversion: (Number(item.play_conversion_rate || 0) * 100).toFixed(1)
    };
  });
});
const nextInsight = computed(() => {
  const top = insights.value.top_signals || {};
  const bestType = top.slice_type?.name || top.structure?.name || "高表现结构";
  return `下轮优先扩充“${bestType}”相近候选，并继续用授权窗口指标校准。`;
});
const baselineRows = computed(() => state.baselines.filter(row => row.metric_name === "reward_proxy").slice(0, 6));
const accountQualityRows = computed<AccountQuality[]>(() => Array.isArray(historicalSummary.value.account_quality) ? historicalSummary.value.account_quality || [] : []);
const historySignalRows = computed<DouyinHistorySignal[]>(() => Array.isArray(historyBaselines.value.top_signals) ? historyBaselines.value.top_signals || [] : []);
const memory = computed(() => state.memoryBuild || {});
const prototypeBank = computed(() => state.prototypeBank || {});
const topPrototypes = computed(() => Array.isArray(prototypeBank.value.prototypes) ? prototypeBank.value.prototypes || [] : []);
const feedbackDatasetOptions = computed<LearningDataset[]>(() => {
  const account = state.feedbackAccount.trim();
  if (!account) return state.learningDatasets;
  const options = new Map<string, LearningDataset>();
  const summaryDatasets = Array.isArray(historicalSummary.value.datasets) ? historicalSummary.value.datasets as LearningDataset[] : [];
  for (const dataset of summaryDatasets) {
    if (dataset.id) options.set(dataset.id, dataset);
  }
  for (const dataset of state.learningDatasets) {
    if (datasetBelongsToAccount(dataset, account) && dataset.id) options.set(dataset.id, dataset);
  }
  return options.size ? Array.from(options.values()) : state.learningDatasets;
});
const selectedDataset = computed(() => feedbackDatasetOptions.value.find(item => item.id === state.feedbackDataset)
  || state.learningDatasets.find(item => item.id === state.feedbackDataset)
  || null);
const historicalImport = computed(() => state.historicalImport || {});
const learningCounts = computed(() => state.feedbackAccount.trim()
  ? datasetCounts(historicalSummary.value)
  : datasetCounts(selectedDataset.value));
const learningCountCaption = computed(() => {
  if (state.feedbackAccount.trim()) {
    return `${feedbackAccountLabel.value}：当前页按账号展示源文件行数、源去重、正式入库和可训练样本。`;
  }
  const datasetName = selectedDataset.value
    ? datasetOptionTitle(selectedDataset.value)
    : state.feedbackDataset || "当前数据集";
  return `${datasetName}：当前页统一展示源文件行数、源去重、正式入库和可训练样本。`;
});
const interestClock = computed(() => state.interestClock || {});
const recommendations = computed(() => {
  if (Array.isArray(interestClock.value.recommendations)) return interestClock.value.recommendations;
  if (Array.isArray(interestClock.value.top_windows)) return interestClock.value.top_windows;
  if (Array.isArray(interestClock.value.suggestions)) return interestClock.value.suggestions;
  return [];
});
const latestBacktest = computed(() => state.backtestReports[0] || null);
const latestMetrics = computed(() => latestBacktest.value?.metrics || {});
const calibrationQueue = computed(() => state.semanticCalibrationQueue || {});
const calibrationSamples = computed<SemanticCalibrationSample[]>(() => {
  if (Array.isArray(calibrationQueue.value.samples)) return calibrationQueue.value.samples || [];
  if (Array.isArray(calibrationQueue.value.queue)) return calibrationQueue.value.queue || [];
  return [];
});
const recentlySavedSamples = computed<SemanticCalibrationSample[]>(() => Array.isArray(calibrationQueue.value.recently_saved_samples)
  ? calibrationQueue.value.recently_saved_samples || []
  : []);
const memoryTotal = computed(() => Number(memory.value.total_candidates || 0));
const prototypeTotal = computed(() => Number(prototypeBank.value.prototype_count ?? prototypeBank.value.count ?? topPrototypes.value.length));
const topHour = computed(() => {
  const hour = recommendations.value[0]?.publish_hour;
  return Number(hour) >= 0 ? `${hour}:00` : "-";
});
const ndcgText = computed(() => latestBacktest.value ? Number(latestMetrics.value.ndcg_at_k || 0).toFixed(2) : "-");
const backtestClass = computed(() => latestBacktest.value?.status === "ready" ? "ok" : (latestBacktest.value?.status === "low_confidence" ? "warn" : "neutral"));
const backtestText = computed(() => `验证样本 ${countLabel(Number(latestMetrics.value.sample_count || 0))} / NDCG ${Number(latestMetrics.value.ndcg_at_k || 0).toFixed(2)} / 相对随机 ${Number(latestMetrics.value.topk_lift_vs_random || 0).toFixed(2)}x / ${sampleSourceLabel(String(latestMetrics.value.sample_source || "training_samples"))}`);
const calibrationQueueText = computed(() => {
  const total = Number(calibrationQueue.value.total_candidates ?? calibrationQueue.value.count ?? calibrationSamples.value.length);
  const visible = calibrationSamples.value.length;
  const policy = String(calibrationQueue.value.queue_policy || "优先校准高影响、低可信和缺关键语义字段样本");
  return `显示 ${visible}/${total} 条，${policy}`;
});
const calibrationEmptyText = computed(() => {
  if (!state.semanticCalibrationQueue) return "队列尚未加载，点击“刷新队列”获取待校准样本。";
  const summary = calibrationQueue.value.batch_summary;
  const item = summary && typeof summary === "object" ? summary as Record<string, unknown> : {};
  const pending = Number(item.pending_count ?? calibrationQueue.value.total_candidates ?? calibrationQueue.value.count ?? 0);
  const saved = Number(item.saved_count || 0);
  if (pending <= 0 && saved > 0) {
    return `当前筛选条件下待校准样本已完成，已保存人工标签 ${countLabel(saved)} 条。`;
  }
  return "暂无待校准样本，或当前账号/数据集筛选下样本已人工确认。";
});
const calibrationBatchText = computed(() => {
  const summary = calibrationQueue.value.batch_summary;
  if (!summary || typeof summary !== "object") return "";
  const item = summary as Record<string, unknown>;
  const missing = Array.isArray(item.top_missing_fields)
    ? item.top_missing_fields.slice(0, 3).map(entry => {
      const row = entry as Record<string, unknown>;
      return `${fieldLabel(String(row.field || ""))} ${Number(row.count || 0)}`;
    }).join(" / ")
    : "";
  const accounts = Array.isArray(item.impact_accounts)
    ? item.impact_accounts.slice(0, 2).map(entry => {
      const row = entry as Record<string, unknown>;
      return `${accountDisplayName(String(row.account_id || ""))} ${Number(row.count || 0)}`;
    }).join(" / ")
    : "";
  return `待校准 ${Number(item.pending_count || 0)} / 已保存 ${Number(item.saved_count || 0)}${missing ? ` / 缺失 ${missing}` : ""}${accounts ? ` / 影响账号 ${accounts}` : ""}`;
});
const v21ReportText = computed(() => {
  const weightConfig = latestMetrics.value.weight_config;
  const baselineGap = latestMetrics.value.baseline_gap;
  const calibration = latestMetrics.value.calibration_summary;
  if (!weightConfig && !baselineGap && !calibration) return "";
  const weight = weightConfig && typeof weightConfig === "object" ? weightConfig as Record<string, unknown> : {};
  const gap = baselineGap && typeof baselineGap === "object" ? baselineGap as Record<string, unknown> : {};
  const summary = calibration && typeof calibration === "object" ? calibration as Record<string, unknown> : {};
  const decision = String(summary.production_status || summary.decision || "");
  const semanticGap = Number(gap.lift_vs_semantic_baseline ?? gap.topk_lift_gap_vs_semantic_baseline ?? gap.lift_gap_vs_semantic_baseline ?? 0);
  return `${weight.name || "weight_config"} / 较语义基线 ${semanticGap >= 0 ? "+" : ""}${semanticGap.toFixed(2)} / ${decision || "research_only"}`;
});
const semanticGap = computed<Record<string, unknown>>(() => {
  const value = latestMetrics.value.semantic_gap_analysis;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const semanticGapStatus = computed(() => Boolean(semanticGap.value.passed) ? "领先语义" : "待校准");
const semanticGapClass = computed(() => Boolean(semanticGap.value.passed) ? "ok" : "warn");
const semanticGapText = computed(() => {
  if (!latestBacktest.value || !Object.keys(semanticGap.value).length) return "";
  return `v2.2 较语义基线 lift ${Number(semanticGap.value.lift_gap || 0) >= 0 ? "+" : ""}${Number(semanticGap.value.lift_gap || 0).toFixed(2)} / 目标 +${Number(semanticGap.value.required_lift_gap || 0).toFixed(2)}`;
});
const strategyComparison = computed<Record<string, Record<string, unknown>>>(() => {
  const value = latestMetrics.value.strategy_comparison;
  return value && typeof value === "object" ? value as Record<string, Record<string, unknown>> : {};
});
const backtestStrategyRows = computed(() => ["research_ranker_v2_2", "semantic_baseline_v2", "research_ranker_v2_1", "research_ranker_v2", "current_rules"].map((strategy) => {
  const item = strategyComparison.value[strategy] || {};
  return {
    strategy,
    label: strategyLabel(strategy),
    sampleCount: Number(item.sample_count || 0),
    lift: Number(item.topk_lift_vs_random || 0).toFixed(2),
    highHit: Number(item.high_interaction_hit_rate || 0).toFixed(2),
    lowAvoid: Number(item.low_interaction_avoidance_rate || 0).toFixed(2)
  };
}).filter((item) => item.sampleCount > 0));
const diagnosticRows = computed(() => {
  const diagnostics = latestMetrics.value.diagnostic_samples;
  if (!diagnostics || typeof diagnostics !== "object") return [];
  const groups: Array<[string, string, string]> = [
    ["missed_high_interaction", "高互动漏召", "高互动历史样本未进入当前 TopK，优先校准 hook/结构。"],
    ["low_interaction_false_positive", "低互动误推", "TopK 命中低互动样本，优先复核风险和上下文差异。"],
    ["semantic_disagreements", "语义分歧", "v2.2 与语义基线排序分歧较大，适合进入下一批校准。"]
  ];
  const rows: Array<Record<string, string>> = [];
  for (const [key, label, reason] of groups) {
    const items = (diagnostics as Record<string, unknown>)[key];
    if (!Array.isArray(items)) continue;
    for (const item of items.slice(0, 2)) {
      const row = item as Record<string, unknown>;
      rows.push({
        group: key,
        groupLabel: label,
        reason,
        sampleId: String(row.sample_id || row.platform_item_id || row.title || key),
        title: String(row.title || ""),
        accountId: String(row.account_id || ""),
        performanceLabel: String(row.performance_label || ""),
        gap: Number(row.disagreement_score || 0).toFixed(2)
      });
    }
  }
  return rows.slice(0, 6);
});
const promotionGate = computed<Record<string, unknown>>(() => {
  const value = latestMetrics.value.promotion_gate;
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
});
const promotionGateStatus = computed(() => Boolean(promotionGate.value.passed) ? "可提升" : "研究证据");
const promotionGateClass = computed(() => Boolean(promotionGate.value.passed) ? "ok" : "warn");
const promotionGateText = computed(() => {
  if (!latestBacktest.value) return "";
  return `门控 lift ${Number(promotionGate.value.topk_lift_vs_random || 0).toFixed(2)} / 账号 ${Number(promotionGate.value.improved_ready_account_count || 0)}/${Number(promotionGate.value.required_improved_ready_account_count || 3)} / ${promotionGate.value.decision || "keep_as_research_evidence"}`;
});
const feedbackAccountLabel = computed(() => accountDisplayName(state.feedbackAccount.trim()));
const feedbackAccountOptions = computed<FeedbackAccountOption[]>(() => {
  const options = new Map<string, FeedbackAccountOption>();
  for (const item of accountQualityRows.value) {
    const id = item.account_id || "";
    if (!id) continue;
    options.set(id, {
      id,
      label: accountDisplayName(id, item.account_display_name),
      tier: item.account_tier
    });
  }
  for (const dataset of state.learningDatasets) {
    const id = dataset.account_id || dataset.program_key || "";
    if (!id || id === "all" || options.has(id)) continue;
    options.set(id, {
      id,
      label: accountDisplayName(id, dataset.account_display_name || dataset.display_name || dataset.name),
      tier: dataset.account_tier
    });
  }
  return Array.from(options.values()).sort((a, b) => a.label.localeCompare(b.label, "zh-Hans-CN"));
});
const interactionCoverageText = computed(() => {
  const rates = [
    historicalSummary.value.likes_coverage_rate,
    historicalSummary.value.favorites_coverage_rate,
    historicalSummary.value.comments_coverage_rate,
    historicalSummary.value.shares_coverage_rate
  ].map(value => Number(value || 0)).filter(value => Number.isFinite(value));
  if (!rates.length) return "-";
  return percent(Math.min(...rates));
});
const lineageStatusText = computed(() => {
  const duplicates = Number(historicalSummary.value.duplicate_item_group_count || 0);
  const playMissing = Number(historicalSummary.value.play_missing_rate || 0);
  const coverage = interactionCoverageText.value;
  const duplicateText = duplicates ? `仍有 ${duplicates} 组重复视频需处理` : "正式样本已按作品去重";
  const playText = playMissing >= 0.99 ? "播放量暂缺，当前只表达高互动/高可见热度" : `播放量缺失 ${percent(playMissing)}`;
  return `${duplicateText}；四项互动最低覆盖 ${coverage}；${playText}。`;
});
const prototypeText = computed(() => {
  const top = topPrototypes.value[0];
  if (!top) return "";
  const params = top.parameters || {};
  const range = Array.isArray(params.duration_seconds_range) && params.duration_seconds_range.length >= 2
    ? `${params.duration_seconds_range[0]}-${params.duration_seconds_range[1]}秒`
    : "建议时长待学习";
  return `${top.prototype_name || "高互动原型"} / ${prototypeLevel(top)} / ${prototypeBenchmarkLine(top)} / ${range} / ${params.cover_focus || "封面看点待学习"}`;
});

function sampleReward(sample: TrainingSample): number {
  return Number(sample.normalized_reward || sample.reward_proxy || 0);
}

function compactNumber(value?: number): string {
  const num = Number(value || 0);
  if (num >= 100000000) return `${(num / 100000000).toFixed(1)}亿`;
  if (num >= 10000) return `${(num / 10000).toFixed(1)}万`;
  return `${Math.round(num)}`;
}

function countLabel(value?: number): string {
  const num = Number(value || 0);
  return Number.isFinite(num) ? Math.round(num).toLocaleString("zh-CN") : "0";
}

function percent(value?: number): string {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function accountCoverageText(item: AccountQuality): string {
  const rates = [
    item.likes_coverage_rate,
    item.favorites_coverage_rate,
    item.comments_coverage_rate,
    item.shares_coverage_rate
  ].map(value => Number(value || 0)).filter(value => Number.isFinite(value));
  return rates.length ? percent(Math.min(...rates)) : "-";
}

function accountDisplayName(accountId?: string, displayName?: string): string {
  const id = (accountId || "").trim();
  if (!id) return "全部账号";
  const explicit = (displayName || "").trim();
  if (explicit && explicit !== id) return explicit;
  const fromQuality = accountQualityRows.value.find(item => item.account_id === id)?.account_display_name;
  if (fromQuality) return fromQuality;
  const fromDataset = state.learningDatasets.find(item => item.account_id === id || item.program_key === id)?.account_display_name;
  return fromDataset || id;
}

function sampleSourceLabel(value: string): string {
  if (value === "historical_capture_samples") return "正式历史样本";
  if (value === "training_samples") return "授权指标训练样本";
  return value || "未知来源";
}

function strategyLabel(value: string): string {
  const labels: Record<string, string> = {
    research_ranker_v2_2: "历史证据 v2.2",
    research_ranker_v2_1: "历史证据 v2.1",
    research_ranker_v2: "历史证据 v2",
    current_rules: "当前规则",
    semantic_baseline_v2: "语义基线"
  };
  return labels[value] || value;
}

function sampleId(sample: SemanticCalibrationSample): string {
  return String(sample.sample_id || sample.id || "");
}

function calibrationPriority(sample: SemanticCalibrationSample): string {
  const value = Number(sample.priority_score ?? sample.priority ?? 0);
  return Number.isFinite(value) ? value.toFixed(2) : "0.00";
}

function calibrationFields(sample: SemanticCalibrationSample): string[] {
  const fields = Array.isArray(sample.recommended_fields) && sample.recommended_fields.length
    ? sample.recommended_fields
    : (Array.isArray(sample.suggested_fields) && sample.suggested_fields.length
      ? sample.suggested_fields
      : (Array.isArray(sample.needs) && sample.needs.length ? sample.needs : sample.missing_fields || []));
  return fields.map(item => String(item || "")).filter(Boolean).slice(0, 5);
}

function calibrationReason(sample: SemanticCalibrationSample): string {
  const reason = sample.queue_reason ? queueReasonLabel(sample.queue_reason) : "";
  const impact = String(sample.impact_reason || sample.label_reason || "等待人工校准语义字段");
  const baseline = Number(sample.baseline_strategy_score || 0);
  const ranker = Number(sample.ranker_strategy_score || 0);
  const gap = baseline || ranker ? ` / baseline ${baseline.toFixed(1)} -> ranker ${ranker.toFixed(1)}` : "";
  return `${reason ? `${reason} / ` : ""}${impact}${gap}`;
}

function savedSampleSummary(sample: SemanticCalibrationSample): string {
  const fields = [
    sample.content_category ? `类别 ${sample.content_category}` : "",
    sample.hook_type ? `Hook ${sample.hook_type}` : "",
    sample.slice_structure ? `结构 ${sample.slice_structure}` : "",
    sample.artist_names ? `艺人 ${sample.artist_names}` : "",
    sample.song_title ? `歌名 ${sample.song_title}` : ""
  ].filter(Boolean);
  return fields.length ? fields.join(" / ") : "已保存人工标签，可重新打开后进入待校准队列。";
}

function semanticOptions(field: string): Array<{ value: string; label: string }> {
  const catalog = calibrationQueue.value.semantic_label_catalog;
  const options = catalog && typeof catalog === "object" ? (catalog as Record<string, unknown>)[field] : null;
  if (Array.isArray(options)) {
    return options.map(item => {
      const row = item as Record<string, unknown>;
      const value = String(row.value || "");
      return {
        value,
        label: `${row.label || value}${value ? ` / ${value}` : ""}`
      };
    }).filter(item => item.value);
  }
  return [{ value: "unknown", label: "未知 / unknown" }];
}

function queueReasonLabel(value?: unknown): string {
  const key = String(value || "");
  const labels: Record<string, string> = {
    high_interaction_weak_label: "高互动弱标签",
    high_impact_weak_label: "高影响弱标签",
    low_interaction_risk: "低互动风险",
    semantic_ranker_disagreement: "排序分歧",
    high_interaction_missed_by_ranker: "高互动漏召",
    low_interaction_false_positive: "低互动误推"
  };
  return labels[key] || key || "校准";
}

function fieldLabel(value: string): string {
  const labels: Record<string, string> = {
    content_category: "类别",
    hook_type: "Hook",
    slice_structure: "结构",
    artist_names: "艺人",
    song_title: "歌名",
    tags: "标签",
    classification_confidence: "可信度"
  };
  return labels[value] || value;
}

function performanceLabel(value?: unknown): string {
  if (value === "high") return "高互动";
  if (value === "low") return "低互动";
  if (value === "mid") return "中位";
  return "待标注";
}

function performanceClass(value?: unknown): string {
  if (value === "high") return "ok";
  if (value === "low") return "warn";
  return "neutral";
}

function confidenceLabel(value?: string): string {
  if (value === "ready") return "可用于账号趋势";
  if (value === "low_confidence") return "低置信趋势";
  if (value === "insufficient_history") return "样本不足";
  return "待评估";
}

function signalDimensionLabel(value?: string): string {
  const labels: Record<string, string> = {
    account: "账号",
    content_category: "内容类别",
    hook_type: "Hook",
    slice_structure: "切片结构",
    program_name: "节目",
    duration_bucket: "时长段",
    publish_hour: "发布时间",
    artist: "艺人",
    tag: "标签"
  };
  return labels[value || ""] || value || "维度";
}

function prototypeKeywords(item: PrototypeBankItem): string {
  const words = Array.isArray(item.keywords) ? item.keywords.slice(0, 4).join(" / ") : "";
  const score = Number(item.avg_score || 0).toFixed(1);
  return words ? `${words} / score ${score}` : `score ${score}`;
}

function datasetOptionLabel(dataset: LearningDataset): string {
  const counts = datasetCounts(dataset);
  const hasCounts = counts.sourceRaw || counts.sourceUnique || counts.historical || counts.trainingReady;
  const suffix = hasCounts
    ? ` · 源${countLabel(counts.sourceRaw)} / 去重${countLabel(counts.sourceUnique)} / 入库${countLabel(counts.historical)} / 可训${countLabel(counts.trainingReady)}`
    : "";
  const label = dataset.display_name || dataset.name || accountDisplayName(dataset.account_id || dataset.program_key, dataset.account_display_name) || dataset.id;
  return `${label}${suffix}`;
}

function datasetOptionTitle(dataset: LearningDataset): string {
  return dataset.display_name
    || dataset.name
    || accountDisplayName(dataset.account_id || dataset.program_key, dataset.account_display_name)
    || dataset.id;
}

function datasetBelongsToAccount(dataset: LearningDataset | null | undefined, accountId: string): boolean {
  if (!dataset || !accountId) return false;
  return dataset.account_id === accountId
    || dataset.program_key === accountId
    || dataset.id === accountId
    || Boolean(dataset.id?.startsWith(`${accountId}_`));
}

function alignDatasetToFeedbackAccount(): void {
  const account = state.feedbackAccount.trim();
  if (!account) {
    state.feedbackDataset = "all";
    return;
  }
  const current = state.learningDatasets.find(item => item.id === state.feedbackDataset);
  if (current && state.feedbackDataset !== "all" && datasetBelongsToAccount(current, account)) return;
  const match = state.learningDatasets.find(item => datasetBelongsToAccount(item, account));
  if (match?.id) state.feedbackDataset = match.id;
}

const SOURCE_RAW_KEYS = [
  "source_raw_count",
  "source_raw_rows",
  "source_rows",
  "file_rows",
  "raw_rows",
  "raw_count",
  "total_rows"
];
const SOURCE_UNIQUE_KEYS = [
  "source_unique_count",
  "source_unique_rows",
  "unique_rows",
  "deduped_count",
  "deduped_rows",
  "unique_count",
  "valid_rows"
];
const HISTORICAL_KEYS = [
  "stored_sample_count",
  "formal_sample_count",
  "deduped_sample_count",
  "trainable_sample_count",
  "historical_count",
  "historical_sample_count",
  "stored_count",
  "stored_samples",
  "imported_samples",
  "sample_count",
  "count"
];
const TRAINING_READY_KEYS = [
  "trainable_sample_count",
  "formal_sample_count",
  "training_ready_count",
  "training_ready_samples",
  "trainable_count",
  "trainable_samples",
  "training_sample_count",
  "training_samples",
  "eligible_count",
  "usable_count"
];

function asCountSource(value: unknown): CountSource {
  return value && typeof value === "object" ? value as Record<string, unknown> : null;
}

function countFrom(source: CountSource, keys: string[]): number | null {
  if (!source) return null;
  for (const key of keys) {
    const value = source[key];
    if (value === undefined || value === null || value === "") continue;
    const num = Number(value);
    if (Number.isFinite(num)) return Math.max(0, Math.round(num));
  }
  return null;
}

function firstCount(sources: CountSource[], keys: string[]): number | null {
  for (const source of sources) {
    const count = countFrom(source, keys);
    if (count !== null) return count;
  }
  return null;
}

function datasetIdFrom(source: CountSource): string {
  if (!source) return "";
  const id = source.id ?? source.dataset_id;
  return typeof id === "string" ? id : "";
}

function findDatasetById(items: unknown, datasetId: string): CountSource {
  if (!datasetId || !Array.isArray(items)) return null;
  return asCountSource(items.find(item => {
    const source = asCountSource(item);
    return datasetIdFrom(source) === datasetId;
  }));
}

function selectedDatasetId(dataset?: unknown): string {
  const source = asCountSource(dataset);
  const id = source?.id ?? source?.dataset_id;
  return typeof id === "string" && id ? id : state.feedbackDataset || "all";
}

function sumDatasetCounts(keys: string[]): number | null {
  const datasets = Array.isArray(historicalSummary.value.datasets)
    ? historicalSummary.value.datasets
    : state.learningDatasets;
  const total = datasets.reduce((sum, item) => sum + (countFrom(asCountSource(item), keys) ?? 0), 0);
  return total > 0 ? total : null;
}

function trainingSampleCountFor(datasetId: string): number | null {
  const samples = state.trainingSamples;
  if (!samples.length) return null;
  const rowsWithDataset = samples.filter(sample => {
    const source = asCountSource(sample);
    return Boolean(source?.dataset_id || source?.dataset);
  });
  if (datasetId && datasetId !== "all" && rowsWithDataset.length) {
    const count = rowsWithDataset.filter(sample => {
      const source = asCountSource(sample);
      const nested = asCountSource(source?.dataset);
      return source?.dataset_id === datasetId || nested?.id === datasetId;
    }).length;
    return count || null;
  }
  return samples.length;
}

function datasetCounts(dataset?: unknown): LearningCountSummary {
  const datasetSource = asCountSource(dataset);
  const datasetId = selectedDatasetId(dataset);
  const isAll = datasetId === "all";
  const historicalDataset = findDatasetById(historicalSummary.value.datasets, datasetId);
  const importDataset = findDatasetById(historicalImport.value.datasets, datasetId);
  const prototypeDataset = datasetIdFrom(asCountSource(prototypeBank.value.dataset)) === datasetId ? asCountSource(prototypeBank.value.dataset) : null;
  const prototypeResult = prototypeBank.value.dataset_id === datasetId ? asCountSource(prototypeBank.value) : null;
  const aggregateSources = [
    datasetSource,
    asCountSource(historicalSummary.value),
    asCountSource(historicalImport.value),
    asCountSource(prototypeBank.value.source_summary),
    asCountSource(prototypeBank.value)
  ];
  const scopedSources = [
    datasetSource,
    historicalDataset,
    importDataset,
    prototypeDataset,
    prototypeResult
  ];
  const sources = isAll ? aggregateSources : scopedSources;
  const sourceRaw = firstCount(sources, SOURCE_RAW_KEYS)
    ?? (isAll ? sumDatasetCounts(SOURCE_RAW_KEYS) : null)
    ?? firstCount(sources, SOURCE_UNIQUE_KEYS)
    ?? (isAll ? sumDatasetCounts(SOURCE_UNIQUE_KEYS) : null)
    ?? firstCount(sources, HISTORICAL_KEYS)
    ?? 0;
  const sourceUnique = firstCount(sources, SOURCE_UNIQUE_KEYS)
    ?? (isAll ? sumDatasetCounts(SOURCE_UNIQUE_KEYS) : null)
    ?? firstCount(sources, HISTORICAL_KEYS)
    ?? sourceRaw;
  const historical = firstCount(sources, HISTORICAL_KEYS)
    ?? (isAll ? sumDatasetCounts(HISTORICAL_KEYS) : null)
    ?? sourceUnique;
  const trainingReady = firstCount(sources, TRAINING_READY_KEYS)
    ?? (isAll ? sumDatasetCounts(TRAINING_READY_KEYS) : null)
    ?? trainingSampleCountFor(datasetId)
    ?? Number(state.stats.training_samples || 0)
    ?? historical;
  return {
    sourceRaw,
    sourceUnique,
    historical,
    trainingReady
  };
}

function prototypeLevel(item: PrototypeBankItem): string {
  const level = item.parameters?.absolute_level || {};
  return level.code ? `${level.code} ${level.label || ""}`.trim() : "L- 待定";
}

function prototypeMetric(item: PrototypeBankItem): Record<string, unknown> {
  const metric = item.parameters?.performance_metric;
  return metric && typeof metric === "object" ? metric as Record<string, unknown> : {};
}

function prototypeBenchmarkLine(item: PrototypeBankItem): string {
  const metric = prototypeMetric(item);
  const liftInfo = item.parameters?.account_lift || {};
  const label = String(metric.label || "互动热度");
  const lift = Number(metric.p75_lift || liftInfo.p75_lift || 0);
  const accountP75 = Number(metric.account_p75 || liftInfo.account_p75_views || 0);
  const prototypeP75 = Number(metric.p75 || item.p75_views || 0);
  if (prototypeP75 <= 0) return `${label} P75 待学习`;
  if (lift <= 0 || accountP75 <= 0) return `该原型${label}高位线 ${compactNumber(prototypeP75)}，账号高位线待学习`;
  if (lift >= 1) {
    return `${label} P75 ${compactNumber(prototypeP75)}，相当于账号高位线 ${compactNumber(accountP75)} 的${lift.toFixed(1)}倍`;
  }
  return `${label} P75 ${compactNumber(prototypeP75)}，约为账号高位线 ${compactNumber(accountP75)} 的${Math.round(lift * 100)}%`;
}

function prototypeBenchmarkTip(item: PrototypeBankItem): string {
  const metric = prototypeMetric(item);
  const liftInfo = item.parameters?.account_lift || {};
  const label = String(metric.label || "互动热度");
  const basis = String(metric.basis || "reward_proxy");
  const accountP75 = Number(metric.account_p75 || liftInfo.account_p75_views || 0);
  const basisNote = basis === "views" ? "真实播放/可见播放口径" : "点赞、评论、收藏、转发生成的互动热度代理分";
  return `${label} P75：该原型自己的高位表现线，来自历史样本里靠前25%的${basisNote}。账号高位线：当前账号全部样本的同口径 P75${accountP75 > 0 ? `，当前约 ${compactNumber(accountP75)}` : ""}。`;
}

function prototypeStability(item: PrototypeBankItem): string {
  return item.parameters?.stability?.label || "稳定性待定";
}

function loadFeedbackSafe(): void {
  loadFeedback().catch(error => toast(error.message));
}

function onFeedbackAccountChange(): void {
  alignDatasetToFeedbackAccount();
  loadFeedbackSafe();
}

async function submitMetrics(): Promise<void> {
  const file = metricsFile.value?.files?.[0];
  if (!file) {
    toast("请选择 CSV 或 Excel 文件");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  await withBusy("metrics-import", () => importMetrics(form));
  if (metricsFile.value) metricsFile.value.value = "";
}

async function submitDouyinFile(): Promise<void> {
  const file = douyinFile.value?.files?.[0];
  if (!file) {
    toast("请选择 CSV、Excel 或 JSON 文件");
    return;
  }
  const form = new FormData();
  const fileName = file.name.toLowerCase();
  const source = fileName.endsWith(".json") ? "json" : (fileName.endsWith(".xlsx") || fileName.endsWith(".xslx") ? "xlsx" : "csv");
  form.append("file", file);
  form.append("account_id", state.feedbackAccount || "main");
  form.append("source", source);
  await withBusy("douyin-file", () => syncDouyinFile(form));
  if (douyinFile.value) douyinFile.value.value = "";
}
</script>
