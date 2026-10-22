// Monitor detail drawer adapter. Idea ported from yukkcat/chatgpt2api (AGPL-3.0-only).
// Realtime monitor records are projected into the call-log shape so the request detail
// drawer (fields, step timeline, attempts) is reused as-is.
import { normalizeSystemLogRow, type SystemLog, type SystemLogRow } from '@/api/logs'
import type { RealtimeMonitorRecord } from '@/api/monitor'

const PASS_THROUGH_KEYS = [
  'endpoint',
  'model',
  'account_email',
  'conversation_id',
  'error',
  'raw_error',
  'upstream_error',
  'proxy_source',
  'proxy_hash',
  'egress_key',
  'egress_label',
  'proxy_group_id',
  'proxy_node_id',
  'proxy_node_name',
  'image_egress_limit',
  'has_proxy',
  'egress_mode',
  'outcome',
  'error_code',
  'public_error',
  'status_code',
  'attempt_count',
  'switch_count',
  'image_requested_count',
  'image_succeeded_count',
  'image_failed_count',
  'image_result_status',
] as const

export function monitorRecordToSystemLog(
  record: RealtimeMonitorRecord & { events?: unknown; cancelled?: boolean },
  options: { compact?: boolean } = {},
): SystemLog {
  const detail: Record<string, unknown> = {
    call_id: record.call_id,
    key_name: record.key_name,
    role: record.role,
    status: record.status || 'running',
    stage: record.stage_label || record.stage,
    reason: record.local_reason,
    started_at: record.started_at,
    ended_at: record.ended_at || record.updated_at,
    duration_ms: record.duration_ms ?? record.elapsed_ms,
    perf: record.perf,
    metrics: record.metrics,
    monitor: record,
  }
  PASS_THROUGH_KEYS.forEach((key) => {
    const value = (record as unknown as Record<string, unknown>)[key]
    if (value !== undefined && value !== null && value !== '') detail[key] = value
  })
  if (record.cancelled) detail.reason = detail.reason || '管理员已取消'
  if (options.compact) detail.detail_compact = true
  return {
    id: record.call_id,
    type: 'call',
    time: record.started_at || record.updated_at,
    summary: record.summary || `${record.model || ''} ${record.stage_label || ''}`.trim(),
    detail,
  }
}

export function monitorRecordToRow(
  record: RealtimeMonitorRecord & { events?: unknown; cancelled?: boolean },
  options: { compact?: boolean; apiBaseUrl?: string } = {},
): SystemLogRow {
  return normalizeSystemLogRow(monitorRecordToSystemLog(record, options), 0, { apiBaseUrl: options.apiBaseUrl })
}
