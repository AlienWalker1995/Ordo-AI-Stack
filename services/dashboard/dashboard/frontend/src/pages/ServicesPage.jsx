// Services: every container, grouped, with the one action its state calls for.
//   GET  /api/services/table                     grouped rows with a verdict and allowed actions
//   GET  /api/hardware/service-pressure          CPU and memory per container (slow: ~8 s, so async)
//   POST /api/ops/services/{compose}/{action}    start | stop | restart
//   GET  /api/ops/services/{compose}/logs        tail, polled while the drawer is open
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, usePolling } from '../api.js'
import { BTN, BTN_DANGER, Chip, Dot, Drawer, INPUT, Skeleton, Unavailable } from '../components/ui.jsx'
import { useToast } from '../components/Toast.jsx'

// Infrastructure groups start collapsed: they matter when something is wrong, not every visit.
const COLLAPSED_BY_DEFAULT = new Set(['Platform', 'Tools (MCP)', 'Network', 'Jobs'])

const VERDICT_LABEL = {
  up: 'Running', starting: 'Starting', unhealthy: 'Unhealthy', failed: 'Failed', stopped: 'Stopped', done: 'Finished',
}
const VERDICT_TONE = {
  up: 'ok', starting: 'starting', unhealthy: 'unhealthy', failed: 'failed', stopped: 'stopped', done: 'done',
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
        const d = await api.get(`/api/ops/services/${encodeURIComponent(service.compose)}/logs?tail=300`)
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
    <Drawer open={!!service} title={service ? `${service.name} logs` : ''} onClose={onClose}>
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
  if (!row.compose) {
    return <span className="text-caption text-muted" title="This is a link and a health check, not one container">Link only</span>
  }
  if (!row.controllable) {
    return <span className="text-caption text-muted" title="The control plane will not restart the service that is serving it">Host only</span>
  }
  if (row.lent) {
    return <span className="text-caption text-muted" title="Its GPU is lent to a render; it restarts when the render finishes">Back after the render</span>
  }
  return (
    <>
      {row.actions.includes('start') && <button type="button" className={BTN} disabled={busy} onClick={() => onAction(row, 'start')}>Start</button>}
      {row.actions.includes('restart') && <button type="button" className={BTN} disabled={busy} onClick={() => onAction(row, 'restart')} aria-label={`Restart ${row.name}`}>Restart</button>}
      {row.actions.includes('stop') && <button type="button" className={BTN_DANGER} disabled={busy} onClick={() => onAction(row, 'stop')} aria-label={`Stop ${row.name}`}>Stop</button>}
    </>
  )
}

function ServiceRow({ row, usage, onLogs, onAction, busy }) {
  return (
    <tr className="border-b border-border-subtle last:border-b-0 hover:bg-surface/60">
      <td className="py-2 pl-3 pr-2">
        <span className="flex items-center gap-2">
          {/* An evicted resident exits cleanly, but "Finished" would misread a loan as done. */}
          <Dot tone={row.lent ? 'info' : VERDICT_TONE[row.verdict]} />
          <span className="text-caption text-fg-muted">{row.lent ? 'Lent to a render' : VERDICT_LABEL[row.verdict] || row.verdict}</span>
        </span>
      </td>
      <td className="px-2 py-2">
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
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-caption tabular-nums text-muted">{row.uptime || '—'}</td>
      <td className="whitespace-nowrap px-2 py-2 text-right font-mono text-caption tabular-nums text-fg-muted">{usage ? formatCpu(usage.cpu_pct) : '—'}</td>
      <td className="whitespace-nowrap px-2 py-2 text-right font-mono text-caption tabular-nums text-fg-muted">{usage?.mem_gb != null ? `${usage.mem_gb.toFixed(1)} GB` : '—'}</td>
      <td className="whitespace-nowrap py-2 pl-2 pr-3">
        <div className="flex items-center justify-end gap-1.5">
          {row.compose && <button type="button" className={BTN} onClick={() => onLogs(row)} aria-label={`Logs for ${row.name}`}>Logs</button>}
          <div className="flex w-[8.5rem] items-center gap-1.5">
            <StateActions row={row} onAction={onAction} busy={busy} />
          </div>
        </div>
      </td>
    </tr>
  )
}

function Group({ group, usageById, filter: rawFilter, onLogs, onAction, busyId }) {
  const filter = rawFilter.trim().toLowerCase()
  const rows = group.services.filter((s) => !filter
    || s.name.toLowerCase().includes(filter) || (s.compose || '').toLowerCase().includes(filter))
  if (!rows.length) return null
  const problems = rows.filter((r) => ['unhealthy', 'failed'].includes(r.verdict)).length
  return (
    <details open={filter ? true : !COLLAPSED_BY_DEFAULT.has(group.group) || problems > 0}
             className="min-w-0 rounded-md border border-border-subtle bg-bg-elevated">
      <summary className="flex cursor-pointer items-center gap-2 px-3 py-2.5 text-heading text-fg">
        {group.group}
        <span className="text-caption font-normal text-muted">{rows.length}</span>
        {problems > 0 && <Chip tone="danger">{problems} need attention</Chip>}
      </summary>
      <div className="relative overflow-x-auto border-t border-border-subtle">
        <table className="w-full min-w-[46rem] border-collapse">
          <thead>
            <tr className="text-left text-micro uppercase tracking-[0.08em] text-muted">
              <th className="w-[8.5rem] py-2 pl-3 pr-2 font-semibold">Status</th>
              <th className="px-2 py-2 font-semibold">Service</th>
              <th className="w-[8rem] px-2 py-2 font-semibold">Up for</th>
              <th className="w-[6.5rem] px-2 py-2 text-right font-semibold">CPU</th>
              <th className="w-[6rem] px-2 py-2 text-right font-semibold">Memory</th>
              <th className="w-[13.5rem] py-2 pl-2 pr-3"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <ServiceRow key={row.compose || row.card_id} row={row} usage={usageById[row.compose] || usageById[row.card_id]}
                          onLogs={onLogs} onAction={onAction} busy={busyId === row.compose} />
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
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
    if (action !== 'start' && !window.confirm(`${action === 'stop' ? 'Stop' : 'Restart'} ${row.name}?`)) return
    setBusyId(row.compose)
    try {
      await api.post(`/api/ops/services/${encodeURIComponent(row.compose)}/${action}`)
      toast(`${row.name}: ${action === 'start' ? 'started' : action === 'stop' ? 'stopped' : 'restarted'}`, 'success')
      table.refresh()
    } catch (e) {
      toast(`${row.name}: ${e.message}`, 'error')
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

  return (
    <div className="grid gap-3 [&>*]:min-w-0">
      <div className="flex flex-wrap items-center gap-3">
        <label htmlFor="service-filter" className="sr-only">Filter services</label>
        <input id="service-filter" className={INPUT + ' w-72 max-w-full'} placeholder="Filter by name"
               value={filter} onChange={(e) => setFilter(e.target.value)} />
        {!table.data.control_plane && <Unavailable>The control plane is not answering; states may be stale.</Unavailable>}
        {pressure.error && <span className="text-caption text-muted">CPU and memory are unavailable right now.</span>}
      </div>
      {table.data.groups.map((g) => (
        <Group key={g.group} group={g} usageById={usageById} filter={filter}
               onLogs={setLogsFor} onAction={onAction} busyId={busyId} />
      ))}
      <LogsDrawer service={logsFor} onClose={closeLogs} />
    </div>
  )
}
