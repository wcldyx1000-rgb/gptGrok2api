<template>
  <section>
    <PagePanel class="!rounded-xl">
      <PanelHeader title="运行环境" align="start" />

      <div class="mt-4 grid min-w-0 gap-5 xl:grid-cols-[minmax(20rem,0.9fr)_minmax(0,1.4fr)]">
        <section class="min-w-0 xl:h-full" aria-label="资源占用">
          <div class="grid grid-cols-2 gap-x-5 gap-y-5 xl:h-full xl:grid-rows-2 xl:gap-y-0">
            <div
              v-for="metric in resourceMetrics"
              :key="metric.label"
              class="min-w-0 xl:flex xl:flex-col xl:justify-center"
            >
              <div class="flex items-center justify-between gap-3">
                <div class="flex min-w-0 items-center gap-2">
                  <Icon :icon="metric.icon" class="h-4 w-4 shrink-0" :class="metric.iconClass" aria-hidden="true" />
                  <p class="truncate text-sm text-muted-foreground">{{ metric.label }}</p>
                </div>
                <p class="shrink-0 text-sm font-semibold tabular-nums text-foreground">{{ metric.value }}</p>
              </div>
              <div
                class="mt-2.5 h-1.5 overflow-hidden rounded-full bg-muted/70"
                :role="metric.progress === null ? undefined : 'progressbar'"
                :aria-label="metric.progress === null ? undefined : `${metric.label}占用率`"
                :aria-valuenow="metric.progress === null ? undefined : metric.progress"
                :aria-valuemin="metric.progress === null ? undefined : 0"
                :aria-valuemax="metric.progress === null ? undefined : 100"
              >
                <span
                  v-if="metric.progress !== null"
                  class="block h-full rounded-full bg-current transition-[width] duration-500"
                  :class="metric.progressClass"
                  :style="{ width: `${metric.progress}%` }"
                ></span>
              </div>
            </div>
          </div>
        </section>

        <section
          class="min-w-0 border-t border-border/60 pt-4 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-0"
          aria-label="运行信息"
        >
          <dl class="grid min-w-0 grid-cols-1 gap-x-6 sm:grid-cols-2">
            <div
              v-for="detail in details"
              :key="detail.label"
              class="flex min-w-0 items-center justify-between gap-3 border-b border-border/50 py-2 first:pt-0 sm:[&:nth-child(2)]:pt-0"
            >
              <dt class="flex min-w-0 shrink-0 items-center gap-2 text-xs text-muted-foreground">
                <Icon :icon="detail.icon" class="h-4 w-4 shrink-0" aria-hidden="true" />
                <span class="truncate">{{ detail.label }}</span>
              </dt>
              <dd class="min-w-0 truncate text-right text-sm font-medium tabular-nums" :title="detail.value">{{ detail.value }}</dd>
            </div>
          </dl>
        </section>
      </div>
    </PagePanel>
  </section>
</template>

<script setup lang="ts">
// Ported from yukkcat/chatgpt2api (AGPL-3.0-only): dashboard runtime environment card.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Icon } from '@iconify/vue'
import { versionApi } from '@/api/version'
import PagePanel from '@/components/ai/PagePanel.vue'
import PanelHeader from '@/components/ai/PanelHeader.vue'
import type { DashboardRuntime } from '@/types/api'
import { readDashboardRefreshIntervalSeconds } from './dashboardPreferences'

const runtime = ref<DashboardRuntime | null>(null)
const appVersion = ref('')
let timer: number | null = null
let disposed = false
let loading = false

// 与概览自动刷新保持同一偏好间隔；页面隐藏时停止，重新可见后立即刷新一次（串行，不并发）。
async function load() {
  if (loading || disposed) return
  loading = true
  try {
    runtime.value = await versionApi.runtime()
  } catch {
    // 保留上一次数据；运行环境卡片失败不影响概览其它部分。
  } finally {
    loading = false
    scheduleNext()
  }
}

function scheduleNext() {
  if (timer !== null) window.clearTimeout(timer)
  timer = null
  if (disposed || document.hidden) return
  timer = window.setTimeout(load, readDashboardRefreshIntervalSeconds() * 1000)
}

function handleVisibilityChange() {
  if (document.hidden) {
    if (timer !== null) window.clearTimeout(timer)
    timer = null
    return
  }
  void load()
}

onMounted(() => {
  void load()
  document.addEventListener('visibilitychange', handleVisibilityChange)
  versionApi.current().then((info) => { appVersion.value = info.tag }).catch(() => {})
})
onBeforeUnmount(() => {
  disposed = true
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  if (timer !== null) window.clearTimeout(timer)
})

function formatPercent(value: number | null | undefined) {
  return value === null || value === undefined ? '--' : `${value.toFixed(1)}%`
}

function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '--'
  if (value < 1_024) return `${Math.round(value)} B`
  if (value < 1_024 ** 2) return `${(value / 1_024).toFixed(1)} KB`
  if (value < 1_024 ** 3) return `${(value / 1_024 ** 2).toFixed(1)} MB`
  if (value < 1_024 ** 4) return `${(value / 1_024 ** 3).toFixed(1)} GB`
  return `${(value / 1_024 ** 4).toFixed(1)} TB`
}

function formatRate(value: number | null | undefined) {
  if (value === null || value === undefined) return '--'
  if (value < 1_000) return `${Math.round(value)} B/s`
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1)} KB/s`
  return `${(value / 1_000_000).toFixed(1)} MB/s`
}

function formatCpuCapacity(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return '--'
  const digits = Number.isInteger(value) ? 0 : 2
  return `${value.toLocaleString('zh-CN', { maximumFractionDigits: digits })} 核`
}

function memoryScopeLabel(scope: 'container' | 'system' | 'visible' | undefined) {
  if (scope === 'container') return '容器内存'
  if (scope === 'system') return '系统内存'
  return '可见内存'
}

function formatUptime(value: number | null | undefined) {
  if (value === null || value === undefined) return '--'
  const seconds = Math.max(0, Math.trunc(value))
  const days = Math.floor(seconds / 86_400)
  const hours = Math.floor((seconds % 86_400) / 3_600)
  const minutes = Math.floor((seconds % 3_600) / 60)
  if (days > 0) return `${days}天 ${hours}小时`
  if (hours > 0) return `${hours}小时 ${minutes}分`
  return `${minutes}分`
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return '--'
  return value.replace('T', ' ').replace(/([+-]\d\d:\d\d|Z)$/, '')
}

const resourceMetrics = computed(() => {
  const current = runtime.value
  return [
    {
      label: '应用 CPU',
      value: formatPercent(current?.process_cpu_percent),
      icon: 'lucide:cpu',
      iconClass: 'text-sky-600 dark:text-sky-400',
      progress: current?.process_cpu_percent ?? null,
      progressClass: 'text-sky-500 dark:text-sky-400',
    },
    {
      label: '应用内存',
      value: formatBytes(current?.process_memory_bytes),
      icon: 'lucide:memory-stick',
      iconClass: 'text-indigo-600 dark:text-indigo-400',
      progress: current?.process_memory_percent ?? null,
      progressClass: 'text-indigo-500 dark:text-indigo-400',
    },
    {
      label: memoryScopeLabel(current?.memory_scope),
      value: formatPercent(current?.memory_percent),
      icon: 'lucide:memory-stick',
      iconClass: 'text-amber-600 dark:text-amber-400',
      progress: current?.memory_percent ?? null,
      progressClass: 'text-amber-500 dark:text-amber-400',
    },
    {
      label: '数据盘',
      value: formatPercent(current?.storage_percent),
      icon: 'lucide:hard-drive',
      iconClass: 'text-muted-foreground',
      progress: current?.storage_percent ?? null,
      progressClass: 'text-slate-500 dark:text-slate-400',
    },
  ]
})

const details = computed(() => {
  const current = runtime.value
  return [
    {
      label: '运行方式',
      value: current ? (current.runtime_mode === 'docker' ? 'Docker' : `Python ${current.python_version}`) : '--',
      icon: 'lucide:server-cog',
    },
    { label: '应用版本', value: appVersion.value || '--', icon: 'lucide:tag' },
    { label: '实例名称', value: current?.instance_name || '--', icon: 'lucide:hard-drive' },
    { label: '系统发行版', value: current?.distribution || '--', icon: 'lucide:binary' },
    {
      label: '内核 / 架构',
      value: current ? `${current.kernel_version} / ${current.architecture}` : '--',
      icon: 'lucide:binary',
    },
    { label: 'CPU 容量', value: formatCpuCapacity(current?.cpu_capacity), icon: 'lucide:cpu' },
    { label: '服务启动', value: formatDateTime(current?.service_started_at), icon: 'lucide:clock-3' },
    { label: '入站速率', value: formatRate(current?.network_rx_bytes_per_sec), icon: 'lucide:arrow-down-to-line' },
    { label: '服务运行', value: formatUptime(current?.service_uptime_seconds), icon: 'lucide:timer' },
    { label: '出站速率', value: formatRate(current?.network_tx_bytes_per_sec), icon: 'lucide:arrow-up-from-line' },
  ]
})
</script>
