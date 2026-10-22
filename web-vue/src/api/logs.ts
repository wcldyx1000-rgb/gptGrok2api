import apiClient from './client'
import type { AdminLogGroup, AdminLogStats, AdminLogsResponse, LogEntry } from '@/types/api'
import { isImageModelId } from '@/config/modelCatalog'

type LogsListParams = {
  limit?: number
  level?: string
  search?: string
}

export type SystemLog = {
  id?: string
  time?: string
  type?: string
  summary?: string
  detail?: Record<string, any>
}

type BackendLogsResponse = {
  items?: SystemLog[]
  total?: number
  limit?: number
  offset?: number
  has_more?: boolean
  facets_scope?: string
  stats_scope?: string
  total_scope?: string
  facets?: {
    statuses?: Record<string, number>
    endpoints?: Record<string, number>
    models?: Record<string, number>
    accounts?: Record<string, number>
  }
  stats?: {
    total?: number
    success?: number
    failed?: number
    limited?: number
    image?: number
    partial_success?: number
    text_review?: number
  }
}

export type SystemLogsListParams = {
  type?: string
  start_date?: string
  end_date?: string
  status?: string
  endpoint?: string
  model?: string
  account?: string
  conversation_id?: string
  search?: string
  limit?: number
  offset?: number
  /** 默认 true：列表返回精简字段；传 false 取完整 detail */
  compact?: boolean
}

export type SystemLogsResponse = {
  items: SystemLog[]
  total: number
  limit: number
  offset: number
  has_more: boolean
  facets_scope: string
  stats_scope: string
  total_scope: string
  facets: {
    statuses: Record<string, number>
    endpoints: Record<string, number>
    models: Record<string, number>
    accounts: Record<string, number>
  }
  stats: {
    total: number
    success: number
    failed: number
    limited: number
    image: number
    partial_success?: number
    text_review?: number
  }
}

export type RuntimeLog = {
  id?: string
  time?: string
  level?: string
  message?: string
  source?: string
  path?: string
}

type RuntimeLogsResponseRaw = {
  items?: RuntimeLog[]
  total?: number
  limit?: number
  sources?: {
    memory?: boolean
    files?: string[]
  }
}

export type RuntimeLogsListParams = {
  level?: string
  search?: string
  source?: string
  limit?: number
}

export type RuntimeLogsResponse = {
  items: RuntimeLog[]
  total: number
  limit: number
  sources: {
    memory: boolean
    files: string[]
  }
}

export type LogDiagnosisChip = {
  label: string
  tone: 'neutral' | 'success' | 'warning' | 'danger' | 'info'
}

export type LogImageAttempt = {
  slot: number
  attempt: number
  status: string
  account: string
  durationMs: number
  egressLabel: string
  egressKey: string
  proxySource: string
  failureCode: string
  errorType: string
  statusCode: number
  switchedAccount: boolean
  egressFallback: boolean
  retryKind: string
  /** 生成成功但结果交付失败（如图片下载失败）。 */
  deliveryFailed: boolean
}

export type SystemLogRow = {
  id: string
  raw: SystemLog
  /** 实际出图分辨率，如 1024×1024；多张且尺寸一致时为 "1024×1024 ×2"，未知为空 */
  resolution: string
  resultImageCount: number
  requestSize: string
  requestQuality: string
  requestResponseFormat: string
  requestCount: string
  imageAttempts: LogImageAttempt[]
  imageAttemptCount: number
  /** 后端归类的调用结果；旧日志没有该字段时为空，由前端按状态推导。 */
  outcome: string
  imageRequestedCount: number
  imageSucceededCount: number
  imageFailedCount: number
  switchCount: number
  /** 列表返回的是精简字段，详情需按 id 拉取完整记录 */
  detailCompact: boolean
  time: string
  type: string
  summary: string
  endpoint: string
  model: string
  status: string
  keyId: string
  keyName: string
  role: string
  accountEmail: string
  accountId: string
  conversationId: string
  proxySource: string
  proxyHash: string
  egressKey: string
  egressLabel: string
  proxyGroupId: string
  proxyNodeId: string
  proxyNodeName: string
  imageEgressLimit: string
  hasProxy: string
  egressMode: string
  durationMs: string
  statusCode: string
  startedAt: string
  endedAt: string
  requestText: string
  requestTextFull: string
  requestTextTruncated: boolean
  requestShape: string
  error: string
  errorCode: string
  stage: string
  reason: string
  upstreamErrorType: string
  upstreamRequestId: string
  canResumePoll: boolean
  toolInvoked: string
  upstreamMessageLen: string
  blocked: string
  upstreamPreview: string
  rawUpstreamMessage: string
  rawUpstreamError: string
  urls: string[]
  imageUrls: string[]
  diagnosisChips: LogDiagnosisChip[]
  preview: string
  rawJson: string
}

export type NormalizeSystemLogRowOptions = {
  apiBaseUrl?: string
}

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

function formatDetailValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return ''
  if (Array.isArray(value)) return value.map(formatDetailValue).filter(Boolean).join(' · ')
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, item]) => item !== undefined && item !== null && item !== '')
    if (!entries.length) return ''
    const primitive = entries.every(([, item]) => !item || ['string', 'number', 'boolean'].includes(typeof item))
    if (primitive && entries.length <= 8) {
      return entries.map(([key, item]) => `${key}: ${formatDetailValue(item)}`).join(' · ')
    }
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  return String(value).trim()
}

function normalizeLevel(item: SystemLog): LogEntry['level'] {
  const detail = item.detail || {}
  const status = cleanString(detail.status).toLowerCase()
  const error = cleanString(detail.error)
  const errorCode = cleanString(detail.error_code || detail?.diagnosis?.error_code)
  if (status === 'failed' || error || errorCode) return 'ERROR'
  if (status === 'warning' || status === 'limited') return 'WARNING'
  return 'INFO'
}

const LOG_IMAGE_URL_RE = /!\[[^\]]*\]\(((?:https?:\/\/|\/images\/|\/image-thumbnails\/)[^\s)"']+)\)/g

function isImageChatLog(endpoint: string, model: string): boolean {
  return endpoint.includes('/v1/chat') && isImageModelId(model)
}

function isImageEndpointLog(endpoint: string, model = ''): boolean {
  return endpoint.includes('/images/') || isImageChatLog(endpoint, model)
}

function pickEndpointTag(endpoint: string, model = ''): string {
  if (endpoint.includes('/images/edits')) return 'IMAGE-EDIT'
  if (endpoint.includes('/images/generations')) return 'IMAGE-GEN'
  if (isImageChatLog(endpoint, model)) return 'IMAGE-CHAT'
  if (endpoint.includes('/v1/chat')) return 'CHAT'
  return 'SYSTEM'
}

function terminalStage(status: string, error: string): string {
  if (status === 'success') return 'success'
  if (status === 'failed' || error) return 'failed'
  return ''
}

function terminalStatus(status: string, error: string): AdminLogGroup['status'] {
  if (status === 'success') return 'success'
  if (status === 'failed' || error) return 'error'
  return 'in_progress'
}

function detailValue(detail: Record<string, any>, key: string): string {
  const value = detail[key]
  if (value !== undefined && value !== null && value !== '') return formatDetailValue(value)
  const diagnosis = detail.diagnosis
  if (diagnosis && typeof diagnosis === 'object') return formatDetailValue(diagnosis[key])
  return ''
}

function detailRawValue(detail: Record<string, any>, key: string): unknown {
  if (Object.prototype.hasOwnProperty.call(detail, key)) return detail[key]
  const diagnosis = detail.diagnosis
  if (diagnosis && typeof diagnosis === 'object' && Object.prototype.hasOwnProperty.call(diagnosis, key)) {
    return diagnosis[key]
  }
  return undefined
}

function collectUrls(value: unknown): string[] {
  const urls: string[] = []
  if (Array.isArray(value)) {
    value.forEach((item) => urls.push(...collectUrls(item)))
  } else if (value && typeof value === 'object') {
    Object.entries(value as Record<string, unknown>).forEach(([key, item]) => {
      if (key === 'url' && typeof item === 'string') urls.push(item)
      else if (key === 'urls' && Array.isArray(item)) urls.push(...item.map((url) => cleanString(url)).filter(Boolean))
      else urls.push(...collectUrls(item))
    })
  } else if (typeof value === 'string') {
    for (const match of value.matchAll(LOG_IMAGE_URL_RE)) {
      if (match[1]) urls.push(match[1].replace(/[.,;]+$/, ''))
    }
  }
  return Array.from(new Set(urls))
}

function normalizePreviewUrl(url: string, apiBaseUrl = ''): string {
  const value = cleanString(url)
  if (!value || value.startsWith('file-service://')) return ''
  if (value.startsWith('/images/') || value.startsWith('/image-thumbnails/')) return value
  if (value.startsWith('images/') || value.startsWith('image-thumbnails/')) return `/${value}`
  if (/^https?:\/\//i.test(value)) {
    try {
      const parsed = new URL(value)
      if (parsed.pathname.startsWith('/images/') || parsed.pathname.startsWith('/image-thumbnails/')) {
        return `${parsed.pathname}${parsed.search}${parsed.hash}`
      }
    } catch {
      return value
    }
    return value
  }
  if (value.startsWith('/') && apiBaseUrl) return `${apiBaseUrl}${value}`
  return ''
}

function normalizePreviewUrls(urls: string[], apiBaseUrl = ''): string[] {
  return Array.from(new Set(urls.map((url) => normalizePreviewUrl(url, apiBaseUrl)).filter(Boolean)))
}

function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

export function summarizeLogText(value: string, max = 220): string {
  const clean = value.replace(/\s+/g, ' ').trim()
  if (clean.length <= max) return clean
  return `${clean.slice(0, max - 1)}…`
}

export function formatLogDuration(value: string): string {
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed < 0) return ''
  if (parsed < 1000) return `${Math.round(parsed)}ms`
  if (parsed < 10000) return `${(parsed / 1000).toFixed(2)}s`
  return `${(parsed / 1000).toFixed(1)}s`
}

function nonNegativeInt(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : 0
}

/** 由 result_images 生成列表分辨率文案；与后端 format_result_resolutions 保持一致 */
export function formatResultResolution(images: unknown): string {
  if (!Array.isArray(images)) return ''
  const labels = images
    .map((image) => {
      const record = image && typeof image === 'object' ? image as Record<string, unknown> : {}
      const width = nonNegativeInt(record.width)
      const height = nonNegativeInt(record.height)
      return width && height ? `${width}×${height}` : ''
    })
    .filter(Boolean)
  if (!labels.length) return ''
  const unique = Array.from(new Set(labels))
  if (unique.length === 1) return labels.length === 1 ? unique[0] : `${unique[0]} ×${labels.length}`
  return unique.join(' / ')
}

function normalizeImageAttempts(value: unknown): LogImageAttempt[] {
  if (!Array.isArray(value)) return []
  const rows: LogImageAttempt[] = []
  value.forEach((item) => {
    if (!item || typeof item !== 'object') return
    const record = item as Record<string, unknown>
    rows.push({
      slot: nonNegativeInt(record.slot) || 1,
      attempt: nonNegativeInt(record.attempt),
      status: cleanString(record.status),
      account: cleanString(record.account),
      durationMs: nonNegativeInt(record.duration_ms),
      egressLabel: cleanString(record.egress_label),
      egressKey: cleanString(record.egress_key),
      proxySource: cleanString(record.proxy_source),
      failureCode: cleanString(record.failure_code),
      errorType: cleanString(record.error_type),
      statusCode: nonNegativeInt(record.status_code),
      switchedAccount: record.switched_account === true,
      egressFallback: record.egress_fallback === true,
      retryKind: cleanString(record.retry_kind),
      deliveryFailed: cleanString(record.failure_scope).toLowerCase() === 'delivery'
        || cleanString(record.failure_code).toLowerCase() === 'image_download_failed',
    })
  })
  return rows
}

function boolDetailLabel(value: unknown): string {
  if (value === true || value === 'true') return 'true'
  if (value === false || value === 'false') return 'false'
  return cleanString(value)
}

function buildSystemLogDiagnosisChips(row: {
  status: string
  durationMs: string
  statusCode: string
  errorCode: string
  stage: string
  reason: string
  requestShape: string
  imageCount: number
  canResumePoll: boolean
  rawUpstreamMessage: string
  upstreamPreview: string
  upstreamMessageLen: string
  toolInvoked: string
}): LogDiagnosisChip[] {
  const chips: LogDiagnosisChip[] = []
  const duration = formatLogDuration(row.durationMs)
  if (duration) chips.push({ label: `耗时 ${duration}`, tone: 'neutral' })
  if (row.statusCode) chips.push({ label: `HTTP ${row.statusCode}`, tone: Number(row.statusCode) >= 400 ? 'danger' : 'neutral' })
  if (row.errorCode) chips.push({ label: `code=${row.errorCode}`, tone: 'warning' })
  if (row.stage) chips.push({ label: `stage=${row.stage}`, tone: 'info' })
  if (row.canResumePoll) chips.push({ label: '可继续轮询', tone: 'info' })
  if (row.rawUpstreamMessage || row.upstreamPreview || row.upstreamMessageLen) {
    chips.push({ label: row.upstreamMessageLen ? `上游文本 ${row.upstreamMessageLen}` : '上游文本', tone: 'warning' })
  }
  if (row.toolInvoked) chips.push({ label: `tool=${row.toolInvoked}`, tone: row.toolInvoked === 'false' ? 'warning' : 'neutral' })
  if (row.requestShape) chips.push({ label: `shape=${row.requestShape}`, tone: 'neutral' })
  if (row.imageCount) chips.push({ label: `图片 ${row.imageCount}`, tone: 'success' })
  if (chips.length === 0 && row.status.toLowerCase() === 'success') {
    chips.push({ label: '正常', tone: 'success' })
  }
  if (chips.length === 0 && row.reason) {
    chips.push({ label: summarizeLogText(row.reason, 28), tone: 'warning' })
  }
  return chips.slice(0, 5)
}

export function normalizeSystemLogRow(item: SystemLog, index: number, options: NormalizeSystemLogRowOptions = {}): SystemLogRow {
  const detail = item.detail || {}
  const monitor = detail.monitor && typeof detail.monitor === 'object' ? detail.monitor as Record<string, any> : {}
  const error = detailValue(detail, 'error')
  const requestText = detailValue(detail, 'request_text')
  const requestTextFull = detailValue(detail, 'request_text_full') || requestText
  const requestTextTruncated = detailRawValue(detail, 'request_text_truncated') === true
  const rawUpstreamMessage = detailValue(detail, 'raw_upstream_message')
  const upstreamPreview = detailValue(detail, 'upstream_message_preview')
  const duplicateDiagnosticValues = [error, rawUpstreamMessage, upstreamPreview].filter(Boolean)
  const rawUpstreamError = [
    detailValue(detail, 'upstream_error'),
    detailValue(detail, 'raw_error'),
  ].find((value) => value && !duplicateDiagnosticValues.includes(value)) || ''
  const reason = detailValue(detail, 'reason')
  const summary = cleanString(item.summary)
  const preview = summarizeLogText(requestText || rawUpstreamMessage || upstreamPreview || error || rawUpstreamError || reason || summary)
  const urls = collectUrls(detail)
  const imageUrls = normalizePreviewUrls(urls, options.apiBaseUrl)
  const status = detailValue(detail, 'status')
  const durationMs = detailValue(detail, 'duration_ms')
  const statusCode = detailValue(detail, 'status_code')
  const startedAt = detailValue(detail, 'started_at')
  const endedAt = detailValue(detail, 'ended_at')
  const requestShape = detailValue(detail, 'request_shape')
  const errorCode = detailValue(detail, 'error_code')
  const stage = detailValue(detail, 'stage')
  const upstreamErrorType = detailValue(detail, 'upstream_error_type')
  const upstreamRequestId = detailValue(detail, 'upstream_request_id')
  const canResumePoll = detail.can_resume_poll === true || detailValue(detail, 'can_resume_poll') === 'true'
  const toolInvoked = boolDetailLabel(detailRawValue(detail, 'tool_invoked'))
  const blocked = boolDetailLabel(detailRawValue(detail, 'blocked'))
  const upstreamMessageLen = detailValue(detail, 'upstream_message_len')
  const time = startedAt || cleanString(item.time) || endedAt
  const requestMeta = detail.request_meta && typeof detail.request_meta === 'object'
    ? detail.request_meta as Record<string, unknown>
    : {}
  const imageAttempts = normalizeImageAttempts(detail.image_attempts)
  const resultImages = Array.isArray(detail.result_images) ? detail.result_images : []

  return {
    id: cleanString(item.id) || `log-${index}`,
    raw: item,
    resolution: formatResultResolution(resultImages),
    resultImageCount: nonNegativeInt(detail.result_image_count) || resultImages.length,
    requestSize: formatDetailValue(requestMeta.size),
    requestQuality: formatDetailValue(requestMeta.quality),
    requestResponseFormat: formatDetailValue(requestMeta.response_format),
    requestCount: formatDetailValue(requestMeta.n),
    imageAttempts,
    imageAttemptCount: imageAttempts.length || nonNegativeInt(detail.image_attempt_count) || nonNegativeInt(detail.attempt_count),
    outcome: cleanString(detail.outcome).toLowerCase(),
    imageRequestedCount: nonNegativeInt(detail.image_requested_count),
    imageSucceededCount: nonNegativeInt(detail.image_succeeded_count),
    imageFailedCount: nonNegativeInt(detail.image_failed_count),
    switchCount: nonNegativeInt(detail.switch_count)
      || imageAttempts.filter((attempt) => attempt.switchedAccount).length,
    detailCompact: detail.detail_compact === true,
    time,
    type: cleanString(item.type),
    summary,
    endpoint: detailValue(detail, 'endpoint'),
    model: detailValue(detail, 'model'),
    status,
    keyId: detailValue(detail, 'key_id'),
    keyName: detailValue(detail, 'key_name'),
    role: detailValue(detail, 'role'),
    accountEmail: detailValue(detail, 'account_email'),
    accountId: detailValue(detail, 'provider_account_id'),
    conversationId: detailValue(detail, 'conversation_id'),
    proxySource: detailValue(detail, 'proxy_source') || formatDetailValue(monitor.proxy_source),
    proxyHash: detailValue(detail, 'proxy_hash') || formatDetailValue(monitor.proxy_hash),
    egressKey: detailValue(detail, 'egress_key') || formatDetailValue(monitor.egress_key),
    egressLabel: detailValue(detail, 'egress_label') || formatDetailValue(monitor.egress_label),
    proxyGroupId: detailValue(detail, 'proxy_group_id') || formatDetailValue(monitor.proxy_group_id),
    proxyNodeId: detailValue(detail, 'proxy_node_id') || formatDetailValue(monitor.proxy_node_id),
    proxyNodeName: detailValue(detail, 'proxy_node_name') || formatDetailValue(monitor.proxy_node_name),
    imageEgressLimit: detailValue(detail, 'image_egress_limit') || formatDetailValue(monitor.image_egress_limit),
    hasProxy: detailValue(detail, 'has_proxy') || boolDetailLabel(monitor.has_proxy),
    egressMode: detailValue(detail, 'egress_mode') || formatDetailValue(monitor.egress_mode),
    durationMs,
    statusCode,
    startedAt,
    endedAt,
    requestText,
    requestTextFull,
    requestTextTruncated,
    requestShape,
    error,
    errorCode,
    stage,
    reason,
    upstreamErrorType,
    upstreamRequestId,
    canResumePoll,
    toolInvoked,
    upstreamMessageLen,
    blocked,
    upstreamPreview,
    rawUpstreamMessage,
    rawUpstreamError,
    urls,
    imageUrls,
    diagnosisChips: buildSystemLogDiagnosisChips({
      status,
      durationMs,
      statusCode,
      errorCode,
      stage,
      reason,
      requestShape,
      imageCount: imageUrls.length,
      canResumePoll,
      rawUpstreamMessage,
      upstreamPreview,
      upstreamMessageLen,
      toolInvoked,
    }),
    preview,
    rawJson: prettyJson(detail),
  }
}

export function isSystemLogFailed(item: SystemLogRow): boolean {
  return item.status.toLowerCase() === 'failed' || Boolean(item.error || item.errorCode)
}

export function isSystemLogSuccess(item: SystemLogRow): boolean {
  return item.status.toLowerCase() === 'success'
}

const RATE_LIMITED_ERROR_CODES = new Set([
  'rate_limited',
  'rate_limit',
  'rate_limit_exceeded',
  'upstream_rate_limited',
  'insufficient_quota',
  'quota_exhausted',
  'image_quota_exhausted',
])

/**
 * 是否限流：优先使用后端写入的 outcome / error_code；
 * 只有两者都缺失的旧日志才按文本推断（兜底，可能误判）。
 */
export function isSystemLogLimited(item: SystemLogRow): boolean {
  if (item.outcome) return item.outcome === 'rate_limited'
  const code = (item.errorCode || '').toLowerCase()
  if (code) return RATE_LIMITED_ERROR_CODES.has(code) || code === '429'
  if (item.status.toLowerCase() === 'limited') return true
  const text = [item.status, item.reason, item.error].join(' ').toLowerCase()
  return text.includes('limit') || text.includes('quota') || text.includes('受限') || text.includes('限流')
}

function summarizeDetail(detail: Record<string, any>) {
  const parts = [
    detailValue(detail, 'stage') ? `stage=${detailValue(detail, 'stage')}` : '',
    detailValue(detail, 'error_code') ? `error_code=${detailValue(detail, 'error_code')}` : '',
    detailValue(detail, 'reason') ? `reason=${detailValue(detail, 'reason')}` : '',
    detailValue(detail, 'conversation_id') ? `conversation=${detailValue(detail, 'conversation_id')}` : '',
    detailValue(detail, 'duration_ms') ? `duration_ms=${detailValue(detail, 'duration_ms')}` : '',
    detailValue(detail, 'upstream_message_preview') ? `upstream="${detailValue(detail, 'upstream_message_preview')}"` : '',
    detailValue(detail, 'raw_upstream_message') ? `raw_upstream="${detailValue(detail, 'raw_upstream_message')}"` : '',
  ].filter(Boolean)
  return parts.join(' | ')
}

function buildMessage(item: SystemLog): string {
  const detail = item.detail || {}
  const endpoint = cleanString(detail.endpoint)
  const model = cleanString(detail.model)
  const status = cleanString(detail.status)
  const summary = cleanString(item.summary)
  const requestText = cleanString(detail.request_text)
  const error = cleanString(detail.error)
  const detailSummary = summarizeDetail(detail)
  const tags = [`[${pickEndpointTag(endpoint, model)}]`]
  const conversationId = cleanString(detail.conversation_id)
  if (conversationId) tags.push(`[req_${conversationId.replace(/[^a-z0-9]/gi, '').slice(0, 12)}]`)

  return [
    tags.join(''),
    summary || 'log',
    endpoint ? `endpoint=${endpoint}` : '',
    model ? `model=${model}` : '',
    status ? `status=${status}` : '',
    detailSummary,
    requestText ? `request: ${requestText}` : '',
    error ? `error: ${error}` : '',
  ].filter(Boolean).join(' ')
}

function mapLog(item: SystemLog, index: number): LogEntry {
  const detail = item.detail || {}
  const id = cleanString(item.id) || `log-${index}`
  const endpoint = cleanString(detail.endpoint)
  const model = cleanString(detail.model)
  const status = cleanString(detail.status)
  const error = cleanString(detail.error)
  const conversationId = cleanString(detail.conversation_id)
  const reqId = conversationId || id
  const message = buildMessage(item)
  return {
    time: cleanString(item.time),
    level: normalizeLevel(item),
    message,
    row_id: id,
    req_id: reqId,
    tags: [pickEndpointTag(endpoint, model), cleanString(item.type).toUpperCase()].filter(Boolean),
    account_id: cleanString(detail.account_email || detail.key_name || detail.key_id),
    text: message,
    layer: endpoint ? 'reverse' : 'system',
    lane: '',
    model,
    kind: detailValue(detail, 'error_code') || (error ? 'upstream_error' : ''),
    stage: terminalStage(status, error),
    served_label: '',
  }
}

function applyLocalFilters(logs: LogEntry[], params?: LogsListParams) {
  const level = cleanString(params?.level).toUpperCase()
  const search = cleanString(params?.search).toLowerCase()
  const limit = Math.min(Math.max(Number(params?.limit || 300), 10), 1000)
  const filtered = logs.filter((log) => {
    if (level && log.level !== level) return false
    if (!search) return true
    return [
      log.message,
      log.model,
      log.kind,
      log.account_id,
      log.req_id,
    ].some((value) => cleanString(value).toLowerCase().includes(search))
  })
  return filtered.slice(0, limit)
}

function buildStats(logs: LogEntry[]): AdminLogStats {
  const byLevel: Record<string, number> = {}
  logs.forEach((log) => {
    byLevel[log.level] = (byLevel[log.level] || 0) + 1
  })
  const recentErrors = logs.filter((log) => log.level === 'ERROR' || log.level === 'CRITICAL').slice(0, 10)
  return {
    memory: {
      total: logs.length,
      by_level: byLevel,
      capacity: 1000,
    },
    active: {
      source: 'file',
      total: logs.length,
    },
    errors: {
      count: recentErrors.length,
      recent: recentErrors,
    },
    chat_count: logs.filter((log) => log.tags?.includes('CHAT')).length,
  }
}

function buildGroups(logs: LogEntry[], rawItems: SystemLog[]): AdminLogGroup[] {
  const itemById = new Map(rawItems.map((item, index) => [cleanString(item.id) || `log-${index}`, item]))
  const grouped = new Map<string, { logs: LogEntry[]; raws: SystemLog[] }>()
  logs.forEach((log) => {
    const groupId = cleanString(log.req_id || log.row_id)
    const bucket = grouped.get(groupId) || { logs: [], raws: [] }
    bucket.logs.push(log)
    bucket.raws.push(itemById.get(cleanString(log.row_id)) || {})
    grouped.set(groupId, bucket)
  })

  return Array.from(grouped.entries()).map(([groupId, bucket]) => {
    const firstLog = bucket.logs[0]
    const lastLog = bucket.logs[bucket.logs.length - 1]
    const terminalRaw = [...bucket.raws].reverse().find((raw) => {
      const detail = raw.detail || {}
      return cleanString(detail.status) || cleanString(detail.error)
    }) || bucket.raws[bucket.raws.length - 1] || {}
    const firstRaw = bucket.raws[0] || {}
    const raw = terminalRaw
    const firstDetail = firstRaw.detail || {}
    const detail = raw.detail || {}
    const status = cleanString(detail.status)
    const error = cleanString(detail.error)
    return {
      id: groupId,
      row_ids: bucket.logs.map((log) => cleanString(log.row_id)).filter(Boolean),
      status: terminalStatus(status, error),
      account_id: cleanString(firstLog.account_id || lastLog.account_id),
      model: cleanString(firstLog.model || lastLog.model),
      lane: cleanString(firstLog.lane || lastLog.lane),
      terminal_kind: cleanString(lastLog.kind || firstLog.kind),
      started_at: detailValue(firstDetail, 'started_at') || cleanString(firstRaw.time),
      ended_at: detailValue(detail, 'ended_at') || cleanString(raw.time),
      user_preview: cleanString(firstDetail.request_text || detail.request_text).slice(0, 140),
      assistant_preview: cleanString(detail.error || detail.upstream_message_preview || detail.raw_upstream_message).slice(0, 140),
      count: bucket.logs.length,
    }
  })
}

function mapResponse(response: BackendLogsResponse, params?: LogsListParams): AdminLogsResponse {
  const rawItems = response.items || []
  const allLogs = rawItems.map(mapLog)
  const logs = applyLocalFilters(allLogs, params)
  return {
    total: logs.length,
    limit: Math.min(Math.max(Number(params?.limit || 300), 10), 1000),
    logs,
    groups: buildGroups(logs, rawItems),
    filters: {
      level: params?.level || null,
      search: params?.search || null,
      start_time: null,
      end_time: null,
    },
    stats: buildStats(logs),
  }
}

function normalizeSystemParams(params?: SystemLogsListParams) {
  const limit = Number(params?.limit || 500)
  const offset = Number(params?.offset || 0)
  return {
    type: cleanString(params?.type),
    start_date: cleanString(params?.start_date),
    end_date: cleanString(params?.end_date),
    status: cleanString(params?.status),
    endpoint: cleanString(params?.endpoint),
    model: cleanString(params?.model),
    account: cleanString(params?.account),
    conversation_id: cleanString(params?.conversation_id),
    search: cleanString(params?.search),
    limit: Number.isFinite(limit) ? Math.min(Math.max(Math.trunc(limit), 1), 500) : 500,
    offset: Number.isFinite(offset) ? Math.max(Math.trunc(offset), 0) : 0,
    // 列表只取精简字段，完整详情在打开抽屉时按 id 拉取（GET /api/logs/{id}）
    compact: params?.compact === false ? undefined : 1,
  }
}

function buildSystemStatsFallback(items: SystemLog[]) {
  const isSuccess = (item: SystemLog) => cleanString(detailValue(item.detail || {}, 'status')).toLowerCase() === 'success'
  const isFailed = (item: SystemLog) => {
    const detail = item.detail || {}
    return cleanString(detailValue(detail, 'status')).toLowerCase() === 'failed'
      || Boolean(detailValue(detail, 'error') || detailValue(detail, 'error_code'))
  }
  const isLimited = (item: SystemLog) => {
    const detail = item.detail || {}
    return [
      detailValue(detail, 'status'),
      detailValue(detail, 'error_code'),
      detailValue(detail, 'reason'),
      detailValue(detail, 'error'),
    ].join(' ').toLowerCase().includes('limit')
  }
  return {
    total: items.length,
    success: items.filter(isSuccess).length,
    failed: items.filter(isFailed).length,
    limited: items.filter(isLimited).length,
    image: items.filter((item) => {
      const detail = item.detail || {}
      return isImageEndpointLog(detailValue(detail, 'endpoint'), detailValue(detail, 'model'))
    }).length,
  }
}

function normalizeSystemResponse(response: BackendLogsResponse): SystemLogsResponse {
  const items = response.items || []
  const facets = response.facets || {}
  const stats = response.stats || {}
  const fallbackStats = buildSystemStatsFallback(items)
  const total = response.total === undefined ? items.length : Number(response.total || 0)
  return {
    items,
    total,
    limit: Number(response.limit || items.length || 0),
    offset: Number(response.offset || 0),
    has_more: response.has_more === true,
    facets_scope: cleanString(response.facets_scope),
    stats_scope: cleanString(response.stats_scope),
    total_scope: cleanString(response.total_scope),
    facets: {
      statuses: facets.statuses || {},
      endpoints: facets.endpoints || {},
      models: facets.models || {},
      accounts: facets.accounts || {},
    },
    stats: {
      total: Number(stats.total ?? total),
      success: Number(stats.success ?? fallbackStats.success),
      failed: Number(stats.failed ?? fallbackStats.failed),
      limited: Number(stats.limited ?? fallbackStats.limited),
      image: Number(stats.image ?? fallbackStats.image),
      partial_success: Number(stats.partial_success ?? 0),
      text_review: Number(stats.text_review ?? 0),
    },
  }
}

function normalizeRuntimeParams(params?: RuntimeLogsListParams) {
  const limit = Number(params?.limit || 300)
  return {
    level: cleanString(params?.level),
    search: cleanString(params?.search),
    source: cleanString(params?.source),
    limit: Number.isFinite(limit) ? Math.min(Math.max(Math.trunc(limit), 1), 2000) : 300,
  }
}

function normalizeRuntimeResponse(response: RuntimeLogsResponseRaw): RuntimeLogsResponse {
  const sources = response.sources || {}
  return {
    items: response.items || [],
    total: Number(response.total || response.items?.length || 0),
    limit: Number(response.limit || response.items?.length || 0),
    sources: {
      memory: sources.memory !== false,
      files: Array.isArray(sources.files) ? sources.files : [],
    },
  }
}

export const logsApi = {
  list: async (params?: LogsListParams) => {
    const limit = Math.min(Math.max(Number(params?.limit || 500), 10), 500)
    const response = await apiClient.get<never, BackendLogsResponse>('/api/logs', {
      params: { limit },
    })
    return mapResponse(response, params)
  },

  listSystem: async (params?: SystemLogsListParams) => {
    const response = await apiClient.get<never, BackendLogsResponse>('/api/logs', {
      params: normalizeSystemParams(params),
    })
    return normalizeSystemResponse(response)
  },

  listRuntime: async (params?: RuntimeLogsListParams) => {
    const response = await apiClient.get<never, RuntimeLogsResponseRaw>('/api/runtime-logs', {
      params: normalizeRuntimeParams(params),
    })
    return normalizeRuntimeResponse(response)
  },

  /** 单条完整日志（含请求全文、监控事件、生成尝试等列表里被精简掉的字段） */
  detail: async (id: string): Promise<SystemLog | null> => {
    const response = await apiClient.get<never, { item?: SystemLog }>(`/api/logs/${encodeURIComponent(id)}`)
    return response?.item ?? null
  },

  delete: async (ids: string[]) =>
    apiClient.post<{ ids: string[] }, { removed: number }>('/api/logs/delete', { ids }),
}
