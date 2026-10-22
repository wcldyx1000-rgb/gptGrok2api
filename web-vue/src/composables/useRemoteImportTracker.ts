// Remote CPA / Sub2API import tracking. Idea ported from yukkcat/chatgpt2api (AGPL-3.0-only).
//
// 远程导入全局只允许运行一个任务（后端保证）。这里用模块级单例跟踪它：
// 进度抽屉在任何页面实例里都显示同一个任务，刷新页面或离开再回来后可通过
// resume() 向后端查询进行中的任务并继续展示进度。
import { computed, reactive } from 'vue'

import { accountImportsApi, type ActiveRemoteImport, type CPAImportJob } from '@/api/accountImports'

export type RemoteImportSource = 'cpa' | 'sub2api'

const POLL_INTERVAL_MS = 1000
const MAX_POLL_ERRORS = 5

const state = reactive({
  open: false,
  source: 'cpa' as RemoteImportSource,
  sourceId: '',
  sourceName: '',
  job: null as CPAImportJob | null,
  tracking: false,
  error: '',
})

let waiters: Array<(job: CPAImportJob | null) => void> = []
let pollTimer: number | null = null
let finishedListeners: Array<(job: CPAImportJob | null) => void> = []

function isFinished(job: CPAImportJob | null) {
  return !job || job.status === 'completed' || job.status === 'failed'
}

function settle(job: CPAImportJob | null) {
  state.tracking = false
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
  const pending = waiters
  waiters = []
  for (const resolve of pending) resolve(job)
  for (const listener of [...finishedListeners]) listener(job)
}

async function poll(errors = 0) {
  try {
    const response = state.source === 'cpa'
      ? await accountImportsApi.getCPAImportJob(state.sourceId)
      : await accountImportsApi.getSub2APIImportJob(state.sourceId)
    state.job = response.import_job || state.job
    state.error = ''
    if (isFinished(response.import_job || null)) {
      settle(state.job)
      return
    }
    errors = 0
  } catch (error) {
    errors += 1
    state.error = error instanceof Error ? error.message : String(error || '读取导入进度失败')
    if (errors >= MAX_POLL_ERRORS) {
      settle(state.job)
      return
    }
  }
  pollTimer = window.setTimeout(() => { void poll(errors) }, POLL_INTERVAL_MS)
}

function track(active: { source: RemoteImportSource; id: string; name?: string; job: CPAImportJob | null }) {
  if (pollTimer !== null) window.clearTimeout(pollTimer)
  state.source = active.source
  state.sourceId = active.id
  state.sourceName = active.name || active.id
  state.job = active.job
  state.error = ''
  state.open = true
  state.tracking = true
  if (isFinished(active.job)) {
    settle(active.job)
    return
  }
  pollTimer = window.setTimeout(() => { void poll() }, POLL_INTERVAL_MS)
}

/** 等待当前跟踪的任务结束，返回最终任务状态。 */
function whenDone(): Promise<CPAImportJob | null> {
  if (!state.tracking) return Promise.resolve(state.job)
  return new Promise((resolve) => { waiters.push(resolve) })
}

/** 向后端查询是否有进行中的远程导入；有则开始跟踪并返回该任务。 */
async function resume(): Promise<ActiveRemoteImport | null> {
  if (state.tracking) {
    return {
      source: state.source,
      id: state.sourceId,
      name: state.sourceName,
      job: state.job as CPAImportJob,
    }
  }
  try {
    const { active } = await accountImportsApi.getActiveRemoteImport()
    if (!active) return null
    track({ source: active.source, id: active.id, name: active.name, job: active.job })
    return active
  } catch {
    return null
  }
}

function close() {
  state.open = false
}

function onFinished(listener: (job: CPAImportJob | null) => void) {
  finishedListeners.push(listener)
  return () => {
    finishedListeners = finishedListeners.filter((item) => item !== listener)
  }
}

let drawerOwner: symbol | null = null

export function useRemoteImportTracker() {
  const percent = computed(() => {
    const job = state.job
    if (!job || !job.total) return 0
    return Math.min(100, Math.round((job.completed / job.total) * 100))
  })

  return {
    state,
    percent,
    track,
    whenDone,
    resume,
    close,
    onFinished,
    /** 多个导入面板同时存在时，只有第一个挂载的实例负责渲染抽屉。 */
    claimDrawer(owner: symbol) {
      if (drawerOwner === null) drawerOwner = owner
      return drawerOwner === owner
    },
    releaseDrawer(owner: symbol) {
      if (drawerOwner === owner) drawerOwner = null
    },
    isDrawerOwner(owner: symbol) {
      return drawerOwner === owner
    },
  }
}
