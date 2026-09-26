// App shell: a compact header (name, five pages, search, settings) over one page at a time.
// The page is kept in the URL hash so it can be linked to; links to the retired tabs land on
// the page that absorbed them.
import { Component, lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import CommandPalette from './components/CommandPalette.jsx'
import LocalSignIn from './components/LocalSignIn.jsx'
import SettingsDrawer from './components/SettingsDrawer.jsx'
import { ToastProvider } from './components/Toast.jsx'

// One page's render error is contained to that page and cleared when the page changes.
class PageErrorBoundary extends Component {
  constructor(props) { super(props); this.state = { err: null } }
  static getDerivedStateFromError(err) { return { err } }
  render() {
    if (this.state.err) {
      return (
        <div className="rounded-md border border-border border-l-[3px] border-l-danger bg-danger/[0.05] px-4 py-6" role="alert">
          <div className="text-body font-semibold text-danger">This page hit an error and stopped rendering.</div>
          <div className="mt-1 text-caption text-muted">Switch pages and back to retry. {String(this.state.err?.message || this.state.err)}</div>
        </div>
      )
    }
    return this.props.children
  }
}

const PAGES = [
  { id: 'overview', label: 'Overview', Component: lazy(() => import('./pages/OverviewPage.jsx')) },
  { id: 'services', label: 'Services', Component: lazy(() => import('./pages/ServicesPage.jsx')) },
  { id: 'models', label: 'Models', Component: lazy(() => import('./pages/ModelsPage.jsx')) },
  { id: 'media', label: 'Media', Component: lazy(() => import('./pages/MediaPage.jsx')) },
  { id: 'performance', label: 'Performance', Component: lazy(() => import('./pages/PerformancePage.jsx')) },
]
const PAGE_IDS = new Set(PAGES.map((p) => p.id))

// Where each retired tab's content now lives.
const RETIRED_TABS = {
  modelctl: 'models', throughput: 'models', gpu: 'overview', orchestration: 'overview',
  rag: 'overview', dependencies: 'services', comfyui: 'media', grafana: 'performance', mcp: 'overview',
}

function pageFromHash() {
  const hash = (location.hash || '').replace(/^#/, '')
  if (PAGE_IDS.has(hash)) return hash
  return RETIRED_TABS[hash] || 'overview'
}

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

export default function App() {
  const [active, setActive] = useState(pageFromHash)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const tabRefs = useRef([])

  useEffect(() => {
    const onHash = () => {
      const page = pageFromHash()
      setActive(page)
      if ((location.hash || '').replace(/^#/, '') !== page) history.replaceState(null, '', '#' + page)
    }
    window.addEventListener('hashchange', onHash)
    // A retired tab's hash is rewritten to its new page so the address bar stays truthful.
    if ((location.hash || '').replace(/^#/, '') !== pageFromHash()) history.replaceState(null, '', '#' + pageFromHash())
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((o) => !o)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const navigate = useCallback((id) => {
    setActive(id)
    history.replaceState(null, '', '#' + id)
  }, [])
  const openSettings = useCallback(() => setSettingsOpen(true), [])
  const closeSettings = useCallback(() => setSettingsOpen(false), [])
  const closePalette = useCallback(() => setPaletteOpen(false), [])

  // WAI-ARIA tabs: arrows move and wrap, Home/End jump, focus follows selection.
  const onTabKeyDown = (e) => {
    const idx = PAGES.findIndex((p) => p.id === active)
    let next = null
    if (e.key === 'ArrowRight') next = (idx + 1) % PAGES.length
    else if (e.key === 'ArrowLeft') next = (idx - 1 + PAGES.length) % PAGES.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = PAGES.length - 1
    if (next == null) return
    e.preventDefault()
    navigate(PAGES[next].id)
    tabRefs.current[next]?.focus()
  }

  const ActivePage = (PAGES.find((p) => p.id === active) || PAGES[0]).Component

  return (
    <ToastProvider>
      {/* The band and its rule span the viewport; only the contents keep the page's width. */}
      <header className="sticky top-0 z-30 mb-5 border-b border-border-subtle bg-bg/95 backdrop-blur">
        <div className="mx-auto flex max-w-container flex-wrap items-center gap-x-6 gap-y-2 px-6 py-3 max-md:px-4">
          <span className="text-title font-bold tracking-[-0.01em] text-fg">Ordo</span>
          <nav role="tablist" aria-label="Pages" onKeyDown={onTabKeyDown}
               className="flex gap-1 max-md:order-last max-md:w-full max-md:overflow-x-auto max-md:[scrollbar-width:none]">
            {PAGES.map((p, i) => {
              const selected = p.id === active
              return (
                <button key={p.id} ref={(el) => { tabRefs.current[i] = el }} type="button" role="tab"
                        id={`tab-${p.id}`} aria-selected={selected} aria-controls="page" tabIndex={selected ? 0 : -1}
                        onClick={() => navigate(p.id)}
                        className={'h-8 shrink-0 rounded-sm px-3 text-label font-semibold transition-colors ' +
                          (selected ? 'bg-accent/[0.12] text-accent-soft' : 'text-fg-muted hover:bg-surface hover:text-fg')}>
                  {p.label}
                </button>
              )
            })}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <button type="button" onClick={() => setPaletteOpen(true)}
                    className="inline-flex h-8 items-center gap-2 rounded-sm border border-border px-3 text-label text-fg-muted transition-colors hover:border-accent/40 hover:text-fg"
                    aria-keyshortcuts={isMac ? 'Meta+K' : 'Control+K'}>
              Search
              <kbd className="rounded-[3px] border border-border px-1 font-mono text-micro text-muted">{isMac ? '⌘K' : 'Ctrl K'}</kbd>
            </button>
            <button type="button" onClick={openSettings}
                    className="inline-flex h-8 items-center rounded-sm border border-border px-3 text-label text-fg-muted transition-colors hover:border-accent/40 hover:text-fg">
              Settings
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-container px-6 pb-10 max-md:px-4">
        <LocalSignIn />

        <main id="page" role="tabpanel" aria-labelledby={`tab-${active}`} tabIndex={-1}>
          <PageErrorBoundary key={active}>
            <Suspense fallback={<div className="py-10 text-center text-caption text-muted" role="status">Loading…</div>}>
              <ActivePage />
            </Suspense>
          </PageErrorBoundary>
        </main>
      </div>
      <SettingsDrawer open={settingsOpen} onClose={closeSettings} />
      <CommandPalette open={paletteOpen} onClose={closePalette} pages={PAGES}
                      onNavigate={navigate} onOpenSettings={openSettings} />
    </ToastProvider>
  )
}
