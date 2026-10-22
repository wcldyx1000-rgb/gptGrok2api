<!-- Monitor detail drawer. Idea ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<template>
  <LogsDetailDrawer
    :log="runtime.selectedLog.value"
    :loading="runtime.detailLoading.value"
    :load-error="runtime.detailError.value"
    :primary-fields="runtime.selectedPrimaryDetailFields.value"
    :diagnostic-fields="runtime.selectedDiagnosticDetailFields.value"
    :timeline-segments="runtime.selectedTimelineSegments.value"
    :timeline-legend-items="runtime.selectedTimelineLegendItems.value"
    :timeline-groups="runtime.selectedTimelineGroups.value"
    :bottleneck-step="runtime.selectedBottleneckStep.value"
    :timeline-step-count="runtime.selectedTimelineStepCount.value"
    :timeline-segment-total="runtime.selectedTimelineSegmentTotal.value"
    :has-timeline="runtime.selectedHasTimeline.value"
    :timeline-details-visible="runtime.timelineDetailsVisible.value"
    :images="[]"
    @close="emit('close')"
    @copy="copyText"
    @image-error="runtime.markPreviewBroken"
    @toggle-timeline-details="runtime.toggleTimelineDetails"
  >
    <template v-if="cancellable" #actions>
      <Button size="sm" variant="outline" root-class="text-rose-600 hover:text-rose-700" :disabled="cancelling" @click="cancelRequest">
        {{ cancelling ? '取消中...' : '取消请求' }}
      </Button>
    </template>
  </LogsDetailDrawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Button } from 'nanocat-ui'
import { monitorApi, type RealtimeMonitorRecord } from '@/api/monitor'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import LogsDetailDrawer from '@/views/logs/LogsDetailDrawer.vue'
import { useLogDetailRuntime } from '@/views/logs/logDetailRuntime'
import { monitorRecordToRow } from '@/views/monitor/monitorDetailAdapter'

const props = defineProps<{
  /** 点击的监控行；为空时关闭抽屉 */
  record: RealtimeMonitorRecord | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'cancelled', callId: string): void
}>()

const toast = useToast()
const confirmDialog = useConfirmDialog()
const cancelling = ref(false)
// 只有仍在进行（running）的请求可以取消；已在取消中或已结束的不显示按钮。
const cancellable = computed(() => String(props.record?.status || '').toLowerCase() === 'running')
const runtime = useLogDetailRuntime({
  // 行内先显示列表里的精简信息，再按 call_id 拉取含阶段事件的完整记录。
  loadDetail: async (item) => {
    const response = await monitorApi.detail(item.id)
    return response?.detail ? monitorRecordToRow(response.detail) : null
  },
})

watch(
  () => props.record,
  (record) => {
    if (record) void runtime.openDetail(monitorRecordToRow(record, { compact: true }))
    else runtime.closeDetail()
  },
  { immediate: true },
)

async function cancelRequest() {
  const record = props.record
  if (!record || cancelling.value) return
  const confirmed = await confirmDialog.ask({
    title: '取消请求',
    message: `确认取消请求 ${record.call_id}？上游已完成的部分不会回滚。`,
    confirmText: '取消请求',
    cancelText: '返回',
  })
  if (!confirmed) return
  cancelling.value = true
  try {
    await monitorApi.cancel(record.call_id)
    toast.success('已发送取消请求')
    emit('cancelled', record.call_id)
  } catch (error) {
    toast.error(errorMessage(error, '取消请求失败，请求可能已经结束'))
  } finally {
    cancelling.value = false
  }
}

async function copyText(value: string) {
  const text = String(value || '').trim()
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    toast.success('已复制')
  } catch {
    toast.error('复制失败')
  }
}
</script>
