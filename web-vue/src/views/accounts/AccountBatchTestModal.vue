<template>
  <ModalShell :open="open" :z-index="140" max-width="48rem" @close="emit('close')">
    <ModalHeader
      title="批量测试账号"
      :subtitle="mode === 'probe' ? `共 ${rows.length} 个账号 · 逐个做一次远程信息检查，不消耗图片额度` : `共 ${rows.length} 个账号 · 逐个发起真实${mode === 'chat' ? '对话' : '画图'}测试，会消耗额度`"
      :close-disabled="running"
      compact
      @close="emit('close')"
    />

    <ModalBody density="compact" class="space-y-3">
      <div class="flex flex-wrap items-center gap-2" role="tablist" aria-label="测试方式">
        <Button
          v-for="option in modeOptions"
          :key="option.value"
          size="xs"
          :variant="mode === option.value ? 'primary' : 'outline'"
          :disabled="running"
          @click="emit('update:mode', option.value)"
        >
          {{ option.label }}
        </Button>
      </div>
      <div v-if="mode !== 'probe'" class="grid grid-cols-1 gap-2 md:grid-cols-[12rem_1fr]">
        <label class="text-xs">
          <span class="ui-field-label">测试模型</span>
          <GroupedSelectMenu
            v-if="modelOptions?.length"
            :model-value="model"
            :options="modelSelectOptions"
            :disabled="running || modelCatalogLoading"
            aria-label="测试模型"
            selected-indicator="none"
            block
            @update:model-value="emit('update:model', String($event))"
          />
          <Input v-else :model-value="model" block :disabled="running" placeholder="模型 ID" @update:model-value="emit('update:model', $event)" />
        </label>
        <label class="text-xs">
          <span class="ui-field-label">提示词</span>
          <Input :model-value="prompt" block :disabled="running" placeholder="测试提示词" @update:model-value="emit('update:prompt', $event)" />
        </label>
        <p class="text-xs text-amber-600 md:col-span-2">
          真实测试会通过账号自己的 token 发送请求并写入调用日志；画图测试会扣减账号图片额度，批量时逐个串行执行。
        </p>
      </div>
      <div class="batch-test-summary" aria-live="polite">
        <span>已测 <b>{{ finished }}</b>/{{ rows.length }}</span>
        <span class="is-ok">可用 <b>{{ successCount }}</b></span>
        <span class="is-bad">不可用 <b>{{ failedCount }}</b></span>
      </div>

      <div class="batch-test-bar" aria-hidden="true">
        <span :style="{ width: `${rows.length ? Math.round((finished / rows.length) * 100) : 0}%` }" />
      </div>

      <div class="batch-test-table scrollbar-slim">
        <table class="w-full text-xs">
          <thead>
            <tr>
              <th class="w-[40%]">账号</th>
              <th class="w-20">结果</th>
              <th class="w-16 text-right">耗时</th>
              <th>额度 / 原因</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.id"
              :class="{ 'is-clickable': row.content || row.message, 'is-selected': detailRow?.id === row.id }"
              @click="emit('select-row', detailRow?.id === row.id ? '' : row.id)"
            >
              <td class="truncate font-medium text-foreground" :title="row.label">{{ row.label }}</td>
              <td>
                <span class="batch-test-state" :class="`is-${row.status}`">{{ statusLabel[row.status] }}</span>
              </td>
              <td class="text-right tabular-nums text-muted-foreground">
                {{ row.durationMs ? formatDuration(row.durationMs) : '—' }}
              </td>
              <td class="truncate text-muted-foreground" :class="{ 'text-rose-600': row.status === 'failed' }" :title="row.message">
                {{ row.message || '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="detailRow" class="batch-test-detail scrollbar-slim">
        <p class="mb-1 text-xs font-medium text-foreground">{{ detailRow.label }} · 测试结果</p>
        <p v-if="detailRow.quotaText" class="mb-2 text-xs text-muted-foreground">{{ detailRow.quotaText }}</p>
        <template v-for="(part, index) in detailParts" :key="index">
          <img v-if="part.image" :src="part.image" alt="测试生成的图片" class="mb-2 max-h-64 rounded-md border border-border" />
          <p v-else class="whitespace-pre-wrap break-words text-xs text-foreground">{{ part.text }}</p>
        </template>
        <p v-if="!detailParts.length" class="text-xs text-muted-foreground">{{ detailRow.message || '没有返回内容' }}</p>
      </div>
    </ModalBody>

    <ModalFooter compact>
      <Button size="sm" variant="outline" :disabled="running" @click="emit('close')">关闭</Button>
      <Button v-if="running" size="sm" variant="outline" :disabled="stopRequested" @click="emit('stop')">
        {{ stopRequested ? '停止中...' : '停止' }}
      </Button>
      <Button v-else size="sm" variant="primary" :disabled="!rows.length" @click="emit('run')">
        {{ finished ? '重新测试' : '开始测试' }}
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button, Input } from 'nanocat-ui'
import type { AccountTestMode } from '@/api/accounts'

import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import type { AccountBatchTestRow } from './accountBatchTestRuntime'

const props = defineProps<{
  open: boolean
  rows: AccountBatchTestRow[]
  mode: AccountTestMode
  model: string
  prompt: string
  detailRow: AccountBatchTestRow | null
  modelOptions?: string[]
  modelCatalogLoading?: boolean
  running: boolean
  stopRequested: boolean
  finished: number
  successCount: number
  failedCount: number
}>()

const emit = defineEmits<{
  close: []
  run: []
  stop: []
  'update:mode': [value: AccountTestMode]
  'update:model': [value: string]
  'update:prompt': [value: string]
  'select-row': [id: string]
}>()

const modeOptions: Array<{ label: string; value: AccountTestMode }> = [
  { label: '可用性探测', value: 'probe' },
  { label: '对话测试', value: 'chat' },
  { label: '画图测试', value: 'image' },
]

// 内容里的 Markdown 图片拆成 <img>，其余按文本显示（不使用 v-html）。
const detailParts = computed(() => {
  const content = props.detailRow?.content || ''
  const parts: Array<{ text?: string; image?: string }> = []
  const pattern = /!\[[^\]]*\]\(([^)\s]+)\)/g
  let last = 0
  for (const match of content.matchAll(pattern)) {
    const before = content.slice(last, match.index).trim()
    if (before) parts.push({ text: before })
    const src = match[1] || ''
    if (/^(https?:\/\/|\/|data:image\/)/i.test(src)) parts.push({ image: src })
    last = (match.index || 0) + match[0].length
  }
  const rest = content.slice(last).trim()
  if (rest) parts.push({ text: rest })
  return parts
})

const statusLabel: Record<AccountBatchTestRow['status'], string> = {
  pending: '等待',
  running: '测试中',
  success: '可用',
  failed: '不可用',
}

function formatDuration(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

// 当前值不在目录里（如默认的 auto）时也要能显示出来。
const modelSelectOptions = computed(() => {
  const values = [...(props.modelOptions || [])]
  if (props.model && !values.includes(props.model)) values.unshift(props.model)
  return values.map((value) => ({ label: value, value }))
})
</script>

<style scoped>
.batch-test-summary {
  display: flex;
  gap: 14px;
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}
.batch-test-summary b {
  color: hsl(var(--foreground));
  font-variant-numeric: tabular-nums;
}
.batch-test-summary .is-ok b { color: rgb(5 150 105); }
.batch-test-summary .is-bad b { color: rgb(225 29 72); }

.batch-test-bar {
  height: 4px;
  border-radius: 999px;
  background: hsl(var(--muted));
  overflow: hidden;
}
.batch-test-bar span {
  display: block;
  height: 100%;
  background: hsl(var(--primary));
  transition: width 0.2s ease;
}

.batch-test-table {
  max-height: min(26rem, 55dvh);
  overflow: auto;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
}
.batch-test-table table {
  table-layout: fixed;
  border-collapse: collapse;
}
.batch-test-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 7px 10px;
  text-align: left;
  font-weight: 500;
  color: hsl(var(--muted-foreground));
  background: hsl(var(--card));
  border-bottom: 1px solid hsl(var(--border));
}
.batch-test-table td {
  padding: 7px 10px;
  border-bottom: 1px solid hsl(var(--border) / 0.6);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.batch-test-table tr:last-child td { border-bottom: 0; }
.batch-test-table tr.is-clickable { cursor: pointer; }
.batch-test-table tr.is-selected td { background: hsl(var(--primary) / 0.06); }
.batch-test-detail {
  max-height: 16rem;
  overflow: auto;
  padding: 10px 12px;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--muted) / 0.3);
}

.batch-test-state {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-weight: 600;
  background: hsl(var(--muted));
  color: hsl(var(--muted-foreground));
}
.batch-test-state.is-running { background: rgb(59 130 246 / 0.12); color: rgb(37 99 235); }
.batch-test-state.is-success { background: rgb(16 185 129 / 0.12); color: rgb(5 150 105); }
.batch-test-state.is-failed { background: rgb(244 63 94 / 0.12); color: rgb(225 29 72); }
</style>
