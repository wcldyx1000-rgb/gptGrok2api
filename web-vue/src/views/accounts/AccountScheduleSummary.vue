<template>
  <div class="schedule-summary" role="group" aria-label="按调度状态筛选">
    <span class="schedule-summary-title">调度</span>
    <button
      type="button"
      class="schedule-chip is-all"
      :class="{ 'is-active': !activeCode }"
      @click="emit('select', 'all')"
    >
      全部 <b>{{ total }}</b>
    </button>
    <button
      v-for="item in chips"
      :key="item.code"
      type="button"
      class="schedule-chip"
      :class="[`is-${item.code}`, { 'is-active': activeCode === item.code }]"
      :title="item.hint"
      @click="emit('select', activeCode === item.code ? 'all' : `sched:${item.code}`)"
    >
      <span class="schedule-chip-dot" aria-hidden="true" />
      {{ item.label }} <b>{{ item.count }}</b>
    </button>
  </div>
</template>

<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<script setup lang="ts">
import { computed } from 'vue'

import { scheduleCodeLabels } from './viewUtils'

const props = defineProps<{ summary: Record<string, number>, filter: string }>()
const emit = defineEmits<{ (e: 'select', value: string): void }>()

// 固定顺序：先看能接请求的，再看各种暂停原因；数量为 0 的不显示（「可接请求」始终显示）
const ORDER = [
  'ready', 'busy', 'cooldown', 'ramp', 'ramp_full', 'cooling', 'auto_paused',
  'auth_pending', 'no_credential', 'egress_switching', 'egress_down',
]
const HINTS: Record<string, string> = {
  ready: '生图和对话都会正常分到这些号',
  busy: '生图和对话并发都已占满，等当前请求结束',
  cooldown: '新号冷静期：不接生图，也不接对话',
  ramp: '放量期：每天限量出图（含今日已满的）',
  ramp_full: '放量期今天的出图上限已用完，北京时间 0 点恢复',
  cooling: '连续失败后暂时降低优先级',
  auto_paused: '反复连续失败，自动暂停一段时间（仍可兜底）',
  auth_pending: '上游返回 401，核验前暂不接请求',
  no_credential: 'AT 已失效且没有可用 RT，不参与调度',
  egress_switching: '换出口前的停用期，不接任何请求',
  egress_down: '绑定的槽位没有选节点，不接请求',
}

const total = computed(() => Object.values(props.summary).reduce((sum, value) => sum + Number(value || 0), 0))
const activeCode = computed(() => (props.filter.startsWith('sched:') ? props.filter.slice(6) : ''))
const chips = computed(() => ORDER
  .map(code => ({
    code,
    label: scheduleCodeLabels[code],
    // 「放量期」的数字把今日已满的也算上，和筛选结果一致
    count: code === 'ramp' ? Number(props.summary.ramp || 0) + Number(props.summary.ramp_full || 0) : Number(props.summary[code] || 0),
    hint: HINTS[code],
  }))
  .filter(item => item.code === 'ready' || item.count > 0))
</script>

<style scoped>
.schedule-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
}

.schedule-summary-title {
  margin-right: 0.15rem;
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}

.schedule-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.2rem 0.65rem;
  border: 1px solid hsl(var(--border));
  border-radius: 999px;
  background: hsl(var(--card));
  color: hsl(var(--foreground));
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.schedule-chip b {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.schedule-chip:hover {
  border-color: hsl(var(--foreground) / 0.35);
}

.schedule-chip.is-active {
  border-color: var(--chip-color, hsl(var(--foreground)));
  background: var(--chip-bg, hsl(var(--muted)));
}

.schedule-chip-dot {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: var(--chip-color);
}

.is-ready { --chip-color: rgb(16 185 129); --chip-bg: rgb(16 185 129 / 0.1); }
.is-busy { --chip-color: rgb(14 165 233); --chip-bg: rgb(14 165 233 / 0.1); }
.is-cooldown { --chip-color: rgb(59 130 246); --chip-bg: rgb(59 130 246 / 0.1); }
.is-ramp { --chip-color: rgb(139 92 246); --chip-bg: rgb(139 92 246 / 0.1); }
.is-ramp_full,
.is-cooling { --chip-color: rgb(245 158 11); --chip-bg: rgb(245 158 11 / 0.12); }
.is-auto_paused { --chip-color: rgb(234 88 12); --chip-bg: rgb(234 88 12 / 0.12); }
.is-auth_pending { --chip-color: rgb(14 165 233); --chip-bg: rgb(14 165 233 / 0.1); }
.is-egress_switching { --chip-color: rgb(59 130 246); --chip-bg: rgb(59 130 246 / 0.12); }
.is-no_credential,
.is-egress_down { --chip-color: rgb(244 63 94); --chip-bg: rgb(244 63 94 / 0.12); }
</style>
