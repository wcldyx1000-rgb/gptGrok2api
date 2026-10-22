import apiClient from './client'
import type { ProxyGroup } from './proxy'

export type AccountLane = 'fast' | 'thinking' | 'pro'
export type AccountBackendStatus = '正常' | '限流' | '异常' | '禁用'
export type AccountStatusCategory = 'normal' | 'limited' | 'abnormal' | 'disabled'
export type CheckoutLinkStatus = 'ready' | 'failed' | 'pending' | 'processing' | ''
export type AccountHealthLevel = 'healthy' | 'warning' | 'danger' | 'idle'
export type AccountHealthTone = 'success' | 'warning' | 'error' | 'neutral'
export type AccountHealthFilter = 'all' | 'risk' | AccountHealthLevel

export interface AccountPoolWarning {
  code: string
  tone: 'warning' | 'error'
  message: string
}

export interface AccountPoolHealth {
  level: 'healthy' | 'warning' | 'danger'
  label: string
  tone: AccountHealthTone
  warnings: AccountPoolWarning[]
  suggested_additional_accounts: number
  metrics: {
    total_accounts: number
    ready_accounts: number
    danger_accounts: number
    warning_accounts: number
    demand_1h: number
    capacity_1h: number
    utilization: number | null
    waits_1h: number
    avg_wait_seconds: number
    timeouts_1h: number
    avg_hold_seconds: number
    observed_seconds: number
  }
}

export interface AccountPoolHealthResponse {
  pool: AccountPoolHealth | Record<string, never>
  counts: Partial<Record<AccountHealthLevel, number>>
}

export interface AccountTestResult {
  ok: boolean
  duration_ms: number
  message?: string
  error?: string
}

/** 账号真实测试：probe 只探测远程信息；chat / image 发起真实对话或画图。 */
export type AccountTestMode = 'probe' | 'chat' | 'image'

export interface AccountCapabilityTestResult {
  status: 'success' | 'failed'
  status_label: string
  tone: 'success' | 'danger'
  account_id: string
  account_label: string
  mode: 'chat' | 'image'
  mode_label: string
  model: string
  duration_ms: number
  content: string
  quota_before_label: string
  quota_after_label: string
  quota_deducted: boolean
  error_code: string
  error_message: string
}

/** 跨分页选择范围：filter 为当前筛选结果，excluded_account_ids 为取消勾选的账号。 */
export interface AccountSelectionScope {
  mode: 'explicit' | 'filter' | 'all'
  account_ids?: string[]
  excluded_account_ids?: string[]
  keyword?: string
  status?: string
  group_id?: string
  health?: string
  /** “更多筛选”取值，与 GET /api/accounts 的 flag 参数一致。 */
  flag?: string
}

export interface AccountSelectionPreview {
  matching_count: number
  selected_count: number
  excluded_account_ids: string[]
  account_ids: string[]
  errors: Array<{ id: string; code: string; message: string }>
}

export type AccountExportFormat = 'json' | 'zip' | 'cpa' | 'sub2api' | 'agent_identity' | 'txt' | 'backup'

export interface CodexOAuthStartResponse {
  session_id: string
  authorize_url: string
  expires_in: string
  redirect_uri_prefix: string
}

export interface CodexOAuthBindResult {
  id: string
  email: string
  codex_plan_type: string
  codex_token_expires_at: number
}

export type AccountPresentationTone = 'neutral' | 'success' | 'warning' | 'error' | 'info'

export interface AccountEgress {
  mode: 'slot' | 'direct' | 'group' | 'profile' | 'custom' | 'default'
  slot: number | null
  label: string
  source: 'account' | 'group' | 'default'
  source_label: string
  usable: boolean
  switch_to: string | null
  switch_to_label: string
  switch_at: number | null
}

export interface AccountEgressSwitchResult {
  target: string
  due_at: string
  scheduled: string[]
  applied: string[]
  skipped: Array<{ token: string; reason: string }>
}

export interface Account {
  /** 后端稳定管理 ID（acct_...），token 轮换后不变；所有账号操作都用它定位。 */
  id: string
  access_token?: string
  /** 后端是否保存了 refresh_token（token 本身不在列表里返回）。 */
  has_refresh_token?: boolean
  /** 是否已绑定 Codex 登录（仅 codex-gpt-image-2 使用，不影响账号原有 token）。 */
  codex_bound?: boolean
  codex_plan_type?: string
  codex_token_expires_at?: number
  backend_status?: string
  status_category?: AccountStatusCategory
  status_label?: string
  email?: string
  user_id?: string
  type?: string
  source_type?: string
  agent_identity_status?: string
  agent_runtime_id?: string
  survival_status?: string
  survival_alive?: boolean | null
  survival_last_checked_at?: string
  survival_last_confirmed_at?: string
  survival_observed_seconds?: number
  survival_plan_type?: string
  proxy?: string
  group_id?: string
  checkout_link_status?: CheckoutLinkStatus
  // After final-link extraction, `checkout_url` is kept as a compatibility
  // alias. Keep the intermediate ChatGPT/hosted URLs separate so callers
  // never mistake a checkout bootstrap URL for a payment link.
  checkout_final_url?: string
  checkout_final_kind?: string
  checkout_url?: string
  checkout_hosted_url?: string
  checkout_chatgpt_url?: string
  checkout_session_id?: string
  checkout_processor_entity?: string
  checkout_channel?: string
  checkout_country?: string
  checkout_currency?: string
  checkout_ui_mode?: 'hosted' | 'custom' | string
  checkout_provider?: string
  checkout_kind?: string
  checkout_payment_method?: string
  checkout_amount_minor?: number
  checkout_is_free_trial?: boolean
  checkout_hosted_instructions_url?: string
  checkout_qr_image_url_png?: string
  checkout_qr_image_url_svg?: string
  checkout_qr_code_data?: string
  checkout_qr_expires_at?: string | number
  checkout_qr_expires_at_unix?: number
  checkout_qr_ttl_seconds?: number
  checkout_plan?: string
  checkout_session_status?: string
  checkout_payment_status?: string
  checkout_promo_campaign?: string
  checkout_promo_fallback_used?: boolean
  checkout_created_at?: string
  checkout_last_attempt_at?: string
  checkout_attempt_count?: number
  checkout_last_error?: string
  checkout_last_error_at?: string
  checkout_upstream_status?: number
  quota?: number
  image_quota_unknown?: boolean
  /** GPT pool auto pause: ISO time until which the account is deprioritised. */
  auto_paused_until?: string
  auto_pause_reason?: string
  /** Set when the refresh_token is rejected and automatic relogin is pending. */
  relogin_needed_at?: string
  relogin_attempts?: number
  last_relogin_error?: string
  // ── 展示字段（services/account_view.py）：只有布尔和时间，没有任何明文凭据 ──
  access_token_status?: 'valid' | 'expiring' | 'invalid'
  access_token_label?: string
  access_token_tone?: AccountPresentationTone
  access_token_issued_at?: number | null
  access_token_expires_at?: number | null
  refresh_token_status?: 'valid' | 'missing' | 'invalid'
  refresh_token_label?: string
  refresh_token_tone?: AccountPresentationTone
  can_refresh_access_token?: boolean
  credential_availability?: 'usable' | 'recoverable' | 'unavailable'
  credential_availability_label?: string
  credential_availability_tone?: AccountPresentationTone
  refresh_token_invalid_at?: number | null
  last_token_refresh_at?: number | null
  last_token_refresh_error?: string | null
  last_token_refresh_error_at?: number | null
  rescue_code?: 'refresh' | 'outlook' | 'exhausted' | 'manual' | 'none'
  rescue_label?: string
  rescue_tone?: AccountPresentationTone
  rescue_detail?: string
  has_outlook_credentials?: boolean
  has_password?: boolean
  has_totp?: boolean
  relogin_failures?: number
  relogin_last_error?: string | null
  quota_remaining?: number
  quota_unknown?: boolean
  quota_unlimited?: boolean
  quota_state?: 'unlimited' | 'unknown' | 'exhausted' | 'available'
  quota_label?: string
  quota_reset_at?: number | null
  quota_source?: 'estimated' | 'remote' | 'unknown'
  quota_synced_at?: number | null
  quota_reset_idle?: boolean
  group_name?: string
  proxy_label?: string
  status_raw_error?: string
  egress?: AccountEgress | null
  /** Inferred GPT image account health (see services/account_health.py). */
  /** Live scheduling view from services/account_service.py scheduling_view (Unix seconds). */
  scheduling?: AccountScheduling
  health_level?: AccountHealthLevel
  health_label?: string
  health_tone?: AccountHealthTone
  health_reasons?: string[]
  recent_uses_1h?: number
  recent_uses_24h?: number
  name: string
  status?: 'ready' | 'incomplete' | 'disabled' | 'invalid' | 'auto_disabled' | 'cooling' | 'backoff'
  status_reason?: string
  status_reason_code?:
    | 'disabled'
    | 'snlm0e_refresh_failed'
    | 'account_invalid'
    | 'pro_cooldown'
    | 'video_cooldown'
    | 'lane_backoff'
    | 'lane_degraded'
    | 'image_generation_unavailable'
    | 'image_degraded_to_fast'
    | 'parse_failure'
    | 'text_pending'
    | 'upstream_error'
    | 'image_quota_exhausted'
    | ''
  cookie: string
  snlm0e: string
  push_id: string
  push_id_set?: boolean
  enabled: boolean
  auto_disabled?: boolean
  auto_disabled_reason?: string
  auto_disabled_at?: number
  health_fail_streak?: number
  last_health_check_at?: number
  lanes: AccountLane[]
  model_ids: Record<AccountLane, string>
  failure_count: number
  success_count: number
  last_error: string
  last_error_kind?:
    | 'quota_exhausted'
    | 'media_pending'
    | 'media_generation_unavailable'
    | 'media_degraded'
    | 'lane_degraded'
    | 'parse_failure'
    | 'text_pending'
    | 'upstream_error'
    | 'auth_invalid'
    | ''
  pro_cooldown_until?: number
  pro_cooldown_seconds?: number
  pro_cooldown_local?: string
  pro_cooldown_reason?: string
  video_cooldown_until?: number
  video_cooldown_seconds?: number
  video_cooldown_local?: string
  video_cooldown_reason?: string
  snlm0e_refreshed_at?: number
  snlm0e_refresh_fail_count?: number
  snlm0e_last_refresh_error?: string
  snlm0e_next_refresh_after?: number
  daily_usage?: {
    fast: number
    thinking: number
    pro: number
    image: number
    music: number
    video: number
  }
  quota_limits?: {
    enabled: boolean
    fast: number
    thinking: number
    pro: number
    image: number
    music: number
    video: number
  }
  quota_summary?: {
    enabled: boolean
    period: string
    reset_in_seconds: number
    conversation: { used: number; limit: number; remaining: number; limited: boolean }
    pro: { used: number; limit: number; remaining: number; limited: boolean }
    image: { used: number; limit: number; remaining: number; limited: boolean }
    music: { used: number; limit: number; remaining: number; limited: boolean }
    video: { used: number; limit: number; remaining: number; limited: boolean }
  }
  lane_backoff_until?: Partial<Record<AccountLane, number>>
  lane_backoff_reason?: Partial<Record<AccountLane, string>>
  lane_backoff_kind?: Partial<Record<AccountLane, string>>
  lane_backoff_summary?: {
    active: boolean
    lanes: AccountLane[]
    max_wait_seconds: number
    summary: string
    items: Array<{
      lane: AccountLane
      wait_seconds: number
      until: number
      until_local: string
      reason: string
      kind: string
      label: string
    }>
  }
  last_used_at: number
  created_at: number
  restore_at?: number
  updated_at: number
  is_demo?: boolean
}

export interface ReverseMonitorLane {
  total: number
  enabled: number
  available: number
}

export interface ReverseMonitor {
  total_accounts: number
  lanes: Record<AccountLane, ReverseMonitorLane>
}

export interface AccountsResponse {
  accounts: Account[]
  total?: number
  all_total?: number
  /** 调度状态汇总（除“状态”外的其它筛选生效）：code -> 数量。 */
  availability_summary?: Record<string, number>
  page?: number
  page_size?: number
}

export interface AccountsBulkResponse {
  status: string
  success_count: number
  errors: string[]
}

export type ReverseLane = AccountLane
export type ReverseAccount = Account
export type ReverseAccountsResponse = AccountsResponse
export type ReverseAccountsBulkResponse = AccountsBulkResponse

export interface AccountGroup {
  id: string
  name: string
  proxy?: string
  proxy_group_id?: string
  enabled: boolean
  notes?: string
  account_count?: number
}

export interface AccountGroupPayload extends Partial<AccountGroup> {
  create_only?: boolean
}

export interface AccountScheduling {
  new_account_stage: '' | 'cooldown' | 'ramp'
  new_account_joined_at: number | null
  cooldown_until: number | null
  ramp_until: number | null
  ramp_used_today: number
  ramp_daily_limit: number
  ramp_exhausted: boolean
  ramp_resume_at: number | null
  failure_cooldown_until: number | null
  failure_streak: number
  auto_paused_until: number | null
  auto_pause_reason: string
  image_inflight: number
  image_concurrency: number
  text_inflight: number
  text_concurrency: number
  auth_pending: boolean
  auth_pending_event: string
  auth_pending_since: number | null
  credential_status: 'usable' | 'recoverable' | 'unavailable'
  access_token_status: 'valid' | 'expiring' | 'invalid'
  refresh_token_status: 'valid' | 'missing' | 'invalid'
  image_schedulable: boolean
}

/** One per-account line of an operation (services/account_service.py update_refresh_progress). */
export interface AccountOperationEvent {
  sequence: number
  timestamp: string
  account_id: string
  account_label: string
  action: string
  status: 'info' | 'success' | 'failed' | 'skipped'
  message: string
}

export interface AccountRefreshProgress {
  total: number
  processed: number
  done: boolean
  error?: string | null
  /** Present on /api/accounts/operations/{id}. */
  status?: 'running' | 'success' | 'failed'
  percent?: number
  events?: AccountOperationEvent[]
  last_sequence?: number
  status_counts?: Record<string, number>
  total_quota?: number
  result?: {
    refreshed?: number
    errors?: Array<{ token?: string; error?: string } | string>
    items?: BackendAccount[]
  } | null
}

type BackendAccount = Record<string, any>

function normalizeScheduling(raw: unknown): AccountScheduling | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const s = raw as Record<string, any>
  const num = (value: unknown): number | null => (Number(value) > 0 ? Number(value) : null)
  const count = (value: unknown, fallback = 0): number => Math.max(0, Number(value ?? fallback) || 0)
  const stage = s.new_account_stage === 'cooldown' || s.new_account_stage === 'ramp' ? s.new_account_stage : ''
  return {
    new_account_stage: stage,
    new_account_joined_at: num(s.new_account_joined_at),
    cooldown_until: num(s.cooldown_until),
    ramp_until: num(s.ramp_until),
    ramp_used_today: count(s.ramp_used_today),
    ramp_daily_limit: count(s.ramp_daily_limit),
    ramp_exhausted: Boolean(s.ramp_exhausted),
    ramp_resume_at: num(s.ramp_resume_at),
    failure_cooldown_until: num(s.failure_cooldown_until),
    failure_streak: count(s.failure_streak),
    auto_paused_until: num(s.auto_paused_until),
    auto_pause_reason: cleanString(s.auto_pause_reason),
    image_inflight: count(s.image_inflight),
    image_concurrency: count(s.image_concurrency, 1),
    text_inflight: count(s.text_inflight),
    text_concurrency: count(s.text_concurrency, 1),
    auth_pending: Boolean(s.auth_pending),
    auth_pending_event: cleanString(s.auth_pending_event),
    auth_pending_since: num(s.auth_pending_since),
    credential_status: ['recoverable', 'unavailable'].includes(s.credential_status) ? s.credential_status : 'usable',
    access_token_status: ['expiring', 'invalid'].includes(s.access_token_status) ? s.access_token_status : 'valid',
    refresh_token_status: ['missing', 'invalid'].includes(s.refresh_token_status) ? s.refresh_token_status : 'valid',
    image_schedulable: s.image_schedulable !== false,
  }
}

type BackendAccountsResponse = {
  items?: BackendAccount[]
  total?: number
  all_total?: number
  availability_summary?: Record<string, number>
  page?: number
  page_size?: number
}

type BackendAccountMutationResponse = {
  item?: BackendAccount
  items?: BackendAccount[]
  added?: number
  skipped?: number
  refreshed?: number
  updated?: number
  removed?: number
  errors?: Array<{ token?: string; error?: string } | string>
  verified?: number
  rejected?: AccountImportRejected[]
  group_id?: string
  groups?: AccountGroup[]
}

export type AccountImportRejected = {
  email?: string
  reason?: string
  status?: string
}

type AccountImportPayload = Record<string, unknown>
type AccountImportOptions = {
  refresh?: boolean
  returnItems?: boolean
  /** 入池前验证 refresh_token 与额度，不可用的账号不留在号池。 */
  verifyOnImport?: boolean
  /** undefined 不改分组；'' 归入未分组；其它为已存在的账号组 ID。 */
  targetGroupId?: string
  /** 导入完整备份：保留备份里的管理 ID 与全部字段。 */
  restore?: boolean
}
type AccountImportCleanupResponse = {
  checked?: number
  abnormal?: number
  removed?: number
}

const DEFAULT_LANES: AccountLane[] = ['fast', 'thinking', 'pro']
const EMPTY_MODEL_IDS: Record<AccountLane, string> = { fast: '', thinking: '', pro: '' }
export const ACCOUNT_BACKEND_STATUS_VALUES = ['正常', '限流', '异常', '禁用'] as const
const ACCOUNT_STATUS_CATEGORY_VALUES = ['normal', 'limited', 'abnormal', 'disabled'] as const
const STATUS_NORMAL: AccountBackendStatus = '正常'
const STATUS_DISABLED: AccountBackendStatus = '禁用'
const STATUS_LIMITED: AccountBackendStatus = '限流'
const STATUS_INVALID: AccountBackendStatus = '异常'

const accountTokenById = new Map<string, string>()
const accountLabelById = new Map<string, string>()

const MANAGEMENT_ID_PATTERN = /^acct_[0-9a-f]{24}$/

/** 后端稳定管理 ID；旧版展示 ID（邮箱等）不满足该格式。 */
export function isAccountManagementId(value: unknown): boolean {
  return MANAGEMENT_ID_PATTERN.test(String(value || '').trim().toLowerCase())
}

/** 把一组账号标识拆成 account_ids（管理 ID）与 access_tokens（旧版标识，兼容旧接口）。 */
function targetFields(values: readonly string[]): { account_ids: string[]; access_tokens: string[] } {
  const accountIds: string[] = []
  const accessTokens: string[] = []
  for (const raw of new Set(values.map((value) => String(value || '').trim()).filter(Boolean))) {
    if (isAccountManagementId(raw)) {
      accountIds.push(raw.toLowerCase())
      continue
    }
    const token = resolveToken(raw)
    if (token) accessTokens.push(token)
  }
  return { account_ids: accountIds, access_tokens: accessTokens }
}

/** 用于提示文案：优先显示邮箱，没有则显示 ID。 */
export function accountDisplayLabel(accountId: string): string {
  return accountLabelById.get(accountId) || accountId
}

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

export function normalizeAccountBackendStatus(
  value: unknown,
  fallback: AccountBackendStatus = STATUS_NORMAL,
): AccountBackendStatus {
  const raw = cleanString(value)
  return ACCOUNT_BACKEND_STATUS_VALUES.includes(raw as AccountBackendStatus) ? raw as AccountBackendStatus : fallback
}

function toTimestampSeconds(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.floor(value > 10_000_000_000 ? value / 1000 : value)
  }
  const raw = cleanString(value)
  if (!raw) return 0
  const parsed = Date.parse(raw.replace(' ', 'T'))
  return Number.isNaN(parsed) ? 0 : Math.floor(parsed / 1000)
}

function maskToken(token: string): string {
  if (!token) return ''
  if (token.length <= 12) return '********'
  return `${token.slice(0, 6)}...${token.slice(-4)}`
}

function isMaskedToken(value: string): boolean {
  return value.includes('...') || /^\*+$/.test(value)
}

function displayIdForAccount(item: BackendAccount, index: number, usedIds: Set<string>): string {
  const base = (
    cleanString(item.email)
    || cleanString(item.user_id)
    || cleanString(item.account_id)
    || `account-${index + 1}`
  ).slice(0, 96)
  let candidate = base
  let suffix = 2
  while (usedIds.has(candidate)) {
    candidate = `${base}#${suffix}`
    suffix += 1
  }
  usedIds.add(candidate)
  return candidate
}

function backendStatusToFrontend(item: BackendAccount): Pick<
  Account,
  'enabled' | 'status' | 'status_reason' | 'status_reason_code' | 'last_error_kind'
> {
  const rawStatus = cleanString(item.status)
  const quota = Number(item.quota ?? 0)
  const imageQuotaUnknown = Boolean(item.image_quota_unknown)
  const lastRefreshError = cleanString(item.last_refresh_error || item.last_token_refresh_error)

  if (rawStatus === STATUS_DISABLED || rawStatus.toLowerCase() === 'disabled') {
    return {
      enabled: false,
      status: 'disabled',
      status_reason: '账号禁用',
      status_reason_code: 'disabled',
      last_error_kind: '',
    }
  }

  if (rawStatus === STATUS_INVALID || rawStatus.toLowerCase() === 'invalid') {
    return {
      enabled: true,
      status: 'invalid',
      status_reason: lastRefreshError || '账号鉴权异常',
      status_reason_code: 'account_invalid',
      last_error_kind: 'auth_invalid',
    }
  }

  const invalidCount = Number(item.invalid_count ?? 0) || 0
  if (invalidCount >= 1 && lastRefreshError && rawStatus !== STATUS_LIMITED) {
    return {
      enabled: true,
      status: 'invalid',
      status_reason: '已检测到鉴权异常，已进入统一异常处理流程。',
      status_reason_code: 'account_invalid',
      last_error_kind: 'auth_invalid',
    }
  }

  if (rawStatus === STATUS_LIMITED) {
    return {
      enabled: true,
      status: 'cooling',
      status_reason: '远程确认图片额度已用完',
      status_reason_code: 'image_quota_exhausted',
      last_error_kind: 'quota_exhausted',
    }
  }

  return {
    enabled: true,
    status: 'ready',
    status_reason: lastRefreshError || (!imageQuotaUnknown && quota <= 0 ? '本地额度待远程刷新，以请求前预检结果为准' : ''),
    status_reason_code: '',
    last_error_kind: lastRefreshError ? 'upstream_error' : '',
  }
}

function normalizeAccountStatusCategory(value: unknown): AccountStatusCategory | undefined {
  const raw = cleanString(value)
  return ACCOUNT_STATUS_CATEGORY_VALUES.includes(raw as AccountStatusCategory)
    ? raw as AccountStatusCategory
    : undefined
}

function normalizeCheckoutLinkStatus(value: unknown): CheckoutLinkStatus {
  const raw = cleanString(value).toLowerCase()
  return ['ready', 'failed', 'pending', 'processing'].includes(raw)
    ? raw as CheckoutLinkStatus
    : ''
}

function optionalNonNegativeInteger(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '') return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : undefined
}

function optionalBoolean(value: unknown): boolean | undefined {
  if (value === true || String(value).toLowerCase() === 'true') return true
  if (value === false || String(value).toLowerCase() === 'false') return false
  return undefined
}

function normalizeHealthLevel(value: unknown): AccountHealthLevel | undefined {
  const level = cleanString(value)
  return level === 'healthy' || level === 'warning' || level === 'danger' || level === 'idle' ? level : undefined
}

function normalizeHealthTone(value: unknown): AccountHealthTone | undefined {
  const tone = cleanString(value)
  return tone === 'success' || tone === 'warning' || tone === 'error' || tone === 'neutral' ? tone : undefined
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T | undefined {
  const raw = cleanString(value)
  return (allowed as readonly string[]).includes(raw) ? raw as T : undefined
}

function optionalTimestamp(value: unknown): number | null {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : null
}

const PRESENTATION_TONES = ['neutral', 'success', 'warning', 'error', 'info'] as const

function normalizeEgress(value: unknown): AccountEgress | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  return {
    mode: oneOf(raw.mode, ['slot', 'direct', 'group', 'profile', 'custom', 'default'] as const) ?? 'default',
    slot: raw.slot === null || raw.slot === undefined ? null : Number(raw.slot) || null,
    label: cleanString(raw.label) || '默认出口',
    source: oneOf(raw.source, ['account', 'group', 'default'] as const) ?? 'default',
    source_label: cleanString(raw.source_label),
    usable: raw.usable !== false,
    switch_to: cleanString(raw.switch_to) || null,
    switch_to_label: cleanString(raw.switch_to_label),
    switch_at: optionalTimestamp(raw.switch_at),
  }
}

/** services/account_view.py 生成的凭据 / 救回 / 额度 / 出口展示字段。 */
function normalizeAccountDisplay(item: BackendAccount): Partial<Account> {
  const tone = (value: unknown) => oneOf(value, PRESENTATION_TONES)
  return {
    access_token_status: oneOf(item.access_token_status, ['valid', 'expiring', 'invalid'] as const),
    access_token_label: cleanString(item.access_token_label),
    access_token_tone: tone(item.access_token_tone),
    access_token_issued_at: optionalTimestamp(item.access_token_issued_at),
    access_token_expires_at: optionalTimestamp(item.access_token_expires_at),
    refresh_token_status: oneOf(item.refresh_token_status, ['valid', 'missing', 'invalid'] as const),
    refresh_token_label: cleanString(item.refresh_token_label),
    refresh_token_tone: tone(item.refresh_token_tone),
    can_refresh_access_token: Boolean(item.can_refresh_access_token),
    credential_availability: oneOf(item.credential_availability, ['usable', 'recoverable', 'unavailable'] as const),
    credential_availability_label: cleanString(item.credential_availability_label),
    credential_availability_tone: tone(item.credential_availability_tone),
    refresh_token_invalid_at: optionalTimestamp(item.refresh_token_invalid_at),
    last_token_refresh_at: optionalTimestamp(item.last_token_refresh_at),
    last_token_refresh_error: cleanString(item.last_token_refresh_error) || null,
    last_token_refresh_error_at: optionalTimestamp(item.last_token_refresh_error_at),
    rescue_code: oneOf(item.rescue_code, ['refresh', 'outlook', 'exhausted', 'manual', 'none'] as const),
    rescue_label: cleanString(item.rescue_label),
    rescue_tone: tone(item.rescue_tone),
    rescue_detail: cleanString(item.rescue_detail),
    has_outlook_credentials: Boolean(item.has_outlook_credentials),
    has_password: Boolean(item.has_password),
    has_totp: Boolean(item.has_totp),
    relogin_failures: Math.max(0, Number(item.relogin_failures || 0) || 0),
    relogin_last_error: cleanString(item.relogin_last_error) || null,
    quota_remaining: Math.max(0, Number(item.quota_remaining || 0) || 0),
    quota_unknown: Boolean(item.quota_unknown),
    quota_unlimited: Boolean(item.quota_unlimited),
    quota_state: oneOf(item.quota_state, ['unlimited', 'unknown', 'exhausted', 'available'] as const),
    quota_label: cleanString(item.quota_label),
    quota_reset_at: optionalTimestamp(item.quota_reset_at),
    quota_source: oneOf(item.quota_source, ['estimated', 'remote', 'unknown'] as const),
    quota_synced_at: optionalTimestamp(item.quota_synced_at),
    quota_reset_idle: Boolean(item.quota_reset_idle),
    group_name: cleanString(item.group_name),
    proxy_label: cleanString(item.proxy_label),
    status_raw_error: cleanString(item.status_raw_error),
    egress: normalizeEgress(item.egress),
  }
}

function mapBackendAccount(item: BackendAccount, index: number, usedIds: Set<string>): Account {
  const accessToken = cleanString(item.access_token || item.accessToken)
  const backendId = cleanString(item.id).toLowerCase()
  const id = isAccountManagementId(backendId) ? backendId : displayIdForAccount(item, index, usedIds)
  if (accessToken) accountTokenById.set(id, accessToken)
  accountLabelById.set(id, cleanString(item.email) || id)

  const quota = Math.max(0, Number(item.quota ?? 0) || 0)
  const imageQuotaUnknown = Boolean(item.image_quota_unknown)
  const rawStatus = cleanString(item.status)
  const status = backendStatusToFrontend(item)
  const createdAt = toTimestampSeconds(item.created_at)
  const updatedAt = toTimestampSeconds(item.updated_at || item.last_used_at || item.created_at)
  const restoreAt = toTimestampSeconds(item.restore_at || item.quota_restore_at || item.reset_at)
  const lastRefreshError = cleanString(item.last_refresh_error || item.last_token_refresh_error)
  const type = cleanString(item.type || item.plan_type || 'free')
  const sourceType = cleanString(item.source_type || 'web')
  const email = cleanString(item.email)
  const userId = cleanString(item.user_id)
  const checkoutFinalUrl = cleanString(item.checkout_final_url)
  const checkoutFinalKind = cleanString(item.checkout_final_kind)
  const checkoutUrl = cleanString(item.checkout_url || checkoutFinalUrl)
  const checkoutStatus = normalizeCheckoutLinkStatus(item.checkout_link_status)

  return {
    id,
    access_token: accessToken,
    has_refresh_token: Boolean(item.has_refresh_token),
    codex_bound: Boolean(item.codex_bound),
    codex_plan_type: cleanString(item.codex_plan_type),
    codex_token_expires_at: optionalNonNegativeInteger(item.codex_token_expires_at),
    backend_status: rawStatus || STATUS_NORMAL,
    status_category: normalizeAccountStatusCategory(item.status_category),
    status_label: cleanString(item.status_label),
    email,
    user_id: userId,
    type,
    source_type: sourceType,
    agent_identity_status: cleanString(item.agent_identity_status),
    agent_runtime_id: cleanString(item.agent_runtime_id),
    survival_status: cleanString(item.survival_status),
    survival_alive: item.survival_alive === null ? null : optionalBoolean(item.survival_alive),
    survival_last_checked_at: cleanString(item.survival_last_checked_at),
    survival_last_confirmed_at: cleanString(item.survival_last_confirmed_at),
    survival_observed_seconds: optionalNonNegativeInteger(item.survival_observed_seconds),
    survival_plan_type: cleanString(item.survival_plan_type),
    proxy: cleanString(item.proxy),
    group_id: cleanString(item.group_id),
    checkout_link_status: checkoutStatus || (checkoutFinalUrl ? 'ready' : ''),
    checkout_final_url: checkoutFinalUrl || (checkoutFinalKind ? checkoutUrl : ''),
    checkout_final_kind: checkoutFinalKind,
    checkout_url: checkoutUrl,
    checkout_hosted_url: cleanString(item.checkout_hosted_url),
    checkout_chatgpt_url: cleanString(item.checkout_chatgpt_url),
    checkout_session_id: cleanString(item.checkout_session_id),
    checkout_processor_entity: cleanString(item.checkout_processor_entity),
    checkout_channel: cleanString(item.checkout_channel),
    checkout_country: cleanString(item.checkout_country),
    checkout_currency: cleanString(item.checkout_currency),
    checkout_ui_mode: cleanString(item.checkout_ui_mode),
    checkout_provider: cleanString(item.checkout_provider),
    checkout_kind: cleanString(item.checkout_kind),
    checkout_payment_method: cleanString(item.checkout_payment_method),
    checkout_amount_minor: optionalNonNegativeInteger(item.checkout_amount_minor),
    checkout_is_free_trial: optionalBoolean(item.checkout_is_free_trial),
    checkout_hosted_instructions_url: cleanString(item.checkout_hosted_instructions_url),
    checkout_qr_image_url_png: cleanString(item.checkout_qr_image_url_png),
    checkout_qr_image_url_svg: cleanString(item.checkout_qr_image_url_svg),
    checkout_qr_code_data: cleanString(item.checkout_qr_code_data),
    checkout_qr_expires_at: item.checkout_qr_expires_at ?? undefined,
    checkout_qr_expires_at_unix: optionalNonNegativeInteger(item.checkout_qr_expires_at_unix),
    checkout_qr_ttl_seconds: optionalNonNegativeInteger(item.checkout_qr_ttl_seconds),
    checkout_plan: cleanString(item.checkout_plan),
    checkout_session_status: cleanString(item.checkout_session_status),
    checkout_payment_status: cleanString(item.checkout_payment_status),
    checkout_promo_campaign: cleanString(item.checkout_promo_campaign),
    checkout_promo_fallback_used: Boolean(item.checkout_promo_fallback_used),
    checkout_created_at: cleanString(item.checkout_created_at),
    checkout_last_attempt_at: cleanString(item.checkout_last_attempt_at),
    checkout_attempt_count: Math.max(0, Number(item.checkout_attempt_count || 0) || 0),
    checkout_last_error: cleanString(item.checkout_last_error),
    checkout_last_error_at: cleanString(item.checkout_last_error_at),
    checkout_upstream_status: Number(item.checkout_upstream_status || 0) || undefined,
    quota,
    image_quota_unknown: imageQuotaUnknown,
    auto_paused_until: cleanString(item.auto_paused_until),
    auto_pause_reason: cleanString(item.auto_pause_reason),
    relogin_needed_at: cleanString(item.relogin_needed_at),
    relogin_attempts: Math.max(0, Number(item.relogin_attempts || 0) || 0),
    last_relogin_error: cleanString(item.last_relogin_error),
    scheduling: normalizeScheduling(item.scheduling),
    ...normalizeAccountDisplay(item),
    health_level: normalizeHealthLevel(item.health_level),
    health_label: cleanString(item.health_label),
    health_tone: normalizeHealthTone(item.health_tone),
    health_reasons: Array.isArray(item.health_reasons) ? item.health_reasons.map(cleanString).filter(Boolean) : [],
    recent_uses_1h: optionalNonNegativeInteger(item.recent_uses_1h),
    recent_uses_24h: optionalNonNegativeInteger(item.recent_uses_24h),
    name: email || `${type} / ${sourceType}`,
    cookie: maskToken(accessToken),
    snlm0e: '',
    push_id: '',
    enabled: status.enabled,
    status: status.status,
    status_reason: status.status_reason,
    status_reason_code: status.status_reason_code,
    lanes: [...DEFAULT_LANES],
    model_ids: { ...EMPTY_MODEL_IDS },
    failure_count: Number(item.fail ?? 0) || 0,
    success_count: Number(item.success ?? 0) || 0,
    last_error: lastRefreshError,
    last_error_kind: status.last_error_kind,
    daily_usage: { fast: 0, thinking: 0, pro: 0, image: 0, music: 0, video: 0 },
    quota_limits: {
      enabled: !imageQuotaUnknown,
      fast: -1,
      thinking: -1,
      pro: -1,
      image: imageQuotaUnknown ? -1 : quota,
      music: -1,
      video: -1,
    },
    quota_summary: {
      enabled: !imageQuotaUnknown,
      period: 'current',
      reset_in_seconds: 0,
      conversation: { used: 0, limit: -1, remaining: -1, limited: false },
      pro: { used: 0, limit: -1, remaining: -1, limited: false },
      image: {
        used: 0,
        limit: imageQuotaUnknown ? -1 : quota,
        remaining: imageQuotaUnknown ? -1 : quota,
        limited: rawStatus === STATUS_LIMITED,
      },
      music: { used: 0, limit: -1, remaining: -1, limited: false },
      video: { used: 0, limit: -1, remaining: -1, limited: false },
    },
    last_used_at: toTimestampSeconds(item.last_used_at),
    created_at: createdAt,
    restore_at: restoreAt,
    updated_at: updatedAt || createdAt,
  }
}

export type AccountListParams = {
  page?: number
  page_size?: number
  keyword?: string
  status?: 'all' | 'normal' | 'limited' | 'abnormal' | 'disabled' | 'valid_checkout' | `sched:${string}`
  group_id?: string
  health?: AccountHealthFilter
  flag?: string
}

function normalizeAvailabilitySummary(value: unknown): Record<string, number> {
  const result: Record<string, number> = {}
  if (!value || typeof value !== 'object') return result
  for (const [code, count] of Object.entries(value as Record<string, unknown>)) {
    const n = Number(count)
    if (code && Number.isFinite(n) && n > 0) result[code] = n
  }
  return result
}

function mapAccountsResponse(response: BackendAccountsResponse): AccountsResponse {
  accountTokenById.clear()
  const usedIds = new Set<string>()
  const accounts = (response.items || []).map((item, index) => mapBackendAccount(item, index, usedIds))
  return {
    accounts,
    total: Number.isFinite(Number(response.total)) ? Number(response.total) : accounts.length,
    all_total: Number.isFinite(Number(response.all_total)) ? Number(response.all_total) : undefined,
    availability_summary: normalizeAvailabilitySummary(response.availability_summary),
    page: Number.isFinite(Number(response.page)) ? Number(response.page) : undefined,
    page_size: Number.isFinite(Number(response.page_size)) ? Number(response.page_size) : undefined,
  }
}

function resolveToken(accountIdOrToken: string): string {
  const value = cleanString(accountIdOrToken)
  return accountTokenById.get(value) || value
}

function payloadToken(payload: Partial<Account>): string {
  const mappedToken = payload.id ? accountTokenById.get(payload.id) : ''
  const candidate = cleanString(payload.access_token || payload.cookie || mappedToken)
  if (candidate && !isMaskedToken(candidate)) return candidate
  return mappedToken || ''
}

function backendStatusForEnabled(enabled: boolean | undefined): AccountBackendStatus {
  return enabled === false ? STATUS_DISABLED : STATUS_NORMAL
}

function backendStatusForPayload(payload: Partial<Account>): AccountBackendStatus {
  const raw = cleanString(payload.backend_status || payload.status)
  if (ACCOUNT_BACKEND_STATUS_VALUES.includes(raw as AccountBackendStatus)) {
    return raw as AccountBackendStatus
  }
  return backendStatusForEnabled(payload.enabled)
}

function accountFromPayload(payload: Partial<Account> & { refresh_token?: string }, requireToken = true) {
  const accessToken = payloadToken(payload)
  if (!accessToken && requireToken) {
    throw new Error('请填写 access token')
  }
  const refreshToken = cleanString(payload.refresh_token)
  return {
    access_token: accessToken,
    ...(refreshToken ? { refresh_token: refreshToken } : {}),
    type: payload.type,
    source_type: payload.source_type,
    proxy: payload.proxy,
    group_id: payload.group_id,
    quota: payload.quota,
    status: backendStatusForPayload(payload),
  }
}

type RefreshStartResponse = { progress_id: string; target_ids?: string[]; errors?: unknown[] }

async function refreshAndPoll(accountIdsOrTokens: string[]) {
  const start = await apiClient.post<Record<string, unknown>, RefreshStartResponse>(
    '/api/accounts/sync',
    targetFields(accountIdsOrTokens),
  )
  const progressId = cleanString(start.progress_id)
  if (!progressId) return { status: 'ok' }

  const deadline = Date.now() + 60_000
  while (Date.now() < deadline) {
    const progress = await apiClient.get<never, AccountRefreshProgress>(
      `/api/accounts/operations/${encodeURIComponent(progressId)}`,
    )
    const legacyProgress = progress as unknown as { status?: string; finished?: boolean }
    if (progress.done || legacyProgress.status === 'done' || legacyProgress.finished) {
      if (progress.error) throw new Error(String(progress.error))
      const errors = Array.isArray(progress?.result?.errors) ? progress.result.errors : []
      if (errors.length) {
        const first = errors[0]
        const message = typeof first === 'string'
          ? first
          : [first?.token, first?.error].map(cleanString).filter(Boolean).join(': ')
        throw new Error(message || `账号刷新失败，共 ${errors.length} 个错误`)
      }
      return { status: 'ok', progress }
    }
    await new Promise((resolve) => window.setTimeout(resolve, 800))
  }
  throw new Error('刷新进度超时，请稍后重新打开列表查看结果')
}

/**
 * 启动一个带进度的账号刷新类任务并轮询。
 * endpoint: sync 同步账号信息与额度；refresh-access-token 只用 RT 换新 AT。
 */
async function runProgressTask(
  endpoint: 'sync' | 'refresh-access-token',
  accountIdsOrTokens: string[],
  onProgress?: (progress: AccountRefreshProgress) => void,
  options?: { all?: boolean },
) {
  const targets = targetFields(accountIdsOrTokens)
  const count = targets.account_ids.length + targets.access_tokens.length
  if (!count && !options?.all) {
    throw new Error('没有可刷新的账号')
  }

  const body: Record<string, unknown> = options?.all && !count ? { selection: { mode: 'all' } } : targets
  const start = await apiClient.post<Record<string, unknown>, RefreshStartResponse>(
    `/api/accounts/${endpoint}`,
    body,
  )
  const progressId = cleanString(start.progress_id)
  if (!progressId) {
    return { status: 'ok', progress: null as AccountRefreshProgress | null }
  }

  const deadline = Date.now() + (
    options?.all ? 12 * 60 * 60 * 1000 : Math.max(90_000, count * 15_000)
  )
  while (Date.now() < deadline) {
    // 统一进度接口（带 status/percent/events）；旧的 /api/accounts/refresh/progress 仍保留给外部调用方。
    const progress = await apiClient.get<never, AccountRefreshProgress>(
      `/api/accounts/operations/${encodeURIComponent(progressId)}`,
    )
    onProgress?.(progress)
    if (progress.done || progress.error) {
      if (progress.error) throw new Error(String(progress.error))
      return { status: 'ok', progress }
    }
    await new Promise((resolve) => window.setTimeout(resolve, 900))
  }

  throw new Error('刷新进度超时，请稍后重新打开列表查看结果')
}

async function refreshAndPollWithProgress(
  accountIdsOrTokens: string[],
  onProgress?: (progress: AccountRefreshProgress) => void,
  options?: { all?: boolean },
) {
  return runProgressTask('sync', accountIdsOrTokens, onProgress, options)
}

function mutationErrors(errors: BackendAccountMutationResponse['errors']): string[] {
  return Array.isArray(errors)
    ? errors
      .map((item) => (typeof item === 'string' ? item : String(item.error || (item as { message?: string }).message || item.token || '')))
      .filter(Boolean)
    : []
}

async function updateStatus(accountId: string, status: string) {
  const target = targetFields([accountId])
  const response = await apiClient.post<
    Record<string, unknown>,
    BackendAccountMutationResponse
  >('/api/accounts/update', target.account_ids.length
    ? { id: target.account_ids[0], status }
    : { access_token: target.access_tokens[0] || '', status })
  return {
    status: 'ok',
    account: response.item ? mapBackendAccount(response.item, 0, new Set()) : undefined,
  }
}

async function updateStatusBatch(accountIdsOrTokens: string[], status: string) {
  const target = targetFields(accountIdsOrTokens)
  if (!target.account_ids.length && !target.access_tokens.length) {
    return { status: 'ok', success_count: 0, errors: [] as string[] }
  }
  const response = await apiClient.post<
    Record<string, unknown>,
    BackendAccountMutationResponse
  >('/api/accounts/batch-update', { ...target, status })
  return {
    status: 'ok',
    success_count: Number(response.updated || 0),
    errors: mutationErrors(response.errors),
  }
}

async function deleteAccountsByIds(accountIdsOrTokens: string[]) {
  const target = targetFields(accountIdsOrTokens)
  if (!target.account_ids.length && !target.access_tokens.length) {
    return { status: 'ok', success_count: 0, errors: [] as string[] }
  }
  const response = await apiClient.request<unknown, BackendAccountMutationResponse>({
    method: 'DELETE',
    url: '/api/accounts',
    data: { account_ids: target.account_ids, tokens: target.access_tokens },
  })
  for (const accountId of accountIdsOrTokens) {
    accountTokenById.delete(accountId)
    accountLabelById.delete(accountId)
  }
  return {
    status: 'ok',
    success_count: Number(response.removed || 0),
    errors: mutationErrors(response.errors),
  }
}

export const accountsApi = {
  list: async (params?: AccountListParams) => {
    const response = await apiClient.get<never, BackendAccountsResponse>('/api/accounts', {
      params: params || undefined,
    })
    return mapAccountsResponse(response)
  },

  listGroups: () =>
    apiClient.get<never, { groups: AccountGroup[]; proxy_groups?: ProxyGroup[] }>('/api/account-groups'),

  saveGroup: (payload: AccountGroupPayload) =>
    apiClient.post<AccountGroupPayload, { group: AccountGroup; groups: AccountGroup[]; proxy_groups?: ProxyGroup[] }>(
      '/api/account-groups',
      payload,
    ),

  deleteGroup: (id: string) =>
    apiClient.delete<never, { deleted: string; groups: AccountGroup[]; proxy_groups?: ProxyGroup[]; items?: BackendAccount[] }>(
      `/api/account-groups/${encodeURIComponent(id)}`,
    ),

  upsert: async (payload: Partial<Account> & { refresh_token?: string }) => {
    const editingId = cleanString(payload.id)
    if (isAccountManagementId(editingId)) {
      // 编辑：用稳定管理 ID 定位；AT / RT 留空表示不覆盖，已有凭据保持不变。
      const account = accountFromPayload(payload, false)
      const response = await apiClient.post<Record<string, unknown>, BackendAccountMutationResponse>(
        '/api/accounts/update',
        {
          id: editingId,
          access_token: account.access_token || '',
          refresh_token: cleanString(payload.refresh_token),
          type: account.type,
          source_type: account.source_type,
          status: account.status,
          quota: account.quota,
          proxy: account.proxy,
          group_id: account.group_id,
        },
      )
      return {
        status: 'ok',
        account: response.item ? mapBackendAccount(response.item, 0, new Set()) : undefined,
      }
    }

    const account = accountFromPayload(payload)
    const existingToken = payload.id ? accountTokenById.get(payload.id) : ''
    if (existingToken && existingToken === account.access_token) {
      const response = await apiClient.post<
        {
          access_token: string
          type?: string
          source_type?: string
          status?: string
          quota?: number
          proxy?: string
          group_id?: string
        },
        BackendAccountMutationResponse
      >('/api/accounts/update', {
        access_token: existingToken,
        type: account.type,
        source_type: account.source_type,
        status: account.status,
        quota: account.quota,
        proxy: account.proxy,
        group_id: account.group_id,
      })
      return {
        status: 'ok',
        account: response.item ? mapBackendAccount(response.item, 0, new Set()) : undefined,
      }
    }

    const response = await apiClient.post<
      { tokens: string[]; accounts: Array<Record<string, unknown>> },
      { items?: BackendAccount[] }
    >('/api/accounts', {
      tokens: [],
      accounts: [account],
    })
    const mapped = mapAccountsResponse({ items: response.items || [] })
    return { status: 'ok', account: mapped.accounts.find((item) => item.access_token === account.access_token) || mapped.accounts[0] }
  },

  importAccounts: async (
    accountPayloads: AccountImportPayload[],
    fallbackSourceType = 'manual',
    options: AccountImportOptions = {},
  ) => {
    const deduped = new Map<string, AccountImportPayload>()
    for (const payload of accountPayloads) {
      if (!payload || typeof payload !== 'object') continue
      const accessToken = cleanString(payload.access_token || payload.accessToken || payload.cookie)
      if (!accessToken) continue
      const nextPayload: AccountImportPayload = {
        ...payload,
        access_token: accessToken,
        source_type: cleanString(payload.source_type) || fallbackSourceType,
        status: cleanString(payload.status) || STATUS_NORMAL,
      }
      delete nextPayload.accessToken
      deduped.set(accessToken, nextPayload)
    }
    const accounts = Array.from(deduped.values())
    if (!accounts.length) {
      return {
        status: 'ok',
        added: 0,
        skipped: 0,
        refreshed: 0,
        verified: 0,
        rejected: [] as AccountImportRejected[],
        errors: [] as string[],
      }
    }
    const response = await apiClient.post<
      {
        tokens: string[]
        accounts: Array<Record<string, unknown>>
        refresh?: boolean
        return_items?: boolean
        verify_on_import?: boolean
        restore?: boolean
        target_group_id?: string
      },
      BackendAccountMutationResponse
    >('/api/accounts', {
      tokens: [],
      accounts,
      refresh: options.refresh ?? true,
      return_items: options.returnItems ?? false,
      verify_on_import: options.verifyOnImport ?? false,
      ...(options.restore ? { restore: true } : {}),
      ...(options.targetGroupId !== undefined ? { target_group_id: options.targetGroupId } : {}),
    })
    return {
      status: 'ok',
      added: Number(response.added || 0),
      skipped: Number(response.skipped || 0),
      refreshed: Number(response.refreshed || 0),
      verified: Number(response.verified || 0),
      rejected: Array.isArray(response.rejected) ? response.rejected : [],
      errors: Array.isArray(response.errors)
        ? response.errors.map((item) => (typeof item === 'string' ? item : [item.token, item.error].filter(Boolean).join(': '))).filter(Boolean)
        : [],
    }
  },

  importTokens: async (tokens: string[], sourceType: string) => {
    const accounts = Array.from(new Set(tokens.map((token) => cleanString(token)).filter(Boolean)))
      .map((accessToken) => ({
        access_token: accessToken,
        type: 'free',
        source_type: sourceType,
        status: STATUS_NORMAL,
      }))
    return accountsApi.importAccounts(accounts, sourceType)
  },

  cleanupImportedAbnormalAccounts: async (tokens: string[], remove = false) => {
    const accessTokens = Array.from(new Set(tokens.map((token) => cleanString(token)).filter(Boolean)))
    if (!accessTokens.length) {
      return { status: 'ok', checked: 0, abnormal: 0, removed: 0 }
    }
    const response = await apiClient.post<
      { access_tokens: string[]; remove: boolean },
      AccountImportCleanupResponse
    >('/api/accounts/import-cleanup', {
      access_tokens: accessTokens,
      remove,
    })
    return {
      status: 'ok',
      checked: Number(response.checked || 0),
      abnormal: Number(response.abnormal || 0),
      removed: Number(response.removed || 0),
    }
  },

  poolHealth: () => apiClient.get<never, AccountPoolHealthResponse>('/api/accounts/health'),

  /** Probe one account with a remote info check; spends no image quota. */
  testAccount: (accountId: string) => {
    const target = targetFields([accountId])
    return apiClient.post<Record<string, unknown>, AccountTestResult>('/api/accounts/test', target.account_ids.length
      ? { id: target.account_ids[0] }
      : { access_token: target.access_tokens[0] || '' })
  },

  /** 真实测试：发起一次对话或画图，返回内容与额度扣减情况。 */
  testAccountCapability: (
    accountId: string,
    payload: { mode: 'chat' | 'image'; model: string; prompt: string },
  ) => {
    const target = targetFields([accountId])
    return apiClient.post<Record<string, unknown>, AccountCapabilityTestResult>('/api/accounts/test', {
      ...(target.account_ids.length ? { id: target.account_ids[0] } : { access_token: target.access_tokens[0] || '' }),
      ...payload,
    }, { timeout: 5 * 60 * 1000 })
  },

  /** 申请换出口（对接 services/egress_slots.request_switch）：先停用到期再切换并恢复。 */
  switchEgress: (accountIds: readonly string[], target: string) =>
    apiClient.post<Record<string, unknown>, AccountEgressSwitchResult>('/api/accounts/egress/switch', {
      account_ids: [...accountIds],
      target,
    }),

  cancelEgressSwitch: (accountIds: readonly string[]) =>
    apiClient.post<Record<string, unknown>, { cancelled: string[] }>('/api/accounts/egress/cancel-switch', {
      account_ids: [...accountIds],
    }),

  /** 仅管理员可用；每次调用都会写入审计日志。 */
  getAccessToken: async (accountId: string) => {
    const response = await apiClient.get<never, { access_token: string }>(
      `/api/accounts/${encodeURIComponent(accountId)}/access-token`,
    )
    return cleanString(response.access_token)
  },

  getRefreshToken: async (accountId: string) => {
    const response = await apiClient.get<never, { refresh_token: string }>(
      `/api/accounts/${encodeURIComponent(accountId)}/refresh-token`,
    )
    return cleanString(response.refresh_token)
  },

  /** 跨分页选择：按筛选条件解析出全部匹配账号 ID（已扣除 excluded）。 */
  previewSelection: (selection: AccountSelectionScope) =>
    apiClient.post<{ selection: AccountSelectionScope }, AccountSelectionPreview>(
      '/api/accounts/selection-preview',
      { selection },
    ),

  /** 只用 refresh_token 换新 access_token，不拉取账号信息也不查询额度。 */
  refreshAccessTokensWithProgress: (
    accountIdsOrTokens: string[],
    onProgress?: (progress: AccountRefreshProgress) => void,
  ) => runProgressTask('refresh-access-token', accountIdsOrTokens, onProgress),

  /** 导入本地 Sub2API 导出文件（content 为文件 JSON 文本）。 */
  importSub2apiFile: async (
    content: string,
    options: { refresh?: boolean; targetGroupId?: string } = {},
  ) => {
    const response = await apiClient.post<Record<string, unknown>, {
      parsed?: number
      added?: number
      skipped?: number
      refreshed?: number
      errors?: BackendAccountMutationResponse['errors']
    }>('/api/accounts/import-sub2api-file', {
      content,
      refresh: options.refresh ?? true,
      ...(options.targetGroupId !== undefined ? { target_group_id: options.targetGroupId } : {}),
    })
    return {
      parsed: Number(response.parsed || 0),
      added: Number(response.added || 0),
      skipped: Number(response.skipped || 0),
      refreshed: Number(response.refreshed || 0),
      errors: mutationErrors(response.errors),
    }
  },

  clearAutoPause: async (accountIds: string[]) => {
    const target = targetFields(accountIds)
    if (!target.account_ids.length && !target.access_tokens.length) return { cleared: 0 }
    const response = await apiClient.post<Record<string, unknown>, { cleared?: number }>(
      '/api/accounts/clear-auto-pause',
      target,
    )
    return { cleared: Number(response.cleared || 0) }
  },

  startCodexOAuth: (emailHint = '') =>
    apiClient.post<{ email_hint: string }, CodexOAuthStartResponse>(
      '/api/accounts/codex-oauth/start',
      { email_hint: emailHint },
    ),

  finishCodexOAuth: (sessionId: string, callback: string, accountId: string) =>
    apiClient.post<Record<string, unknown>, CodexOAuthBindResult>(
      '/api/accounts/codex-oauth/finish',
      { session_id: sessionId, callback, account_id: accountId },
    ),

  delete: async (accountId: string) => {
    await deleteAccountsByIds([accountId])
    accountTokenById.delete(accountId)
    return { status: 'ok', deleted: accountId }
  },

  refreshToken: async (accountId: string) => {
    await refreshAndPoll([accountId])
    return { status: 'ok', account: undefined as unknown as Account }
  },

  extractCheckout: async (accountId: string) => {
    const payload = {
      access_token: resolveToken(accountId),
    }
    const response = await apiClient.post<
      typeof payload,
      BackendAccountMutationResponse & Record<string, unknown>
    >('/api/accounts/checkout-session', payload)
    return {
      checkout_link_status: normalizeCheckoutLinkStatus(response.checkout_link_status),
      checkout_final_url: cleanString(response.checkout_final_url),
      checkout_final_kind: cleanString(response.checkout_final_kind),
      checkout_url: cleanString(response.checkout_url),
      checkout_chatgpt_url: cleanString(response.checkout_chatgpt_url || response.chatgpt_checkout_url),
      checkout_hosted_url: cleanString(response.checkout_hosted_url || response.hosted_checkout_url),
      checkout_payment_method: cleanString(response.checkout_payment_method),
      checkout_amount_minor: optionalNonNegativeInteger(response.checkout_amount_minor),
      checkout_is_free_trial: optionalBoolean(response.checkout_is_free_trial),
      account: response.item ? mapBackendAccount(response.item, 0, new Set()) : undefined,
    }
  },

  enqueueCheckoutRetries: async (accountIds: string[]) => {
    const accessTokens = Array.from(new Set(
      accountIds.map((accountId) => resolveToken(accountId)).filter(Boolean),
    ))
    return apiClient.post<
      { access_tokens: string[] },
      { queued?: number; skipped?: number }
    >('/api/accounts/checkout-retries', { access_tokens: accessTokens })
  },

  refreshAccountsWithProgress: (
    accountIdsOrTokens: string[],
    onProgress?: (progress: AccountRefreshProgress) => void,
  ) => refreshAndPollWithProgress(accountIdsOrTokens, onProgress),

  refreshAllAccountsWithProgress: (
    onProgress?: (progress: AccountRefreshProgress) => void,
  ) => refreshAndPollWithProgress([], onProgress, { all: true }),

  exportAccounts: (accountIdsOrTokens: string[], format: AccountExportFormat = 'json') =>
    apiClient.post<Record<string, unknown>, Blob>('/api/accounts/export', {
      ...targetFields(accountIdsOrTokens),
      format,
    }, {
      responseType: 'blob',
    }),

  resetAccountState: async (accountId: string) => {
    return updateStatus(accountId, STATUS_NORMAL)
  },

  bindGroup: async (accountIdsOrTokens: string[], groupId: string) => {
    const response = await apiClient.post<
      Record<string, unknown>,
      BackendAccountMutationResponse
    >('/api/accounts/group', {
      ...targetFields(accountIdsOrTokens),
      group_id: groupId,
    })
    return {
      status: 'ok',
      updated: response.updated || 0,
      group_id: response.group_id || groupId,
      groups: response.groups || [],
      accounts: response.items ? mapAccountsResponse({ items: response.items }).accounts : [],
    }
  },

  enable: async (accountId: string) =>
    updateStatus(accountId, STATUS_NORMAL),

  disable: async (accountId: string) =>
    updateStatus(accountId, STATUS_DISABLED),

  bulkEnable: (accountIds: string[]) =>
    updateStatusBatch(accountIds, STATUS_NORMAL),

  bulkDisable: (accountIds: string[]) =>
    updateStatusBatch(accountIds, STATUS_DISABLED),

  bulkDelete: (accountIds: string[]) =>
    deleteAccountsByIds(accountIds),

  resolveCookie: async (_cookie: string) => ({
    status: 'unsupported',
    snlm0e: '',
    model_ids: { ...EMPTY_MODEL_IDS },
  }),
}
