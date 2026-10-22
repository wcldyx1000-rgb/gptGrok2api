<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). 数据来自 services/account_view.egress_view。 -->
<template>
  <div class="account-cell" :title="tooltip">
    <span class="account-cell-main egress-main" :class="{ 'is-broken': egress && !egress.usable }">
      <span class="egress-dot" :class="`is-${egress?.mode || 'default'}`" aria-hidden="true" />
      {{ egress?.label || '默认出口' }}
    </span>
    <span v-if="egress?.switch_to" class="account-cell-sub is-warning">
      → {{ egress.switch_to_label }}<template v-if="egress.switch_at"> · {{ remainingText(egress.switch_at, now).replace('剩 ', '') }}后</template>
    </span>
    <span v-else class="account-cell-sub">{{ egress?.source_label || '默认出口' }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { Account } from '@/api/accounts'
import { useNowTicker } from '@/composables/useNowTicker'
import { remainingText, shortTime } from './viewUtils'
import './accountCells.css'

const props = defineProps<{ item: Account }>()
const now = useNowTicker()
const egress = computed(() => props.item.egress || null)

const tooltip = computed(() => {
  const value = egress.value
  if (!value) return ''
  const lines = [`实际出口：${value.label}`, `来自：${value.source_label}`]
  if (value.switch_to) {
    lines.push(`正在换到 ${value.switch_to_label}：停用中，${value.switch_at ? `${shortTime(value.switch_at, now.value)} 生效` : '到点生效'}`)
  }
  if (value.source !== 'account') lines.push('账号没单独指定代理，按“账号 → 账号组 → 默认出口”顺序回退')
  if (!value.usable) lines.push('这个出口当前不可用（槽位没有选节点）')
  return lines.join('\n')
})
</script>

<style scoped>
.egress-main {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 11rem;
}
.egress-main.is-broken { color: rgb(225 29 72); }
.egress-dot {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 2px;
  background: hsl(var(--muted-foreground) / 0.6);
}
.egress-dot.is-slot { background: rgb(5 150 105); }
.egress-dot.is-group { background: rgb(124 58 237); }
.egress-dot.is-custom,
.egress-dot.is-profile { background: rgb(37 99 235); }
.egress-dot.is-direct { background: rgb(100 116 139); }
.egress-dot.is-default { background: hsl(var(--muted-foreground) / 0.45); }
</style>
