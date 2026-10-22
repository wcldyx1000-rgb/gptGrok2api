<!-- Account operation progress drawer. Idea ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<!-- 统一的账号操作进度抽屉：进度条 + 指标 + 逐账号事件日志（数据来自 /api/accounts/operations/{id}）。 -->
<template>
  <ModalShell
    :open="open"
    placement="end"
    align="start"
    max-width="28rem"
    :z-index="140"
    :close-on-backdrop="false"
  >
    <ModalHeader :title="title" :close-disabled="closeDisabled" compact @close="emit('close')">
      <template #actions>
        <Button
          v-if="canStop"
          size="xs"
          variant="outline"
          root-class="min-w-14 justify-center text-amber-600"
          :disabled="stopping"
          @click="emit('stop')"
        >
          {{ stopping ? '停止中...' : '停止' }}
        </Button>
      </template>
    </ModalHeader>
    <div class="space-y-4 px-5 py-4">
      <div class="flex items-center justify-between text-xs text-muted-foreground">
        <span>{{ statusText }} · {{ processed }} / {{ total }}</span>
        <span>{{ percent }}%</span>
      </div>
      <ProgressBar :value="percent" aria-label="账号操作进度" />
      <MetricStrip v-if="metrics.length" :items="metrics" columns-class="grid-cols-2" density="compact" />
      <SurfaceBox v-if="error" tag="p" tone="danger" density="compact">{{ error }}</SurfaceBox>
      <section v-if="events.length" aria-label="处理明细">
        <p class="mb-1 text-[11px] text-muted-foreground">处理明细（最近 {{ visibleEvents.length }} 条，共 {{ events.length }} 条）</p>
        <ul class="max-h-72 space-y-1 overflow-auto text-xs">
          <li
            v-for="event in visibleEvents"
            :key="event.sequence"
            class="flex items-start gap-2 rounded px-1 py-0.5"
            :title="event.message"
          >
            <span class="mt-0.5 shrink-0 font-semibold" :class="eventTone(event.status)">{{ eventLabel(event.status) }}</span>
            <span class="min-w-0 flex-1 truncate">
              <span class="text-foreground">{{ event.account_label || event.account_id || '—' }}</span>
              <span class="text-muted-foreground"> · {{ event.message }}</span>
            </span>
          </li>
        </ul>
      </section>
    </div>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button } from 'nanocat-ui'

import type { AccountOperationEvent } from '@/api/accounts'
import MetricStrip from './MetricStrip.vue'
import ModalHeader from './ModalHeader.vue'
import ModalShell from './ModalShell.vue'
import ProgressBar from './ProgressBar.vue'
import SurfaceBox from './SurfaceBox.vue'

const props = withDefaults(defineProps<{
  open: boolean
  title: string
  processed?: number
  total?: number
  percent?: number
  statusText?: string
  metrics?: Array<{ key: string, label: string, value: string | number }>
  events?: AccountOperationEvent[]
  error?: string
  closeDisabled?: boolean
  canStop?: boolean
  stopping?: boolean
}>(), {
  processed: 0,
  total: 0,
  percent: 0,
  statusText: '',
  metrics: () => [],
  events: () => [],
  error: '',
  closeDisabled: false,
  canStop: false,
  stopping: false,
})

const emit = defineEmits<{
  close: []
  stop: []
}>()

// 事件最多保留 500 条；抽屉里只渲染最新 100 条，失败的排在前面更容易看到。
const visibleEvents = computed(() => {
  const latest = props.events.slice(-100)
  return [...latest].sort((a, b) => Number(b.status === 'failed') - Number(a.status === 'failed') || b.sequence - a.sequence)
})

function eventLabel(status: AccountOperationEvent['status']): string {
  return { success: '成功', failed: '失败', skipped: '跳过', info: '信息' }[status] || '信息'
}

function eventTone(status: AccountOperationEvent['status']): string {
  return {
    success: 'text-emerald-600',
    failed: 'text-rose-600',
    skipped: 'text-amber-600',
    info: 'text-muted-foreground',
  }[status] || 'text-muted-foreground'
}
</script>
