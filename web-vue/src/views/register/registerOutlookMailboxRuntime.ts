// Outlook 邮箱池逐邮箱管理视图的数据层。
// Ported from chatgpt-register (AGPL-3.0-only).
import { computed, onBeforeUnmount, ref, type Ref } from 'vue'
import { registerApi, type OutlookPoolEntry } from '@/api/register'

type ConfirmOptions = {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
}

export type OutlookMailboxRuntimeInput = {
  providerId: Ref<string>
  confirm: (options: ConfirmOptions) => Promise<boolean>
  notifySuccess: (message: string) => void
  notifyError: (message: string) => void
  /** 重置/删除成功后通知上层刷新配置里的统计 */
  onChanged?: () => void
}

export const OUTLOOK_STATE_FILTERS: Array<{ key: string; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'unused', label: '可用' },
  { key: 'leased', label: '租用中' },
  { key: 'used', label: '已用' },
  { key: 'failed', label: '失败' },
  { key: 'family_used', label: '家族已用' },
  { key: 'login_required', label: '需登录' },
  { key: 'token_invalid', label: '令牌失效' },
]

const STATE_LABELS: Record<string, string> = Object.fromEntries(
  OUTLOOK_STATE_FILTERS.filter(item => item.key !== 'all').map(item => [item.key, item.label]),
)

const STATE_TONES: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'muted'> = {
  unused: 'success',
  leased: 'info',
  used: 'muted',
  failed: 'warning',
  family_used: 'muted',
  login_required: 'danger',
  token_invalid: 'danger',
}

export function outlookStateLabel(state: string) {
  return STATE_LABELS[state] || state || '未知'
}

export function outlookStateTone(state: string) {
  return STATE_TONES[state] || 'muted'
}

export function formatOutlookTime(value: string) {
  const text = String(value || '').trim()
  if (!text) return '—'
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function useRegisterOutlookMailboxes(input: OutlookMailboxRuntimeInput) {
  const items = ref<OutlookPoolEntry[]>([])
  const total = ref(0)
  const page = ref(1)
  const pages = ref(1)
  const pageSize = ref(20)
  const stateCounts = ref<Record<string, number>>({})
  const stats = ref<Record<string, number>>({})
  const query = ref('')
  const stateFilter = ref('all')
  const loading = ref(false)
  const loaded = ref(false)
  const errorMessage = ref('')
  const busyId = ref('')
  let seq = 0
  let queryTimer: ReturnType<typeof setTimeout> | undefined

  const empty = computed(() => loaded.value && total.value === 0)

  async function load() {
    const providerId = String(input.providerId.value || '').trim()
    if (!providerId) return
    const current = ++seq
    loading.value = true
    try {
      const response = await registerApi.listOutlookPoolEntries({
        provider_id: providerId,
        q: query.value.trim() || undefined,
        state: stateFilter.value,
        page: page.value,
        page_size: pageSize.value,
      })
      if (current !== seq) return
      items.value = Array.isArray(response.items) ? response.items : []
      total.value = Number(response.total) || 0
      page.value = Number(response.page) || 1
      pages.value = Math.max(1, Number(response.pages) || 1)
      stateCounts.value = response.state_counts || {}
      stats.value = response.stats || {}
      errorMessage.value = ''
      loaded.value = true
    } catch (error: any) {
      if (current !== seq) return
      errorMessage.value = error?.message || '加载邮箱池失败'
    } finally {
      if (current === seq) loading.value = false
    }
  }

  function setQuery(value: string) {
    query.value = value
    if (queryTimer !== undefined) clearTimeout(queryTimer)
    queryTimer = setTimeout(() => {
      queryTimer = undefined
      page.value = 1
      void load()
    }, 300)
  }

  function setState(value: string) {
    stateFilter.value = value
    page.value = 1
    void load()
  }

  function setPage(value: number) {
    const next = Math.min(Math.max(1, value), pages.value)
    if (next === page.value) return
    page.value = next
    void load()
  }

  async function resetMailbox(entry: OutlookPoolEntry) {
    if (!entry.resettable || busyId.value) return
    const ok = await input.confirm({
      title: '重置为可用',
      message: entry.state === 'used'
        ? `${entry.email} 已成功注册过账号，重置后会再次被领用，可能重复注册同一邮箱。确认重置吗？`
        : `将 ${entry.email} 重置为可用状态，下次注册可再次领用。`,
      confirmText: '重置',
    })
    if (!ok) return
    busyId.value = entry.id
    try {
      await registerApi.resetOutlookMailbox(input.providerId.value, entry.id)
      input.notifySuccess('邮箱已重置为可用')
      input.onChanged?.()
      await load()
    } catch (error: any) {
      input.notifyError(error?.message || '重置邮箱失败')
      await load()
    } finally {
      busyId.value = ''
    }
  }

  async function deleteMailbox(entry: OutlookPoolEntry) {
    if (!entry.deletable || busyId.value) return
    const ok = await input.confirm({
      title: '从邮箱池删除',
      message: `将从邮箱池永久删除 ${entry.email} 及其加号别名的凭据材料。已成功使用的记录会保留，避免重新导入后重复使用。`,
      confirmText: '删除',
    })
    if (!ok) return
    busyId.value = entry.id
    try {
      await registerApi.deleteOutlookMailbox(input.providerId.value, entry.id)
      input.notifySuccess('邮箱已删除')
      input.onChanged?.()
      await load()
    } catch (error: any) {
      input.notifyError(error?.message || '删除邮箱失败')
      await load()
    } finally {
      busyId.value = ''
    }
  }

  onBeforeUnmount(() => {
    if (queryTimer !== undefined) clearTimeout(queryTimer)
    seq += 1
  })

  return {
    items,
    total,
    page,
    pages,
    pageSize,
    stateCounts,
    stats,
    query,
    stateFilter,
    loading,
    loaded,
    empty,
    errorMessage,
    busyId,
    load,
    setQuery,
    setState,
    setPage,
    resetMailbox,
    deleteMailbox,
  }
}
