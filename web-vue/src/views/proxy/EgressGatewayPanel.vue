<template>
  <PagePanel class="space-y-4">
    <PanelHeader title="出口槽位（mihomo）">
      <template #copy>
        <p class="mt-1 text-xs text-muted-foreground">
          可选功能，默认关闭。通过 mihomo 控制接口管理订阅/住宅代理，把节点固定到槽位（slot-N 对应端口 7000+N），
          只有 OpenAI 相关域名走槽位节点；节点不在时槽位保持拒绝，不会从别的出口出去。
        </p>
      </template>
      <template #actions>
        <Button size="sm" variant="outline" :disabled="loading" @click="load">{{ loading ? '刷新中...' : '刷新' }}</Button>
      </template>
    </PanelHeader>

    <p v-if="error" class="text-xs text-rose-600">{{ error }}</p>

    <FormSection title="网关设置" density="roomy">
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label class="flex items-center gap-2 text-xs md:col-span-2">
          <Checkbox v-model="form.enabled" />
          <span>启用出口槽位（开启后后台每 20 秒核对一次 mihomo 配置，并执行到期的账号换出口）</span>
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">控制接口地址</span>
          <Input v-model.trim="form.controller_url" block root-class="font-mono" placeholder="http://mihomo:9090" />
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">控制接口密钥</span>
          <Input
            v-model.trim="form.secret"
            type="password"
            block
            root-class="font-mono"
            :placeholder="snapshot?.settings.secret_set ? '已保存，留空则沿用' : '未设置'"
          />
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">槽位地址模板</span>
          <Input v-model.trim="form.proxy_template" block root-class="font-mono" placeholder="http://mihomo:{port}" />
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">换出口前停用（小时）</span>
          <Input v-model.number="form.switch_hold_hours" type="number" min="0" block placeholder="24" />
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">单个槽位最多绑定账号数</span>
          <Input v-model.number="form.slot_capacity" type="number" min="1" max="100000" block placeholder="1000" />
        </label>
      </div>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="primary" :disabled="busy" @click="saveSettings">保存网关设置</Button>
        <span v-if="snapshot" class="text-xs" :class="snapshot.status.ok ? 'text-emerald-600' : 'text-muted-foreground'">
          {{ snapshot.status.ok ? `已连接 ${snapshot.status.version || ''}` : (snapshot.status.error || '未连接') }}
        </span>
      </div>
    </FormSection>

    <template v-if="snapshot?.settings.enabled">
      <FormSection title="来源" density="roomy">
        <div class="space-y-2 text-xs">
          <div v-for="source in snapshot.sources" :key="source.id" class="flex flex-wrap items-center gap-2">
            <span class="font-medium">{{ source.name }}</span>
            <span class="text-muted-foreground">
              {{ source.kind === 'subscription' ? `订阅 ${source.url}` : `手动 ${source.line_count} 条` }} · {{ source.node_count }} 个节点
            </span>
            <Button v-if="source.kind === 'subscription'" size="sm" variant="ghost" :disabled="busy" @click="run(() => egressApi.refreshSource(source.id))">刷新</Button>
            <Button size="sm" variant="ghost" :disabled="busy" @click="run(() => egressApi.removeSource(source.id))">删除</Button>
          </div>
          <p v-if="!snapshot.sources.length" class="text-muted-foreground">还没有来源。</p>
        </div>
        <div class="mt-3 grid grid-cols-1 gap-2 md:grid-cols-[8rem_10rem_minmax(0,1fr)_auto]">
          <GroupedSelectMenu v-model="sourceKind" :options="sourceKindOptions" selected-indicator="none" block />
          <Input v-model.trim="sourceName" block placeholder="名称" />
          <Input
            v-if="sourceKind === 'subscription'"
            v-model.trim="sourceUrl"
            block
            root-class="font-mono"
            placeholder="https://订阅链接"
          />
          <textarea
            v-else
            v-model="sourceText"
            rows="3"
            class="ui-textarea-sm font-mono"
            placeholder="每行一个代理，如 http://user:pass@1.2.3.4:8000"
          ></textarea>
          <Button size="sm" variant="outline" :disabled="busy || !sourceName" @click="addSource">添加来源</Button>
        </div>
      </FormSection>

      <FormSection title="槽位" density="roomy">
        <template #actions>
          <Input v-model.number="slotCount" type="number" min="1" :max="snapshot.settings.max_slots" root-class="w-20" />
          <Button size="sm" variant="outline" :disabled="busy" @click="run(() => egressApi.setSlotCount(Number(slotCount) || 1))">调整数量</Button>
          <Button size="sm" variant="outline" :disabled="busy || snapshot.job.running" @click="startChecks">
            {{ snapshot.job.running ? `检测中 ${snapshot.job.done}/${snapshot.job.total}` : '检测全部节点' }}
          </Button>
          <Button size="sm" variant="primary" :disabled="busy" @click="useForRegister">用于注册</Button>
        </template>
        <div class="space-y-2 text-xs">
          <template v-for="slot in snapshot.slots" :key="slot.slot">
          <div class="grid grid-cols-1 items-center gap-2 md:grid-cols-[7rem_minmax(0,1fr)_auto]">
            <span class="font-mono">slot-{{ slot.slot }} :{{ slot.port }}</span>
            <GroupedSelectMenu
              :model-value="slot.node || ''"
              :options="nodeOptions"
              selected-indicator="none"
              block
              @update:model-value="assignSlot(slot.slot, String($event))"
            />
            <span :class="slot.active === 'REJECT' ? 'text-rose-600' : 'text-emerald-600'">
              {{ slot.active === 'REJECT' ? '拒绝' : '生效' }}{{ slot.error ? ` · ${slot.error}` : '' }}
            </span>
          </div>
          <div class="grid grid-cols-1 items-center gap-2 pb-2 md:grid-cols-[7rem_minmax(0,1fr)_minmax(0,1fr)_auto]">
            <span class="text-muted-foreground">地区环境</span>
            <Input
              :model-value="localeDraft(slot).timezone"
              block
              root-class="font-mono"
              :list="`slot-tz-${slot.slot}`"
              placeholder="时区，如 Asia/Taipei"
              @update:model-value="setLocaleDraft(slot.slot, { timezone: String($event).trim() })"
            />
            <datalist :id="`slot-tz-${slot.slot}`">
              <option v-for="zone in slot.suggested_locale?.timezone_options || []" :key="zone" :value="zone" />
            </datalist>
            <Input
              :model-value="localeDraft(slot).language"
              block
              root-class="font-mono"
              placeholder="语言，如 zh-TW"
              @update:model-value="setLocaleDraft(slot.slot, { language: String($event).trim() })"
            />
            <div class="flex flex-wrap items-center gap-2">
              <Button
                v-if="slot.suggested_locale"
                size="sm"
                variant="ghost"
                :title="`按出口国家 ${slot.suggested_locale.country} 推荐`"
                @click="applySuggestedLocale(slot)"
              >
                套用推荐（{{ slot.suggested_locale.country }}）
              </Button>
              <Button size="sm" variant="outline" :disabled="busy" @click="saveSlotLocale(slot.slot)">保存地区</Button>
            </div>
          </div>
          </template>
        </div>
      </FormSection>

      <FormSection title="账号出口" density="roomy">
        <div v-if="accounts" class="space-y-1 text-xs text-muted-foreground">
          <p>
            <span v-for="item in accounts.slots" :key="item.slot" class="mr-3">
              slot-{{ item.slot }}：{{ item.accounts }} / {{ item.capacity }} 个<span v-if="item.down" class="text-rose-600">（拒绝中）</span>
            </span>
            <span class="mr-3">直连：{{ accounts.direct_accounts }} 个</span>
            <span>默认出口：{{ accounts.default_accounts }} 个</span>
          </p>
          <p v-for="item in accounts.pending_switches" :key="item.token" class="font-mono">
            {{ item.email || item.token }}：{{ item.from }} → {{ item.to }}，{{ item.due_at }} 生效
          </p>
        </div>
        <div class="mt-3 grid grid-cols-1 gap-2 md:grid-cols-[minmax(0,1fr)_10rem_auto_auto]">
          <textarea
            v-model="switchTokens"
            rows="3"
            class="ui-textarea-sm font-mono"
            placeholder="要换出口的账号 access_token，每行一个"
          ></textarea>
          <GroupedSelectMenu v-model="switchTarget" :options="switchTargetOptions" selected-indicator="none" block />
          <Button size="sm" variant="outline" :disabled="busy || !switchTokenList.length" @click="requestSwitch">申请换出口</Button>
          <Button size="sm" variant="ghost" :disabled="busy || !switchTokenList.length" @click="cancelSwitch">取消申请</Button>
        </div>
        <p class="mt-2 text-xs text-muted-foreground">
          换出口期间账号停用（状态“禁用”），到期后台切换并恢复原状态；停用时长为 0 时立即切换。
        </p>
        <p v-if="switchMessage" class="mt-1 text-xs text-emerald-600">{{ switchMessage }}</p>
      </FormSection>
    </template>

    <FormSection title="免费公开代理（仅注册）" density="roomy">
      <p class="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
        风险提示：免费代理由陌生人运营，可能记录或篡改经过的流量（包括邮箱验证码和账号凭据），并且很快失效。
        抓到的代理需先通过出口检测才会写入“免费公开代理”代理组；只有在注册设置里手动选择该代理组才会使用，请勿用于对话/生图出口。
      </p>
      <div v-if="freeProxy" class="grid grid-cols-1 gap-3 md:grid-cols-3">
        <label class="flex items-center gap-2 text-xs md:col-span-3">
          <Checkbox v-model="freeForm.enabled" />
          <span>启用定时拉取</span>
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">协议</span>
          <GroupedSelectMenu v-model="freeForm.protocol" :options="protocolOptions" selected-indicator="none" block />
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">拉取间隔（分钟）</span>
          <Input v-model.number="freeForm.interval_minutes" type="number" min="10" block />
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">最多保留节点</span>
          <Input v-model.number="freeForm.max_nodes" type="number" min="1" block />
        </label>
        <label class="flex items-center gap-2 text-xs md:col-span-3">
          <Checkbox v-model="freeForm.allow_datacenter" />
          <span>允许机房 IP（关闭后只保留非机房出口）</span>
        </label>
        <div class="flex flex-wrap items-center gap-2 md:col-span-3">
          <Button size="sm" variant="primary" :disabled="busy" @click="saveFreeProxy">保存</Button>
          <Button size="sm" variant="outline" :disabled="busy || freeProxy.running" @click="runFreeProxy">
            {{ freeProxy.running ? '拉取中...' : '立即拉取' }}
          </Button>
          <span class="text-xs text-muted-foreground">
            {{ freeProxy.last_message || '尚未运行' }}<span v-if="freeProxy.last_error" class="text-rose-600"> · {{ freeProxy.last_error }}</span>
          </span>
        </div>
      </div>
    </FormSection>
  </PagePanel>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Checkbox, Input } from 'nanocat-ui'
import FormSection from '@/components/ai/FormSection.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import PanelHeader from '@/components/ai/PanelHeader.vue'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import { egressApi, type EgressSlot, type EgressSlotsSnapshot, type FreeProxyStatus } from '@/api/egress'
import apiClient from '@/api/client'

interface AccountsOverview {
  slots: { slot: number; accounts: number; down: boolean; capacity: number }[]
  direct_accounts: number
  default_accounts: number
  pending_switches: { email: string; token: string; from: string; to: string; due_at: string }[]
}

const sourceKindOptions = [
  { value: 'subscription', label: '订阅' },
  { value: 'manual', label: '手动代理' },
]
const protocolOptions = [
  { value: 'http', label: 'HTTP' },
  { value: 'socks5', label: 'SOCKS5' },
]
const recommendationLabels: Record<string, string> = {
  register: '适合注册',
  daily: '日常可用',
  unchecked: '未检测',
  unusable: '不可用',
}

const loading = ref(false)
const busy = ref(false)
const error = ref('')
const snapshot = ref<EgressSlotsSnapshot | null>(null)
const accounts = ref<AccountsOverview | null>(null)
const freeProxy = ref<FreeProxyStatus | null>(null)
const form = reactive({ enabled: false, controller_url: '', proxy_template: '', secret: '', switch_hold_hours: 24, slot_capacity: 1000 })
const localeDrafts = reactive<Record<number, { timezone?: string; language?: string }>>({})
const freeForm = reactive({ enabled: false, protocol: 'http', interval_minutes: 60, max_nodes: 20, allow_datacenter: true })
const slotCount = ref(4)
const sourceKind = ref('subscription')
const sourceName = ref('')
const sourceUrl = ref('')
const sourceText = ref('')
const switchTokens = ref('')
const switchTarget = ref('direct')
const switchMessage = ref('')

const nodeOptions = computed(() => [
  { value: '', label: '（不分配，拒绝）' },
  ...(snapshot.value?.nodes || []).map(node => ({
    value: node.name,
    label: `${node.name} · ${recommendationLabels[node.recommendation] || node.recommendation}${node.check?.country ? ` · ${node.check.country}` : ''}`,
  })),
])
const switchTargetOptions = computed(() => [
  ...(snapshot.value?.slots || []).map(slot => ({ value: `slot:${slot.slot}`, label: `slot-${slot.slot}` })),
  { value: 'direct', label: '直连' },
  { value: 'default', label: '默认出口' },
])
const switchTokenList = computed(() => switchTokens.value.split(/\s+/).map(item => item.trim()).filter(Boolean))

function applySnapshot(next: EgressSlotsSnapshot) {
  snapshot.value = next
  form.enabled = next.settings.enabled
  form.controller_url = next.settings.controller_url
  form.proxy_template = next.settings.proxy_template
  form.switch_hold_hours = next.settings.switch_hold_hours
  form.slot_capacity = next.settings.slot_capacity
  for (const key of Object.keys(localeDrafts)) delete localeDrafts[Number(key)]
  form.secret = ''
  slotCount.value = next.settings.slot_count
}

function localeDraft(slot: EgressSlot) {
  const draft = localeDrafts[slot.slot] || {}
  return {
    timezone: draft.timezone ?? slot.locale?.timezone ?? '',
    language: draft.language ?? slot.locale?.language ?? '',
  }
}

function setLocaleDraft(slot: number, patch: { timezone?: string; language?: string }) {
  localeDrafts[slot] = { ...localeDrafts[slot], ...patch }
}

function applySuggestedLocale(slot: EgressSlot) {
  const suggested = slot.suggested_locale
  if (!suggested) return
  // 美国等多时区国家只推荐语言，时区需要从候选里手动选一个。
  setLocaleDraft(slot.slot, {
    timezone: suggested.timezone || localeDraft(slot).timezone,
    language: suggested.language,
  })
}

function saveSlotLocale(slotNumber: number) {
  const slot = snapshot.value?.slots.find(item => item.slot === slotNumber)
  if (!slot) return
  const draft = localeDraft(slot)
  return run(() => egressApi.setSlotLocale(slotNumber, draft))
}

function applyFreeProxy(next: FreeProxyStatus) {
  freeProxy.value = next
  freeForm.enabled = next.enabled
  freeForm.protocol = next.protocol
  freeForm.interval_minutes = next.interval_minutes
  freeForm.max_nodes = next.max_nodes
  freeForm.allow_datacenter = next.allow_datacenter
}

function errorText(err: unknown) {
  return err instanceof Error ? err.message : '操作失败'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    applySnapshot(await egressApi.slots())
    applyFreeProxy(await egressApi.freeProxy())
    accounts.value = snapshot.value?.settings.enabled
      ? await apiClient.get<never, AccountsOverview>('/api/egress/accounts')
      : null
  } catch (err) {
    error.value = errorText(err)
  } finally {
    loading.value = false
  }
}

async function run(action: () => Promise<EgressSlotsSnapshot>) {
  busy.value = true
  error.value = ''
  try {
    applySnapshot(await action())
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

function saveSettings() {
  return run(() => egressApi.saveSlotSettings({ ...form, secret: form.secret || undefined }))
}

async function addSource() {
  await run(() => egressApi.addSource({
    kind: sourceKind.value as 'subscription' | 'manual',
    name: sourceName.value,
    url: sourceUrl.value,
    text: sourceText.value,
  }))
  if (!error.value) {
    sourceName.value = ''
    sourceUrl.value = ''
    sourceText.value = ''
  }
}

function assignSlot(slot: number, node: string) {
  return run(() => egressApi.assignSlot(slot, node || null))
}

async function startChecks() {
  busy.value = true
  try {
    await egressApi.startChecks()
    await load()
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

function useForRegister() {
  return run(() => egressApi.useForRegister())
}

async function requestSwitch() {
  busy.value = true
  switchMessage.value = ''
  try {
    const result = await apiClient.post<unknown, { scheduled: string[]; applied: string[]; skipped: unknown[] }>(
      '/api/egress/accounts/switch',
      { access_tokens: switchTokenList.value, target: switchTarget.value },
    )
    switchMessage.value = `已安排 ${result.scheduled.length} 个，立即切换 ${result.applied.length} 个，跳过 ${result.skipped.length} 个`
    await load()
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

async function cancelSwitch() {
  busy.value = true
  switchMessage.value = ''
  try {
    const result = await apiClient.post<unknown, { cancelled: string[] }>(
      '/api/egress/accounts/cancel-switch',
      { access_tokens: switchTokenList.value },
    )
    switchMessage.value = `已取消 ${result.cancelled.length} 个换出口申请`
    await load()
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

async function saveFreeProxy() {
  busy.value = true
  try {
    applyFreeProxy(await egressApi.saveFreeProxy({ ...freeForm, protocol: freeForm.protocol as 'http' | 'socks5' }))
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

async function runFreeProxy() {
  busy.value = true
  try {
    const result = await egressApi.runFreeProxy()
    applyFreeProxy(result.free_proxy)
  } catch (err) {
    error.value = errorText(err)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>
