// Header hardware bar — CPU / RAM / Disk / GPU widgets from /api/hardware, polled every
// 5s (usePolling pauses while the tab is hidden). Faithful to the legacy `refreshHardware`
// + `renderHwGpuCard`: same value formatting, the same 75%/90% warn/danger bar thresholds,
// and a single GPU card that auto-cycles through all GPUs every 5s (hover to pause,
// click card / dot to switch). Dims to "stale" if no successful fetch for >15s.
import { useEffect, useRef, useState } from 'react'
import { api, usePolling } from '../api.js'

// Legacy `setBar` thresholds: warn at >=75% (<90), danger at >=90%.
function barClass(pct) {
  if (pct >= 90) return 'danger'
  if (pct >= 75) return 'warn'
  return ''
}
const clampPct = (pct) => Math.min(100, Math.max(0, Number(pct) || 0))

function StatCard({ label, value, pct, barKind = '' }) {
  return (
    <div className="hw-stat">
      <span className="hw-stat-label">{label}</span>
      <span className="hw-stat-val">{value}</span>
      <div className={`hw-bar-fill ${barClass(pct)} ${barKind}`.trim()}>
        <span style={{ width: clampPct(pct) + '%' }} />
      </div>
    </div>
  )
}

// Normalize /api/hardware into the cycling GPU list, mirroring the legacy fallback that
// derives a single-GPU list from the `gpu` probe when the `gpus` array is empty.
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

function GpuCard({ gpus }) {
  const [idx, setIdx] = useState(0)
  const pausedRef = useRef(false)
  const countRef = useRef(gpus.length)
  countRef.current = gpus.length

  useEffect(() => { if (idx >= gpus.length) setIdx(0) }, [gpus.length, idx])

  // Auto-cycle every 5s (paused on hover), matching the legacy card.
  useEffect(() => {
    if (gpus.length <= 1) return
    const t = setInterval(() => {
      if (!pausedRef.current) setIdx((i) => (i + 1) % countRef.current)
    }, 5000)
    return () => clearInterval(t)
  }, [gpus.length])

  if (!gpus.length) return null
  const g = gpus[Math.min(idx, gpus.length - 1)]
  const total = Number(g.vram_total_gb) || 0
  const usedRaw = Number(g.vram_used_gb)
  const used = (g.vram_used_gb != null && Number.isFinite(usedRaw)) ? usedRaw : null
  const vramPct = (used != null && total > 0) ? (used / total) * 100 : 0
  const util = g.utilization_pct != null ? Number(g.utilization_pct) : 0
  const name = (g.name || 'GPU').replace(/NVIDIA\s+GeForce\s+/i, '').replace(/NVIDIA\s+/i, '')
  const usedStr = used != null ? `${used.toFixed(1)} / ${total.toFixed(1)} GB` : `— / ${total.toFixed(1)} GB`
  const freeStr = used != null ? `${(total - used).toFixed(1)} GB free` : 'used unavailable'
  const temp = g.temp_c != null ? ` · ${g.temp_c}°C` : ''
  const multi = gpus.length > 1

  return (
    <div
      className="hw-stat hw-stat-gpu hw-gpu-cycle"
      title={multi ? 'Click to show next GPU (auto-cycles; hover to pause)' : undefined}
      onMouseEnter={() => { pausedRef.current = true }}
      onMouseLeave={() => { pausedRef.current = false }}
      onClick={() => { if (multi) setIdx((i) => (i + 1) % gpus.length) }}
    >
      <div className="hw-gpu-top">
        <span className="hw-stat-label" title={g.name || 'GPU'}>
          {name}{multi && <span className="hw-gpu-count"> {idx + 1}/{gpus.length}</span>}
        </span>
        {multi && (
          <span className="hw-gpu-dots">
            {gpus.map((gg, i) => (
              <button
                key={i}
                type="button"
                className={`hw-gpu-dot${i === idx ? ' active' : ''}`}
                title={(gg.name || 'GPU').replace(/NVIDIA\s+/i, '')}
                aria-label={`Show GPU ${i + 1}`}
                onClick={(e) => { e.stopPropagation(); setIdx(i) }}
              />
            ))}
          </span>
        )}
      </div>
      <span className="hw-stat-val">{usedStr}</span>
      <span className="hw-stat-gpu-free">{freeStr + temp}</span>
      <div className={`hw-bar-fill ${barClass(vramPct)}`.trim()}>
        <span style={{ width: vramPct.toFixed(0) + '%' }} />
      </div>
      <span className="hw-stat-sublabel">Compute · {util}%</span>
      <div className="hw-bar-fill gpu-util"><span style={{ width: util + '%' }} /></div>
    </div>
  )
}

export default function HwStatBar() {
  const { data } = usePolling(() => api.get('/api/hardware'), 5000)
  const [stale, setStale] = useState(false)
  const lastOkRef = useRef(Date.now())

  useEffect(() => { if (data) lastOkRef.current = Date.now() }, [data])
  useEffect(() => {
    const t = setInterval(() => setStale(Date.now() - lastOkRef.current > 15000), 3000)
    return () => clearInterval(t)
  }, [])

  const d = data || {}
  const cpu = d.cpu_pct != null ? d.cpu_pct : 0
  const ramUsed = d.ram_used_gb != null ? d.ram_used_gb : 0
  const ramTotal = d.ram_total_gb != null ? d.ram_total_gb : 0
  const ramPct = d.ram_pct != null ? d.ram_pct : 0
  const hasDisk = d.disk_used_gb != null && d.disk_total_gb != null
  const diskPct = d.disk_pct != null
    ? d.disk_pct
    : (hasDisk && d.disk_total_gb > 0 ? (d.disk_used_gb / d.disk_total_gb) * 100 : 0)
  const gpus = deriveGpus(d)

  return (
    <div className={`hw-bar ${stale ? 'stale' : ''}`.trim()} aria-label="System resources" title={stale ? 'Hardware data may be stale' : undefined}>
      <StatCard label="CPU" value={`${Number(cpu).toFixed(0)}%`} pct={cpu} />
      <StatCard label="RAM" value={`${ramUsed} / ${ramTotal} GB`} pct={ramPct} />
      {hasDisk && <StatCard label="Disk" value={`${d.disk_used_gb} / ${d.disk_total_gb} GB`} pct={diskPct} />}
      <GpuCard gpus={gpus} />
    </div>
  )
}
