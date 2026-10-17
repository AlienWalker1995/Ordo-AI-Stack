// The Services page model: pure functions over /api/services/table rows and
// /api/hardware/service-pressure usage, shared by the status strip, the project sections and the
// sortable columns. No React here; the page renders what these return.

import { uptimeSeconds } from './serviceActions.js'

/**
 * One row of GET /api/services/table (dashboard/console.py build_service_table).
 * @typedef {Object} ServiceRow
 * @property {string} id Stable row id ("ordo:<compose>", "host:<container>" or "link:<card>").
 * @property {string} name Display name.
 * @property {string} project Compose project the row belongs to.
 * @property {string | null} container Container name, null for probe-only cards.
 * @property {string | null} compose Compose service name, null outside ordo.
 * @property {string | null} card_id Dashboard card id when the row has one.
 * @property {'up'|'starting'|'unhealthy'|'failed'|'stopped'|'done'|'unknown'} verdict
 * @property {string | null} uptime Docker uptime text, null when not running.
 * @property {string | null} status Raw docker status line.
 * @property {string | null} open_url Link to open, when the row has one.
 * @property {string | null} error Probe failure note, when any.
 * @property {boolean} lent True while a render has borrowed the resident's GPU.
 * @property {string[]} actions Lifecycle actions offered ("start", "restart", "stop").
 * @property {'ordo'|'managed'|'host'|'link'|'none'} control
 * @property {string | null} reason Why a read-only row is read-only.
 */

/**
 * One entry of GET /api/hardware/service-pressure services[].
 * @typedef {Object} PressureEntry
 * @property {number | null} cpu_pct CPU in percent of one core.
 * @property {number | null} mem_gb Memory in GB.
 */

/**
 * Pressure entries keyed by their id, built from services[] the way the page already does
 * (ServicesPage.jsx usageById).
 * @typedef {Record<string, PressureEntry>} PressureMap
 */

/**
 * One project section of the grouped table.
 * @typedef {Object} ProjectSection
 * @property {string} project
 * @property {string} control Header badge text: "Ordo", "Managed", "Managed · restart only"
 *   or "Read-only".
 * @property {ServiceRow[]} rows In input order; sortRows orders them for display.
 * @property {{ total: number, running: number, problem: number, warning: number, job: number }} counts
 * @property {number | null} cpu Sum of measured cpu_pct, null when nothing in the section is measured.
 * @property {number | null} memGb Sum of measured mem_gb, null when nothing in the section is measured.
 * @property {string | null} note The project-level read-only reason, once, or null.
 */

// One definition of "problem", shared by the strip, the sort and the section headers. Stopping
// stays a warning, not a problem: stopping is a legitimate operator choice. "unknown" is neither
// a problem nor confirmed up: the state could not be read, so it reads as information.
export function severity(row) {
  if (row.verdict === 'lent' || row.lent) return 'info'
  switch (row.verdict) {
    case 'failed':
    case 'unhealthy':
      return 'problem'
    case 'starting':
    case 'stopped':
      return 'warning'
    case 'done':
      return 'job'
    case 'up':
      return 'ok'
    default:
      return 'info'
  }
}

// The pressure endpoint keys its entries by compose service id today (its gap for host rows will
// key them by row.id); the page resolves a row's usage by compose first, then card id, and this
// follows that convention.
function usageFor(row, pressure) {
  if (!pressure) return null
  return (row.compose && pressure[row.compose]) || (row.card_id && pressure[row.card_id]) || null
}

const round2 = (value) => Math.round(value * 100) / 100

// Header badge mapping: all rows ordo/host/link -> Ordo; managed -> Managed, suffixed when no row
// in the project offers stop; none -> Read-only. A project mixing kinds claims the stronger one
// first (managed over read-only).
function controlBadge(rows) {
  if (rows.some((row) => row.control === 'managed')) {
    return rows.some((row) => row.actions.includes('stop')) ? 'Managed' : 'Managed · restart only'
  }
  if (rows.some((row) => row.control === 'none')) return 'Read-only'
  return 'Ordo'
}

/**
 * The shared reason of a section's read-only rows, shown once in the section header instead of
 * repeating on every row. Rows without a reason are ignored; differing reasons join with "; ".
 * @param {ProjectSection} section
 * @returns {string | null}
 */
export function projectNote(section) {
  const reasons = [...new Set(
    section.rows.filter((row) => row.control === 'none' && row.reason).map((row) => row.reason),
  )]
  return reasons.length ? reasons.join('; ') : null
}

/**
 * Group flat table rows into one section per compose project, returned in sectionOrder order.
 * @param {ServiceRow[]} rows
 * @param {PressureMap | null} [pressure] Usage per row, for the section aggregates.
 * @returns {ProjectSection[]}
 */
export function groupByProject(rows, pressure = null) {
  const byProject = new Map()
  for (const row of rows) {
    const key = row.project ?? 'unknown'
    if (!byProject.has(key)) byProject.set(key, [])
    byProject.get(key).push(row)
  }
  const sections = [...byProject.entries()].map(([project, sectionRows]) => {
    const counts = { total: sectionRows.length, running: 0, problem: 0, warning: 0, job: 0 }
    let cpu = null
    let memGb = null
    for (const row of sectionRows) {
      if (row.verdict === 'up') counts.running += 1
      const level = severity(row)
      if (level === 'problem') counts.problem += 1
      else if (level === 'warning') counts.warning += 1
      if (row.verdict === 'done') counts.job += 1
      const usage = usageFor(row, pressure)
      if (usage?.cpu_pct != null) cpu = (cpu ?? 0) + usage.cpu_pct
      if (usage?.mem_gb != null) memGb = (memGb ?? 0) + usage.mem_gb
    }
    /** @type {ProjectSection} */
    const section = {
      project,
      control: controlBadge(sectionRows),
      rows: sectionRows,
      counts,
      cpu: cpu == null ? null : round2(cpu),
      memGb: memGb == null ? null : round2(memGb),
      note: null,
    }
    section.note = projectNote(section)
    return section
  })
  return sectionOrder(sections)
}

// Section order: worst health first (any failed/unhealthy), then ordo, then the rest by container
// count desc, ties by name. Within the failing tier, more problems first.
export function sectionOrder(sections) {
  const tier = (section) => {
    if (section.counts.problem > 0) return 0
    if (section.project === 'ordo') return 1
    return 2
  }
  return [...sections].sort((a, b) => {
    const ta = tier(a)
    const tb = tier(b)
    if (ta !== tb) return ta - tb
    if (ta === 0 && a.counts.problem !== b.counts.problem) return b.counts.problem - a.counts.problem
    if (ta === 2 && a.rows.length !== b.rows.length) return b.rows.length - a.rows.length
    return a.project.localeCompare(b.project)
  })
}

// Row order inside a section: problems first (failed, unhealthy, starting, stopped), then running
// by name, finished jobs last. The rank map keeps that exact order; lent residents — the backend
// sends them as stopped or failed with lent: true, never a "lent" verdict — and "unknown" sit
// between the warnings and the running rows, where their severity puts them.
const ROW_RANK = { failed: 0, unhealthy: 1, starting: 2, stopped: 3, lent: 4, unknown: 5, up: 6, done: 7 }

/**
 * Sort rows for display within one section. Returns a new array; the input keeps its order.
 * @param {ServiceRow[]} rows
 * @param {{ key?: 'severity'|'name'|'uptime'|'cpu'|'mem', dir?: 'asc'|'desc' }} [options]
 *   Default key "severity": worst first, ties by name, finished jobs last in either direction.
 *   "uptime" asc is recent first (shortest uptime); unparseable uptimes sort last. "cpu" and
 *   "mem" read the pressure map; unmeasured rows sort last.
 * @param {PressureMap | null} [pressure] Needed for the "cpu" and "mem" keys.
 * @returns {ServiceRow[]}
 */
export function sortRows(rows, options = {}, pressure = null) {
  const { key = 'severity', dir = 'asc' } = options
  const sign = dir === 'desc' ? -1 : 1
  const byName = (a, b) => a.name.localeCompare(b.name)
  const compare = (a, b) => {
    switch (key) {
      case 'name':
        return sign * byName(a, b)
      case 'uptime': {
        const ua = uptimeSeconds(a.uptime)
        const ub = uptimeSeconds(b.uptime)
        if (ua == null) return ub == null ? 0 : 1
        if (ub == null) return -1
        return sign * (ua - ub)
      }
      case 'cpu':
      case 'mem': {
        const field = key === 'cpu' ? 'cpu_pct' : 'mem_gb'
        const va = usageFor(a, pressure)?.[field]
        const vb = usageFor(b, pressure)?.[field]
        if (va == null) return vb == null ? 0 : 1
        if (vb == null) return -1
        return sign * (va - vb)
      }
      default: {
        // Finished jobs stay last in either direction: they are not a problem being sorted out.
        const ja = a.verdict === 'done' ? 1 : 0
        const jb = b.verdict === 'done' ? 1 : 0
        if (ja !== jb) return ja - jb
        // A lent resident arrives as stopped (or failed) with lent: true; its severity is info,
        // so it sorts in the lent slot, not the warning tier its verdict would give it.
        const ra = a.lent ? ROW_RANK.lent : ROW_RANK[a.verdict] ?? 9
        const rb = b.lent ? ROW_RANK.lent : ROW_RANK[b.verdict] ?? 9
        if (ra !== rb) return sign * (ra - rb)
        return byName(a, b)
      }
    }
  }
  return [...rows].sort(compare)
}

/**
 * The identifier shown inline after a row's name: the compose service when it differs from the
 * display name, otherwise the container name without its "<project>-" prefix and "-1" suffix.
 * Null when the row carries neither (probe-only cards show the name alone).
 * @param {ServiceRow} row
 * @returns {string | null}
 */
export function displayIdentifier(row) {
  if (row.compose && row.compose !== row.name) return row.compose
  const container = row.container
  if (!container) return null
  let out = container
  const prefix = `${row.project}-`
  if (row.project && out.startsWith(prefix)) out = out.slice(prefix.length)
  if (out.endsWith('-1')) out = out.slice(0, -2)
  return out || container
}

/**
 * Counts for the status strip: the toggle buttons and their numbers. "running" counts verdict
 * "up" only; a lent row reads as information (severity), so it counts toward total only.
 * @param {ServiceRow[]} rows
 * @returns {{ total: number, running: number, failed: number, unhealthy: number, stopped: number, jobs: number }}
 */
export function statusCounts(rows) {
  const counts = { total: rows.length, running: 0, failed: 0, unhealthy: 0, stopped: 0, jobs: 0 }
  for (const row of rows) {
    if (row.verdict === 'up') counts.running += 1
    else if (row.lent) continue
    else if (row.verdict === 'failed') counts.failed += 1
    else if (row.verdict === 'unhealthy') counts.unhealthy += 1
    else if (row.verdict === 'stopped') counts.stopped += 1
    else if (row.verdict === 'done') counts.jobs += 1
  }
  return counts
}
