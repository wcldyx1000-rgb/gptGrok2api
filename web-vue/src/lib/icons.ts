// Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
import { addCollection } from '@iconify/vue'
import { localLucideIcons } from './localLucideIcons.generated'

let localIconsRegistered = false

export function registerLocalIcons() {
  if (localIconsRegistered) return

  addCollection(localLucideIcons)
  localIconsRegistered = true
}
