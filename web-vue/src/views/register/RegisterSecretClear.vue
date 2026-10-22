<template>
  <Checkbox
    v-if="visible"
    class="register-secret-clear"
    :model-value="modelValue"
    :disabled="disabled"
    @update:model-value="value => emit('update:modelValue', Boolean(value))"
  >
    保存时清除已配置的值
  </Checkbox>
</template>

<script setup lang="ts">
import { Checkbox } from 'nanocat-ui'

// 敏感字段只写不读：后端返回 `<key>_set` 表示已配置，留空保存沿用旧值，
// 只有勾选这里（提交 `clear_<key>: true`）才会真正清除。
withDefaults(defineProps<{
  modelValue?: boolean
  /** 已配置且本次没有输入新值时才显示 */
  visible?: boolean
  disabled?: boolean
}>(), {
  modelValue: false,
  visible: false,
  disabled: false,
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
}>()
</script>

<style scoped>
.register-secret-clear {
  margin-top: 4px;
  font-size: 12px;
}
</style>
