// App shell: dark-theme header with the hardware stat bar, a tab nav, and one panel
// per section. All eleven tabs are fully ported from the legacy vanilla-JS shell. Active
// tab is synced to the URL hash (deep-linkable, matching the legacy hash routing).
import { useEffect, useState } from 'react'
import HwStatBar from './components/HwStatBar.jsx'
import ServicesTab from './components/ServicesTab.jsx'
import McpTab from './components/McpTab.jsx'
import GrafanaTab from './components/GrafanaTab.jsx'
import DependenciesTab from './components/DependenciesTab.jsx'
import ModelsTab from './components/ModelsTab.jsx'
import ModelctlTab from './components/ModelctlTab.jsx'
import HardwareTab from './components/HardwareTab.jsx'
import ThroughputTab from './components/ThroughputTab.jsx'
import OrchestrationTab from './components/OrchestrationTab.jsx'
import RagTab from './components/RagTab.jsx'
import ComfyTab from './components/ComfyTab.jsx'
import { ToastProvider } from './components/Toast.jsx'

const TABS = [
  { id: 'services', label: '⚡ Services', Component: ServicesTab },
  { id: 'models', label: '📦 Models', Component: ModelsTab },
  { id: 'modelctl', label: '⚙️ Model', Component: ModelctlTab },
  { id: 'mcp', label: '🧩 MCP', Component: McpTab },
  { id: 'gpu', label: '🖥️ Hardware', Component: HardwareTab },
  { id: 'throughput', label: '📈 Throughput', Component: ThroughputTab },
  { id: 'orchestration', label: '🛠️ Orchestration', Component: OrchestrationTab },
  { id: 'rag', label: '🔍 RAG', Component: RagTab },
  { id: 'comfyui', label: '🎨 ComfyUI', Component: ComfyTab },
  { id: 'grafana', label: '📊 Grafana', Component: GrafanaTab },
  { id: 'dependencies', label: '🔗 Dependencies', Component: DependenciesTab },
]

const TAB_IDS = new Set(TABS.map((t) => t.id))

function initialTab() {
  const hash = (location.hash || '').replace(/^#/, '')
  return TAB_IDS.has(hash) ? hash : 'services'
}

export default function App() {
  const [active, setActive] = useState(initialTab)

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

  const ActivePanel = (TABS.find((t) => t.id === active) || TABS[0]).Component

  return (
    <ToastProvider>
      <div className="mx-auto max-w-container animate-fade-up px-6 py-8">
        <header className="mb-8 flex items-start gap-6">
          <div className="min-w-0 flex-1">
            <h1 className="font-display text-[2.5rem] font-bold uppercase leading-[1.15] tracking-[0.04em] text-fg">
              Ordo AI Stack
            </h1>
            <p className="mt-2 text-[0.8125rem] font-normal tracking-[0.01em] text-muted">
              Control interface — services, models, and hardware for the local AI stack.
            </p>
          </div>
        </header>

        <HwStatBar />

        <nav
          className="mb-5 mt-4 flex flex-wrap gap-1 border-b border-border max-md:flex-nowrap max-md:overflow-x-auto max-md:[scrollbar-width:none]"
          role="tablist"
          aria-label="Dashboard sections"
        >
          {TABS.map((t) => {
            const isActive = active === t.id
            return (
              <button
                key={t.id}
                type="button"
                className={
                  'shrink-0 cursor-pointer rounded-t-sm border-b-2 px-4 py-2 font-semibold tracking-[0.01em] transition-colors hover:bg-white/[0.03] ' +
                  (isActive
                    ? 'border-accent text-accent'
                    : 'border-transparent text-fg-muted hover:text-fg')
                }
                role="tab"
                aria-selected={isActive}
                onClick={() => select(t.id)}
              >
                {t.label}
              </button>
            )
          })}
        </nav>

        <main>
          <ActivePanel />
        </main>
      </div>
    </ToastProvider>
  )
}
