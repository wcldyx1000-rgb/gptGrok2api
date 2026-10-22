<template>
  <tr
    class="border-t border-border transition-colors"
    :class="[rowClass(item), selected ? 'bg-primary/5' : '']"
    v-memo="[signature, detailSignature, selected, refreshing, resetting, busy, lastUsedText]"
  >
    <td class="py-4 pr-4 align-middle">
      <Checkbox
        :model-value="selected"
        :disabled="item.is_demo"
        @update:model-value="emit('toggle-select', item.id, $event)"
      />
    </td>
    <td class="py-4 pr-5 align-middle">
      <div class="account-cell">
        <span class="account-cell-main max-w-[16rem]" :title="accountPrimaryText(item)">{{ accountPrimaryText(item) }}</span>
        <span class="account-cell-sub max-w-[16rem]" :title="accountSecondaryText(item)">
          {{ accountSourceText(item) }}<template v-if="item.group_name"> · {{ item.group_name }}</template>
        </span>
      </div>
    </td>
    <td class="py-4 pr-5 align-middle">
      <StatusDetailPill
        :label="statusText(item)"
        :tone-class="`${statusClass(item)} border-border`"
        title="状态详情"
        detail-label="状态说明"
        raw-error-label="原始报错"
        :card-class="statusDetailCardClass"
        :detail="statusDetailText(item)"
        :raw-error="statusRawError(item)"
      />
      <p v-if="accountPoolStateText(item)" class="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
        {{ accountPoolStateText(item) }}
      </p>
    </td>
    <td class="py-4 pr-5 align-middle">
      <StatusDetailPill
        v-if="item.health_level"
        :label="accountHealthText(item)"
        :tone-class="accountHealthClass(item)"
        title="健康度"
        detail-label="判断依据"
        :card-class="statusDetailCardClass"
        :detail="accountHealthDetail(item)"
      />
      <span v-else class="text-xs text-muted-foreground">—</span>
    </td>
    <td class="py-4 pr-5 align-middle">
      <StatusDetailPill
        v-if="item.scheduling"
        :label="accountScheduleText(item)"
        :tone-class="accountScheduleClass(item)"
        title="调度状态"
        detail-label="调度说明"
        :card-class="statusDetailCardClass"
        :detail="accountScheduleDetail(item)"
      />
      <span v-else class="text-xs text-muted-foreground">—</span>
    </td>
    <td class="py-4 pr-5 align-middle">
      <AccountCredentialStatus
        :item="item"
        @copy-credential="(kind) => emit('copy-credential', item, kind)"
      />
    </td>
    <td class="py-4 pr-5 align-middle">
      <AccountEgressCell :item="item" />
    </td>
    <td class="py-4 pr-5 align-middle">
      <AccountQuotaCell :item="item" />
    </td>
    <td class="py-4 pr-5 align-middle">
      <div class="account-cell" :title="statsTooltip">
        <span class="font-mono text-sm tabular-nums">
          <span class="text-emerald-600">{{ item.success_count || 0 }}</span>
          <span class="mx-1 text-muted-foreground/60">/</span>
          <span class="text-rose-600">{{ item.failure_count || 0 }}</span>
        </span>
        <span class="account-cell-sub" :class="accountFailureRateTone(item)">{{ accountFailureRateText(item) }}</span>
      </div>
    </td>
    <td class="py-4 pr-5 align-middle">
      <div class="account-cell" :title="`创建：${accountCreatedText(item)}
最近使用：${formatAccountDate(item.last_used_at)}`">
        <span class="account-cell-main text-[12.5px] font-normal" :class="{ 'text-muted-foreground': idle }">{{ lastUsedText }}</span>
        <span class="account-cell-sub">建于 {{ accountCreatedText(item) }}</span>
      </div>
    </td>
    <td class="py-4 text-right align-middle">
      <AccountActionButtons
        :item="item"
        :refreshing="refreshing"
        :resetting="resetting"
        :busy="busy"
        align="end"
        @edit="emit('edit', item)"
        @toggle-enabled="emit('toggle-enabled', item)"
        @refresh-token="emit('refresh-token', item.id)"
        @reset-state="emit('reset-state', item.id)"
        @clear-auto-pause="emit('clear-auto-pause', item.id)"
        @copy-final-checkout-link="emit('copy-final-checkout-link', item)"
        @open-final-checkout-link="emit('open-final-checkout-link', item)"
        @remove="emit('remove', item.id)"
        @account-action="(key) => emit('account-action', key, item)"
      />
    </td>
  </tr>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useNowTicker } from '@/composables/useNowTicker'
import { Checkbox, StatusDetailPill } from 'nanocat-ui'

import AccountActionButtons from '@/components/ai/AccountActionButtons.vue'
import type { Account } from '@/api/accounts'
import AccountCredentialStatus from './AccountCredentialStatus.vue'
import AccountEgressCell from './AccountEgressCell.vue'
import AccountQuotaCell from './AccountQuotaCell.vue'
import './accountCells.css'
import {
  accountCreatedText,
  accountFailureRateText,
  accountFailureRateTone,
  accountIdleOverDay,
  accountLastUsedText,
  formatAccountDate,
  accountHealthClass,
  accountHealthDetail,
  accountHealthText,
  accountPoolStateText,
  accountScheduleClass,
  accountScheduleDetail,
  accountScheduleText,
  accountPrimaryText,
  accountRowSignature,
  accountSecondaryText,
  accountSourceText,
  boundedSignatureText,
  rowClass,
  statusClass,
  statusRawError,
  statusText,
} from './viewUtils'

const props = withDefaults(defineProps<{
  item: Account
  selected: boolean
  refreshing?: boolean
  resetting?: boolean
  busy?: boolean
  statusDetailCardClass?: string
  statusDetailText: (item: Account) => string
}>(), {
  refreshing: false,
  resetting: false,
  busy: false,
  statusDetailCardClass: '',
})

const now = useNowTicker()
const lastUsedText = computed(() => accountLastUsedText(props.item, now.value))
const idle = computed(() => accountIdleOverDay(props.item, now.value))
const statsTooltip = computed(() => (
  `成功 ${props.item.success_count || 0} 次，失败 ${props.item.failure_count || 0} 次（累计）
满 5 次才计算失败率；≥20% 标黄，≥40% 标红`
))
const signature = computed(() => accountRowSignature(props.item))
const detailSignature = computed(() => boundedSignatureText(props.statusDetailText(props.item)))

const emit = defineEmits<{
  (e: 'toggle-select', id: string, checked: unknown): void
  (e: 'copy-credential', item: Account, kind: 'access' | 'refresh'): void
  (e: 'edit', item: Account): void
  (e: 'toggle-enabled', item: Account): void
  (e: 'refresh-token', id: string): void
  (e: 'reset-state', id: string): void
  (e: 'clear-auto-pause', id: string): void
  (e: 'copy-final-checkout-link', item: Account): void
  (e: 'open-final-checkout-link', item: Account): void
  (e: 'remove', id: string): void
  (e: 'account-action', key: 'view-credentials' | 'test-account' | 'refresh-access-token' | 'bind-codex' | 'egress-switch' | 'egress-cancel', item: Account): void
}>()
</script>
