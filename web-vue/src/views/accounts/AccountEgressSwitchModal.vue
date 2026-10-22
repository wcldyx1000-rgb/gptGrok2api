<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<template>
  <ModalShell :open="open" aria-label="更换出口" :z-index="140" scrollable @close="emit('close')">
    <ModalHeader
      title="更换出口"
      :subtitle="accountLabel ? accountLabel : `选中的 ${count} 个账号`"
      :close-disabled="submitting"
      compact
      @close="emit('close')"
    />

    <ModalBody density="compact" class="space-y-3">
      <div class="egress-switch-warning">
        换出口就是换 IP：账号会<b>立即停用 {{ hours }} 小时</b>，期间生图、对话都不接；到点自动切到新出口并恢复。
        等待期间可以「取消更换出口」，取消后马上恢复原出口。
      </div>

      <p v-if="error" class="text-xs text-rose-600">{{ error }}</p>

      <div class="egress-options" role="radiogroup" aria-label="目标出口">
        <p v-if="loading && !options.length" class="col-span-2 py-4 text-center text-xs text-muted-foreground">读取出口槽位…</p>
        <label
          v-for="option in options"
          :key="option.value"
          class="egress-option"
          :class="{ 'is-selected': modelValue === option.value, 'is-disabled': option.disabled }"
        >
          <input
            type="radio"
            name="egress-target"
            class="sr-only"
            :value="option.value"
            :checked="modelValue === option.value"
            :disabled="option.disabled || submitting"
            @change="emit('update:modelValue', option.value)"
          />
          <span class="egress-radio" aria-hidden="true" />
          <span class="min-w-0 flex-1">
            <span class="block text-[13px] font-medium text-foreground">{{ option.label }}</span>
            <span class="block truncate text-[11.5px] text-muted-foreground" :title="option.detail">{{ option.detail }}</span>
          </span>
        </label>
      </div>

      <p class="text-[11.5px] text-muted-foreground">已经是这个出口、或目标槽位满了的号会被跳过，结果会提示。</p>
    </ModalBody>

    <ModalFooter compact>
      <Button size="sm" variant="outline" :disabled="submitting" @click="emit('close')">取消</Button>
      <Button size="sm" variant="primary" :disabled="!modelValue || submitting" @click="emit('submit')">
        {{ submitting ? '提交中…' : (hours > 0 ? `停用 ${hours} 小时并更换` : '立即更换') }}
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { Button } from 'nanocat-ui'

import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import type { EgressTargetOption } from './accountEgressRuntime'

defineProps<{
  open: boolean
  modelValue: string
  options: EgressTargetOption[]
  count: number
  accountLabel: string
  hours: number
  loading: boolean
  submitting: boolean
  error: string
}>()

const emit = defineEmits<{
  close: []
  submit: []
  'update:modelValue': [value: string]
}>()
</script>

<style scoped>
.egress-switch-warning {
  padding: 9px 11px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: rgb(146 64 14);
  background: rgb(245 158 11 / 0.12);
}
:global(html[data-theme="dark"]) .egress-switch-warning { color: rgb(253 230 138); }
.egress-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.egress-option {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}
.egress-option:hover { border-color: hsl(var(--foreground) / 0.3); }
.egress-option.is-selected { border-color: hsl(var(--primary)); background: hsl(var(--primary) / 0.05); }
.egress-option.is-disabled { cursor: not-allowed; opacity: 0.5; }
.egress-radio {
  width: 14px;
  height: 14px;
  flex: none;
  border: 1.5px solid hsl(var(--muted-foreground) / 0.6);
  border-radius: 999px;
}
.is-selected .egress-radio {
  border-color: hsl(var(--primary));
  box-shadow: inset 0 0 0 3px hsl(var(--card)), inset 0 0 0 7px hsl(var(--primary));
}
</style>
