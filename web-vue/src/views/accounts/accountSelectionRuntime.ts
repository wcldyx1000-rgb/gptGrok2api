import { computed, ref, type ComputedRef, type Ref } from 'vue'

import type { Account } from '@/api/accounts'

type AccountSelectionRuntimeOptions = {
  accounts: Ref<Account[]>
  pagedAccounts: ComputedRef<Account[]>
}

export function useAccountSelectionRuntime(options: AccountSelectionRuntimeOptions) {
  const selectedIds = ref<string[]>([])
  // 跨分页全选筛选结果后，已选账号不一定在当前页，不能按当前页裁剪。
  const crossPageActive = ref(false)
  const selectedSet = computed(() => new Set(selectedIds.value))
  const selectedCount = computed(() => selectedIds.value.length)
  const allVisibleSelected = computed(() => {
    const visible = options.pagedAccounts.value
      .filter((item) => !item.is_demo)
      .map((item) => item.id)
    if (!visible.length) return false
    return visible.every((id) => selectedSet.value.has(id))
  })

  function pruneToCurrentAccounts() {
    if (!selectedIds.value.length || crossPageActive.value) return
    const existingIds = new Set(options.accounts.value.map((item) => item.id))
    selectedIds.value = selectedIds.value.filter((id) => existingIds.has(id))
  }

  function isSelected(accountId: string) {
    return selectedSet.value.has(accountId)
  }

  function toggleSelect(accountId: string, checked?: boolean) {
    const next = new Set(selectedIds.value)
    const shouldSelect = typeof checked === 'boolean' ? checked : !next.has(accountId)
    if (shouldSelect) {
      next.add(accountId)
    } else {
      next.delete(accountId)
    }
    selectedIds.value = Array.from(next)
  }

  function clearSelection() {
    selectedIds.value = []
    crossPageActive.value = false
  }

  /** 用后端解析出的整批账号 ID 替换当前选择（跨分页全选筛选结果）。 */
  function selectIds(ids: readonly string[]) {
    selectedIds.value = Array.from(new Set(ids.filter(Boolean)))
    crossPageActive.value = selectedIds.value.length > 0
  }

  function toggleSelectAllVisible(checked?: boolean) {
    const ids = options.pagedAccounts.value
      .filter((item) => !item.is_demo)
      .map((item) => item.id)
    const next = new Set(selectedIds.value)
    const shouldSelect = typeof checked === 'boolean' ? checked : !allVisibleSelected.value
    for (const id of ids) {
      if (shouldSelect) next.add(id)
      else next.delete(id)
    }
    selectedIds.value = Array.from(next)
  }

  function removeSelectedIds(ids: readonly string[]) {
    if (!ids.length || !selectedIds.value.length) return
    const removed = new Set(ids)
    selectedIds.value = selectedIds.value.filter((id) => !removed.has(id))
  }

  return {
    selectedIds,
    crossPageActive,
    selectIds,
    selectedSet,
    selectedCount,
    allVisibleSelected,
    pruneToCurrentAccounts,
    isSelected,
    toggleSelect,
    clearSelection,
    toggleSelectAllVisible,
    removeSelectedIds,
  }
}
