<template>
  <ModalShell :open="open" :z-index="140" max-width="52rem" @close="emit('close')">
    <ModalHeader
      title="邮箱密码登录导入"
      subtitle="逐个用 邮箱|密码|2FA 登录已有 ChatGPT 账号拿 token，连同浏览器身份写入号池；出口沿用注册中心的代理设置"
      :close-disabled="starting"
      compact
      @close="emit('close')"
    />

    <ModalBody density="compact" class="space-y-3">
      <template v-if="!job || job.terminal">
        <label class="block text-xs">
          <span class="ui-field-label">账号列表{{ lineCount ? `（${lineCount} 行）` : '' }}</span>
          <textarea
            :value="text"
            rows="8"
            class="ui-textarea-sm font-mono"
            placeholder="一行一个：邮箱|密码|2FA 密钥（没有 2FA 可留空）&#10;也支持 ---- / 制表符 / 逗号分隔，# 开头的行会被忽略"
            :disabled="starting"
            @input="emit('update:text', ($event.target as HTMLTextAreaElement).value)"
          ></textarea>
        </label>
        <SurfaceBox tag="label" density="compact" class="flex items-start gap-2">
          <Checkbox :model-value="verify" :disabled="starting" @update:model-value="emit('update:verify', Boolean($event))" />
          <span>
            入池前验证
            <span class="block text-xs text-muted-foreground">登录成功后同步一次额度，状态不是正常或限流的账号不会留在号池。</span>
          </span>
        </SurfaceBox>
      </template>

      <template v-if="job">
        <div class="login-import-summary" aria-live="polite">
          <span>{{ statusLabel[job.status] }}</span>
          <span>进度 <b>{{ job.done }}</b>/{{ job.total }}</span>
          <span class="is-ok">登录成功 <b>{{ job.ok }}</b></span>
          <span v-if="job.verify" class="is-ok">入池可用 <b>{{ job.pool_ok }}</b></span>
          <span class="is-bad">失败 <b>{{ job.fail }}</b></span>
          <span v-if="job.current" class="truncate">当前：{{ job.current }}</span>
        </div>

        <div class="login-import-bar" aria-hidden="true">
          <span :style="{ width: `${job.total ? Math.round((job.done / job.total) * 100) : 0}%` }" />
        </div>

        <SurfaceBox v-if="job.skipped_lines.length" tone="muted" density="compact" wrap>
          有 {{ job.skipped_lines.length }} 行无法识别，已跳过：{{ job.skipped_lines.slice(0, 5).join('、') }}{{ job.skipped_lines.length > 5 ? ' ……' : '' }}
        </SurfaceBox>

        <div v-if="job.results.length" class="login-import-table scrollbar-slim">
          <table class="w-full text-xs">
            <thead>
              <tr>
                <th class="w-[40%]">邮箱</th>
                <th class="w-24">结果</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in job.results" :key="`${row.email}-${index}`">
                <td class="truncate font-medium text-foreground" :title="row.email">{{ row.email }}</td>
                <td>
                  <span class="login-import-state" :class="`is-${resultTone(row)}`">{{ resultLabel(row) }}</span>
                </td>
                <td class="truncate text-muted-foreground" :title="row.error">{{ row.error || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <details v-if="job.logs.length" class="text-xs">
          <summary class="cursor-pointer text-muted-foreground">运行日志（{{ job.logs.length }}）</summary>
          <div class="login-import-logs scrollbar-slim">
            <div v-for="(log, index) in job.logs" :key="index" :class="`is-${log.level}`">{{ log.text }}</div>
          </div>
        </details>
      </template>
    </ModalBody>

    <ModalFooter compact>
      <Button size="sm" variant="outline" @click="emit('close')">{{ job && !job.terminal ? '后台运行' : '关闭' }}</Button>
      <Button v-if="job && !job.terminal" size="sm" variant="outline" :disabled="stopping" @click="emit('stop')">
        {{ stopping ? '停止中...' : '停止' }}
      </Button>
      <Button v-else size="sm" variant="primary" :disabled="starting || !text.trim()" @click="emit('start')">
        {{ starting ? '启动中...' : '开始登录导入' }}
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { Button, Checkbox } from 'nanocat-ui'

import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import type { LoginImportJob, LoginImportJobStatus, LoginImportResult } from '@/api/loginImport'

defineProps<{
  open: boolean
  text: string
  verify: boolean
  job: LoginImportJob | null
  lineCount: number
  starting: boolean
  stopping: boolean
}>()

const emit = defineEmits<{
  close: []
  start: []
  stop: []
  'update:text': [value: string]
  'update:verify': [value: boolean]
}>()

const statusLabel: Record<LoginImportJobStatus, string> = {
  running: '运行中',
  completed: '已完成',
  stopped: '已停止',
}

function resultTone(row: LoginImportResult) {
  if (!row.ok) return 'failed'
  return row.usable === false ? 'warning' : 'success'
}

function resultLabel(row: LoginImportResult) {
  if (!row.ok) return '登录失败'
  return row.usable === false ? '验证未通过' : '已入池'
}
</script>

<style scoped>
.login-import-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}
.login-import-summary b {
  color: hsl(var(--foreground));
  font-variant-numeric: tabular-nums;
}
.login-import-summary .is-ok b { color: rgb(5 150 105); }
.login-import-summary .is-bad b { color: rgb(225 29 72); }

.login-import-bar {
  height: 4px;
  border-radius: 999px;
  background: hsl(var(--muted));
  overflow: hidden;
}
.login-import-bar span {
  display: block;
  height: 100%;
  background: hsl(var(--primary));
  transition: width 0.2s ease;
}

.login-import-table {
  max-height: min(20rem, 45dvh);
  overflow: auto;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
}
.login-import-table table {
  table-layout: fixed;
  border-collapse: collapse;
}
.login-import-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 7px 10px;
  text-align: left;
  font-weight: 500;
  color: hsl(var(--muted-foreground));
  background: hsl(var(--card));
  border-bottom: 1px solid hsl(var(--border));
}
.login-import-table td {
  padding: 7px 10px;
  border-bottom: 1px solid hsl(var(--border) / 0.6);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.login-import-table tr:last-child td { border-bottom: 0; }

.login-import-state {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-weight: 600;
}
.login-import-state.is-success { background: rgb(16 185 129 / 0.12); color: rgb(5 150 105); }
.login-import-state.is-warning { background: rgb(245 158 11 / 0.14); color: rgb(180 83 9); }
.login-import-state.is-failed { background: rgb(244 63 94 / 0.12); color: rgb(225 29 72); }

.login-import-logs {
  margin-top: 6px;
  max-height: 12rem;
  overflow: auto;
  padding: 8px 10px;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  line-height: 1.6;
  color: hsl(var(--muted-foreground));
}
.login-import-logs .is-green { color: rgb(5 150 105); }
.login-import-logs .is-yellow { color: rgb(180 83 9); }
.login-import-logs .is-red { color: rgb(225 29 72); }
</style>
