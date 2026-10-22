<!-- Remote import progress drawer. Idea ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<template>
  <ModalShell
    :open="visible"
    placement="end"
    align="start"
    max-width="26rem"
    :z-index="150"
    :close-on-backdrop="!state.tracking"
    @close="tracker.close"
  >
    <ModalHeader
      :title="state.source === 'cpa' ? '远程 CPA 导入' : 'Sub2API 导入'"
      :subtitle="state.sourceName || undefined"
      :close-disabled="false"
      compact
      @close="tracker.close"
    />
    <div class="space-y-4 px-5 py-4">
      <div class="flex items-center justify-between text-xs text-muted-foreground">
        <span>{{ statusText }}</span>
        <span>{{ job?.completed || 0 }} / {{ job?.total || 0 }} · {{ tracker.percent.value }}%</span>
      </div>
      <ProgressBar :value="tracker.percent.value" aria-label="远程导入进度" />
      <MetricStrip :items="metrics" columns-class="grid-cols-2" density="compact" />
      <SurfaceBox v-if="state.error" tag="p" tone="danger" density="compact">{{ state.error }}</SurfaceBox>
      <ul v-if="job?.errors?.length" class="max-h-48 space-y-1 overflow-auto text-xs text-rose-600">
        <li v-for="(item, index) in job.errors.slice(0, 20)" :key="index" class="truncate" :title="item.error">
          {{ item.name }}：{{ item.error }}
        </li>
        <li v-if="job.errors.length > 20" class="text-muted-foreground">…… 共 {{ job.errors.length }} 条错误</li>
      </ul>
      <p class="text-[11px] text-muted-foreground">
        远程导入全局同一时间只运行一个任务；离开或刷新页面后再回来会自动恢复这里的进度。
      </p>
    </div>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { useRemoteImportTracker } from '@/composables/useRemoteImportTracker'
import MetricStrip from './MetricStrip.vue'
import ModalHeader from './ModalHeader.vue'
import ModalShell from './ModalShell.vue'
import ProgressBar from './ProgressBar.vue'
import SurfaceBox from './SurfaceBox.vue'

const tracker = useRemoteImportTracker()
const { state } = tracker
const owner = Symbol('remote-import-drawer')
const isOwner = ref(false)

onMounted(() => { isOwner.value = tracker.claimDrawer(owner) })
onBeforeUnmount(() => { tracker.releaseDrawer(owner) })

const visible = computed(() => isOwner.value && state.open)
const job = computed(() => state.job)

const statusText = computed(() => {
  const status = job.value?.status
  if (status === 'completed') return '已完成'
  if (status === 'failed') return '已结束（有失败）'
  if (status === 'running') return '导入中'
  return '等待开始'
})

const metrics = computed(() => [
  { key: 'added', label: '新增', value: job.value?.added || 0 },
  { key: 'skipped', label: '跳过', value: job.value?.skipped || 0 },
  { key: 'refreshed', label: '已刷新', value: job.value?.refreshed || 0 },
  { key: 'failed', label: '失败', value: job.value?.failed || 0 },
])
</script>
