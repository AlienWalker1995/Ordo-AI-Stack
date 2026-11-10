// The viewer's colour theme. 'auto' follows the operating system; 'light' and 'dark' pin one.
// The choice is a per-viewer convenience kept in localStorage (it may be unavailable, in which case
// the page simply follows the OS). The resolved theme is written to <html data-theme>, which
// selects the palette in src/index.css.
import { createContext, useContext, useEffect, useState } from 'react'

const STORAGE_KEY = 'ordo.theme'
export const THEME_CHOICES = [
  { id: 'auto', label: 'Auto' },
  { id: 'light', label: 'Light' },
  { id: 'dark', label: 'Dark' },
]
const VALID = new Set(THEME_CHOICES.map((t) => t.id))
const LIGHT_QUERY = '(prefers-color-scheme: light)'

export function readThemeChoice() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return VALID.has(stored) ? stored : 'auto'
  } catch {
    return 'auto'
  }
}

function saveThemeChoice(choice) {
  try {
    localStorage.setItem(STORAGE_KEY, choice)
  } catch {
    // Storage blocked: the choice lasts for this page view only.
  }
}

function systemPrefersLight() {
  return typeof window.matchMedia === 'function' && window.matchMedia(LIGHT_QUERY).matches
}

/** 'light' or 'dark' for a choice, asking the OS when the choice is 'auto'. */
export function resolveTheme(choice) {
  if (choice === 'light' || choice === 'dark') return choice
  return systemPrefersLight() ? 'light' : 'dark'
}

/** Set the palette before the first render, so the page never flashes the other theme. */
export function applyThemeChoice(choice) {
  document.documentElement.dataset.theme = resolveTheme(choice)
}

const ThemeContext = createContext({ choice: 'auto', resolved: 'dark', setChoice: () => {} })

export function ThemeProvider({ children }) {
  const [choice, setChoiceState] = useState(readThemeChoice)
  const [resolved, setResolved] = useState(() => resolveTheme(choice))

  useEffect(() => {
    const apply = () => {
      const next = resolveTheme(choice)
      document.documentElement.dataset.theme = next
      setResolved(next)
    }
    apply()
    if (choice !== 'auto' || typeof window.matchMedia !== 'function') return undefined
    // Following the OS: switch when it does.
    const query = window.matchMedia(LIGHT_QUERY)
    query.addEventListener('change', apply)
    return () => query.removeEventListener('change', apply)
  }, [choice])

  const setChoice = (next) => {
    if (!VALID.has(next)) return
    saveThemeChoice(next)
    setChoiceState(next)
  }
  return <ThemeContext.Provider value={{ choice, resolved, setChoice }}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  return useContext(ThemeContext)
}
