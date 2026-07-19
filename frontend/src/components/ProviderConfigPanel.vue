<template>
  <div v-show="active" id="provider-config" class="feedback-block provider-config-block">
    <h3><Icon name="settings-2" />公网模型 API</h3>
    <div class="inner provider-config-inner">
      <div class="provider-config-head">
        <div>
          <span class="eyebrow">ALIYUN BAILIAN · G3</span>
          <strong>百炼连接配置</strong>
          <p>只保存连接信息，不会启用公网调用，也不会产生模型费用。</p>
        </div>
        <span class="status" :class="status?.api_key_configured ? 'ok' : 'warn'">
          {{ status?.api_key_configured ? "密钥已配置" : "待配置密钥" }}
        </span>
      </div>

      <div v-if="loading" class="provider-config-loading" role="status">
        <span class="spinner"></span>正在读取安全配置状态
      </div>
      <template v-else>
        <div class="provider-security-note" :class="secureSubmission ? 'safe' : 'blocked'" role="status">
          <Icon :name="secureSubmission ? 'shield-check' : 'shield-alert'" />
          <div>
            <strong>{{ secureSubmission ? "当前连接允许安全保存" : "当前连接禁止提交密钥" }}</strong>
            <span>{{ securityReason }}</span>
          </div>
        </div>

        <form class="provider-config-form" autocomplete="off" @submit.prevent="saveConfig">
          <label class="provider-field provider-field-wide">
            <span>OpenAI 兼容地址</span>
            <input
              v-model.trim="form.base_url"
              type="url"
              inputmode="url"
              placeholder="https://&lt;workspace&gt;.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
              :disabled="!secureSubmission || saving"
              required
            />
            <small>粘贴百炼页面中的“OpenAI 兼容地址”，仅接受北京地域工作空间 HTTPS 地址。</small>
          </label>

          <label class="provider-field">
            <span>固定模型快照</span>
            <select v-model="form.model_id" :disabled="!secureSubmission || saving" required>
              <option v-for="model in allowedModels" :key="model" :value="model">{{ model }}</option>
            </select>
          </label>

          <label class="provider-field">
            <span>API Key</span>
            <input
              v-model="form.api_key"
              type="password"
              name="bailian-api-key"
              autocomplete="new-password"
              autocapitalize="none"
              spellcheck="false"
              data-1p-ignore
              data-lpignore="true"
              :placeholder="status?.api_key_configured ? '留空保持现有密钥' : 'sk-…'"
              :disabled="!secureSubmission || saving"
              :required="!status?.api_key_configured"
            />
            <small>密钥只写入 ECS 权限文件，保存成功后输入框立即清空。</small>
          </label>

          <fieldset class="provider-budget-fields provider-field-wide" :disabled="!secureSubmission || saving">
            <legend>费用硬上限（人民币）</legend>
            <label><span>单请求</span><input v-model.trim="form.per_request_cny" type="number" min="0.0001" step="0.0001" required /></label>
            <label><span>单批次</span><input v-model.trim="form.per_batch_cny" type="number" min="0.0001" step="0.0001" required /></label>
            <label><span>单日</span><input v-model.trim="form.per_day_cny" type="number" min="0.0001" step="0.0001" required /></label>
          </fieldset>

          <div v-if="!secureSubmission" class="provider-tunnel provider-field-wide">
            <div>
              <strong>建议通过 SSH 本地端口转发配置</strong>
              <code>{{ tunnelCommand }}</code>
              <span>保持命令运行，然后打开 <code>http://127.0.0.1:8765/</code>。</span>
            </div>
            <button type="button" @click="copyTunnelCommand"><Icon name="copy" />{{ copied ? "已复制" : "复制命令" }}</button>
          </div>

          <div v-if="message" class="provider-config-message provider-field-wide" :class="messageKind" aria-live="polite">
            <Icon :name="messageKind === 'success' ? 'check-circle-2' : 'circle-alert'" />{{ message }}
          </div>

          <div class="provider-config-actions provider-field-wide">
            <div>
              <span>保存后公网 API 仍为关闭</span>
              <small>数据许可、保留策略和人工验证通过后才能另行启用。</small>
            </div>
            <button class="primary" type="submit" :disabled="!secureSubmission || saving">
              <span v-if="saving" class="spinner"></span><Icon v-else name="shield-check" />安全保存（不调用模型）
            </button>
          </div>
        </form>

        <div class="provider-gates" aria-label="公网模型治理门禁">
          <div v-for="gate in gateRows" :key="gate.key">
            <span>{{ gate.label }}</span>
            <strong class="status" :class="gate.ready ? 'ok' : 'warn'">{{ gate.ready ? "已满足" : "未满足" }}</strong>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { api, jsonBody } from "../api";
import Icon from "./Icon.vue";

interface ProviderConfigStatus {
  provider: string;
  model_id: string;
  base_url: string;
  api_key_configured: boolean;
  budgets: Record<string, string>;
  allowed_models: string[];
  secure_submission_allowed: boolean;
  secure_submission_reason: string;
  public_api_enabled: boolean;
  network_calls_allowed: boolean;
  gates: Record<string, boolean>;
  configuration_errors: string[];
  saved?: boolean;
}

const props = defineProps<{ active: boolean }>();
const status = ref<ProviderConfigStatus | null>(null);
const loading = ref(false);
const saving = ref(false);
const loaded = ref(false);
const copied = ref(false);
const message = ref("");
const messageKind = ref<"success" | "error">("success");
const form = reactive({
  base_url: "",
  model_id: "qwen3.5-flash-2026-02-23",
  api_key: "",
  per_request_cny: "0.05",
  per_batch_cny: "0.20",
  per_day_cny: "1.00"
});

const tunnelCommand = "ssh -i /Users/fuqiang/aliyun/douyin.pem -L 8765:127.0.0.1:8000 root@121.199.170.85";
const secureSubmission = computed(() => status.value?.secure_submission_allowed === true);
const securityReason = computed(() => status.value?.secure_submission_reason || "正在检查连接安全性");
const allowedModels = computed(() => status.value?.allowed_models || [form.model_id]);
const gateRows = computed(() => {
  const gates = status.value?.gates || {};
  return [
    { key: "connection", label: "连接与预算", ready: Boolean(gates.provider_selected && gates.fixed_model_selected && gates.workspace_base_url_configured && gates.secret_configured && gates.budget_configured) },
    { key: "permission", label: "数据出境许可", ready: Boolean(gates.data_permission_configured) },
    { key: "retention", label: "保留策略确认", ready: Boolean(gates.retention_policy_confirmed) },
    { key: "enabled", label: "公网调用开关", ready: Boolean(gates.public_api_enabled) }
  ];
});

function applyStatus(value: ProviderConfigStatus): void {
  status.value = value;
  form.base_url = value.base_url || "";
  form.model_id = value.model_id || form.model_id;
  form.api_key = "";
  form.per_request_cny = value.budgets?.per_request_cny || "0.05";
  form.per_batch_cny = value.budgets?.per_batch_cny || "0.20";
  form.per_day_cny = value.budgets?.per_day_cny || "1.00";
}

async function loadConfig(): Promise<void> {
  loading.value = true;
  message.value = "";
  try {
    applyStatus(await api<ProviderConfigStatus>("/providers/config", { cache: "no-store" }));
    loaded.value = true;
  } catch (error) {
    messageKind.value = "error";
    message.value = error instanceof Error ? error.message : "无法读取 Provider 配置";
  } finally {
    loading.value = false;
  }
}

async function saveConfig(): Promise<void> {
  if (!secureSubmission.value || saving.value) return;
  saving.value = true;
  message.value = "";
  try {
    const value = await api<ProviderConfigStatus>("/providers/config", jsonBody({
      provider: "aliyun_bailian",
      model_id: form.model_id,
      base_url: form.base_url,
      api_key: form.api_key,
      per_request_cny: form.per_request_cny,
      per_batch_cny: form.per_batch_cny,
      per_day_cny: form.per_day_cny
    }));
    applyStatus(value);
    messageKind.value = "success";
    message.value = "连接配置已安全保存；公网调用仍为关闭，未发起模型请求。";
  } catch (error) {
    form.api_key = "";
    messageKind.value = "error";
    message.value = error instanceof Error ? error.message : "保存失败";
  } finally {
    saving.value = false;
  }
}

async function copyTunnelCommand(): Promise<void> {
  try {
    await navigator.clipboard.writeText(tunnelCommand);
    copied.value = true;
    window.setTimeout(() => { copied.value = false; }, 1600);
  } catch {
    messageKind.value = "error";
    message.value = "复制失败，请手动选择命令。";
  }
}

watch(() => props.active, active => {
  if (active && !loaded.value && !loading.value) loadConfig();
}, { immediate: true });
</script>
