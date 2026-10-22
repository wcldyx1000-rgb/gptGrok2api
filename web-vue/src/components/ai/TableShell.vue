<template>
  <div class="table-shell">
    <div class="scrollbar-slim table-shell__scroll" :class="{ 'table-shell__scroll--sticky': stickyHeader }">
      <slot />
    </div>
    <div
      v-if="$slots.footer"
      class="table-shell__footer"
      :class="{ 'table-shell__footer--bordered': footerBorder }"
    >
      <slot name="footer" />
    </div>
  </div>
</template>

<script setup lang="ts">
// 本地实现的表头吸顶：开启后表格区域内部纵向滚动，thead 固定在顶部（nanocat-ui 0.1.6 的 TableShell 没有该能力）。
withDefaults(defineProps<{
  footerBorder?: boolean
  stickyHeader?: boolean
}>(), {
  footerBorder: false,
  stickyHeader: false,
})
</script>

<style scoped>
.table-shell {
  min-width: 0;
}

.table-shell__scroll {
  overflow-x: auto;
}

.table-shell__scroll--sticky {
  max-height: min(70vh, 56rem);
  overflow-y: auto;
}

.table-shell__scroll--sticky :deep(thead th) {
  position: sticky;
  top: 0;
  z-index: 2;
  background: hsl(var(--card, var(--background)));
  box-shadow: inset 0 -1px 0 hsl(var(--border));
}

.table-shell__footer {
  padding: var(--table-shell-footer-padding, 12px 16px);
}

.table-shell__footer--bordered {
  border-top: 1px solid hsl(var(--border));
}
</style>
