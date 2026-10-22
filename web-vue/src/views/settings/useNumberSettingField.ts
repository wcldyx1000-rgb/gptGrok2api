// Ported from yukkcat/chatgpt2api (AGPL-3.0-only); ranges come from local call sites instead of backend metadata.
import { computed, getCurrentScope, onScopeDispose, ref, shallowReactive, watch, type ComputedRef, type Ref } from 'vue'
import type { NumberSettingOptions } from './settingsView'

export type NumberSettingField = {
  input: Ref<string>
  update: (value: string) => void
  min: ComputedRef<number>
  max: ComputedRef<number | undefined>
  step: ComputedRef<number | 'any'>
  /** 输入框占位：显示默认值。 */
  placeholder: ComputedRef<string>
  /** 非法时的红字提示；合法为空串。 */
  error: ComputedRef<string>
  isValid: ComputedRef<boolean>
}

export type NumberSettingFieldOptions = NumberSettingOptions & {
  /** 返回 false 时不校验（所在功能被关闭时不阻塞保存）。 */
  enabled?: () => boolean
}

// 页面上所有数值设置字段的登记表：任一字段非法时设置页禁用「保存」。字段随所属组件作用域销毁而注销。
const registeredFields = shallowReactive(new Set<NumberSettingField>())

export const hasInvalidNumberSettings = computed(() => (
  Array.from(registeredFields).some((field) => !field.isValid.value)
))

export function validateNumberSettingInput(raw: string, options: NumberSettingOptions = {}): string {
  const text = raw.trim()
  if (!text) return '请输入数值'
  const value = Number(text)
  if (!Number.isFinite(value)) return '请输入有效数值'
  if (options.integer && !Number.isInteger(value)) return '请输入整数'
  const min = options.min ?? 0
  if (value < min) return `不能小于 ${min}`
  if (typeof options.max === 'number' && value > options.max) return `不能大于 ${options.max}`
  return ''
}

export function useNumberSettingField(
  getter: () => number,
  setter: (value: number) => void,
  options: NumberSettingFieldOptions = {},
): NumberSettingField {
  const input = ref('')
  const min = computed(() => options.min ?? 0)
  const max = computed(() => options.max)
  const step = computed(() => (options.integer ? 1 : 'any') as number | 'any')
  const placeholder = computed(() => (options.fallback == null ? '' : String(options.fallback)))
  const error = computed(() => {
    if (options.enabled && !options.enabled()) return ''
    return validateNumberSettingInput(input.value, options)
  })
  const isValid = computed(() => !error.value)

  watch(getter, (value) => {
    const next = String(value)
    if (input.value !== next) {
      input.value = next
    }
  }, { immediate: true })

  // 非法输入保留在输入框里并提示错误，不再静默钳位；只有合法值才写回设置。
  const update = (value: string) => {
    input.value = value
    if (validateNumberSettingInput(value, options)) return
    setter(Number(value))
  }

  const field: NumberSettingField = { input, update, min, max, step, placeholder, error, isValid }
  registeredFields.add(field)
  if (getCurrentScope()) onScopeDispose(() => registeredFields.delete(field))
  return field
}
