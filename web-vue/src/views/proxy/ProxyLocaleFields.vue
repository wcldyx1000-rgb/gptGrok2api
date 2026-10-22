<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). -->
<template>
  <label class="block text-xs" :class="timezoneClass">
    <span class="ui-field-label">
      {{ timezoneLabel }}
      <span v-if="timezoneHint" class="text-muted-foreground">· {{ timezoneHint }}</span>
    </span>
    <GroupedSelectMenu
      :model-value="timezone"
      :groups="timezoneGroups"
      :aria-label="timezoneLabel"
      placeholder="默认"
      selected-indicator="none"
      block
      @update:model-value="selectTimezone"
    />
  </label>
  <label class="block text-xs" :class="languageClass">
    <span class="ui-field-label">语言</span>
    <GroupedSelectMenu
      :model-value="language"
      :groups="languageGroups"
      aria-label="语言"
      placeholder="默认"
      selected-indicator="none"
      block
      @update:model-value="selectLanguage"
    />
  </label>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'

import {
  proxyLanguageGroups,
  proxyNodeTimezoneHint,
  proxyRegionLanguage,
  proxyTimezoneGroups,
} from '@/views/proxy/proxyView'

const props = withDefaults(defineProps<{
  timezone: string
  language: string
  timezoneLabel?: string
  timezoneClass?: string
  languageClass?: string
}>(), {
  timezoneLabel: '时区',
  timezoneClass: '',
  languageClass: '',
})

const emit = defineEmits<{
  'update:timezone': [value: string]
  'update:language': [value: string]
}>()

const timezoneGroups = computed(() => proxyTimezoneGroups(props.timezone))
const languageGroups = computed(() => proxyLanguageGroups(props.language))
const timezoneHint = computed(() => proxyNodeTimezoneHint(props.timezone) || '')

function firstValue(value: string | string[]) {
  return String((Array.isArray(value) ? value[0] : value) || '')
}

function selectTimezone(value: string | string[]) {
  const timezone = firstValue(value)
  emit('update:timezone', timezone)
  // 选“默认”时一并清空语言；选地区时带出当地语言，之后仍可单独改语言。
  emit('update:language', timezone ? (proxyRegionLanguage(timezone) || props.language) : '')
}

function selectLanguage(value: string | string[]) {
  emit('update:language', firstValue(value))
}
</script>
