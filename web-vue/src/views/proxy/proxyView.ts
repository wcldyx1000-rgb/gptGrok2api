import type { ActionMenuItem } from 'nanocat-ui'
import { parseProxyReference, serializeProxyReference, type ProxyGroup, type ProxyNode, type ProxyTestResult } from '@/api/proxy'
import { actionMenuGroups } from '@/components/ai/menuItems'

export type DefaultProxyMode = 'direct' | 'group' | 'custom'
export type FallbackProxyMode = 'off' | DefaultProxyMode

export const defaultProxyModeOptions = [
  { label: '直连', value: 'direct' },
  { label: '代理组', value: 'group' },
  { label: '自定义代理', value: 'custom' },
] as const

export const fallbackProxyModeOptions = [
  { label: '关闭', value: 'off' },
  { label: '直连', value: 'direct' },
  { label: '代理组', value: 'group' },
  { label: '自定义代理', value: 'custom' },
] as const

const defaultProxyModes = new Set<string>(defaultProxyModeOptions.map((item) => item.value))
const fallbackProxyModes = new Set<string>(fallbackProxyModeOptions.map((item) => item.value))

export function toDefaultProxyMode(value: string | string[]): DefaultProxyMode {
  const raw = Array.isArray(value) ? value[0] : value
  return defaultProxyModes.has(raw) ? raw as DefaultProxyMode : 'direct'
}

export function toFallbackProxyMode(value: string | string[]): FallbackProxyMode {
  const raw = Array.isArray(value) ? value[0] : value
  return fallbackProxyModes.has(raw) ? raw as FallbackProxyMode : 'off'
}

export function proxyGroupReference(group: Pick<ProxyGroup, 'id'>) {
  return serializeProxyReference('group', group.id)
}

function signatureValue(value: unknown): string {
  return String(value ?? '').trim().replaceAll('|', '/')
}

function boundedSignatureText(value: unknown, limit = 160): string {
  const text = signatureValue(value)
  if (text.length <= limit) return text
  return `${text.length}:${text.slice(0, limit)}:${text.slice(-24)}`
}

function proxyNodeSignature(node: ProxyNode) {
  return [
    node.id,
    node.name,
    boundedSignatureText(node.url, 96),
    node.enabled !== false ? 1 : 0,
    node.image_concurrency_limit,
    node.last_latency_ms,
    node.fail_count,
    boundedSignatureText(node.last_error),
    node.last_checked_at,
    node.last_error_at,
    node.cooldown_until,
    boundedSignatureText(node.notes),
  ].map(signatureValue).join(',')
}

function testingKeyForGroup(group: ProxyGroup, testingKey: string) {
  if (testingKey === `group:${group.id}:all`) return testingKey
  return group.nodes.some((node) => testingKey === proxyNodeTestKey(group, node)) ? testingKey : ''
}

export function proxyGroupRowSignature(group: ProxyGroup, testingKey: string, savingGroupId: string, deletingGroupId: string) {
  return [
    group.id,
    group.name,
    group.enabled !== false ? 1 : 0,
    group.strategy,
    group.rotation_interval_minutes,
    boundedSignatureText(group.notes),
    group.nodes.map(proxyNodeSignature).join(';'),
    testingKeyForGroup(group, testingKey),
    savingGroupId === group.id ? savingGroupId : '',
    deletingGroupId === group.id ? deletingGroupId : '',
  ].map(signatureValue).join('|')
}

export function proxyGroupActionItems(
  group: ProxyGroup,
  testingKey: string,
  savingGroupId: string,
  deletingGroupId: string,
): ActionMenuItem[] {
  const allKey = `group:${group.id}:all`
  return actionMenuGroups(
    [
      {
        key: 'test-all',
        label: testingKey === allKey ? '检测中...' : '检测全部节点',
        disabled: testingKey === allKey || group.nodes.length === 0,
      },
    ],
    [
      {
        key: 'toggle-enabled',
        label: savingGroupId === group.id
          ? '处理中...'
          : group.enabled ? '停用代理组' : '启用代理组',
        disabled: savingGroupId === group.id,
      },
    ],
    [
      {
        key: 'delete',
        label: deletingGroupId === group.id ? '删除中...' : '删除代理组',
        danger: true,
        disabled: deletingGroupId === group.id,
      },
    ],
  )
}

export function proxyGroupOptions(
  groups: readonly ProxyGroup[],
  selectedId = '',
) {
  const rows = groups.map((group) => ({
    label: `${group.enabled === false ? '停用 · ' : ''}${group.name || group.id}${Array.isArray(group.nodes) ? ` · ${group.nodes.length} 个节点` : ''}`,
    value: group.id,
  }))
  if (selectedId && !rows.some((item) => item.value === selectedId)) {
    rows.unshift({ label: `未知代理组 · ${selectedId}`, value: selectedId })
  }
  return [
    { label: '选择代理组', value: '' },
    ...rows,
  ]
}

export function buildProxyPreview(
  mode: FallbackProxyMode,
  selectedGroupId: string,
  customInput: string,
  groups: readonly Pick<ProxyGroup, 'id' | 'name'>[],
) {
  if (mode === 'off') return '关闭'
  if (mode === 'direct') return '直连'
  if (mode === 'group') {
    const group = groups.find((item) => item.id === selectedGroupId)
    return selectedGroupId ? `代理组：${group?.name || selectedGroupId}` : '代理组：未选择'
  }
  return customInput || '自定义代理：未填写'
}

export function normalizeDefaultProxyForCompare(value: unknown) {
  const reference = parseProxyReference(value)
  if (reference.mode === 'global' || reference.mode === 'direct') return 'direct'
  if (reference.mode === 'group') return serializeProxyReference('group', reference.value)
  if (reference.mode === 'profile') return String(value || '').trim()
  return reference.value.trim()
}

export function proxyNodeTestKey(group: Pick<ProxyGroup, 'id'>, node: Pick<ProxyNode, 'id'>) {
  return `group:${group.id}:${node.id}`
}

export function isProxyNodeTesting(group: Pick<ProxyGroup, 'id'>, node: Pick<ProxyNode, 'id'>, testingKey: string) {
  return testingKey === `group:${group.id}:all` || testingKey === proxyNodeTestKey(group, node)
}

export function proxyNodeTestSummary(
  group: Pick<ProxyGroup, 'id'>,
  node: ProxyNode,
  testResults: Readonly<Record<string, ProxyTestResult>>,
  testingKey: string,
) {
  if (isProxyNodeTesting(group, node, testingKey)) return '检测中...'
  const result = testResults[proxyNodeTestKey(group, node)]
  if (result?.ok) return `HTTP ${result.status || '-'} · ${result.latency_ms || 0}ms`
  if (result && !result.ok) return result.error || '检测失败'
  if (node.last_error) return node.last_error
  if (node.last_checked_at) return `${node.last_latency_ms || 0}ms`
  return '尚未测试'
}

export function proxyNodeTestClass(
  group: Pick<ProxyGroup, 'id'>,
  node: ProxyNode,
  testResults: Readonly<Record<string, ProxyTestResult>>,
  testingKey: string,
) {
  if (isProxyNodeTesting(group, node, testingKey)) return 'text-sky-600'
  const result = testResults[proxyNodeTestKey(group, node)]
  if (result) return result.ok ? 'text-emerald-600' : 'text-rose-600'
  if (node.last_error) return 'text-rose-600'
  if (node.last_checked_at) return 'text-emerald-600'
  return 'text-muted-foreground'
}

// ---- Ported from yukkcat/chatgpt2api (AGPL-3.0-only): 出口时区 / 语言分组选项 ----
/** 时区输入的即时提示：当前 UTC 偏移（含夏令时）；空串表示未填写，null 表示浏览器无法识别。 */
export function proxyNodeTimezoneHint(timezone: string): string | null {
  const value = timezone.trim()
  if (!value) return ''
  try {
    const part = new Intl.DateTimeFormat('en-US', { timeZone: value, timeZoneName: 'longOffset' })
      .formatToParts(new Date())
      .find((item) => item.type === 'timeZoneName')
    const offset = (part?.value || 'GMT').replace('GMT', '')
    return `UTC${offset || '+00:00'}`
  } catch {
    return null
  }
}

type ProxyLocaleRegion = { label: string, timezone: string, language: string }

/** 常用出口地区：选时区时自动带出当地浏览器语言。时区名与后端 utils/locale_env.py 的长名称表保持一致。 */
const PROXY_LOCALE_REGIONS: ReadonlyArray<{ group: string, regions: ProxyLocaleRegion[] }> = [
  {
    group: '亚洲',
    regions: [
      { label: '中国大陆', timezone: 'Asia/Shanghai', language: 'zh-CN' },
      { label: '台湾', timezone: 'Asia/Taipei', language: 'zh-TW' },
      { label: '香港', timezone: 'Asia/Hong_Kong', language: 'zh-HK' },
      { label: '澳门', timezone: 'Asia/Macau', language: 'zh-MO' },
      { label: '日本', timezone: 'Asia/Tokyo', language: 'ja-JP' },
      { label: '韩国', timezone: 'Asia/Seoul', language: 'ko-KR' },
      { label: '新加坡', timezone: 'Asia/Singapore', language: 'en-SG' },
      { label: '马来西亚', timezone: 'Asia/Kuala_Lumpur', language: 'ms-MY' },
      { label: '泰国', timezone: 'Asia/Bangkok', language: 'th-TH' },
      { label: '越南', timezone: 'Asia/Ho_Chi_Minh', language: 'vi-VN' },
      { label: '菲律宾', timezone: 'Asia/Manila', language: 'en-PH' },
      { label: '印度尼西亚', timezone: 'Asia/Jakarta', language: 'id-ID' },
      { label: '印度', timezone: 'Asia/Kolkata', language: 'en-IN' },
      { label: '阿联酋', timezone: 'Asia/Dubai', language: 'en-AE' },
    ],
  },
  {
    group: '美洲',
    regions: [
      { label: '美国东部', timezone: 'America/New_York', language: 'en-US' },
      { label: '美国中部', timezone: 'America/Chicago', language: 'en-US' },
      { label: '美国山地', timezone: 'America/Denver', language: 'en-US' },
      { label: '美国亚利桑那', timezone: 'America/Phoenix', language: 'en-US' },
      { label: '美国西部', timezone: 'America/Los_Angeles', language: 'en-US' },
      { label: '美国阿拉斯加', timezone: 'America/Anchorage', language: 'en-US' },
      { label: '美国夏威夷', timezone: 'Pacific/Honolulu', language: 'en-US' },
      { label: '加拿大东部', timezone: 'America/Toronto', language: 'en-CA' },
      { label: '加拿大西部', timezone: 'America/Vancouver', language: 'en-CA' },
      { label: '墨西哥', timezone: 'America/Mexico_City', language: 'es-MX' },
      { label: '巴西', timezone: 'America/Sao_Paulo', language: 'pt-BR' },
    ],
  },
  {
    group: '欧洲',
    regions: [
      { label: '英国', timezone: 'Europe/London', language: 'en-GB' },
      { label: '爱尔兰', timezone: 'Europe/Dublin', language: 'en-IE' },
      { label: '葡萄牙', timezone: 'Europe/Lisbon', language: 'pt-PT' },
      { label: '德国', timezone: 'Europe/Berlin', language: 'de-DE' },
      { label: '法国', timezone: 'Europe/Paris', language: 'fr-FR' },
      { label: '荷兰', timezone: 'Europe/Amsterdam', language: 'nl-NL' },
      { label: '比利时', timezone: 'Europe/Brussels', language: 'nl-BE' },
      { label: '西班牙', timezone: 'Europe/Madrid', language: 'es-ES' },
      { label: '意大利', timezone: 'Europe/Rome', language: 'it-IT' },
      { label: '瑞士', timezone: 'Europe/Zurich', language: 'de-CH' },
      { label: '瑞典', timezone: 'Europe/Stockholm', language: 'sv-SE' },
      { label: '波兰', timezone: 'Europe/Warsaw', language: 'pl-PL' },
      { label: '芬兰', timezone: 'Europe/Helsinki', language: 'fi-FI' },
      { label: '俄罗斯（莫斯科）', timezone: 'Europe/Moscow', language: 'ru-RU' },
    ],
  },
  {
    group: '大洋洲',
    regions: [
      { label: '澳大利亚东部', timezone: 'Australia/Sydney', language: 'en-AU' },
      { label: '澳大利亚墨尔本', timezone: 'Australia/Melbourne', language: 'en-AU' },
      { label: '澳大利亚西部', timezone: 'Australia/Perth', language: 'en-AU' },
      { label: '新西兰', timezone: 'Pacific/Auckland', language: 'en-NZ' },
    ],
  },
  {
    group: '其他',
    regions: [{ label: 'UTC', timezone: 'UTC', language: 'en-US' }],
  },
]

const DEFAULT_LOCALE_OPTION = { label: '默认', value: '' }

function withCurrentValue(groups: { label: string, options: { label: string, value: string }[] }[], value: string) {
  const known = groups.some((group) => group.options.some((option) => option.value === value))
  if (!value || known) return groups
  return [{ label: '当前值', options: [{ label: value, value }] }, ...groups]
}

/** 时区下拉分组；保存过但不在常用列表里的值单独列出，避免被静默替换。 */
export function proxyTimezoneGroups(current: string) {
  return withCurrentValue([
    { label: '', options: [DEFAULT_LOCALE_OPTION] },
    ...PROXY_LOCALE_REGIONS.map((item) => ({
      label: item.group,
      options: item.regions.map((region) => ({
        label: `${region.label} · ${region.timezone} · ${proxyNodeTimezoneHint(region.timezone) || ''}`,
        value: region.timezone,
      })),
    })),
  ], current)
}

function languageLabel(language: string) {
  try {
    const name = new Intl.DisplayNames(['zh-CN'], { type: 'language' }).of(language)
    return name && name !== language ? `${name} · ${language}` : language
  } catch {
    return language
  }
}

/** 语言下拉分组，取常用地区的语言去重。 */
export function proxyLanguageGroups(current: string) {
  const languages = [...new Set(PROXY_LOCALE_REGIONS.flatMap((item) => item.regions.map((region) => region.language)))]
  return withCurrentValue([
    { label: '', options: [DEFAULT_LOCALE_OPTION] },
    { label: '语言', options: languages.map((language) => ({ label: languageLabel(language), value: language })) },
  ], current)
}

/** 选中时区对应的当地语言；未收录的时区返回空串。 */
export function proxyRegionLanguage(timezone: string) {
  for (const item of PROXY_LOCALE_REGIONS) {
    const region = item.regions.find((entry) => entry.timezone === timezone)
    if (region) return region.language
  }
  return ''
}
