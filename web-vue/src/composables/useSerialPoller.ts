// 串行轮询：上一次执行完成后才排下一次，页面隐藏时停止，重新可见时立即补一次。
// 对应审计结论「轮询必须非并发、不随页面隐藏继续打后端」。

export type SerialPoller = {
  start: (options?: { immediate?: boolean }) => void
  stop: () => void
  readonly running: boolean
}

export function createSerialPoller(
  run: () => unknown | Promise<unknown>,
  intervalMs: number,
): SerialPoller {
  let running = false
  let inflight = false
  let timer: ReturnType<typeof setTimeout> | undefined

  function clearTimer() {
    if (timer !== undefined) {
      clearTimeout(timer)
      timer = undefined
    }
  }

  function schedule(delay: number) {
    clearTimer()
    if (!running) return
    timer = setTimeout(() => {
      timer = undefined
      void tick()
    }, delay)
  }

  async function tick() {
    if (!running || inflight) return
    // 隐藏时不执行也不续约；visibilitychange 回到可见后再恢复。
    if (typeof document !== 'undefined' && document.hidden) return
    inflight = true
    try {
      await run()
    } catch {
      /* 单次失败不终止轮询 */
    } finally {
      inflight = false
      schedule(intervalMs)
    }
  }

  function onVisibilityChange() {
    if (!running) return
    if (typeof document !== 'undefined' && document.hidden) {
      clearTimer()
      return
    }
    if (!inflight && timer === undefined) schedule(0)
  }

  return {
    start(options = {}) {
      if (running) return
      running = true
      if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibilityChange)
      schedule(options.immediate ? 0 : intervalMs)
    },
    stop() {
      running = false
      clearTimer()
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibilityChange)
    },
    get running() {
      return running
    },
  }
}
