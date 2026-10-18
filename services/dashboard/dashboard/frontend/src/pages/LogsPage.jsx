import { useMemo, useState } from 'react'
import { api, usePolling } from '../api.js'
import { INPUT, Panel, Skeleton, Unavailable } from '../components/ui.jsx'

const stamp = (value) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString() : 'Never'
const Table = ({ children, className = '' }) => <div className={'overflow-x-auto ' + className}><table className="w-full min-w-[720px] text-left text-caption">{children}</table></div>
const Header = ({ columns }) => <thead><tr className="border-b border-border text-micro uppercase tracking-wide text-muted">{columns.map((x) => <th key={x} scope="col" className="px-2 py-2">{x}</th>)}</tr></thead>

export default function LogsPage() {
  const [filter, setFilter] = useState('')
  const diagnostics = usePolling(() => api.get('/api/diagnostics'), 10000)
  const data = diagnostics.data
  const needle = filter.trim().toLowerCase()
  const failures = useMemo(() => (data?.recent || []).filter((row) =>
    !needle || [row.code, row.dep, row.trace, row.target, row.detail].some((value) => String(value || '').toLowerCase().includes(needle))), [data, needle])

  if (!data) return diagnostics.error
    ? <Unavailable error={diagnostics.error}>Diagnostics are unavailable: {diagnostics.error.message}</Unavailable>
    : <Skeleton className="h-64 w-full" />

  return <div className="space-y-4">
    <div><h1 className="text-title font-bold text-fg">Logs</h1><p className="mt-1 text-caption text-muted">Dashboard dependency failures and request traces. Refreshes every 10 seconds.</p></div>
    <Panel title="Dependency status">
      <div className="grid gap-2 md:hidden">{data.dependencies.map((dep) => <article key={dep.dep} className="rounded-sm border border-border-subtle p-3">
        <div className="flex justify-between gap-2"><strong>{dep.dep}</strong><span>Status: {dep.status}</span></div>
        <p className="mt-1 text-caption text-muted">Last ok: {stamp(dep.last_ok)} · Failures: {dep.consecutive_failures}</p>
        <p className="text-caption text-muted">Last failure: {dep.last_failure ? `${dep.last_failure.code} · ${stamp(dep.last_failure.at)}` : 'Never'}</p>
      </article>)}</div>
      <Table className="hidden md:block"><Header columns={['Dependency', 'Status', 'Last ok', 'Last failure', 'Consecutive failures']} /><tbody>{data.dependencies.map((dep) => <tr key={dep.dep} className="border-b border-border-subtle"><th scope="row" className="px-2 py-2 font-semibold">{dep.dep}</th><td className="px-2 py-2">{dep.status}</td><td className="px-2 py-2">{stamp(dep.last_ok)}</td><td className="px-2 py-2">{dep.last_failure ? <>{dep.last_failure.code}<br />{stamp(dep.last_failure.at)}</> : 'Never'}</td><td className="px-2 py-2">{dep.consecutive_failures}</td></tr>)}</tbody></Table>
    </Panel>
    <Panel title="Recent failures" action={<label className="sr-only" htmlFor="log-filter">Filter failures</label>}>
      <input id="log-filter" className={INPUT + ' w-full md:max-w-sm'} placeholder="Filter code, dependency, trace id" value={filter} onChange={(e) => setFilter(e.target.value)} />
      {failures.length === 0 ? <p className="py-6 text-center text-body text-muted">{(data.recent || []).length === 0 ? 'No dependency failures recorded since the dashboard started' : 'No failures match this filter'}</p> : <>
        <div className="mt-3 grid gap-2 md:hidden">{failures.map((row, i) => <article key={`${row.at}-${i}`} className="rounded-sm border border-border-subtle p-3"><strong>{row.code}</strong><p>{row.dep} · {stamp(row.at)}</p><p className="text-muted">{row.target} · {row.elapsed_ms} ms · Trace {row.trace}</p><p className="break-words text-muted">{row.detail}</p></article>)}</div>
        <Table className="mt-3 hidden md:block"><Header columns={['Time', 'Code', 'Dependency', 'Target', 'Elapsed', 'Trace id', 'Detail']} /><tbody>{failures.map((row, i) => <tr key={`${row.at}-${i}`} className="border-b border-border-subtle"><td className="px-2 py-2">{stamp(row.at)}</td><td className="px-2 py-2 font-mono">{row.code}</td><td className="px-2 py-2">{row.dep}</td><td className="px-2 py-2">{row.target}</td><td className="px-2 py-2">{row.elapsed_ms} ms</td><td className="px-2 py-2 font-mono">{row.trace}</td><td className="max-w-xs break-words px-2 py-2">{row.detail}</td></tr>)}</tbody></Table>
      </>}
    </Panel>
    <Panel title="Failure code legend"><div className="grid gap-2 sm:grid-cols-2">{data.codes.map((item) => <p key={item.code} className="text-caption"><code className="font-mono text-fg">{item.code}</code><span className="text-muted">: {item.meaning}</span></p>)}</div></Panel>
  </div>
}
