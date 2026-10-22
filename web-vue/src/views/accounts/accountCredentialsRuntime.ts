import { ref } from 'vue'

import { accountDisplayLabel, accountsApi, type Account } from '@/api/accounts'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

export type AccountCredentialKind = 'access_token' | 'refresh_token'

/**
 * 查看 / 复制账号凭据（仅管理员；后端每次读取都会写审计日志）。
 * 凭据只在弹窗打开期间保存在内存里，关闭即清除，不进入账号列表。
 */
export function useAccountCredentialsRuntime() {
  const toast = useToast()
  const opened = ref(false)
  const accountId = ref('')
  const accountLabel = ref('')
  const hasRefreshToken = ref(false)
  const values = ref<Record<AccountCredentialKind, string>>({ access_token: '', refresh_token: '' })
  const loading = ref<Record<AccountCredentialKind, boolean>>({ access_token: false, refresh_token: false })
  const errors = ref<Record<AccountCredentialKind, string>>({ access_token: '', refresh_token: '' })

  function reset() {
    values.value = { access_token: '', refresh_token: '' }
    errors.value = { access_token: '', refresh_token: '' }
    loading.value = { access_token: false, refresh_token: false }
  }

  function open(item: Pick<Account, 'id' | 'has_refresh_token'>) {
    reset()
    accountId.value = item.id
    accountLabel.value = accountDisplayLabel(item.id)
    hasRefreshToken.value = Boolean(item.has_refresh_token)
    opened.value = true
  }

  function close() {
    opened.value = false
    accountId.value = ''
    reset()
  }

  async function reveal(kind: AccountCredentialKind): Promise<string> {
    if (values.value[kind]) return values.value[kind]
    loading.value = { ...loading.value, [kind]: true }
    errors.value = { ...errors.value, [kind]: '' }
    try {
      const value = kind === 'access_token'
        ? await accountsApi.getAccessToken(accountId.value)
        : await accountsApi.getRefreshToken(accountId.value)
      values.value = { ...values.value, [kind]: value }
      return value
    } catch (error) {
      errors.value = { ...errors.value, [kind]: errorMessage(error) }
      return ''
    } finally {
      loading.value = { ...loading.value, [kind]: false }
    }
  }

  async function copy(kind: AccountCredentialKind) {
    const value = await reveal(kind)
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      toast.success(kind === 'access_token' ? 'Access Token 已复制' : 'Refresh Token 已复制')
    } catch (error) {
      toast.error(`复制失败：${errorMessage(error)}`)
    }
  }

  return { opened, accountId, accountLabel, hasRefreshToken, values, loading, errors, open, close, reveal, copy }
}
