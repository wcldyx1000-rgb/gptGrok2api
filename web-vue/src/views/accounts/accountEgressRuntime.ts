// Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
// 账号换出口：对接 services/egress_slots.py 的 request_switch / cancel_switch（槽位 / 直连 / 默认出口）。
import { computed, ref } from 'vue'

import { accountsApi } from '@/api/accounts'
import type { Account } from '@/api/accounts'
import { egressApi } from '@/api/egress'
import type { EgressAccountsOverview, EgressSlotsSnapshot } from '@/api/egress'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

type AccountEgressRuntimeOptions = {
  selectedIds: { value: string[] }
  loadData: (options?: { silentErrorToast?: boolean }) => Promise<void>
}

export type EgressTargetOption = {
  value: string
  label: string
  detail: string
  disabled: boolean
}

export function useAccountEgressRuntime(options: AccountEgressRuntimeOptions) {
  const toast = useToast()

  function reportError(prefix: string, err: unknown) {
    error.value = `${prefix}：${errorMessage(err)}`
    toast.error(error.value)
  }
  const confirmDialog = useConfirmDialog()

  const overview = ref<EgressAccountsOverview | null>(null)
  const snapshot = ref<EgressSlotsSnapshot | null>(null)
  const loading = ref(false)
  const opened = ref(false)
  const target = ref('')
  const submitting = ref(false)
  const error = ref('')
  const accountsRef = ref<Account[]>([])
  // 跨分页选中、但不在当前页的账号（只知道 ID）：换出口 / 取消换出口仍然对它们生效
  const offPageIds = ref<Set<string>>(new Set())

  const count = computed(() => accountsRef.value.length)
  const hours = computed(() => snapshot.value?.settings.switch_hold_hours ?? 24)
  const accountLabel = computed(() => (
    accountsRef.value.length === 1 ? (accountsRef.value[0].email || accountsRef.value[0].id) : ''
  ))

  const targetOptions = computed<EgressTargetOption[]>(() => {
    const data = overview.value
    if (!data) return []
    const nodeBySlot = new Map((snapshot.value?.slots || []).map((slot) => [slot.slot, slot.node]))
    const rows: EgressTargetOption[] = data.slots.map((slot) => {
      const full = slot.accounts + count.value > slot.capacity
      return {
        value: `slot:${slot.slot}`,
        label: `槽位 ${slot.slot}`,
        detail: [
          nodeBySlot.get(slot.slot) || '未选节点',
          `${slot.accounts}/${slot.capacity}`,
          slot.down ? '不可用' : '',
          full ? '容量不够' : '',
        ].filter(Boolean).join(' · '),
        disabled: slot.down,
      }
    })
    rows.push({ value: 'direct', label: '直连', detail: `本机网络 · ${data.direct_accounts} 个号`, disabled: false })
    rows.push({ value: 'default', label: '默认出口', detail: `沿用全局代理设置 · ${data.default_accounts} 个号`, disabled: false })
    return rows
  })

  async function loadOverview() {
    loading.value = true
    error.value = ''
    try {
      const [accountsOverview, slots] = await Promise.all([egressApi.accountsOverview(), egressApi.slots()])
      overview.value = accountsOverview
      snapshot.value = slots
    } catch (err) {
      reportError('读取出口槽位失败', err)
    } finally {
      loading.value = false
    }
  }

  function resolveAccounts(item: Account | undefined, all: Account[]): Account[] {
    offPageIds.value = new Set()
    if (item) return [item]
    const selected = new Set(options.selectedIds.value)
    const loaded = all.filter((account) => selected.has(account.id) && !account.is_demo)
    const loadedIds = new Set(all.map((account) => account.id))
    const offPage = options.selectedIds.value.filter((id) => id && !loadedIds.has(id))
    offPageIds.value = new Set(offPage)
    return [...loaded, ...offPage.map((id) => ({ id } as Account))]
  }

  async function openSwitch(all: Account[], item?: Account) {
    accountsRef.value = resolveAccounts(item, all)
    if (!accountsRef.value.length) {
      toast.warning('请先选择账号')
      return
    }
    target.value = ''
    opened.value = true
    await loadOverview()
  }

  function closeSwitch() {
    if (submitting.value) return
    opened.value = false
  }

  async function submitSwitch() {
    if (!target.value || submitting.value) return
    submitting.value = true
    error.value = ''
    try {
      const result = await accountsApi.switchEgress(accountsRef.value.map((item) => item.id), target.value)
      const skipped = result.skipped.length
      const changed = result.scheduled.length + result.applied.length
      if (result.applied.length && !result.scheduled.length) {
        toast.success(`${result.applied.length} 个账号已切到新出口${skipped ? `；跳过 ${skipped} 个` : ''}`)
      } else if (changed) {
        toast.success(`${result.scheduled.length} 个账号已停用，${hours.value} 小时后切到新出口${skipped ? `；跳过 ${skipped} 个` : ''}`)
      } else {
        toast.warning(`没有账号需要更换：${result.skipped[0]?.reason || '都已是这个出口'}`)
      }
      opened.value = false
      await options.loadData({ silentErrorToast: true })
    } catch (err) {
      reportError('更换出口失败', err)
    } finally {
      submitting.value = false
    }
  }

  async function cancelSwitch(all: Account[], item?: Account) {
    // 不在当前页的账号不知道有没有在换出口，一并提交（后端对没在换的号是空操作）
    const targets = resolveAccounts(item, all).filter(
      (account) => offPageIds.value.has(account.id) || account.egress?.switch_to,
    )
    if (!targets.length) {
      toast.warning(item ? '这个账号没有在换出口' : '选中的账号没有在换出口')
      return
    }
    const confirmed = await confirmDialog.ask({
      title: '取消更换出口',
      message: `取消后 ${targets.length === 1 ? (targets[0].email || targets[0].id) : `${targets.length} 个账号`}恢复原出口并接单（IP 没变，不用再等）。`,
      confirmText: '取消更换',
      cancelText: '不取消',
    })
    if (!confirmed) return
    try {
      const result = await accountsApi.cancelEgressSwitch(targets.map((account) => account.id))
      toast.success(result.cancelled.length ? `已取消 ${result.cancelled.length} 个账号的换出口` : '选中的账号没有在换出口')
      await options.loadData({ silentErrorToast: true })
    } catch (err) {
      reportError('取消更换出口失败', err)
    }
  }

  return {
    opened,
    target,
    submitting,
    loading,
    error,
    count,
    hours,
    accountLabel,
    targetOptions,
    openSwitch,
    closeSwitch,
    submitSwitch,
    cancelSwitch,
  }
}
