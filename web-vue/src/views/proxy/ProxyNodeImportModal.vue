<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<template>
  <ModalShell
    :open="open"
    max-width="40rem"
    :z-index="140"
    @close="requestClose"
  >
    <ModalHeader
      title="批量添加代理节点"
      :subtitle="groupName || '当前代理组草稿'"
      compact
      @close="requestClose"
    />

    <ModalBody class="space-y-4">
      <label class="block text-xs">
        <span class="ui-field-label">代理节点</span>
        <textarea
          v-model="sourceText"
          rows="12"
          class="ui-textarea-sm min-h-[17rem] w-full resize-y font-mono"
          placeholder="http://127.0.0.1:7890 30&#10;http://user:password@host:7890 20&#10;socks5://127.0.0.1:1080&#10;host:port:user:pass"
          aria-label="每行一个代理地址，可在地址后填写图片并发"
          autocomplete="off"
          autocapitalize="off"
          spellcheck="false"
        ></textarea>
      </label>

      <div class="space-y-1 text-xs leading-5 text-muted-foreground">
        <p>每行格式：代理地址 图片并发</p>
        <p>图片并发省略时默认 30，填写 0 表示不限；重复地址会自动跳过。</p>
      </div>

      <section v-if="invalidItems.length" class="space-y-2" aria-label="格式错误">
        <p class="text-xs font-medium text-rose-600">请修正以下 {{ invalidItems.length }} 行</p>
        <div class="max-h-40 overflow-y-auto border-y border-border">
          <div
            v-for="item in invalidItems"
            :key="`${item.line}:${item.raw}`"
            class="grid grid-cols-[2.25rem_minmax(0,1fr)] gap-2 border-b border-border/70 py-2 text-xs last:border-b-0"
          >
            <span class="text-right font-mono text-muted-foreground">{{ item.line }}</span>
            <div class="min-w-0">
              <p class="break-all font-mono text-foreground">{{ item.raw }}</p>
              <p class="mt-0.5 text-rose-600">{{ item.reason }}</p>
            </div>
          </div>
        </div>
      </section>

      <p v-if="submitError" class="text-xs text-rose-600">{{ submitError }}</p>
    </ModalBody>

    <ModalFooter align="between">
      <span class="text-xs text-muted-foreground">添加到草稿，保存代理组后生效</span>
      <div class="flex items-center gap-2">
        <Button size="xs" variant="outline" :disabled="submitting" @click="requestClose">取消</Button>
        <Button
          size="xs"
          variant="primary"
          :disabled="submitting || !sourceText.trim()"
          @click="submitImport"
        >
          {{ submitting ? '添加中...' : '批量添加' }}
        </Button>
      </div>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { Button } from 'nanocat-ui'

import {
  proxyApi,
  type ProxyNodeImportInvalidItem,
  type ProxyNodeImportResult,
} from '@/api/proxy'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

const props = withDefaults(defineProps<{
  open: boolean
  groupName?: string
  existingUrls?: string[]
}>(), {
  groupName: '',
  existingUrls: () => [],
})

const emit = defineEmits<{
  close: []
  apply: [result: ProxyNodeImportResult]
}>()

const toast = useToast()
const sourceText = ref('')
const submitting = ref(false)
const submitError = ref('')
const invalidItems = ref<ProxyNodeImportInvalidItem[]>([])

watch(() => props.open, (open) => {
  if (!open) return
  sourceText.value = ''
  submitError.value = ''
  invalidItems.value = []
  submitting.value = false
})

watch(sourceText, () => {
  submitError.value = ''
  invalidItems.value = []
})

function requestClose() {
  if (submitting.value) return
  emit('close')
}

async function submitImport() {
  if (submitting.value || !sourceText.value.trim()) return
  submitting.value = true
  submitError.value = ''
  invalidItems.value = []
  try {
    const result = await proxyApi.importNodes(sourceText.value, props.existingUrls)
    // 有格式错误时整批不写入，让用户修正后重试，避免部分导入后难以对照。
    if (result.invalid_items.length) {
      invalidItems.value = result.invalid_items
      return
    }
    if (!result.nodes.length) {
      submitError.value = result.duplicate_count
        ? `所有 ${result.duplicate_count} 个地址都已存在`
        : '没有可添加的代理节点'
      return
    }
    emit('apply', result)
    toast.success(
      result.duplicate_count
        ? `已添加 ${result.added_count} 个节点，跳过 ${result.duplicate_count} 个重复地址`
        : `已添加 ${result.added_count} 个节点`,
    )
    emit('close')
  } catch (error) {
    submitError.value = errorMessage(error, '批量添加代理节点失败')
  } finally {
    submitting.value = false
  }
}
</script>
