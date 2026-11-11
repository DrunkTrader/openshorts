// Theme: localStorage.os_theme ('light' | 'dark'), else prefers-color-scheme.
// The same logic is inlined in index.html so the first paint is already right.
const KEY = 'os_theme'
const mq = () => window.matchMedia('(prefers-color-scheme: dark)')

export function storedTheme() {
  try { const v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null } catch { return null }
}

export function currentTheme() {
  return storedTheme() || (mq().matches ? 'dark' : 'light')
}

export function applyTheme(theme) {
  const root = document.documentElement
  root.dataset.theme = theme
  document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', theme)
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#0b0b0c' : '#faf9f7')
}

export function setTheme(theme) {
  try { localStorage.setItem(KEY, theme) } catch { /* private mode: still apply for this session */ }
  applyTheme(theme)
}

// Follow the OS until the user picks explicitly.
export function watchSystemTheme() {
  const m = mq()
  const on = () => { if (!storedTheme()) applyTheme(m.matches ? 'dark' : 'light') }
  m.addEventListener('change', on)
  return () => m.removeEventListener('change', on)
}
