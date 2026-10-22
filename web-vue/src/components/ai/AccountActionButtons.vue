<template>
  <div class="flex items-center gap-2" :class="alignClass">
    <Button
      size="xs"
      variant="outline"
      root-class="w-14 justify-center"
      :disabled="item.is_demo || busy"
      @click="emit('edit')"
    >
      编辑
    </Button>
    <FloatingActionMenu
      label="更多"
      :items="menuItems"
      :disabled="item.is_demo || busy"
      align="right"
      size="sm"
      trigger-class="h-7 justify-center px-2 text-[11px]"
      :trigger-width="64"
      @select="handleSelect"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button } from 'nanocat-ui'
import type { ActionMenuItem } from 'nanocat-ui'
import type { Account } from '@/api/accounts'
import { isAutoPaused } from '@/views/accounts/viewUtils'
import FloatingActionMenu from './FloatingActionMenu.vue'
import { actionMenuGroups } from './menuItems'

const props = withDefaults(defineProps<{
  item: Account
  refreshing?: boolean
  resetting?: boolean
  /** 批量任务进行中：整行操作暂时禁用。 */
  busy?: boolean
  align?: 'start' | 'end'
}>(), {
  refreshing: false,
  resetting: false,
  busy: false,
  align: 'start',
})

const emit = defineEmits<{
  (e: 'edit'): void
  (e: 'toggle-enabled'): void
  (e: 'refresh-token'): void
  (e: 'reset-state'): void
  (e: 'clear-auto-pause'): void
  (e: 'copy-final-checkout-link'): void
  (e: 'open-final-checkout-link'): void
  (e: 'remove'): void
  /** 凭据 / 测试 / Codex 绑定等扩展操作，由账号页统一处理。 */
  (e: 'account-action', key: 'view-credentials' | 'test-account' | 'refresh-access-token' | 'bind-codex' | 'egress-switch' | 'egress-cancel'): void
}>()

const alignClass = computed(() => (
  props.align === 'end' ? 'justify-end' : 'justify-start'
))

function finalCheckoutLinkUrl(item: Account): string {
  const finalUrl = String(item.checkout_final_url || '').trim()
  if (finalUrl) return finalUrl
  return String(item.checkout_final_kind || '').trim()
    ? String(item.checkout_url || '').trim()
    : ''
}

const hasFinalCheckoutLink = computed(() => Boolean(finalCheckoutLinkUrl(props.item)))

const menuItems = computed<ActionMenuItem[]>(() => actionMenuGroups(
  [
    {
      key: 'copy-final-checkout-link',
      label: '复制最终支付链接',
      disabled: !hasFinalCheckoutLink.value,
    },
    {
      key: 'open-final-checkout-link',
      label: '打开最终支付链接',
      disabled: !hasFinalCheckoutLink.value,
    },
  ],
  [
    { key: 'view-credentials', label: '查看 / 复制凭据' },
    { key: 'test-account', label: '测试账号（对话 / 画图）' },
    {
      key: 'refresh-access-token',
      label: '仅刷新 Access Token',
      disabled: !props.item.has_refresh_token,
    },
    {
      key: 'bind-codex',
      label: props.item.codex_bound ? '重新绑定 Codex 登录' : '绑定 Codex 登录',
    },
    props.item.egress?.switch_to
      ? { key: 'egress-cancel', label: '取消更换出口' }
      : { key: 'egress-switch', label: '更换出口' },
  ],
  [
    {
      key: 'refresh-token',
      label: props.refreshing ? '刷新中...' : '刷新账号信息和额度',
      disabled: props.refreshing,
    },
    {
      key: 'reset-state',
      label: props.resetting ? '重置中...' : '重置状态',
      disabled: props.resetting,
    },
    ...(isAutoPaused(props.item)
      ? [{ key: 'clear-auto-pause', label: '解除自动暂停' }]
      : []),
  ],
  [
    {
      key: 'toggle-enabled',
      label: props.item.enabled ? '禁用账号' : '启用账号',
    },
  ],
  [
    {
      key: 'remove',
      label: '删除账号',
      danger: true,
    },
  ],
))

function handleSelect(key: string) {
  if (key === 'copy-final-checkout-link') emit('copy-final-checkout-link')
  if (key === 'open-final-checkout-link') emit('open-final-checkout-link')
  if (key === 'toggle-enabled') emit('toggle-enabled')
  if (key === 'refresh-token') emit('refresh-token')
  if (key === 'reset-state') emit('reset-state')
  if (key === 'clear-auto-pause') emit('clear-auto-pause')
  if (key === 'remove') emit('remove')
  if (
    key === 'view-credentials'
    || key === 'test-account'
    || key === 'refresh-access-token'
    || key === 'bind-codex'
    || key === 'egress-switch'
    || key === 'egress-cancel'
  ) emit('account-action', key)
}
</script>
