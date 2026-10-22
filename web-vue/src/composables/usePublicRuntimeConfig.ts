// Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
import { computed, ref } from 'vue'
import { settingsApi } from '@/api/settings'
import { useSettingsStore } from '@/stores/settings'
import type { Settings } from '@/types/api'

type PublicRuntimeView = {
  thirdPartyApps: Settings['third_party_apps']
  apiBaseUrl: string
}

const sharedView = ref<PublicRuntimeView | null>(null)
const isLoading = ref(false)
const loadError = ref<Error | null>(null)

const PUBLIC_RUNTIME_TTL_MS = 30_000
let loadedAt = 0
let generation = 0
let inflight: {
  generation: number
  promise: Promise<PublicRuntimeView | null>
} | null = null

function normalizeBaseUrl(value: string) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  try {
    const base = typeof window !== 'undefined' ? window.location.origin : undefined
    return new URL(raw, base).toString().replace(/\/+$/, '')
  } catch {
    return raw.replace(/\/+$/, '')
  }
}

function fallbackBaseUrl() {
  const configured = String(import.meta.env.VITE_API_URL || '').trim()
  if (configured) return normalizeBaseUrl(configured)
  return typeof window !== 'undefined' ? normalizeBaseUrl(window.location.origin) : ''
}

/** 公开运行时配置（接口地址、第三方应用）；30 秒内复用缓存，并发请求共用同一次读取。 */
export function usePublicRuntimeConfig() {
  const settingsStore = useSettingsStore()
  const apiBaseUrl = computed(() => (
    normalizeBaseUrl(settingsStore.settings?.basic?.base_url || sharedView.value?.apiBaseUrl || '')
    || fallbackBaseUrl()
  ))
  const thirdPartyApps = computed(() => sharedView.value?.thirdPartyApps || null)

  async function loadPublicRuntimeConfig(force = false) {
    if (!force && sharedView.value && Date.now() - loadedAt < PUBLIC_RUNTIME_TTL_MS) {
      return sharedView.value
    }
    if (!force && inflight?.generation === generation) return inflight.promise
    if (force) generation += 1

    const requestGeneration = generation
    isLoading.value = true
    const request = (async () => {
      try {
        const response = await settingsApi.getPublicRuntime()
        if (requestGeneration === generation) {
          sharedView.value = response
          loadedAt = Date.now()
          loadError.value = null
        }
        return requestGeneration === generation ? response : sharedView.value
      } catch (error) {
        if (requestGeneration === generation) {
          loadError.value = error instanceof Error ? error : new Error('Failed to load public runtime config')
        }
        return sharedView.value
      }
    })()
    inflight = { generation: requestGeneration, promise: request }

    try {
      return await request
    } finally {
      if (inflight?.promise === request) {
        inflight = null
        isLoading.value = false
      }
    }
  }

  return {
    apiBaseUrl,
    thirdPartyApps,
    isLoading,
    loadError,
    loadPublicRuntimeConfig,
  }
}
