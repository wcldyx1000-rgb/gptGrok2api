import { computed, ref, toRef } from 'vue'
import { useRouter } from 'vue-router'
import { accountsApi } from '@/api/accounts'
import type {
  Account,
  AccountHealthFilter,
} from '@/api/accounts'
import { usePageRuntime } from '@/composables/usePageRuntime'
import { usePagedQuery } from '@/composables/usePageQuery'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import { useAccountBulkActionsRuntime } from './accountBulkActionsRuntime'
import { useAccountBulkProgressRuntime } from './accountBulkProgressRuntime'
import { useAccountCrudRuntime } from './accountCrudRuntime'
import { useAccountExportRuntime } from './accountExportRuntime'
import { useAccountGroupsRuntime } from './accountGroupsRuntime'
import { useAccountImportRuntime } from './accountImportRuntime'
import { useAccountPageLifecycleRuntime } from './accountPageLifecycleRuntime'
import { useAccountProxyRuntime } from './accountProxyRuntime'
import { useAccountSelectionRuntime } from './accountSelectionRuntime'
import {
  accountFlagFilterGroups,
  checkoutFinalLinkUrl,
  scheduleCodeLabels,
  statusCategory,
  type AccountStatusFilter,
} from './viewUtils'

type AccountsViewMode = 'list' | 'cards'
type AccountListStatusFilter = AccountStatusFilter | 'valid_checkout' | `sched:${string}`
export type { AccountImportMode } from './accountImportRuntime'

const ACCOUNT_PAGE_SIZE_OPTIONS = [20, 50, 100]
const DEFAULT_PAGE_SIZE = 20
const REFRESH_BATCH_SIZE = 20
const ACCOUNT_LIST_REQUEST_KEY = 'accounts:list'
const ACCOUNT_GROUPS_REQUEST_KEY = 'accounts:groups'
const LIST_RELOAD_TIMER_KEY = 'accounts:list-reload'

function normalizeErrorMessage(error: unknown): string {
  return errorMessage(error)
}

export function useAccountsPage() {
  const loading = ref(false)
  const keyword = ref('')
  const statusFilter = ref<AccountListStatusFilter>('all')
  const groupFilter = ref('all')
  const healthFilter = ref<AccountHealthFilter>('all')
  const flagFilter = ref('all')
  // 调度状态汇总：除“状态”外的其它筛选生效，点芯片即设置 statusFilter = sched:xxx
  const availabilitySummary = ref<Record<string, number>>({})
  const pageSize = ref(DEFAULT_PAGE_SIZE)
  const accounts = ref<Account[]>([])
  const accountAllTotal = ref(0)
  const viewMode = ref<AccountsViewMode>('list')
  const bulkProgress = useAccountBulkProgressRuntime()
  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  const pageRuntime = usePageRuntime('accounts')
  const router = useRouter()
  const accountListQuery = usePagedQuery({
    runtime: pageRuntime,
    key: ACCOUNT_LIST_REQUEST_KEY,
    pageSize,
    loading,
    errorMessage: '加载失败',
    fetch: ({ page, pageSize: size }) => accountsApi.list({
      page,
      page_size: size,
      keyword: keyword.value.trim(),
      status: statusFilter.value,
      group_id: groupFilter.value,
      health: healthFilter.value,
      flag: flagFilter.value,
    }),
    resolvePage: (res) => res.page,
    resolvePageCount: (res) => {
      const total = Number(res.total ?? res.accounts?.length ?? 0)
      const size = Number(res.page_size ?? pageSize.value)
      if (!Number.isFinite(total) || !Number.isFinite(size) || size <= 0) return 1
      return Math.max(1, Math.ceil(total / size))
    },
    resolveTotal: (res) => res.total ?? res.accounts?.length ?? 0,
    apply: (res) => {
      accountAllTotal.value = Number(res.all_total ?? 0)
      availabilitySummary.value = res.availability_summary || {}
      accounts.value = (res.accounts || []).map((item) => ({
        ...item,
        lanes: Array.isArray(item.lanes) ? item.lanes : [],
        model_ids: {
          fast: item.model_ids?.fast || '',
          thinking: item.model_ids?.thinking || '',
          pro: item.model_ids?.pro || '',
        },
      }))
      accountSelection.pruneToCurrentAccounts()
    },
    onError: (_message, error) => {
      setError('加载失败', error)
    },
  })
  const filteredAccounts = computed(() => accounts.value)

  const currentPage = accountListQuery.currentPage
  const accountListTotal = accountListQuery.total
  const pageCount = accountListQuery.pageCount

  const pagedAccounts = computed(() => accounts.value)

  const statusFilterOptions = computed(() => {
    const base = [
      { label: '全部状态', value: 'all' },
      { label: '正常', value: 'normal' },
      { label: '限流', value: 'limited' },
      { label: '异常', value: 'abnormal' },
      { label: '禁用', value: 'disabled' },
      { label: '有效支付链接', value: 'valid_checkout' },
    ]
    // 选了调度状态芯片时，让状态下拉也能显示当前值
    if (statusFilter.value.startsWith('sched:')) {
      const code = statusFilter.value.slice(6)
      base.push({ label: `调度：${scheduleCodeLabels[code] || code}`, value: statusFilter.value })
    }
    return base
  })

  const healthFilterOptions = [
    { label: '全部健康度', value: 'all' },
    { label: '有风险（注意 + 危险）', value: 'risk' },
    { label: '健康', value: 'healthy' },
    { label: '注意', value: 'warning' },
    { label: '危险', value: 'danger' },
    { label: '未参与调度', value: 'idle' },
  ] as const

  const groupFilterOptions = computed(() => [
    { label: '全部账号组', value: 'all' },
    { label: '未分组', value: '__ungrouped__' },
    ...accountGroups.value.map((group) => ({
      label: `${group.enabled === false ? '停用 · ' : ''}${group.name || group.id}`,
      value: group.id,
    })),
  ])

  const abnormalAccountIds = computed(() => (
    accounts.value
      .filter((item) => statusCategory(item) === 'abnormal')
      .map((item) => item.id)
  ))

  const abnormalAccountCount = computed(() => abnormalAccountIds.value.length)
  const accountSelection = useAccountSelectionRuntime({
    accounts,
    pagedAccounts,
  })
  const selectedIds = accountSelection.selectedIds
  const selectedCount = accountSelection.selectedCount
  const allVisibleSelected = accountSelection.allVisibleSelected
  const batchBusy = bulkProgress.batchBusy
  const batchActionLabel = bulkProgress.batchActionLabel
  const showRefreshProgress = bulkProgress.showRefreshProgress
  const refreshProgressTitle = bulkProgress.refreshProgressTitle
  const refreshProgress = bulkProgress.refreshProgress
  const refreshProgressPercent = bulkProgress.refreshProgressPercent
  const refreshProgressMetricLabel = bulkProgress.refreshProgressMetricLabel
  const refreshProgressMetricValue = bulkProgress.refreshProgressMetricValue
  const refreshProgressStatusText = bulkProgress.refreshProgressStatusText
  const canStopRefreshProgress = bulkProgress.canStopRefreshProgress
  const bulkStopRequested = bulkProgress.bulkStopRequested

  function setError(prefix: string, error: unknown, notify = true) {
    const message = normalizeErrorMessage(error)
    if (notify) toast.error(`${prefix}: ${message}`)
  }

  const accountGroupsRuntime = useAccountGroupsRuntime({
    runtime: pageRuntime,
    requestKey: ACCOUNT_GROUPS_REQUEST_KEY,
    groupFilter,
    loadData,
    setError,
  })
  const accountGroups = accountGroupsRuntime.accountGroups
  const proxyGroups = accountGroupsRuntime.proxyGroups
  const accountGroupsLoading = accountGroupsRuntime.accountGroupsLoading
  const showAccountGroupsModal = accountGroupsRuntime.showAccountGroupsModal
  const accountGroupSaving = accountGroupsRuntime.accountGroupSaving
  const editingAccountGroupId = accountGroupsRuntime.editingAccountGroupId
  const selectedBindGroupId = accountGroupsRuntime.selectedBindGroupId
  const accountGroupForm = accountGroupsRuntime.accountGroupForm
  const accountGroupOptions = accountGroupsRuntime.accountGroupOptions
  const accountGroupProxyOptions = accountGroupsRuntime.accountGroupProxyOptions
  const bindAccountGroupOptions = accountGroupsRuntime.bindAccountGroupOptions
  const accountGroupProxyMode = accountGroupsRuntime.accountGroupProxyMode
  const selectedAccountGroupProxyGroupId = accountGroupsRuntime.selectedAccountGroupProxyGroupId
  const accountGroupCustomProxyInput = accountGroupsRuntime.accountGroupCustomProxyInput
  const accountGroupProxyPreview = accountGroupsRuntime.accountGroupProxyPreview
  const applyAccountGroupsPayload = accountGroupsRuntime.applyAccountGroupsPayload
  const loadAccountGroups = accountGroupsRuntime.loadAccountGroups
  const resetAccountGroupForm = accountGroupsRuntime.resetAccountGroupForm
  const openAccountGroupsModal = accountGroupsRuntime.openAccountGroupsModal
  const closeAccountGroupsModal = accountGroupsRuntime.closeAccountGroupsModal
  const editAccountGroup = accountGroupsRuntime.editAccountGroup
  const saveAccountGroup = accountGroupsRuntime.saveAccountGroup
  const deleteAccountGroup = accountGroupsRuntime.deleteAccountGroup
  const setAccountGroupProxyMode = accountGroupsRuntime.setAccountGroupProxyMode
  const selectAccountGroupProxyGroup = accountGroupsRuntime.selectAccountGroupProxyGroup
  const setAccountGroupCustomProxyInput = accountGroupsRuntime.setAccountGroupCustomProxyInput

  const accountCrud = useAccountCrudRuntime({
    loadData,
    loadAccountGroups,
    normalizeErrorMessage,
    setError,
  })
  const saving = accountCrud.saving
  const showModal = accountCrud.showModal
  const editingId = accountCrud.editingId
  const refreshingAccountIds = accountCrud.refreshingAccountIds
  const resettingAccountIds = accountCrud.resettingAccountIds
  const accountStatusOptions = accountCrud.accountStatusOptions
  const form = accountCrud.form

  const accountProxyRuntime = useAccountProxyRuntime({
    proxyGroups,
    proxyValue: toRef(form, 'proxy'),
    setError,
  })
  const proxyTesting = accountProxyRuntime.proxyTesting
  const proxyMode = accountProxyRuntime.proxyMode
  const accountProxyModeOptions = accountProxyRuntime.accountProxyModeOptions
  const proxyGroupOptions = accountProxyRuntime.proxyGroupOptions
  const selectedProxyGroupId = accountProxyRuntime.selectedProxyGroupId
  const customProxyInput = accountProxyRuntime.customProxyInput
  const accountProxyPreview = accountProxyRuntime.accountProxyPreview
  const setProxyMode = accountProxyRuntime.setProxyMode
  const selectProxyGroup = accountProxyRuntime.selectProxyGroup
  const setCustomProxyInput = accountProxyRuntime.setCustomProxyInput
  const testAccountProxy = accountProxyRuntime.testAccountProxy
  accountCrud.setProxyControlsSync(accountProxyRuntime.syncProxyControlsFromValue)

  const accountExport = useAccountExportRuntime({
    accounts,
    selectedIds,
    accountAllTotal,
    accountListTotal,
    setError,
  })
  const exportBusy = accountExport.exportBusy
  const exportAccounts = accountExport.exportAccounts

  const accountImport = useAccountImportRuntime({
    bulkProgress,
    normalizeErrorMessage,
    setError,
    loadData,
  })
  const importBusy = accountImport.importBusy
  const showImportModal = accountImport.showImportModal
  const importMode = accountImport.importMode
  const importModeOptions = accountImport.importModeOptions
  const oauthEmailHint = accountImport.oauthEmailHint
  const oauthCallbackText = accountImport.oauthCallbackText
  const oauthSessionId = accountImport.oauthSessionId
  const oauthAuthorizeUrl = accountImport.oauthAuthorizeUrl
  const oauthRedirectUriPrefix = accountImport.oauthRedirectUriPrefix
  const manualTokenText = accountImport.manualTokenText
  const sessionJsonText = accountImport.sessionJsonText
  const verifyOnImport = accountImport.verifyOnImport
  const importTargetGroup = accountImport.importTargetGroup
  const targetGroupParam = accountImport.targetGroupParam
  const importBackupFile = accountImport.importBackupFile
  const importSub2apiFiles = accountImport.importSub2apiFiles

  const accountBulkActions = useAccountBulkActionsRuntime({
    bulkProgress,
    accountSelection,
    accounts,
    accountGroups,
    proxyGroups,
    selectedBindGroupId,
    accountAllTotal,
    accountListTotal,
    refreshBatchSize: REFRESH_BATCH_SIZE,
    normalizeErrorMessage,
    setError,
    loadData,
    applyAccountGroupsPayload,
  })
  const refreshAllAccounts = accountBulkActions.refreshAllAccounts
  const refreshSelectedAccounts = accountBulkActions.refreshSelectedAccounts
  const refreshSelectedAccessTokens = accountBulkActions.refreshSelectedAccessTokens
  const refreshAccessTokensFor = accountBulkActions.refreshAccessTokensFor
  const requestStopRefreshProgress = accountBulkActions.requestStopRefreshProgress
  const runBulkAction = accountBulkActions.runBulkAction
  const bindSelectedAccountsToGroup = accountBulkActions.bindSelectedAccountsToGroup

  const filtersActive = computed(() => Boolean(
    keyword.value.trim()
    || statusFilter.value !== 'all'
    || groupFilter.value !== 'all'
    || healthFilter.value !== 'all'
    || flagFilter.value !== 'all',
  ))

  function resetFilters() {
    keyword.value = ''
    statusFilter.value = 'all'
    groupFilter.value = 'all'
    healthFilter.value = 'all'
    flagFilter.value = 'all'
  }

  const selectingAllMatching = ref(false)

  /** 跨分页全选：让后端按当前筛选条件解析出全部匹配账号的 ID。 */
  async function selectAllMatching() {
    if (selectingAllMatching.value) return
    selectingAllMatching.value = true
    try {
      const preview = await accountsApi.previewSelection({
        mode: 'filter',
        keyword: keyword.value.trim(),
        status: statusFilter.value,
        group_id: groupFilter.value,
        health: healthFilter.value,
        flag: flagFilter.value,
        excluded_account_ids: [],
      })
      accountSelection.selectIds(preview.account_ids)
      toast.success(`已选择全部 ${preview.selected_count} 个筛选结果`)
    } catch (error) {
      setError('选择全部筛选结果失败', error)
    } finally {
      selectingAllMatching.value = false
    }
  }

  /** 复制凭据：走管理员专用接口（no-store，且写审计日志），列表里不携带 refresh_token。 */
  async function copyAccountCredential(item: Account, kind: 'access' | 'refresh' = 'access') {
    try {
      const token = kind === 'refresh'
        ? await accountsApi.getRefreshToken(item.id)
        : (item.is_demo ? String(item.access_token || '').trim() : await accountsApi.getAccessToken(item.id))
      if (!token) {
        toast.warning(kind === 'refresh' ? '当前账号没有 Refresh Token' : '当前账号没有可复制的 Access Token')
        return
      }
      await navigator.clipboard.writeText(token)
      toast.success(kind === 'refresh' ? 'Refresh Token 已复制' : 'Access Token 已复制')
    } catch (error) {
      setError(kind === 'refresh' ? '复制 Refresh Token 失败' : '复制 Access Token 失败', error)
    }
  }

  async function extractSelectedCheckout() {
    if (batchBusy.value) {
      toast.info('已有批量操作正在执行')
      return
    }

    const selected = new Set(selectedIds.value)
    const targetAccounts = accounts.value.filter((item) => selected.has(item.id) && !item.is_demo)
    if (!targetAccounts.length) {
      toast.warning('请先选择 OpenAI 账号')
      return
    }

    const title = '批量持续提链'
    const confirmed = await confirmDialog.ask({
      title,
      message: `将把 ${targetAccounts.length} 个选中账号按当前渠道配置追加到持续提链队列，直到成功或你停止任务。是否继续？`,
      confirmText: '加入队列',
      cancelText: '取消',
    })
    if (!confirmed || batchBusy.value) return

    bulkProgress.start(title, targetAccounts.length, 'checkout')
    try {
      const result = await accountsApi.enqueueCheckoutRetries(targetAccounts.map((item) => item.id))
      const queued = Math.max(0, Number(result.queued || 0))
      const skipped = Math.max(0, Number(result.skipped || 0))
      bulkProgress.finish({
        total: targetAccounts.length,
        processed: targetAccounts.length,
        total_quota: 0,
      })
      if (queued > 0) {
        toast.success(`已加入 ${queued} 个持续提链任务${skipped ? `，跳过 ${skipped} 个` : ''}，正在打开提链进度`)
        try {
          await router.push({ name: 'register', query: { focus: 'checkout' } })
        } catch {
          toast.warning('任务已加入队列，请前往“账号注册”查看提链进度')
        }
      } else {
        toast.warning(skipped ? `没有新增提链任务，跳过 ${skipped} 个` : '未加入新的提链任务')
      }
    } catch (error) {
      bulkProgress.fail(targetAccounts.length, 0, normalizeErrorMessage(error))
      setError('加入持续提链队列失败', error)
    } finally {
      try {
        await loadData({ silentErrorToast: true })
      } catch {
        toast.warning('任务已加入队列，但账号列表刷新失败')
      }
      bulkProgress.end()
    }
  }

  async function copyFinalCheckoutLink(item: Account) {
    const url = checkoutFinalLinkUrl(item)
    if (!url) {
      toast.warning('当前账号没有可复制的最终支付链接')
      return
    }
    try {
      await navigator.clipboard.writeText(url)
      toast.success('最终支付链接已复制')
    } catch (error) {
      setError('复制最终支付链接失败', error)
    }
  }

  function openFinalCheckoutLink(item: Account) {
    const url = checkoutFinalLinkUrl(item)
    if (!url) {
      toast.warning('当前账号没有可打开的最终支付链接')
      return
    }
    const opened = window.open(url, '_blank', 'noopener,noreferrer')
    if (!opened) toast.warning('浏览器阻止了新窗口，请允许弹窗后重试')
  }

  async function loadData(options?: { silentErrorToast?: boolean }) {
    await accountListQuery.load({ silentError: options?.silentErrorToast })
  }

  const isSelected = accountSelection.isSelected
  const toggleSelect = accountSelection.toggleSelect
  const clearSelection = accountSelection.clearSelection
  const toggleSelectAllVisible = accountSelection.toggleSelectAllVisible

  const setImportMode = accountImport.setImportMode
  const openImportModal = accountImport.openImportModal
  const closeImportModal = accountImport.closeImportModal
  const importManualTokenText = accountImport.importManualTokenText
  const importTokenTextFile = accountImport.importTokenTextFile
  const importSessionJson = accountImport.importSessionJson
  const startOAuthLogin = accountImport.startOAuthLogin
  const openOAuthAuthorizeUrl = accountImport.openOAuthAuthorizeUrl
  const copyOAuthAuthorizeUrl = accountImport.copyOAuthAuthorizeUrl
  const finishOAuthLogin = accountImport.finishOAuthLogin
  const importLocalCPAFiles = accountImport.importLocalCPAFiles

  function closeRefreshProgress() {
    bulkProgress.close()
  }

  const openCreateModal = accountCrud.openCreateModal
  const openEditModal = accountCrud.openEditModal
  const closeModal = accountCrud.closeModal
  const saveAccount = accountCrud.saveAccount
  const toggleEnabled = accountCrud.toggleEnabled
  const refreshToken = accountCrud.refreshToken
  const resetAccountState = accountCrud.resetAccountState
  const clearAutoPause = accountCrud.clearAutoPause
  const removeAccount = accountCrud.removeAccount

  const pageLifecycle = useAccountPageLifecycleRuntime({
    runtime: pageRuntime,
    viewMode,
    pageSize,
    currentPage,
    keyword,
    statusFilter,
    groupFilter,
    healthFilter,
    flagFilter,
    keepSelectionOnPageChange: () => accountSelection.crossPageActive.value,
    pageSizeDefault: DEFAULT_PAGE_SIZE,
    pageSizeOptions: ACCOUNT_PAGE_SIZE_OPTIONS,
    reloadTimerKey: LIST_RELOAD_TIMER_KEY,
    loadData,
    loadGroups: loadAccountGroups,
    invalidateData: accountListQuery.invalidate,
    invalidateGroups: accountGroupsRuntime.invalidate,
    clearSelection,
    shouldSkipRefresh: () => Boolean(
      showModal.value ||
      showImportModal.value ||
      showAccountGroupsModal.value ||
      saving.value ||
      batchBusy.value ||
      importBusy.value ||
      accountGroupsLoading.value ||
      accountGroupSaving.value,
    ),
  })
  const setViewMode = pageLifecycle.setViewMode

  return {
    loading,
    saving,
    showModal,
    keyword,
    statusFilter,
    groupFilter,
    healthFilter,
    flagFilter,
    flagFilterGroups: accountFlagFilterGroups,
    availabilitySummary,
    filtersActive,
    resetFilters,
    statusFilterOptions,
    groupFilterOptions,
    healthFilterOptions,
    editingId,
    accounts,
    accountListTotal,
    accountAllTotal,
    selectedIds,
    selectedCount,
    abnormalAccountCount,
    allVisibleSelected,
    currentPage,
    pageSize,
    pageSizeOptions: ACCOUNT_PAGE_SIZE_OPTIONS,
    pageCount,
    batchBusy,
    batchActionLabel,
    viewMode,
    refreshingAccountIds,
    resettingAccountIds,
    importBusy,
    exportBusy,
    showImportModal,
    importMode,
    importModeOptions,
    oauthEmailHint,
    oauthCallbackText,
    oauthSessionId,
    oauthAuthorizeUrl,
    oauthRedirectUriPrefix,
    manualTokenText,
    sessionJsonText,
    verifyOnImport,
    importTargetGroup,
    targetGroupParam,
    importBackupFile,
    importSub2apiFiles,
    accountGroups,
    proxyGroups,
    accountGroupsLoading,
    showAccountGroupsModal,
    accountGroupSaving,
    editingAccountGroupId,
    accountGroupForm,
    accountGroupOptions,
    accountGroupProxyOptions,
    bindAccountGroupOptions,
    selectedBindGroupId,
    proxyTesting,
    proxyMode,
    accountGroupProxyMode,
    accountProxyModeOptions,
    proxyGroupOptions,
    selectedProxyGroupId,
    customProxyInput,
    selectedAccountGroupProxyGroupId,
    accountGroupCustomProxyInput,
    accountProxyPreview,
    accountGroupProxyPreview,
    showRefreshProgress,
    refreshProgressTitle,
    refreshProgress,
    refreshProgressPercent,
    refreshProgressMetricLabel,
    refreshProgressMetricValue,
    refreshProgressStatusText,
    canStopRefreshProgress,
    bulkStopRequested,
    accountStatusOptions,
    form,
    filteredAccounts,
    pagedAccounts,
    setViewMode,
    isSelected,
    toggleSelect,
    clearSelection,
    toggleSelectAllVisible,
    setImportMode,
    openImportModal,
    closeImportModal,
    loadAccountGroups,
    openAccountGroupsModal,
    closeAccountGroupsModal,
    resetAccountGroupForm,
    editAccountGroup,
    saveAccountGroup,
    deleteAccountGroup,
    testAccountProxy,
    setProxyMode,
    selectProxyGroup,
    setCustomProxyInput,
    setAccountGroupProxyMode,
    selectAccountGroupProxyGroup,
    setAccountGroupCustomProxyInput,
    importManualTokenText,
    importTokenTextFile,
    importSessionJson,
    startOAuthLogin,
    openOAuthAuthorizeUrl,
    copyOAuthAuthorizeUrl,
    finishOAuthLogin,
    importLocalCPAFiles,
    refreshAllAccounts,
    refreshSelectedAccounts,
    refreshSelectedAccessTokens,
    refreshAccessTokensFor,
    selectAllMatching,
    selectingAllMatching,
    crossPageSelected: accountSelection.crossPageActive,
    requestStopRefreshProgress,
    closeRefreshProgress,
    loadData,
    copyAccountCredential,
    extractSelectedCheckout,
    copyFinalCheckoutLink,
    openFinalCheckoutLink,
    openCreateModal,
    openEditModal,
    closeModal,
    saveAccount,
    toggleEnabled,
    refreshToken,
    resetAccountState,
    clearAutoPause,
    removeAccount,
    runBulkAction,
    bindSelectedAccountsToGroup,
    exportAccounts,
  }
}
