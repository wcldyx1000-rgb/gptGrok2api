import type { Account, AccountGroup, AccountLane } from '@/api/accounts'
import type { ProxyGroup } from '@/api/proxy'
import { parseProxyReference, proxyReferenceLabel } from '@/api/proxy'
import { PILL_TONE_CLASS } from '@/lib/pillTones'

export type QuotaKey = 'fast' | 'thinking' | 'pro' | 'image' | 'music' | 'video'
export type AccountStatusFilter = 'all' | 'normal' | 'limited' | 'abnormal' | 'disabled'

export type QuotaLine = {
  used: number
  limit: number
  remaining: number
  limited: boolean
}

type GroupLevel = 'ok' | 'warn' | 'error'
type GroupStatus = 'ok' | 'cooldown' | 'local_full' | 'upstream_hint'

type GroupState = {
  id: 'pro' | 'image' | 'music' | 'video'
  title: string
  quotaKey: QuotaKey
  status: GroupStatus
  level: GroupLevel
  detail: string
}

type GroupEvalContext = {
  item: Account
  line: QuotaLine
  resetHint: string
}

type GroupDefinition = {
  id: GroupState['id']
  title: string
  quotaKey: QuotaKey
  evaluate: (ctx: GroupEvalContext) => GroupState
}

export const quotaOrder: QuotaKey[] = ['fast', 'thinking', 'pro', 'image', 'music', 'video']

const laneOrder: AccountLane[] = ['fast', 'thinking', 'pro']

export type AccountGroupRow = AccountGroup & {
  raw: AccountGroup
  account_count: number
  proxy_label: string
}

export type AccountProgressMetricItem = {
  key: string
  label: string
  value: string | number
}

const IMAGE_UNAVAILABLE_HINTS = [
  '不能创建图片',
  '无法生成更多图像',
  '今天无法为您生成更多图像',
  '请明天再来',
  "can't create any",
  'cannot create any',
  "can't seem to create any",
  "image creation isn't available",
  'image creation may not be available',
  'unable to generate more images',
  "can't generate more images",
  'generate more images today',
]

const ACCOUNT_STATUS_CATEGORY_VALUES = ['normal', 'limited', 'abnormal', 'disabled'] as const
const ACCOUNT_STATUS_LABELS: Record<Exclude<AccountStatusFilter, 'all'>, string> = {
  normal: '正常',
  limited: '限流',
  abnormal: '异常',
  disabled: '禁用',
}

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

function accountStatusCategoryValue(item: Account): Exclude<AccountStatusFilter, 'all'> | '' {
  const raw = cleanString(item.status_category)
  return ACCOUNT_STATUS_CATEGORY_VALUES.includes(raw as Exclude<AccountStatusFilter, 'all'>)
    ? raw as Exclude<AccountStatusFilter, 'all'>
    : ''
}

function signatureValue(value: unknown): string {
  return cleanString(value).replaceAll('|', '/')
}

export function boundedSignatureText(value: unknown, limit = 160): string {
  const text = signatureValue(value)
  if (text.length <= limit) return text
  return `${text.length}:${text.slice(0, limit)}:${text.slice(-24)}`
}

export function accountRowSignature(item: Account): string {
  return [
    item.id,
    rowClass(item),
    accountTokenPreview(item),
    accountSourceText(item),
    statusText(item),
    statusClass(item),
    boundedSignatureText(statusRawError(item)),
    accountPrimaryText(item),
    accountSecondaryText(item),
    accountCreatedText(item),
    accountRestoreText(item),
    accountQuotaText(item),
    boundedSignatureText(item.access_token, 48),
    boundedSignatureText(item.cookie, 48),
    item.type,
    item.source_type,
    item.group_id,
    item.proxy,
    item.has_refresh_token ? 1 : 0,
    item.codex_bound ? 1 : 0,
    item.checkout_link_status,
    item.checkout_channel,
    item.checkout_final_kind,
    boundedSignatureText(checkoutFinalLinkUrl(item), 64),
    item.checkout_payment_method,
    item.checkout_amount_minor ?? '',
    item.checkout_is_free_trial ? 1 : 0,
    boundedSignatureText(item.checkout_last_error),
    item.checkout_attempt_count || 0,
    item.backend_status,
    item.image_quota_unknown ? 1 : 0,
    item.success_count || 0,
    item.failure_count || 0,
    item.enabled ? 1 : 0,
    item.is_demo ? 1 : 0,
    item.auto_paused_until,
    item.relogin_needed_at,
    item.relogin_attempts || 0,
    boundedSignatureText(item.last_relogin_error),
    item.health_level,
    boundedSignatureText(accountHealthDetail(item)),
    item.recent_uses_1h || 0,
    accountScheduleSignature(item),
    accountDisplaySignature(item),
  ].map(signatureValue).join('|')
}

/** 凭据 / 救回 / 额度 / 出口展示字段的签名（行 v-memo 用）。 */
function accountDisplaySignature(item: Account): string {
  return [
    item.access_token_status, item.access_token_issued_at, item.access_token_expires_at,
    item.refresh_token_status, item.refresh_token_invalid_at, item.last_token_refresh_at,
    item.last_token_refresh_error_at, boundedSignatureText(item.last_token_refresh_error, 48),
    item.rescue_code, item.has_outlook_credentials ? 1 : 0, item.has_password ? 1 : 0, item.has_totp ? 1 : 0,
    item.relogin_failures || 0, boundedSignatureText(item.relogin_last_error, 48),
    item.quota_state, item.quota_label, item.quota_source, item.quota_reset_at, item.quota_synced_at,
    item.quota_reset_idle ? 1 : 0, item.group_name,
    item.egress?.mode, item.egress?.label, item.egress?.usable ? 1 : 0,
  ].join(',')
}

const HEALTH_TONE_CLASS: Record<string, string> = {
  success: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  warning: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  error: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  neutral: 'border-border bg-muted/30 text-muted-foreground',
}

export function accountHealthText(item: Account): string {
  return item.health_label || '—'
}

export function accountHealthClass(item: Account): string {
  return HEALTH_TONE_CLASS[item.health_tone || 'neutral'] || HEALTH_TONE_CLASS.neutral
}

/** Reasons plus recent usage, shown in the health pill's detail card. */
export function accountHealthDetail(item: Account): string {
  const lines = [...(item.health_reasons || [])]
  if (item.health_level === 'healthy' && !lines.length) lines.push('近期没有异常信号')
  if (item.health_level) lines.push(`近 1 小时使用 ${item.recent_uses_1h || 0} 次，近 24 小时 ${item.recent_uses_24h || 0} 次`)
  return lines.join('\n')
}

function includesHint(input: unknown, hints: string[]): boolean {
  const text = String(input || '').toLowerCase()
  return Boolean(text) && hints.some((hint) => text.includes(hint.toLowerCase()))
}

function normalizeLimit(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? Math.trunc(n) : -1
}

function normalizeUsed(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0
}

function buildQuotaLine(used: number, limit: number): QuotaLine {
  if (limit < 0) {
    return { used, limit: -1, remaining: -1, limited: false }
  }
  const safeLimit = Math.max(0, limit)
  return {
    used,
    limit: safeLimit,
    remaining: Math.max(0, safeLimit - used),
    limited: true,
  }
}

function formatDateTime(timestampSeconds: number): string {
  const date = new Date(timestampSeconds * 1000)
  if (Number.isNaN(date.getTime())) return '-'
  const yyyy = date.getFullYear()
  const mm = String(date.getMonth() + 1).padStart(2, '0')
  const dd = String(date.getDate()).padStart(2, '0')
  const hh = String(date.getHours()).padStart(2, '0')
  const mi = String(date.getMinutes()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
}

export function formatAccountDate(timestampSeconds?: number): string {
  const value = Number(timestampSeconds || 0)
  if (!Number.isFinite(value) || value <= 0) return '-'
  return formatDateTime(value)
}

export function accountFailureRateText(item: Account): string {
  const rate = failureRate(item)
  return rate === null ? '次数太少' : `失败率 ${Math.round(rate * 100)}%`
}

/** 失败率对应的语义色：>=40% 红，>=20% 黄，其余绿；次数太少不着色。 */
export function accountFailureRateTone(item: Account): '' | 'is-error' | 'is-warning' | 'is-success' {
  const rate = failureRate(item)
  if (rate === null) return ''
  if (rate >= 0.4) return 'is-error'
  if (rate >= 0.2) return 'is-warning'
  return 'is-success'
}

export function accountLastUsedText(item: Pick<Account, 'last_used_at'>, now: number): string {
  return item.last_used_at ? `${agoText(item.last_used_at, now)}使用` : '从未使用'
}

export function accountIdleOverDay(item: Pick<Account, 'last_used_at'>, now: number): boolean {
  return !item.last_used_at || now - item.last_used_at >= 86400
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (hours > 0) return `${hours}h${minutes}m`
  return `${Math.max(1, minutes)}m`
}

function quotaResetHint(item: Account): string {
  const resetInSeconds = Number(item.quota_summary?.reset_in_seconds || 0)
  return resetInSeconds > 0 ? `约 ${formatDuration(resetInSeconds)} 后重置` : ''
}

export function quotaLine(item: Account, key: QuotaKey): QuotaLine {
  const usage = item.daily_usage || { fast: 0, thinking: 0, pro: 0, image: 0, music: 0, video: 0 }
  const limits = item.quota_limits || { enabled: true, fast: -1, thinking: -1, pro: -1, image: -1, music: -1, video: -1 }
  return buildQuotaLine(normalizeUsed(usage[key]), normalizeLimit(limits[key]))
}

const GROUPS: GroupDefinition[] = [
  {
    id: 'pro',
    title: 'Pro',
    quotaKey: 'pro',
    evaluate: ({ item, line, resetHint }) => {
      const cooldownSeconds = Number(item.pro_cooldown_seconds || 0)
      if (cooldownSeconds > 0) {
        return {
          id: 'pro',
          title: 'Pro',
          quotaKey: 'pro',
          status: 'cooldown',
          level: 'warn',
          detail: `Pro：冷却 ${formatDuration(cooldownSeconds)}（到 ${formatDateTime(Number(item.pro_cooldown_until || 0))}）`,
        }
      }
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'pro',
          title: 'Pro',
          quotaKey: 'pro',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `Pro：本地配额已满，${resetHint}` : 'Pro：本地配额已满',
        }
      }
      return {
        id: 'pro',
        title: 'Pro',
        quotaKey: 'pro',
        status: 'ok',
        level: 'ok',
        detail: 'Pro：正常',
      }
    },
  },
  {
    id: 'image',
    title: '图片',
    quotaKey: 'image',
    evaluate: ({ item, line, resetHint }) => {
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'image',
          title: '图片',
          quotaKey: 'image',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `图片：本地配额已满，${resetHint}` : '图片：本地配额已满',
        }
      }

      if (item.status_reason_code === 'image_degraded_to_fast' || String(item.last_error_kind || '') === 'media_degraded') {
        return {
          id: 'image',
          title: '图片',
          quotaKey: 'image',
          status: 'upstream_hint',
          level: 'warn',
          detail: '图片：本次请求被降级到 Fast，建议检查登录状态或上游能力',
        }
      }

      if (
        item.status_reason_code === 'image_generation_unavailable' ||
        String(item.last_error_kind || '') === 'media_generation_unavailable' ||
        includesHint(item.status_reason, IMAGE_UNAVAILABLE_HINTS) ||
        includesHint(item.last_error, IMAGE_UNAVAILABLE_HINTS)
      ) {
        const detail = String(item.status_reason || item.last_error || '').trim()
        return {
          id: 'image',
          title: '图片',
          quotaKey: 'image',
          status: 'upstream_hint',
          level: 'warn',
          detail: detail ? `图片：${detail}` : '图片：上游暂时不可用',
        }
      }

      return {
        id: 'image',
        title: '图片',
        quotaKey: 'image',
        status: 'ok',
        level: 'ok',
        detail: '图片：正常',
      }
    },
  },
  {
    id: 'video',
    title: '视频',
    quotaKey: 'video',
    evaluate: ({ item, line, resetHint }) => {
      const cooldownSeconds = Number(item.video_cooldown_seconds || 0)
      if (cooldownSeconds > 0) {
        return {
          id: 'video',
          title: '视频',
          quotaKey: 'video',
          status: 'cooldown',
          level: 'warn',
          detail: `视频：冷却 ${formatDuration(cooldownSeconds)}（到 ${formatDateTime(Number(item.video_cooldown_until || 0))}）`,
        }
      }
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'video',
          title: '视频',
          quotaKey: 'video',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `视频：本地配额已满，${resetHint}` : '视频：本地配额已满',
        }
      }
      return {
        id: 'video',
        title: '视频',
        quotaKey: 'video',
        status: 'ok',
        level: 'ok',
        detail: '视频：正常',
      }
    },
  },
  {
    id: 'music',
    title: '音乐',
    quotaKey: 'music',
    evaluate: ({ line, resetHint }) => {
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'music',
          title: '音乐',
          quotaKey: 'music',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `音乐：本地配额已满，${resetHint}` : '音乐：本地配额已满',
        }
      }
      return {
        id: 'music',
        title: '音乐',
        quotaKey: 'music',
        status: 'ok',
        level: 'ok',
        detail: '音乐：正常',
      }
    },
  },
]

function getGroupStates(item: Account): GroupState[] {
  const resetHint = quotaResetHint(item)
  return GROUPS.map((group) => group.evaluate({ item, line: quotaLine(item, group.quotaKey), resetHint }))
}

function laneBackoffDetailLines(item: Account): string[] {
  const summary = item.lane_backoff_summary
  if (!summary?.active || !Array.isArray(summary.items)) return []
  return summary.items.map((entry) => {
    const lane = String(entry.lane || '').trim()
    const waitSeconds = Number(entry.wait_seconds || 0)
    const untilLocal = String(entry.until_local || '').trim()
    const reason = String(entry.reason || '').trim()
    const base = lane
      ? `${lane}：限流 ${formatDuration(waitSeconds)}`
      : `限流 ${formatDuration(waitSeconds)}`
    const withUntil = untilLocal ? `${base}（到 ${untilLocal}）` : base
    return reason ? `${withUntil}；原因：${reason}` : withUntil
  })
}

function getStateByQuotaKey(states: GroupState[], key: QuotaKey): GroupState | null {
  return states.find((state) => state.quotaKey === key) || null
}

export function quotaIssueDetailLines(item: Account): string[] {
  return [
    ...laneBackoffDetailLines(item),
    ...getGroupStates(item)
    .filter((state) => state.status !== 'ok')
    .map((state) => state.detail),
  ]
}

export function statusText(item: Account): string {
  const category = accountStatusCategoryValue(item)
  if (category) return ACCOUNT_STATUS_LABELS[category]

  const backendStatus = String(item.backend_status || '').trim()
  const status = String(item.status || '').trim().toLowerCase()
  const reasonCode = String(item.status_reason_code || '').toLowerCase()
  const errorKind = String(item.last_error_kind || '').toLowerCase()

  if (!item.enabled || status === 'disabled' || backendStatus === '禁用') return '禁用'
  if (
    backendStatus === '异常' ||
    status === 'abnormal' ||
    status === 'invalid' ||
    status === 'incomplete' ||
    reasonCode === 'snlm0e_refresh_failed' ||
    reasonCode === 'account_invalid' ||
    reasonCode === 'parse_failure' ||
    reasonCode === 'upstream_error' ||
    errorKind === 'auth_invalid' ||
    errorKind === 'parse_failure' ||
    errorKind === 'upstream_error'
  ) return '异常'
  if (
    backendStatus === '限流' ||
    status === 'limited' ||
    status === 'rate_limited' ||
    status === 'cooling' ||
    status === 'backoff' ||
    reasonCode === 'lane_backoff' ||
    reasonCode === 'pro_cooldown' ||
    reasonCode === 'video_cooldown' ||
    reasonCode === 'image_generation_unavailable' ||
    reasonCode === 'image_degraded_to_fast' ||
    reasonCode === 'lane_degraded' ||
    reasonCode === 'text_pending' ||
    errorKind === 'quota_exhausted' ||
    errorKind === 'media_pending' ||
    errorKind === 'media_generation_unavailable' ||
    errorKind === 'media_degraded' ||
    errorKind === 'lane_degraded' ||
    errorKind === 'text_pending'
  ) return '限流'
  return '正常'
}

export function statusCategory(item: Account): Exclude<AccountStatusFilter, 'all'> {
  const category = accountStatusCategoryValue(item)
  if (category) return category

  const text = statusText(item)
  if (text === '禁用') return 'disabled'
  if (text === '正常') return 'normal'
  if (text === '限流') return 'limited'
  return 'abnormal'
}

export function statusClass(item: Account): string {
  const category = statusCategory(item)
  if (category === 'normal') return PILL_TONE_CLASS.success
  if (category === 'limited') return PILL_TONE_CLASS.warning
  if (category === 'abnormal') return PILL_TONE_CLASS.danger
  if (category === 'disabled') return PILL_TONE_CLASS.neutral
  return PILL_TONE_CLASS.neutral
}

export function statusReason(item: Account): string {
  const explicitReason = String(item.status_reason || '').trim()
  if (explicitReason) return explicitReason

  if (String(item.last_error_kind || '').toLowerCase() === 'auth_invalid') {
    return '登录态失效'
  }

  const issueLines = quotaIssueDetailLines(item)
  if (issueLines.length > 0) return issueLines.join('；')

  const lastError = String(item.last_error || '').trim()
  if (lastError) return lastError
  if (!item.enabled || item.status === 'disabled') return '账号禁用'
  if (item.status === 'incomplete') return '配置不完整，请检查 access token、账号类型或代理'
  if (item.status === 'invalid') return '账号鉴权异常'
  return '账号正常可用'
}

export function statusRawError(item: Account): string {
  const raw = String(item.last_error || '').trim()
  if (!raw) return ''
  const human = String(item.status_reason || '').trim()
  return raw === human ? '' : raw
}

export function rowClass(item: Account): string {
  const category = statusCategory(item)
  if (category === 'disabled') return 'bg-muted/50'
  if (category === 'abnormal') return 'bg-rose-500/5'
  if (category === 'limited') return 'bg-amber-500/5'
  if (!item.access_token && !item.cookie) return 'bg-muted/30'
  return ''
}

export function quotaLabel(key: QuotaKey): string {
  if (key === 'fast') return '文本 fast'
  if (key === 'thinking') return '文本 thinking'
  if (key === 'pro') return '文本 pro'
  if (key === 'image') return '图片'
  if (key === 'music') return '音乐'
  return '视频'
}

export function quotaLineText(line: QuotaLine): string {
  if (line.limit < 0 || !line.limited) return `${line.used}/∞`
  return `${line.used}/${line.limit}`
}

function isLowRemaining(line: QuotaLine): boolean {
  if (line.limit < 0 || !line.limited || line.remaining <= 0) return false
  const threshold = Math.max(1, Math.min(3, Math.floor(line.limit * 0.1)))
  return line.remaining <= threshold
}

export function quotaLineClass(item: Account, key: QuotaKey, line?: QuotaLine): string {
  const target = line || quotaLine(item, key)
  const state = getStateByQuotaKey(getGroupStates(item), key)

  if (state?.level === 'error') return PILL_TONE_CLASS.danger
  if (state?.level === 'warn' && !(target.limited && target.remaining <= 0)) return PILL_TONE_CLASS.warning
  if (target.limit < 0 || !target.limited) return PILL_TONE_CLASS.neutral
  if (target.remaining <= 0) return PILL_TONE_CLASS.danger
  if (isLowRemaining(target)) return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.success
}

export function quotaSummaryClass(item: Account): string {
  if (item.lane_backoff_summary?.active) return PILL_TONE_CLASS.warning

  const proEnabled = Array.isArray(item.lanes) && item.lanes.includes('pro')
  if (!proEnabled) return PILL_TONE_CLASS.neutral

  const proState = getStateByQuotaKey(getGroupStates(item), 'pro')
  const proLine = quotaLine(item, 'pro')

  if (proState?.status === 'cooldown') return PILL_TONE_CLASS.warning
  if (proLine.limited && proLine.remaining <= 0) return PILL_TONE_CLASS.danger
  if (proLine.limit < 0 || !proLine.limited) return PILL_TONE_CLASS.neutral
  if (isLowRemaining(proLine)) return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.success
}

export function quotaSummaryText(): string {
  return '图片额度'
}

export function laneEnabled(lanes: AccountLane[], lane: AccountLane): boolean {
  return lanes.includes(lane)
}

function laneCount(lanes: AccountLane[]): number {
  return laneOrder.filter((lane) => lanes.includes(lane)).length
}

export function laneSummaryClass(lanes: AccountLane[]): string {
  const enabledCount = laneCount(lanes)
  if (enabledCount === laneOrder.length) return PILL_TONE_CLASS.success
  if (enabledCount === 0) return PILL_TONE_CLASS.neutral
  return PILL_TONE_CLASS.warning
}

export function laneSummaryText(lanes: AccountLane[]): string {
  return `${laneCount(lanes)}/${laneOrder.length}`
}

export function laneLineClass(lane: AccountLane, lanes: AccountLane[]): string {
  if (!laneEnabled(lanes, lane)) return 'text-muted-foreground'
  if (lane === 'fast') return 'bg-emerald-500/10 text-emerald-700'
  if (lane === 'thinking') return 'bg-cyan-500/10 text-cyan-700'
  return 'bg-blue-500/10 text-blue-700'
}

export function accountPrimaryText(item: Account): string {
  return cleanString(item.email) || cleanString(item.user_id) || cleanString(item.name) || item.id
}

export function accountSecondaryText(item: Account): string {
  const userId = cleanString(item.user_id)
  const email = cleanString(item.email)
  if (email && userId) return userId
  return item.id
}

export function accountSourceText(item: Account): string {
  const type = cleanString(item.type) || 'free'
  const sourceType = cleanString(item.source_type) || 'web'
  return `${type} / ${sourceType}`
}

export function accountProxyText(item: Account): string {
  return proxyReferenceLabel(item.proxy)
}

export function accountTokenPreview(item: Account): string {
  const masked = cleanString(item.cookie)
  if (masked) return masked
  const token = cleanString(item.access_token)
  if (!token) return '缺失'
  if (token.length <= 12) return '********'
  return `${token.slice(0, 6)}...${token.slice(-4)}`
}

export function accountQuotaText(item: Account): string {
  if (item.image_quota_unknown) return '未知'
  return `${Math.max(0, Number(item.quota || 0))}`
}

export function accountCreatedText(item: Account): string {
  return formatAccountDate(item.created_at)
}

export function accountRestoreText(item: Account): string {
  return formatAccountDate(item.restore_at)
}

export function checkoutChannelText(value: unknown): string {
  return cleanString(value) ? 'UPI' : ''
}

export function checkoutFinalKindText(value: unknown): string {
  const kind = cleanString(value).toLowerCase()
  const labels: Record<string, string> = {
    upi_instructions: 'Stripe UPI 指引',
    stripe_upi_instructions: 'Stripe UPI 指引',
    stripe_upi: 'Stripe UPI 指引',
  }
  return labels[kind] || cleanString(value)
}

function checkoutPaymentMethodText(value: unknown): string {
  return cleanString(value) ? 'UPI' : ''
}

function checkoutAmountText(item: Account): string {
  const minor = item.checkout_amount_minor
  if (minor === undefined || minor === null || !Number.isFinite(Number(minor))) return ''
  const currency = cleanString(item.checkout_currency).toUpperCase()
  if (!currency) return `${Math.max(0, Math.trunc(Number(minor)))}`
  const fractionDigits = 2
  const amount = Math.max(0, Math.trunc(Number(minor))) / (10 ** fractionDigits)
  try {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency,
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    }).format(amount)
  } catch {
    return `${currency} ${amount.toFixed(fractionDigits)}`
  }
}

/**
 * The legacy `checkout_url` was an OpenAI bootstrap URL.  Only use it as a
 * final-link alias after the backend explicitly supplies a final-link kind.
 */
export function checkoutFinalLinkUrl(item: Account): string {
  const finalUrl = cleanString(item.checkout_final_url)
  if (finalUrl) return finalUrl
  return cleanString(item.checkout_final_kind) ? cleanString(item.checkout_url) : ''
}

export function checkoutFinalLinkStatusLabel(item: Account): string {
  if (checkoutFinalLinkUrl(item)) {
    const kind = checkoutFinalKindText(item.checkout_final_kind)
    return kind ? `已就绪 · ${kind}` : '已就绪'
  }
  const status = cleanString(item.checkout_link_status).toLowerCase()
  if (status === 'pending' || status === 'processing') return '提取中'
  if (status === 'failed') return '提取失败'
  return '未提取'
}

export function checkoutFinalLinkToneClass(item: Account): string {
  if (checkoutFinalLinkUrl(item)) return PILL_TONE_CLASS.success
  const status = cleanString(item.checkout_link_status).toLowerCase()
  if (status === 'failed') return PILL_TONE_CLASS.danger
  if (status === 'pending' || status === 'processing') return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.neutral
}

/** A compact, list-friendly checkout state. Detailed protocol diagnostics stay in registration logs. */
export function checkoutExtractionStatusLabel(item: Account): string {
  if (checkoutFinalLinkUrl(item)) return '是'
  const status = cleanString(item.checkout_link_status).toLowerCase()
  if (status === 'pending' || status === 'processing') return '提链中'
  return '否'
}

export function checkoutExtractionToneClass(item: Account): string {
  return checkoutFinalLinkToneClass(item)
}

export function checkoutFinalLinkDetail(item: Account): string {
  const details = [
    `状态：${checkoutFinalLinkStatusLabel(item)}`,
  ]
  const channel = checkoutChannelText(item.checkout_channel)
  const country = cleanString(item.checkout_country).toUpperCase()
  const currency = cleanString(item.checkout_currency).toUpperCase()
  const paymentMethod = checkoutPaymentMethodText(item.checkout_payment_method)
  const amount = checkoutAmountText(item)
  if (channel) details.push(`渠道：${channel}`)
  if (country || currency) details.push(`地区：${[country, currency].filter(Boolean).join(' / ')}`)
  if (paymentMethod) details.push(`支付方式：${paymentMethod}`)
  if (item.checkout_is_free_trial) details.push('权益：免费试用')
  else if (amount) details.push(`金额：${amount}`)
  const qrExpiresAt = item.checkout_qr_expires_at_unix || item.checkout_qr_expires_at
  if (qrExpiresAt) {
    const numeric = Number(qrExpiresAt)
    const value = Number.isFinite(numeric) && numeric > 0
      ? formatAccountDate(numeric > 1e12 ? numeric / 1000 : numeric)
      : cleanString(qrExpiresAt)
    if (value) details.push(`二维码到期：${value}`)
  }
  if (!checkoutFinalLinkUrl(item) && cleanString(item.checkout_last_error)) {
    details.push(`错误：${cleanString(item.checkout_last_error)}`)
  }
  return details.join('\n')
}

export function accountStatusDetailText(
  item: Account,
  groupLabel: (groupId: string | undefined) => string,
  proxyText: (account: Account) => string = accountProxyText,
): string {
  return [
    statusReason(item),
    ...accountPoolStateLines(item),
    `账号组：${groupLabel(item.group_id)}`,
    `代理：${proxyText(item)}`,
  ].filter(Boolean).join('\n')
}

export function isAutoPaused(item: Account, now = Date.now()): boolean {
  const until = Date.parse(cleanString(item.auto_paused_until))
  return Number.isFinite(until) && until > now
}

export function isReloginPending(item: Account): boolean {
  return Boolean(cleanString(item.relogin_needed_at))
}

/** Short badge text for GPT pool states that sit beside the main status. */
export function accountPoolStateText(item: Account): string {
  const parts: string[] = []
  if (isAutoPaused(item)) parts.push('自动暂停')
  if (isReloginPending(item)) parts.push(item.relogin_attempts ? `待重登 ${item.relogin_attempts}/3` : '待重登')
  return parts.join(' · ')
}

function accountPoolStateLines(item: Account): string[] {
  const lines: string[] = []
  if (isAutoPaused(item)) {
    const until = formatAccountDate(Date.parse(cleanString(item.auto_paused_until)) / 1000)
    const reason = cleanString(item.auto_pause_reason)
    lines.push(`自动暂停至 ${until}${reason ? `（${reason}）` : ''}，仅降低优先级`)
  }
  if (isReloginPending(item)) {
    const error = cleanString(item.last_relogin_error)
    lines.push(`凭据失效待自动重新登录，已尝试 ${item.relogin_attempts || 0} 次${error ? `，最近错误：${error}` : ''}`)
  }
  return lines
}

/** 额度重置的一句话描述（卡片视图用；表格里由 AccountQuotaCell 实时显示）。 */
export function accountQuotaResetLine(item: Account, now: number): string {
  if (item.quota_state === 'unlimited') return '不限'
  if (item.quota_reset_idle) return '未开始计时'
  const reset = item.quota_reset_at ?? item.restore_at
  if (!reset) return '重置时间未知'
  if (reset <= now) return '已过重置点'
  return `${remainingText(reset, now).replace('剩 ', '')}后重置`
}

export function accountDetailItems(item: Account, now = Math.floor(Date.now() / 1000)) {
  const survival = item.survival_status
    ? `${item.survival_status}${item.survival_observed_seconds ? ` · ${formatDuration(item.survival_observed_seconds)}` : ''}`
    : '-'
  const egress = item.egress
  const egressText = egress
    ? `${egress.label}${egress.switch_to ? ` → ${egress.switch_to_label}` : ''}${egress.usable ? '' : '（不可用）'}`
    : accountProxyText(item) || '默认出口'
  return [
    { label: '创建时间', value: accountCreatedText(item) },
    { label: '图片额度', value: `${item.quota_label || accountQuotaText(item)} · ${accountQuotaResetLine(item, now)}` },
    { label: '出口', value: egressText },
    { label: '分组', value: item.group_name || '—' },
    { label: '成功 / 失败', value: `${item.success_count || 0} / ${item.failure_count || 0} · ${accountFailureRateText(item)}` },
    { label: '最近使用', value: item.last_used_at ? `${agoText(item.last_used_at, now)}（${formatAccountDate(item.last_used_at)}）` : '从未使用' },
    { label: '存活确认', value: survival },
  ]
}

export function accountGroupNameMap(groups: readonly AccountGroup[]): Map<string, string> {
  return new Map(groups.map((group) => [group.id, group.name || group.id]))
}

export function accountGroupLabel(groupId: string | undefined, groupNames: ReadonlyMap<string, string>): string {
  const id = cleanString(groupId)
  if (!id) return '未分组'
  return groupNames.get(id) || id
}

export function accountGroupProxyLabel(group: AccountGroup, proxyGroups: readonly ProxyGroup[]): string {
  const legacyProxyGroupId = cleanString(group.proxy_group_id)
  const proxyReference = parseProxyReference(group.proxy || (legacyProxyGroupId ? `group:${legacyProxyGroupId}` : ''))
  const proxyGroup = proxyReference.mode === 'group'
    ? proxyGroups.find((item) => item.id === proxyReference.value)
    : null
  if (proxyReference.mode === 'global') return '使用默认出口'
  if (proxyReference.mode === 'direct') return '强制直连'
  if (proxyReference.mode === 'group') return `代理组：${proxyGroup?.name || proxyReference.value || '-'}`
  if (proxyReference.mode === 'profile') return `历史代理：${proxyReference.value || '-'}`
  return `自定义代理：${proxyReference.value || '-'}`
}

export function buildAccountGroupRows(
  groups: readonly AccountGroup[],
  proxyGroups: readonly ProxyGroup[],
): AccountGroupRow[] {
  return groups.map((group) => ({
    ...group,
    raw: group,
    name: group.name || group.id,
    account_count: Number(group.account_count || 0),
    proxy_label: accountGroupProxyLabel(group, proxyGroups),
  }))
}

export function buildAccountProgressMetricItems(
  metricLabel: string,
  metricValue: string | number,
  statusTextValue: string,
): AccountProgressMetricItem[] {
  return [
    {
      key: 'metric',
      label: metricLabel,
      value: metricValue,
    },
    {
      key: 'status',
      label: '状态',
      value: statusTextValue,
    },
  ]
}

// ── GPT 号池调度视图（后端 scheduling_view）：与“健康”列互补，回答“此刻调度会怎么对待这个号” ──
export type AccountScheduleCode =
  | 'unknown' | 'ready' | 'busy' | 'cooldown' | 'ramp' | 'ramp_full' | 'cooling'
  | 'auto_paused' | 'auth_pending' | 'no_credential' | 'egress_switching' | 'egress_down'

const SCHEDULE_LABELS: Record<AccountScheduleCode, string> = {
  unknown: '—',
  ready: '可接请求',
  busy: '并发已满',
  cooldown: '新号冷静期',
  ramp: '放量期',
  ramp_full: '今日已满',
  cooling: '失败冷却',
  auto_paused: '自动暂停',
  auth_pending: '待核验',
  no_credential: '凭据失效',
  egress_switching: '换出口中',
  egress_down: '出口不可用',
}

const SCHEDULE_TONE: Record<AccountScheduleCode, string> = {
  unknown: 'border-border bg-muted/30 text-muted-foreground',
  ready: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  busy: 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  cooldown: 'border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300',
  ramp: 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300',
  ramp_full: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  cooling: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  auto_paused: 'border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-300',
  auth_pending: 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  no_credential: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  egress_switching: 'border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300',
  egress_down: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300',
}

/** 同时命中多种状态时，按“最能解释为什么现在不接请求”的顺序取一个。 */
export function accountScheduleCode(item: Account): AccountScheduleCode {
  const s = item.scheduling
  if (item.egress?.switch_to) return 'egress_switching'
  if (!s) return 'unknown'
  if (item.egress && !item.egress.usable) return 'egress_down'
  if (s.credential_status === 'unavailable') return 'no_credential'
  if (s.auth_pending) return 'auth_pending'
  if (s.new_account_stage === 'cooldown') return 'cooldown'
  if (s.auto_paused_until) return 'auto_paused'
  if (s.failure_cooldown_until) return 'cooling'
  if (s.new_account_stage === 'ramp' && s.ramp_exhausted) return 'ramp_full'
  if (s.image_inflight >= s.image_concurrency && s.text_inflight >= s.text_concurrency) return 'busy'
  if (s.new_account_stage === 'ramp') return 'ramp'
  return 'ready'
}

export function accountScheduleText(item: Account): string {
  return SCHEDULE_LABELS[accountScheduleCode(item)]
}

export function accountScheduleClass(item: Account): string {
  return SCHEDULE_TONE[accountScheduleCode(item)]
}

function scheduleTime(seconds: number): string {
  const date = new Date(seconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}-${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 悬停详情：每种可能影响调度的因素一行，没有的不显示。 */
export function accountScheduleDetail(item: Account): string {
  const s = item.scheduling
  if (!s) return ''
  const lines: string[] = []
  const egress = item.egress
  if (egress?.switch_to) {
    lines.push(`换到${egress.switch_to_label}前先停用，期间不接任何请求；${egress.switch_at ? `${scheduleTime(egress.switch_at)} ` : '到点'}自动切换并恢复`)
  } else if (egress && !egress.usable) {
    lines.push('绑定的出口槽位没有可用节点（REJECT），不接请求；请到「代理 / 出口槽位」给槽位选节点')
  }
  if (s.credential_status === 'unavailable') lines.push('AT 已失效且没有可用 RT，不参与调度（需重新登录或更新凭据）')
  else if (s.credential_status === 'recoverable') lines.push('AT 已失效，将用 RT 刷新后恢复')
  if (s.auth_pending) lines.push(`上游返回 401，正在核验（来源：${s.auth_pending_event || '未知'}），核验前暂不接请求`)
  if (s.new_account_stage === 'cooldown' && s.cooldown_until) lines.push(`新号冷静期至 ${scheduleTime(s.cooldown_until)}，期间不接生图和对话`)
  if (s.new_account_stage === 'ramp') {
    lines.push(`放量期至 ${s.ramp_until ? scheduleTime(s.ramp_until) : '—'}，今日生图 ${s.ramp_used_today}/${s.ramp_daily_limit} 张`)
    if (s.ramp_exhausted && s.ramp_resume_at) lines.push(`今日上限已满，${scheduleTime(s.ramp_resume_at)} 恢复生图（对话不受限）`)
  }
  if (s.failure_cooldown_until) lines.push(`连续失败冷却至 ${scheduleTime(s.failure_cooldown_until)}（短时，只在没有其它号时才用）`)
  if (s.auto_paused_until) lines.push(`自动暂停至 ${scheduleTime(s.auto_paused_until)}${s.auto_pause_reason ? `：${s.auto_pause_reason}` : ''}`)
  if (s.failure_streak > 0) lines.push(`当前连续失败 ${s.failure_streak} 次`)
  lines.push(`并发占用：生图 ${s.image_inflight}/${s.image_concurrency}，对话 ${s.text_inflight}/${s.text_concurrency}`)
  const credential = { valid: '有效', expiring: '即将过期', invalid: '已失效' }[s.access_token_status]
  const refresh = { valid: '有', missing: '缺失', invalid: '已失效' }[s.refresh_token_status]
  lines.push(`凭据：AT ${credential}，RT ${refresh}`)
  return lines.join('\n')
}

/** 行签名用：调度视图里会变的字段（后端秒级时间点原样参与即可）。 */
export function accountScheduleSignature(item: Account): string {
  const s = item.scheduling
  if (!s) return ''
  return [
    s.new_account_stage, s.cooldown_until, s.ramp_until, s.ramp_used_today, s.ramp_exhausted,
    s.failure_cooldown_until, s.failure_streak, s.auto_paused_until, s.image_inflight, s.text_inflight,
    s.auth_pending, s.credential_status,
    item.egress?.switch_to, item.egress?.switch_at, item.egress?.usable,
  ].join(',')
}

// ── 时间展示（凭据 / 额度 / 出口单元格共用） ─────────────────────
function pad2(value: number) {
  return String(value).padStart(2, '0')
}

/** 10-17 20:31（今天的只显示 20:31） */
export function shortTime(seconds: number | null | undefined, now: number): string {
  if (!seconds) return '—'
  const date = new Date(seconds * 1000)
  const today = new Date(now * 1000)
  const hm = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  if (date.toDateString() === today.toDateString()) return hm
  return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${hm}`
}

/** 只按小时显示：剩 70 小时 / 不到 1 小时 */
export function remainingText(seconds: number | null | undefined, now: number): string {
  if (!seconds) return ''
  const hours = Math.floor((seconds - now) / 3600)
  return hours >= 1 ? `剩 ${hours} 小时` : '不到 1 小时'
}

/** 距今多久：刚刚 / 12 分钟前 / 3 小时前 / 2 天前 */
export function agoText(seconds: number | null | undefined, now: number): string {
  if (!seconds) return '从未'
  const diff = Math.max(0, now - seconds)
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

/** 失败率：总次数太少（<5）时不下结论 */
export function failureRate(item: Account): number | null {
  const success = Number(item.success_count || 0)
  const failure = Number(item.failure_count || 0)
  const total = success + failure
  return total >= 5 ? failure / total : null
}

/** 额度单元格的悬停说明：数值来源 + 同步时间 */
export function quotaTooltip(item: Account, now: number): string {
  if (item.quota_state === 'unlimited') return '无限额度'
  if (item.quota_state === 'unknown') return '额度未知：本地扣到 0 或还没同步过，下次调度会远程确认'
  const synced = item.quota_synced_at ? `${shortTime(item.quota_synced_at, now)}（${agoText(item.quota_synced_at, now)}）` : '从未'
  if (item.quota_source === 'estimated') return `估算值：上次远程同步于 ${synced}，之后按本地出图次数扣减`
  return `远程同步值：同步于 ${synced}`
}

export const scheduleCodeLabels: Record<string, string> = SCHEDULE_LABELS

// ── 更多筛选：来源 / 套餐 / 凭据风险 / 表现 / 出口（值和后端 _account_matches_flag 对应）──
export const accountFlagFilterGroups = [
  { label: '更多筛选', options: [{ label: '更多筛选', value: 'all' }] },
  {
    label: '凭据风险',
    options: [
      { label: 'RT 缺失或失效', value: 'rt_missing' },
      { label: 'AT 失效后救不回', value: 'no_rescue' },
    ],
  },
  {
    label: '表现',
    options: [
      { label: '失败率高（≥40%）', value: 'high_failure' },
      { label: '24 小时没用过', value: 'idle' },
      { label: '自动暂停中', value: 'auto_paused' },
    ],
  },
  {
    label: '出口',
    options: [
      { label: '未分配槽位', value: 'egress_direct' },
      { label: '换出口中', value: 'egress_switching' },
      { label: '出口不可用', value: 'egress_down' },
    ],
  },
  {
    label: '来源',
    options: [
      { label: '网页登录', value: 'source:web' },
      { label: 'CPA / Sub2API 导入', value: 'source:codex' },
    ],
  },
  {
    label: '套餐',
    options: ['free', 'plus', 'pro', 'team'].map((plan) => ({
      label: plan === 'free' ? 'Free' : plan[0].toUpperCase() + plan.slice(1),
      value: `plan:${plan}`,
    })),
  },
]
