import apiClient from './client'
import type {
  DashboardRuntime,
  SystemUpdateTask,
  VersionCheckResponse,
  VersionInfoResponse,
} from '@/types/api'
import { projectReleasePageUrl, projectRepository } from '@/config/project'

// 后端 /api/system/update-status：版本来源是本项目自己的 GitHub Releases。
type UpdateStatusResponse = {
  current_tag?: string
  latest_tag?: string
  update_available?: boolean
  release_url?: string
  status_label?: string
  status_message?: string
  tone?: string
  changelog?: string
  can_update?: boolean
}

function toVersionInfo(payload: { version?: string }): VersionInfoResponse {
  const version = String(payload.version || '').trim()
  return {
    version,
    tag: version ? (version.startsWith('v') ? version : `v${version}`) : '',
    commit: '',
  }
}

export const versionApi = {
  async current() {
    const payload = await apiClient.get<never, { version: string }>('/version')
    return toVersionInfo(payload)
  },

  async check(force = false): Promise<VersionCheckResponse> {
    const payload = await apiClient.get<never, UpdateStatusResponse>('/api/system/update-status', {
      params: force ? { force: true } : undefined,
    })
    if (!payload || typeof payload !== 'object' || !('status_label' in payload)) {
      throw new Error('版本接口返回格式异常')
    }
    const current = toVersionInfo({ version: String(payload.current_tag || '').replace(/^v/, '') })
    const latestTag = String(payload.latest_tag || '').trim()
    const latestVersion = latestTag.replace(/^v/, '')
    const checkFailed = payload.status_label === '检查失败'
    return {
      ...current,
      repository: projectRepository,
      latest_tag: latestTag,
      latest_version: latestVersion,
      release_url: payload.release_url || projectReleasePageUrl,
      changelog: payload.changelog,
      is_latest: Boolean(latestVersion) && !payload.update_available,
      update_available: Boolean(payload.update_available),
      can_update: Boolean(payload.can_update),
      status_message: payload.status_message,
      check_error: checkFailed ? (payload.status_message || 'GitHub 版本检查失败') : undefined,
    }
  },

  updateTask() {
    return apiClient.get<never, SystemUpdateTask>('/api/system/update-task')
  },

  /** 开始一键更新。必须在用户确认后调用，服务端要求显式 confirm。 */
  startUpdate() {
    return apiClient.post<{ confirm: boolean }, SystemUpdateTask>('/api/system/update', { confirm: true })
  },

  runtime() {
    return apiClient.get<never, DashboardRuntime>('/api/system/runtime')
  },
}
