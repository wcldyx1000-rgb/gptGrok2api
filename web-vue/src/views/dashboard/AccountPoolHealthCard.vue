<template>
  <section v-if="pool" class="pool-health" :class="`is-${pool.level}`">
    <header class="pool-health-head">
      <div class="flex items-center gap-2">
        <span class="pool-health-dot" aria-hidden="true" />
        <p class="text-sm font-semibold text-foreground">GPT 号池健康度：{{ pool.label }}</p>
      </div>
      <p class="text-xs text-muted-foreground">
        可用 {{ pool.metrics.ready_accounts }}/{{ pool.metrics.total_accounts }}
        · 近 1 小时需求 {{ pool.metrics.demand_1h }} / 承载 {{ pool.metrics.capacity_1h }}
        <template v-if="pool.metrics.utilization !== null"> · 利用率 {{ Math.round(pool.metrics.utilization * 100) }}%</template>
        <template v-if="counts.danger || counts.warning"> · 危险 {{ counts.danger || 0 }} · 注意 {{ counts.warning || 0 }}</template>
      </p>
    </header>
    <ul v-if="pool.warnings.length" class="pool-health-list">
      <li v-for="warning in pool.warnings" :key="warning.code" :class="`is-${warning.tone}`">{{ warning.message }}</li>
    </ul>
    <p v-else class="text-xs text-muted-foreground">号池运行正常，暂无预警。统计按进程内近 1 小时用量计算，重启后重新累计。</p>
    <RouterLink
      v-if="counts.danger || counts.warning"
      to="/accounts"
      class="mt-2 inline-block text-xs font-medium text-primary hover:underline"
    >
      去账号页按“有风险”筛选查看
    </RouterLink>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { accountsApi, type AccountPoolHealth, type AccountPoolHealthResponse } from '@/api/accounts'

const REFRESH_INTERVAL_MS = 60_000

const pool = ref<AccountPoolHealth | null>(null)
const counts = ref<AccountPoolHealthResponse['counts']>({})
let timer: number | undefined

async function load() {
  try {
    const response = await accountsApi.poolHealth()
    pool.value = response.pool && 'level' in response.pool ? response.pool as AccountPoolHealth : null
    counts.value = response.counts || {}
  } catch {
    // Health is advisory; the dashboard keeps working without it.
  }
}

onMounted(() => {
  void load()
  // 页面隐藏时不刷新，避免后台标签页持续请求。
  timer = window.setInterval(() => {
    if (!document.hidden) void load()
  }, REFRESH_INTERVAL_MS)
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<style scoped>
.pool-health {
  border: 1px solid hsl(var(--border));
  border-radius: 12px;
  background: hsl(var(--card));
  padding: 14px 16px;
}
.pool-health-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px 16px;
  margin-bottom: 8px;
}
.pool-health-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: rgb(16 185 129);
}
.pool-health.is-warning .pool-health-dot { background: rgb(245 158 11); }
.pool-health.is-danger .pool-health-dot { background: rgb(244 63 94); }
.pool-health.is-warning { border-color: rgb(245 158 11 / 0.35); }
.pool-health.is-danger { border-color: rgb(244 63 94 / 0.4); }
.pool-health-list {
  display: grid;
  gap: 4px;
  font-size: 12px;
}
.pool-health-list li {
  padding-left: 12px;
  position: relative;
  color: hsl(var(--foreground));
}
.pool-health-list li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0.55em;
  width: 5px;
  height: 5px;
  border-radius: 999px;
  background: rgb(245 158 11);
}
.pool-health-list li.is-error::before { background: rgb(244 63 94); }
</style>
