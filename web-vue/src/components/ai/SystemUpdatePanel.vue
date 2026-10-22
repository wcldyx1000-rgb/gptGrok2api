<template>
  <section
    v-if="status?.update_available"
    class="mt-3 rounded-2xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm"
    aria-label="一键更新"
  >
    <div class="flex flex-wrap items-center justify-between gap-2">
      <p class="min-w-0 flex-1 font-medium text-amber-800 dark:text-amber-200">
        {{ status.status_message || `发现新版本 ${status.latest_tag}` }}
      </p>
      <Button
        v-if="status.can_update"
        size="xs"
        variant="primary"
        :disabled="busy"
        @click="startUpdate"
      >
        {{ busy ? '更新中...' : `一键更新到 ${status.latest_tag}` }}
      </Button>
    </div>

    <div v-if="task && task.state !== 'idle'" class="mt-3 space-y-2" aria-live="polite">
      <ProgressBar :value="progress" aria-label="更新进度" />
      <p :class="taskToneClass" class="text-xs font-medium">
        {{ task.status_label }}：{{ task.message }}
      </p>
      <p v-if="task.error && task.state === 'failed'" class="text-xs text-rose-600">
        {{ task.error }}（原有版本未受影响，可稍后重试，或前往发布页手动升级。）
      </p>
      <p v-if="restarting" class="text-xs text-muted-foreground">
        服务正在重启，页面会在恢复后自动刷新，请勿关闭。
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Button } from 'nanocat-ui'
import { versionApi } from '@/api/version'
import ProgressBar from '@/components/ai/ProgressBar.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import type { SystemUpdateTask, VersionCheckResponse } from '@/types/api'

const props = defineProps<{
  status: VersionCheckResponse | null
}>()

const confirmDialog = useConfirmDialog()
const toast = useToast()
const task = ref<SystemUpdateTask | null>(null)
const starting = ref(false)
const restarting = ref(false)
let timer: number | null = null
let failedPolls = 0

const busy = computed(() => starting.value || Boolean(task.value?.busy) || restarting.value)
const progress = computed(() => {
  if (!task.value || !task.value.total) return 0
  return Math.round((task.value.current / task.value.total) * 100)
})
const taskToneClass = computed(() => {
  switch (task.value?.tone) {
    case 'success': return 'text-emerald-600'
    case 'danger': return 'text-rose-600'
    case 'warning': return 'text-amber-700'
    default: return 'text-foreground/80'
  }
})

function stopPolling() {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}

function schedulePoll() {
  stopPolling()
  timer = window.setTimeout(pollTask, 2000)
}

async function pollTask() {
  try {
    const next = await versionApi.updateTask()
    failedPolls = 0
    task.value = next
    restarting.value = next.stage === 'restarting' && next.busy
    if (next.busy) {
      schedulePoll()
      return
    }
    if (next.state === 'succeeded' && next.stage === 'completed' && next.latest_tag) {
      toast.success(`已更新到 ${next.latest_tag}，即将刷新页面`)
      window.setTimeout(() => window.location.reload(), 1500)
    } else if (next.state === 'failed') {
      toast.error(next.error || '更新失败')
    }
  } catch {
    // 重启期间接口暂时不可达是预期行为，持续重试一段时间。
    failedPolls += 1
    if (restarting.value && failedPolls < 90) {
      schedulePoll()
    } else if (failedPolls < 5) {
      schedulePoll()
    } else {
      toast.warning('无法获取更新进度，请刷新页面确认服务状态')
    }
  }
}

async function startUpdate() {
  const target = props.status?.latest_tag || '新版本'
  const current = props.status?.tag || props.status?.version || '当前版本'
  const confirmed = await confirmDialog.ask({
    title: `确认更新到 ${target}`,
    message: [
      `当前版本：${current}`,
      `目标版本：${target}（来自本项目 GitHub Releases，下载后会校验 SHA-256）`,
      '预计耗时：约 1–5 分钟，取决于网络和依赖同步速度。',
      '更新完成后服务会自动重启，重启期间（约十几秒到一两分钟）接口和管理台不可用，进行中的请求会中断。',
      '数据与配置不会被修改；若校验或依赖同步失败，会自动恢复原版本并显示原因。',
      '是否现在开始更新？',
    ].join('\n'),
    confirmText: '开始更新',
    cancelText: '取消',
  })
  if (!confirmed) return

  starting.value = true
  try {
    task.value = await versionApi.startUpdate()
    failedPolls = 0
    schedulePoll()
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    toast.error((detail && (detail.error || detail)) || error?.message || '无法开始更新')
  } finally {
    starting.value = false
  }
}

onMounted(async () => {
  // 页面刷新后恢复进行中的更新任务显示；空闲任务不展示。
  try {
    const current = await versionApi.updateTask()
    if (current.state !== 'idle') task.value = current
    if (current.busy) {
      restarting.value = current.stage === 'restarting'
      schedulePoll()
    }
  } catch {
    // 忽略：任务状态不可用时仅隐藏进度区域
  }
})

onBeforeUnmount(stopPolling)
</script>
