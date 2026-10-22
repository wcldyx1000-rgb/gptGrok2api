import { computed, ref, type Ref } from 'vue'

import { accountDisplayLabel, accountsApi } from '@/api/accounts'
import type { Account, AccountTestMode } from '@/api/accounts'
import { useModelCatalog } from '@/composables/useModelCatalog'
import { useToast } from '@/composables/useToast'

// Each probe is a remote account info check (no image quota spent). Two at a
// time keeps a single egress from bursting upstream. Real chat / image tests
// spend quota, so they run one at a time.
const BATCH_TEST_CONCURRENCY = 2
const REAL_TEST_CONCURRENCY = 1

export const DEFAULT_CHAT_TEST_MODEL = 'auto'
export const DEFAULT_IMAGE_TEST_MODEL = 'gpt-image-2'

export type AccountBatchTestRow = {
  id: string
  label: string
  status: 'pending' | 'running' | 'success' | 'failed'
  durationMs: number
  message: string
  /** 真实测试返回的完整内容（对话文本或带图片的 Markdown）。 */
  content?: string
  quotaText?: string
}

type AccountBatchTestRuntimeOptions = {
  accounts: Ref<Account[]>
  selectedIds: Ref<string[]>
  loadData: (options?: { silentErrorToast?: boolean }) => Promise<void>
}

export function useAccountBatchTestRuntime(options: AccountBatchTestRuntimeOptions) {
  const toast = useToast()
  const opened = ref(false)
  const running = ref(false)
  const stopRequested = ref(false)
  const rows = ref<AccountBatchTestRow[]>([])
  // probe：只做远程信息探测；chat / image：真实对话或画图（会消耗额度）。
  const mode = ref<AccountTestMode>('probe')
  const model = ref(DEFAULT_CHAT_TEST_MODEL)
  const prompt = ref('请回复“pong”。')
  const detailRowId = ref('')
  // 测试模型下拉选项来自模型目录（目录读不到时退回手输）。
  const { chatModels, imageModels, isLoading: modelCatalogLoading, loadModelCatalog } = useModelCatalog(() => null)
  const modelOptions = computed(() => {
    if (mode.value === 'chat') return chatModels.value
    if (mode.value === 'image') return imageModels.value
    return []
  })
  const detailRow = computed(() => rows.value.find((row) => row.id === detailRowId.value) || null)

  function setMode(next: AccountTestMode) {
    if (running.value) return
    mode.value = next
    if (next === 'chat') {
      if (!model.value || model.value === DEFAULT_IMAGE_TEST_MODEL) model.value = DEFAULT_CHAT_TEST_MODEL
      if (!prompt.value.trim() || prompt.value === '一只坐在窗边的橘猫') prompt.value = '请回复“pong”。'
    } else if (next === 'image') {
      if (!model.value || model.value === DEFAULT_CHAT_TEST_MODEL) model.value = DEFAULT_IMAGE_TEST_MODEL
      if (!prompt.value.trim() || prompt.value === '请回复“pong”。') prompt.value = '一只坐在窗边的橘猫'
    }
  }

  const finished = computed(() => rows.value.filter((row) => row.status === 'success' || row.status === 'failed').length)
  const successCount = computed(() => rows.value.filter((row) => row.status === 'success').length)
  const failedCount = computed(() => rows.value.filter((row) => row.status === 'failed').length)

  function labelFor(id: string) {
    const account = options.accounts.value.find((item) => item.id === id)
    return account?.email || account?.name || accountDisplayLabel(id)
  }

  function open(targetIds?: readonly string[]) {
    if (running.value) return
    const ids = targetIds?.length ? [...targetIds] : [...options.selectedIds.value]
    if (!ids.length) {
      toast.warning('请先选择要测试的账号')
      return
    }
    rows.value = ids.map((id) => ({ id, label: labelFor(id), status: 'pending', durationMs: 0, message: '' }))
    detailRowId.value = ''
    opened.value = true
    void loadModelCatalog().catch(() => undefined)
  }

  function close() {
    if (running.value) return
    opened.value = false
  }

  function stop() {
    if (running.value) stopRequested.value = true
  }

  function snippet(content: string) {
    const text = content.replace(/!\[[^\]]*\]\([^)]*\)/g, '[图片]').replace(/\s+/g, ' ').trim()
    return text.length > 80 ? `${text.slice(0, 80)}…` : text
  }

  async function testOneReal(row: AccountBatchTestRow, started: number) {
    const result = await accountsApi.testAccountCapability(row.id, {
      mode: mode.value as 'chat' | 'image',
      model: model.value.trim(),
      prompt: prompt.value.trim(),
    })
    const ok = result.status === 'success'
    row.status = ok ? 'success' : 'failed'
    row.durationMs = result.duration_ms || Math.round(performance.now() - started)
    row.content = ok ? result.content : ''
    row.quotaText = `额度 ${result.quota_before_label} → ${result.quota_after_label}${result.quota_deducted ? '（已扣减）' : ''}`
    row.message = ok
      ? `${snippet(result.content) || '测试通过'} · ${row.quotaText}`
      : result.error_message || '测试失败'
  }

  async function testOne(row: AccountBatchTestRow) {
    row.status = 'running'
    const started = performance.now()
    try {
      if (mode.value !== 'probe') {
        await testOneReal(row, started)
        return
      }
      const result = await accountsApi.testAccount(row.id)
      row.status = result.ok ? 'success' : 'failed'
      row.durationMs = result.duration_ms || Math.round(performance.now() - started)
      row.message = (result.ok ? result.message : (result.error || result.message)) || (result.ok ? '可用' : '不可用')
    } catch (error) {
      row.status = 'failed'
      row.durationMs = Math.round(performance.now() - started)
      row.message = error instanceof Error ? error.message : String(error || '请求失败')
    }
  }

  async function run() {
    if (running.value || !rows.value.length) return
    if (mode.value !== 'probe' && (!model.value.trim() || !prompt.value.trim())) {
      toast.warning('请填写测试模型和提示词')
      return
    }
    running.value = true
    stopRequested.value = false
    for (const row of rows.value) {
      row.status = 'pending'
      row.durationMs = 0
      row.message = ''
      row.content = ''
      row.quotaText = ''
    }
    const queue = [...rows.value]
    const worker = async () => {
      while (queue.length && !stopRequested.value) {
        const row = queue.shift()
        if (row) await testOne(row)
      }
    }
    try {
      await Promise.all(Array.from({
        length: Math.min(mode.value === 'probe' ? BATCH_TEST_CONCURRENCY : REAL_TEST_CONCURRENCY, queue.length),
      }, worker))
      if (stopRequested.value) toast.info('已停止，未测试的账号保持原样')
      else if (failedCount.value) toast.warning(`测试完成：${successCount.value} 个可用，${failedCount.value} 个不可用`)
      else toast.success(`测试完成：${successCount.value} 个账号都可用`)
    } finally {
      running.value = false
      stopRequested.value = false
      await options.loadData({ silentErrorToast: true })
    }
  }

  return {
    opened,
    running,
    stopRequested,
    rows,
    mode,
    model,
    prompt,
    detailRowId,
    detailRow,
    modelOptions,
    modelCatalogLoading,
    setMode,
    finished,
    successCount,
    failedCount,
    open,
    close,
    stop,
    run,
  }
}
