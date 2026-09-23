// Dependencies tab — port of the legacy dependency-probe panel. Consumes /api/dependencies
// ({version, description, entries:[{id, name, category, ok, error, latency_ms, hint}]}) and
// renders a reachable-count summary plus the probes grouped by category. Each entry shows a
// status dot (green ok / red fail), its measured latency, and the hint or error. Polls every
// 20s (paused when hidden). The legacy shell used one flat table with a Category column; this
// port groups by category (as specced) — same content, clearer at a glance.
import { useMemo } from 'react'
import { api, usePolling } from '../api.js'

function fmtLatency(ms) {
  if (ms == null || ms === '') return '—'
  const n = Number(ms)
  return Number.isFinite(n) ? `${n.toFixed(1)}ms` : '—'
}

export default function DependenciesTab() {
  const { data, error } = usePolling(() => api.get('/api/dependencies'), 20000)

  const entries = data?.entries || []
  const okN = entries.filter((e) => e.ok).length
  const total = entries.length

  // Group entries by category, preserving first-seen category order.
  const groups = useMemo(() => {
    const map = new Map()
    for (const e of entries) {
      const cat = e.category || 'other'
      if (!map.has(cat)) map.set(cat, [])
      map.get(cat).push(e)
    }
    return [...map.entries()]
  }, [entries])

  const summaryCls =
    'flex items-center gap-2 mb-5 px-4 py-3 rounded-sm text-[0.8125rem] font-medium bg-bg-elevated border border-border-subtle ' +
    (total === 0
      ? 'border-l-[3px] border-l-warning bg-warning/[0.04]'
      : okN === total
        ? 'border-l-[3px] border-l-success bg-success/[0.05]'
        : 'border-l-[3px] border-l-warning bg-warning/[0.04]')

  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        Dependencies
      </h2>
      <p className="mb-4 text-[0.8125rem] leading-[1.5] text-muted">
        {data?.description || 'Live reachability probes for the services this stack depends on.'}
      </p>

      {error && !data ? (
        <div className={summaryCls} role="status">
          Could not load dependencies — check that the dashboard API is up.
        </div>
      ) : !data ? (
        <div className="space-y-2">
          {[0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-10 w-full" />)}
        </div>
      ) : (
        <>
          <div className={summaryCls} role="status" aria-live="polite">
            {total === 0 ? 'No entries' : `${okN}/${total} dependencies reachable`}
          </div>

          <div className="space-y-6">
            {groups.map(([cat, items]) => (
              <div key={cat}>
                <h3 className="mb-2 text-heading text-fg">
                  {cat}
                </h3>
                <div className="grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))] max-[600px]:grid-cols-1">
                  {items.map((e) => (
                    <div
                      key={e.id || e.name}
                      className={
                        'flex flex-col gap-1.5 rounded-md border border-border-subtle border-l-2 bg-bg-elevated px-4 py-3 ' +
                        (e.ok ? 'border-l-success' : 'border-l-danger')
                      }
                    >
                      <div className="flex items-center gap-2.5">
                        <span
                          className={`status-dot ${e.ok ? 'ok' : 'fail'}`}
                          aria-hidden="true"
                          title={e.ok ? 'OK' : e.error || 'Unreachable'}
                        />
                        <span className="min-w-0 flex-1 truncate text-[0.875rem] font-medium text-fg" title={e.id || ''}>
                          {e.name || e.id || '—'}
                        </span>
                        <span className="shrink-0 font-mono text-[0.72rem] text-muted">
                          {e.ok ? fmtLatency(e.latency_ms) : '—'}
                        </span>
                      </div>
                      {!e.ok && e.error && (
                        <div className="break-words pl-[1.375rem] text-[0.75rem] leading-[1.4] text-danger">
                          {e.error}
                        </div>
                      )}
                      {e.hint && (
                        <div className="break-words pl-[1.375rem] text-[0.75rem] leading-[1.4] text-fg-muted">
                          {e.hint}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  )
}
