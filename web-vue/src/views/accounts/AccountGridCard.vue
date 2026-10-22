<template>
  <article
    class="ui-card flex h-full flex-col gap-4 transition-all"
    :class="[rowClass(item), selected ? 'ring-2 ring-primary/30' : 'hover:border-primary/30']"
    v-memo="[signature, detailSignature, selected, refreshing, resetting, busy, lastUsedKey]"
  >
    <div class="flex items-start justify-between gap-3">
      <div class="flex min-w-0 items-start gap-3">
        <Checkbox
          :model-value="selected"
          :disabled="item.is_demo"
          @update:model-value="emit('toggle-select', item.id, $event)"
        />
        <div class="min-w-0">
          <h3 class="truncate text-sm font-medium text-foreground">{{ accountPrimaryText(item) }}</h3>
          <p class="mt-1 truncate font-mono text-xs text-muted-foreground">{{ accountSecondaryText(item) }}</p>
        </div>
      </div>
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
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <StatusPill
        :label="accountSourceText(item)"
        tone-class="border-cyan-500/40 bg-cyan-500/10 text-cyan-600"
      />
      <AccountCredentialStatus
        :item="item"
        @copy-credential="(kind) => emit('copy-credential', item, kind)"
      />
      <StatusDetailPill
        v-if="item.scheduling"
        :label="accountScheduleText(item)"
        :tone-class="accountScheduleClass(item)"
        title="调度状态"
        detail-label="调度说明"
        :card-class="statusDetailCardClass"
        :detail="accountScheduleDetail(item)"
      />
      <StatusPill
        v-if="accountPoolStateText(item)"
        :label="accountPoolStateText(item)"
        tone-class="border-amber-500/40 bg-amber-500/10 text-amber-600"
      />
    </div>

    <KeyValueList
      :items="accountDetailItems(item)"
      :columns="2"
    />

    <AccountActionButtons
      class="mt-auto"
      :item="item"
      :refreshing="refreshing"
      :resetting="resetting"
      :busy="busy"
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
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useNowTicker } from '@/composables/useNowTicker'
import { Checkbox, KeyValueList, StatusDetailPill, StatusPill } from 'nanocat-ui'

import AccountActionButtons from '@/components/ai/AccountActionButtons.vue'
import type { Account } from '@/api/accounts'
import AccountCredentialStatus from './AccountCredentialStatus.vue'
import './accountCells.css'
import {
  accountDetailItems,
  accountPoolStateText,
  accountPrimaryText,
  accountScheduleClass,
  accountScheduleDetail,
  accountScheduleText,
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
// 卡片详情里的「最近使用」随时钟变化，用分钟桶参与 v-memo。
const lastUsedKey = computed(() => (props.item.last_used_at ? Math.floor(now.value / 60) : 0))
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
