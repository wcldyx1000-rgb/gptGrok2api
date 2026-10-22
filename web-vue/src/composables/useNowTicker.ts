// Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
import { onScopeDispose, ref } from 'vue'

// 全页共用一个时钟（秒），让"剩余时间"自己往下走，不用每个单元格各开定时器。
const now = ref(Math.floor(Date.now() / 1000))
let timer = 0
let users = 0

export function useNowTicker() {
  users += 1
  now.value = Math.floor(Date.now() / 1000)
  if (!timer) {
    timer = window.setInterval(() => {
      now.value = Math.floor(Date.now() / 1000)
    }, 30_000)
  }
  onScopeDispose(() => {
    users -= 1
    if (users <= 0 && timer) {
      window.clearInterval(timer)
      timer = 0
    }
  })
  return now
}
