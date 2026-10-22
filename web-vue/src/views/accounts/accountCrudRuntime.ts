import { reactive, ref } from 'vue'

import {
  accountDisplayLabel,
  accountsApi,
  normalizeAccountBackendStatus,
  type Account,
  type AccountBackendStatus,
} from '@/api/accounts'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'

export type AccountForm = {
  id: string
  access_token: string
  /** 留空表示不修改已有 refresh token。 */
  refresh_token: string
  type: string
  source_type: string
  group_id: string
  proxy: string
  quota: string
  status: AccountBackendStatus
}

type AccountCrudRuntimeOptions = {
  loadData: (options?: { silentErrorToast?: boolean }) => Promise<void>
  loadAccountGroups: (options?: { silentErrorToast?: boolean }) => Promise<void>
  normalizeErrorMessage: (error: unknown) => string
  setError: (prefix: string, error: unknown, notify?: boolean) => void
}

const accountStatusOptions = [
  { label: '正常', value: '正常' },
  { label: '限流', value: '限流' },
  { label: '异常', value: '异常' },
  { label: '禁用', value: '禁用' },
] as const

function createDefaultForm(): AccountForm {
  return {
    id: '',
    access_token: '',
    refresh_token: '',
    type: 'free',
    source_type: 'web',
    group_id: '',
    proxy: '',
    quota: '',
    status: '正常',
  }
}

function normalizeQuota(value: unknown): number | undefined {
  const raw = String(value ?? '').trim()
  if (!raw) return undefined
  const parsed = Number(raw)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : undefined
}

export function useAccountCrudRuntime(options: AccountCrudRuntimeOptions) {
  const saving = ref(false)
  const showModal = ref(false)
  const editingId = ref<string | null>(null)
  // 逐账号忙态：连点多个账号时每个账号各自保持忙态，互不覆盖。
  const refreshingAccountIds = reactive(new Set<string>())
  const resettingAccountIds = reactive(new Set<string>())
  const form = reactive(createDefaultForm())
  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  let syncProxyControlsFromValue: (value: unknown) => void = () => {}

  function setProxyControlsSync(sync: (value: unknown) => void) {
    syncProxyControlsFromValue = sync
    syncProxyControlsFromValue(form.proxy)
  }

  function resetForm() {
    editingId.value = null
    Object.assign(form, createDefaultForm())
    syncProxyControlsFromValue(form.proxy)
  }

  function openCreateModal() {
    resetForm()
    void options.loadAccountGroups({ silentErrorToast: true })
    showModal.value = true
  }

  function openEditModal(item: Account) {
    editingId.value = item.id
    form.id = item.id
    form.access_token = item.access_token || ''
    form.refresh_token = ''
    form.type = item.type || 'free'
    form.source_type = item.source_type || 'web'
    form.group_id = item.group_id || ''
    form.proxy = item.proxy || ''
    form.quota = item.image_quota_unknown ? '' : String(item.quota ?? '')
    form.status = normalizeAccountBackendStatus(item.backend_status, item.enabled ? '正常' : '禁用')
    syncProxyControlsFromValue(form.proxy)
    void options.loadAccountGroups({ silentErrorToast: true })
    showModal.value = true
  }

  function closeModal() {
    showModal.value = false
    resetForm()
  }

  async function saveAccount() {
    const isEditing = Boolean(editingId.value)
    // 新增必须有 access token；编辑时 AT / RT 留空表示不覆盖已有凭据。
    if (!isEditing && !form.access_token.trim()) {
      toast.warning('Access token 不能为空')
      return
    }

    saving.value = true
    const accountIdForNotice = accountDisplayLabel(editingId.value || form.id || '')

    try {
      const payloadId = editingId.value || form.id || undefined
      await accountsApi.upsert({
        id: payloadId,
        access_token: form.access_token.trim(),
        refresh_token: form.refresh_token.trim(),
        type: form.type.trim() || undefined,
        source_type: form.source_type.trim() || undefined,
        group_id: form.group_id.trim(),
        proxy: form.proxy.trim(),
        quota: normalizeQuota(form.quota),
        backend_status: form.status,
        enabled: form.status !== '禁用',
      })
      toast.success(isEditing ? `账号 ${accountIdForNotice} 已更新` : '账号已添加')
      closeModal()
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      options.setError('保存失败', error)
    } finally {
      saving.value = false
    }
  }

  async function toggleEnabled(item: Account) {
    const nextEnabled = !item.enabled
    const confirmed = await confirmDialog.ask({
      title: nextEnabled ? '确认启用账号' : '确认禁用账号',
      message: `即将${nextEnabled ? '启用' : '禁用'}账号 ${accountDisplayLabel(item.id)}。这会影响该账号是否参与后续请求分配，是否继续？`,
      confirmText: nextEnabled ? '启用' : '禁用',
      cancelText: '取消',
    })
    if (!confirmed) return

    try {
      if (item.enabled) {
        await accountsApi.disable(item.id)
      } else {
        await accountsApi.enable(item.id)
      }
      toast.success(`账号 ${accountDisplayLabel(item.id)} 已${item.enabled ? '禁用' : '启用'}`)
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      options.setError('切换状态失败', error)
    }
  }

  async function refreshToken(accountId: string) {
    const confirmed = await confirmDialog.ask({
      title: '确认刷新账号',
      message: `即将刷新账号 ${accountDisplayLabel(accountId)} 的远端信息和额度，可能触发外部 ChatGPT 请求。是否继续？`,
      confirmText: '开始刷新',
      cancelText: '取消',
    })
    if (!confirmed) return

    refreshingAccountIds.add(accountId)
    toast.info(`正在刷新账号 ${accountDisplayLabel(accountId)} 的远端信息...`)
    try {
      await accountsApi.refreshToken(accountId)
      toast.success(`账号 ${accountDisplayLabel(accountId)} 刷新成功`)
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      toast.error(`账号 ${accountDisplayLabel(accountId)} 刷新失败：${options.normalizeErrorMessage(error)}`)
      await options.loadData({ silentErrorToast: true })
    } finally {
      refreshingAccountIds.delete(accountId)
    }
  }

  async function resetAccountState(accountId: string) {
    const confirmed = await confirmDialog.ask({
      title: '重置账号状态',
      message: `是否重置账号 ${accountDisplayLabel(accountId)} 的配额和冷却？此操作会清空本地计数并移除冷却状态。`,
      confirmText: '确认重置',
      cancelText: '取消',
    })
    if (!confirmed) return

    resettingAccountIds.add(accountId)
    try {
      await accountsApi.resetAccountState(accountId)
      toast.success(`账号 ${accountDisplayLabel(accountId)} 已重置`)
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      toast.error(`账号 ${accountDisplayLabel(accountId)} 重置失败：${options.normalizeErrorMessage(error)}`)
      await options.loadData({ silentErrorToast: true })
    } finally {
      resettingAccountIds.delete(accountId)
    }
  }

  async function clearAutoPause(accountId: string) {
    try {
      const result = await accountsApi.clearAutoPause([accountId])
      if (result.cleared) toast.success(`账号 ${accountDisplayLabel(accountId)} 已解除自动暂停`)
      else toast.error(`账号 ${accountDisplayLabel(accountId)} 不存在或已被移除`)
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      toast.error(`账号 ${accountDisplayLabel(accountId)} 解除暂停失败：${options.normalizeErrorMessage(error)}`)
    }
  }

  async function removeAccount(accountId: string) {
    const confirmed = await confirmDialog.ask({
      title: '删除账号',
      message: `确认删除账号 ${accountDisplayLabel(accountId)} 吗？此操作不可恢复。`,
      confirmText: '确认删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    try {
      await accountsApi.delete(accountId)
      toast.success(`账号 ${accountDisplayLabel(accountId)} 已删除`)
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      options.setError('删除失败', error)
    }
  }

  return {
    saving,
    showModal,
    editingId,
    refreshingAccountIds,
    resettingAccountIds,
    accountStatusOptions,
    form,
    setProxyControlsSync,
    resetForm,
    openCreateModal,
    openEditModal,
    closeModal,
    saveAccount,
    toggleEnabled,
    refreshToken,
    resetAccountState,
    clearAutoPause,
    removeAccount,
  }
}
