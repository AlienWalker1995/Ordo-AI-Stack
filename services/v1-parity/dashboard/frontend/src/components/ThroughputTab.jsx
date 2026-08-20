// Throughput tab — inference telemetry + an on-demand benchmark. Consumes:
//   - GET  /api/throughput/stats         — per-model tok/s percentiles (timestamped,
//         7-day retention) + the AUTHORITATIVE active_model (ops /model-config), its
//         server-derived active_model_alias (gateway pin-alias), control_plane_ok
//         (ops reachability, distinct from an empty/no-model config), + last_benchmark
//   - GET  /api/throughput/service-usage — which service drove which model, recent tok/s
//   - GET  /api/performance/summary      — context size + fleet summary
//   - POST /api/throughput/benchmark     — run a quick tok/s benchmark (confirm + pending)
// The hero is the registry's active model — never inferred from sample recency or
// counts. Sample keys are real GGUF basenames (the gateway callback attributes each
// completion to the deployment that served it). Telemetry polls every 10s (paused when
// hidden); the benchmark targets the active model's gateway pin-alias (from the server,
// not re-derived client-side).
import { useCallback, useState } from 'react'
import { api, usePolling } from '../api.js'
import { useToast } from './Toast.jsx'

// Callers that reach the gateway with the stock OpenAI SDK send its class name as the "service".
// Render those as an honest "unidentified caller" instead of dressing a raw SDK class up as a
// curated service identity (the real fix is server-side attribution; this stops the UI lying).
const RAW_SDK_RE = /^(async)?(openai|azureopenai|anthropic)$/i
function serviceLabel(name) {
  const n = (name || '').trim()
  if (!n) return { text: 'unattributed', dim: true }
  if (RAW_SDK_RE.test(n)) return { text: 'unidentified caller', dim: true, title: n }
  return { text: n, dim: false }
}

// tok/s ramp — mirrors the semantic tokens (success/warning/danger in tailwind.config.js) so it
// stays inside the app's single palette instead of forking a second neon color set. Inline styles
// can't reference the Tailwind tokens directly, so the token hexes are duplicated here by intent.
const TPS_MUTED = '#8a90a8', TPS_SUCCESS = '#2bb673', TPS_WARNING = '#e0a52e', TPS_DANGER = '#e5495f'
function tpsColor(tps) {
  if (!tps) return TPS_MUTED
  if (tps >= 15) return TPS_SUCCESS
  if (tps >= 6) return TPS_WARNING
  return TPS_DANGER
}
const fmt = (v) => (v == null || v === 0 || Number.isNaN(v)) ? '—' : (Math.round(v * 10) / 10).toString()
const fmtInt = (v) => (v == null || v === 0 || Number.isNaN(v)) ? '—' : Math.round(v).toString()
function fmtAgo(ts) {
  if (!ts) return ''
  const secs = Date.now() / 1000 - ts
  if (secs < 0) return ''
  if (secs < 60) return 'just now'
  if (secs < 3600) return Math.round(secs / 60) + 'm ago'
  if (secs < 86400) return Math.round(secs / 3600) + 'h ago'
  return Math.round(secs / 86400) + 'd ago'
}

const BTN =
  'inline-flex h-9 items-center justify-center whitespace-nowrap rounded-sm border border-border bg-surface px-4 text-[0.8125rem] font-medium tracking-[0.02em] text-fg transition-all hover:border-accent/30 hover:bg-accent/[0.07] hover:text-accent disabled:cursor-not-allowed disabled:opacity-40'
const PANEL = 'rounded-md border border-border-subtle bg-bg-elevated p-5'
const RAIL_LABEL = 'text-micro font-semibold text-muted'
const RAIL_VAL = 'mt-0.5 font-mono text-[0.9rem] font-semibold tabular-nums text-fg'

function Metric({ label, value }) {
  return (
    <div className="rounded-sm border border-border-subtle bg-bg px-3 py-2 text-center">
      <div className={RAIL_LABEL}>{label}</div>
      <div className={RAIL_VAL}>{value}</div>
    </div>
  )
}

export default function ThroughputTab() {
  const toast = useToast()

  const { data, error, refresh } = usePolling(async () => {
    const [stats, usage, summary] = await Promise.all([
      api.get('/api/throughput/stats').catch(() => ({ ok: false, models: {} })),
      api.get('/api/throughput/service-usage').catch(() => ({ ok: false, by_model: {} })),
      api.get('/api/performance/summary').catch(() => null),
    ])
    return { stats, usage, summary }
  }, 10000)

  const stats = data?.stats
  const summary = data?.summary

  // statsOk: did OUR /api/throughput/stats call succeed. controlPlaneOk: given statsOk,
  // did the ops-controller /model-config lookup behind it succeed. These are distinct
  // failure modes — conflating them blames the control plane for a dashboard-API outage.
  const statsOk = stats?.ok === true
  const controlPlaneOk = statsOk ? (stats.control_plane_ok !== false) : true
  const models = statsOk && stats.models ? stats.models : {}

  // Authoritative model state — ops-controller /model-config via /stats. Never guessed:
  // null means either the control plane is unreachable or reachable-but-unconfigured;
  // the hero panel below distinguishes those (see controlPlaneOk).
  const activeModel = statsOk ? (stats.active_model ?? null) : null
  const activeStats = activeModel ? models[activeModel] : null

  // Other models with recent samples (the store evicts after 7 days) — history, not "Active".
  const historyRows = Object.entries(models)
    .filter(([name]) => name !== activeModel)
    .sort((a, b) => (b[1].last_ts || 0) - (a[1].last_ts || 0))

  const byModel = data?.usage?.ok ? (data.usage.by_model || {}) : {}
  const usageRows = []
  Object.entries(byModel).forEach(([model, info]) => {
    (info.services || []).forEach((svc) => usageRows.push({ model, svc }))
  })
  usageRows.sort((a, b) => (b.svc.last_ts || 0) - (a.svc.last_ts || 0))
  const maxTps = Math.max(1, ...usageRows.map((r) => r.svc.last_tps || 0))

  const ctx = summary?.llamacpp_ctx_size || 0
  const ctxLabel = ctx ? (ctx >= 1000 ? `${Math.round(ctx / 1000)}K ctx` : `${ctx} ctx`) : 'no ctx'

  // ---- Benchmark (confirm + pending) ----
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null) // fresh run result
  const lastBench = stats?.last_benchmark || null
  const shownBench = result || lastBench

  // Gateway pin-alias for the active GGUF, derived server-side (single source of truth —
  // see _gateway_pin_alias in app.py). Pin (not local-chat) so the benchmark measures the
  // GPU deployment and honestly errors if it's evicted, instead of silently measuring the
  // CPU fallback. Falls back to local-chat only when there's no server-derived alias.
  const benchTarget = (statsOk && stats.active_model_alias) || 'local-chat'

  const runBenchmark = useCallback(async () => {
    if (!confirm(`Run a throughput benchmark on "${benchTarget}"?\n\nThis sends a short generation through the Model Gateway and reports tok/s.`)) return
    setRunning(true)
    try {
      const d = await api.post('/api/throughput/benchmark', { model: benchTarget })
      setResult(d)
      toast(`Benchmark: ${d.output_tokens_per_sec} tok/s (${d.model})`, 'success')
      refresh()
    } catch (e) {
      toast(`Benchmark failed: ${e.message || e}`, 'error')
    } finally {
      setRunning(false)
    }
  }, [benchTarget, toast, refresh])

  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        Throughput
      </h2>
      <p className="mb-5 text-[0.8125rem] leading-[1.5] text-muted">
        Live inference throughput of the active local model — updated after every completion
        through the Model Gateway. Run a benchmark to seed the dashboard.
      </p>

      {error && !data ? (
        <div className="flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
          Could not load throughput telemetry — check that the dashboard API is up.
        </div>
      ) : !data ? (
        <div className="space-y-3">{[0, 1].map((i) => <div key={i} className="skeleton h-28 w-full" />)}</div>
      ) : (
        <>
          {/* Active model (authoritative) + percentile rail */}
          <div className={`${PANEL} mb-6`}>
            {!statsOk ? (
              <div className="flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
                Could not load throughput telemetry — the dashboard API request failed.
              </div>
            ) : !controlPlaneOk ? (
              <div className="flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
                Model control plane unreachable — cannot determine the active model.
              </div>
            ) : activeModel === null ? (
              <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">
                No active model configured — set one in Model Control.
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <div className="min-w-0">
                    <span className="text-micro font-semibold text-muted">Active model</span>
                    <div className="mt-0.5 truncate font-mono text-[0.95rem] font-semibold text-fg" title={activeModel}>
                      {activeModel}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="font-mono text-[1.6rem] font-bold leading-none text-accent">
                      {activeStats ? fmt(activeStats.latest) : '—'}
                    </div>
                    <div className="text-micro font-semibold text-muted">tok/s latest</div>
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.72rem] text-muted">
                  <span>{activeStats ? `${activeStats.sample_count} samples` : 'no traffic yet — run a benchmark'}</span>
                  <span>·</span>
                  <span>{ctxLabel}</span>
                  {activeStats?.last_ts && <><span>·</span><span>last sample {fmtAgo(activeStats.last_ts)}</span></>}
                </div>
                <div className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-6">
                  <Metric label="p50 tok/s" value={activeStats ? fmt(activeStats.p50) : '—'} />
                  <Metric label="p95 tok/s" value={activeStats ? fmt(activeStats.p95) : '—'} />
                  <Metric label="p99 tok/s" value={activeStats ? fmt(activeStats.p99) : '—'} />
                  <Metric label="peak tok/s" value={activeStats ? fmt(activeStats.peak) : '—'} />
                  <Metric label="TTFT p50" value={activeStats ? fmtInt(activeStats.ttft_p50_ms) : '—'} />
                  <Metric label="TTFT p95" value={activeStats ? fmtInt(activeStats.ttft_p95_ms) : '—'} />
                </div>
              </>
            )}
            {historyRows.length > 0 && (
              <div className="mt-4 border-t border-border-subtle pt-3">
                <div className="mb-1.5 text-micro font-semibold text-muted">Recent models (last 7 days)</div>
                {historyRows.map(([name, m]) => (
                  <div key={name} className="flex items-center justify-between gap-3 py-1 text-[0.75rem]">
                    <span className="truncate font-mono text-fg-muted" title={name}>{name}</span>
                    <span className="shrink-0 text-muted">
                      p50 {fmt(m.p50)} tok/s · last seen {fmtAgo(m.last_ts)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Benchmark runner */}
          <div className={`${PANEL} mb-6`}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <span className="text-heading text-fg">Benchmark</span>
                <p className="mt-1 text-[0.8rem] text-fg-muted">
                  Target: <code className="text-accent-soft">{benchTarget}</code>
                </p>
              </div>
              <button type="button" className={BTN} disabled={running} onClick={runBenchmark}>
                {running ? 'Running…' : 'Run benchmark'}
              </button>
            </div>
            {running && (
              <div className="mt-3 flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.06] px-3 py-2 text-[0.8rem] text-warning" role="status" aria-live="polite">
                <span className="status-dot pending" aria-hidden="true" />
                Running benchmark — generating through the Model Gateway…
              </div>
            )}
            {shownBench && !running && (
              <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Metric label="tok/s" value={`${shownBench.output_tokens_per_sec ?? '—'}`} />
                <Metric label="output tokens" value={`${shownBench.output_tokens ?? '—'}`} />
                <Metric label="eval ms" value={typeof shownBench.eval_duration_ms === 'number' ? `${shownBench.eval_duration_ms}` : '—'} />
                <Metric label="load ms" value={typeof shownBench.load_duration_ms === 'number' ? `${shownBench.load_duration_ms}` : '—'} />
              </div>
            )}
          </div>

          {/* Service usage */}
          <div>
            <h3 className="mb-1 text-heading text-fg">Service activity</h3>
            <p className="mb-3 text-[0.8125rem] leading-[1.5] text-muted">
              Recent traffic by service and model. Traffic from Open WebUI, Claude Code, and n8n appears here.
            </p>
            {usageRows.length === 0 ? (
              <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">
                No service activity yet — run a benchmark or send traffic through the gateway.
              </div>
            ) : (
              <div className="space-y-1.5">
                {usageRows.map(({ model, svc }, i) => (
                  <div key={model + (svc.name || '') + i} className="flex items-center gap-3 rounded-sm border border-border-subtle bg-bg-elevated px-4 py-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono text-[0.78rem] text-fg" title={model}>{model}</div>
                      {(() => { const l = serviceLabel(svc.name); return (
                        <div className={`truncate text-[0.7rem] ${l.dim ? 'italic text-muted/70' : 'text-muted'}`} title={l.title || undefined}>{l.text}</div>
                      ) })()}
                    </div>
                    <div className="hidden h-1.5 w-32 shrink-0 overflow-hidden rounded-full bg-bg sm:block">
                      <div className="h-full rounded-full" style={{ width: `${Math.round((svc.last_tps || 0) / maxTps * 100)}%`, background: tpsColor(svc.last_tps) }} />
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="font-mono text-[0.78rem] font-semibold tabular-nums" style={{ color: tpsColor(svc.last_tps) }}>
                        {svc.last_tps || 0} tok/s
                      </div>
                      <div className="text-[0.65rem] text-muted">{fmtAgo(svc.last_ts)}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  )
}
