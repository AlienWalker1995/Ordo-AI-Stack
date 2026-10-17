// Services: every container on the host, one flat list, with the actions the control plane allows.
//   GET  /api/services/table                                    rows with a verdict, a control kind and allowed actions
//   GET  /api/hardware/service-pressure                         CPU and memory per container (slow: ~8 s, so async)
//   POST /api/ops/services/{compose}/{action}                   start | stop | restart an ordo service
//   GET  /api/ops/services/{compose}/logs                       tail, polled while the drawer is open
//   POST /api/ops/projects/{project}/containers/{name}/restart  restart a managed project's container
//   GET  /api/ops/projects/{project}/containers/{name}/logs     its tail
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, usePolling } from '../api.js'
import { BTN, BTN_DANGER, Dot, Drawer, INPUT, Skeleton, Unavailable } from '../components/ui.jsx'
import { useToast } from '../components/Toast.jsx'

const VERDICT_LABEL = {
  up: 'Running', starting: 'Starting', unhealthy: 'Unhealthy', failed: 'Failed', stopped: 'Stopped', done: 'Finished',
  unknown: 'Unknown',
}
const VERDICT_TONE = {
  up: 'ok', starting: 'starting', unhealthy: 'unhealthy', failed: 'failed', stopped: 'stopped', done: 'done',
}

// The control kinds whose container has logs the control plane will read: an ordo service (the
// host-only agent and ops-controller included) and a managed project's container.
const HAS_LOGS = new Set(['ordo', 'host', 'managed'])

// The ops routes behind a row: an ordo service by its compose name, a managed project's container
// by project and container name.
function opsPath(row) {
  if (row.control === 'managed') {
    return `/api/ops/projects/${encodeURIComponent(row.project)}/containers/${encodeURIComponent(row.container)}`
  }
  return `/api/ops/services/${encodeURIComponent(row.compose)}`
}

// What buttons, confirmations and toasts call a row. Container names repeat across projects (two
// projects can each run a `caddy`), so a row outside ordo carries its project.
function rowLabel(row) {
  return ['managed', 'none'].includes(row.control) && row.project ? `${row.name} (${row.project})` : row.name
}

function formatCpu(pct) {
  if (pct == null) return '—'
  return pct >= 100 ? `${(pct / 100).toFixed(1)} cores` : `${pct.toFixed(pct < 10 ? 1 : 0)}%`
}

function LogsDrawer({ service, onClose }) {
  const [text, setText] = useState('')
  const [paused, setPaused] = useState(false)
  const [error, setError] = useState(null)
  const preRef = useRef(null)

  useEffect(() => {
    if (!service || paused) return undefined
    let stopped = false
    const load = async () => {
      try {
        const d = await api.get(`${opsPath(service)}/logs?tail=300`)
        if (!stopped) { setText(d.logs || ''); setError(null) }
      } catch (e) {
        if (!stopped) setError(e.message)
      }
    }
    load()
    const timer = setInterval(load, 3000)
    return () => { stopped = true; clearInterval(timer) }
  }, [service, paused])

  useEffect(() => {
    const el = preRef.current
    if (el && !paused) el.scrollTop = el.scrollHeight
  }, [text, paused])

  return (
    <Drawer open={!!service} title={service ? `${rowLabel(service)} logs` : ''} onClose={onClose}>
      <div className="mb-3 flex items-center gap-2">
        <button type="button" className={BTN} onClick={() => setPaused((p) => !p)} aria-pressed={paused}>
          {paused ? 'Resume live tail' : 'Pause'}
        </button>
        <span className="text-caption text-muted">{paused ? 'Paused' : 'Refreshing every 3 s'} · last 300 lines</span>
      </div>
      {error && <Unavailable>Could not read logs: {error}</Unavailable>}
      <pre ref={preRef} className="max-h-[calc(100vh-10rem)] overflow-auto whitespace-pre-wrap break-words rounded-sm border border-border-subtle bg-bg p-3 font-mono text-caption text-fg-muted">
        {text || (error ? '' : 'Loading…')}
      </pre>
    </Drawer>
  )
}

// State actions sit inline in a fixed-width slot so Logs lines up down the column. A dropdown
// here would be clipped by the table's horizontal scroller.
function StateActions({ row, onAction, busy }) {
  if (row.control === 'link') {
    return <span className="text-caption text-muted" title="This is a link and a health check, not one container">Link only</span>
  }
  if (row.control === 'host') {
    return <span className="text-caption text-muted" title="The control plane will not restart the service that is serving it">Host only</span>
  }
  if (row.lent) {
    return <span className="text-caption text-muted" title="Its GPU is lent to a render; it restarts when the render finishes">Back after the render</span>
  }
  const label = rowLabel(row)
  return (
    <>
      {row.actions.includes('start') && <button type="button" className={BTN} disabled={busy} onClick={() => onAction(row, 'start')} aria-label={`Start ${label}`}>Start</button>}
      {row.actions.includes('restart') && <button type="button" className={BTN} disabled={busy} onClick={() => onAction(row, 'restart')} aria-label={`Restart ${label}`}>Restart</button>}
      {row.actions.includes('stop') && <button type="button" className={BTN_DANGER} disabled={busy} onClick={() => onAction(row, 'stop')} aria-label={`Stop ${label}`}>Stop</button>}
    </>
  )
}

// A read-only row says why in place of its actions: the reason names the one change that would
// give it controls.
function NotManaged({ row }) {
  return <p className="text-caption text-muted">Not managed: {row.reason}</p>
}

// Status, name and the per-row actions are shared by the wide table row and the narrow card, so
// the two layouts cannot drift apart.
function ServiceStatus({ row }) {
  return (
    <span className="flex items-center gap-2">
      {/* An evicted resident exits cleanly, but "Finished" would misread a loan as done. */}
      <Dot tone={row.lent ? 'info' : VERDICT_TONE[row.verdict]} />
      <span className="text-caption text-fg-muted">{row.lent ? 'Lent to a render' : VERDICT_LABEL[row.verdict] || row.verdict}</span>
    </span>
  )
}

function ServiceName({ row }) {
  return (
    <div className="flex min-w-0 flex-col">
      <span className="truncate text-body text-fg">
        {row.open_url
          ? <a href={row.open_url} target="_blank" rel="noreferrer" className="text-fg no-underline hover:text-accent">{row.name}<span aria-hidden="true" className="ml-1 text-muted">↗</span></a>
          : row.name}
      </span>
      {((row.compose && row.name !== row.compose) || row.error) && (
        <span className="truncate font-mono text-micro text-muted">
          {[row.compose && row.name !== row.compose ? row.compose : null, row.error].filter(Boolean).join(' · ')}
        </span>
      )}
    </div>
  )
}

function LogsButton({ row, onLogs }) {
  if (!HAS_LOGS.has(row.control)) return null
  return <button type="button" className={BTN} onClick={() => onLogs(row)} aria-label={`Logs for ${rowLabel(row)}`}>Logs</button>
}

function ServiceRow({ row, usage, onLogs, onAction, busy }) {
  return (
    <tr className="border-b border-border-subtle last:border-b-0 hover:bg-surface/60">
      <td className="py-2 pl-3 pr-2"><ServiceStatus row={row} /></td>
      <td className="px-2 py-2"><ServiceName row={row} /></td>
      <td className="whitespace-nowrap px-2 py-2 text-caption text-fg-muted">{row.project || '—'}</td>
      <td className="whitespace-nowrap px-2 py-2 text-caption tabular-nums text-muted">{row.uptime || '—'}</td>
      <td className="whitespace-nowrap px-2 py-2 text-right font-mono text-caption tabular-nums text-fg-muted">{usage ? formatCpu(usage.cpu_pct) : '—'}</td>
      <td className="whitespace-nowrap px-2 py-2 text-right font-mono text-caption tabular-nums text-fg-muted">{usage?.mem_gb != null ? `${usage.mem_gb.toFixed(1)} GB` : '—'}</td>
      <td className="py-2 pl-2 pr-3">
        {row.control === 'none'
          ? <NotManaged row={row} />
          : (
            <div className="flex items-center justify-end gap-1.5 whitespace-nowrap">
              <LogsButton row={row} onLogs={onLogs} />
              <div className="flex w-[8.5rem] items-center gap-1.5">
                <StateActions row={row} onAction={onAction} busy={busy} />
              </div>
            </div>
          )}
      </td>
    </tr>
  )
}

// The same row as a card, for widths where the table would need a sideways scroll: status and
// name first, then the numbers, then every action on its own line so none sits off-screen.
function ServiceCard({ row, usage, onLogs, onAction, busy }) {
  return (
    <li className="grid gap-2 border-b border-border-subtle px-3 py-3 last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <ServiceName row={row} />
        <div className="shrink-0 whitespace-nowrap"><ServiceStatus row={row} /></div>
      </div>
      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-caption text-muted">
        <div className="flex gap-1"><dt>Project</dt><dd className="text-fg-muted">{row.project || '—'}</dd></div>
        <div className="flex gap-1"><dt>Up for</dt><dd className="tabular-nums text-fg-muted">{row.uptime || '—'}</dd></div>
        <div className="flex gap-1"><dt>CPU</dt><dd className="font-mono tabular-nums text-fg-muted">{usage ? formatCpu(usage.cpu_pct) : '—'}</dd></div>
        <div className="flex gap-1"><dt>Memory</dt><dd className="font-mono tabular-nums text-fg-muted">{usage?.mem_gb != null ? `${usage.mem_gb.toFixed(1)} GB` : '—'}</dd></div>
      </dl>
      <div className="flex flex-wrap items-center gap-1.5">
        {row.control === 'none'
          ? <NotManaged row={row} />
          : (
            <>
              <LogsButton row={row} onLogs={onLogs} />
              <StateActions row={row} onAction={onAction} busy={busy} />
            </>
          )}
      </div>
    </li>
  )
}

function matches(row, query) {
  return [row.name, row.project, row.compose].some((value) => (value || '').toLowerCase().includes(query))
}

export default function ServicesPage() {
  const toast = useToast()
  const table = usePolling(() => api.get('/api/services/table'), 10000)
  const pressure = usePolling(() => api.get('/api/hardware/service-pressure'), 20000)
  const [filter, setFilter] = useState('')
  const [logsFor, setLogsFor] = useState(null)
  const [busyId, setBusyId] = useState(null)

  const usageById = useMemo(() => {
    const out = {}
    for (const s of pressure.data?.services || []) out[s.id] = s
    return out
  }, [pressure.data])

  const onAction = useCallback(async (row, action) => {
    const label = rowLabel(row)
    if (action !== 'start' && !window.confirm(`${action === 'stop' ? 'Stop' : 'Restart'} ${label}?`)) return
    setBusyId(row.id)
    try {
      await api.post(`${opsPath(row)}/${action}`)
      toast(`${label}: ${action === 'start' ? 'started' : action === 'stop' ? 'stopped' : 'restarted'}`, 'success')
      table.refresh()
    } catch (e) {
      toast(`${label}: ${e.message}`, 'error')
    } finally {
      setBusyId(null)
    }
  }, [toast, table])

  const closeLogs = useCallback(() => setLogsFor(null), [])

  if (!table.data) {
    return table.error
      ? <Unavailable>The service list is unavailable: {table.error.message}</Unavailable>
      : <Skeleton className="h-64 w-full" />
  }

  const query = filter.trim().toLowerCase()
  const rows = table.data.rows.filter((row) => !query || matches(row, query))
  const usageFor = (row) => usageById[row.compose] || usageById[row.card_id]

  return (
    <div className="grid gap-3 [&>*]:min-w-0">
      <div className="flex flex-wrap items-center gap-3">
        <label htmlFor="service-filter" className="sr-only">Filter services</label>
        <input id="service-filter" className={INPUT + ' w-full lg:w-72'} placeholder="Filter by name or project"
               value={filter} onChange={(e) => setFilter(e.target.value)} />
        {table.data.control_plane === 'down' && <Unavailable>The control plane is not answering; states may be stale.</Unavailable>}
        {table.data.control_plane === 'up' && table.data.host_containers === 'down' && (
          <span className="text-caption text-muted">Containers outside ordo are unavailable right now.</span>
        )}
        {pressure.error && <span className="text-caption text-muted">CPU and memory are unavailable right now.</span>}
      </div>
      {query && !rows.length ? (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-border-subtle bg-bg-elevated px-3 py-2.5">
          <span className="text-body text-fg-muted">No services match "{filter.trim()}"</span>
          <button type="button" className={BTN} onClick={() => setFilter('')}>Clear</button>
        </div>
      ) : (
        <div className="min-w-0 rounded-md border border-border-subtle bg-bg-elevated">
          {/* Cards below lg: the table needs 52rem, more than a phone or a narrow tablet has. */}
          <ul className="lg:hidden">
            {rows.map((row) => (
              <ServiceCard key={row.id} row={row} usage={usageFor(row)}
                           onLogs={setLogsFor} onAction={onAction} busy={busyId === row.id} />
            ))}
          </ul>
          <div className="relative overflow-x-auto max-lg:hidden">
            <table className="w-full min-w-[52rem] border-collapse">
              <thead>
                <tr className="border-b border-border-subtle text-left text-micro uppercase tracking-[0.08em] text-muted">
                  <th className="w-[8.5rem] py-2 pl-3 pr-2 font-semibold">Status</th>
                  <th className="px-2 py-2 font-semibold">Service</th>
                  <th className="w-[8rem] px-2 py-2 font-semibold">Project</th>
                  <th className="w-[8rem] px-2 py-2 font-semibold">Up for</th>
                  <th className="w-[6.5rem] px-2 py-2 text-right font-semibold">CPU</th>
                  <th className="w-[6rem] px-2 py-2 text-right font-semibold">Memory</th>
                  <th className="w-[13.5rem] py-2 pl-2 pr-3"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <ServiceRow key={row.id} row={row} usage={usageFor(row)}
                              onLogs={setLogsFor} onAction={onAction} busy={busyId === row.id} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <LogsDrawer service={logsFor} onClose={closeLogs} />
    </div>
  )
}
