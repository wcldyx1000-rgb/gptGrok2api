<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only); a submit form was added on top of the recent-task list. -->
<template>
  <ModalShell
    :open="open"
    aria-label="最近文件任务"
    panel-class="studio-recent-file-tasks-modal"
    close-on-backdrop
    @close="emit('close')"
  >
    <ModalHeader
      title="最近文件任务"
      :subtitle="headerSubtitle"
      compact
      @close="emit('close')"
    />

    <ModalBody density="compact" class="recent-file-task-body">
      <form class="recent-file-task-form" @submit.prevent="handleSubmit">
        <div class="recent-file-task-kinds" role="group" aria-label="文件类型">
          <button
            v-for="option in kindOptions"
            :key="option.value"
            type="button"
            class="recent-file-task-kind"
            :class="{ 'is-active': draftKind === option.value }"
            :aria-pressed="draftKind === option.value"
            @click="draftKind = option.value"
          >
            {{ option.label }}
          </button>
        </div>
        <textarea
          v-model="draftPrompt"
          rows="3"
          class="recent-file-task-prompt"
          :placeholder="draftKind === 'psd' ? '描述要拆分成图层的内容（可选）' : '描述要生成的演示文稿（可选）'"
        ></textarea>
        <div class="recent-file-task-upload">
          <label class="recent-file-task-pick">
            <Icon icon="lucide:image-plus" class="h-3.5 w-3.5" />
            <span>{{ draftKind === 'psd' ? '上传原图（必填）' : '上传参考图（可选）' }}</span>
            <input
              type="file"
              accept="image/*"
              multiple
              class="sr-only"
              @change="handleFilesPicked"
            />
          </label>
          <span v-for="(image, index) in draftImages" :key="index" class="recent-file-task-thumb">
            <img :src="image" alt="" />
            <button type="button" aria-label="移除图片" @click="draftImages.splice(index, 1)">
              <Icon icon="lucide:x" class="h-3 w-3" />
            </button>
          </span>
        </div>
        <div class="recent-file-task-submit">
          <p v-if="submitHint" class="recent-file-task-hint">{{ submitHint }}</p>
          <Button size="sm" type="submit" :disabled="isSubmitting || isReadingFiles">
            {{ isSubmitting ? '提交中' : `提交 ${draftKind.toUpperCase()} 任务` }}
          </Button>
        </div>
      </form>

      <PageLoadingState
        v-if="isLoading && tasks.length === 0"
        compact
        title="正在读取文件任务"
        description="获取当前密钥最近提交的 PPT 与 PSD 任务。"
      />

      <StateBlock v-else-if="error && tasks.length === 0">
        <EmptyState plain title="文件任务加载失败" :description="error" />
        <div class="recent-file-task-state-action">
          <Button size="sm" variant="outline" :disabled="isLoading" @click="refresh">
            重新加载
          </Button>
        </div>
      </StateBlock>

      <EmptyState
        v-else-if="tasks.length === 0"
        plain
        title="暂无文件任务"
        description="提交 PPT 或 PSD 后，任务会显示在这里。"
      />

      <div v-else class="recent-file-task-content">
        <p v-if="actionError" class="recent-file-task-action-error" role="alert">
          {{ actionError }}
        </p>

        <div class="recent-file-task-list" aria-live="polite">
          <article v-for="task in tasks" :key="task.id" class="recent-file-task-card">
            <div class="recent-file-task-icon" :class="`is-${task.status}`" aria-hidden="true">
              <Icon
                :icon="task.status_icon"
                class="h-4 w-4"
                :class="{ 'animate-spin': task.is_active }"
              />
            </div>

            <div class="recent-file-task-info">
              <div class="recent-file-task-heading">
                <strong>{{ task.kind.toUpperCase() }} 文件任务</strong>
                <MetaChip size="xs" :tone="task.status_tone">
                  {{ task.status_label }}
                </MetaChip>
              </div>
              <div class="recent-file-task-meta">
                <span>{{ task.updated_at || task.created_at }}</span>
                <span v-if="task.elapsed_seconds > 0">耗时 {{ formatElapsed(task.elapsed_seconds) }}</span>
              </div>
              <p v-if="task.error" class="recent-file-task-error">{{ task.error }}</p>
            </div>

            <div
              v-if="task.can_download || task.can_delete || task.error"
              class="recent-file-task-actions"
            >
              <button
                v-if="task.can_download && task.result?.primary_url"
                type="button"
                class="recent-file-task-download"
                :disabled="isDownloadingFileTask"
                @click="handleDownload(task, task.result.primary_url, 'file')"
              >
                <Icon icon="lucide:download" class="h-3.5 w-3.5" />
                下载文件
              </button>
              <button
                v-if="task.can_download && task.result?.zip_url"
                type="button"
                class="recent-file-task-download"
                :disabled="isDownloadingFileTask"
                @click="handleDownload(task, task.result.zip_url, 'zip')"
              >
                <Icon icon="lucide:archive" class="h-3.5 w-3.5" />
                下载 ZIP
              </button>
              <Button
                v-if="task.error"
                size="xs"
                variant="outline"
                :disabled="isTaskBusy(task.id)"
                @click="handleCopyError(task)"
              >
                复制错误
              </Button>
              <Button
                v-if="task.can_delete"
                size="xs"
                variant="outline"
                root-class="text-rose-600"
                :disabled="isTaskBusy(task.id)"
                @click="handleDelete(task)"
              >
                {{ isTaskBusy(task.id) ? '处理中' : '删除' }}
              </Button>
            </div>
          </article>
        </div>
      </div>
    </ModalBody>

    <ModalFooter align="between" compact>
      <span class="recent-file-task-count">最近 {{ tasks.length }} 条</span>
      <Button size="sm" variant="outline" :disabled="isLoading" @click="refresh">
        {{ isLoading ? '刷新中' : '刷新' }}
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { Icon } from '@iconify/vue'
import { Button, EmptyState } from 'nanocat-ui'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { EditableFileKind, EditableFileTask } from '@/api/editableFileTasks'
import MetaChip from '@/components/ai/MetaChip.vue'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { createSerialPoller } from '@/composables/useSerialPoller'
import { useToast } from '@/composables/useToast'
import { useStudioRecentFileTasksRuntime } from '@/views/studio/studioRecentFileTasksRuntime'
import {
  useEditableFileTaskDownload,
  type EditableFileTaskDownloadType,
} from './editableFileTaskView'

const props = defineProps<{
  open: boolean
  /** 由 Studio 输入区带入的提示词 / 参考图；nonce 变化时重新填充表单。 */
  prefill?: { prompt: string; images: string[]; nonce: number } | null
}>()

const emit = defineEmits<{
  close: []
  deleted: [taskId: string]
}>()

const confirmDialog = useConfirmDialog()
const toast = useToast()
const {
  isDownloading: isDownloadingFileTask,
  download: downloadFileTask,
} = useEditableFileTaskDownload({
  onError: (message) => toast.error(message),
})
const {
  tasks,
  isLoading,
  isSubmitting,
  error,
  actionError,
  isTaskBusy,
  refresh,
  submitTask,
  removeTask,
  copyTaskError,
} = useStudioRecentFileTasksRuntime()

const kindOptions: Array<{ label: string, value: EditableFileKind }> = [
  { label: 'PPT', value: 'ppt' },
  { label: 'PSD', value: 'psd' },
]
const draftKind = ref<EditableFileKind>('ppt')
const draftPrompt = ref('')
const draftImages = ref<string[]>([])
const isReadingFiles = ref(false)
const submitHint = ref('')
const MAX_DRAFT_IMAGES = 8

function readAsDataUrl(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
}

async function handleFilesPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || []).filter((file) => file.type.startsWith('image/'))
  input.value = ''
  if (!files.length) return
  isReadingFiles.value = true
  try {
    const room = Math.max(0, MAX_DRAFT_IMAGES - draftImages.value.length)
    const urls = await Promise.all(files.slice(0, room).map(readAsDataUrl))
    draftImages.value.push(...urls.filter(Boolean))
    if (files.length > room) toast.error(`最多上传 ${MAX_DRAFT_IMAGES} 张图片`)
  } catch (readError) {
    toast.error(readError instanceof Error ? readError.message : '读取图片失败')
  } finally {
    isReadingFiles.value = false
  }
}

async function handleSubmit() {
  submitHint.value = ''
  if (draftKind.value === 'psd' && !draftImages.value.length) {
    submitHint.value = 'PSD 文件任务至少需要上传一张原图'
    return
  }
  const task = await submitTask({
    kind: draftKind.value,
    prompt: draftPrompt.value.trim(),
    base64Images: [...draftImages.value],
  })
  if (task) {
    draftPrompt.value = ''
    draftImages.value = []
    toast.success(`${draftKind.value.toUpperCase()} 文件任务已提交`)
  } else if (actionError.value) {
    toast.error(actionError.value)
  }
}
const headerSubtitle = computed(() => {
  if (isLoading.value && tasks.value.length === 0) return '正在读取最近任务'
  return tasks.value.length ? `${tasks.value.length} 条最近任务` : 'PPT 与 PSD 任务记录'
})

function formatElapsed(seconds: number) {
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds % 60
  return remainingSeconds ? `${minutes} 分 ${remainingSeconds} 秒` : `${minutes} 分`
}

async function handleDownload(
  task: EditableFileTask,
  url: string,
  type: EditableFileTaskDownloadType,
) {
  await downloadFileTask(task, url, type)
}

async function handleCopyError(task: EditableFileTask) {
  const copied = await copyTaskError(task)
  if (copied) toast.success('错误信息已复制')
  else if (actionError.value) toast.error(actionError.value)
}

async function handleDelete(task: EditableFileTask) {
  const confirmed = await confirmDialog.ask({
    title: '删除文件任务',
    message: `确认删除这条 ${task.kind.toUpperCase()} 文件任务记录及其本地产物？此操作不可恢复。`,
    confirmText: '删除',
    cancelText: '取消',
  })
  if (!confirmed) return

  const removed = await removeTask(task)
  if (removed) {
    emit('deleted', task.id)
    toast.success('文件任务已删除')
  }
  else if (actionError.value) toast.error(actionError.value)
}

watch(
  () => props.prefill?.nonce,
  () => {
    const prefill = props.prefill
    if (!prefill) return
    draftPrompt.value = prefill.prompt
    draftImages.value = prefill.images.slice(0, MAX_DRAFT_IMAGES)
  },
  { immediate: true },
)

watch(
  () => props.open,
  (open) => {
    if (open) void refresh()
  },
  { immediate: true },
)

// 串行轮询：上一次刷新结束后才排下一次，页面隐藏时暂停。
const poller = createSerialPoller(() => refresh(), 4000)

function stopPolling() {
  poller.stop()
}

watch(
  [() => props.open, () => tasks.value.some((task) => task.is_active)],
  ([open, hasRunningTask]) => {
    stopPolling()
    if (!open || !hasRunningTask) return
    poller.start()
  },
  { immediate: true },
)

onBeforeUnmount(stopPolling)
</script>

<style scoped>
:global(.studio-recent-file-tasks-modal) {
  display: flex;
  max-height: min(86vh, 44rem);
  flex-direction: column;
}

.recent-file-task-body {
  min-height: 12rem;
  overflow-y: auto;
}

.recent-file-task-form {
  display: grid;
  gap: 0.55rem;
  margin-bottom: 0.8rem;
  border: 1px solid hsl(var(--border) / 0.76);
  border-radius: 0.8rem;
  background: hsl(var(--background) / 0.72);
  padding: 0.7rem 0.75rem;
}

.recent-file-task-kinds {
  display: inline-flex;
  gap: 0.4rem;
}

.recent-file-task-kind {
  border: 1px solid hsl(var(--border));
  border-radius: 0.58rem;
  background: hsl(var(--card));
  color: hsl(var(--foreground));
  padding: 0.25rem 0.8rem;
  font-size: 0.75rem;
  font-weight: 650;
  font-family: inherit;
  cursor: pointer;
}

.recent-file-task-kind.is-active {
  border-color: hsl(var(--primary));
  background: hsl(var(--primary) / 0.1);
}

.recent-file-task-prompt {
  width: 100%;
  resize: vertical;
  border: 1px solid hsl(var(--border));
  border-radius: 0.65rem;
  background: hsl(var(--card));
  color: hsl(var(--foreground));
  padding: 0.5rem 0.65rem;
  font-size: 0.8rem;
  font-family: inherit;
}

.recent-file-task-upload {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}

.recent-file-task-pick {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  border: 1px dashed hsl(var(--border));
  border-radius: 0.58rem;
  padding: 0.3rem 0.6rem;
  font-size: 0.74rem;
  cursor: pointer;
}

.recent-file-task-thumb {
  position: relative;
  width: 2.4rem;
  height: 2.4rem;
  overflow: hidden;
  border-radius: 0.5rem;
  border: 1px solid hsl(var(--border));
}

.recent-file-task-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.recent-file-task-thumb button {
  position: absolute;
  top: 0;
  right: 0;
  display: inline-flex;
  background: rgb(0 0 0 / 0.55);
  color: white;
  border: 0;
  cursor: pointer;
}

.recent-file-task-submit {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6rem;
}

.recent-file-task-hint {
  margin: 0;
  color: rgb(220 38 38);
  font-size: 0.74rem;
}

.recent-file-task-list {
  display: grid;
  gap: 0.55rem;
}

.recent-file-task-content {
  display: grid;
  gap: 0.65rem;
}

.recent-file-task-action-error {
  margin: 0;
  border: 1px solid rgb(239 68 68 / 0.2);
  border-radius: 0.65rem;
  background: rgb(239 68 68 / 0.06);
  color: rgb(220 38 38);
  padding: 0.55rem 0.65rem;
  font-size: 0.75rem;
  line-height: 1.45;
}

.recent-file-task-card {
  display: grid;
  min-width: 0;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 0.7rem;
  border: 1px solid hsl(var(--border) / 0.76);
  border-radius: 0.8rem;
  background: hsl(var(--background) / 0.72);
  padding: 0.65rem 0.75rem;
}

.recent-file-task-icon {
  display: inline-flex;
  width: 2rem;
  height: 2rem;
  align-items: center;
  justify-content: center;
  border-radius: 0.65rem;
  background: hsl(var(--secondary));
  color: hsl(var(--muted-foreground));
}

.recent-file-task-icon.is-success {
  background: rgb(34 197 94 / 0.1);
  color: rgb(22 163 74);
}

.recent-file-task-icon.is-error {
  background: rgb(239 68 68 / 0.1);
  color: rgb(220 38 38);
}

.recent-file-task-info {
  display: grid;
  min-width: 0;
  gap: 0.28rem;
}

.recent-file-task-heading,
.recent-file-task-meta,
.recent-file-task-actions,
.recent-file-task-download {
  display: flex;
  align-items: center;
}

.recent-file-task-heading {
  min-width: 0;
  flex-wrap: wrap;
  gap: 0.45rem;
}

.recent-file-task-heading strong {
  color: hsl(var(--foreground));
  font-size: 0.82rem;
  font-weight: 700;
}

.recent-file-task-meta {
  flex-wrap: wrap;
  gap: 0.35rem 0.75rem;
  color: hsl(var(--muted-foreground));
  font-size: 0.72rem;
}

.recent-file-task-error {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  color: rgb(220 38 38);
  font-size: 0.74rem;
  line-height: 1.45;
}

.recent-file-task-actions {
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.4rem;
}

.recent-file-task-download {
  min-height: 1.9rem;
  justify-content: center;
  gap: 0.3rem;
  border: 1px solid hsl(var(--border));
  border-radius: 0.58rem;
  background: hsl(var(--card));
  color: hsl(var(--foreground));
  padding: 0.3rem 0.5rem;
  font-size: 0.72rem;
  font-weight: 650;
  font-family: inherit;
  cursor: pointer;
  text-decoration: none;
}

.recent-file-task-download:hover,
.recent-file-task-download:focus-visible {
  border-color: hsl(var(--foreground) / 0.22);
  background: hsl(var(--secondary));
}

.recent-file-task-download:disabled {
  cursor: wait;
  opacity: 0.56;
}

.recent-file-task-state-action {
  display: flex;
  justify-content: center;
  margin-top: 0.75rem;
}

.recent-file-task-count {
  color: hsl(var(--muted-foreground));
  font-size: 0.76rem;
}

@media (max-width: 640px) {
  .recent-file-task-card {
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
  }

  .recent-file-task-actions {
    grid-column: 2;
    justify-content: flex-start;
  }
}
</style>
