import { ref } from 'vue'

import { accountsApi } from '@/api/accounts'
import { accountImportsApi } from '@/api/accountImports'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import type { useAccountBulkProgressRuntime } from './accountBulkProgressRuntime'

export type AccountImportMode =
  | 'oauth_login'
  | 'backup_json'
  | 'access_token'
  | 'session_json'
  | 'cpa_json'
  | 'sub2api_json'
  | 'remote_cpa'
  | 'sub2api'

export const BACKUP_FORMAT = 'gptgrok2api-accounts-backup'
export const TARGET_GROUP_KEEP = '__keep__'
export const TARGET_GROUP_UNGROUPED = '__ungrouped__'

const IMPORT_BATCH_SIZE = 20

type AccountImportRuntimeOptions = {
  bulkProgress: ReturnType<typeof useAccountBulkProgressRuntime>
  normalizeErrorMessage: (error: unknown) => string
  setError: (prefix: string, error: unknown, notify?: boolean) => void
  loadData: (options?: { silentErrorToast?: boolean }) => Promise<void>
}

function uniqueTokens(tokens: string[]) {
  return Array.from(new Set(tokens.map((token) => token.trim()).filter(Boolean)))
}

function parseTokenLines(text: string) {
  return uniqueTokens(
    text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#')),
  )
}

type AccountImportPayload = Record<string, unknown>

function accountPayloadToken(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  const source = value as AccountImportPayload
  return String(source.access_token || source.accessToken || '').trim()
}

function parseSessionJsonAccount(rawText: string): AccountImportPayload {
  const text = rawText.trim()
  if (!text) throw new Error('请先粘贴 Session JSON')
  const parsed = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Session JSON 格式不正确')
  }
  const source = parsed as AccountImportPayload
  const token = accountPayloadToken(source)
  if (!token) throw new Error('Session JSON 中没有找到 accessToken')
  return {
    ...source,
    access_token: token,
    type: source.type || source.plan_type || 'free',
    source_type: source.source_type || source.sourceType || 'session_json',
  }
}

function tokenFromCPAAccount(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  const source = value as Record<string, unknown>
  return String(source.access_token || source.accessToken || '').trim()
}

function parseCPAJsonTokens(rawText: string, label: string) {
  const text = rawText.trim()
  if (!text) throw new Error(`${label} 是空文件`)
  const parsed = JSON.parse(text)
  const candidates: unknown[] = []

  if (Array.isArray(parsed)) {
    candidates.push(...parsed)
  } else if (parsed && typeof parsed === 'object') {
    if (tokenFromCPAAccount(parsed)) {
      candidates.push(parsed)
    } else {
      const source = parsed as Record<string, unknown>
      for (const key of ['accounts', 'items', 'data', 'results']) {
        const rows = source[key]
        if (Array.isArray(rows)) candidates.push(...rows)
      }
    }
  }

  const tokens = uniqueTokens(candidates.map(tokenFromCPAAccount).filter(Boolean))
  if (!tokens.length) throw new Error(`${label} 中没有找到 access_token`)
  return tokens
}

export function useAccountImportRuntime(options: AccountImportRuntimeOptions) {
  const importBusy = ref(false)
  const showImportModal = ref(false)
  const importMode = ref<AccountImportMode>('access_token')
  const oauthEmailHint = ref('')
  const oauthCallbackText = ref('')
  const oauthSessionId = ref('')
  const oauthAuthorizeUrl = ref('')
  const oauthRedirectUriPrefix = ref('')
  const manualTokenText = ref('')
  const sessionJsonText = ref('')
  // 入池前验证：先用 refresh_token 换一次 token 并同步额度，不可用的账号不留在号池。
  const verifyOnImport = ref(false)
  // 导入目标账号组：__keep__ 不改动分组，__ungrouped__ 归入未分组，其它为账号组 ID。
  const importTargetGroup = ref<string>(TARGET_GROUP_KEEP)
  const toast = useToast()
  const confirmDialog = useConfirmDialog()

  /** undefined 表示保持账号原分组；'' 表示未分组。 */
  function targetGroupParam(): string | undefined {
    if (importTargetGroup.value === TARGET_GROUP_KEEP) return undefined
    if (importTargetGroup.value === TARGET_GROUP_UNGROUPED) return ''
    return importTargetGroup.value
  }

  const importModeOptions = [
    { label: 'OAuth 登录已有账号', value: 'oauth_login' },
    { label: '导入完整备份文件', value: 'backup_json' },
    { label: '导入 Access Token', value: 'access_token' },
    { label: '导入 Session JSON', value: 'session_json' },
    { label: '导入 CPA JSON 文件', value: 'cpa_json' },
    { label: '导入 Sub2API JSON 文件', value: 'sub2api_json' },
    { label: '从远程 CPA 服务器导入', value: 'remote_cpa' },
    { label: '从 Sub2API 服务器导入', value: 'sub2api' },
  ] as const

  function setImportMode(mode: AccountImportMode) {
    importMode.value = mode
  }

  async function openImportModal(mode: AccountImportMode = 'access_token') {
    showImportModal.value = true
    setImportMode(mode)
  }

  function closeImportModal() {
    if (importBusy.value) return
    showImportModal.value = false
  }

  async function promptRemoveImportedAbnormalAccounts(importedTokens: string[], errorCount: number) {
    if (errorCount <= 0 || options.bulkProgress.bulkStopRequested.value) return

    let preview: Awaited<ReturnType<typeof accountsApi.cleanupImportedAbnormalAccounts>>
    try {
      preview = await accountsApi.cleanupImportedAbnormalAccounts(importedTokens, false)
    } catch (error) {
      options.setError('检查本次异常账号失败，已先保留', error)
      return
    }

    if (!preview.abnormal) {
      toast.info('本次导入有刷新异常，但没有找到可清理的异常账号，可能未写入本地或状态已变化')
      return
    }

    const confirmed = await confirmDialog.ask({
      title: '移除本次异常账号？',
      message: `本次导入刷新返回 ${errorCount} 条异常。\n后端确认 ${preview.abnormal} 个本次导入账号当前状态为异常，是否直接删除？\n\n只会删除本次导入且状态为异常的账号，正常、限流和历史账号会保留。`,
      confirmText: `删除 ${preview.abnormal} 个`,
      cancelText: '先保留',
    })

    if (!confirmed) return

    try {
      const result = await accountsApi.cleanupImportedAbnormalAccounts(importedTokens, true)
      toast.success(`已移除 ${result.removed || 0} 个本次异常账号`)
    } catch (error) {
      options.setError('移除本次异常账号失败', error)
    } finally {
      await options.loadData({ silentErrorToast: true })
    }
  }

  async function importAccountPayloadBatch(
    accountPayloads: AccountImportPayload[],
    sourceType: string,
    title: string,
    extra: { restore?: boolean } = {},
  ) {
    const payloadsByToken = new Map<string, AccountImportPayload>()
    for (const payload of accountPayloads) {
      const accessToken = accountPayloadToken(payload)
      if (accessToken) payloadsByToken.set(accessToken, payload)
    }
    const normalizedAccountPayloads = Array.from(payloadsByToken.values())
    const normalizedTokens = Array.from(payloadsByToken.keys())
    if (!normalizedAccountPayloads.length) {
      toast.warning('没有可导入的 access token')
      return
    }

    const restore = Boolean(extra.restore)
    const verify = restore ? false : verifyOnImport.value
    const confirmed = await confirmDialog.ask({
      title,
      message: restore
        ? `即将还原 ${normalizedTokens.length} 个账号，保留备份里的账号 ID、分组、代理和统计，不会联网同步。已存在的账号会被备份内容覆盖。是否继续？`
        : verify
        ? `即将验证并导入 ${normalizedTokens.length} 个账号，验证不通过的账号不会留在号池。是否继续？`
        : `即将导入 ${normalizedTokens.length} 个账号，已存在账号会刷新远端信息。是否继续？`,
      confirmText: '确认导入',
      cancelText: '取消',
    })
    if (!confirmed) return

    importBusy.value = true
    options.bulkProgress.start(title, normalizedTokens.length, 'mutation')
    let addedCount = 0
    let skippedCount = 0
    let refreshedCount = 0
    let processed = 0
    const errors: string[] = []
    const rejected: string[] = []
    try {
      for (let index = 0; index < normalizedTokens.length; index += IMPORT_BATCH_SIZE) {
        if (options.bulkProgress.bulkStopRequested.value) break
        const batch = normalizedAccountPayloads.slice(index, index + IMPORT_BATCH_SIZE)
        try {
          const result = await accountsApi.importAccounts(
            batch,
            sourceType,
            {
              refresh: !restore,
              returnItems: false,
              verifyOnImport: verify,
              restore,
              targetGroupId: targetGroupParam(),
            },
          )
          addedCount += Number(result.added || 0)
          skippedCount += Number(result.skipped || 0)
          refreshedCount += Number(result.refreshed || 0)
          rejected.push(...result.rejected.map((item) => `${item.email || '未知邮箱'}：${item.reason || item.status || '验证未通过'}`))
          errors.push(...(Array.isArray(result.errors) ? result.errors.filter(Boolean) : []))
        } catch (error) {
          errors.push(`${accountPayloadToken(batch[0])?.slice(0, 6) || '-'}... 等 ${batch.length} 个账号：${options.normalizeErrorMessage(error)}`)
        } finally {
          processed = Math.min(normalizedTokens.length, processed + batch.length)
          options.bulkProgress.update({
            total: normalizedTokens.length,
            processed,
            done: processed >= normalizedTokens.length,
            total_quota: 0,
          })
        }
      }

      await options.loadData({ silentErrorToast: true })
      const stopped = options.bulkProgress.bulkStopRequested.value && processed < normalizedTokens.length
      options.bulkProgress.finish({
        total: normalizedTokens.length,
        processed,
        total_quota: 0,
      })
      if (stopped) {
        toast.warning(`${title}已停止：已处理 ${processed}/${normalizedTokens.length} 个`)
      } else if (verify) {
        const summary = `${title}完成：验证通过并入池 ${addedCount}，未通过 ${rejected.length}，失败 ${errors.length}`
        if (rejected.length || errors.length) {
          toast.warning(summary)
          await confirmDialog.ask({
            title: '入池验证未通过的账号',
            message: [...rejected, ...errors].slice(0, 30).join('\n')
              + (rejected.length + errors.length > 30 ? `\n…… 共 ${rejected.length + errors.length} 条` : ''),
            confirmText: '知道了',
            cancelText: '关闭',
          })
        } else {
          toast.success(summary)
        }
      } else if (errors.length > 0) {
        toast.warning(`${title}完成：新增 ${addedCount}，跳过 ${skippedCount}，刷新 ${refreshedCount}，失败 ${errors.length}`)
      } else {
        toast.success(`${title}完成：新增 ${addedCount}，跳过 ${skippedCount}，刷新 ${refreshedCount}`)
      }
      if (addedCount + skippedCount + refreshedCount > 0) {
        manualTokenText.value = ''
        sessionJsonText.value = ''
      }
      // 入池验证已把不可用账号移出号池，不必再提示清理。
      if (!stopped && !verify && errors.length > 0) {
        await promptRemoveImportedAbnormalAccounts(normalizedTokens, errors.length)
      }
    } catch (error) {
      options.bulkProgress.finish({
        total: normalizedTokens.length,
        processed,
        error: options.normalizeErrorMessage(error),
        total_quota: 0,
      })
      options.setError(`${title}失败`, error)
    } finally {
      importBusy.value = false
      options.bulkProgress.end()
    }
  }

  async function importTokenBatch(tokens: string[], sourceType: string, title: string) {
    await importAccountPayloadBatch(
      uniqueTokens(tokens).map((accessToken) => ({
        access_token: accessToken,
        type: 'free',
        source_type: sourceType,
      })),
      sourceType,
      title,
    )
  }

  async function importManualTokenText() {
    await importTokenBatch(parseTokenLines(manualTokenText.value), 'manual', '导入 Access Token')
  }

  async function importTokenTextFile(file: File | null | undefined) {
    if (!file) return
    const text = await file.text()
    manualTokenText.value = text
    await importManualTokenText()
  }

  function parseBackupAccounts(rawText: string, label: string): AccountImportPayload[] {
    const text = rawText.trim()
    if (!text) throw new Error(`${label} 是空文件`)
    const parsed = JSON.parse(text)
    const rows = Array.isArray(parsed)
      ? parsed
      : parsed && typeof parsed === 'object' && Array.isArray((parsed as { accounts?: unknown }).accounts)
        ? (parsed as { accounts: unknown[] }).accounts
        : []
    if (!Array.isArray(parsed) && (parsed as { format?: unknown })?.format !== BACKUP_FORMAT) {
      throw new Error(`${label} 不是本系统导出的完整备份文件（导出时请选择「完整备份」）`)
    }
    const accounts = rows.filter(
      (row): row is AccountImportPayload => Boolean(row) && typeof row === 'object' && !Array.isArray(row)
        && Boolean(accountPayloadToken(row)),
    )
    if (!accounts.length) throw new Error(`${label} 中没有找到账号`)
    return accounts
  }

  async function importBackupFile(file: File | null | undefined) {
    if (!file) return
    let accounts: AccountImportPayload[]
    try {
      accounts = parseBackupAccounts(await file.text(), file.name)
    } catch (error) {
      options.setError('读取备份文件失败', error)
      return
    }
    await importAccountPayloadBatch(accounts, 'backup', '导入完整备份文件', { restore: true })
  }

  async function importSub2apiFiles(files: FileList | File[] | null | undefined) {
    const fileList = Array.from(files || [])
    if (!fileList.length) return
    const confirmed = await confirmDialog.ask({
      title: '导入 Sub2API JSON 文件',
      message: `即将读取 ${fileList.length} 个 Sub2API JSON 文件，保存账号后同步账号与额度。是否继续？`,
      confirmText: '开始导入',
      cancelText: '取消',
    })
    if (!confirmed) return
    importBusy.value = true
    let parsed = 0
    let added = 0
    let skipped = 0
    let refreshed = 0
    const errors: string[] = []
    try {
      for (const file of fileList) {
        try {
          const result = await accountsApi.importSub2apiFile(await file.text(), {
            refresh: true,
            targetGroupId: targetGroupParam(),
          })
          parsed += result.parsed
          added += result.added
          skipped += result.skipped
          refreshed += result.refreshed
          errors.push(...result.errors)
        } catch (error) {
          errors.push(`${file.name}：${options.normalizeErrorMessage(error)}`)
        }
      }
      await options.loadData({ silentErrorToast: true })
      const summary = `Sub2API JSON 导入完成：解析 ${parsed}，新增 ${added}，跳过 ${skipped}，刷新 ${refreshed}`
      if (errors.length) toast.warning(`${summary}，失败 ${errors.length}：${errors[0]}`)
      else toast.success(summary)
    } finally {
      importBusy.value = false
    }
  }

  async function importSessionJson() {
    await importAccountPayloadBatch([parseSessionJsonAccount(sessionJsonText.value)], 'session_json', '导入 Session JSON')
  }

  async function startOAuthLogin() {
    importBusy.value = true
    try {
      const result = await accountImportsApi.startOAuthLogin(oauthEmailHint.value)
      oauthSessionId.value = String(result.session_id || '')
      oauthAuthorizeUrl.value = String(result.authorize_url || '')
      oauthRedirectUriPrefix.value = String(result.redirect_uri_prefix || '')
      oauthCallbackText.value = ''
      if (!oauthSessionId.value || !oauthAuthorizeUrl.value) {
        throw new Error('后端没有返回完整的 OAuth 授权会话')
      }
      window.open(oauthAuthorizeUrl.value, '_blank', 'noopener,noreferrer')
      toast.success('OAuth 授权链接已生成')
    } catch (error) {
      options.setError('生成 OAuth 授权链接失败', error)
    } finally {
      importBusy.value = false
    }
  }

  function openOAuthAuthorizeUrl() {
    if (!oauthAuthorizeUrl.value) {
      void startOAuthLogin()
      return
    }
    window.open(oauthAuthorizeUrl.value, '_blank', 'noopener,noreferrer')
  }

  async function copyOAuthAuthorizeUrl() {
    const value = oauthAuthorizeUrl.value.trim()
    if (!value) {
      toast.warning('请先生成 OAuth 授权链接')
      return
    }
    try {
      await navigator.clipboard.writeText(value)
      toast.success('授权链接已复制')
    } catch (error) {
      options.setError('复制 OAuth 授权链接失败', error)
    }
  }

  async function finishOAuthLogin() {
    const sessionId = oauthSessionId.value.trim()
    const callback = oauthCallbackText.value.trim()
    if (!sessionId) {
      toast.warning('请先生成 OAuth 授权链接')
      return
    }
    if (!callback) {
      toast.warning('请先粘贴 callback URL 或 code')
      return
    }

    importBusy.value = true
    try {
      const result = await accountImportsApi.finishOAuthLogin(sessionId, callback, targetGroupParam())
      await options.loadData({ silentErrorToast: true })
      const added = Number(result.added || 0)
      const skipped = Number(result.skipped || 0)
      const refreshed = Number(result.refreshed || 0)
      const errors = Array.isArray(result.errors) ? result.errors.length : 0
      if (errors > 0) {
        toast.warning(`OAuth 登录导入完成：新增 ${added}，跳过 ${skipped}，刷新 ${refreshed}，异常 ${errors}`)
      } else {
        toast.success(`OAuth 登录导入完成：新增 ${added}，跳过 ${skipped}，刷新 ${refreshed}`)
      }
      oauthEmailHint.value = ''
      oauthCallbackText.value = ''
      oauthSessionId.value = ''
      oauthAuthorizeUrl.value = ''
      oauthRedirectUriPrefix.value = ''
    } catch (error) {
      options.setError('OAuth 登录导入失败', error)
    } finally {
      importBusy.value = false
    }
  }

  async function importLocalCPAFiles(files: FileList | File[] | null | undefined) {
    const fileList = Array.from(files || [])
    if (!fileList.length) return
    importBusy.value = true
    try {
      const tokens: string[] = []
      for (const file of fileList) {
        const text = await file.text()
        tokens.push(...parseCPAJsonTokens(text, file.name))
      }
      importBusy.value = false
      await importTokenBatch(tokens, 'cpa_json', '导入 CPA JSON 文件')
    } catch (error) {
      options.setError('导入 CPA JSON 文件失败', error)
    } finally {
      importBusy.value = false
    }
  }

  return {
    importBusy,
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
    setImportMode,
    openImportModal,
    closeImportModal,
    importManualTokenText,
    importTokenTextFile,
    importSessionJson,
    startOAuthLogin,
    openOAuthAuthorizeUrl,
    copyOAuthAuthorizeUrl,
    finishOAuthLogin,
    importLocalCPAFiles,
  }
}
