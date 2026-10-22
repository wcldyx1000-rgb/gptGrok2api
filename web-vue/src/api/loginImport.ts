import apiClient from './client'

export type LoginImportJobStatus = 'running' | 'completed' | 'stopped'

export interface LoginImportResult {
  email: string
  ok: boolean
  /** 入池验证结果；未开启验证时登录成功即为 true，登录失败为 null。 */
  usable: boolean | null
  error: string
}

export interface LoginImportLog {
  time: string
  text: string
  level: string
}

export interface LoginImportJob {
  job_id: string
  status: LoginImportJobStatus
  terminal: boolean
  verify: boolean
  total: number
  done: number
  ok: number
  pool_ok: number
  fail: number
  current: string
  skipped_lines: string[]
  results: LoginImportResult[]
  logs: LoginImportLog[]
  result_message?: string
  created_at: string
  updated_at: string
}

export interface ReactivateResult {
  ok: number
  fail: number
  results: Array<{ email: string; ok: boolean; error: string }>
}

export const loginImportApi = {
  start: (text: string, verify: boolean) =>
    apiClient.post<{ text: string; verify: boolean }, { job: LoginImportJob }>('/api/accounts/login-import', { text, verify }),

  get: (jobId = '') =>
    apiClient.get<never, { job: LoginImportJob | null }>('/api/accounts/login-import', { params: { job_id: jobId } }),

  stop: (jobId: string) =>
    apiClient.post<{ job_id: string }, { job: LoginImportJob | null }>('/api/accounts/login-import/stop', { job_id: jobId }),

  /** 号池已有账号用 Outlook 邮箱无密码登录换新 token。 */
  reactivate: (emails: string[]) =>
    apiClient.post<{ emails: string[] }, ReactivateResult>('/api/accounts/reactivate', { emails }),
}
