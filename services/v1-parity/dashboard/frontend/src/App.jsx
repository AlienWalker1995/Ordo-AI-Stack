// App shell: dark-theme header with the hardware stat bar, a tab nav, and one panel
// per section. All eleven tabs are fully ported from the legacy vanilla-JS shell. Active
// tab is synced to the URL hash (deep-linkable, matching the legacy hash routing).
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import HwStatBar from './components/HwStatBar.jsx'
import { ToastProvider } from './components/Toast.jsx'

// Tab panels are code-split: each becomes its own lazily-loaded chunk so the initial
// bundle only ships the shell + the first visible tab. HwStatBar and ToastProvider stay
// eager (always visible). React.lazy needs a default export, which every *Tab has.
const ServicesTab = lazy(() => import('./components/ServicesTab.jsx'))
const McpTab = lazy(() => import('./components/McpTab.jsx'))
const GrafanaTab = lazy(() => import('./components/GrafanaTab.jsx'))
const DependenciesTab = lazy(() => import('./components/DependenciesTab.jsx'))
const ModelsTab = lazy(() => import('./components/ModelsTab.jsx'))
const ModelctlTab = lazy(() => import('./components/ModelctlTab.jsx'))
const HardwareTab = lazy(() => import('./components/HardwareTab.jsx'))
const ThroughputTab = lazy(() => import('./components/ThroughputTab.jsx'))
const OrchestrationTab = lazy(() => import('./components/OrchestrationTab.jsx'))
const RagTab = lazy(() => import('./components/RagTab.jsx'))
const ComfyTab = lazy(() => import('./components/ComfyTab.jsx'))

// Calm loading placeholder while a tab chunk resolves — muted tone, no glow/pulse spam,
// reduced-motion friendly (a static label rather than a spinner).
function TabFallback() {
  return (
    <div className="py-10 text-center text-caption text-muted" role="status" aria-live="polite">
      Loading…
    </div>
  )
}

const TABS = [
  { id: 'services', label: 'Services', Component: ServicesTab },
  { id: 'models', label: 'Models', Component: ModelsTab },
  { id: 'modelctl', label: 'Model', Component: ModelctlTab },
  { id: 'mcp', label: 'MCP', Component: McpTab },
  { id: 'gpu', label: 'Hardware', Component: HardwareTab },
  { id: 'throughput', label: 'Throughput', Component: ThroughputTab },
  { id: 'orchestration', label: 'Orchestration', Component: OrchestrationTab },
  { id: 'rag', label: 'RAG', Component: RagTab },
  { id: 'comfyui', label: 'ComfyUI', Component: ComfyTab },
  { id: 'grafana', label: 'Grafana', Component: GrafanaTab },
  { id: 'dependencies', label: 'Dependencies', Component: DependenciesTab },
]

const TAB_IDS = new Set(TABS.map((t) => t.id))

function initialTab() {
  const hash = (location.hash || '').replace(/^#/, '')
  return TAB_IDS.has(hash) ? hash : 'services'
}

export default function App() {
  const [active, setActive] = useState(initialTab)
  const tabRefs = useRef([])

  useEffect(() => {
    const onHash = () => {
      const hash = (location.hash || '').replace(/^#/, '')
      if (TAB_IDS.has(hash)) setActive(hash)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const select = (id) => {
    setActive(id)
    history.replaceState(null, '', '#' + id)
  }

  // Roving-tabindex keyboard nav: Arrow keys wrap, Home/End jump to ends. Moving
  // selection also moves focus to the newly-selected tab (WAI-ARIA tabs pattern).
  const onTabKeyDown = (e) => {
    const idx = TABS.findIndex((t) => t.id === active)
    if (idx < 0) return
    let next = null
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % TABS.length
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + TABS.length) % TABS.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = TABS.length - 1
    if (next == null) return
    e.preventDefault()
    select(TABS[next].id)
    tabRefs.current[next]?.focus()
  }

  const ActivePanel = (TABS.find((t) => t.id === active) || TABS[0]).Component

  return (
    <ToastProvider>
      <div className="mx-auto max-w-container animate-fade-up px-6 py-8">
        <header className="mb-8 flex items-start gap-6">
          <div className="min-w-0 flex-1">
            <h1 className="font-display text-display text-fg">
              Ordo AI Stack
            </h1>
            <p className="mt-2 text-body text-muted">
              Control interface: services, models, and hardware for the local AI stack.
            </p>
          </div>
        </header>

        <HwStatBar />

        <nav
          className="mb-5 mt-4 flex flex-wrap gap-1 border-b border-border max-md:flex-nowrap max-md:overflow-x-auto max-md:[scrollbar-width:none]"
          role="tablist"
          aria-label="Dashboard sections"
          onKeyDown={onTabKeyDown}
        >
          {TABS.map((t, i) => {
            const isActive = active === t.id
            return (
              <button
                key={t.id}
                ref={(el) => { tabRefs.current[i] = el }}
                type="button"
                className={
                  'shrink-0 cursor-pointer rounded-t-sm border-b-2 px-4 py-2 font-semibold tracking-[0.01em] transition-colors hover:bg-white/[0.03] ' +
                  (isActive
                    ? 'border-accent text-accent'
                    : 'border-transparent text-fg-muted hover:text-fg')
                }
                id={`tab-${t.id}`}
                role="tab"
                aria-selected={isActive}
                aria-controls={`panel-${t.id}`}
                tabIndex={isActive ? 0 : -1}
                onClick={() => select(t.id)}
              >
                {t.label}
              </button>
            )
          })}
        </nav>

        <main
          id={`panel-${active}`}
          role="tabpanel"
          aria-labelledby={`tab-${active}`}
          tabIndex={0}
        >
          <Suspense fallback={<TabFallback />}>
            <ActivePanel />
          </Suspense>
        </main>
      </div>
    </ToastProvider>
  )
}
