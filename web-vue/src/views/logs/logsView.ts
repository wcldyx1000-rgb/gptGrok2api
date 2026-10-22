import type { GalleryFile } from '@/api/gallery'
import type { RuntimeLog, SystemLogsResponse, SystemLogRow } from '@/api/logs'
import type { ActionMenuItem } from 'nanocat-ui'
import { actionMenuGroups } from '@/components/ai/menuItems'
import {
  isSystemLogFailed as isFailed,
  isSystemLogLimited as isLimited,
  isSystemLogSuccess as isSuccess,
} from '@/api/logs'

export type LogFilterOption = { label: string; value: string }
export type LogGroupedSelectOption = LogFilterOption & { disabled?: boolean }
export type LogGroupedSelectGroup = {
  label?: string
  options: LogGroupedSelectOption[]
}

export type SystemLogFilters = {
  search: string
  startDate: string
  endDate: string
  status: string
  endpoint: string
  model: string
  account: string
  conversationId: string
  type: string
}

export type RuntimeLogFilters = {
  level: string
  source: string
  search: string
}

export type AdvancedLogFilterKey = 'type' | 'status' | 'model' | 'account'

export type LogMetricItem = {
  label: string
  value: string | number
  class: string
}

export type LogStatusTone = 'success' | 'danger' | 'warning' | 'info' | 'muted'

type RuntimeFilterKey = 'level' | 'source'

type AdvancedConditionGroup = {
  key: AdvancedLogFilterKey
  label: string
  options: LogFilterOption[]
}

export type LogPreviewImage = {
  url: string
  title?: string
  filename?: string
  alt?: string
  broken?: boolean
}

export type SystemLogRowSignatureInput = {
  selected: boolean
  firstImageBroken: boolean
}

export const typeOptions = [
  { label: '调用日志', value: 'call' },
  { label: '账号日志', value: 'account' },
  { label: '全部类型', value: '' },
]

export const systemLogPageSizeOptions = [20, 50, 100, 200, 500]

export const runtimeLimitOptions = [
  { label: '100', value: '100' },
  { label: '300', value: '300' },
  { label: '500', value: '500' },
  { label: '1000', value: '1000' },
  { label: '2000', value: '2000' },
]

export const runtimeLevelOptions = [
  { label: '全部级别', value: '' },
  { label: 'debug', value: 'debug' },
  { label: 'info', value: 'info' },
  { label: 'warning', value: 'warning' },
  { label: 'error', value: 'error' },
]

export const runtimeSourceOptions = [
  { label: '全部来源', value: '' },
  { label: '内存日志', value: 'memory' },
  { label: '文件尾部', value: 'file' },
]

export const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '成功', value: 'success' },
  { label: '失败', value: 'failed' },
  { label: '限流/受限', value: 'limited' },
  { label: '部分成功', value: 'partial' },
  { label: '文本回复', value: 'text' },
]

export const systemQuickFilterOptions: LogGroupedSelectOption[] = [
  { label: '只看失败', value: 'quick:status:failed' },
  { label: '图生图', value: 'quick:endpoint:/v1/images/edits' },
  { label: '文生图', value: 'quick:endpoint:/v1/images/generations' },
]

export const quickEndpointValues = ['/v1/images/edits', '/v1/images/generations'] as const

export const systemQuickFilterGroups: LogGroupedSelectGroup[] = [
  { options: systemQuickFilterOptions },
]

export function cleanLogString(value: unknown): string {
  if (value === undefined || value === null) return ''
  return String(value).trim()
}

function signatureValue(value: unknown): string {
  return cleanLogString(value).replaceAll('|', '/')
}

function boundedSignatureText(value: unknown, limit = 180): string {
  const text = signatureValue(value)
  if (text.length <= limit) return text
  return `${text.length}:${text.slice(0, limit)}:${text.slice(-24)}`
}

export function currentMenuLabel(label: string, active: boolean): string {
  return active ? `${label}（当前）` : label
}

export function buildRuntimeFilterMenuItems(filters: Pick<RuntimeLogFilters, 'level' | 'source'>): ActionMenuItem[] {
  return actionMenuGroups<ActionMenuItem>(
    runtimeLevelOptions
      .filter((item) => item.value)
      .map((item) => ({
        key: `runtime-level:${item.value}`,
        label: currentMenuLabel(`级别 ${item.label}`, filters.level === item.value),
      })),
    runtimeSourceOptions
      .filter((item) => item.value)
      .map((item) => ({
        key: `runtime-source:${item.value}`,
        label: currentMenuLabel(item.label, filters.source === item.value),
      })),
    [
      { key: 'runtime-clear:level', label: '清除级别筛选', disabled: !filters.level },
      { key: 'runtime-clear:source', label: '清除来源筛选', disabled: !filters.source },
    ],
  )
}

export function parseRuntimeFilterMenuKey(key: string): { key: RuntimeFilterKey; value: string } | null {
  if (key.startsWith('runtime-level:')) {
    return { key: 'level', value: key.slice('runtime-level:'.length) }
  }
  if (key.startsWith('runtime-source:')) {
    return { key: 'source', value: key.slice('runtime-source:'.length) }
  }
  if (key === 'runtime-clear:level') return { key: 'level', value: '' }
  if (key === 'runtime-clear:source') return { key: 'source', value: '' }
  return null
}

export function normalizeRuntimeLimit(value: unknown, fallback = 500): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.min(Math.max(Math.trunc(parsed), 1), 2000) : fallback
}

export function optionFromFacet(facet: Record<string, number>, allLabel: string): LogFilterOption[] {
  return [
    { label: allLabel, value: '' },
    ...Object.keys(facet)
      .map(cleanLogString)
      .filter(Boolean)
      .sort((a, b) => a.localeCompare(b))
      .map((value) => ({ label: `${value} (${facet[value] || 0})`, value })),
  ]
}

export function buildAdvancedConditionMenuGroups(
  modelFacet: Record<string, number>,
  accountFacet: Record<string, number>,
): LogGroupedSelectGroup[] {
  const groups: AdvancedConditionGroup[] = [
    {
      key: 'type',
      label: '类型',
      options: [
        { label: '调用日志', value: 'call' },
        { label: '账号日志', value: 'account' },
        { label: '全部类型', value: '' },
      ],
    },
    {
      key: 'status',
      label: '状态',
      options: statusOptions,
    },
    {
      key: 'model',
      label: '模型',
      options: optionFromFacet(modelFacet, '全部模型'),
    },
    {
      key: 'account',
      label: '账号',
      options: optionFromFacet(accountFacet, '全部账号'),
    },
  ]

  return groups.map((group) => ({
    label: group.label,
    options: group.options.map((option) => ({
      label: option.label,
      value: advancedConditionOptionValue(group.key, option.value),
    })),
  }))
}

export function typeLabel(type: string): string {
  if (type === 'call') return '调用日志'
  if (type === 'account') return '账号日志'
  return type || '日志'
}

export function tokenLabel(item: SystemLogRow): string {
  return item.keyName || item.keyId || item.accountEmail
}

export function summaryText(item: SystemLogRow): string {
  return item.summary || item.error || item.reason || item.preview
}

export type LogOutcome = 'success' | 'partial_success' | 'text_review' | 'rate_limited' | 'failed' | 'running' | 'unknown'

/** 调用结果：优先用后端写入的 outcome，旧日志按状态/错误推导。 */
export function logOutcome(item: SystemLogRow): LogOutcome {
  const status = item.status.toLowerCase()
  if (status === 'running' || status === 'cancelling') return 'running'
  if (['success', 'partial_success', 'text_review', 'rate_limited', 'failed'].includes(item.outcome)) {
    return item.outcome as LogOutcome
  }
  if (isSuccess(item)) return 'success'
  if (isFailed(item)) return isLimited(item) ? 'rate_limited' : 'failed'
  if (isLimited(item)) return 'rate_limited'
  return 'unknown'
}

const outcomePresentation: Record<LogOutcome, { label: string; tone: LogStatusTone }> = {
  success: { label: '成功', tone: 'success' },
  partial_success: { label: '部分成功', tone: 'warning' },
  text_review: { label: '文本', tone: 'warning' },
  rate_limited: { label: '限流', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
  running: { label: '运行中', tone: 'info' },
  unknown: { label: '记录', tone: 'muted' },
}

export function statusLabel(item: SystemLogRow): string {
  const status = item.status.toLowerCase()
  if (status === 'cancelling') return '取消中'
  const outcome = logOutcome(item)
  if (outcome === 'unknown') return item.status || outcomePresentation.unknown.label
  return outcomePresentation[outcome].label
}

export function statusTone(item: SystemLogRow): LogStatusTone {
  return outcomePresentation[logOutcome(item)].tone
}

export type LogBusinessKind =
  | 'account' | 'image_generation' | 'image_edit' | 'image_chat'
  | 'chat' | 'responses' | 'messages' | 'search' | 'file' | 'other'

const businessLabels: Partial<Record<LogBusinessKind, string>> = {
  account: '账号操作',
  image_generation: '文生图',
  image_edit: '图生图',
  image_chat: '对话生图',
  chat: '对话',
  responses: '响应',
  messages: '消息',
  search: '搜索',
  file: '文件',
}

export function logBusinessKind(item: SystemLogRow): LogBusinessKind {
  const endpoint = item.endpoint.toLowerCase().replace(/\/+$/, '')
  const model = item.model.toLowerCase()
  if (item.type === 'account') return 'account'
  if (endpoint.endsWith('/images/generations')) return 'image_generation'
  if (endpoint.endsWith('/images/edits')) return 'image_edit'
  if (endpoint.endsWith('/chat/completions')) return model.includes('image') ? 'image_chat' : 'chat'
  if (endpoint.endsWith('/responses')) return model.includes('image') ? 'image_chat' : 'responses'
  if (endpoint.endsWith('/messages')) return 'messages'
  if (endpoint.includes('search')) return 'search'
  if (endpoint.includes('file')) return 'file'
  return 'other'
}

const imageFailureLabels: Record<string, string> = {
  upstream_error: '上游请求失败',
  internal_error: '内部处理异常',
  upstream_unavailable: '上游服务暂不可用',
  upstream_connection_failed: '无法连接上游',
  upstream_connection_timeout: '上游连接超时',
  upstream_rate_limited: '上游服务限流',
  image_poll_timeout: '等待图片结果超时',
  image_stream_timeout: '上游图片流超时',
  image_stream_interrupted: '上游图片流中断',
  image_tool_error: '图片工具异常',
  image_quota_exhausted: '图片额度已用尽',
  file_upload_throttled: '参考图上传受限',
  auth_invalid: '账号登录态失效',
  content_policy_violation: '内容安全策略拒绝',
  invalid_image_input: '图片输入无效',
  upstream_text_reply: '上游仅返回文本',
  no_image_generated: '未生成图片',
  unsupported_model: '模型不支持生图',
  image_download_failed: '图片下载失败',
  task_interrupted: '图片任务被中断',
  no_available_account: '暂无可用账号',
  insufficient_quota: '图片额度不足',
}

export function imageFailureLabel(code: string): string {
  return imageFailureLabels[cleanLogString(code).toLowerCase()] || ''
}

export type LogRowPresentation = {
  requestKind: string
  requestPrimary: string
  requestSecondary: string
  requestParameters: string
  executionPrimary: string
  executionSecondary: string
  resultText: string
  diagnostics: string
  durationTone: LogStatusTone
}

const IMAGE_BUSINESS: LogBusinessKind[] = ['image_generation', 'image_edit', 'image_chat']

function logResultText(item: SystemLogRow, outcome: LogOutcome, business: LogBusinessKind): string {
  const requested = item.imageRequestedCount
  const succeeded = item.imageSucceededCount || item.resultImageCount
  if (outcome === 'partial_success') return `生成 ${succeeded}/${Math.max(requested, succeeded)} 张图片`
  if (outcome === 'text_review') return '上游返回文本'
  if (outcome === 'failed' || outcome === 'rate_limited') {
    return item.error || imageFailureLabel(item.errorCode) || item.summary || '调用失败'
  }
  if (succeeded > 0 && IMAGE_BUSINESS.includes(business)) {
    return requested > 1 ? `生成 ${succeeded}/${requested} 张图片` : `生成 ${succeeded} 张图片`
  }
  if (outcome === 'success') {
    if (IMAGE_BUSINESS.includes(business)) return '图片生成完成'
    if (business === 'search') return '搜索完成'
    if (['chat', 'responses', 'messages'].includes(business)) return '文本响应完成'
    if (business === 'account') return item.summary || '账号操作完成'
    return '调用完成'
  }
  return item.summary || item.error || '调用完成'
}

export function logRowPresentation(item: SystemLogRow): LogRowPresentation {
  const outcome = logOutcome(item)
  const business = logBusinessKind(item)
  const parameters = [
    item.requestSize ? `请求 ${item.requestSize}` : '',
    item.requestQuality ? `质量 ${item.requestQuality}` : '',
    item.requestResponseFormat ? `返回 ${item.requestResponseFormat}` : '',
  ].filter(Boolean).join(' · ')
  const caller = item.keyName || item.keyId
  const account = item.accountEmail || item.accountId
  const executionParts = [
    item.imageRequestedCount > 0 ? `${item.imageRequestedCount} 个任务` : '',
    item.imageAttemptCount > 0 ? `${item.imageAttemptCount} 次尝试` : '',
  ].filter(Boolean)
  const showFailure = outcome === 'failed' || outcome === 'rate_limited'
  const durationMs = Number(item.durationMs) || 0
  return {
    requestKind: business === 'account' ? '' : (businessLabels[business] || typeLabel(item.type)),
    requestPrimary: item.model || typeLabel(item.type),
    requestSecondary: item.endpoint,
    requestParameters: parameters,
    executionPrimary: account || caller || '-',
    executionSecondary: [executionParts.join(' · '), account ? caller : '']
      .filter(Boolean).join(' · '),
    resultText: logResultText(item, outcome, business),
    diagnostics: showFailure
      ? [Number(item.statusCode) > 0 ? `HTTP ${item.statusCode}` : '', item.errorCode].filter(Boolean).join(' · ')
      : '',
    durationTone: outcome === 'failed' ? 'danger' : durationMs >= 60_000 ? 'warning' : 'success',
  }
}

export function systemLogRowSignature(item: SystemLogRow, input: SystemLogRowSignatureInput): string {
  return [
    item.id,
    input.selected ? 1 : 0,
    input.firstImageBroken ? 1 : 0,
    boundedSignatureText(item.time),
    boundedSignatureText(typeLabel(item.type), 64),
    boundedSignatureText(tokenLabel(item), 96),
    boundedSignatureText(item.durationMs, 64),
    boundedSignatureText(item.resolution, 64),
    boundedSignatureText(statusLabel(item), 64),
    statusTone(item),
    boundedSignatureText(summaryText(item)),
    item.imageUrls.length,
    item.imageUrls.slice(0, 4).map((url) => boundedSignatureText(url, 96)).join(','),
    boundedSignatureText(item.preview),
    isFailed(item) ? 1 : 0,
    item.outcome,
    item.imageRequestedCount,
    item.imageAttemptCount,
    item.switchCount,
    boundedSignatureText(item.model, 64),
    boundedSignatureText(item.accountEmail, 96),
  ].map(signatureValue).join('|')
}

export function filenameFromUrl(url: string): string {
  const value = cleanLogString(url)
  if (!value) return '-'
  try {
    const parsed = new URL(value, 'https://local.invalid')
    return decodeURIComponent(parsed.pathname.split('/').pop() || value)
  } catch {
    return decodeURIComponent(value.split(/[/?#]/)[0]?.split('/').pop() || value)
  }
}

export function buildLogPreviewImages(
  item: SystemLogRow | null | undefined,
  isPreviewBroken: (url: string) => boolean,
): LogPreviewImage[] {
  if (!item) return []
  return item.imageUrls.map((url, index) => {
    const sourceUrl = item.urls[index] || url
    return {
      url,
      title: sourceUrl,
      filename: filenameFromUrl(sourceUrl),
      alt: `日志结果图片 ${index + 1}`,
      broken: isPreviewBroken(url),
    }
  })
}

export function buildLogPreviewGalleryFile(image: LogPreviewImage | null | undefined): GalleryFile | null {
  if (!image) return null
  const filename = image.filename || filenameFromUrl(image.title || image.url) || 'log-preview-image'
  return {
    filename,
    path: image.title || image.url,
    url: image.url,
    thumbnail_url: image.url,
    size: 0,
    created_at: '',
    mtime: 0,
    date: '',
    type: 'image',
    expired: false,
    expires_in_seconds: null,
    tags: [],
    storage: 'log',
    local: false,
    webdav: false,
    width: null,
    height: null,
  }
}

export function formatRuntimeLogLine(item: RuntimeLog): string {
  const time = cleanLogString(item.time)
  const level = cleanLogString(item.level).toUpperCase()
  const source = cleanLogString(item.source)
  const message = cleanLogString(item.message) || '-'
  const path = cleanLogString(item.path)
  return [
    time,
    level,
    source ? `[${source}]` : '',
    message,
    path,
  ].filter(Boolean).join(' ')
}

export function runtimeStats(items: readonly RuntimeLog[]) {
  const counts = { total: items.length, warning: 0, error: 0, memory: 0, file: 0 }
  items.forEach((item) => {
    const level = cleanLogString(item.level).toLowerCase()
    const source = cleanLogString(item.source).toLowerCase()
    if (level === 'warning') counts.warning += 1
    if (level === 'error' || level === 'critical') counts.error += 1
    if (source === 'memory') counts.memory += 1
    if (source === 'file') counts.file += 1
  })
  return counts
}

export function systemMetricItems(logMeta: Pick<SystemLogsResponse, 'stats' | 'stats_scope'>): LogMetricItem[] {
  const stats = logMeta.stats
  const pageScope = logMeta.stats_scope === 'page'
  return [
    { label: '总数', value: stats.total, class: 'text-foreground' },
    { label: pageScope ? '本页成功' : '成功', value: stats.success, class: 'text-emerald-600' },
    { label: pageScope ? '本页失败' : '失败', value: stats.failed, class: 'text-rose-600' },
    { label: pageScope ? '本页限流' : '限流', value: stats.limited, class: 'text-amber-600' },
    { label: pageScope ? '本页部分成功' : '部分成功', value: stats.partial_success ?? 0, class: 'text-orange-600' },
    { label: pageScope ? '本页文本' : '文本回复', value: stats.text_review ?? 0, class: 'text-violet-600' },
    { label: pageScope ? '本页图片' : '图片接口', value: stats.image, class: 'text-cyan-600' },
  ]
}

export function runtimeMetricItems(items: readonly RuntimeLog[]): LogMetricItem[] {
  const counts = runtimeStats(items)
  return [
    { label: '运行日志', value: counts.total, class: 'text-foreground' },
    { label: 'Warning', value: counts.warning, class: 'text-amber-600' },
    { label: 'Error', value: counts.error, class: 'text-rose-600' },
    { label: '内存', value: counts.memory, class: 'text-cyan-600' },
    { label: '文件', value: counts.file, class: 'text-violet-600' },
  ]
}

export function activeSystemFilterCount(filters: SystemLogFilters): number {
  return [
    filters.search,
    filters.startDate,
    filters.endDate,
    filters.status,
    filters.endpoint,
    filters.model,
    filters.account,
    filters.conversationId,
    filters.type !== 'call' ? filters.type || 'all' : '',
  ].filter(Boolean).length
}

export function activeRuntimeFilterCount(filters: RuntimeLogFilters): number {
  return [
    filters.level,
    filters.source,
    filters.search,
  ].filter(Boolean).length
}

export function advancedConditionCount(filters: Pick<SystemLogFilters, 'type' | 'status' | 'model' | 'account'>): number {
  return [
    filters.type !== 'call' ? filters.type || 'all' : '',
    filters.status,
    filters.model,
    filters.account,
  ].filter(Boolean).length
}

export function advancedConditionOptionValue(key: AdvancedLogFilterKey, value: string): string {
  return `advanced:${key}:${encodeURIComponent(value)}`
}

export function parseAdvancedConditionOptionValue(key: string): { conditionKey: AdvancedLogFilterKey; value: string } | null {
  const match = key.match(/^advanced:(type|status|model|account):(.*)$/)
  if (!match) return null
  return {
    conditionKey: match[1] as AdvancedLogFilterKey,
    value: decodeURIComponent(match[2] || ''),
  }
}

export function latestAdvancedConditionValue(values: readonly string[], key: AdvancedLogFilterKey): string | null {
  const matched = values
    .map(parseAdvancedConditionOptionValue)
    .filter((item): item is { conditionKey: AdvancedLogFilterKey; value: string } => Boolean(item && item.conditionKey === key))
  if (matched.length === 0) return null
  return matched[matched.length - 1].value
}

export function buildAdvancedConditionSelection(filters: Pick<SystemLogFilters, 'type' | 'status' | 'model' | 'account'>): string[] {
  const values: string[] = []
  if (filters.type !== 'call') values.push(advancedConditionOptionValue('type', filters.type))
  if (filters.status) values.push(advancedConditionOptionValue('status', filters.status))
  if (filters.model) values.push(advancedConditionOptionValue('model', filters.model))
  if (filters.account) values.push(advancedConditionOptionValue('account', filters.account))
  return values
}

export function buildSystemQuickFilterSelection(filters: Pick<SystemLogFilters, 'status' | 'endpoint'>): string[] {
  const values: string[] = []
  if (filters.status === 'failed') values.push('quick:status:failed')
  if (filters.endpoint === '/v1/images/edits') values.push('quick:endpoint:/v1/images/edits')
  if (filters.endpoint === '/v1/images/generations') values.push('quick:endpoint:/v1/images/generations')
  return values
}

export function latestQuickEndpointValue(values: readonly string[]): string | null {
  const matched = values
    .filter((item) => item.startsWith('quick:endpoint:'))
    .map((item) => item.slice('quick:endpoint:'.length))
  return matched.length ? matched[matched.length - 1] : null
}

export function isQuickEndpointValue(value: string): boolean {
  return (quickEndpointValues as readonly string[]).includes(value)
}
