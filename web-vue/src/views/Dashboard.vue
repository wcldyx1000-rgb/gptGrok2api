<template>
  <div class="space-y-5">
    <PageLoadingState
      v-if="!dashboardDataReady && dashboardLoading"
      title="正在加载概览"
      description="读取最新账号、调用趋势和模型统计。"
    />

    <StateBlock
      v-else-if="!dashboardDataReady"
      dashed
      title="概览加载失败"
      :description="dashboardLoadError || '后台暂时繁忙，请稍后重试。'"
    >
      <Button class="mt-4" size="sm" variant="outline" @click="retryDashboard">
        重新加载
      </Button>
    </StateBlock>

    <template v-else>
    <p
      v-if="dashboardStaleWarning"
      class="flex items-center gap-2 rounded-md border border-amber-300/70 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700/70 dark:bg-amber-950/30 dark:text-amber-200"
      role="status"
    >
      {{ dashboardStaleWarning }}
    </p>

    <section class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <StatCard
        v-for="stat in stats"
        :key="stat.label"
        :label="stat.label"
        :value="stat.value"
        :caption="stat.meta"
        :icon="stat.icon"
        :icon-bg="stat.iconBg"
        :icon-color="stat.iconColor"
        panel-class="!rounded-xl"
      />
    </section>

    <section class="space-y-2" aria-label="调用概览">
      <h2 class="px-1 text-sm font-semibold text-foreground">调用概览 <span class="text-xs font-normal text-muted-foreground">近 24 小时</span></h2>
      <div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard
          v-for="stat in callStats"
          :key="stat.label"
          :label="stat.label"
          :value="stat.value"
          :caption="stat.meta"
          :icon="stat.icon"
          :icon-bg="stat.iconBg"
          :icon-color="stat.iconColor"
          panel-class="!rounded-xl"
        />
      </div>
    </section>

    <AccountPoolHealthCard />

    <RuntimeEnvironmentCard />

    <section class="grid grid-cols-1 gap-4">
      <ChartCard
        title="模型请求分布"
        description="口径：仅统计成功及部分成功的请求，失败请求不计入。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeHourlyRequests" aria-label="模型请求分布时间范围" />
        </template>
        <div ref="hourlyRequestsChartRef" class="h-72 w-full px-2"></div>
      </ChartCard>
    </section>

    <section class="grid grid-cols-1 gap-4">
      <ChartCard
        title="调用趋势"
        description="口径：按时间桶统计全部请求，区分成功、失败与限流；虚线为账号切换次数。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeTrend" aria-label="调用趋势时间范围" />
        </template>
        <div ref="trendChartRef" class="h-56 w-full"></div>
      </ChartCard>
    </section>

    <section class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <ChartCard
        title="成功率趋势"
        description="口径：成功 /（成功 + 失败），上游返回文本的请求不计入。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeSuccessRate" aria-label="成功率趋势时间范围" />
        </template>
        <div ref="successRateChartRef" class="h-56 w-full"></div>
      </ChartCard>

      <ChartCard
        title="调用活跃度"
        description="每个格子对应一个时间桶，颜色越深请求越多。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeActivity" aria-label="调用活跃度时间范围" />
        </template>
        <div class="flex h-56 items-center px-1">
          <div
            v-if="activityHasData"
            class="mx-auto flex w-full flex-col gap-4"
            :style="{ maxWidth: activityGridMaxWidth }"
          >
            <div
              :key="`${activityAnimationEpoch}-${timeRangeActivity}`"
              class="grid gap-1.5"
              :style="{ gridTemplateColumns: `repeat(${activityColumnCount}, minmax(0, 1fr))` }"
            >
              <span
                v-for="(bucket, index) in activityBuckets"
                :key="`${activityAnimationEpoch}-${timeRangeActivity}-${bucket.label}-${index}`"
                class="dashboard-activity-cell grid min-w-0"
                :style="{ animationDelay: `${Math.min(index * 2, 32)}ms` }"
              >
                <HoverCard card-class="w-48" :offset="8">
                  <span
                    class="block aspect-square w-full min-w-0 rounded-[3px] border border-transparent transition-[background-color,box-shadow,transform] duration-200 hover:-translate-y-0.5 hover:shadow-sm"
                    :class="activityCellClass(bucket.total_calls)"
                    role="img"
                    :aria-label="activityTooltip(bucket)"
                  ></span>
                  <template #content>
                    <div class="mb-2 text-xs font-semibold text-foreground">{{ bucket.label }}</div>
                    <div class="grid gap-1.5 text-xs">
                      <div class="flex items-center justify-between gap-4">
                        <span class="text-muted-foreground">总请求</span>
                        <span class="font-semibold text-foreground">{{ bucket.total_calls }}</span>
                      </div>
                      <div class="flex items-center justify-between gap-4">
                        <span class="text-muted-foreground">成功</span>
                        <span class="font-semibold text-foreground">{{ bucket.success_calls }}</span>
                      </div>
                      <div class="flex items-center justify-between gap-4">
                        <span class="text-muted-foreground">失败 / 限流</span>
                        <span class="font-semibold text-foreground">{{ bucket.failed_calls }}</span>
                      </div>
                    </div>
                  </template>
                </HoverCard>
              </span>
            </div>
            <div class="flex items-center justify-between text-[11px] text-muted-foreground">
              <span>{{ activityBuckets[0]?.label || '--' }}</span>
              <span class="flex items-center gap-1.5">
                <span>少</span>
                <span class="h-2.5 w-2.5 rounded-[2px] bg-muted/60"></span>
                <span class="h-2.5 w-2.5 rounded-[2px] bg-emerald-100 dark:bg-emerald-950/70"></span>
                <span class="h-2.5 w-2.5 rounded-[2px] bg-emerald-200 dark:bg-emerald-900/70"></span>
                <span class="h-2.5 w-2.5 rounded-[2px] bg-emerald-400 dark:bg-emerald-700"></span>
                <span class="h-2.5 w-2.5 rounded-[2px] bg-emerald-600 dark:bg-emerald-500"></span>
                <span>多</span>
              </span>
              <span>{{ activityBuckets[activityBuckets.length - 1]?.label || '--' }}</span>
            </div>
          </div>
          <p v-else class="w-full text-center text-sm text-muted-foreground" role="status">
            当前范围内暂无可统计请求
          </p>
        </div>
      </ChartCard>

      <ChartCard
        title="成功耗时趋势"
        description="口径：仅统计成功请求的平均耗时，无成功请求的时间桶不绘点。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeSuccessDuration" aria-label="成功耗时趋势时间范围" />
        </template>
        <div ref="successDurationChartRef" class="h-56 w-full"></div>
      </ChartCard>

      <ChartCard
        title="模型耗时"
        description="口径：各模型成功请求的平均耗时。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeResponseTime" aria-label="模型耗时时间范围" />
        </template>
        <div ref="responseTimeChartRef" class="h-56 w-full"></div>
      </ChartCard>
    </section>

    <section class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <ChartCard
        title="模型调用占比"
        description="口径：统计全部请求（含失败）。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeModel" aria-label="模型调用占比时间范围" />
        </template>
        <div ref="modelChartRef" class="h-56 w-full"></div>
      </ChartCard>

      <ChartCard
        title="模型使用排行"
        description="口径：统计全部请求（含失败）。"
        panel-class="!rounded-xl"
      >
        <template #actions>
          <TimeRangeTabs v-model="timeRangeModelRank" aria-label="模型使用排行时间范围" />
        </template>
        <div ref="modelRankChartRef" class="h-56 w-full"></div>
      </ChartCard>
    </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button, ChartCard, HoverCard, StatCard } from 'nanocat-ui'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import TimeRangeTabs from '@/components/ai/TimeRangeTabs.vue'
import AccountPoolHealthCard from './dashboard/AccountPoolHealthCard.vue'
import RuntimeEnvironmentCard from './dashboard/RuntimeEnvironmentCard.vue'
import { useDashboardPage } from './dashboard/useDashboardPage'

defineOptions({ name: 'Dashboard' })

const {
  stats,
  callStats,
  dashboardDataReady,
  dashboardLoading,
  dashboardLoadError,
  dashboardStaleWarning,
  retryDashboard,
  timeRangeHourlyRequests,
  timeRangeTrend,
  timeRangeSuccessRate,
  timeRangeModel,
  timeRangeModelRank,
  timeRangeResponseTime,
  timeRangeSuccessDuration,
  timeRangeActivity,
  activityAnimationEpoch,
  activityBuckets,
  activityHasData,
  hourlyRequestsChartRef,
  trendChartRef,
  successRateChartRef,
  responseTimeChartRef,
  successDurationChartRef,
  modelChartRef,
  modelRankChartRef,
} = useDashboardPage()

type ActivityBucket = (typeof activityBuckets.value)[number]

const activityMaxCalls = computed(() => Math.max(0, ...activityBuckets.value.map((bucket) => bucket.total_calls)))
const activityColumnCount = computed(() => {
  if (timeRangeActivity.value === '24h') return 12
  if (timeRangeActivity.value === '7d') return 7
  return 10
})
const activityGridMaxWidth = computed(() => {
  const columnCount = activityColumnCount.value
  const cellWidthRem = timeRangeActivity.value === '7d' ? 5.5 : 3.5
  const gapWidthRem = 0.375
  return `${columnCount * cellWidthRem + (columnCount - 1) * gapWidthRem}rem`
})

function activityCellClass(total: number) {
  const max = activityMaxCalls.value
  if (total <= 0 || max <= 0) return 'bg-muted/60'
  const ratio = total / max
  if (ratio <= 0.25) return 'bg-emerald-100 dark:bg-emerald-950/70'
  if (ratio <= 0.5) return 'bg-emerald-200 dark:bg-emerald-900/70'
  if (ratio <= 0.75) return 'bg-emerald-400 dark:bg-emerald-700'
  return 'bg-emerald-600 dark:bg-emerald-500'
}

function activityTooltip(bucket: ActivityBucket) {
  return `${bucket.label}：${bucket.total_calls} 次请求，成功 ${bucket.success_calls}，失败或限流 ${bucket.failed_calls}`
}
</script>

<style scoped>
.dashboard-activity-cell {
  animation: dashboard-activity-in 360ms cubic-bezier(0.22, 1, 0.36, 1) both;
}

@keyframes dashboard-activity-in {
  from {
    opacity: 0;
    transform: translateY(4px) scale(0.92);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .dashboard-activity-cell {
    animation: none;
  }
}
</style>
