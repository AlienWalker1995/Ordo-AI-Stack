// App shell: dark-theme header with the hardware stat bar, a tab nav, and one panel
// per section. Services is fully ported; every other tab is a stub seam a later
// iteration fills. Active tab is synced to the URL hash (deep-linkable, matching the
// legacy hash routing).
import { useEffect, useState } from 'react'
import HwStatBar from './components/HwStatBar.jsx'
import ServicesTab from './components/ServicesTab.jsx'
import { ToastProvider } from './components/Toast.jsx'
import {
  McpTab, ModelsTab, HardwareTab, ThroughputTab,
  OrchestrationTab, GrafanaTab, RagTab, ComfyTab, DependenciesTab,
} from './components/StubTab.jsx'

// Services first — it is the fully-implemented tab this iteration ships.
const TABS = [
  { id: 'services', label: '⚡ Services', Component: ServicesTab },
  { id: 'models', label: '📦 Models', Component: ModelsTab },
  { id: 'modelctl', label: '⚙️ Model', Component: ModelsTab },
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
      <div className="container">
        <header>
          <div className="header-brand">
            <h1>Ordo AI Stack</h1>
            <p className="subtitle">Control interface — services, models, and hardware for the local AI stack.</p>
          </div>
        </header>

        <HwStatBar />

        <nav className="tab-bar" role="tablist" aria-label="Dashboard sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`tab-btn ${active === t.id ? 'active' : ''}`.trim()}
              role="tab"
              aria-selected={active === t.id}
              onClick={() => select(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <main>
          <ActivePanel />
        </main>
      </div>
    </ToastProvider>
  )
}
