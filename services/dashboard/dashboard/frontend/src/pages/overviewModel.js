// Pure model for the Overview KPI band: turns the /api/overview payload and the /api/perf/series
// samples into the tiles the band draws. It follows the spec's tile table exactly; a tile whose
// source is missing is omitted, never drawn as zero (the exact 24 h token count is gap G3, so
// until it lands the tile estimates from the rate series instead).
import { clock } from '../lib/format.js'

// Container counts across every project from /api/overview services.by_project (G2): the status
// line and the Containers-up tile count the same containers, ordo and non-ordo alike.
/** @param {Record<string, { total?: number, up?: number, failed?: number, unhealthy?: number }> | null | undefined} [byProject] */
export function countContainers(byProject) {
  if (!byProject || typeof byProject !== 'object') return null
  let total = 0
  let up = 0
  let failed = 0
  let unhealthy = 0
  const failedProjects = []
  const unhealthyProjects = []
  for (const [project, stats] of Object.entries(byProject)) {
    if (!stats || typeof stats.total !== 'number') continue
    total += stats.total
    up += stats.up || 0
    if (stats.failed > 0) { failed += stats.failed; failedProjects.push(project) }
    if (stats.unhealthy > 0) { unhealthy += stats.unhealthy; unhealthyProjects.push(project) }
  }
  if (!total) return null
  return { up, total, failed, unhealthy, failedProjects, unhealthyProjects }
}

// Tokens generated over the sample span, estimated from the rate series /api/perf/series serves
// (5-minute tok/s samples, last 24 h): each rate integrated over the gap to the next sample.
// Null when neither role has two or more samples, so the tile is left undrawn instead of
// showing a zero.
/** @param {{ gpu?: [number, number][], cpu?: [number, number][] } | undefined} [series] */
export function tokensFromSeries(series) {
  const integrate = (points) => {
    if (!Array.isArray(points) || points.length < 2) return null
    let sum = 0
    for (let i = 1; i < points.length; i++) {
      const [t0, v0] = points[i - 1]
      const t1 = points[i][0]
      if (typeof v0 === 'number' && Number.isFinite(t1) && t1 > t0) sum += v0 * (t1 - t0)
    }
    return sum
  }
  const parts = [integrate(series?.gpu), integrate(series?.cpu)].filter((v) => v != null)
  return parts.length ? parts.reduce((a, b) => a + b, 0) : null
}

// "~1.2 M": the estimate is always marked, because the exact 24 h count is gap G3.
/** @param {number | null | undefined} value */
export function formatTokens(value) {
  if (value == null) return null
  if (value >= 1e6) return `~${(value / 1e6).toFixed(1)} M`
  if (value >= 1e3) return `~${Math.round(value / 1e3)} k`
  return `~${Math.round(value)}`
}

// The median speed per chat role, passed through untouched (p50, n, provisional, model): the
// Median primitive renders the provisional mark, the tile only needs the evidence beside it.
/** @param {{ gpu?: object, cpu?: object } | undefined} [chat] */
export function speedByRole(chat) {
  const out = {}
  for (const role of ['gpu', 'cpu']) {
    const m = chat?.[role]
    if (m && typeof m.p50 === 'number') out[role] = m
  }
  return out
}

// VRAM on the first GPU that reports usable totals (the resident-model card, gpus[0]).
/** @param {Array<object> | undefined} [gpus] */
export function vramFacts(gpus) {
  const gpu = Array.isArray(gpus)
    ? gpus.find((g) => g && typeof g.vram_total_gb === 'number' && g.vram_total_gb > 0)
    : null
  if (!gpu || typeof gpu.vram_used_gb !== 'number') return null
  const pctUsed = Math.max(0, Math.min(100, (gpu.vram_used_gb / gpu.vram_total_gb) * 100))
  return {
    name: gpu.name || 'GPU',
    pct: Math.round(pctUsed),
    utilPct: gpu.util_pct ?? null,
    tenants: gpu.tenants || [],
    borrowedBy: gpu.borrowed_by || [],
  }
}

// Disk from the overview's host block; the warning threshold matches the Host panel (80%).
/** @param {{ disk_pct?: number, disk_used_gb?: number, disk_total_gb?: number } | undefined} [host] */
export function diskFacts(host) {
  if (!host || typeof host.disk_pct !== 'number') return null
  return {
    pct: Math.round(host.disk_pct),
    warn: host.disk_pct >= 80,
    usedGb: host.disk_used_gb ?? null,
    totalGb: host.disk_total_gb ?? null,
  }
}

// The median's evidence as a tooltip: how many samples, over what span.
function medianEvidence(m) {
  if (!m.n) return 'No samples yet'
  const span = m.first_ts && m.last_ts ? `, ${clock(m.first_ts)} to ${clock(m.last_ts)}` : ''
  return `${m.n} sample${m.n !== 1 ? 's' : ''}${span}`
}

const ENGINE_LABEL = { gpu: 'GPU', cpu: 'CPU fallback', none: 'None' }

// The six tiles of the KPI band, in the spec's order. Each is omitted when its source is
// missing, so the band draws fewer tiles rather than placeholder zeros.
/**
 * @param {{ services?: object, chat?: object, gpus?: Array<object>, host?: object } | undefined} overview
 * @param {{ gpu?: [number, number][], cpu?: [number, number][] } | undefined} series
 */
export function kpiTiles(overview, series) {
  const tiles = []
  const containers = countContainers(overview?.services?.by_project)
  if (containers) {
    const problems = [
      containers.failed ? `${containers.failed} failed in ${containers.failedProjects.join(', ')}` : '',
      containers.unhealthy ? `${containers.unhealthy} unhealthy in ${containers.unhealthyProjects.join(', ')}` : '',
    ].filter(Boolean)
    tiles.push({
      key: 'containers',
      label: 'Containers up',
      value: `${containers.up} / ${containers.total}`,
      sub: problems.length ? problems.join(' · ') : 'all projects up',
      tone: containers.failed ? 'danger' : containers.unhealthy ? 'warning' : 'fg',
      title: 'services.by_project from /api/overview',
    })
  }
  const engine = overview?.chat?.engine
  if (ENGINE_LABEL[engine]) {
    const chat = overview.chat
    const active = engine === 'cpu' ? chat.cpu : chat.gpu
    tiles.push({
      key: 'engine',
      label: 'Chat engine',
      value: ENGINE_LABEL[engine],
      sub: chat.reason || active?.model || '',
      tone: engine === 'none' ? 'danger' : engine === 'cpu' ? 'warning' : 'fg',
      title: 'chat.engine from /api/overview',
    })
  }
  const speeds = speedByRole(overview?.chat)
  if (speeds.gpu) {
    const m = speeds.gpu
    tiles.push({
      key: 'speed',
      label: 'GPU chat speed',
      median: { value: m.p50, n: m.n || 0, provisional: !!m.provisional, title: medianEvidence(m) },
      sub: m.provisional ? '' : m.n ? `${m.n} samples` : '',
      sparkline: Array.isArray(series?.gpu) ? series.gpu : undefined,
      title: 'chat.gpu.p50 from /api/overview',
    })
  }
  const tokens = tokensFromSeries(series)
  if (tokens != null) {
    tiles.push({
      key: 'tokens',
      label: 'Tokens generated, 24 h',
      value: formatTokens(tokens),
      sub: 'estimated from 5 min rates',
      title: 'integrated /api/perf/series gpu and cpu samples',
    })
  }
  const vram = vramFacts(overview?.gpus)
  if (vram) {
    const holders = vram.borrowedBy.length
      ? `lent to ${vram.borrowedBy.join(', ')}`
      : `resident: ${vram.tenants.join(', ')}`
    tiles.push({
      key: 'vram',
      label: `${vram.name} VRAM`,
      value: `${vram.pct}%`,
      sub: [vram.utilPct != null ? `${vram.utilPct}% compute` : '', holders].filter(Boolean).join(' · '),
      tone: vram.pct >= 95 ? 'warning' : 'fg',
      title: 'gpus[0] from /api/overview',
    })
  }
  const disk = diskFacts(overview?.host)
  if (disk) {
    tiles.push({
      key: 'disk',
      label: 'Disk',
      value: `${disk.pct}%`,
      sub: disk.usedGb != null && disk.totalGb != null
        ? `${(disk.usedGb / 1000).toFixed(2)} / ${(disk.totalGb / 1000).toFixed(2)} TB`
        : '',
      tone: disk.warn ? 'warning' : 'fg',
      title: 'host.disk from /api/overview',
    })
  }
  return tiles
}
