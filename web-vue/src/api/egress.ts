import apiClient from './client'
import type { DisabledProxyNode } from '@/types/api'

export type EgressVerdict = 'ok' | 'warn' | 'blocked' | 'error'

export interface EgressCheckResult {
  proxy: string
  verdict: EgressVerdict
  message: string
  ip?: string
  country_code?: string
  country?: string
  city?: string
  isp?: string
  org?: string
  hosting?: boolean
  flagged_proxy?: boolean
  latency_ms?: number
  checked_at?: string
}

export interface EgressPreflightReport {
  ok: boolean
  message: string
  results: EgressCheckResult[]
}

export interface EgressQuotaUsage {
  proxy: string
  count_today: number
  next_available_in: number
}

export interface EgressHealth {
  disabled_nodes: DisabledProxyNode[]
  quota_usage: EgressQuotaUsage[]
}

export interface RegisterEgressConfig {
  check_enabled: boolean
  block_unsupported: boolean
  daily_limit: number
  min_gap_secs: number
}

export interface EgressSlotSettings {
  enabled: boolean
  controller_url: string
  proxy_template: string
  secret_set: boolean
  slot_count: number
  max_slots: number
  switch_hold_hours: number
  slot_capacity: number
}

export interface EgressSlotNode {
  name: string
  type: string
  source_id: string
  check?: EgressCheckResult | null
  recommendation: 'register' | 'daily' | 'unchecked' | 'unusable'
}

export interface EgressSlot {
  slot: number
  port: number
  proxy_url: string
  node: string
  node_info?: EgressSlotNode | null
  missing: boolean
  active: string
  error: string
  locale?: EgressSlotLocale | null
  suggested_locale?: EgressSuggestedLocale | null
}

export interface EgressSlotLocale {
  timezone: string
  language: string
}

export interface EgressSuggestedLocale extends EgressSlotLocale {
  country: string
  timezone_options: string[]
}

export interface EgressSource {
  id: string
  kind: 'subscription' | 'manual'
  name: string
  url: string
  line_count: number
  node_count: number
  created_at: string
}

export interface EgressSlotsSnapshot {
  status: { ok: boolean; error: string; version?: string; applied_at?: string; checked_at?: string }
  settings: EgressSlotSettings
  sources: EgressSource[]
  nodes: EgressSlotNode[]
  slots: EgressSlot[]
  job: { running: boolean; total: number; done: number }
  register_group_id: string
}

export interface EgressSlotSettingsPayload {
  enabled?: boolean
  controller_url?: string
  proxy_template?: string
  secret?: string
  switch_hold_hours?: number
  slot_capacity?: number
}

export interface FreeProxyStatus {
  enabled: boolean
  protocol: 'http' | 'socks5'
  limit: number
  test_limit: number
  max_nodes: number
  interval_minutes: number
  allow_datacenter: boolean
  last_run_at: string
  last_error: string
  last_fetched: number
  last_ok: number
  last_message: string
  running: boolean
  next_run_at: string
  group_id: string
}

export type FreeProxyPayload = Partial<Pick<
  FreeProxyStatus,
  'enabled' | 'protocol' | 'limit' | 'test_limit' | 'max_nodes' | 'interval_minutes' | 'allow_datacenter'
>>

export const DEFAULT_REGISTER_EGRESS: RegisterEgressConfig = {
  check_enabled: true,
  block_unsupported: true,
  daily_limit: 0,
  min_gap_secs: 0,
}

export interface EgressAccountsOverview {
  capacity: number
  slots: Array<{ slot: number; accounts: number; down: boolean; capacity: number }>
  direct_accounts: number
  default_accounts: number
  pending_switches: Array<{ email: string; token: string; from: string; to: string; due_at: string }>
}

export const egressApi = {
  accountsOverview: () =>
    apiClient.get<never, EgressAccountsOverview>('/api/egress/accounts'),

  check: (proxy = '') =>
    apiClient.post<{ proxy: string }, EgressPreflightReport>('/api/egress/check', { proxy, use_cache: false }),

  health: () =>
    apiClient.get<never, EgressHealth>('/api/egress/health'),

  slots: () =>
    apiClient.get<never, EgressSlotsSnapshot>('/api/egress/slots'),

  saveSlotSettings: (payload: EgressSlotSettingsPayload) =>
    apiClient.post<EgressSlotSettingsPayload, EgressSlotsSnapshot>('/api/egress/slots/settings', payload),

  setSlotCount: (count: number) =>
    apiClient.post<{ count: number }, EgressSlotsSnapshot>('/api/egress/slots/count', { count }),

  addSource: (payload: { kind: 'subscription' | 'manual'; name: string; url?: string; text?: string }) =>
    apiClient.post<typeof payload, EgressSlotsSnapshot>('/api/egress/slots/sources', payload),

  removeSource: (id: string) =>
    apiClient.delete<never, EgressSlotsSnapshot>(`/api/egress/slots/sources/${encodeURIComponent(id)}`),

  refreshSource: (id: string) =>
    apiClient.post<never, EgressSlotsSnapshot>(`/api/egress/slots/sources/${encodeURIComponent(id)}/refresh`),

  assignSlot: (slot: number, node: string | null) =>
    apiClient.post<{ node: string | null }, EgressSlotsSnapshot>(`/api/egress/slots/${slot}`, { node }),

  setSlotLocale: (slot: number, locale: EgressSlotLocale) =>
    apiClient.post<EgressSlotLocale, EgressSlotsSnapshot>(`/api/egress/slots/${slot}/locale`, locale),

  startChecks: (names: string[] = []) =>
    apiClient.post<{ names: string[] }, EgressSlotsSnapshot['job']>('/api/egress/slots-checks', { names }),

  useForRegister: () =>
    apiClient.post<never, EgressSlotsSnapshot & { slot_count: number }>('/api/egress/slots-use-for-register'),

  freeProxy: () =>
    apiClient.get<never, FreeProxyStatus>('/api/egress/free-proxy'),

  saveFreeProxy: (payload: FreeProxyPayload) =>
    apiClient.post<FreeProxyPayload, FreeProxyStatus>('/api/egress/free-proxy', payload),

  runFreeProxy: () =>
    apiClient.post<never, { free_proxy: FreeProxyStatus; result: { message: string } }>('/api/egress/free-proxy/run'),
}
