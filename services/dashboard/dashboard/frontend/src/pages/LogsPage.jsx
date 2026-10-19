// Logs: the dashboard's dependency failures and request traces, polled from
//   GET /api/diagnostics   {dependencies: [{dep, target, status, ...}], recent: [...], legend}
// One status row per dependency (per target for PROBE and EDGE), the failure ring newest first
// with a filter, and a short legend of the KINDs and DEPs a code is made of.
// Below lg each table is a list of cards, like the Services page.
import { useMemo, useState } from 'react'
import { api, usePolling } from '../api.js'
import { Dot, INPUT, Panel, Skeleton, Unavailable } from '../components/ui.jsx'
import { formatElapsed } from '../lib/format.js'

const STATUS_TONE = { ok: 'ok', failing: 'failed', unknown: 'unknown' }
const STATUS_LABEL = { ok: 'OK', failing: 'Failing', unknown: 'Not seen yet' }

const TH = 'px-2 py-2 font-semibold'
const HEAD_ROW = 'border-b border-border-subtle text-left text-micro uppercase tracking-[0.08em] text-muted'
// Baseline, not top: the cells mix body and caption sizes, and their first lines must line up.
const ROW = 'border-b border-border-subtle align-baseline last:border-b-0 hover:bg-surface/60'
const CARD = 'grid gap-2 border-b border-border-subtle py-3 first:pt-0 last:border-b-0 last:pb-0'
const CODE = 'font-mono text-caption text-fg'
const MONO_MUTED = 'font-mono text-caption text-fg-muted'
const META = 'flex flex-wrap gap-x-4 gap-y-1 text-caption text-muted'

/** A local date and time from an epoch in seconds. */
function stamp(epochSeconds) {
  return epochSeconds ? new Date(epochSeconds * 1000).toLocaleString() : 'Never'
}

function rowKey(row) {
  return `${row.dep}:${row.target || ''}`
}

function Status({ status }) {
  return (
    <span className="flex items-center gap-2 whitespace-nowrap">
      <Dot tone={STATUS_TONE[status] || 'unknown'} />
      <span className="text-caption text-fg-muted">{STATUS_LABEL[status] || status}</span>
    </span>
  )
}

// The dependency, and under it the target a per-target dependency (PROBE, EDGE) keeps apart.
function DependencyName({ row }) {
  return (
    <div className="flex min-w-0 flex-col">
      <span className="text-body text-fg">{row.dep}</span>
      {row.target && <span className="truncate font-mono text-micro text-muted">{row.target}</span>}
    </div>
  )
}

function LastFailure({ failure }) {
  if (!failure) return <div className="text-caption text-muted">Never</div>
  return (
    <div className="flex flex-col">
      <span className={CODE}>{failure.code}</span>
      <span className="text-caption tabular-nums text-muted">{stamp(failure.at)}</span>
    </div>
  )
}

function DependencyTable({ rows }) {
  return (
    <div className="relative overflow-x-auto max-lg:hidden">
      <table className="w-full min-w-[52rem] border-collapse">
        <thead>
          <tr className={HEAD_ROW}>
            <th scope="col" className={TH + ' w-[9rem] pl-0'}>Status</th>
            <th scope="col" className={TH}>Dependency</th>
            <th scope="col" className={TH + ' w-[12rem]'}>Last ok</th>
            <th scope="col" className={TH + ' w-[16rem]'}>Last failure</th>
            <th scope="col" className={TH + ' w-[8rem] pr-0 text-right'}>Failures in a row</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)} className={ROW}>
              <td className="py-2 pr-2"><Status status={row.status} /></td>
              <th scope="row" className="px-2 py-2 text-left font-normal"><DependencyName row={row} /></th>
              <td className="px-2 py-2 text-caption tabular-nums text-fg-muted">{stamp(row.last_ok)}</td>
              <td className="px-2 py-2"><LastFailure failure={row.last_failure} /></td>
              <td className="py-2 pl-2 text-right font-mono text-caption tabular-nums text-fg-muted">{row.consecutive_failures}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DependencyCards({ rows }) {
  return (
    <ul className="lg:hidden">
      {rows.map((row) => (
        <li key={rowKey(row)} className={CARD}>
          <div className="flex items-start justify-between gap-3">
            <DependencyName row={row} />
            <div className="shrink-0"><Status status={row.status} /></div>
          </div>
          <dl className={META}>
            <div className="flex gap-1"><dt>Last ok</dt><dd className="tabular-nums text-fg-muted">{stamp(row.last_ok)}</dd></div>
            <div className="flex gap-1"><dt>Failures in a row</dt><dd className="font-mono tabular-nums text-fg-muted">{row.consecutive_failures}</dd></div>
          </dl>
          {row.last_failure && (
            <dl className={META}>
              <div className="flex flex-wrap items-baseline gap-x-1">
                <dt>Last failure</dt>
                <dd className={CODE}>{row.last_failure.code}</dd>
                <dd className="tabular-nums text-fg-muted">{stamp(row.last_failure.at)}</dd>
              </div>
            </dl>
          )}
        </li>
      ))}
    </ul>
  )
}

function FailureTable({ failures }) {
  return (
    <div className="relative overflow-x-auto max-lg:hidden">
      <table className="w-full min-w-[52rem] border-collapse">
        <thead>
          <tr className={HEAD_ROW}>
            <th scope="col" className={TH + ' w-[10rem] pl-0'}>Time</th>
            <th scope="col" className={TH + ' w-[13rem]'}>Code</th>
            <th scope="col" className={TH + ' w-[13rem]'}>Target</th>
            <th scope="col" className={TH + ' w-[5.5rem] text-right'}>Elapsed</th>
            <th scope="col" className={TH + ' w-[9rem]'}>Trace id</th>
            <th scope="col" className={TH + ' pr-0'}>Detail</th>
          </tr>
        </thead>
        <tbody>
          {failures.map((row, i) => (
            <tr key={`${row.at}-${row.trace}-${i}`} className={ROW}>
              <td className="py-2 pr-2 text-caption tabular-nums text-fg-muted">{stamp(row.at)}</td>
              <td className={'px-2 py-2 ' + CODE}>{row.code}</td>
              <td className={'break-all px-2 py-2 ' + MONO_MUTED}>{row.target}</td>
              <td className={'whitespace-nowrap px-2 py-2 text-right tabular-nums ' + MONO_MUTED}>{formatElapsed(row.elapsed_ms)}</td>
              <td className={'px-2 py-2 ' + MONO_MUTED}>{row.trace}</td>
              <td className="break-words py-2 pl-2 text-caption text-fg-muted">{row.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FailureCards({ failures }) {
  return (
    <ul className="lg:hidden">
      {failures.map((row, i) => (
        <li key={`${row.at}-${row.trace}-${i}`} className={CARD}>
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <span className={CODE}>{row.code}</span>
            <span className="text-caption tabular-nums text-fg-muted">{stamp(row.at)}</span>
          </div>
          <dl className={META}>
            <div className="flex min-w-0 gap-1"><dt>Target</dt><dd className={'break-all ' + MONO_MUTED}>{row.target}</dd></div>
            <div className="flex gap-1"><dt>Elapsed</dt><dd className={'tabular-nums ' + MONO_MUTED}>{formatElapsed(row.elapsed_ms)}</dd></div>
            <div className="flex gap-1"><dt>Trace</dt><dd className={MONO_MUTED}>{row.trace}</dd></div>
          </dl>
          <p className="break-words text-caption text-fg-muted">{row.detail}</p>
        </li>
      ))}
    </ul>
  )
}

function matches(row, needle) {
  return [row.code, row.dep, row.trace, row.target, row.detail]
    .some((value) => String(value || '').toLowerCase().includes(needle))
}

function RecentFailures({ recent }) {
  const [filter, setFilter] = useState('')
  const needle = filter.trim().toLowerCase()
  const failures = useMemo(() => recent.filter((row) => !needle || matches(row, needle)), [recent, needle])
  return (
    <Panel title="Recent failures">
      <label htmlFor="log-filter" className="sr-only">Filter failures</label>
      <input id="log-filter" className={INPUT + ' mb-3 w-full lg:w-72'} placeholder="Filter by code, dependency or trace id"
             value={filter} onChange={(e) => setFilter(e.target.value)} />
      {failures.length === 0
        ? (
          <p className="py-6 text-center text-body text-muted">
            {recent.length === 0 ? 'No dependency failures recorded since the dashboard started' : 'No failures match this filter'}
          </p>
        )
        : <><FailureCards failures={failures} /><FailureTable failures={failures} /></>}
    </Panel>
  )
}

function LegendList({ title, items, term }) {
  return (
    <div className="min-w-0">
      <h3 className="mb-2 text-heading text-fg">{title}</h3>
      <dl className="grid gap-1.5">
        {items.map((item) => (
          <div key={item[term]} className="grid grid-cols-[9.5rem_1fr] gap-3 max-sm:grid-cols-1 max-sm:gap-0">
            <dt className={CODE}>{item[term]}</dt>
            <dd className="text-caption text-muted">{item.meaning}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function Legend({ legend }) {
  return (
    <Panel title="Reading a code">
      <p className="mb-3 text-caption text-muted">
        A code is <span className={CODE}>DEP-&lt;dependency&gt;-&lt;kind&gt;</span>, for
        example <span className={CODE}>DEP-OPS-TIMEOUT</span>.
      </p>
      <div className="grid gap-5 md:grid-cols-2">
        <LegendList title="Kinds" items={legend.kinds} term="kind" />
        <LegendList title="Dependencies" items={legend.dependencies} term="dep" />
      </div>
    </Panel>
  )
}

export default function LogsPage() {
  const diagnostics = usePolling(() => api.get('/api/diagnostics'), 10000)
  const data = diagnostics.data

  if (!data) {
    return diagnostics.error
      ? <Unavailable error={diagnostics.error}>Diagnostics are unavailable: {diagnostics.error.message}</Unavailable>
      : <Skeleton className="h-64 w-full" />
  }

  return (
    <div className="grid gap-3 [&>*]:min-w-0">
      <p className="text-caption text-muted">Dependency failures and request traces since the dashboard started. Refreshes every 10 seconds.</p>
      <Panel title="Dependency status">
        <DependencyCards rows={data.dependencies} />
        <DependencyTable rows={data.dependencies} />
      </Panel>
      <RecentFailures recent={data.recent || []} />
      <Legend legend={data.legend} />
    </div>
  )
}
