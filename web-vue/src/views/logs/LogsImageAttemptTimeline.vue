<!-- Attempt timeline idea ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<template>
  <section v-if="groups.length" class="attempt-timeline">
    <button
      type="button"
      class="attempt-timeline__header"
      :aria-expanded="visible"
      @click="visible = !visible"
    >
      <span class="attempt-timeline__title">生成尝试</span>
      <span class="attempt-timeline__summary">
        <MetaChip size="xs" tone="muted">{{ totalAttempts }} 次尝试</MetaChip>
        <MetaChip v-if="totalSwitches" size="xs" tone="warning">换号 {{ totalSwitches }} 次</MetaChip>
        <strong>{{ visible ? '收起' : '展开' }}</strong>
      </span>
    </button>

    <div v-show="visible" class="attempt-timeline__list">
      <section v-for="group in groups" :key="group.slot" class="attempt-timeline__group">
        <div v-if="groups.length > 1" class="attempt-timeline__group-header">
          <strong>{{ group.label }}</strong>
          <span>
            <StateBadge :tone="group.tone" size="xs" shape="rounded" :bordered="false">{{ group.statusText }}</StateBadge>
            {{ group.attemptText }}<template v-if="group.switchText"> · {{ group.switchText }}</template>
          </span>
        </div>
        <ol class="attempt-timeline__items">
          <li v-for="row in group.attempts" :key="row.key" class="attempt-timeline__item">
            <span class="attempt-timeline__marker" :class="`attempt-timeline__marker--${row.tone}`" />
            <div class="attempt-timeline__content">
              <div class="attempt-timeline__row">
                <strong>尝试 {{ row.attempt }}</strong>
                <StateBadge :tone="row.tone" size="xs" shape="rounded" :bordered="false">{{ row.statusText }}</StateBadge>
                <span v-if="row.switchText" class="attempt-timeline__switch">{{ row.switchText }}</span>
                <span class="attempt-timeline__duration">{{ row.durationText || '-' }}</span>
              </div>
              <div class="attempt-timeline__facts">
                <span :title="row.account">账号 {{ row.account || '未记录' }}</span>
                <span v-if="row.egressText" :title="row.egressKey">出口 {{ row.egressText }}</span>
                <span v-if="row.failureText" class="attempt-timeline__failure">
                  {{ row.failureText }}<code v-if="row.failureText !== row.failureCode && row.failureCode">{{ row.failureCode }}</code>
                  <code v-if="row.statusCode">HTTP {{ row.statusCode }}</code>
                </span>
              </div>
            </div>
          </li>
        </ol>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import MetaChip from '@/components/ai/MetaChip.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import type { LogImageAttempt } from '@/api/logs'
import { buildAttemptGroups } from '@/views/logs/logAttemptView'

const props = defineProps<{
  attempts: readonly LogImageAttempt[]
  /** 失败或发生过换号时默认展开 */
  defaultOpen?: boolean
}>()

const groups = computed(() => buildAttemptGroups(props.attempts))
const totalAttempts = computed(() => groups.value.reduce((total, group) => total + group.attempts.length, 0))
const totalSwitches = computed(() => props.attempts.filter((attempt) => attempt.switchedAccount).length)
const visible = ref(Boolean(props.defaultOpen) || props.attempts.some((attempt) => attempt.status !== 'success'))
</script>

<style scoped>
.attempt-timeline {
  border: 1px solid hsl(var(--border));
  border-radius: 12px;
  background: hsl(var(--card));
  padding: 12px;
}

.attempt-timeline__header {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  text-align: left;
}

.attempt-timeline__title {
  font-size: 12px;
  font-weight: 600;
  color: hsl(var(--foreground));
}

.attempt-timeline__summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: hsl(var(--muted-foreground));
}

.attempt-timeline__list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}

.attempt-timeline__group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}

.attempt-timeline__items {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.attempt-timeline__item {
  display: flex;
  gap: 10px;
}

.attempt-timeline__marker {
  flex: none;
  width: 8px;
  height: 8px;
  margin-top: 6px;
  border-radius: 999px;
  background: hsl(var(--muted-foreground));
}

.attempt-timeline__marker--success {
  background: hsl(142 70% 40%);
}

.attempt-timeline__marker--danger {
  background: hsl(0 72% 51%);
}

.attempt-timeline__marker--warning {
  background: hsl(38 92% 50%);
}

.attempt-timeline__content {
  min-width: 0;
  flex: 1;
}

.attempt-timeline__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.attempt-timeline__switch {
  color: hsl(38 92% 40%);
  font-size: 11px;
}

.attempt-timeline__duration {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  color: hsl(var(--muted-foreground));
}

.attempt-timeline__facts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 2px;
  font-size: 11px;
  color: hsl(var(--muted-foreground));
  overflow-wrap: anywhere;
}

.attempt-timeline__failure {
  color: hsl(0 72% 45%);
}

.attempt-timeline__failure code {
  margin-left: 4px;
  font-size: 10px;
}
</style>
