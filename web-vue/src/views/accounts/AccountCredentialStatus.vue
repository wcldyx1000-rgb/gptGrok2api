<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<template>
  <div class="inline-flex min-w-0 items-center">
    <HoverCard card-class="w-72" :offset="8">
      <span class="account-cell" aria-label="凭据状态">
        <span class="inline-flex items-center gap-1.5">
          <MetaChip :tone="chipTone(item.access_token_tone)" size="xs" strong>
            {{ item.access_token_label || 'AT —' }}
          </MetaChip>
          <MetaChip :tone="refreshChipTone" size="xs" strong>
            {{ item.refresh_token_label || 'RT —' }}
          </MetaChip>
        </span>
        <span v-if="item.rescue_label" class="account-cell-sub max-w-[10rem]" :class="rescueClass">
          失效后 {{ item.rescue_label }}<template v-if="item.has_password"> · 密码{{ item.has_totp ? '+2FA' : '' }}</template>
        </span>
      </span>

      <template #content>
        <div class="space-y-3 text-xs leading-5">
          <section>
            <div class="mb-1.5 flex items-center justify-between gap-3">
              <div class="text-[11px] font-semibold text-foreground">Access Token</div>
              <Button
                size="xs"
                variant="outline"
                root-class="h-6 px-2 text-[11px] font-normal"
                @click.stop="emit('copy-credential', 'access')"
              >
                复制 AT
              </Button>
            </div>
            <dl class="space-y-1">
              <div class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">状态</dt>
                <dd class="text-right font-medium text-foreground">{{ item.access_token_label || '—' }}</dd>
              </div>
              <div v-if="item.access_token_issued_at" class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">签发时间</dt>
                <dd class="text-right tabular-nums text-foreground">{{ formatAccountDate(item.access_token_issued_at) }}</dd>
              </div>
              <div v-if="item.access_token_expires_at" class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">到期时间</dt>
                <dd class="text-right tabular-nums text-foreground">{{ formatAccountDate(item.access_token_expires_at) }}</dd>
              </div>
            </dl>
          </section>

          <section class="border-t border-border/70 pt-2.5">
            <div class="mb-1.5 flex items-center justify-between gap-3">
              <div class="text-[11px] font-semibold text-foreground">Refresh Token</div>
              <Button
                size="xs"
                variant="outline"
                root-class="h-6 px-2 text-[11px] font-normal"
                :disabled="refreshTokenMissing"
                @click.stop="emit('copy-credential', 'refresh')"
              >
                复制 RT
              </Button>
            </div>
            <dl class="space-y-1">
              <div class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">状态</dt>
                <dd class="text-right font-medium text-foreground">{{ item.refresh_token_label || '—' }}</dd>
              </div>
              <div v-if="item.last_token_refresh_at" class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">最近成功刷新 AT</dt>
                <dd class="text-right tabular-nums text-foreground">{{ formatAccountDate(item.last_token_refresh_at) }}</dd>
              </div>
              <div v-if="item.refresh_token_invalid_at" class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">失效时间</dt>
                <dd class="text-right tabular-nums text-foreground">{{ formatAccountDate(item.refresh_token_invalid_at) }}</dd>
              </div>
              <div v-if="item.last_token_refresh_error_at" class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">失败时间</dt>
                <dd class="text-right tabular-nums text-foreground">{{ formatAccountDate(item.last_token_refresh_error_at) }}</dd>
              </div>
              <div v-if="item.last_token_refresh_error" class="flex items-start justify-between gap-4">
                <dt class="shrink-0 text-muted-foreground">失败原因</dt>
                <dd class="break-words text-right text-foreground">{{ item.last_token_refresh_error }}</dd>
              </div>
            </dl>
          </section>

          <section v-if="item.rescue_label" class="border-t border-border/70 pt-2.5">
            <div class="mb-1.5 text-[11px] font-semibold text-foreground">AT 失效后</div>
            <dl class="space-y-1">
              <div class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">救回方式</dt>
                <dd class="text-right font-medium" :class="rescueClass">{{ item.rescue_label }}</dd>
              </div>
              <div class="flex items-start justify-between gap-4">
                <dt class="shrink-0 text-muted-foreground">说明</dt>
                <dd class="break-words text-right text-foreground">{{ item.rescue_detail }}</dd>
              </div>
              <div class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">Outlook / 密码 / 2FA</dt>
                <dd class="text-right text-foreground">
                  {{ item.has_outlook_credentials ? '有' : '无' }} / {{ item.has_password ? '有' : '无' }} / {{ item.has_totp ? '有' : '无' }}
                </dd>
              </div>
              <div v-if="item.relogin_failures" class="flex items-start justify-between gap-4">
                <dt class="text-muted-foreground">自动重登失败</dt>
                <dd class="text-right tabular-nums text-foreground">{{ item.relogin_failures }} 次</dd>
              </div>
              <div v-if="item.relogin_last_error" class="flex items-start justify-between gap-4">
                <dt class="shrink-0 text-muted-foreground">最近错误</dt>
                <dd class="break-words text-right text-foreground">{{ item.relogin_last_error }}</dd>
              </div>
            </dl>
          </section>
        </div>
      </template>
    </HoverCard>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button, HoverCard } from 'nanocat-ui'

import MetaChip from '@/components/ai/MetaChip.vue'
import type { Account, AccountPresentationTone } from '@/api/accounts'
import { formatAccountDate } from './viewUtils'
import './accountCells.css'

const props = defineProps<{
  item: Account
}>()

const emit = defineEmits<{
  (e: 'copy-credential', kind: 'access' | 'refresh'): void
}>()

type ChipTone = 'default' | 'success' | 'warning' | 'danger' | 'info'

function chipTone(tone: AccountPresentationTone | undefined): ChipTone {
  if (tone === 'error') return 'danger'
  if (tone === 'success' || tone === 'warning' || tone === 'info') return tone
  return 'default'
}

const refreshTokenMissing = computed(() => props.item.refresh_token_status === 'missing' || !props.item.has_refresh_token)
// RT 缺失原本是灰色，容易被忽略：救不回来时标红，只能靠 Outlook/密码时标黄
const refreshChipTone = computed<ChipTone>(() => {
  if (props.item.refresh_token_status !== 'missing') return chipTone(props.item.refresh_token_tone)
  if (props.item.rescue_code === 'none' || props.item.rescue_code === 'exhausted') return 'danger'
  return 'warning'
})
const rescueClass = computed(() => {
  const tone = props.item.rescue_tone
  if (tone === 'error') return 'is-error'
  if (tone === 'warning') return 'is-warning'
  return ''
})
</script>
