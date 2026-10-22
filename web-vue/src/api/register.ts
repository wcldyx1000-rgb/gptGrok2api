import apiClient from './client'
import type { RegisterEgressConfig } from './egress'

export type OutlookMailboxParseStats = {
  raw_lines?: number
  non_empty?: number
  valid?: number
  duplicates?: number
  invalid?: number
  skipped?: number
  existing_total?: number
  saved_total?: number
  issues?: Array<{
    line?: number
    reason?: string
    email?: string
  }>
  [key: string]: unknown
}

export type OutlookFailedMailbox = {
  id: string
  email: string
  reason?: string
  updated_at?: string
}

export type OutlookPoolState =
  | 'unused'
  | 'leased'
  | 'used'
  | 'failed'
  | 'family_used'
  | 'login_required'
  | 'token_invalid'

/** 逐邮箱投影：邮箱已脱敏，只有 has_* 布尔，不含密码/令牌/client_id/租约令牌 */
export type OutlookPoolEntry = {
  id: string
  email: string
  alias_of: string
  is_alias: boolean
  state: OutlookPoolState | string
  inherited: boolean
  reason: string
  updated_at: string
  leased_at: string
  lease_stale: boolean
  cooling: boolean
  use_count: number
  has_password: boolean
  has_client_id: boolean
  has_refresh_token: boolean
  resettable: boolean
  deletable: boolean
}

export type OutlookPoolEntriesResponse = {
  provider_id: string
  items: OutlookPoolEntry[]
  total: number
  page: number
  page_size: number
  pages: number
  state_counts: Record<string, number>
  stats: Record<string, number>
}

export type OutlookPoolEntriesQuery = {
  provider_id: string
  q?: string
  state?: string
  page?: number
  page_size?: number
}

export type RegisterProvider = {
  id?: string
  provider_id?: string
  enable?: boolean
  type?: string
  label?: string
  api_base?: string
  api_key?: string
  admin_key?: string
  admin_email?: string
  admin_password?: string
  ddg_token?: string
  cf_inbox_jwt?: string
  cf_api_base?: string
  cf_api_key?: string
  /** 凭据只写不读：后端返回空值并附带 `<key>_set`，保存时留空表示沿用已存值 */
  api_key_set?: boolean
  admin_key_set?: boolean
  admin_password_set?: boolean
  ddg_token_set?: boolean
  cf_inbox_jwt_set?: boolean
  cf_api_key_set?: boolean
  cf_auth_mode?: string
  cf_create_path?: string
  cf_messages_path?: string
  default_domain?: string
  key_mode?: 'public' | 'custom' | string
  local_compose?: boolean
  project?: string
  purpose?: string
  keyword?: string
  wait_ms?: number
  use_proxy?: boolean
  email_prefix?: string
  subdomain?: string | string[]
  domain?: string[]
  cf_domain?: string[]
  random_subdomain?: boolean
  wildcard?: boolean
  expiry_time?: number
  mailboxes?: string
  mailboxes_count?: number
  mailboxes_base_count?: number
  mailboxes_alias_count?: number
  mailboxes_preview?: string[]
  mailboxes_failed?: OutlookFailedMailbox[]
  alias_enabled?: boolean
  alias_per_email?: number
  alias_prefix?: string
  alias_include_original?: boolean
  mailboxes_stats?: {
    unused?: number
    in_use?: number
    used?: number
    login_required?: number
    token_invalid?: number
    failed?: number
    available?: number
    busy?: number
    retryable?: number
    invalid?: number
    abnormal?: number
    [key: string]: number | undefined
  }
  mailboxes_parse_stats?: OutlookMailboxParseStats
  mode?: 'graph' | 'imap' | 'auto' | string
  imap_host?: string
  message_limit?: number
  [key: string]: unknown
}

export type RegisterTarget = 'openai' | 'grok'

export type GrokTurnstileProvider = 'yescaptcha' | '2captcha' | 'local' | 'custom'

export type GrokOAuthDeliveryConfig = {
  sub2api: OpenAISub2APISyncConfig
  cpa: {
    enabled: boolean
    pool_id: string
  }
}

export type GrokRegisterConfig = {
  signup_flow: 'xconsole' | 'legacy' | string
  browser_flow_enabled: boolean
  provider: GrokTurnstileProvider | string
  api_key: string
  api_key_set?: boolean
  api_base: string
  request_timeout: number
  captcha_timeout: number
  captcha_poll_interval: number
  local_concurrency: number
  create_path: string
  result_path: string
  max_mail_retries: number
  turnstile_prefetch: boolean
  xai_cli_oauth_enabled: boolean
  xai_cli_oauth_flow: 'device' | 'pkce_reference'
  xai_cli_pkce_reference_dir: string
  oauth_delivery: GrokOAuthDeliveryConfig
  grok2api_enabled: boolean
  grok2api_api_base: string
  grok2api_admin_key: string
  grok2api_pool: 'auto' | 'basic' | 'super' | 'heavy' | string
  grok2api_auto_nsfw: boolean
  grok2api_verify_on_import: boolean
  grok2api_timeout: number
  [key: string]: unknown
}

export type OpenAICheckoutConfig = {
  enabled: boolean
  channel: 'upi' | 'pix'
  /** Pix only: choose one of the three independently implemented flows. */
  pix_protocol?: 'enhanced' | 'reference' | 'standalone'
  country: string
  currency: string
  checkout_ui_mode: 'custom'
  threads: number
  checkout_proxy_enabled: boolean
  checkout_proxy_url: string
  promotion_proxy_enabled: boolean
  promotion_proxy_url: string
  provider_proxy_enabled: boolean
  provider_proxy_url: string
  /** Keep retrying final-link protocols with a fresh proxy allocation after a failed round. */
  continuous_retry?: boolean
  /** Legacy single-stage Checkout proxy fields accepted from stored configs. */
  residential_proxy_enabled?: boolean
  residential_proxy_url?: string
}

export type OpenAISub2APISyncConfig = {
  /** Whether newly registered OpenAI accounts should be sent to the selected Sub2API server. */
  enabled: boolean
  /** Saved Sub2API connection ID. */
  server_id: string
  /** Use an existing remote group or let the remote side create/find a named group. */
  group_mode: 'existing' | 'custom' | string
  /** Existing remote group ID when `group_mode` is `existing`. */
  group_id: string
  /** Desired remote group name when `group_mode` is `custom`. */
  group_name: string
}

export type OpenAICPASyncConfig = {
  /** Whether newly registered OpenAI accounts should be uploaded to the selected CPA server. */
  enabled: boolean
  /** Saved CPA connection ID. */
  pool_id: string
}

/** One Checkout extraction attempt reported by the registration runtime. */
export type CheckoutTask = {
  task_id?: string
  index?: number
  email?: string
  status?: string
  stage?: string
  /** Credential-free description of the exact protocol milestone. */
  progress_detail?: string
  /** Current or most recently completed extraction round. */
  attempt?: number
  /** Timestamp at which a retrying task will be picked up again. */
  next_retry_at?: string
  /** Payment-link extraction channel selected for this task. */
  channel?: 'upi' | 'pix'
  payment_link?: string
  error_short?: string
  updated_at?: string
  [key: string]: unknown
}

export type LegacyRegisterConfig = {
  target: RegisterTarget | string
  grok: GrokRegisterConfig
  checkout: OpenAICheckoutConfig
  sub2api_sync: OpenAISub2APISyncConfig
  cpa_sync: OpenAICPASyncConfig
  agent_identity_archive: {
    enabled: boolean
  }
  confirm_refresh_token: boolean
  egress?: RegisterEgressConfig
  mail: {
    request_timeout?: number
    wait_timeout?: number
    wait_interval?: number
    user_agent?: string
    providers?: RegisterProvider[]
    [key: string]: unknown
  }
  proxy: string
  total: number
  threads: number
  mode: 'total' | 'quota' | 'available' | string
  target_quota: number
  target_available: number
  check_interval: number
  enabled: boolean
  stats?: {
    success?: number
    fail?: number
    done?: number
    running?: number
    threads?: number
    elapsed_seconds?: number
    avg_seconds?: number
    success_rate?: number
    current_quota?: number
    current_available?: number
    pool_ok?: number
    pool_fail?: number
    pool_usable_rate?: number
    [key: string]: unknown
  }
  logs?: Array<{
    time: string
    text: string
    level?: string
  }>
  grok_oauth_logs?: Array<{
    time: string
    text: string
    level?: string
  }>
  checkout_logs?: Array<{
    time: string
    text: string
    level?: string
  }>
  checkout_tasks?: CheckoutTask[]
  checkout_retries_active?: boolean
  checkout_retry_job_count?: number
}

export type GptMailStatus = {
  ok?: boolean
  key_mode?: string
  api_base?: string
  source?: string
  is_active?: boolean
  daily_limit?: number | null
  used_today?: number | null
  remaining_today?: number | null
  total_limit?: number | null
  total_usage?: number | null
  remaining_total?: number | null
  reset_at?: string
  seconds_until_reset?: number | null
  checked_at?: string
  key_hint?: string
  local_compose?: boolean
  default_domain?: string
}

export const registerApi = {
  getConfig() {
    return apiClient.get<any, { register: LegacyRegisterConfig }>('/api/register')
  },
  updateConfig(payload: Partial<LegacyRegisterConfig>) {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register', payload)
  },
  startLegacy() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/start')
  },
  stopLegacy() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/stop')
  },
  stopCheckoutRetries() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/checkout-retries/stop')
  },
  clearCheckoutHistory() {
    return apiClient.post<any, { register: LegacyRegisterConfig; removed: number }>('/api/register/checkout-history/clear')
  },
  resetLegacy() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/reset')
  },
  resetOutlookPool(scope: 'all' | 'retryable' | 'invalid' | 'unused' | 'failed' = 'all') {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/outlook-pool/reset', { scope })
  },
  retrySelectedOutlookMailboxes(providerId: string, mailboxIds: string[]) {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/outlook-pool/retry-selected', {
      provider_id: providerId,
      mailbox_ids: mailboxIds,
    })
  },
  listOutlookPoolEntries(params: OutlookPoolEntriesQuery) {
    return apiClient.get<any, OutlookPoolEntriesResponse>('/api/register/outlook-pool/entries', { params })
  },
  resetOutlookMailbox(providerId: string, mailboxId: string) {
    return apiClient.post<any, OutlookPoolEntriesResponse>('/api/register/outlook-pool/mailboxes/reset', {
      provider_id: providerId,
      mailbox_id: mailboxId,
    })
  },
  deleteOutlookMailbox(providerId: string, mailboxId: string) {
    return apiClient.post<any, OutlookPoolEntriesResponse>('/api/register/outlook-pool/mailboxes/delete', {
      provider_id: providerId,
      mailbox_id: mailboxId,
    })
  },
  getGptMailStatus(provider: RegisterProvider, force = true) {
    return apiClient.post<any, { status: GptMailStatus }>('/api/register/gptmail/status', { provider, force })
  },
  refreshGptMailKey(provider: RegisterProvider, force = true) {
    return apiClient.post<any, { status: GptMailStatus }>('/api/register/gptmail/refresh-key', { provider, force })
  },
  exportGrokAccounts(format: 'cpa' | 'sub2api' = 'sub2api') {
    return apiClient.get<never, Blob>('/api/register/grok/accounts/export', {
      params: { format },
      responseType: 'blob',
    })
  },
}
