// Orchestration tab — read-only status of the GPU scheduler + job queue. Port of the legacy
// loadOrchestrationTab + loadOrchGpu. Consumes:
//   - GET /api/orchestration/readiness   — upstream readiness (200 ok / 503 not-ready)
//   - GET /api/orchestration/gpu         — live leases: running, queued, evicted, VRAM, state
//   - GET /api/orchestration/gpu/history — finished leases (newest first)
//   - GET /api/orchestration/jobs        — orchestration job list ({jobs, count})
// Polls every 10s (usePolling pauses while hidden). Nothing here mutates state — the tab is a
// window onto the scheduler, matching the legacy panel.
import { api, usePolling } from '../api.js'

const BADGE = 'inline-flex items-center rounded-full border px-2 py-0.5 text-[0.62rem] font-semibold uppercase tracking-[0.06em]'
const B_SUCCESS = 'border-success/30 bg-success/10 text-success'
const B_ERROR = 'border-danger/30 bg-danger/10 text-danger'
const B_WARN = 'border-warning/30 bg-warning/10 text-warning'
const B_MUTED = 'border-border-subtle bg-bg text-muted'

const kindBadge = (k) => ({ training: B_WARN, media: B_MUTED, chat: B_SUCCESS }[k] || B_MUTED)

function jobStateBadge(s) {
  s = String(s || '').toLowerCase()
  if (/ready|done|complete|success|finish/.test(s)) return B_SUCCESS
  if (/error|fail|cancel/.test(s)) return B_ERROR
  if (/run|queue|pend|active|progress/.test(s)) return B_WARN
  return B_MUTED
}
const outcomeBadge = (o) => (o === 'completed' ? B_SUCCESS : o === 'swept' ? B_WARN : B_ERROR)

function fmtDur(s) {
  s = Number(s) || 0
  return s >= 3600 ? (s / 3600).toFixed(1) + 'h' : s >= 60 ? Math.round(s / 60) + 'm' : Math.round(s) + 's'
}
function fmtAgo(ts) {
  if (!ts) return ''
  const secs = Date.now() / 1000 - ts
  if (secs < 0) return ''
  if (secs < 60) return 'just now'
  if (secs < 3600) return Math.round(secs / 60) + 'm ago'
  if (secs < 86400) return Math.round(secs / 3600) + 'h ago'
  return Math.round(secs / 86400) + 'd ago'
}

function Row({ children }) {
  return <div className="flex flex-wrap items-center gap-2 rounded-sm border border-border-subtle bg-bg-elevated px-3 py-2 text-[0.8rem]">{children}</div>
}
const JOB_ID = 'min-w-0 flex-1 truncate font-mono text-[0.75rem] text-fg-muted'
const JOB_TIME = 'shrink-0 font-mono text-[0.7rem] text-muted'
const SUBHEAD = 'mb-2 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted'

export default function OrchestrationTab() {
  const { data, error } = usePolling(async () => {
    const [readiness, gpu, history, jobs] = await Promise.all([
      // readiness returns 503 (not an exception we want to swallow) when not ready — the
      // body carries {ok:false, detail}, so surface that instead of a hard error.
      api.get('/api/orchestration/readiness').catch((e) => (e.body && typeof e.body === 'object' ? e.body : { ok: false, detail: e.message })),
      api.get('/api/orchestration/gpu').catch((e) => ({ _error: e.message || String(e) })),
      api.get('/api/orchestration/gpu/history').catch(() => ({ history: [] })),
      api.get('/api/orchestration/jobs').catch((e) => ({ _error: e.message || String(e) })),
    ])
    return { readiness, gpu, history, jobs }
  }, 10000)

  const readiness = data?.readiness
  const gpu = data?.gpu
  const history = data?.history?.history || []
  const jobsData = data?.jobs
  const jobs = Array.isArray(jobsData) ? jobsData : (jobsData?.jobs || [])

  const gpuErr = gpu?._error
  const total = Number(gpu?.total_vram_gb) || 0
  const used = Math.max(0, total - (Number(gpu?.free_vram_gb) || 0))
  const vramPct = total ? Math.round((used / total) * 100) : 0
  const running = gpu?.running || []
  const queued = gpu?.queued || []
  const evicted = Object.keys(gpu?.evicted_residents || {})

  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        Orchestration
      </h2>
      <p className="mb-5 text-[0.8125rem] leading-[1.5] text-muted">
        Live view of the GPU scheduler — which jobs hold the GPU, what is queued, and the
        orchestration job history. Read-only; refreshes every 10 seconds.
      </p>

      {error && !data ? (
        <div className="flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
          Could not reach the orchestration API — check that the dashboard is up.
        </div>
      ) : !data ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => <div key={i} className="skeleton h-24 w-full" />)}
        </div>
      ) : (
        <>
          {/* Readiness */}
          <div
            className={
              'mb-6 flex flex-wrap items-center gap-3 rounded-sm border border-border-subtle border-l-[3px] px-4 py-3 text-[0.8125rem] ' +
              (readiness?.ok ? 'border-l-success bg-success/[0.05]' : 'border-l-warning bg-warning/[0.04]')
            }
            role="status"
            aria-live="polite"
          >
            <span className={`${BADGE} ${readiness?.ok ? B_SUCCESS : B_WARN}`}>
              {readiness?.ok ? 'Ready' : 'Not ready'}
            </span>
            {readiness?.detail && <span className="text-fg-muted">{String(readiness.detail)}</span>}
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            {/* GPU leases */}
            <div>
              <h3 className={SUBHEAD}>GPU leases</h3>
              {gpuErr ? (
                <p className="text-[0.8125rem] text-muted">Scheduler unavailable: <span className="font-mono text-[0.75rem]">{gpuErr}</span></p>
              ) : (
                <>
                  {evicted.length > 0 && (
                    <div className="mb-3 rounded-sm border border-warning/30 bg-warning/[0.08] px-3 py-2 text-[0.78rem] text-warning">
                      🧠 {evicted.join(', ')} evicted — chat paused until the lease ends
                    </div>
                  )}
                  <div className="mb-1 flex items-center justify-between text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">
                    <span>VRAM</span>
                    <span className="font-mono normal-case tracking-normal text-fg-muted">
                      {used.toFixed(1)} / {total.toFixed(0)} GB · {gpu?.state || '?'}
                    </span>
                  </div>
                  <div className="h-2 w-full overflow-hidden rounded-full border border-border-subtle bg-bg">
                    <div
                      className={'h-full rounded-full ' + (vramPct >= 90 ? 'bg-danger' : vramPct >= 75 ? 'bg-warning' : 'bg-accent')}
                      style={{ width: vramPct + '%' }}
                    />
                  </div>

                  <div className="mt-3 space-y-1.5">
                    {running.length === 0 ? (
                      <p className="text-[0.8125rem] text-muted">Nothing holds the GPU.</p>
                    ) : running.map((j, i) => (
                      <Row key={j.id || i}>
                        <span className={`${BADGE} ${kindBadge(j.kind)}`}>{j.kind || '?'}</span>
                        <span className={JOB_ID} title={String(j.id)}>{j.id}</span>
                        <span className={JOB_TIME}>held {fmtDur(j.held_s)} · renews {Math.round(j.lease_ttl_s || 0)}s</span>
                      </Row>
                    ))}
                  </div>

                  {queued.length > 0 && (
                    <>
                      <h4 className="mb-1 mt-4 text-[0.65rem] font-semibold uppercase tracking-[0.08em] text-muted">Queued</h4>
                      <div className="space-y-1.5">
                        {queued.map((j, i) => (
                          <Row key={j.id || i}>
                            <span className={`${BADGE} ${B_MUTED}`}>queued</span>
                            <span className={JOB_ID} title={String(j.id)}>{j.id}</span>
                            <span className={JOB_TIME}>{j.vram_gb} GB {j.kind || ''}</span>
                          </Row>
                        ))}
                      </div>
                    </>
                  )}
                </>
              )}

              {/* Lease history */}
              <h3 className={`${SUBHEAD} mt-6`}>Lease history</h3>
              {history.length === 0 ? (
                <p className="text-[0.8125rem] text-muted">No finished leases yet.</p>
              ) : (
                <div className="space-y-1.5">
                  {history.slice(0, 20).map((h, i) => {
                    const dur = (h.ended && h.started) ? fmtDur(h.ended - h.started) : '—'
                    return (
                      <Row key={h.id || i}>
                        <span className={`${BADGE} ${outcomeBadge(h.outcome)}`}>{h.outcome}</span>
                        <span className={`${BADGE} ${kindBadge(h.kind)}`}>{h.kind || '?'}</span>
                        <span className={JOB_ID} title={String(h.id)}>{h.id}</span>
                        <span className={JOB_TIME}>{h.vram_gb} GB · {dur} · {fmtAgo(h.ended)}</span>
                      </Row>
                    )
                  })}
                </div>
              )}
            </div>

            {/* Jobs */}
            <div>
              <h3 className={SUBHEAD}>Recent jobs</h3>
              {jobsData?._error ? (
                <p className="text-[0.8125rem] text-muted">Jobs unavailable: <span className="font-mono text-[0.75rem]">{jobsData._error}</span></p>
              ) : jobs.length === 0 ? (
                <p className="text-[0.8125rem] text-muted">No jobs.</p>
              ) : (
                <>
                  <p className="mb-2 text-[0.75rem] text-muted">
                    {jobs.length} job{jobs.length === 1 ? '' : 's'}{jobs.length > 20 ? ' · showing 20' : ''}
                  </p>
                  <div className="space-y-1.5">
                    {jobs.slice(0, 20).map((j, i) => {
                      const id = String(j.job_id || '?')
                      const st = String(j.state || '?')
                      const tsRaw = j.updated_at || j.created_at
                      const ts = tsRaw ? Date.parse(tsRaw) / 1000 : 0
                      return (
                        <Row key={id + i}>
                          <span className="min-w-0 flex-1 truncate font-mono text-[0.75rem] text-fg-muted" title={id}>{id.slice(0, 16)}</span>
                          <span className={`${BADGE} ${jobStateBadge(st)}`}>{st.replace(/_/g, ' ')}</span>
                          <span className={JOB_TIME}>{fmtAgo(ts)}</span>
                        </Row>
                      )
                    })}
                  </div>
                </>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  )
}
