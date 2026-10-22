<template>
  <FormSection title="稳定代理 / Cloudflare 清障">
    <div class="rounded-xl border border-border bg-background px-3 py-3">
      <div class="grid grid-cols-2 gap-2 text-xs md:grid-cols-5">
        <div
          v-for="item in summaryItems"
          :key="item.label"
          class="min-w-0 rounded-lg border border-border/70 bg-card px-2.5 py-2"
        >
          <p class="text-muted-foreground">{{ item.label }}</p>
          <p class="mt-1 truncate font-medium text-foreground">{{ item.value }}</p>
        </div>
      </div>
      <p v-if="runtimeStatus?.cached_clearance_hosts?.length" class="mt-2 break-all text-xs text-muted-foreground">
        已缓存：{{ runtimeStatus.cached_clearance_hosts.join(' / ') }}
      </p>
      <p v-if="runtimeStatus?.flaresolverr_sessions" class="mt-1 text-xs text-muted-foreground">
        FlareSolverr 复用会话：{{ runtimeStatus.flaresolverr_sessions }} 个
      </p>
      <div v-if="runtimeStatus?.disabled_proxy_nodes?.length" class="mt-2 space-y-1 text-xs">
        <p class="text-amber-600">临时停用的出口（冷却后自动恢复）：</p>
        <p
          v-for="node in runtimeStatus.disabled_proxy_nodes"
          :key="node.proxy"
          class="break-all font-mono text-muted-foreground"
        >
          {{ node.proxy }} · 剩余 {{ Math.ceil(node.remaining_sec / 60) }} 分钟<span v-if="node.last_error"> · {{ node.last_error }}</span>
        </p>
      </div>
    </div>

    <div class="settings-check-grid">
      <div class="settings-check-item">
        <div class="settings-check-control">
          <Checkbox v-model="proxyRuntime.enabled">启用稳定代理运行时</Checkbox>
          <HelpTip text="关闭时不会接管上游请求。" />
        </div>
      </div>
      <div class="settings-check-item">
        <div class="settings-check-control">
          <Checkbox v-model="proxyRuntime.clearance.enabled">启用 Cloudflare 清障</Checkbox>
          <HelpTip text="只关闭清障时，可保留代理出站但不会注入 clearance。" />
        </div>
      </div>
      <div class="settings-check-item">
        <div class="settings-check-control">
          <Checkbox v-model="proxyRuntime.skip_ssl_verify">跳过上游 SSL 校验</Checkbox>
          <HelpTip text="仅在代理或上游证书链异常时使用。" />
        </div>
      </div>
      <div class="settings-check-item">
        <div class="settings-check-control">
          <Checkbox v-model="proxyRuntime.clearance.warm_up_on_start">启动后预热 clearance</Checkbox>
          <HelpTip text="服务启动后主动获取一次 clearance，减少首个请求等待。" />
        </div>
      </div>
    </div>

    <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
      <FormField label="出站方式">
        <GroupedSelectMenu
          v-model="proxyRuntime.egress_mode"
          :options="proxyRuntimeEgressOptions"
          selected-indicator="none"
          aria-label="稳定代理出站方式"
          block
        />
      </FormField>

      <FormField label="清障方式">
        <GroupedSelectMenu
          v-model="proxyRuntime.clearance.mode"
          :options="proxyClearanceModeOptions"
          selected-indicator="none"
          aria-label="Cloudflare 清障方式"
          block
        />
      </FormField>

      <FormField label="代理地址">
        <template #label-extra>
          <HelpTip text="Docker 清障编排默认使用 Privoxy HTTP 代理。" />
        </template>
        <Input
          v-model.trim="proxyRuntime.proxy_url"
          block
          root-class="font-mono"
          placeholder="http://privoxy:8118"
          @update:model-value="$emit('clearClearanceTestResult')"
        />
      </FormField>

      <FormField label="资源代理地址">
        <Input
          v-model.trim="proxyRuntime.resource_proxy_url"
          block
          root-class="font-mono"
          placeholder="留空则复用代理地址"
          @update:model-value="$emit('clearClearanceTestResult')"
        />
      </FormField>

      <FormField label="默认出口时区">
        <template #label-extra>
          <HelpTip text="未单独配置时区/语言的出口（含直连）使用的 IANA 时区，应与服务器出口 IP 所在地一致；与语言同时填写才生效，留空沿用 Asia/Shanghai。" />
        </template>
        <Input
          v-model.trim="proxyRuntime.timezone"
          block
          root-class="font-mono"
          placeholder="如 America/New_York"
        />
      </FormField>

      <FormField label="默认出口语言">
        <template #label-extra>
          <HelpTip text="BCP 47 语言标签，用于 Accept-Language、OAI-Language 和 pow/sentinel 环境；与时区同时填写才生效，留空沿用 zh-CN。" />
        </template>
        <Input
          v-model.trim="proxyRuntime.language"
          block
          root-class="font-mono"
          placeholder="如 en-US"
        />
      </FormField>

      <FormField label="出口失败停用阈值">
        <template #label-extra>
          <HelpTip text="代理组里的节点连续失败达到这个次数后临时停用（不再被选中），整组都停用时照常选。只影响运行时选择，不改动代理配置。1–20，默认 1。" />
        </template>
        <SettingsNumberInput :field="nodeFailThresholdField" />
      </FormField>

      <FormField label="出口停用时长">
        <template #label-extra>
          <HelpTip text="单位秒，最小 10；停用到期后节点自动恢复参与选择。默认 600。" />
        </template>
        <SettingsNumberInput :field="nodeCooldownField" />
      </FormField>

      <FormField
        v-if="proxyRuntime.clearance.mode === 'flaresolverr'"
        label="FlareSolverr URL"
        class="md:col-span-2"
      >
        <Input
          v-model.trim="proxyRuntime.clearance.flaresolverr_url"
          block
          root-class="font-mono"
          placeholder="http://flaresolverr:8191"
          @update:model-value="$emit('clearClearanceTestResult')"
        />
      </FormField>

      <template v-if="proxyRuntime.clearance.mode === 'manual'">
        <FormField label="cf_clearance">
          <Input
            v-model.trim="proxyRuntime.clearance.cf_clearance"
            block
            root-class="font-mono"
            :placeholder="proxyRuntime.clearance.has_cf_clearance ? '已保存，留空则沿用' : '手动填写 cf_clearance'"
            @update:model-value="$emit('clearClearanceTestResult')"
          />
        </FormField>

        <FormField label="Cookie">
          <Input
            v-model.trim="proxyRuntime.clearance.cf_cookies"
            block
            root-class="font-mono"
            :placeholder="proxyRuntime.clearance.has_cf_cookies ? '已保存，留空则沿用' : '可粘贴完整 Cookie'"
            @update:model-value="$emit('clearClearanceTestResult')"
          />
        </FormField>
      </template>

      <FormField label="User-Agent" class="md:col-span-2">
        <Input
          v-model.trim="proxyRuntime.clearance.user_agent"
          block
          root-class="font-mono"
          placeholder="Mozilla/5.0 ..."
          @update:model-value="$emit('clearClearanceTestResult')"
        />
      </FormField>

      <FormField label="清障超时">
        <template #label-extra>
          <HelpTip text="单位秒。" />
        </template>
        <SettingsNumberInput :field="clearanceTimeoutField" />
      </FormField>

      <FormField label="缓存刷新间隔">
        <template #label-extra>
          <HelpTip text="单位秒，最小 60。" />
        </template>
        <SettingsNumberInput :field="clearanceRefreshIntervalField" />
      </FormField>

      <template v-if="proxyRuntime.clearance.mode === 'flaresolverr'">
        <FormField label="提前续期比例">
          <template #label-extra>
            <HelpTip text="凭证用掉这个比例的有效期（以缓存刷新间隔计）后在后台换新，请求不用原地等清障。0.1–0.95，默认 0.8。" />
          </template>
          <SettingsNumberInput :field="earlyRefreshRatioField" />
        </FormField>

        <FormField label="FlareSolverr 会话上限">
          <template #label-extra>
            <HelpTip text="每个出口复用一个常驻浏览器会话，再次过验证不用冷启动；每个会话约占数百 MB 内存。1–16，默认 4。" />
          </template>
          <SettingsNumberInput :field="maxSessionsField" />
        </FormField>

        <FormField label="会话空闲回收">
          <template #label-extra>
            <HelpTip text="单位秒，最小 30；会话空闲超过这个时间自动关闭释放内存。" />
          </template>
          <SettingsNumberInput :field="sessionIdleField" />
        </FormField>
      </template>

      <FormField label="测试目标" class="md:col-span-2">
        <div class="flex flex-col gap-2 sm:flex-row">
          <Input
            :model-value="clearanceTestTarget"
            block
            root-class="font-mono"
            placeholder="https://chatgpt.com"
            @update:model-value="$emit('update:clearanceTestTarget', String($event))"
          />
          <Button
            size="sm"
            variant="outline"
            root-class="shrink-0"
            :disabled="runtimeLoading"
            @click="$emit('refreshRuntimeStatus')"
          >
            {{ runtimeLoading ? '刷新中...' : '刷新状态' }}
          </Button>
          <Button
            size="sm"
            variant="outline"
            root-class="shrink-0"
            :disabled="runtimeTesting"
            @click="$emit('testClearance')"
          >
            {{ runtimeTesting ? '测试中...' : '测试清障' }}
          </Button>
        </div>
      </FormField>
    </div>

    <div v-if="clearanceTestResult" class="rounded-xl border border-border bg-background px-3 py-2 text-xs">
      <p :class="clearanceTestResult.ok ? 'text-emerald-600' : 'text-rose-600'">
        {{ clearanceTestResult.ok ? `清障可用：${clearanceTestResult.latency_ms} ms` : `清障不可用：${clearanceTestResult.error || '未知错误'}` }}
      </p>
      <p v-if="clearanceTestResult.user_agent" class="mt-1 break-all text-muted-foreground">
        User-Agent：{{ clearanceTestResult.user_agent }}
      </p>
    </div>
  </FormSection>
</template>

<script setup lang="ts">
import SettingsNumberInput from './SettingsNumberInput.vue'
import { computed } from 'vue'
import { Button, Checkbox, FormField, FormSection, HelpTip, Input } from 'nanocat-ui'
import type { ClearanceTestResult, ProxyRuntimeStatus, Settings } from '@/types/api'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import {
  buildProxyRuntimeSummaryItems,
  proxyClearanceModeOptions,
  proxyRuntimeEgressOptions,
} from '@/views/settings/settingsView'
import { useNumberSettingField, type NumberSettingField } from '@/views/settings/useNumberSettingField'

const props = defineProps<{
  settings: Settings
  runtimeStatus: ProxyRuntimeStatus | null
  runtimeLoading: boolean
  runtimeTesting: boolean
  clearanceTestTarget: string
  clearanceTestResult: ClearanceTestResult | null
  clearanceTimeoutField: NumberSettingField
  clearanceRefreshIntervalField: NumberSettingField
}>()

defineEmits<{
  'update:clearanceTestTarget': [value: string]
  clearClearanceTestResult: []
  refreshRuntimeStatus: []
  testClearance: []
}>()

const proxyRuntime = computed(() => props.settings.proxy_runtime)
const summaryItems = computed(() => buildProxyRuntimeSummaryItems(props.runtimeStatus))

const earlyRefreshRatioField = useNumberSettingField(
  () => proxyRuntime.value.clearance.early_refresh_ratio ?? 0.8,
  (value) => { proxyRuntime.value.clearance.early_refresh_ratio = value },
  { min: 0.1, max: 0.95, fallback: 0.8 },
)
const maxSessionsField = useNumberSettingField(
  () => proxyRuntime.value.clearance.max_sessions ?? 4,
  (value) => { proxyRuntime.value.clearance.max_sessions = value },
  { integer: true, min: 1, max: 16, fallback: 4 },
)
const sessionIdleField = useNumberSettingField(
  () => proxyRuntime.value.clearance.session_idle_sec ?? 600,
  (value) => { proxyRuntime.value.clearance.session_idle_sec = value },
  { integer: true, min: 30, fallback: 600 },
)
const nodeFailThresholdField = useNumberSettingField(
  () => proxyRuntime.value.node_fail_threshold ?? 1,
  (value) => { proxyRuntime.value.node_fail_threshold = value },
  { integer: true, min: 1, max: 20, fallback: 1 },
)
const nodeCooldownField = useNumberSettingField(
  () => proxyRuntime.value.node_cooldown_sec ?? 600,
  (value) => { proxyRuntime.value.node_cooldown_sec = value },
  { integer: true, min: 10, max: 86400, fallback: 600 },
)
</script>

<style scoped>
.settings-check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
  gap: 8px;
}

.settings-check-item {
  min-height: 38px;
  border: 1px solid hsl(var(--border));
  border-radius: 14px;
  background: hsl(var(--background) / 0.72);
  transition:
    border-color 0.16s ease,
    background-color 0.16s ease;
}

.settings-check-item:hover {
  border-color: hsl(var(--foreground) / 0.18);
  background: hsl(var(--muted) / 0.24);
}

.settings-check-control {
  display: flex;
  min-height: 38px;
  align-items: center;
  gap: 8px;
  padding-right: 10px;
}

.settings-check-item :deep(label) {
  display: flex;
  width: 100%;
  flex: 1;
  min-height: 38px;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
}

.settings-check-item :deep(label > span:last-child) {
  color: hsl(var(--foreground) / 0.78);
  line-height: 1.35;
}
</style>
