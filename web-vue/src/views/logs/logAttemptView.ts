// Attempt timeline model. UI idea ported from yukkcat/chatgpt2api (AGPL-3.0-only).
import type { LogImageAttempt } from '@/api/logs'
import { formatRequestDuration } from '@/lib/requestDuration'

export type AttemptTone = 'success' | 'danger' | 'warning' | 'muted'

export type AttemptRow = LogImageAttempt & {
  key: string
  tone: AttemptTone
  statusText: string
  durationText: string
  egressText: string
  failureText: string
  switchText: string
}

export type AttemptGroup = {
  slot: number
  label: string
  statusText: string
  tone: AttemptTone
  attemptText: string
  switchText: string
  attempts: AttemptRow[]
}

const failureLabels: Record<string, string> = {
  upstream_error: '上游请求失败',
  internal_error: '内部处理异常',
  upstream_unavailable: '上游服务暂不可用',
  upstream_error_reply: '上游返回错误回复',
  upstream_text_reply: '上游返回文本而非图片',
  upstream_rate_limited: '上游限流 (429)',
  image_quota_exhausted: '图片额度耗尽',
  insufficient_quota: '图片额度不足',
  file_upload_throttled: '参考图上传受限',
  image_poll_timeout: '轮询超时',
  image_stream_timeout: '流式超时',
  image_stream_interrupted: '流式中断',
  image_tool_error: '图片工具异常',
  content_policy_violation: '内容策略拒绝',
  invalid_image_input: '图片输入无效',
  upstream_connection_failed: '上游连接失败',
  upstream_connection_timeout: '上游连接超时',
  auth_invalid: '账号登录态失效',
  no_image_generated: '上游未生成图片',
  unsupported_model: '模型不支持生图',
  image_download_failed: '图片下载失败',
  task_interrupted: '图片任务被中断',
  no_available_account: '暂无可用账号',
  request_cancelled: '请求已取消',
}

function attemptTone(attempt: LogImageAttempt): AttemptTone {
  if (attempt.deliveryFailed) return 'warning'
  if (attempt.status === 'success') return 'success'
  if (attempt.status === 'message') return 'warning'
  if (attempt.status === 'failed') return 'danger'
  return 'muted'
}

function attemptStatusText(attempt: LogImageAttempt): string {
  if (attempt.deliveryFailed) return '生成成功'
  if (attempt.status === 'success') return '成功'
  if (attempt.status === 'message') return '文本回复'
  if (attempt.status === 'failed') return '失败'
  return attempt.status || '未知'
}

function egressText(attempt: LogImageAttempt): string {
  const label = attempt.egressLabel && attempt.egressLabel !== 'direct' ? attempt.egressLabel : ''
  const base = label || attempt.egressKey || ''
  const text = base.length > 40 ? `${base.slice(0, 39)}…` : base
  return attempt.egressFallback ? `${text || '出口'} (回退)` : text
}

function switchText(attempt: LogImageAttempt): string {
  if (attempt.retryKind === 'egress_fallback') return '切换出口重试'
  return attempt.switchedAccount ? '已换号重试' : ''
}

export function buildAttemptGroups(attempts: readonly LogImageAttempt[]): AttemptGroup[] {
  const grouped = new Map<number, LogImageAttempt[]>()
  attempts.forEach((attempt) => {
    const list = grouped.get(attempt.slot) || []
    list.push(attempt)
    grouped.set(attempt.slot, list)
  })
  return Array.from(grouped.keys())
    .sort((a, b) => a - b)
    .map((slot) => {
      const list = (grouped.get(slot) || []).slice().sort((a, b) => a.attempt - b.attempt)
      const rows: AttemptRow[] = list.map((attempt, index) => ({
        ...attempt,
        key: `${slot}-${attempt.attempt}-${index}`,
        tone: attemptTone(attempt),
        statusText: attemptStatusText(attempt),
        durationText: formatRequestDuration(attempt.durationMs),
        egressText: egressText(attempt),
        failureText: attempt.deliveryFailed
          ? (failureLabels[attempt.failureCode] || '结果交付失败')
          : attempt.status === 'failed' ? (failureLabels[attempt.failureCode] || attempt.failureCode || '生成失败') : '',
        switchText: switchText(attempt),
      }))
      const succeeded = rows.some((row) => row.status === 'success' && !row.deliveryFailed)
      const deliveryFailed = !succeeded && rows.some((row) => row.deliveryFailed)
      const message = rows.some((row) => row.status === 'message')
      const switches = rows.filter((row) => row.switchedAccount).length
      return {
        slot,
        label: `图片 ${slot}`,
        statusText: succeeded ? '成功' : deliveryFailed ? '生成成功' : message ? '文本回复' : '失败',
        tone: succeeded ? 'success' : deliveryFailed || message ? 'warning' : 'danger',
        attemptText: `${rows.length} 次尝试`,
        switchText: switches ? `换号 ${switches} 次` : '',
        attempts: rows,
      }
    })
}

/** 重试耗时拆分：同一张图有多次尝试时返回 "(2s + 5s)"，多张图时带 "图 N：" 前缀。 */
export function attemptDurationBreakdown(attempts: readonly LogImageAttempt[]): string {
  const bySlot = new Map<number, number[]>()
  attempts
    .slice()
    .sort((a, b) => a.slot - b.slot || a.attempt - b.attempt)
    .forEach((attempt) => {
      if (attempt.durationMs <= 0) return
      const list = bySlot.get(attempt.slot) || []
      list.push(attempt.durationMs)
      bySlot.set(attempt.slot, list)
    })
  const retryGroups = Array.from(bySlot.entries()).filter(([, durations]) => durations.length > 1)
  if (!retryGroups.length) return ''
  const multipleSlots = new Set(attempts.map((attempt) => attempt.slot)).size > 1
  const expressions = retryGroups.map(([slot, durations]) => {
    const expression = durations.map((duration) => formatRequestDuration(duration)).join(' + ')
    return multipleSlots ? `图 ${slot}：${expression}` : expression
  })
  return `(${expressions.join('；')})`
}
