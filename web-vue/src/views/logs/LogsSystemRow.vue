<template>
  <tr
    v-memo="[signature]"
    class="border-t border-border transition-colors hover:bg-muted/30"
    :class="{ 'bg-primary/5': selected }"
  >
    <td class="py-4 pl-4 pr-2 align-middle">
      <Checkbox
        :model-value="selected"
        @update:model-value="handleSelect"
      >
        <span class="sr-only">选择日志 {{ item.time || item.id }}</span>
      </Checkbox>
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      <p class="whitespace-nowrap text-foreground">{{ item.time || '-' }}</p>
    </td>
    <td class="py-4 pr-5 align-middle">
      <p
        class="flex min-w-0 items-baseline gap-1.5 truncate text-xs text-foreground"
        :title="[view.requestKind, view.requestPrimary].filter(Boolean).join(' ')"
      >
        <span v-if="view.requestKind" class="shrink-0 text-muted-foreground">{{ view.requestKind }}</span>
        <span class="truncate font-medium">{{ view.requestPrimary }}</span>
      </p>
      <p
        v-if="view.requestSecondary"
        class="mt-1 truncate text-[11px] text-muted-foreground"
        :title="view.requestSecondary"
      >
        {{ view.requestSecondary }}
      </p>
      <p
        v-if="view.requestParameters"
        class="mt-0.5 truncate text-[11px] text-muted-foreground/80"
        :title="view.requestParameters"
      >
        {{ view.requestParameters }}
      </p>
    </td>
    <td class="py-4 pr-5 align-middle">
      <p class="truncate text-xs text-foreground" :title="accountTitle">
        {{ view.executionPrimary }}
      </p>
      <div
        v-if="view.executionSecondary || item.switchCount"
        class="mt-1 flex min-w-0 items-center gap-1.5"
      >
        <span
          v-if="view.executionSecondary"
          class="truncate text-[11px] text-muted-foreground"
          :title="view.executionSecondary"
        >
          {{ view.executionSecondary }}
        </span>
        <MetaChip v-if="item.switchCount" size="xs" tone="warning" chip-class="shrink-0">
          切换 {{ item.switchCount }} 次
        </MetaChip>
      </div>
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      <MetaChip
        v-if="formatDuration(item.durationMs)"
        size="xs"
        :tone="view.durationTone"
        chip-class="font-mono tabular-nums"
      >
        {{ formatDuration(item.durationMs) }}
      </MetaChip>
      <span v-else>-</span>
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      <span class="whitespace-nowrap" :title="item.requestSize ? `请求尺寸 ${item.requestSize}` : ''">
        {{ item.resolution || '-' }}
      </span>
    </td>
    <td class="py-4 pr-5 align-middle">
      <LogImagePreviewCell
        :image-urls="item.imageUrls"
        :first-image-broken="firstImageBroken"
        :alt="item.preview || '日志结果图片'"
        @preview-click="handleOpenDetail"
        @image-error="handleImageError"
      />
    </td>
    <td class="py-4 pr-5 align-middle">
      <div class="min-w-0" :title="summary">
        <div class="flex min-w-0 items-center gap-2">
          <StateBadge :tone="statusTone(item)" shape="rounded" :bordered="false">
            {{ statusLabel(item) }}
          </StateBadge>
          <p
            class="min-w-0 truncate text-xs font-medium text-foreground"
            :class="{ 'text-rose-600': failed }"
            :title="view.resultText"
          >
            {{ view.resultText }}
          </p>
        </div>
        <p
          v-if="view.diagnostics"
          class="mt-1.5 truncate text-[11px] text-muted-foreground"
          :title="view.diagnostics"
        >
          {{ view.diagnostics }}
        </p>
      </div>
    </td>
    <td class="py-4 pr-4 text-right align-middle">
      <div class="flex justify-end gap-1.5">
        <Button size="xs" variant="outline" @click="handleOpenDetail">
          查看详情
        </Button>
        <Button
          size="xs"
          variant="ghost"
          root-class="text-rose-600 hover:text-rose-700"
          @click="handleRequestDelete"
        >
          删除
        </Button>
      </div>
    </td>
  </tr>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button, Checkbox } from 'nanocat-ui'
import LogImagePreviewCell from '@/components/ai/LogImagePreviewCell.vue'
import MetaChip from '@/components/ai/MetaChip.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import {
  formatLogDuration as formatDuration,
  isSystemLogFailed as isFailed,
  type SystemLogRow,
} from '@/api/logs'
import {
  logRowPresentation,
  statusLabel,
  statusTone,
  summaryText,
} from '@/views/logs/logsView'

const props = defineProps<{
  item: SystemLogRow
  signature: string
  selected: boolean
  firstImageBroken: boolean
}>()

const emit = defineEmits<{
  (e: 'toggle-selection', id: string, checked: boolean): void
  (e: 'open-detail', item: SystemLogRow): void
  (e: 'request-delete-log', item: SystemLogRow): void
  (e: 'image-error', url: string): void
}>()

const view = computed(() => logRowPresentation(props.item))
const accountTitle = computed(() => [props.item.accountEmail, props.item.accountId, props.item.keyName || props.item.keyId]
  .filter(Boolean).join(' / '))
const summary = computed(() => summaryText(props.item))
const failed = computed(() => isFailed(props.item))

function handleSelect(checked: boolean | string | number) {
  emit('toggle-selection', props.item.id, Boolean(checked))
}

function handleOpenDetail() {
  emit('open-detail', props.item)
}

function handleRequestDelete() {
  emit('request-delete-log', props.item)
}

function handleImageError(url: string) {
  emit('image-error', url)
}
</script>
