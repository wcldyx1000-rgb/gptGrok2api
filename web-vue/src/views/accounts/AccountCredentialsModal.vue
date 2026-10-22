<template>
  <ModalShell :open="open" :z-index="140" max-width="40rem" @close="emit('close')">
    <ModalHeader title="账号凭据" :subtitle="accountLabel" compact @close="emit('close')" />
    <ModalBody density="compact" class="space-y-4">
      <p class="text-xs text-amber-600">
        凭据可直接登录账号，请只在可信环境查看。每次查看或复制都会写入系统日志（不含凭据内容）。
      </p>
      <section v-for="item in items" :key="item.kind" class="space-y-1.5">
        <div class="flex items-center justify-between gap-2">
          <span class="ui-field-label">{{ item.label }}</span>
          <div class="flex items-center gap-2">
            <Button
              size="xs"
              variant="outline"
              :disabled="item.disabled || loading[item.kind]"
              @click="emit('reveal', item.kind)"
            >
              {{ loading[item.kind] ? '读取中...' : values[item.kind] ? '已显示' : '显示' }}
            </Button>
            <Button
              size="xs"
              variant="outline"
              :disabled="item.disabled || loading[item.kind]"
              @click="emit('copy', item.kind)"
            >
              复制
            </Button>
          </div>
        </div>
        <textarea
          readonly
          rows="3"
          class="ui-textarea-sm font-mono"
          :value="values[item.kind]"
          :placeholder="item.disabled ? item.disabledText : '点击“显示”读取'"
        ></textarea>
        <p v-if="errors[item.kind]" class="text-xs text-rose-600">{{ errors[item.kind] }}</p>
      </section>
    </ModalBody>
    <ModalFooter compact>
      <Button size="sm" variant="outline" @click="emit('close')">关闭</Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button } from 'nanocat-ui'

import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import type { AccountCredentialKind } from './accountCredentialsRuntime'

const props = defineProps<{
  open: boolean
  accountLabel: string
  hasRefreshToken: boolean
  values: Record<AccountCredentialKind, string>
  loading: Record<AccountCredentialKind, boolean>
  errors: Record<AccountCredentialKind, string>
}>()

const emit = defineEmits<{
  close: []
  reveal: [kind: AccountCredentialKind]
  copy: [kind: AccountCredentialKind]
}>()

const items = computed(() => [
  { kind: 'access_token' as const, label: 'Access Token', disabled: false, disabledText: '' },
  {
    kind: 'refresh_token' as const,
    label: 'Refresh Token',
    disabled: !props.hasRefreshToken,
    disabledText: '该账号没有保存 refresh token',
  },
])
</script>
