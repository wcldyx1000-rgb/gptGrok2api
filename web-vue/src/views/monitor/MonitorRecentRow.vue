<template>
  <tr v-memo="[signature]" class="monitor-clickable-row" @click="emit('open-detail', row)">
    <td>
      <p class="font-mono text-xs text-foreground">{{ shortCallId(row.call_id) }}</p>
      <p class="mt-1 text-[11px] text-muted-foreground">{{ row.ended_at || row.updated_at || '-' }}</p>
    </td>
    <td>
      <StateBadge :tone="statusTone(row)" shape="rounded" :bordered="false">
        {{ statusLabel(row) }}
      </StateBadge>
      <p v-if="row.switch_count" class="mt-1 text-[11px] text-amber-600">切换 {{ row.switch_count }} 次</p>
    </td>
    <td class="max-w-[12rem] truncate">{{ row.model || '-' }}</td>
    <td>{{ formatMs(row.duration_ms) }}</td>
    <td>{{ formatMs(metricValue(row, 'handler_queue_ms')) }}</td>
    <td>{{ accountEgressDigest(row) }}</td>
  </tr>
</template>

<script setup lang="ts">
import type { RealtimeMonitorRecord } from '@/api/monitor'
import StateBadge from '@/components/ai/StateBadge.vue'
import {
  accountEgressDigest,
  formatMs,
  metricValue,
  shortCallId,
  statusLabel,
  statusTone,
} from '@/views/monitor/monitorView'

defineProps<{
  row: RealtimeMonitorRecord
  signature: string
}>()

const emit = defineEmits<{
  (e: 'open-detail', row: RealtimeMonitorRecord): void
}>()
</script>
