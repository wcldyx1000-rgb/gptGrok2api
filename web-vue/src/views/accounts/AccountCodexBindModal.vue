<template>
  <ModalShell :open="open" :z-index="140" max-width="40rem" @close="emit('close')">
    <ModalHeader title="绑定 Codex 登录" :subtitle="accountLabel" :close-disabled="busy" compact @close="emit('close')" />
    <ModalBody density="compact" class="space-y-3">
      <SurfaceBox tone="muted" density="compact" wrap>
        为该账号额外保存一套 Codex 客户端登录，仅供 codex-gpt-image-2 画图使用；账号原有的 access / refresh token 不会被修改。
        <span v-if="bound" class="font-medium text-emerald-600">当前已绑定，重新授权会覆盖旧的 Codex 登录。</span>
      </SurfaceBox>
      <p class="text-xs text-amber-600">
        该功能移植自参考项目，上游自述未端到端验证，本项目也未做真实联网验证，请先用单个账号试用。
      </p>
      <label class="block text-xs">
        <span class="ui-field-label">账号邮箱（用于登录提示，可选）</span>
        <Input :model-value="emailHint" block :disabled="busy" placeholder="name@example.com" @update:model-value="emit('update:emailHint', $event.trim())" />
      </label>
      <div class="flex flex-wrap gap-2">
        <Button size="xs" variant="primary" :disabled="busy" @click="emit('start')">
          {{ authorizeUrl ? '重新生成授权链接' : '生成并打开授权页面' }}
        </Button>
        <Button v-if="authorizeUrl" size="xs" variant="outline" :disabled="busy" @click="emit('open-url')">打开授权页面</Button>
        <Button v-if="authorizeUrl" size="xs" variant="outline" :disabled="busy" @click="emit('copy-url')">复制授权链接</Button>
      </div>
      <SurfaceBox v-if="authorizeUrl" tone="muted" density="compact" wrap>
        登录完成后浏览器会跳转到 {{ redirectPrefix || 'http://localhost:1455/auth/callback' }}（本机没有服务监听，页面打不开是正常的），
        把地址栏里的完整地址粘贴到下方。
      </SurfaceBox>
      <label class="block text-xs">
        <span class="ui-field-label">Callback 地址 / Code</span>
        <textarea
          :value="callback"
          rows="4"
          class="ui-textarea-sm font-mono"
          placeholder="粘贴完整 callback 地址，或只粘贴 code"
          :disabled="busy || !sessionId"
          @input="emit('update:callback', ($event.target as HTMLTextAreaElement).value)"
        ></textarea>
      </label>
    </ModalBody>
    <ModalFooter compact>
      <Button size="sm" variant="outline" :disabled="busy" @click="emit('close')">取消</Button>
      <Button size="sm" variant="primary" :disabled="busy || !sessionId || !callback.trim()" @click="emit('finish')">
        {{ busy ? '处理中...' : '完成绑定' }}
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { Button, Input } from 'nanocat-ui'

import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'

defineProps<{
  open: boolean
  busy: boolean
  accountLabel: string
  emailHint: string
  authorizeUrl: string
  redirectPrefix: string
  callback: string
  sessionId: string
  bound: boolean
}>()

const emit = defineEmits<{
  close: []
  start: []
  'open-url': []
  'copy-url': []
  finish: []
  'update:emailHint': [value: string]
  'update:callback': [value: string]
}>()
</script>
