// Hardware / GPU detail tab — the per-GPU DETAIL view (the header HwStatBar shows the
// summary). Consumes:
//   - GET /api/hardware                  — full CPU/RAM/disk + per-GPU list (`gpus`)
//   - GET /api/hardware/service-pressure — per-service CPU/mem/VRAM pressure table
// Ported from the legacy loadGpuTab + service-pressure panels: one detail card per GPU
// (name / VRAM used-total + bar / compute util / temp) and a pressure table sorted by the
// busiest resource. Polls every 5s (usePolling pauses while the tab is hidden).
import { api, usePolling } from '../api.js'

// Shared bar thresholds (faithful to the legacy setBar / hw-bar-fill: warn >=75, danger >=90).
function barClass(pct) {
  if (pct >= 90) return 'danger'
  if (pct >= 75) return 'warn'
  return ''
}
const clampPct = (pct) => Math.min(100, Math.max(0, Number(pct) || 0))
const fmt1 = (v) => (Number.isFinite(Number(v)) ? Number(v).toFixed(1) : '—')

const HW_FILL = 'hw-bar-fill mt-1 h-1 overflow-hidden rounded-full bg-surface'

// Normalize /api/hardware into a per-GPU list, mirroring HwStatBar's deriveGpus fallback:
// prefer the `gpus` array; else derive a single entry from the `gpu` probe.
function deriveGpus(d) {
  if (!d) return []
  const list = Array.isArray(d.gpus) && d.gpus.length
    ? d.gpus
    : (d.gpu && d.gpu.vram_total_gb != null
      ? [{
          name: d.gpu.name,
          vram_total_gb: d.gpu.vram_total_gb,
          vram_used_gb: d.gpu.memory_reading_reliable !== false ? d.gpu.vram_used_gb : null,
          utilization_pct: d.gpu.utilization_pct,
          temp_c: null,
        }]
      : [])
  return list
    .filter((g) => Number(g.vram_total_gb) > 0)
    .sort((a, b) => (Number(b.vram_total_gb) || 0) - (Number(a.vram_total_gb) || 0))
}

function Bar({ pct, kind = '' }) {
  return (
    <div className={`${HW_FILL} ${barClass(pct)} ${kind}`.trim()}>
      <span style={{ width: clampPct(pct) + '%' }} />
    </div>
  )
}

function StatBlock({ label, value, sub, pct }) {
  return (
    <div className="rounded-md border border-border-subtle bg-bg-elevated p-4">
      <span className="text-[0.6rem] font-bold uppercase tracking-[0.12em] text-muted">{label}</span>
      <div className="mt-1 font-mono text-[0.95rem] font-semibold tabular-nums text-fg">{value}</div>
      <Bar pct={pct} />
      {sub && <p className="mt-1 font-mono text-[0.65rem] text-muted">{sub}</p>}
    </div>
  )
}

function GpuDetailCard({ g }) {
  const total = Number(g.vram_total_gb) || 0
  const usedRaw = Number(g.vram_used_gb)
  const used = (g.vram_used_gb != null && Number.isFinite(usedRaw)) ? usedRaw : null
  const vramPct = (used != null && total > 0) ? (used / total) * 100 : 0
  const util = g.utilization_pct != null ? Number(g.utilization_pct) : 0
  const name = (g.name || 'GPU').replace(/NVIDIA\s+GeForce\s+/i, '').replace(/NVIDIA\s+/i, '')

  return (
    <div className="rounded-md border border-border-subtle bg-bg-elevated p-5">
      <div className="mb-3 flex items-start justify-between gap-3">
        <span className="min-w-0 truncate text-[0.9rem] font-semibold text-fg" title={g.name || 'GPU'}>{name}</span>
        {g.temp_c != null && (
          <span className="shrink-0 rounded-full border border-border-subtle bg-bg px-2 py-0.5 font-mono text-[0.7rem] text-muted">
            {g.temp_c}°C
          </span>
        )}
      </div>

      <div className="mb-1 flex items-center justify-between text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">
        <span>VRAM</span>
        <span className="font-mono normal-case tracking-normal text-fg-muted">
          {used != null ? `${fmt1(used)} / ${fmt1(total)} GB` : `— / ${fmt1(total)} GB`}
        </span>
      </div>
      <Bar pct={vramPct} />
      <p className="mt-1 font-mono text-[0.65rem] text-muted">
        {used != null ? `${fmt1(total - used)} GB free · ${Math.round(vramPct)}% used` : 'VRAM reading unavailable'}
      </p>

      <div className="mt-3 mb-1 flex items-center justify-between text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">
        <span>Compute</span>
        <span className="font-mono normal-case tracking-normal text-fg-muted">{util}%</span>
      </div>
      <Bar pct={util} kind="gpu-util" />
    </div>
  )
}

function PressureBar({ pct }) {
  return (
    <div className="h-1.5 w-full min-w-[60px] overflow-hidden rounded-full bg-bg">
      <div
        className={
          'h-full rounded-full transition-[width] ' +
          (pct >= 90 ? 'bg-danger' : pct >= 75 ? 'bg-warning' : 'bg-accent')
        }
        style={{ width: clampPct(pct) + '%' }}
      />
    </div>
  )
}

export default function HardwareTab() {
  const { data, error } = usePolling(async () => {
    const [hw, pressure] = await Promise.all([
      api.get('/api/hardware'),
      api.get('/api/hardware/service-pressure').catch(() => null),
    ])
    return { hw, pressure }
  }, 5000)

  const hw = data?.hw
  const pressure = data?.pressure
  const gpus = deriveGpus(hw)

  const cpuPct = hw?.cpu_pct != null ? Number(hw.cpu_pct) : 0
  const ramPct = hw?.ram_pct != null ? Number(hw.ram_pct) : 0
  const hasDisk = hw?.disk_used_gb != null && hw?.disk_total_gb != null
  const diskPct = hw?.disk_pct != null ? Number(hw.disk_pct) : 0

  const services = pressure?.services || []
  const vramUnavail = pressure?.vram_aggregate_unavailable === true

  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        Hardware / GPU
      </h2>
      <p className="mb-5 text-[0.8125rem] leading-[1.5] text-muted">
        Per-GPU VRAM and compute utilization, plus the compute pressure each service is
        putting on the host. Refreshes every 5 seconds.
      </p>

      {error && !data ? (
        <div className="flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
          Could not load hardware stats — check that the dashboard API is up.
        </div>
      ) : (
        <>
          {/* Host CPU / RAM / Disk detail */}
          <div className="mb-6 grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(220px,1fr))]">
            {!hw ? (
              [0, 1, 2].map((i) => <div key={i} className="skeleton h-20 w-full" />)
            ) : (
              <>
                <StatBlock label="CPU" value={`${Math.round(cpuPct)}%`} sub={pressure?.host?.cpu_cores ? `${pressure.host.cpu_cores} cores` : undefined} pct={cpuPct} />
                <StatBlock label="RAM" value={`${hw.ram_used_gb ?? '—'} / ${hw.ram_total_gb ?? '—'} GB`} sub={`${Math.round(ramPct)}% used`} pct={ramPct} />
                {hasDisk && (
                  <StatBlock label="Disk" value={`${hw.disk_used_gb} / ${hw.disk_total_gb} GB`} sub={`${Math.round(diskPct)}% used`} pct={diskPct} />
                )}
              </>
            )}
          </div>

          {/* Per-GPU detail cards */}
          <h3 className="mb-3 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">GPUs</h3>
          {!hw ? (
            <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))]">
              {[0, 1].map((i) => <div key={i} className="skeleton h-40 w-full" />)}
            </div>
          ) : gpus.length === 0 ? (
            <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">
              No GPU data available. WSL passthrough may be down — try <code className="text-accent-soft">wsl --shutdown</code> then restart Docker.
            </div>
          ) : (
            <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))]">
              {gpus.map((g, i) => <GpuDetailCard key={g.name || i} g={g} />)}
            </div>
          )}

          {/* Service pressure table */}
          <div className="mt-8 border-t border-border-subtle pt-6">
            <h3 className="mb-1 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">Service pressure</h3>
            <p className="mb-4 text-[0.8125rem] leading-[1.5] text-muted">
              CPU, memory, and VRAM each service is consuming, busiest first.
              {vramUnavail && ' Per-service VRAM breakdown is unavailable on this host.'}
            </p>
            {!pressure ? (
              <div className="space-y-2">
                {[0, 1, 2].map((i) => <div key={i} className="skeleton h-9 w-full" />)}
              </div>
            ) : services.length === 0 ? (
              <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">
                No service pressure data — ops-controller may be unreachable.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] border-collapse text-[0.8125rem]">
                  <thead>
                    <tr className="text-left text-[0.65rem] font-semibold uppercase tracking-[0.08em] text-muted">
                      <th className="border-b border-border-subtle px-3 py-2">Service</th>
                      <th className="border-b border-border-subtle px-3 py-2 text-right">CPU</th>
                      <th className="border-b border-border-subtle px-3 py-2">Memory</th>
                      {!vramUnavail && <th className="border-b border-border-subtle px-3 py-2">VRAM</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {services.map((s) => (
                      <tr key={s.id} className="align-middle transition-colors hover:bg-white/[0.02]">
                        <td className="border-b border-border-subtle px-3 py-2">
                          <div className="flex items-center gap-2">
                            <span
                              className={`status-dot ${s.running ? 'ok' : 'fail'}`}
                              aria-hidden="true"
                              title={s.running ? 'Running' : 'Not running'}
                            />
                            <span className="truncate font-medium text-fg" title={s.id}>{s.name || s.id}</span>
                          </div>
                        </td>
                        <td className="border-b border-border-subtle px-3 py-2 text-right font-mono tabular-nums text-fg-muted">
                          {fmt1(s.cpu_pct)}%
                        </td>
                        <td className="border-b border-border-subtle px-3 py-2">
                          <div className="flex items-center gap-2">
                            <PressureBar pct={s.mem_pct} />
                            <span className="w-24 shrink-0 text-right font-mono text-[0.72rem] tabular-nums text-fg-muted">
                              {fmt1(s.mem_gb)} GB · {Math.round(s.mem_pct)}%
                            </span>
                          </div>
                        </td>
                        {!vramUnavail && (
                          <td className="border-b border-border-subtle px-3 py-2">
                            {s.has_gpu || s.vram_gb > 0 ? (
                              <div className="flex items-center gap-2">
                                <PressureBar pct={s.vram_pct} />
                                <span className="w-24 shrink-0 text-right font-mono text-[0.72rem] tabular-nums text-fg-muted">
                                  {fmt1(s.vram_gb)} GB · {Math.round(s.vram_pct)}%
                                </span>
                              </div>
                            ) : (
                              <span className="font-mono text-[0.72rem] text-muted">—</span>
                            )}
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  )
}
