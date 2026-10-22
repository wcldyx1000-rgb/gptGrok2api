import { computed, onBeforeUnmount, ref } from 'vue'

import { loginImportApi, type LoginImportJob } from '@/api/loginImport'
import type { Account } from '@/api/accounts'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

type ReadableRef<T> = {
  readonly value: T
}

type AccountLoginImportRuntimeOptions = {
  accounts: ReadableRef<Account[]>
  selectedIds: ReadableRef<readonly string[]>
  loadData: (options?: { silentErrorToast?: boolean }) => Promise<void>
}

const POLL_INTERVAL_MS = 1500

/** 邮箱|密码|2FA 登录导入，以及选中账号用 Outlook 无密码登录重新激活。 */
export function useAccountLoginImportRuntime(options: AccountLoginImportRuntimeOptions) {
  const toast = useToast()
  const confirmDialog = useConfirmDialog()

  const opened = ref(false)
  const text = ref('')
  const verify = ref(true)
  const job = ref<LoginImportJob | null>(null)
  const starting = ref(false)
  const stopping = ref(false)
  const reactivating = ref(false)
  let pollTimer: ReturnType<typeof setTimeout> | null = null

  const running = computed(() => Boolean(job.value && !job.value.terminal))
  const lineCount = computed(() => text.value.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith('#')).length)

  function clearPoll() {
    if (pollTimer) clearTimeout(pollTimer)
    pollTimer = null
  }

  async function poll() {
    clearPoll()
    const jobId = job.value?.job_id
    if (!jobId) return
    try {
      const response = await loginImportApi.get(jobId)
      if (response.job) job.value = response.job
    } catch (error) {
      toast.error(`获取登录导入进度失败：${errorMessage(error)}`)
    }
    if (job.value && !job.value.terminal) {
      pollTimer = setTimeout(poll, POLL_INTERVAL_MS)
      return
    }
    stopping.value = false
    if (job.value) {
      const message = job.value.result_message || '登录导入已结束'
      if (job.value.fail > 0 || job.value.status === 'stopped') toast.warning(message)
      else toast.success(message)
    }
    await options.loadData({ silentErrorToast: true })
  }

  async function open() {
    opened.value = true
    if (job.value) return
    // 页面刷新后接上仍在运行的任务。
    try {
      const response = await loginImportApi.get()
      if (response.job) {
        job.value = response.job
        if (!response.job.terminal) void poll()
      }
    } catch {
      // 拿不到最近任务不影响新建。
    }
  }

  function close() {
    opened.value = false
  }

  async function start() {
    if (!text.value.trim() || running.value) return
    starting.value = true
    try {
      const response = await loginImportApi.start(text.value, verify.value)
      job.value = response.job
      text.value = ''
      void poll()
    } catch (error) {
      toast.error(`启动登录导入失败：${errorMessage(error)}`)
    } finally {
      starting.value = false
    }
  }

  async function stop() {
    const jobId = job.value?.job_id
    if (!jobId || !running.value) return
    stopping.value = true
    try {
      const response = await loginImportApi.stop(jobId)
      if (response.job) job.value = response.job
    } catch (error) {
      stopping.value = false
      toast.error(`停止登录导入失败：${errorMessage(error)}`)
    }
  }

  async function reactivateSelected() {
    if (reactivating.value) return
    const selected = new Set(options.selectedIds.value)
    const emails = Array.from(new Set(
      options.accounts.value
        .filter((item) => selected.has(item.id))
        .map((item) => String(item.email || '').trim())
        .filter(Boolean),
    ))
    if (!emails.length) {
      toast.warning('选中的账号都没有邮箱，无法重新激活')
      return
    }
    const confirmed = await confirmDialog.ask({
      title: '重新激活选中账号？',
      message: `将用 Outlook 邮箱验证码无密码登录 ${emails.length} 个账号并换上新 token。\n只对保存了 Outlook 邮箱凭据（或邮箱仍在 OutlookToken 邮箱池）的账号有效，账号之间会间隔几秒，可能需要等待一段时间。`,
      confirmText: '开始激活',
      cancelText: '取消',
    })
    if (!confirmed) return
    reactivating.value = true
    try {
      const result = await loginImportApi.reactivate(emails)
      const failures = result.results.filter((item) => !item.ok)
      if (failures.length) {
        toast.warning(`重新激活完成：成功 ${result.ok}，失败 ${result.fail}`)
        await confirmDialog.ask({
          title: '重新激活失败的账号',
          message: failures.slice(0, 30).map((item) => `${item.email}：${item.error || '未知错误'}`).join('\n'),
          confirmText: '知道了',
          cancelText: '关闭',
        })
      } else {
        toast.success(`重新激活完成：成功 ${result.ok}`)
      }
    } catch (error) {
      toast.error(`重新激活失败：${errorMessage(error)}`)
    } finally {
      reactivating.value = false
      await options.loadData({ silentErrorToast: true })
    }
  }

  onBeforeUnmount(clearPoll)

  return {
    opened,
    text,
    verify,
    job,
    running,
    starting,
    stopping,
    reactivating,
    lineCount,
    open,
    close,
    start,
    stop,
    reactivateSelected,
  }
}
