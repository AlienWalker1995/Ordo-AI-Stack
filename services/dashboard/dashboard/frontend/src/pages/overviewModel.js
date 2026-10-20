// Pure model for the Overview KPI band: turns the /api/overview payload and the /api/perf/series
// samples into the tiles the band draws. It follows the spec's tile table exactly; a tile whose
// source is missing is omitted, never drawn as zero (the exact 24 h token count is gap G3, so
// until it lands the tile estimates from the rate series instead).
import { clock, formatGb } from '../lib/format.js'

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
    // Containers without a compose project are filed under "" by service_counts; label them,
    // or the tile would read "1 failed in ".
    if (stats.failed > 0) { failed += stats.failed; failedProjects.push(project || 'no project') }
    if (stats.unhealthy > 0) { unhealthy += stats.unhealthy; unhealthyProjects.push(project || 'no project') }
  }
  if (!total) return null
  return { up, total, failed, unhealthy, failedProjects, unhealthyProjects }
}

// Tokens generated over the sample span, estimated from the rate series /api/perf/series serves
// (5-minute tok/s samples, last 24 h): each rate integrated over the gap to the next sample,
// capped at one step of the request's resolution. The band polls the endpoint with its default
// 24 h window, which the backend resolves at max(60, hours * 3600 // 288) = 300 s and passes
// through without zero-filling — Prometheus query_range drops the timestamps where rate() has
// no data, so a gap wider than one step means the model was silent. The cap must come from the
// request, not the data: derived from the samples it caps nothing when the series is sparse,
// because then every gap equals or exceeds the true step and the last known rate would be
// extrapolated across the whole silent span.
// Null when neither role has two or more samples, so the tile is left undrawn instead of
// showing a zero.
// One step of the request behind the band's 24 h poll: the backend's max(60, hours * 3600 // 288).
const SERIES_STEP_S = Math.max(60, Math.floor((24 * 3600) / 288))

/** @param {{ gpu?: [number, number][], cpu?: [number, number][] } | undefined} [series] */
export function tokensFromSeries(series) {
  const integrate = (points) => {
    if (!Array.isArray(points) || points.length < 2) return null
    let sum = 0
    for (let i = 1; i < points.length; i++) {
      const [t0, v0] = points[i - 1]
      const t1 = points[i][0]
      if (typeof v0 === 'number' && Number.isFinite(t1) && t1 > t0) {
        sum += v0 * Math.min(t1 - t0, SERIES_STEP_S)
      }
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
    usedGb: gpu.vram_used_gb,
    totalGb: gpu.vram_total_gb,
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

// ---------------------------------------------------------------------------------------------
// Activity feed: grouping and kind filters (spec 3.3). Pure, so the collapse rules are tested
// without a page.
// ---------------------------------------------------------------------------------------------

// Consecutive identical events (same title) collapse into one row with a count and the span
// they cover, while the gap between two members stays inside the grouping window: the same
// restart twice a day apart is two rows, not one x2. The feed arrives newest first, so a group
// keeps the NEWEST member's time and kind (the row is about what happened last), and the worst
// severity of its members (a failed restart inside a run of good ones must not read as all
// fine). A run of leases adds up: its duration is the GPU time lent across the run, and it says
// the chat model was out when any member says so.
const GROUP_WINDOW_S = 6 * 3600
const SEVERITY_RANK = { info: 0, ok: 0, warning: 1, critical: 2 }

/** @param {Array<object>} [items] */
export function groupActivity(items) {
  const groups = []
  for (const item of items || []) {
    const last = groups[groups.length - 1]
    if (last && last.title === item.title && last.oldestTs - item.ts <= GROUP_WINDOW_S) {
      last.count += 1
      last.oldestTs = Math.min(last.oldestTs, item.ts)
      if ((SEVERITY_RANK[item.severity] ?? 0) > (SEVERITY_RANK[last.severity] ?? 0)) {
        last.severity = item.severity
      }
      if (typeof item.duration_s === 'number') last.duration_s = (last.duration_s ?? 0) + item.duration_s
      if (item.evicted_chat) last.evicted_chat = true
    } else {
      groups.push({ ...item, count: 1, oldestTs: item.ts })
    }
  }
  return groups
}

// The feed's kinds split into four scannable categories behind the chips. Model changes are
// the action items that name a model switch; everything else an operator did is a service
// change. Renders and leases stand on their own.
export const ACTIVITY_CATEGORIES = ['all', 'render', 'gpu', 'service', 'model']

/** @param {{ kind?: string, title?: string }} item */
export function activityCategory(item) {
  if (item.kind === 'render') return 'render'
  if (item.kind === 'gpu') return 'gpu'
  return String(item.title || '').startsWith('Switched model') ? 'model' : 'service'
}

/** @param {Array<object>} groups */
export function filterActivity(groups, category) {
  if (!category || category === 'all') return groups || []
  return (groups || []).filter((g) => activityCategory(g) === category)
}

// The daily summary line above the list: today's counts from the same payload the list draws
// from (the feed is capped at the request's limit, so the line covers exactly what the feed
// covers). Nothing today means no line, not "Today: 0 renders".
export function activityDaySummary(items, now = Date.now()) {
  const dayStart = new Date(now)
  dayStart.setHours(0, 0, 0, 0)
  const start = dayStart.getTime() / 1000
  let renders = 0
  let failed = 0
  let leases = 0
  let actions = 0
  for (const item of items || []) {
    if (!(typeof item.ts === 'number' && item.ts >= start)) continue
    if (item.kind === 'render') {
      renders += 1
      if (item.severity === 'critical') failed += 1
    } else if (item.kind === 'gpu') {
      leases += 1
    } else {
      actions += 1
    }
  }
  if (!renders && !leases && !actions) return null
  const parts = [`${renders} render${renders !== 1 ? 's' : ''}${failed ? `, ${failed} failed` : ''}`]
  if (leases) parts.push(`${leases} lease${leases !== 1 ? 's' : ''}`)
  if (actions) parts.push(`${actions} operator action${actions !== 1 ? 's' : ''}`)
  return `Today: ${parts.join(' · ')}`
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
    // The GB figure leads so it survives truncation on a phone; the holders part is omitted
    // when the GPU has neither tenants nor borrowers, or the line would end in a dangling colon.
    const holders = vram.borrowedBy.length
      ? `lent to ${vram.borrowedBy.join(', ')}`
      : vram.tenants.length
        ? `resident: ${vram.tenants.join(', ')}`
        : ''
    tiles.push({
      key: 'vram',
      label: `${vram.name} VRAM`,
      value: `${vram.pct}%`,
      sub: [`${formatGb(vram.usedGb)} / ${formatGb(vram.totalGb)} GB`,
            vram.utilPct != null ? `${vram.utilPct}% compute` : '', holders]
        .filter(Boolean).join(' · '),
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
