// Services: every container on the host, one flat list, with the actions the control plane allows.
//   GET  /api/services/table                                    rows with a verdict, a control kind and allowed actions
//   GET  /api/hardware/service-pressure                         CPU and memory per container (slow: ~8 s, so async)
//   POST /api/ops/services/{compose}/{action}                   start | stop | restart an ordo service
//   GET  /api/ops/services/{compose}/logs                       tail, polled while the drawer is open
//   POST /api/ops/projects/{project}/containers/{name}/restart  restart a managed project's container (start, stop when offered)
//   GET  /api/ops/projects/{project}/containers/{name}/logs     its tail
// An action is reported done only once the table shows it (serviceActions.js).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, usePolling } from '../api.js'
import { BTN, BTN_DANGER, ConfirmDialog, Dot, Drawer, INPUT, Skeleton, Unavailable } from '../components/ui.jsx'
import { useToast } from '../components/Toast.jsx'
import {
  CONFIRM_FAST_FOR_MS, CONFIRM_FAST_MS, CONFIRM_SLOW_MS, GIVE_UP_MS, isConfirmed, outcomeUnknown,
} from './serviceActions.js'

const VERDICT_LABEL = {
  up: 'Running', starting: 'Starting', unhealthy: 'Unhealthy', failed: 'Failed', stopped: 'Stopped', done: 'Finished',
  unknown: 'Unknown',
}
const VERDICT_TONE = {
  up: 'ok', starting: 'starting', unhealthy: 'unhealthy', failed: 'failed', stopped: 'stopped', done: 'done',
}
const VERB = { start: 'Start', stop: 'Stop', restart: 'Restart' }
const PENDING_TEXT = { start: 'Starting…', stop: 'Stopping…', restart: 'Restarting…' }
const DONE_TEXT = { start: 'started', stop: 'stopped', restart: 'restarted' }
// What the confirmation dialog says each destructive action does.
const CONSEQUENCE = {
  stop: 'It stays stopped until you start it again.',
  restart: 'It is unavailable until it is back up.',
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

// A row's state as the table last showed it, for a give-up message.
function describeRow(row) {
  if (!row) return 'not listed'
  const verdict = VERDICT_LABEL[row.verdict] || row.verdict
  return row.status ? `${verdict} (${row.status})` : verdict
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

// While a row's action is pending its buttons stay focusable (aria-disabled, not disabled), so
// focus returning from the confirmation dialog lands on the button that asked, not the page.
function ActionButton({ row, action, onAction, busy }) {
  return (
    <button type="button" className={action === 'stop' ? BTN_DANGER : BTN} aria-label={`${VERB[action]} ${rowLabel(row)}`}
            aria-disabled={busy || undefined} aria-busy={busy || undefined}
            onClick={busy ? undefined : () => onAction(row, action)}>
      {VERB[action]}
    </button>
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
  return (
    <>
      {['start', 'restart', 'stop'].filter((action) => row.actions.includes(action)).map((action) => (
        <ActionButton key={action} row={row} action={action} onAction={onAction} busy={busy} />
      ))}
    </>
  )
}

// A read-only row says why in place of its actions: the reason names the one change that would
// give it controls.
function NotManaged({ row }) {
  return <p className="text-caption text-muted">Not managed: {row.reason}</p>
}

// Status, name and the per-row actions are shared by the wide table row and the narrow card, so
// the two layouts cannot drift apart. `action` is the row's action state (see useRowActions).
function ServiceStatus({ row, action }) {
  if (isBusy(action)) {
    return (
      <span className="flex items-center gap-2">
        <Dot tone="starting" />
        <span className="text-caption text-fg-muted">{PENDING_TEXT[action.action]}</span>
      </span>
    )
  }
  return (
    <span className="flex items-center gap-2">
      {/* An evicted resident exits cleanly, but "Finished" would misread a loan as done. */}
      <Dot tone={row.lent ? 'info' : VERDICT_TONE[row.verdict]} />
      <span className="text-caption text-fg-muted">{row.lent ? 'Lent to a render' : VERDICT_LABEL[row.verdict] || row.verdict}</span>
    </span>
  )
}

// A failed or unconfirmed action's reason stays on its row after the toast has gone.
function ServiceName({ row, action }) {
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
      {action?.phase === 'failed' && <span className="text-micro text-danger">{action.note}</span>}
    </div>
  )
}

function LogsButton({ row, onLogs }) {
  if (!HAS_LOGS.has(row.control)) return null
  return <button type="button" className={BTN} onClick={() => onLogs(row)} aria-label={`Logs for ${rowLabel(row)}`}>Logs</button>
}

function ServiceRow({ row, usage, onLogs, onAction, action }) {
  return (
    <tr className="border-b border-border-subtle last:border-b-0 hover:bg-surface/60">
      <td className="py-2 pl-3 pr-2"><ServiceStatus row={row} action={action} /></td>
      <td className="px-2 py-2"><ServiceName row={row} action={action} /></td>
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
                <StateActions row={row} onAction={onAction} busy={isBusy(action)} />
              </div>
            </div>
          )}
      </td>
    </tr>
  )
}

// The same row as a card, for widths where the table would need a sideways scroll: status and
// name first, then the numbers, then every action on its own line so none sits off-screen.
function ServiceCard({ row, usage, onLogs, onAction, action }) {
  return (
    <li className="grid gap-2 border-b border-border-subtle px-3 py-3 last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <ServiceName row={row} action={action} />
        <div className="shrink-0 whitespace-nowrap"><ServiceStatus row={row} action={action} /></div>
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
              <StateActions row={row} onAction={onAction} busy={isBusy(action)} />
            </>
          )}
      </div>
    </li>
  )
}

function matches(row, query) {
  return [row.name, row.project, row.compose].some((value) => (value || '').toLowerCase().includes(query))
}

// A row is busy while its action is requesting or confirming; idle and failed rows can act.
function isBusy(action) {
  return action?.phase === 'requesting' || action?.phase === 'confirming'
}

// Each row's action state, by row id: { phase, action, before, since, seen, note, gaveUp }. A row
// with no entry is idle. `before` is the row as it was when asked, `since` when confirming began,
// `seen` the row as the last table read showed it, `note` a failed row's reason, `gaveUp` marks a
// failure that came from giving up at GIVE_UP_MS (a later read may still confirm it). The
// transitions are drawn in serviceActions.js.
function useRowActions(refreshTable, toast) {
  const [states, setStates] = useState({})
  // The async steps below read and write through this, so a poll never acts on a stale render.
  const statesRef = useRef({})

  const setRow = useCallback((id, next) => {
    const copy = { ...statesRef.current }
    if (next) copy[id] = next
    else delete copy[id]
    statesRef.current = copy
    setStates(copy)
  }, [])

  const fail = useCallback((id, state, message) => {
    toast(`${rowLabel(state.before)}: ${message}`, 'error')
    setRow(id, { ...state, phase: 'failed', note: `${VERB[state.action]} failed: ${message}` })
    refreshTable()
  }, [toast, setRow, refreshTable])

  const run = useCallback(async (row, action) => {
    const requesting = { phase: 'requesting', action, before: row }
    setRow(row.id, requesting)
    try {
      await api.post(`${opsPath(row)}/${action}`)
    } catch (e) {
      if (!outcomeUnknown(e)) { fail(row.id, requesting, e.message); return }
    }
    setRow(row.id, { ...requesting, phase: 'confirming', since: Date.now() })
  }, [setRow, fail])

  // One table read, checked against every confirming row — and against a row that gave up at
  // GIVE_UP_MS but whose target state shows up in a later read: its stale note clears then.
  const settle = useCallback((data) => {
    const now = Date.now()
    for (const [id, state] of Object.entries(statesRef.current)) {
      if (state.phase === 'confirming') {
        const row = data ? data.rows.find((r) => r.id === id) : undefined
        if (row && isConfirmed(state.action, state.before, row)) {
          setRow(id, null)
          toast(`${rowLabel(row)}: ${DONE_TEXT[state.action]}`, 'success')
        } else if (now - state.since >= GIVE_UP_MS) {
          const message = `No change confirmed after ${GIVE_UP_MS / 1000} s; last seen: ${describeRow(row || state.seen || state.before)}`
          toast(`${rowLabel(state.before)}: ${message}`, 'error')
          setRow(id, { ...state, phase: 'failed', gaveUp: true, note: `${VERB[state.action]}: ${message}` })
        } else if (row) {
          setRow(id, { ...state, seen: row })
        }
      } else if (state.phase === 'failed' && state.gaveUp) {
        const row = data ? data.rows.find((r) => r.id === id) : undefined
        if (row && isConfirmed(state.action, state.before, row)) {
          setRow(id, null)
          toast(`${rowLabel(row)}: ${DONE_TEXT[state.action]}`, 'success')
        }
      }
    }
  }, [setRow, toast])

  // While any row is confirming, read the table every CONFIRM_FAST_MS, after CONFIRM_FAST_FOR_MS
  // every CONFIRM_SLOW_MS, and not at all while the tab is hidden (back in view, read at once).
  // Once this loop stops, the page's own poll keeps calling settle, so a gave-up row still
  // clears when a later read shows the target state.
  const confirming = Object.values(states).some((state) => state.phase === 'confirming')
  useEffect(() => {
    if (!confirming) return undefined
    let timer = null
    let reading = false
    let stopped = false
    const delay = () => {
      const now = Date.now()
      const young = Object.values(statesRef.current)
        .some((state) => state.phase === 'confirming' && now - state.since < CONFIRM_FAST_FOR_MS)
      return young ? CONFIRM_FAST_MS : CONFIRM_SLOW_MS
    }
    const schedule = () => {
      if (stopped || reading || timer || document.hidden) return
      timer = setTimeout(read, delay())
    }
    const read = async () => {
      timer = null
      reading = true
      const data = await refreshTable()
      reading = false
      if (stopped) return
      settle(data)
      schedule()
    }
    const onVisibility = () => {
      if (document.hidden) {
        clearTimeout(timer)
        timer = null
      } else if (!timer && !reading) {
        read()
      }
    }
    schedule()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stopped = true
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [confirming, refreshTable, settle])

  return { states, run, settle }
}

export default function ServicesPage() {
  const toast = useToast()
  const table = usePolling(() => api.get('/api/services/table'), 10000)
  const pressure = usePolling(() => api.get('/api/hardware/service-pressure'), 20000)
  const [filter, setFilter] = useState('')
  const [logsFor, setLogsFor] = useState(null)
  // The stop or restart waiting on the confirmation dialog: { row, action }.
  const [asking, setAsking] = useState(null)
  const { states: actionStates, run, settle } = useRowActions(table.refresh, toast)

  // The fast confirming loop stops once no row is confirming. Keep settling on the page's own
  // 10 s poll (reads that already happen) so a row that gave up at 180 s still clears its stale
  // note when a later read shows the target state.
  useEffect(() => {
    if (table.data) settle(table.data)
  }, [table.data, settle])

  const usageById = useMemo(() => {
    const out = {}
    for (const s of pressure.data?.services || []) out[s.id] = s
    return out
  }, [pressure.data])

  // Start changes nothing that is running, so it goes straight out; stop and restart ask first.
  const onAction = useCallback((row, action) => {
    if (action === 'start') run(row, action)
    else setAsking({ row, action })
  }, [run])

  const cancelAsking = useCallback(() => setAsking(null), [])
  const confirmAsking = useCallback(() => {
    setAsking(null)
    run(asking.row, asking.action)
  }, [asking, run])

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
                           onLogs={setLogsFor} onAction={onAction} action={actionStates[row.id]} />
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
                              onLogs={setLogsFor} onAction={onAction} action={actionStates[row.id]} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <LogsDrawer service={logsFor} onClose={closeLogs} />
      <ConfirmDialog open={!!asking} title={asking ? `${VERB[asking.action]} ${rowLabel(asking.row)}?` : ''}
                     confirmLabel={asking ? VERB[asking.action] : ''} danger={asking?.action === 'stop'}
                     onConfirm={confirmAsking} onCancel={cancelAsking}>
        {asking && CONSEQUENCE[asking.action]}
      </ConfirmDialog>
    </div>
  )
}
