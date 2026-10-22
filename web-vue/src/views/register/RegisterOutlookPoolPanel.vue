<template>
  <FormSection title="Outlook 邮箱池" subtitle="逐邮箱状态、别名展开情况、搜索筛选、重置与删除" density="roomy">
    <template #actions>
      <MetaChip size="xs" tone="success">可用 {{ summary.available }}</MetaChip>
      <MetaChip size="xs" tone="muted">租用 {{ summary.inUse }}</MetaChip>
      <MetaChip size="xs" tone="muted">已用 {{ summary.used }}</MetaChip>
      <MetaChip size="xs" :tone="summary.retryable ? 'warning' : 'muted'">失败 {{ summary.retryable }}</MetaChip>
      <MetaChip size="xs" :tone="summary.invalid ? 'danger' : 'muted'">异常 {{ summary.invalid }}</MetaChip>
      <Button size="sm" variant="outline" :aria-expanded="open" @click="toggleOpen">
        {{ open ? '收起' : '展开管理' }}
      </Button>
    </template>

    <p v-if="!open" class="outlook-pool-hint">{{ hint }}</p>

    <div v-else class="outlook-pool">
      <div class="outlook-pool__toolbar">
        <Input
          :model-value="mailboxes.query.value"
          block
          root-class="outlook-pool__search"
          placeholder="搜索邮箱地址"
          @update:model-value="value => mailboxes.setQuery(String(value || ''))"
        />
        <Button size="sm" variant="outline" :disabled="mailboxes.loading.value" @click="mailboxes.load()">
          <Icon icon="lucide:refresh-cw" class="h-3.5 w-3.5" />
          刷新
        </Button>
        <Button size="sm" variant="outline" :disabled="disabled" @click="showImport = !showImport">
          <Icon icon="lucide:upload" class="h-3.5 w-3.5" />
          批量导入
        </Button>
      </div>

      <div class="outlook-pool__filters" role="tablist" aria-label="状态筛选">
        <button
          v-for="item in filters"
          :key="item.key"
          type="button"
          class="outlook-pool__filter"
          :class="{ 'is-active': mailboxes.stateFilter.value === item.key }"
          @click="mailboxes.setState(item.key)"
        >
          {{ item.label }}
          <span class="outlook-pool__filter-count">{{ item.count }}</span>
        </button>
      </div>

      <div v-if="showImport" class="outlook-pool__import">
        <textarea
          v-model="importText"
          class="outlook-pool__textarea"
          :disabled="disabled"
          placeholder="每行一个：邮箱----密码----client_id----refresh_token"
        ></textarea>
        <div class="outlook-pool__import-actions">
          <span class="outlook-pool__muted">导入内容与下方「邮箱池导入」共用同一份草稿，随配置自动保存后进入邮箱池；同名邮箱按新材料覆盖。</span>
          <Button size="sm" :disabled="disabled || !importText.trim()" @click="submitImport">加入导入草稿</Button>
        </div>
      </div>

      <p v-if="mailboxes.errorMessage.value" class="outlook-pool__error">{{ mailboxes.errorMessage.value }}</p>

      <div class="outlook-pool__body scrollbar-slim">
        <table class="outlook-pool__table">
          <thead>
            <tr>
              <th>邮箱</th>
              <th>状态</th>
              <th>别名来源</th>
              <th>最近错误</th>
              <th>租约时间</th>
              <th class="is-num">使用次数</th>
              <th>凭据</th>
              <th class="is-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!mailboxes.items.value.length">
              <td colspan="8" class="outlook-pool__empty">
                {{ mailboxes.loading.value ? '正在加载...' : (mailboxes.loaded.value ? '没有符合条件的邮箱' : '') }}
              </td>
            </tr>
            <tr v-for="entry in mailboxes.items.value" :key="entry.id">
              <td class="is-mono" :title="entry.email">
                {{ entry.email }}
                <span v-if="entry.is_alias" class="outlook-pool__tag">别名</span>
              </td>
              <td>
                <StateBadge :tone="outlookStateTone(entry.state)" size="xs" shape="rounded" :bordered="false">
                  {{ outlookStateLabel(entry.state) }}
                </StateBadge>
                <span v-if="entry.inherited" class="outlook-pool__muted" title="状态继承自原邮箱"> 继承</span>
                <span v-if="entry.cooling" class="outlook-pool__muted" title="瞬时失败后短暂冷却中"> 冷却</span>
              </td>
              <td class="is-mono">{{ entry.alias_of || '—' }}</td>
              <td class="outlook-pool__reason" :title="entry.reason">{{ entry.reason || '—' }}</td>
              <td>
                <template v-if="entry.leased_at">
                  {{ formatOutlookTime(entry.leased_at) }}
                  <span v-if="entry.lease_stale" class="outlook-pool__muted"> 已过期</span>
                </template>
                <template v-else>—</template>
              </td>
              <td class="is-num">{{ entry.use_count }}</td>
              <td>
                <span class="outlook-pool__cred" :class="{ 'is-ok': entry.has_password }" title="密码">密</span>
                <span class="outlook-pool__cred" :class="{ 'is-ok': entry.has_client_id }" title="client_id">ID</span>
                <span class="outlook-pool__cred" :class="{ 'is-ok': entry.has_refresh_token }" title="refresh_token">令</span>
              </td>
              <td class="is-actions">
                <Button
                  size="xs"
                  variant="ghost"
                  :disabled="!entry.resettable || mailboxes.busyId.value === entry.id"
                  :title="entry.resettable ? '重置为可用' : (entry.state === 'leased' ? '租约中，不能重置' : '当前状态无需重置')"
                  @click="mailboxes.resetMailbox(entry)"
                >
                  重置
                </Button>
                <Button
                  v-if="entry.deletable"
                  size="xs"
                  variant="ghost"
                  :disabled="disabled || mailboxes.busyId.value === entry.id"
                  title="从邮箱池删除"
                  @click="mailboxes.deleteMailbox(entry)"
                >
                  删除
                </Button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="outlook-pool__pager">
        <span class="outlook-pool__muted">共 {{ mailboxes.total.value }} 个地址（含别名）</span>
        <Button size="xs" variant="outline" :disabled="mailboxes.page.value <= 1" @click="mailboxes.setPage(mailboxes.page.value - 1)">上一页</Button>
        <span class="outlook-pool__muted">{{ mailboxes.page.value }} / {{ mailboxes.pages.value }}</span>
        <Button size="xs" variant="outline" :disabled="mailboxes.page.value >= mailboxes.pages.value" @click="mailboxes.setPage(mailboxes.page.value + 1)">下一页</Button>
      </div>
      <p class="outlook-pool__muted">{{ hint }}</p>
    </div>
  </FormSection>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Icon } from '@iconify/vue'
import { Button, Input } from 'nanocat-ui'
import type { RegisterProvider } from '@/api/register'
import FormSection from '@/components/ai/FormSection.vue'
import MetaChip from '@/components/ai/MetaChip.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import { outlookPoolHint, outlookPoolSummary } from '@/views/register/registerProviderView'
import {
  formatOutlookTime,
  OUTLOOK_STATE_FILTERS,
  outlookStateLabel,
  outlookStateTone,
  useRegisterOutlookMailboxes,
} from '@/views/register/registerOutlookMailboxRuntime'

const props = defineProps<{
  provider: RegisterProvider
  disabled?: boolean
  confirm: (options: { title?: string; message: string; confirmText?: string; cancelText?: string }) => Promise<boolean>
  notifySuccess: (message: string) => void
  notifyError: (message: string) => void
}>()

const emit = defineEmits<{
  (e: 'import', text: string): void
  (e: 'changed'): void
}>()

const open = ref(false)
const showImport = ref(false)
const importText = ref('')
const providerId = computed(() => String(props.provider.id || props.provider.provider_id || '').trim())

const mailboxes = useRegisterOutlookMailboxes({
  providerId,
  confirm: props.confirm,
  notifySuccess: props.notifySuccess,
  notifyError: props.notifyError,
  onChanged: () => emit('changed'),
})

const summary = computed(() => outlookPoolSummary(props.provider))
const hint = computed(() => outlookPoolHint(props.provider))
const filters = computed(() => OUTLOOK_STATE_FILTERS.map(item => ({
  ...item,
  count: item.key === 'all'
    ? Object.values(mailboxes.stateCounts.value).reduce((sum, value) => sum + value, 0)
    : mailboxes.stateCounts.value[item.key] || 0,
})))

function toggleOpen() {
  open.value = !open.value
  if (open.value) void mailboxes.load()
}

function submitImport() {
  const text = importText.value.trim()
  if (!text) return
  emit('import', text)
  importText.value = ''
  showImport.value = false
}

// 配置保存/统计变化（导入落库、任务释放或占用邮箱）后，已展开的表格跟着刷新。
let refreshTimer: ReturnType<typeof setTimeout> | undefined
watch(
  [() => props.provider.mailboxes_count, () => JSON.stringify(props.provider.mailboxes_stats || {})],
  () => {
    if (!open.value) return
    if (refreshTimer !== undefined) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(() => {
      refreshTimer = undefined
      void mailboxes.load()
    }, 400)
  },
)
</script>

<style scoped>
.outlook-pool {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.outlook-pool-hint,
.outlook-pool__muted {
  color: hsl(var(--muted-foreground));
  font-size: 12px;
  line-height: 1.5;
}

.outlook-pool__toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.outlook-pool__toolbar :deep(.outlook-pool__search) {
  min-width: 12rem;
  flex: 1;
}

.outlook-pool__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.outlook-pool__filter {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid hsl(var(--border));
  border-radius: 999px;
  background: hsl(var(--background));
  padding: 3px 10px;
  color: hsl(var(--muted-foreground));
  font-size: 12px;
  cursor: pointer;
}

.outlook-pool__filter.is-active {
  border-color: hsl(var(--primary));
  color: hsl(var(--foreground));
  background: hsl(var(--muted) / 0.5);
}

.outlook-pool__filter-count {
  font-variant-numeric: tabular-nums;
  opacity: 0.8;
}

.outlook-pool__import {
  display: grid;
  gap: 8px;
}

.outlook-pool__import-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.outlook-pool__textarea {
  min-height: 8rem;
  width: 100%;
  resize: vertical;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--background));
  padding: 8px 10px;
  color: hsl(var(--foreground));
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.outlook-pool__error {
  color: hsl(var(--destructive));
  font-size: 12px;
}

.outlook-pool__body {
  max-height: 28rem;
  overflow: auto;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--background));
}

.outlook-pool__table {
  width: 100%;
  min-width: 52rem;
  border-collapse: collapse;
  font-size: 12px;
}

.outlook-pool__table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: hsl(var(--muted) / 0.35);
  padding: 8px 10px;
  color: hsl(var(--muted-foreground));
  font-weight: 500;
  text-align: left;
  white-space: nowrap;
}

.outlook-pool__table td {
  border-top: 1px solid hsl(var(--border));
  padding: 8px 10px;
  vertical-align: middle;
}

.outlook-pool__table .is-mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.outlook-pool__table .is-num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.outlook-pool__table .is-actions {
  white-space: nowrap;
  text-align: right;
}

.outlook-pool__reason {
  max-width: 16rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.outlook-pool__empty {
  padding: 18px 10px !important;
  color: hsl(var(--muted-foreground));
  text-align: center;
}

.outlook-pool__tag {
  margin-left: 6px;
  border-radius: 4px;
  background: hsl(var(--muted));
  padding: 0 5px;
  color: hsl(var(--muted-foreground));
  font-family: inherit;
  font-size: 11px;
}

.outlook-pool__cred {
  display: inline-block;
  margin-right: 4px;
  border-radius: 4px;
  background: hsl(var(--muted) / 0.5);
  padding: 0 5px;
  color: hsl(var(--muted-foreground));
  font-size: 11px;
  text-decoration: line-through;
}

.outlook-pool__cred.is-ok {
  color: hsl(var(--foreground));
  text-decoration: none;
}

.outlook-pool__pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.outlook-pool__pager > :first-child {
  margin-right: auto;
}
</style>
