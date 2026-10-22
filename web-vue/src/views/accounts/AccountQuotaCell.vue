<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). 合并了原“图片额度”和“恢复时间”两列。 -->
<template>
  <div class="account-cell" :title="tooltip">
    <span class="quota-chip" :class="`is-${tone}`">
      <span v-if="estimated" class="quota-approx" aria-label="估算">≈</span>{{ label }}
    </span>
    <span class="account-cell-sub">{{ resetLine }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { Account } from '@/api/accounts'
import { useNowTicker } from '@/composables/useNowTicker'
import { accountQuotaText, quotaTooltip, remainingText, shortTime } from './viewUtils'
import './accountCells.css'

const props = defineProps<{ item: Account }>()
const now = useNowTicker()

const estimated = computed(() => props.item.quota_source === 'estimated')
const label = computed(() => props.item.quota_label || accountQuotaText(props.item))
const tone = computed(() => {
  if (props.item.quota_state === 'exhausted') return 'danger'
  if (props.item.quota_state === 'unknown') return 'muted'
  if (!props.item.quota_state) {
    if (props.item.image_quota_unknown) return 'muted'
    return Number(props.item.quota || 0) <= 0 ? 'danger' : 'ok'
  }
  return estimated.value ? 'estimated' : 'ok'
})
const resetAt = computed(() => props.item.quota_reset_at ?? props.item.restore_at ?? null)

// 上游对还没用过的额度每次都回“查询时刻 + 24h”，这时显示倒计时没有意义
const resetLine = computed(() => {
  if (props.item.quota_state === 'unlimited') return '不限'
  if (props.item.quota_reset_idle) return '未开始计时'
  const reset = resetAt.value
  if (!reset) return '重置时间未知'
  if (reset <= now.value) return '已过重置点'
  return `${remainingText(reset, now.value).replace('剩 ', '')}后重置`
})

const tooltip = computed(() => {
  const lines = [quotaTooltip(props.item, now.value)]
  const reset = resetAt.value
  if (props.item.quota_reset_idle) {
    lines.push('重置：额度还没开始消耗，上游每次查询都返回“查询时刻 + 24 小时”，出过图再同步才有真实重置时间')
  } else if (reset) {
    lines.push(`重置：${shortTime(reset, now.value)}${reset > now.value ? `（${remainingText(reset, now.value)}）` : '（已过，下次请求时确认）'}`)
  }
  return lines.join('\n')
})
</script>

<style scoped>
.quota-chip {
  display: inline-flex;
  min-width: 2.6rem;
  align-items: center;
  justify-content: center;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}
.quota-approx {
  margin-right: 2px;
  font-weight: 500;
  opacity: 0.8;
}
.is-ok { color: rgb(5 150 105); background: rgb(16 185 129 / 0.12); }
.is-estimated { color: rgb(180 83 9); background: rgb(245 158 11 / 0.14); }
.is-danger { color: rgb(225 29 72); background: rgb(244 63 94 / 0.12); }
.is-muted { color: hsl(var(--muted-foreground)); background: hsl(var(--muted)); }
:global(html[data-theme="dark"]) .is-ok { color: rgb(52 211 153); }
:global(html[data-theme="dark"]) .is-estimated { color: rgb(251 191 36); }
:global(html[data-theme="dark"]) .is-danger { color: rgb(251 113 133); }
</style>
