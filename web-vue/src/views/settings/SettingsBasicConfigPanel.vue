<template>
  <FormSection title="基础配置">
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
      <FormField label="账号刷新间隔">
        <template #label-extra>
          <HelpTip text="单位分钟，控制账号自动刷新频率。" />
        </template>
        <SettingsNumberInput :field="refreshAccountIntervalField" />
      </FormField>

      <FormField label="图片访问地址">
        <template #label-extra>
          <HelpTip text="用于生成图片结果的访问前缀地址。" />
        </template>
        <Input
          v-model.trim="settings.base_url"
          block
          :disabled="Boolean(baseUrlReadOnly)"
          placeholder="https://example.com"
        />
        <p v-if="baseUrlReadOnly" class="mt-1 text-xs text-warning">{{ baseUrlReadOnly }}</p>
      </FormField>

      <FormField label="默认出口" class="md:col-span-2">
        <template #label-extra>
          <HelpTip text="账号个人代理、账号组代理优先于默认出口。可填写代理 URL、direct 或 group:代理组ID；完整选择可到代理管理维护。" />
        </template>
        <div class="flex flex-col gap-2 sm:flex-row">
          <Input
            v-model.trim="settings.proxy"
            block
            root-class="font-mono"
            placeholder="http://127.0.0.1:7890"
            @update:model-value="$emit('clearProxyTestResult')"
          />
          <Button
            size="sm"
            variant="outline"
            root-class="shrink-0"
            :disabled="proxyBusy === 'test'"
            @click="$emit('testDefaultProxy')"
          >
            {{ proxyBusy === 'test' ? '测试中...' : '测试出口' }}
          </Button>
        </div>
        <div v-if="proxyTestResult" class="mt-2 rounded-xl border border-border bg-background px-3 py-2 text-xs">
          <p :class="proxyTestResult.ok ? 'text-emerald-600' : 'text-rose-600'">
            {{ proxyTestResult.ok ? `出口可用：HTTP ${proxyTestResult.status}，${proxyTestResult.latency_ms} ms` : `出口不可用：${proxyTestResult.error || '未知错误'}` }}
          </p>
        </div>
      </FormField>

      <FormField label="图片保留（小时）">
        <template #label-extra>
          <HelpTip text="自动删除多少小时前的本地图片，按文件修改时间精确滚动计算，默认 720 小时（30 天）。旧配置的天数会自动换算为小时。" />
        </template>
        <SettingsNumberInput :field="imageRetentionHoursField" />
      </FormField>

      <FormField label="日志保留（小时）">
        <template #label-extra>
          <HelpTip text="自动删除多少小时前的控制台调用日志，默认 720 小时（30 天）；看板统计独立保存，不受影响。" />
        </template>
        <SettingsNumberInput :field="logRetentionHoursField" />
      </FormField>

      <FormField label="图片最低剩余空间（MB）">
        <template #label-extra>
          <HelpTip text="磁盘剩余空间低于该值时，自动从最旧的图片开始删除直到恢复；0 表示不启用，默认 500。" />
        </template>
        <SettingsNumberInput :field="imageMinFreeMbField" />
      </FormField>

      <div class="flex flex-wrap items-center gap-2 md:col-span-2">
        <Button size="sm" variant="outline" :disabled="cleanupBusy !== ''" @click="$emit('previewCleanup')">
          {{ cleanupBusy === 'preview' ? '检测中...' : '清理预览' }}
        </Button>
        <Button size="sm" variant="outline" :disabled="cleanupBusy !== ''" @click="$emit('runCleanup')">
          {{ cleanupBusy === 'run' ? '清理中...' : '立即清理' }}
        </Button>
        <span class="text-xs text-muted-foreground">按已保存的保留策略检测或立即删除过期日志与图片；后台每 30 分钟自动执行一次。</span>
      </div>

      <FormField label="控制台请求超时">
        <template #label-extra>
          <HelpTip text="单位秒，管理台调用后端 API 的最长等待时间，范围 30–3600，默认 600。保存后立即对当前页面生效；登录状态检查固定 8 秒。" />
        </template>
        <SettingsNumberInput :field="consoleRequestTimeoutField" />
      </FormField>

      <FormField label="图片轮询超时">
        <template #label-extra>
          <HelpTip text="单位秒，等待上游图片结果的最长时间。" />
        </template>
        <SettingsNumberInput :field="imagePollTimeoutField" />
      </FormField>

      <FormField label="上游流超时">
        <template #label-extra>
          <HelpTip text="单位秒，限制上游生图流最长等待时间。单次图片请求总上限 = 上游流超时 + 图片轮询超时 + 30 秒。" />
        </template>
        <SettingsNumberInput :field="imageStreamTimeoutField" />
      </FormField>

      <FormField label="单账号图片并发">
        <template #label-extra>
          <HelpTip text="限制每个账号同时处理的图片请求数量，范围 1-3，默认 1（更稳妥，降低风控概率）。" />
        </template>
        <SettingsNumberInput :field="imageAccountConcurrencyField" />
      </FormField>

      <FormField label="超时继续等待">
        <template #label-extra>
          <HelpTip text="单位秒，图片超时后继续等待的额外时间。" />
        </template>
        <SettingsNumberInput :field="imageTimeoutRetryField" />
      </FormField>
    </div>
  </FormSection>
</template>

<script setup lang="ts">
import SettingsNumberInput from './SettingsNumberInput.vue'
import { Button, FormField, FormSection, HelpTip, Input } from 'nanocat-ui'
import type { ProxyTestResult } from '@/api/proxy'
import type { Settings } from '@/types/api'
import type { NumberSettingField } from '@/views/settings/useNumberSettingField'

defineProps<{
  settings: Settings
  refreshAccountIntervalField: NumberSettingField
  imageRetentionHoursField: NumberSettingField
  logRetentionHoursField: NumberSettingField
  imageMinFreeMbField: NumberSettingField
  cleanupBusy: '' | 'preview' | 'run'
  consoleRequestTimeoutField: NumberSettingField
  imagePollTimeoutField: NumberSettingField
  imageStreamTimeoutField: NumberSettingField
  imageAccountConcurrencyField: NumberSettingField
  imageTimeoutRetryField: NumberSettingField
  proxyBusy: string
  proxyTestResult: ProxyTestResult | null
  baseUrlReadOnly?: string
}>()

defineEmits<{
  clearProxyTestResult: []
  testDefaultProxy: []
  previewCleanup: []
  runCleanup: []
}>()
</script>
