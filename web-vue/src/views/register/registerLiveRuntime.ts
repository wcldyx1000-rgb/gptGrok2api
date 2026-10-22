import { ref } from 'vue'
import type { LegacyRegisterConfig } from '@/api/register'
import type { PageRuntime } from '@/composables/usePageRuntime'

export type RegisterLiveRuntimeInput = {
  runtime: PageRuntime
  getAuthToken: () => string
  loadConfig: (silent?: boolean) => Promise<void>
  applyConfig: (config: LegacyRegisterConfig) => void
  isTaskEnabled: () => boolean
  hasActiveCheckoutRetries?: () => boolean
}

const POLL_INTERVAL_KEY = 'register:poll'

function eventsBaseUrl() {
  return String(import.meta.env.VITE_API_URL || '').replace(/\/$/, '')
}

export function useRegisterLiveRuntime(input: RegisterLiveRuntimeInput) {
  // SSE over fetch + ReadableStream so the admin key travels in the
  // Authorization header instead of the URL (URLs land in access logs).
  const eventSource = ref<AbortController | null>(null)

  function shouldKeepUpdating() {
    return input.isTaskEnabled() || Boolean(input.hasActiveCheckoutRetries?.())
  }

  function stopLiveUpdates() {
    if (eventSource.value) {
      eventSource.value.abort()
      eventSource.value = null
    }
  }

  // 轮询回退：串行（上一次完成后才排下一次）；页面隐藏时 runtime 会清掉定时器，不再继续请求。
  let pollGeneration = 0

  function stopPolling() {
    pollGeneration += 1
    input.runtime.clearTimer(POLL_INTERVAL_KEY)
  }

  function startPolling() {
    stopPolling()
    if (!input.runtime.canRun.value) return
    const generation = pollGeneration
    const tick = async () => {
      if (generation !== pollGeneration || !input.runtime.canRun.value) return
      try {
        await input.loadConfig(true)
      } catch {
        /* 单次失败不终止轮询 */
      }
      if (generation !== pollGeneration || !input.runtime.canRun.value) return
      if (!shouldKeepUpdating()) return
      input.runtime.setTimer(POLL_INTERVAL_KEY, 2000, () => { void tick() })
    }
    input.runtime.setTimer(POLL_INTERVAL_KEY, 2000, () => { void tick() })
  }

  function startLiveUpdates() {
    stopLiveUpdates()
    if (!input.runtime.canRun.value) return
    const token = input.getAuthToken()
    if (!token) {
      startPolling()
      return
    }
    const controller = new AbortController()
    eventSource.value = controller
    void readEventStream(token, controller)
  }

  function handleStreamFailure(controller: AbortController) {
    if (eventSource.value !== controller) return
    stopLiveUpdates()
    if (!input.runtime.canRun.value) return
    startPolling()
  }

  async function readEventStream(token: string, controller: AbortController) {
    try {
      const response = await fetch(`${eventsBaseUrl()}/api/register/events`, {
        headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' },
        cache: 'no-store',
        signal: controller.signal,
      })
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`)
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
        let boundary = buffer.indexOf('\n\n')
        while (boundary >= 0) {
          dispatchBlock(buffer.slice(0, boundary))
          buffer = buffer.slice(boundary + 2)
          boundary = buffer.indexOf('\n\n')
        }
      }
      handleStreamFailure(controller)
    } catch {
      if (controller.signal.aborted) return
      handleStreamFailure(controller)
    }
  }

  function dispatchBlock(block: string) {
    if (!input.runtime.canRun.value) return
    const data = block
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).replace(/^ /, ''))
      .join('\n')
    if (!data) return
    try {
      input.applyConfig(JSON.parse(data) as LegacyRegisterConfig)
    } catch {
      // Ignore malformed event payloads and keep the stream alive.
    }
  }

  function stop() {
    stopLiveUpdates()
    stopPolling()
  }

  return {
    eventSource,
    startLiveUpdates,
    stopLiveUpdates,
    startPolling,
    stopPolling,
    stop,
  }
}
