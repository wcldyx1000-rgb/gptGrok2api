// API 类型定义

export type ProxyRuntimeEgressMode = 'direct' | 'single_proxy'
export type ProxyRuntimeClearanceMode = 'none' | 'manual' | 'flaresolverr'

export interface ProxyRuntimeClearance {
  enabled: boolean
  mode: ProxyRuntimeClearanceMode
  cf_cookies: string
  cf_clearance: string
  has_cf_cookies?: boolean
  has_cf_clearance?: boolean
  user_agent: string
  browser: string
  flaresolverr_url: string
  timeout_sec: number
  refresh_interval: number
  warm_up_on_start: boolean
  max_sessions: number
  session_idle_sec: number
  early_refresh_ratio: number
}

export interface ProxyRuntimeSettings {
  enabled: boolean
  egress_mode: ProxyRuntimeEgressMode
  proxy_url: string
  resource_proxy_url: string
  skip_ssl_verify: boolean
  reset_session_status_codes: number[]
  timezone: string
  language: string
  node_fail_threshold: number
  node_cooldown_sec: number
  clearance: ProxyRuntimeClearance
}

export interface DisabledProxyNode {
  proxy: string
  disabled_until: number
  remaining_sec: number
  last_error: string
}

export interface ProxyRuntimeStatus {
  enabled: boolean
  egress_mode: string
  proxy_source?: string
  has_proxy: boolean
  skip_ssl_verify?: boolean
  clearance_enabled: boolean
  clearance_mode: string
  has_clearance_bundle: boolean
  cached_clearance_hosts: string[]
  flaresolverr_sessions?: number
  disabled_proxy_nodes?: DisabledProxyNode[]
}

export interface ClearanceTestResult {
  ok: boolean
  status: string
  latency_ms: number
  has_cookies: boolean
  user_agent: string
  error?: string | null
  runtime?: ProxyRuntimeStatus
}

export interface ImageErrorMessages {
  fallback: string
  quota: string
  no_account: string
  local_busy: string
  unsupported_model: string
  poll_timeout: string
  stream_interrupted: string
  connection_failed: string
  connection_timeout: string
  token_invalid: string
  text_reply: string
}

export interface Settings {
  proxy?: string
  fallback_proxy?: string
  proxy_runtime: ProxyRuntimeSettings
  base_url?: string
  refresh_account_interval_minute?: number
  image_retention_hours?: number
  log_retention_hours?: number
  image_min_free_mb?: number
  image_poll_timeout_secs?: number
  image_stream_timeout_secs?: number
  console_request_timeout_secs?: number
  image_queue_limit?: number
  account_processing_concurrency?: number
  image_poll_interval_secs?: number
  image_poll_initial_wait_secs?: number
  image_account_concurrency?: number
  image_account_cooldown_secs?: number
  image_preflight_fresh_secs?: number
  /** 旧字段，仅兼容读取；以 image_max_account_attempts 为准 */
  image_account_retry_count?: number
  image_account_retry_enabled?: boolean
  image_max_account_attempts?: number
  image_stream_post_tool_wait_secs?: number
  image_poll_adaptive?: boolean
  image_warm_enabled?: boolean
  chat_sentinel_vm_enabled?: boolean
  chat_temporary_mode?: boolean
  image_stream_direct_resolve?: boolean
  image_drop_extra_variants?: boolean
  image_upscale_enabled?: boolean
  image_upscale_engine?: 'pillow_lanczos' | 'sharp_lanczos3'
  account_auto_pause_strikes?: number
  account_auto_pause_hours?: number
  new_account_cooldown_hours?: number
  new_account_ramp_days?: number
  new_account_ramp_daily_images?: number
  text_account_concurrency?: number
  account_failure_cooldown_secs?: number
  account_relogin_enabled?: boolean
  account_relogin_interval_secs?: number
  account_relogin_concurrency?: number
  image_parallel_generation?: boolean
  image_remove_conversation_after_result?: boolean
  image_error_friendly_enabled?: boolean
  image_error_messages: ImageErrorMessages
  image_settle_enabled?: boolean
  image_check_before_hit_enabled?: boolean
  image_settle_secs?: number
  image_timeout_retry_secs?: number
  auto_remove_invalid_accounts?: boolean
  auto_remove_rate_limited_accounts?: boolean
  log_levels: string[]
  global_system_prompt?: string
  sensitive_words?: string[]
  ai_review: {
    enabled: boolean
    base_url: string
    /** 读取时恒为空串；留空保存表示沿用已存密钥 */
    api_key: string
    has_api_key?: boolean
    clear_api_key?: boolean
    model: string
    prompt: string
  }
  basic: {
    api_key?: string
    base_url?: string
    proxy?: string
    image_expire_hours?: number
  }
  image_generation: {
    enabled: boolean
    supported_models: string[]
    model_options?: string[]
    block_rich_output_on_base_chat_models?: boolean
    output_format?: 'base64' | 'url'
    nanobanana_lane?: 'fast' | 'thinking' | 'pro'
    nanobanana_lane_order?: Array<'fast' | 'thinking' | 'pro'>
  }
  model_catalog?: {
    models?: Array<{
      name: string
      display_name?: string
      lane?: string
      kind?: string
      tool_family?: string
      capabilities?: string[]
      endpoints?: string[]
      aliases?: string[]
      enabled?: boolean
      image_default?: boolean
      lane_order?: string[]
    }>
    chat_models?: string[]
    image_api_models?: string[]
    base_chat_models?: string[]
    specialized_chat_models?: string[]
    image_capable_chat_models?: string[]
  }
  quota_limits: {
    enabled: boolean
    fast_daily_limit: number
    thinking_daily_limit: number
    pro_daily_limit: number
    image_daily_limit: number
    music_daily_limit: number
    video_daily_limit: number
  }
  runtime_capacity: {
    uvicorn_workers: number
    text_concurrency_limit: number
    image_concurrency_limit: number
    request_queue_timeout_seconds: number
  }
  image_storage?: {
    enabled: boolean
    mode: 'local' | 'webdav' | 'both'
    webdav_url: string
    webdav_username: string
    /** 读取时恒为空串；留空保存表示沿用已存密码 */
    webdav_password: string
    has_webdav_password?: boolean
    clear_webdav_password?: boolean
    webdav_root_path: string
    public_base_url: string
  }
  genbox_push?: {
    enabled: boolean
    base_url: string
    source_id: string
    /** 读取时恒为空串；仅在用户输入新密钥时才会发送 */
    push_key: string
    has_push_key?: boolean
    clear_push_key?: boolean
    timeout_secs: number
    auto_push_after_studio: boolean
  }
  backup?: {
    enabled: boolean
    provider: string
    account_id: string
    access_key_id: string
    /** 读取时恒为空串；留空保存表示沿用已存密钥 */
    secret_access_key: string
    has_secret_access_key?: boolean
    clear_secret_access_key?: boolean
    bucket: string
    prefix: string
    interval_minutes: number
    rotation_keep: number
    encrypt: boolean
    passphrase: string
    has_passphrase?: boolean
    clear_passphrase?: boolean
    include: Record<string, boolean>
  }
  chat_completion_cache?: {
    enabled: boolean
    ttl_seconds: number
    max_entries: number
    dedupe_inflight: boolean
    stream_cache: boolean
    normalize_messages: boolean
    drop_adjacent_duplicates: boolean
    drop_assistant_history: boolean
  }
  third_party_apps: {
    infinite_canvas: {
      enabled: boolean
      url: string
    }
  }
  proxy_profiles?: Array<{
    id: string
    name: string
    proxy: string
    no_proxy?: string
    enabled: boolean
    notes?: string
  }>
}

export interface SettingsUpdateResponse {
  status: string
  message?: string
  restart_required?: boolean
  config?: Settings
  runtime_capacity?: {
    uvicorn_workers: number
    text_concurrency_limit: number
    image_concurrency_limit: number
    request_queue_timeout_seconds: number
  }
}

export interface LogEntry {
  time: string
  level: 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL' | 'DEBUG'
  message: string
  row_id?: string
  req_id?: string
  tags?: string[]
  account_id?: string
  text?: string
  layer?: 'system' | 'chat' | 'reverse' | 'other'
  lane?: string
  model?: string
  kind?: string
  stage?: string
  served_label?: string
}

export interface AdminLogGroup {
  id: string
  row_ids: string[]
  status: 'success' | 'error' | 'timeout' | 'in_progress'
  account_id: string
  model: string
  lane: string
  terminal_kind: string
  started_at: string
  ended_at: string
  user_preview: string
  assistant_preview: string
  count: number
}

export interface LogsResponse {
  total: number
  limit: number
  logs: LogEntry[]
}

export interface AdminLogStats {
  memory: {
    total: number
    by_level: Record<string, number>
    capacity: number
  }
  active?: {
    source: 'file' | 'memory'
    total: number
  }
  errors: {
    count: number
    recent: LogEntry[]
  }
  chat_count: number
}

export interface AdminLogsResponse extends LogsResponse {
  filters?: {
    level?: string | null
    search?: string | null
    start_time?: string | null
    end_time?: string | null
  }
  groups?: AdminLogGroup[]
  stats: AdminLogStats
}

export interface AdminStatsTrend {
  labels: string[]
  total_requests: number[]
  success_requests?: number[]
  failed_requests: number[]
  rate_limited_requests: number[]
  model_requests?: Record<string, number[]>
  model_ttfb_times?: Record<string, number[]>
  model_total_times?: Record<string, number[]>
  /** 仅成功请求口径：模型请求分布 */
  model_success_requests?: Record<string, number[]>
  /** 仅成功请求口径：各模型平均耗时（毫秒，无数据为 null） */
  model_avg_success_duration_ms?: Record<string, Array<number | null>>
  /** 仅成功请求口径：每个时间桶的平均耗时（毫秒，无数据为 null） */
  success_duration_ms?: Array<number | null>
}

export interface DashboardSwitchingStats {
  requests?: number
  count?: number
  recovered?: number
  recovery_rate?: number | null
}

export interface AdminStats {
  total_accounts: number
  active_accounts: number
  abnormal_accounts: number
  disabled_accounts: number
  failed_accounts: number
  rate_limited_accounts: number
  idle_accounts: number
  total_quota: number
  unlimited_quota_count?: number
  unknown_quota_count?: number
  account_providers?: Record<string, DashboardProviderAccountStats>
  success_count?: number
  failed_count?: number
  avg_success_duration_ms?: number | null
  /** 调用概览口径（后端 dashboard_metrics）：文本回复不计成败，部分成功算成功。 */
  total_calls?: number
  text_review_count?: number
  success_rate?: number | null
  switching?: DashboardSwitchingStats
  recent_failures?: Array<{
    id?: string
    time?: string
    summary?: string
    endpoint?: string
    error_code?: string
    stage?: string
    reason?: string
    conversation_id?: string
  }>
  trend: AdminStatsTrend
}

export interface LoginRequest {
  password: string
}

export interface LoginResponse {
  ok: boolean
  authenticated: boolean
  version: string
  role?: string
  subject_id?: string
  name?: string
}

export interface AuthStatusResponse {
  ok: boolean
  authenticated: boolean
  version: string
  role?: string
  subject_id?: string
  name?: string
}

export interface VersionInfoResponse {
  version: string
  tag: string
  commit: string
}

export interface VersionCheckResponse extends VersionInfoResponse {
  repository: string
  latest_tag: string
  latest_version: string
  release_url: string
  release_name?: string
  release_notes?: string
  changelog?: string
  published_at?: string
  is_latest: boolean
  update_available: boolean
  check_error?: string
  /** 当前部署是否支持管理台一键更新（受管运行目录） */
  can_update?: boolean
  /** 后端给出的更新状态说明（含不支持一键更新时的升级指引） */
  status_message?: string
}

export interface DashboardRuntime {
  runtime_mode: 'docker' | 'native'
  instance_name: string
  distribution: string
  kernel_version: string
  architecture: string
  python_version: string
  cpu_capacity: number
  service_started_at: string
  service_uptime_seconds: number
  process_cpu_percent: number | null
  process_memory_bytes: number | null
  process_memory_percent: number | null
  memory_scope: 'container' | 'system' | 'visible'
  memory_percent: number | null
  storage_percent: number | null
  network_rx_bytes_per_sec: number | null
  network_tx_bytes_per_sec: number | null
}

export type SystemUpdateState = 'idle' | 'queued' | 'running' | 'succeeded' | 'failed'

export interface SystemUpdateEvent {
  id: string
  timestamp: string
  label: string
  message: string
  tone: 'info' | 'success' | 'warning' | 'danger'
}

export interface SystemUpdateTask {
  task_id: string
  state: SystemUpdateState
  stage: string
  current: number
  total: number
  status_label: string
  message: string
  tone: 'info' | 'success' | 'warning' | 'danger'
  busy: boolean
  current_tag: string
  latest_tag: string
  error: string
  updated_at: string
  events: SystemUpdateEvent[]
}

export interface DashboardProviderAccountStats {
  total: number
  cumulative_total?: number
  active: number
  limited: number
  abnormal: number
  disabled: number
  total_quota: number
  unlimited_quota_count?: number
  unknown_quota_count?: number
  total_success?: number
  total_fail?: number
  by_type?: Record<string, number>
  source_available?: boolean
  healthy: boolean
}

export interface DashboardAccountStats extends DashboardProviderAccountStats {
  providers?: Record<string, DashboardProviderAccountStats>
}

export interface DashboardLogSummary {
  total: number
  success: number
  failed: number
  text_review?: number
  success_rate?: number | null
  avg_success_duration_ms?: number | null
  switching?: DashboardSwitchingStats
  by_endpoint: Record<string, number>
  by_model?: Record<string, number>
  by_status: Record<string, number>
  by_error_code: Record<string, number>
  trend?: AdminStatsTrend
  recent_failures: Array<{
    id?: string
    time?: string
    summary?: string
    endpoint?: string
    error_code?: string
    stage?: string
    reason?: string
    conversation_id?: string
  }>
}

export interface DashboardResponse {
  status: 'ok' | 'degraded'
  healthy: boolean
  version: string
  accounts: DashboardAccountStats
  storage: {
    backend: Record<string, unknown>
    health: Record<string, unknown>
    images: Record<string, unknown>
  }
  logs: DashboardLogSummary
}
