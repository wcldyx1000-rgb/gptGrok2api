// Series builder ported from yukkcat/chatgpt2api (AGPL-3.0-only); adds the rate-limit series used here.
export type DashboardTrendSeriesData = {
  successRequests: number[]
  failedRequests: number[]
  rateLimitedRequests: number[]
  switchCount: number[]
}

type DashboardTrendColors = {
  success: string
  failure: string
  rateLimited: string
  switchAccount: string
}

type LineSeriesOptions = {
  areaOpacity: number
  lineStyle?: { type: 'solid' | 'dashed' | 'dotted'; width: number }
  zIndex: number
}

type LineSeriesFactory<T> = (
  name: string,
  data: number[],
  color: string,
  options: LineSeriesOptions,
) => T

/** 调用趋势曲线：成功 / 失败 / 限流，外加一条虚线的“切号”次数（有切号数据时才画）。 */
export function buildDashboardTrendSeries<T>(
  trend: DashboardTrendSeriesData,
  createLineSeries: LineSeriesFactory<T>,
  colors: DashboardTrendColors,
): T[] {
  const series = [
    createLineSeries('成功', trend.successRequests, colors.success, { areaOpacity: 0.25, zIndex: 1 }),
    createLineSeries('失败', trend.failedRequests, colors.failure, { areaOpacity: 0.3, zIndex: 2 }),
    createLineSeries('限流', trend.rateLimitedRequests, colors.rateLimited, { areaOpacity: 0.3, zIndex: 2 }),
  ]
  if (trend.switchCount.some((value) => value > 0)) {
    series.push(createLineSeries('切号', trend.switchCount, colors.switchAccount, {
      areaOpacity: 0,
      lineStyle: { type: 'dashed', width: 2 },
      zIndex: 3,
    }))
  }
  return series
}
