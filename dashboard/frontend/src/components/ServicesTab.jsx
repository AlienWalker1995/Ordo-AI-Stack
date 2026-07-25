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

const SERVICE_ICONS = {
  'model-gateway': '⇌',
  webui: '💬',
  mcp: '🔌',
  comfyui: '🎨',
  n8n: '⚡',
  qdrant: '🗄',
}

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
  const icon = SERVICE_ICONS[s.id] || '●'

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

  const statusRow = (isBackground || pending) ? (
    <div className="service static-row" style={{ cursor: 'default' }}>
      <span className={`status ${dotCls}`.trim()} aria-hidden="true" title={title} />
      <span className="label">{label}</span>
    </div>
  ) : (
    <a
      href={serviceOpenHref(s)}
      target="_blank"
      rel="noopener"
      className="service"
      aria-label={`Open ${s.name} (opens in new tab)`}
    >
      <span className={`status ${dotCls}`.trim()} aria-hidden="true" title={title} />
      <span className="label">{label}</span>
      <span className="external" aria-hidden="true">↗</span>
    </a>
  )

  return (
    <div className={`service-card ${okCls}`.trim()} data-service-id={s.id}>
      <div className="service-name">
        <span className="service-icon" aria-hidden="true">{icon}</span>
        {s.name}
      </div>
      {statusRow}
      {opsAvailable && (
        <div className="row">
          <button type="button" className="btn-icon" title="Start" aria-label={`Start ${s.name}`} disabled={!!pending} onClick={() => onAction(s.id, 'start')}>▶</button>
          <button type="button" className="btn-icon" title="Stop" aria-label={`Stop ${s.name}`} disabled={!!pending} onClick={() => onAction(s.id, 'stop')}>⏹</button>
          <button type="button" className="btn-icon" title="Restart" aria-label={`Restart ${s.name}`} disabled={!!pending} onClick={() => onAction(s.id, 'restart')}>↻</button>
          <button type="button" className="btn-icon" title="View logs" aria-label={`View logs for ${s.name}`} onClick={() => onLogs(s.id)}>📋</button>
        </div>
      )}
      {!s.ok && s.hint && !pending && <div className="hint">{s.hint}</div>}
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

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={`${id} logs`} onClick={onClose}>
      <div className="modal-content modal-logs" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>{id} — last 100 lines</h2>
          <button type="button" className="btn-icon" aria-label="Close logs" onClick={onClose}>✕</button>
        </div>
        {state.loading
          ? <pre>Loading…</pre>
          : state.error
            ? <pre>{state.error}</pre>
            : <pre>{state.logs}</pre>}
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

  return (
    <section id="services-section">
      <h2>Services</h2>
      <p className="section-desc">
        Click a service to open it. MCP Gateway provides shared tools for Open WebUI, N8N, and Cursor.
      </p>

      {error ? (
        <>
          <div className="status-summary has-issues" role="status">Could not reach services</div>
          <div className="empty">Run: <code>docker compose up -d</code></div>
        </>
      ) : (
        <>
          {total > 0 && (
            <div className={`status-summary ${okCount === total ? 'all-ok' : 'has-issues'}`} role="status" aria-live="polite">
              {okCount === total
                ? `✓ All ${total} services running`
                : `${okCount}/${total} services running — see hints on failed cards`}
            </div>
          )}

          <div className="services">
            {interactive.length === 0 && !data
              ? [0, 1, 2].map((i) => <div key={i} className="skeleton skeleton-line" />)
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
            <div className="bg-jobs">
              <h3 className="bg-jobs-heading">Background jobs</h3>
              <p className="section-desc bg-jobs-desc">Headless workers — no web UI to open, controlled from here.</p>
              <div className="services">
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
