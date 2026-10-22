<!-- Ported from yukkcat/chatgpt2api (AGPL-3.0-only). 数值输入：min/max/默认值占位 + 内联错误提示。 -->
<template>
  <div class="space-y-1">
    <input
      :value="field.input.value"
      type="number"
      class="ui-input-sm w-full"
      :class="{ '!border-rose-500': !field.isValid.value }"
      :min="field.min.value"
      :max="field.max.value"
      :step="field.step.value"
      :placeholder="placeholder ?? field.placeholder.value"
      :disabled="disabled"
      :aria-invalid="!field.isValid.value"
      @input="handleInput"
    />
    <p v-if="field.error.value" class="text-xs text-rose-600 dark:text-rose-400" role="alert">
      {{ field.error.value }}
    </p>
  </div>
</template>

<script setup lang="ts">
import type { NumberSettingField } from './useNumberSettingField'

const props = withDefaults(defineProps<{
  field: NumberSettingField
  disabled?: boolean
  /** 覆盖默认占位（默认取 field.placeholder）。 */
  placeholder?: string
}>(), {
  disabled: false,
  placeholder: undefined,
})

function handleInput(event: Event) {
  props.field.update((event.target as HTMLInputElement).value)
}
</script>
