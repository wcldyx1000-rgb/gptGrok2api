import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { nanocatZhCN, setNanocatLocale } from 'nanocat-ui'
import router from './router'
import { resolveLoginRedirect } from './router/redirect'
import { setUnauthorizedHandler } from './api/client'
import { useAuthStore } from './stores/auth'
import { applyThemeMode, getStoredThemeMode } from './lib/theme'
import { registerLocalIcons } from './lib/icons'
import App from './App.vue'
import './style.css'
import './styles/features.css'

registerLocalIcons()
setNanocatLocale(nanocatZhCN)
applyThemeMode(getStoredThemeMode())

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)

setUnauthorizedHandler(() => {
  const authStore = useAuthStore()
  authStore.clearIdentity()
  // 记录当前页，登录成功后回到原页面。
  const current = router.currentRoute.value
  const redirect = current.name === 'login' ? '' : resolveLoginRedirect(current.fullPath)
  void router.replace(redirect ? { name: 'login', query: { redirect } } : { name: 'login' }).catch(() => {})
})

app.use(router)

app.mount('#app')
