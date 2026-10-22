import { ref } from 'vue'

import { accountDisplayLabel, accountsApi, type Account } from '@/api/accounts'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

type CodexBindRuntimeOptions = {
  loadData: (options?: { silentErrorToast?: boolean }) => Promise<void>
}

/**
 * 绑定 Codex 登录：给已有账号额外保存一套 Codex 客户端 token，只给 codex-gpt-image-2 使用，
 * 不改动账号原有的 access / refresh token。
 * 注意：该流程移植自参考项目，上游自述未端到端验证，本项目也未做真实联网验证。
 */
export function useAccountCodexBindRuntime(options: CodexBindRuntimeOptions) {
  const toast = useToast()
  const opened = ref(false)
  const busy = ref(false)
  const accountId = ref('')
  const accountLabel = ref('')
  const emailHint = ref('')
  const sessionId = ref('')
  const authorizeUrl = ref('')
  const redirectPrefix = ref('')
  const callback = ref('')
  const bound = ref(false)

  function open(item: Pick<Account, 'id' | 'email' | 'codex_bound'>) {
    accountId.value = item.id
    accountLabel.value = accountDisplayLabel(item.id)
    emailHint.value = item.email || ''
    sessionId.value = ''
    authorizeUrl.value = ''
    redirectPrefix.value = ''
    callback.value = ''
    bound.value = Boolean(item.codex_bound)
    opened.value = true
  }

  function close() {
    if (busy.value) return
    opened.value = false
    sessionId.value = ''
    authorizeUrl.value = ''
    callback.value = ''
  }

  async function start() {
    busy.value = true
    try {
      const result = await accountsApi.startCodexOAuth(emailHint.value)
      sessionId.value = String(result.session_id || '')
      authorizeUrl.value = String(result.authorize_url || '')
      redirectPrefix.value = String(result.redirect_uri_prefix || '')
      callback.value = ''
      if (!sessionId.value || !authorizeUrl.value) throw new Error('后端没有返回完整的授权会话')
      window.open(authorizeUrl.value, '_blank', 'noopener,noreferrer')
      toast.success('Codex 授权链接已生成')
    } catch (error) {
      toast.error(`生成 Codex 授权链接失败：${errorMessage(error)}`)
    } finally {
      busy.value = false
    }
  }

  function openAuthorizeUrl() {
    if (authorizeUrl.value) window.open(authorizeUrl.value, '_blank', 'noopener,noreferrer')
    else void start()
  }

  async function copyAuthorizeUrl() {
    if (!authorizeUrl.value) {
      toast.warning('请先生成授权链接')
      return
    }
    try {
      await navigator.clipboard.writeText(authorizeUrl.value)
      toast.success('授权链接已复制')
    } catch (error) {
      toast.error(`复制失败：${errorMessage(error)}`)
    }
  }

  async function finish() {
    if (!sessionId.value) {
      toast.warning('请先生成授权链接')
      return
    }
    if (!callback.value.trim()) {
      toast.warning('请先粘贴 callback 地址')
      return
    }
    busy.value = true
    try {
      const result = await accountsApi.finishCodexOAuth(sessionId.value, callback.value.trim(), accountId.value)
      toast.success(`Codex 登录已绑定到 ${result.email || accountLabel.value}${result.codex_plan_type ? `（${result.codex_plan_type}）` : ''}`)
      bound.value = true
      sessionId.value = ''
      authorizeUrl.value = ''
      callback.value = ''
      opened.value = false
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      toast.error(`绑定 Codex 登录失败：${errorMessage(error)}`)
    } finally {
      busy.value = false
    }
  }

  return {
    opened, busy, accountLabel, emailHint, sessionId, authorizeUrl, redirectPrefix, callback, bound,
    open, close, start, openAuthorizeUrl, copyAuthorizeUrl, finish,
  }
}
