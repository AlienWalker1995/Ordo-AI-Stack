// Services tab — full port of the legacy service grid + Background-jobs section.
// Consumes /api/services (health + open_url + hints) and drives lifecycle through
// /api/ops/services/{id}/{start,stop,restart,logs}, gated on /api/ops/available.
// Behaviour preserved from the vanilla shell: three-state status dot (running/offline/
// unknown, pulsing when healthy), pending transition after an action, 90s watch loop,
// stop/restart confirmations, failed-card hints, and the Open link (open_url →
// SSO-port route → direct host:port). The grid polls every 15s (paused when hidden).
//
// Deliberate deviation from the legacy shell: service logs render in an in-app modal
// instead of a `window.open` popup. The popup is fragile (blocked by default in most
// browsers) whereas a same-origin modal always works and is CSP-safe. Same content:
// the last 100 log lines in a monospace <pre>.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, usePolling } from '../api.js'
import { useToast } from './Toast.jsx'

// Shared Tailwind utility strings (faithful to the legacy .btn-icon / .section-desc tokens).
const BTN_ICON =
  'inline-flex h-9 min-w-9 items-center justify-center whitespace-nowrap rounded-sm border border-border bg-surface px-3 text-[0.8125rem] font-medium tracking-[0.02em] text-fg transition-all hover:border-accent/30 hover:bg-accent/[0.07] hover:text-accent disabled:cursor-not-allowed disabled:opacity-40'
const SECTION_DESC = 'mb-4 text-[0.8125rem] leading-[1.5] text-muted'

// Dashboard behind the SSO front door mounts at /dash; UI services are then reachable
// only through their SSO-gated port on the same hostname. Ported verbatim from legacy.
const APP_PREFIX = (location.pathname.match(/^\/dash(?=\/|$)/) || [''])[0]
const SSO_ROUTES = {
  comfyui: ':8446/',
  webui: ':8443/',
  n8n: ':8445/',
  hermes: ':8447/hermes/',
  'codebase-memory-ui': ':8448/codebase-memory/',
}

function serviceOpenHref(s) {
  const host = location.hostname
  // 1) Server-owned clean per-service tailnet name (single source of truth).
  if (s.open_url) return s.open_url
  // 2) Behind the SSO front door: route to the service's SSO-gated port.
  if (APP_PREFIX && SSO_ROUTES[s.id]) {
    const route = SSO_ROUTES[s.id]
    if (route.startsWith(':')) return `${location.protocol}//${host}${route}`
    return location.origin + route
  }
  // 3) Rewrite the catalog URL's hostname to the dashboard host.
  if (s.url) {
    try {
      const u = new URL(s.url)
      u.hostname = (host === 'localhost' || host === '127.0.0.1') ? '127.0.0.1' : host
      return u.href
    } catch { /* fall through */ }
  }
  // 4) Direct host:port fallback.
  return s.port ? `http://${host}:${s.port}` : '#'
}

function ServiceCard({ s, isBackground, opsAvailable, pending, onAction, onLogs }) {
  const okCls = pending ? 'pending' : s.ok === true ? 'ok-status' : s.ok === false ? 'fail' : ''
  const dotCls = pending ? 'pending' : s.ok === true ? 'ok' : s.ok === false ? 'fail' : ''

  let label, title
  if (pending) {
    label = pending
    title = pending
  } else if (isBackground) {
    label = s.ok === true ? 'Running' : s.ok === false ? 'Offline' : 'Unknown'
    title = s.ok === true ? 'Healthy' : s.ok === false ? 'Offline' : 'No health probe'
  } else {
    label = s.ok ? 'Running' : 'Offline'
    title = s.ok ? 'Healthy' : 'Offline'
  }

  const dot = <span className={`status-dot ${dotCls}`.trim()} aria-hidden="true" title={title} />
  const labelEl = (
    <span className={`min-w-0 flex-1 text-[0.88rem] font-medium ${pending ? 'text-warning' : ''}`.trim()}>
      {label}
    </span>
  )

  const statusRow = (isBackground || pending) ? (
    <div className="flex cursor-default items-center gap-3 py-2 text-fg">
      {dot}
      {labelEl}
    </div>
  ) : (
    <a
      href={serviceOpenHref(s)}
      target="_blank"
      rel="noopener"
      className="flex items-center gap-3 py-2 text-fg no-underline transition-opacity hover:opacity-[0.82]"
      aria-label={`Open ${s.name} (opens in new tab)`}
    >
      {dot}
      {labelEl}
      <span className="shrink-0 text-[0.7rem] opacity-55" aria-hidden="true">↗</span>
    </a>
  )

  return (
    <div
      className={`service-card flex min-h-[4.5rem] flex-col rounded-md px-5 py-4 ${okCls} ${isBackground ? 'bg-job' : ''}`.trim()}
      data-service-id={s.id}
    >
      <div className="mb-2 flex items-center text-[0.8125rem] font-semibold text-fg">
        {s.name}
      </div>
      {statusRow}
      {opsAvailable && (
        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-1 border-t border-border-subtle pt-2">
          <button type="button" className={BTN_ICON} title="Start" aria-label={`Start ${s.name}`} disabled={!!pending} onClick={() => onAction(s.id, 'start')}>Start</button>
          <button type="button" className={BTN_ICON} title="Stop" aria-label={`Stop ${s.name}`} disabled={!!pending} onClick={() => onAction(s.id, 'stop')}>Stop</button>
          <button type="button" className={BTN_ICON} title="Restart" aria-label={`Restart ${s.name}`} disabled={!!pending} onClick={() => onAction(s.id, 'restart')}>Restart</button>
          <button type="button" className={BTN_ICON} title="View logs" aria-label={`View logs for ${s.name}`} onClick={() => onLogs(s.id)}>Logs</button>
        </div>
      )}
      {!s.ok && s.hint && !pending && (
        <div className="mt-3 break-words border-t border-border-subtle pt-3 text-[0.8125rem] leading-[1.5] text-fg-muted">
          {s.hint}
        </div>
      )}
    </div>
  )
}

function LogsModal({ id, onClose }) {
  const [state, setState] = useState({ loading: true, logs: '', error: null })
  useEffect(() => {
    let alive = true
    api.get(`/api/ops/services/${id}/logs?tail=100`)
      .then((d) => { if (alive) setState({ loading: false, logs: d.logs || '(no output)', error: null }) })
      .catch((e) => { if (alive) setState({ loading: false, logs: '', error: e.message || 'Failed to load logs' }) })
    return () => { alive = false }
  }, [id])

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const pre = 'max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-sm border border-border-subtle bg-bg p-4 font-mono text-[0.7rem] leading-[1.5] text-fg-muted'
  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/[0.82] p-4 backdrop-blur-[4px]"
      role="dialog"
      aria-modal="true"
      aria-label={`${id} logs`}
      onClick={onClose}
    >
      <div
        className="w-full max-w-[900px] rounded-[10px] border border-border border-t-2 border-t-accent bg-bg-elevated p-6 shadow-card-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="m-0 font-display text-title text-fg">
            {id}: last 100 lines
          </h2>
          <button type="button" className={BTN_ICON} aria-label="Close logs" onClick={onClose}>✕</button>
        </div>
        {state.loading
          ? <pre className={pre}>Loading…</pre>
          : state.error
            ? <pre className={pre}>{state.error}</pre>
            : <pre className={pre}>{state.logs}</pre>}
      </div>
    </div>
  )
}

export default function ServicesTab() {
  const toast = useToast()
  // One poller drives both ops-availability and the service list (15s, paused when hidden).
  const { data, error, refresh } = usePolling(async () => {
    const [ops, svc] = await Promise.all([
      api.get('/api/ops/available').catch(() => ({ available: false })),
      api.get('/api/services'),
    ])
    return { opsAvailable: ops.available === true, ...svc }
  }, 15000)

  const [pending, setPending] = useState({})   // { id: 'Starting…' }
  const [logsFor, setLogsFor] = useState(null)
  const watchingRef = useRef(new Set())

  const opsAvailable = data?.opsAvailable === true
  const services = data?.services || []
  const interactive = services.filter((s) => !s.background)
  const background = services.filter((s) => s.background)
  const okCount = interactive.filter((s) => s.ok).length
  const total = interactive.length

  // Poll /api/services until `id` reaches the expected state (start/restart => up,
  // stop => down), up to 90s. Mirrors the legacy watchService.
  const watchService = useCallback(async (id, action) => {
    const targetOk = action !== 'stop'
    const verb = action === 'stop' ? 'Stopping' : action === 'restart' ? 'Restarting' : 'Starting'
    if (watchingRef.current.has(id)) return
    watchingRef.current.add(id)
    setPending((p) => ({ ...p, [id]: `${verb}…` }))
    const deadline = Date.now() + 90000
    try {
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 3000))
        let list
        try { list = (await api.get('/api/services')).services || [] } catch { continue }
        const svc = list.find((x) => x.id === id)
        if (svc && !!svc.ok === targetOk) {
          toast(`${id} is now ${targetOk ? 'running' : 'stopped'}`, 'success')
          return
        }
      }
      toast(
        `${id} is taking longer than expected to ${verb.toLowerCase().replace(/ing$/, '')}` +
        (targetOk ? ' — still booting, check logs if it stays red' : ''),
      )
    } finally {
      watchingRef.current.delete(id)
      setPending((p) => { const n = { ...p }; delete n[id]; return n })
      refresh()
    }
  }, [toast, refresh])

  const doAction = useCallback(async (id, action) => {
    if (action === 'stop' && !confirm(`Stop service "${id}"? Active connections will be dropped.`)) return
    if (action === 'restart' && !confirm(`Restart service "${id}"? This will briefly interrupt the service.`)) return
    const verb = action === 'stop' ? 'Stopping' : action === 'restart' ? 'Restarting' : 'Starting'
    try {
      await api.post(`/api/ops/services/${id}/${action}`)
      toast(`${verb} ${id}…`)
      watchService(id, action)
    } catch (e) {
      toast(`${action[0].toUpperCase() + action.slice(1)} failed: ${e.message || e}`, 'error')
    }
  }, [toast, watchService])

  const statusSummary = (extra) =>
    `flex items-center gap-2 mb-4 px-4 py-3 rounded-sm text-[0.8125rem] font-medium bg-bg-elevated border border-border-subtle ${extra}`
  const ALL_OK = 'border-l-[3px] border-l-success bg-success/[0.05]'
  const ISSUES = 'border-l-[3px] border-l-warning bg-warning/[0.04]'
  const GRID = 'grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(200px,1fr))] max-[600px]:grid-cols-1'

  return (
    <section
      id="services-section"
      className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card"
    >
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        Services
      </h2>
      <p className={SECTION_DESC}>
        Click a service to open it. MCP Gateway provides shared tools for Open WebUI, N8N, and Cursor.
      </p>

      {error ? (
        <>
          <div className={statusSummary(ISSUES)} role="status">Could not reach services</div>
          <div className="py-4 text-[0.8125rem] text-muted">Run: <code>docker compose up -d</code></div>
        </>
      ) : (
        <>
          {total > 0 && (
            <div className={statusSummary(okCount === total ? ALL_OK : ISSUES)} role="status" aria-live="polite">
              {okCount === total
                ? `✓ All ${total} services running`
                : `${okCount}/${total} services running — see hints on failed cards`}
            </div>
          )}

          <div className={GRID}>
            {interactive.length === 0 && !data
              ? [0, 1, 2].map((i) => <div key={i} className="skeleton h-[0.9rem]" />)
              : interactive.map((s) => (
                <ServiceCard
                  key={s.id}
                  s={s}
                  isBackground={false}
                  opsAvailable={opsAvailable}
                  pending={pending[s.id]}
                  onAction={doAction}
                  onLogs={setLogsFor}
                />
              ))}
          </div>

          {background.length > 0 && (
            <div className="mt-6 border-t border-border-subtle pt-5">
              <h3 className="mb-1 text-heading text-fg">Background jobs</h3>
              <p className="mb-4 text-[0.8125rem] leading-[1.5] text-muted">Backend services and headless workers — no browsable UI, controlled from here.</p>
              <div className={GRID}>
                {background.map((s) => (
                  <ServiceCard
                    key={s.id}
                    s={s}
                    isBackground
                    opsAvailable={opsAvailable}
                    pending={pending[s.id]}
                    onAction={doAction}
                    onLogs={setLogsFor}
                  />
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {logsFor && <LogsModal id={logsFor} onClose={() => setLogsFor(null)} />}
    </section>
  )
}
