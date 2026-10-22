import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { statsApi, type DashboardSnapshot } from '@/api/stats'
import { monitorApi } from '@/api/monitor'
import { formatRequestDuration } from '@/lib/requestDuration'
import { usePageQuery } from '@/composables/usePageQuery'
import { usePageRuntime } from '@/composables/usePageRuntime'
import {
  getLineChartTheme,
  getPieChartTheme,
  createLineSeries,
  createPieDataItem,
  chartColors,
  getModelColor,
  filterValidModels,
} from '@/lib/chartTheme'
import { buildDashboardTrendSeries } from './dashboardTrendSeries'
import { DEFAULT_DASHBOARD_TIME_RANGE, type DashboardTimeRange } from '@/lib/timeRanges'
import {
  readDashboardDefaultTimeRange,
  readDashboardRefreshIntervalSeconds,
} from './dashboardPreferences'

const DASHBOARD_TOOLTIP_HTML_ESCAPE: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

// ECharts tooltip formatter 返回的字符串会被当作 HTML 渲染；模型名/系列名可能来自用户或上游，必须转义。
function escapeDashboardTooltipText(value: unknown) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => (
    DASHBOARD_TOOLTIP_HTML_ESCAPE[character]
  ))
}

export function useDashboardPage() {
  type ChartInstance = {
    setOption: (
      option: unknown,
      opts?: boolean | { notMerge?: boolean; lazyUpdate?: boolean; replaceMerge?: string[] }
    ) => void
    resize: () => void
    dispose: () => void
    clear?: () => void
    off?: (eventName: string) => void
    on?: (eventName: string, handler: (params: any) => void) => void
    dispatchAction?: (payload: Record<string, unknown>) => void
  }
  type RenderMode = 'initial' | 'range' | 'refresh'
  type ChartType = 'hourlyRequests' | 'trend' | 'successRate' | 'model' | 'modelRank' | 'responseTime' | 'successDuration'
  type OverviewPayload = Record<string, any>
  const pageRuntime = usePageRuntime('dashboard')
  const DASHBOARD_DATA_REQUEST_KEY = 'dashboard:data'
  const CHART_BOOTSTRAP_TIMER_KEY = 'dashboard:chart-bootstrap'
  const AUTO_REFRESH_TIMER_KEY = 'dashboard:auto-refresh'
  const chartRequestKey = (chartType: ChartType) => `dashboard:chart:${chartType}`
  const ALL_CHART_TYPES: ChartType[] = ['hourlyRequests', 'trend', 'successRate', 'model', 'modelRank', 'responseTime', 'successDuration']
  const dashboardDataQuery = usePageQuery({
    runtime: pageRuntime,
    key: DASHBOARD_DATA_REQUEST_KEY,
    errorMessage: '概览加载失败',
  })

  // 每个图表独立的时间范围
  // 默认统计周期来自「设置 - 概览中心」偏好（localStorage），进入页面时重新读取。
  const initialTimeRange = readDashboardDefaultTimeRange()
  const timeRangeHourlyRequests = ref<DashboardTimeRange>(initialTimeRange)
  const timeRangeTrend = ref<DashboardTimeRange>(initialTimeRange)
  const timeRangeSuccessRate = ref<DashboardTimeRange>(initialTimeRange)
  const timeRangeModel = ref<DashboardTimeRange>(initialTimeRange)
  const timeRangeModelRank = ref<DashboardTimeRange>(initialTimeRange)
  const timeRangeResponseTime = ref<DashboardTimeRange>(initialTimeRange)
  const timeRangeSuccessDuration = ref<DashboardTimeRange>(initialTimeRange)
  const timeRangeActivity = ref<DashboardTimeRange>(initialTimeRange)
  const activityAnimationEpoch = ref(0)

  function applyDashboardDefaultTimeRange() {
    const next = readDashboardDefaultTimeRange()
    timeRangeHourlyRequests.value = next
    timeRangeTrend.value = next
    timeRangeSuccessRate.value = next
    timeRangeModel.value = next
    timeRangeModelRank.value = next
    timeRangeResponseTime.value = next
    timeRangeSuccessDuration.value = next
    timeRangeActivity.value = next
  }

  // 创建图表监听器的工厂函数
  function createChartWatcher(chartType: ChartType, updateFn: (mode?: RenderMode) => void) {
    return async (newVal: DashboardTimeRange) => {
      if (!pageRuntime.canRun.value) return
      const requestId = pageRuntime.nextRequest(chartRequestKey(chartType))
      const applied = await loadChartData(chartType, newVal, requestId)
      if (applied) updateFn('range')
    }
  }

  // 监听各图表时间范围变化 - 只更新对应图表
  watch(timeRangeHourlyRequests, createChartWatcher('hourlyRequests', updateHourlyRequestsChart))
  watch(timeRangeTrend, createChartWatcher('trend', updateTrendChart))
  watch(timeRangeSuccessRate, createChartWatcher('successRate', updateSuccessRateChart))
  watch(timeRangeModel, createChartWatcher('model', updateModelChart))
  watch(timeRangeModelRank, createChartWatcher('modelRank', updateModelRankChart))
  watch(timeRangeResponseTime, createChartWatcher('responseTime', updateResponseTimeChart))
  watch(timeRangeSuccessDuration, createChartWatcher('successDuration', updateSuccessDurationChart))
  watch(timeRangeActivity, () => { activityAnimationEpoch.value += 1 })

  function createDefaultStats() {
    return [
      {
        label: '账号总数',
        value: '0',
        meta: '',
        icon: 'lucide:users',
        iconBg: 'bg-sky-100',
        iconColor: 'text-sky-600'
      },
      {
        label: '正常账号',
        value: '0',
        meta: '',
        icon: 'lucide:check-circle',
        iconBg: 'bg-emerald-100',
        iconColor: 'text-emerald-600'
      },
      {
        label: '限流账号',
        value: '0',
        meta: '',
        icon: 'lucide:clock',
        iconBg: 'bg-amber-100',
        iconColor: 'text-amber-600'
      },
      {
        label: '异常账号',
        value: '0',
        meta: '',
        icon: 'lucide:alert-circle',
        iconBg: 'bg-rose-100',
        iconColor: 'text-rose-600'
      },
      {
        label: '禁用账号',
        value: '0',
        meta: '',
        icon: 'lucide:ban',
        iconBg: 'bg-slate-100',
        iconColor: 'text-slate-600'
      },
      {
        label: '剩余额度',
        value: '0',
        meta: '',
        icon: 'lucide:coins',
        iconBg: 'bg-cyan-100',
        iconColor: 'text-cyan-600'
      },
    ]
  }

  const stats = ref(createDefaultStats())

  // 调用概览：近 24 小时口径，数据来自后端 dashboard_metrics（文本回复不计成败，部分成功算成功）。
  function createDefaultCallStats() {
    return [
      { label: '当前并发', value: '--', meta: '正在处理的请求', icon: 'lucide:activity', iconBg: 'bg-violet-100', iconColor: 'text-violet-600' },
      { label: '总调用量', value: '0', meta: '近 24 小时', icon: 'lucide:database', iconBg: 'bg-sky-100', iconColor: 'text-sky-600' },
      { label: '成功率', value: '--', meta: '成功 /（成功 + 失败），文本不计', icon: 'lucide:check-circle', iconBg: 'bg-emerald-100', iconColor: 'text-emerald-600' },
      { label: '平均成功耗时', value: '--', meta: '仅统计成功请求', icon: 'lucide:clock', iconBg: 'bg-amber-100', iconColor: 'text-amber-600' },
      { label: '触发切号', value: '0', meta: '发生过账号切换的请求', icon: 'lucide:shuffle', iconBg: 'bg-rose-100', iconColor: 'text-rose-600' },
      { label: '切换恢复率', value: '--', meta: '切号后最终成功的比例', icon: 'lucide:refresh-cw', iconBg: 'bg-cyan-100', iconColor: 'text-cyan-600' },
    ]
  }
  const callStats = ref(createDefaultCallStats())

  // 每个图表独立的数据状态
  function createEmptyChartData() {
    return {
      hourlyRequests: {
        labels: [] as string[],
        modelRequests: {} as Record<string, number[]>,
      },
      trend: {
        labels: [] as string[],
        totalRequests: [] as number[],
        failedRequests: [] as number[],
        rateLimitedRequests: [] as number[],
        successRequests: [] as number[],
        switchCount: [] as number[],
      },
      successRate: {
        labels: [] as string[],
        totalRequests: [] as number[],
        failedRequests: [] as number[],
      },
      model: {
        modelRequests: {} as Record<string, number[]>,
      },
      modelRank: {
        modelRequests: {} as Record<string, number[]>,
      },
      responseTime: {
        labels: [] as string[],
        modelTtfbTimes: {} as Record<string, number[]>,
        // 各模型成功请求平均耗时（毫秒），无数据的时间桶为 null
        modelTotalTimes: {} as Record<string, Array<number | null>>,
      },
      successDuration: {
        labels: [] as string[],
        avgMs: [] as Array<number | null>,
      },
    }
  }

  const chartData = ref(createEmptyChartData())

  const overviewCache = new Map<string, OverviewPayload>()
  // 单次快照：账号统计与三个统计周期同时更新；同一时刻只允许一个快照请求（串行刷新）。
  let snapshotRequest: Promise<DashboardSnapshot> | null = null
  let snapshotAbort: AbortController | null = null
  const activitySnapshot = shallowRef<Record<string, OverviewPayload>>({})
  const dashboardVersion = ref('')
  const dashboardStaleWarning = ref('')

  const trendChartRef = ref<HTMLDivElement | null>(null)
  const modelChartRef = ref<HTMLDivElement | null>(null)
  const successRateChartRef = ref<HTMLDivElement | null>(null)
  const hourlyRequestsChartRef = ref<HTMLDivElement | null>(null)
  const modelRankChartRef = ref<HTMLDivElement | null>(null)
  const responseTimeChartRef = ref<HTMLDivElement | null>(null)
  const successDurationChartRef = ref<HTMLDivElement | null>(null)

  const charts = {
    trend: null as ChartInstance | null,
    model: null as ChartInstance | null,
    successRate: null as ChartInstance | null,
    hourlyRequests: null as ChartInstance | null,
    modelRank: null as ChartInstance | null,
    responseTime: null as ChartInstance | null,
    successDuration: null as ChartInstance | null,
  }

  type ChartKey = keyof typeof charts
  const renderProfiles: Record<RenderMode, {
    duration: number
    updateDuration: number
    delayStep: number
    lazyUpdate: boolean
  }> = {
    initial: { duration: 860, updateDuration: 620, delayStep: 14, lazyUpdate: false },
    range: { duration: 560, updateDuration: 460, delayStep: 8, lazyUpdate: false },
    refresh: { duration: 260, updateDuration: 220, delayStep: 0, lazyUpdate: true },
  }
  const chartFirstRenderState = ref<Record<ChartKey, boolean>>({
    trend: true,
    model: true,
    successRate: true,
    hourlyRequests: true,
    modelRank: true,
    responseTime: true,
    successDuration: true,
  })
  const chartsBootstrapped = ref(false)
  const dashboardDataReady = ref(false)
  const dashboardLoadSettled = ref(false)
  let dashboardEntrySeq = 0
  const modelLayoutIsMobile = ref<boolean | null>(null)

  // 容器尺寸变化（例如折叠/展开侧栏）时窗口宽度不变，需要 ResizeObserver 触发图表重排。
  let chartResizeObserver: ResizeObserver | null = null
  let chartResizeFrame = 0

  function scheduleChartResize() {
    if (chartResizeFrame) return
    chartResizeFrame = requestAnimationFrame(() => {
      chartResizeFrame = 0
      handleResize()
    })
  }

  function observeChartContainers() {
    chartResizeObserver?.disconnect()
    if (typeof ResizeObserver === 'undefined') return
    chartResizeObserver = new ResizeObserver(scheduleChartResize)
    ;[
      trendChartRef, modelChartRef, successRateChartRef, hourlyRequestsChartRef,
      modelRankChartRef, responseTimeChartRef, successDurationChartRef,
    ].forEach((target) => {
      if (target.value) chartResizeObserver?.observe(target.value)
    })
  }

  function bindResizeListener() {
    window.removeEventListener('resize', handleResize)
    window.addEventListener('resize', handleResize)
    if (chartsBootstrapped.value) observeChartContainers()
  }

  function unbindResizeListener() {
    window.removeEventListener('resize', handleResize)
    chartResizeObserver?.disconnect()
    chartResizeObserver = null
    if (chartResizeFrame) {
      cancelAnimationFrame(chartResizeFrame)
      chartResizeFrame = 0
    }
  }

  function applyAnimatedOption(key: ChartKey, option: Record<string, unknown>, mode: RenderMode = 'refresh') {
    const chart = charts[key]
    if (!chart) return
    const isFirstRender = chartFirstRenderState.value[key]
    const activeMode: RenderMode = isFirstRender ? 'initial' : mode
    const profile = renderProfiles[activeMode]
    const optionWithAnimation = {
      ...option,
      animation: true,
      animationDuration: profile.duration,
      animationDurationUpdate: profile.updateDuration,
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicOut',
      animationDelay: profile.delayStep > 0 ? (idx: number) => Math.min(idx * profile.delayStep, 180) : 0,
      animationDelayUpdate: profile.delayStep > 0 ? (idx: number) => Math.min(idx * Math.max(4, Math.floor(profile.delayStep / 2)), 120) : 0,
    }
    if (activeMode === 'range') {
      chart.clear?.()
    }
    chart.setOption(optionWithAnimation, {
      notMerge: activeMode === 'range',
      lazyUpdate: profile.lazyUpdate,
      replaceMerge: ['series', 'xAxis', 'yAxis', 'legend'],
    })
    chartFirstRenderState.value[key] = false
  }

  function initChart(
    ref: HTMLDivElement | null,
    key: ChartKey,
    updateFn: (mode?: RenderMode) => void
  ) {
    const echarts = (window as any).echarts as { init: (el: HTMLElement) => ChartInstance } | undefined
    if (!echarts || !ref) return
    charts[key] = echarts.init(ref)
    updateFn('initial')
  }

  function bootstrapCharts() {
    if (chartsBootstrapped.value) return
    initChart(trendChartRef.value, 'trend', updateTrendChart)
    initChart(modelChartRef.value, 'model', updateModelChart)
    initChart(successRateChartRef.value, 'successRate', updateSuccessRateChart)
    initChart(hourlyRequestsChartRef.value, 'hourlyRequests', updateHourlyRequestsChart)
    initChart(modelRankChartRef.value, 'modelRank', updateModelRankChart)
    initChart(responseTimeChartRef.value, 'responseTime', updateResponseTimeChart)
    initChart(successDurationChartRef.value, 'successDuration', updateSuccessDurationChart)
    chartsBootstrapped.value = true
    observeChartContainers()
  }

  function resetChartFirstRenderState() {
    chartFirstRenderState.value = {
      trend: true,
      model: true,
      successRate: true,
      hourlyRequests: true,
      modelRank: true,
      responseTime: true,
      successDuration: true,
    }
  }

  function disposeCharts() {
    ;(Object.keys(charts) as ChartKey[]).forEach((key) => {
      charts[key]?.dispose()
      charts[key] = null
    })
    chartsBootstrapped.value = false
    resetChartFirstRenderState()
  }

  function clearChartBootstrapTimer() {
    pageRuntime.clearTimer(CHART_BOOTSTRAP_TIMER_KEY)
  }

  function cancelDashboardDataRequests(options: { clearRequests?: boolean } = {}) {
    dashboardDataQuery.invalidate()
    ALL_CHART_TYPES.forEach((chartType) => {
      pageRuntime.invalidateRequest(chartRequestKey(chartType))
    })
    if (options.clearRequests !== false) {
      snapshotAbort?.abort()
      snapshotAbort = null
      snapshotRequest = null
    }
  }

  function resetDashboardViewState() {
    cancelDashboardDataRequests()
    dashboardDataReady.value = false
    dashboardLoadSettled.value = false
    stats.value = createDefaultStats()
    callStats.value = createDefaultCallStats()
    chartData.value = createEmptyChartData()
    overviewCache.clear()
    activitySnapshot.value = {}
    dashboardStaleWarning.value = ''
    stopAutoRefresh()
    disposeCharts()
    clearChartBootstrapTimer()
    modelLayoutIsMobile.value = null
  }

  // ---- 自动刷新：进入页面立即取一次，之后仅在页面可见时按偏好间隔串行刷新 ----
  function stopAutoRefresh() {
    pageRuntime.clearTimer(AUTO_REFRESH_TIMER_KEY)
  }

  function scheduleAutoRefresh() {
    stopAutoRefresh()
    if (!dashboardDataReady.value) return
    const delayMs = readDashboardRefreshIntervalSeconds() * 1000
    // setTimer 默认要求页面可见；隐藏时到点不会执行，也不会续约，回到可见后由 onShow 重新启动。
    pageRuntime.setTimer(AUTO_REFRESH_TIMER_KEY, delayMs, () => {
      void refreshInBackground()
    })
  }

  function updateAllCharts(mode: RenderMode) {
    updateHourlyRequestsChart(mode)
    updateTrendChart(mode)
    updateSuccessRateChart(mode)
    updateModelChart(mode)
    updateModelRankChart(mode)
    updateResponseTimeChart(mode)
    updateSuccessDurationChart(mode)
  }

  let backgroundRefreshing = false

  async function refreshInBackground() {
    // 串行：上一次刷新没结束就不会再发新的请求；失败时保留最近一次可用数据。
    if (backgroundRefreshing || !pageRuntime.canRun.value || !dashboardDataReady.value) return
    backgroundRefreshing = true
    const entrySeq = dashboardEntrySeq
    try {
      const chartRanges = getDashboardChartRanges()
      await fetchSnapshot()
      if (entrySeq !== dashboardEntrySeq || !pageRuntime.canRun.value) return
      applyDashboardOverview(chartRanges)
      void refreshActiveRequests()
      updateAllCharts('refresh')
      dashboardStaleWarning.value = ''
    } catch (error) {
      if (entrySeq === dashboardEntrySeq && pageRuntime.canRun.value) {
        console.error('Failed to refresh dashboard data:', error)
        dashboardStaleWarning.value = '概览刷新失败，当前显示的是最近一次成功获取的数据。'
      }
    } finally {
      backgroundRefreshing = false
      if (entrySeq === dashboardEntrySeq) scheduleAutoRefresh()
    }
  }

  function scheduleChartBootstrap(delayMs = 80) {
    if (chartsBootstrapped.value) return
    clearChartBootstrapTimer()
    pageRuntime.setTimer(CHART_BOOTSTRAP_TIMER_KEY, delayMs, () => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          bootstrapCharts()
        })
      })
    })
  }

  pageRuntime.onActivate(({ initial }) => {
    bindResizeListener()
    if (initial) {
      void reloadDashboardOnEnter()
      return
    }
    void reloadDashboardOnEnter()
  })

  pageRuntime.onDeactivate(() => {
    unbindResizeListener()
    dashboardEntrySeq += 1
    resetDashboardViewState()
  })

  pageRuntime.onHide(() => {
    // 页面隐藏：停止刷新并取消进行中的请求，但保留最近一次数据，回到可见后再静默刷新。
    unbindResizeListener()
    stopAutoRefresh()
    snapshotAbort?.abort()
    snapshotAbort = null
    snapshotRequest = null
    backgroundRefreshing = false
  })

  pageRuntime.onShow(() => {
    bindResizeListener()
    if (!dashboardDataReady.value) {
      void reloadDashboardOnEnter()
      return
    }
    void refreshInBackground()
  })

  onBeforeUnmount(() => {
    unbindResizeListener()
    dashboardEntrySeq += 1
    clearChartBootstrapTimer()
    disposeCharts()
  })

  function updateTrendChart(mode: RenderMode = 'refresh') {
    if (!charts.trend) return

    const theme = getLineChartTheme()

    applyAnimatedOption('trend', {
      ...theme,
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.trend.labels,
      },
      series: buildDashboardTrendSeries(
        chartData.value.trend,
        createLineSeries,
        {
          success: chartColors.primary,
          failure: chartColors.danger,
          rateLimited: chartColors.warning,
          switchAccount: chartColors.purple,
        },
      ),
    }, mode)
  }

  function getModelTotals() {
    return Object.entries(chartData.value.model.modelRequests)
      .map(([model, data]) => ({
        model,
        data: createPieDataItem(model, data.reduce((sum, item) => sum + item, 0), getModelColor(model)),
        total: data.reduce((sum, item) => sum + item, 0),
      }))
      .filter(item => item.total > 0)
  }

  function updateModelChart(mode: RenderMode = 'refresh') {
    if (!charts.model) return

    const isMobile = window.innerWidth < 768
    modelLayoutIsMobile.value = isMobile
    const theme = getPieChartTheme(isMobile)
    const modelData = getModelTotals().map(item => item.data)
    const modelColors = modelData.map(item => String(item?.itemStyle?.color || getModelColor(String(item?.name || ''))))

    applyAnimatedOption('model', {
      ...theme,
      color: modelColors,
      tooltip: {
        ...theme.tooltip,
        formatter: (params: { name: string; value: number; percent: number }) =>
          `${escapeDashboardTooltipText(params.name)}: ${escapeDashboardTooltipText(params.value)} 次 (${escapeDashboardTooltipText(params.percent)}%)`,
      },
      legend: {
        ...theme.legend,
        data: modelData.map(item => item.name),
      },
      series: [
        {
          ...theme.series,
          center: ['50%', '50%'],
          data: modelData,
        },
      ],
    }, mode)
  }

  function handleResize() {
    Object.entries(charts).forEach(([key, chart]) => {
      if (chart) {
        if (key === 'model') {
          const nowMobile = window.innerWidth < 768
          if (modelLayoutIsMobile.value !== nowMobile) {
            updateModelChart()
          } else {
            chart.resize()
          }
        } else {
          chart.resize()
        }
      }
    })
  }

  function getChartRange(chartType: ChartType) {
    switch (chartType) {
      case 'hourlyRequests':
        return timeRangeHourlyRequests.value
      case 'trend':
        return timeRangeTrend.value
      case 'successRate':
        return timeRangeSuccessRate.value
      case 'model':
        return timeRangeModel.value
      case 'modelRank':
        return timeRangeModelRank.value
      case 'responseTime':
        return timeRangeResponseTime.value
      case 'successDuration':
        return timeRangeSuccessDuration.value
    }
  }

  function fetchSnapshot(): Promise<DashboardSnapshot> {
    if (snapshotRequest) return snapshotRequest
    const controller = new AbortController()
    snapshotAbort = controller
    const request: Promise<DashboardSnapshot> = statsApi
      .snapshot(controller.signal)
      .then((snapshot) => {
        if (snapshotRequest === request && !controller.signal.aborted) {
          Object.entries(snapshot.overviews).forEach(([range, overview]) => {
            overviewCache.set(range, overview as unknown as OverviewPayload)
          })
          activitySnapshot.value = { ...snapshot.overviews } as unknown as Record<string, OverviewPayload>
          dashboardVersion.value = snapshot.version
        }
        return snapshot
      })
      .finally(() => {
        if (snapshotRequest === request) {
          snapshotRequest = null
          snapshotAbort = null
        }
      })
    snapshotRequest = request
    return request
  }

  async function getOverview(timeRange: string, options: { force?: boolean } = {}) {
    if (!options.force) {
      const cached = overviewCache.get(timeRange)
      if (cached) return cached
    }
    await fetchSnapshot()
    const overview = overviewCache.get(timeRange)
    if (!overview) throw new Error('概览快照缺少统计周期 ' + timeRange)
    return overview
  }

  // 调用活跃度：每个统计时间桶一个格子，颜色按调用量分级；没有请求时展示空态。
  const activityBuckets = computed(() => {
    const trend = (activitySnapshot.value[timeRangeActivity.value] as OverviewPayload | undefined)?.trend
    if (!trend) return [] as Array<{ label: string; total_calls: number; success_calls: number; failed_calls: number }>
    const labels = normalizeLabels(trend.labels)
    const total = normalizeNumberSeries(trend.total_requests, labels.length)
    const success = normalizeNumberSeries(trend.success_requests, labels.length)
    const failed = normalizeNumberSeries(trend.failed_requests, labels.length)
    const limited = normalizeNumberSeries(trend.rate_limited_requests, labels.length)
    return labels.map((label, index) => ({
      label,
      total_calls: total[index] || 0,
      success_calls: success[index] || 0,
      failed_calls: (failed[index] || 0) + (limited[index] || 0),
    }))
  })
  const activityHasData = computed(() => activityBuckets.value.some((bucket) => bucket.total_calls > 0))

  function formatStatNumber(value: unknown) {
    const number = Number(value || 0)
    if (!Number.isFinite(number)) return '0'
    return Math.max(0, Math.trunc(number)).toLocaleString('zh-CN')
  }

  function applyAccountStats(overview: OverviewPayload) {
    stats.value[0].value = formatStatNumber(overview.total_accounts)
    stats.value[1].value = formatStatNumber(overview.active_accounts)
    stats.value[2].value = formatStatNumber(overview.rate_limited_accounts)
    stats.value[3].value = formatStatNumber(overview.abnormal_accounts)
    stats.value[4].value = formatStatNumber(overview.disabled_accounts)
    const totalQuota = Number(overview.total_quota || 0)
    stats.value[5].value = formatStatNumber(totalQuota)
    const providers = overview.account_providers || {}
    const gpt = providers.gpt || {}
    const grok = providers.grok || {}
    stats.value[0].meta = `GPT ${formatStatNumber(gpt.total)} · Grok ${formatStatNumber(grok.total)}`
    stats.value[1].meta = `GPT ${formatStatNumber(gpt.active)} · Grok ${formatStatNumber(grok.active)}`
    stats.value[5].meta = `GPT ${formatStatNumber(gpt.total_quota)} · Grok ${formatStatNumber(grok.total_quota)}`
  }

  function formatPercent(value: unknown) {
    if (value === null || value === undefined || value === '') return '--'
    const number = Number(value)
    return Number.isFinite(number) ? `${number}%` : '--'
  }

  function applyCallStats(overview: OverviewPayload) {
    const success = Number(overview.success_count || 0)
    const failed = Number(overview.failed_count || 0)
    const textReview = Number(overview.text_review_count || 0)
    const switching = overview.switching || {}
    const durationText = formatRequestDuration(overview.avg_success_duration_ms)
    const items = callStats.value
    items[1].value = formatStatNumber(overview.total_calls)
    items[1].meta = `成功 ${formatStatNumber(success)} · 失败 ${formatStatNumber(failed)}${textReview > 0 ? ` · 文本 ${formatStatNumber(textReview)}` : ''}`
    items[2].value = formatPercent(overview.success_rate)
    items[3].value = durationText || '--'
    items[4].value = formatStatNumber(switching.requests)
    items[4].meta = `共切换 ${formatStatNumber(switching.count)} 次`
    items[5].value = formatPercent(switching.recovery_rate)
    items[5].meta = `${formatStatNumber(switching.recovered)} / ${formatStatNumber(switching.requests)} 个请求恢复`
  }

  // 当前并发取自实时监控；失败时保留上一次的值，不影响概览其它数据。
  async function refreshActiveRequests() {
    try {
      const realtime = await monitorApi.realtime()
      const active = Number(realtime?.summary?.active ?? realtime?.active?.length ?? 0)
      callStats.value[0].value = formatStatNumber(active)
    } catch {
      /* 实时监控不可用时保持原值 */
    }
  }

  function getTrendPayload(overview: OverviewPayload) {
    return overview.trend || {
      labels: [],
      total_requests: [],
      success_requests: [],
      failed_requests: [],
      rate_limited_requests: [],
      model_requests: {},
      model_ttfb_times: {},
      model_total_times: {},
    }
  }

  function normalizeLabels(raw: unknown): string[] {
    return Array.isArray(raw) ? raw.map(item => String(item || '')) : []
  }

  function normalizeNumberSeries(raw: unknown, pointCount?: number): number[] {
    const source = Array.isArray(raw) ? raw : []
    const normalized = source.map((item) => {
      const value = Number(item || 0)
      return Number.isFinite(value) ? value : 0
    })
    if (pointCount === undefined) return normalized
    return Array.from({ length: pointCount }, (_, index) => normalized[index] ?? 0)
  }

  function normalizeNullableSeries(raw: unknown, pointCount?: number): Array<number | null> {
    const source = Array.isArray(raw) ? raw : []
    const normalized = source.map((item) => {
      if (item === null || item === undefined || item === '') return null
      const value = Number(item)
      return Number.isFinite(value) ? value : null
    })
    if (pointCount === undefined) return normalized
    return Array.from({ length: pointCount }, (_, index) => normalized[index] ?? null)
  }

  function normalizeNullableModelSeries(raw: unknown, pointCount?: number): Record<string, Array<number | null>> {
    const source = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
    const filtered = filterValidModels(source as Record<string, number[]>)
    return Object.fromEntries(
      Object.entries(filtered).map(([model, data]) => [model, normalizeNullableSeries(data, pointCount)]),
    )
  }

  function normalizeModelSeries(raw: unknown, pointCount?: number): Record<string, number[]> {
    const filtered = filterValidModels(raw as Record<string, number[]>)
    return Object.fromEntries(
      Object.entries(filtered).map(([model, data]) => [
        model,
        normalizeNumberSeries(data, pointCount),
      ])
    )
  }

  function applyOverviewToChartData(chartType: ChartType, overview: OverviewPayload) {
    const trend = getTrendPayload(overview)
    const labels = normalizeLabels(trend.labels)
    const pointCount = labels.length > 0 ? labels.length : undefined
    const totalSeries = normalizeNumberSeries(trend.total_requests, pointCount)
    const failed = normalizeNumberSeries(trend.failed_requests, pointCount)
    const limited = normalizeNumberSeries(trend.rate_limited_requests, pointCount)
    const success = normalizeNumberSeries(trend.success_requests, pointCount)
    const modelRequests = normalizeModelSeries(trend.model_requests, pointCount)
    // 模型请求分布只统计成功请求；旧后端没有该字段时回退到全部请求。
    const modelSuccessRequests = trend.model_success_requests
      ? normalizeModelSeries(trend.model_success_requests, pointCount)
      : modelRequests
    const modelTtfbTimes = normalizeModelSeries(trend.model_ttfb_times, pointCount)
    const modelTotalTimes = normalizeNullableModelSeries(
      trend.model_avg_success_duration_ms ?? trend.model_total_times,
      pointCount,
    )
    const successDurationMs = normalizeNullableSeries(trend.success_duration_ms, pointCount)
    // 文本回复不计成败：成功率只按成功 + 失败（含限流）的请求计算。
    const textReview = normalizeNumberSeries(trend.text_review_requests, pointCount)
    const measuredSeries = totalSeries.map((total: number, idx: number) => Math.max(total - (textReview[idx] || 0), 0))
    const switchSeries = normalizeNumberSeries(trend.switch_count, pointCount)
    const failureSeries = totalSeries.map((_: number, idx: number) => (failed[idx] || 0) + (limited[idx] || 0))
    const successSeries = totalSeries.map((total: number, idx: number) => {
      const explicitSuccess = Number(success[idx])
      if (Number.isFinite(explicitSuccess)) return explicitSuccess
      return Math.max(Number(total || 0) - (failed[idx] || 0) - (limited[idx] || 0), 0)
    })

    switch (chartType) {
      case 'hourlyRequests':
        chartData.value.hourlyRequests.labels = labels
        chartData.value.hourlyRequests.modelRequests = modelSuccessRequests
        break
      case 'trend':
        chartData.value.trend.labels = labels
        chartData.value.trend.totalRequests = totalSeries
        chartData.value.trend.failedRequests = failed
        chartData.value.trend.rateLimitedRequests = limited
        chartData.value.trend.successRequests = successSeries
        chartData.value.trend.switchCount = switchSeries
        break
      case 'successRate':
        chartData.value.successRate.labels = labels
        chartData.value.successRate.totalRequests = measuredSeries
        chartData.value.successRate.failedRequests = failureSeries
        break
      case 'model':
        chartData.value.model.modelRequests = modelRequests
        break
      case 'modelRank':
        chartData.value.modelRank.modelRequests = modelRequests
        break
      case 'responseTime':
        chartData.value.responseTime.labels = labels
        chartData.value.responseTime.modelTtfbTimes = modelTtfbTimes
        chartData.value.responseTime.modelTotalTimes = modelTotalTimes
        break
      case 'successDuration':
        chartData.value.successDuration.labels = labels
        chartData.value.successDuration.avgMs = successDurationMs
        break
    }
  }

  function getDashboardChartRanges(): Record<ChartType, DashboardTimeRange> {
    return {
      hourlyRequests: timeRangeHourlyRequests.value,
      trend: timeRangeTrend.value,
      successRate: timeRangeSuccessRate.value,
      model: timeRangeModel.value,
      modelRank: timeRangeModelRank.value,
      responseTime: timeRangeResponseTime.value,
      successDuration: timeRangeSuccessDuration.value,
    }
  }

  function getDashboardOverviewRanges(chartRanges: Record<ChartType, DashboardTimeRange>) {
    return Array.from(
      new Set<string>([
        DEFAULT_DASHBOARD_TIME_RANGE,
        ...Object.values(chartRanges),
        timeRangeActivity.value,
      ])
    )
  }

  async function loadDashboardOverviewRanges(chartRanges: Record<ChartType, DashboardTimeRange>, force: boolean) {
    await Promise.all(
      getDashboardOverviewRanges(chartRanges).map((timeRange) => getOverview(timeRange, { force }))
    )
  }

  function applyDashboardOverview(chartRanges: Record<ChartType, DashboardTimeRange>) {
    const accountOverview = overviewCache.get(DEFAULT_DASHBOARD_TIME_RANGE)
    if (accountOverview) {
      applyAccountStats(accountOverview)
      applyCallStats(accountOverview)
    }

    ALL_CHART_TYPES.forEach((chartType) => {
      if (chartRanges[chartType] !== getChartRange(chartType)) return
      const overview = overviewCache.get(chartRanges[chartType])
      if (overview) applyOverviewToChartData(chartType, overview)
    })
  }

  async function refreshDashboardData(force = false) {
    const chartRanges = getDashboardChartRanges()

    const refreshed = await dashboardDataQuery.run(
      async () => {
        await loadDashboardOverviewRanges(chartRanges, force)
        return true
      },
      {
        apply: () => {
          applyDashboardOverview(chartRanges)
          void refreshActiveRequests()
        },
        onError: (_message, error) => {
          console.error('Failed to refresh dashboard data:', error)
        },
      },
    )
    return Boolean(refreshed)
  }

  async function reloadDashboardOnEnter() {
    const entrySeq = ++dashboardEntrySeq
    resetDashboardViewState()
    applyDashboardDefaultTimeRange()
    await nextTick()
    const refreshed = await refreshDashboardData(true)
    if (entrySeq !== dashboardEntrySeq) return
    dashboardLoadSettled.value = true
    dashboardDataReady.value = refreshed
    if (!refreshed) return
    await nextTick()
    if (entrySeq !== dashboardEntrySeq) return
    scheduleChartBootstrap(refreshed ? 0 : 80)
    scheduleAutoRefresh()
  }

  function retryDashboard() {
    void reloadDashboardOnEnter()
  }

  async function loadChartData(chartType: ChartType, timeRange: DashboardTimeRange, requestId?: number) {
    try {
      const overview = await getOverview(timeRange)
      if (
        requestId !== undefined &&
        (!pageRuntime.isLatestRequest(chartRequestKey(chartType), requestId) || timeRange !== getChartRange(chartType))
      ) {
        return false
      }
      applyOverviewToChartData(chartType, overview)
      return true
    } catch (error) {
      console.error(`Failed to load ${chartType} data:`, error)
      return false
    }
  }

  function updateSuccessRateChart(mode: RenderMode = 'refresh') {
    if (!charts.successRate) return

    const theme = getLineChartTheme()
    const successRates = chartData.value.successRate.totalRequests.map((total, idx) => {
      const failure = chartData.value.successRate.failedRequests[idx] || 0
      return total > 0 ? Math.round(((total - failure) / total) * 100) : 100
    })

    applyAnimatedOption('successRate', {
      ...theme,
      tooltip: {
        ...theme.tooltip,
        trigger: 'axis',
        formatter: (params: any) => {
          if (!params || params.length === 0) return ''
          const param = params[0]
          return `<div style="font-weight: 600; margin-bottom: 4px;">${escapeDashboardTooltipText(param.axisValue)}</div>
            <div style="display: flex; justify-content: space-between; gap: 16px; align-items: center;">
              <span>${param.marker} ${escapeDashboardTooltipText(param.seriesName)}</span>
              <span style="font-weight: 600;">${escapeDashboardTooltipText(param.value)}%</span>
            </div>`
        },
      },
      grid: {
        ...theme.grid,
        top: 32,
        bottom: 32,
      },
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.successRate.labels,
      },
      yAxis: {
        ...theme.yAxis,
        max: 100,
        axisLabel: {
          ...theme.yAxis.axisLabel,
          formatter: '{value}%',
        },
      },
      series: [
        {
          name: '成功率',
          type: 'line',
          data: successRates,
          smooth: true,
          showSymbol: false,
          lineStyle: {
            width: 3,
          },
          areaStyle: {
            opacity: 0.3,
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: chartColors.success },
                { offset: 1, color: 'rgba(16, 185, 129, 0.1)' },
              ],
            },
          },
          itemStyle: {
            color: chartColors.success,
          },
        },
      ],
    }, mode)
  }

  function updateHourlyRequestsChart(mode: RenderMode = 'refresh') {
    if (!charts.hourlyRequests) return

    const theme = getLineChartTheme()
    const modelNames = Object.keys(chartData.value.hourlyRequests.modelRequests)

    if (modelNames.length === 0) {
      applyAnimatedOption('hourlyRequests', {
        ...theme,
        grid: {
          ...theme.grid,
          left: 34,
          right: 24,
          top: 32,
          bottom: 32,
        },
        xAxis: {
          ...theme.xAxis,
          data: chartData.value.hourlyRequests.labels,
          boundaryGap: true,
        },
        yAxis: {
          ...theme.yAxis,
        },
        series: [
          {
            name: '总请求',
            type: 'bar',
            data: [],
            barWidth: '60%',
            itemStyle: {
              color: chartColors.primary,
              borderRadius: [4, 4, 0, 0],
            },
          },
        ],
      }, mode)
      return
    }

    const pointCount = chartData.value.hourlyRequests.labels.length
    const topSeriesIndexByPoint = Array.from({ length: pointCount }, (_, pointIndex) => {
      for (let seriesIndex = modelNames.length - 1; seriesIndex >= 0; seriesIndex -= 1) {
        const value = Number(chartData.value.hourlyRequests.modelRequests[modelNames[seriesIndex]]?.[pointIndex] || 0)
        if (value > 0) return seriesIndex
      }
      return -1
    })

    const series = modelNames.map((modelName, seriesIndex) => ({
      name: modelName,
      type: 'bar',
      stack: 'total',
      itemStyle: {
        color: getModelColor(modelName),
      },
      data: (chartData.value.hourlyRequests.modelRequests[modelName] || []).map((value, pointIndex) => ({
        value,
        itemStyle: {
          color: getModelColor(modelName),
          borderRadius: topSeriesIndexByPoint[pointIndex] === seriesIndex ? [4, 4, 0, 0] : [0, 0, 0, 0],
        },
      })),
    }))

    applyAnimatedOption('hourlyRequests', {
      ...theme,
      color: modelNames.map(name => getModelColor(name)),
      tooltip: {
        ...theme.tooltip,
        trigger: 'axis',
        axisPointer: {
          type: 'shadow',
        },
        formatter: (params: any) => {
          if (!params || params.length === 0) return ''
          let result = `<div style="font-weight: 600; margin-bottom: 4px;">${escapeDashboardTooltipText(params[0].axisValue)}</div>`
          let total = 0
          params.forEach((item: any) => {
            total += item.value || 0
            result += `<div style="display: flex; justify-content: space-between; gap: 16px; align-items: center;">
              <span>${item.marker} ${escapeDashboardTooltipText(item.seriesName)}</span>
              <span style="font-weight: 600;">${escapeDashboardTooltipText(item.value || 0)}</span>
            </div>`
          })
          result += `<div style="margin-top: 6px; padding-top: 6px; border-top: 1px solid #e5e5e5; font-weight: 600;">
            总计: ${total}
          </div>`
          return result
        },
      },
      legend: {
        ...theme.legend,
        data: modelNames,
        top: 0,
        right: 0,
        type: 'scroll',
        pageIconSize: 10,
        pageTextStyle: {
          fontSize: 10,
        },
      },
      grid: {
        ...theme.grid,
        left: 34,
        right: 24,
        top: modelNames.length > 5 ? 56 : 48,
        bottom: 32,
      },
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.hourlyRequests.labels,
        boundaryGap: true,
      },
      yAxis: {
        ...theme.yAxis,
      },
      series: series,
    }, mode)

  }

  function updateModelRankChart(mode: RenderMode = 'refresh') {
    if (!charts.modelRank) return

    const theme = getLineChartTheme()
    const modelTotals = Object.entries(chartData.value.modelRank.modelRequests)
      .map(([model, data]) => ({
        model,
        total: data.reduce((sum, item) => sum + item, 0),
      }))
      .filter(item => item.total > 0)
      .sort((a, b) => b.total - a.total)

    const modelNames = modelTotals.map(item => item.model)
    const modelValues = modelTotals.map(item => item.total)
    const modelColors = modelNames.map(name => getModelColor(name))

    applyAnimatedOption('modelRank', {
      ...theme,
      grid: {
        left: 12,
        right: 60,
        top: 16,
        bottom: 16,
        containLabel: true,
      },
      xAxis: {
        type: 'value',
        minInterval: 1,
        axisLine: {
          show: false,
        },
        axisTick: {
          show: false,
        },
        axisLabel: {
          ...theme.xAxis.axisLabel,
          fontSize: 10,
          formatter: (value: number) => `${Math.trunc(Number(value || 0))}`,
        },
        splitLine: {
          lineStyle: {
            color: '#e5e5e5',
            type: 'solid',
          },
        },
      },
      yAxis: {
        type: 'category',
        data: modelNames,
        axisLine: {
          show: false,
        },
        axisTick: {
          show: false,
        },
        axisLabel: {
          ...theme.yAxis.axisLabel,
          fontSize: 11,
        },
      },
      series: [
        {
          type: 'bar',
          data: modelValues.map((value, idx) => ({
            value,
            itemStyle: {
              color: modelColors[idx],
              borderRadius: [0, 4, 4, 0],
            },
          })),
          barWidth: '50%',
          label: {
            show: true,
            position: 'right',
            fontSize: 11,
            color: '#6b6b6b',
            formatter: '{c}',
          },
        },
      ],
    }, mode)
  }

  function updateSuccessDurationChart(mode: RenderMode = 'refresh') {
    if (!charts.successDuration) return

    const theme = getLineChartTheme()
    const seconds = chartData.value.successDuration.avgMs.map((ms) => (
      ms === null || ms === undefined ? null : Number((Number(ms) / 1000).toFixed(2))
    ))

    applyAnimatedOption('successDuration', {
      ...theme,
      tooltip: {
        ...theme.tooltip,
        trigger: 'axis',
        formatter: (params: any) => {
          if (!params || params.length === 0) return ''
          const param = params[0]
          const value = param.value === null || param.value === undefined ? '--' : `${param.value}s`
          return `<div style="font-weight: 600; margin-bottom: 4px;">${escapeDashboardTooltipText(param.axisValue)}</div>
            <div style="display: flex; justify-content: space-between; gap: 16px; align-items: center;">
              <span>${param.marker} ${escapeDashboardTooltipText(param.seriesName)}</span>
              <span style="font-weight: 600;">${escapeDashboardTooltipText(value)}</span>
            </div>`
        },
      },
      grid: {
        ...theme.grid,
        top: 32,
        bottom: 32,
      },
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.successDuration.labels,
      },
      yAxis: {
        ...theme.yAxis,
        axisLabel: {
          ...theme.yAxis.axisLabel,
          formatter: '{value}s',
        },
      },
      series: [
        createLineSeries('平均成功耗时', seconds as number[], chartColors.primary, {
          smooth: true,
          areaOpacity: 0.2,
          zIndex: 2,
        }),
      ],
    }, mode)
  }

  function updateResponseTimeChart(mode: RenderMode = 'refresh') {
    if (!charts.responseTime) return

    const theme = getLineChartTheme()
    const responseSeriesByModel = chartData.value.responseTime.modelTotalTimes
    const modelNames = Object.keys(responseSeriesByModel)
      .filter((modelName) => (responseSeriesByModel[modelName] || []).some((value) => Number(value || 0) > 0))

    if (modelNames.length === 0) {
      applyAnimatedOption('responseTime', {
        ...theme,
        grid: {
          ...theme.grid,
          top: 32,
          bottom: 32,
        },
        xAxis: {
          ...theme.xAxis,
          data: chartData.value.responseTime.labels,
        },
        yAxis: {
          ...theme.yAxis,
          axisLabel: {
            ...theme.yAxis.axisLabel,
            formatter: '{value}s',
          },
        },
        series: [],
      }, mode)
      return
    }

    const series = modelNames.map((modelName) => {
      const color = getModelColor(modelName)
      const seconds = (responseSeriesByModel[modelName] || []).map((ms) => (
        ms === null || ms === undefined ? null : Number((Number(ms) / 1000).toFixed(2))
      ))
      return createLineSeries(modelName, seconds as number[], color, {
        smooth: true,
        areaOpacity: 0.15,
        zIndex: 2,
      })
    })

    applyAnimatedOption('responseTime', {
      ...theme,
      color: modelNames.map((modelName) => getModelColor(modelName)),
      tooltip: {
        ...theme.tooltip,
        trigger: 'axis',
        formatter: (params: any) => {
          if (!params || params.length === 0) return ''
          let result = `<div style="font-weight: 600; margin-bottom: 4px;">${escapeDashboardTooltipText(params[0].axisValue)}</div>`
          params.forEach((item: any) => {
            result += `<div style="display: flex; justify-content: space-between; gap: 16px; align-items: center;">
              <span>${item.marker} ${escapeDashboardTooltipText(item.seriesName)}</span>
              <span style="font-weight: 600;">${escapeDashboardTooltipText(item.value || 0)}s</span>
            </div>`
          })
          return result
        },
      },
      legend: {
        ...theme.legend,
        data: modelNames,
        top: 0,
        right: 0,
        type: 'scroll',
        pageIconSize: 10,
        pageTextStyle: {
          fontSize: 10,
        },
      },
      grid: {
        ...theme.grid,
        top: modelNames.length > 5 ? 56 : 48,
        bottom: 32,
      },
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.responseTime.labels,
      },
      yAxis: {
        ...theme.yAxis,
        axisLabel: {
          ...theme.yAxis.axisLabel,
          formatter: '{value}s',
        },
      },
      series,
    }, mode)
  }

  return {
    stats,
    callStats,
    dashboardDataReady,
    dashboardLoading: computed(() => dashboardDataQuery.loading.value || !dashboardLoadSettled.value),
    dashboardLoadError: dashboardDataQuery.error,
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
    dashboardStaleWarning,
    dashboardVersion,
    hourlyRequestsChartRef,
    trendChartRef,
    successRateChartRef,
    responseTimeChartRef,
    successDurationChartRef,
    modelChartRef,
    modelRankChartRef,
  }
}
