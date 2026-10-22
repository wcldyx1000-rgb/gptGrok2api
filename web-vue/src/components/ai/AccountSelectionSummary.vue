<template>
  <div class="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
    <Checkbox
      :model-value="allSelected"
      @update:model-value="emit('toggle-all', $event)"
    >
      全选当前结果
    </Checkbox>
    <MetaChip>
      账号总数 {{ totalCount }}
    </MetaChip>
    <MetaChip>
      已选 {{ selectedCount }}
    </MetaChip>
    <button
      v-if="matchingCount > 0 && allSelected && selectedCount < matchingCount"
      type="button"
      class="text-primary underline-offset-2 hover:underline disabled:opacity-60"
      :disabled="selectingMatching"
      @click="emit('select-all-matching')"
    >
      {{ selectingMatching ? '正在选择...' : `选择全部 ${matchingCount} 个筛选结果（含其它分页）` }}
    </button>
    <MetaChip v-else-if="crossPage && selectedCount > 0">
      已跨分页选中筛选结果
    </MetaChip>
    <ViewModeSwitch
      :model-value="viewMode"
      list-label="列表视图"
      cards-label="卡片视图"
      @update:model-value="emit('update:viewMode', $event)"
    />
  </div>
</template>

<script setup lang="ts">
import { Checkbox, ViewModeSwitch } from 'nanocat-ui'
import MetaChip from './MetaChip.vue'

withDefaults(defineProps<{
  allSelected: boolean
  totalCount: number
  selectedCount: number
  viewMode: 'list' | 'cards'
  /** 当前筛选结果总数；传入后在“全选当前页”时提供跨分页全选入口。 */
  matchingCount?: number
  selectingMatching?: boolean
  crossPage?: boolean
}>(), {
  matchingCount: 0,
  selectingMatching: false,
  crossPage: false,
})

const emit = defineEmits<{
  (e: 'toggle-all', value: boolean): void
  (e: 'select-all-matching'): void
  (e: 'update:viewMode', value: 'list' | 'cards'): void
}>()
</script>
