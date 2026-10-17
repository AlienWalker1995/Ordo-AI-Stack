// The Services model: severity, project grouping, section order, row sort, display identifier
// and the strip counts, against the live-shaped table fixture.
import { describe, expect, it } from 'vitest'
import {
  displayIdentifier, groupByProject, projectNote, sectionOrder, severity, sortRows, statusCounts,
} from '../src/pages/servicesModel.js'
import { fixture } from './mockApi.js'

const TABLE = '/api/services/table'
const rows = () => fixture(TABLE).rows

// A minimal row; the tests patch only what they exercise.
function row(overrides = {}) {
  return {
    id: 'x', name: 'x', project: 'p', container: null, compose: null, card_id: null,
    verdict: 'up', uptime: null, status: null, open_url: null, error: null, lent: false,
    actions: [], control: 'ordo', reason: null, ...overrides,
  }
}

describe('severity', () => {
  it.each([
    ['up', {}, 'ok'],
    ['starting', {}, 'warning'],
    ['unhealthy', {}, 'problem'],
    ['failed', {}, 'problem'],
    ['stopped', {}, 'warning'],
    // Stopping is a legitimate operator choice, but a lent resident is information, not a warning.
    ['stopped', { lent: true }, 'info'],
    ['done', {}, 'job'],
    ['unknown', {}, 'info'],
  ])('verdict %j %j reads as %s', (verdict, extra, expected) => {
    expect(severity(row({ verdict, ...extra }))).toBe(expected)
  })
})

describe('groupByProject', () => {
  it('orders the sections with the failed project first, then ordo, then the rest', () => {
    const sections = groupByProject(rows())
    expect(sections.map((s) => s.project)).toEqual(['media-stack', 'ordo', 'web-site'])
  })

  it('keeps every row in exactly one section, in input order inside it', () => {
    const sections = groupByProject(rows())
    const flat = sections.flatMap((s) => s.rows)
    expect(flat).toHaveLength(rows().length)
    expect(new Set(flat.map((r) => r.id)).size).toBe(flat.length)
    const media = sections.find((s) => s.project === 'media-stack')
    expect(media.rows.map((r) => r.name)).toEqual(['media-indexer', 'media-server'])
  })

  it('counts per section: total, running, problem, warning, job', () => {
    const byName = Object.fromEntries(groupByProject(rows()).map((s) => [s.project, s.counts]))
    expect(byName['media-stack']).toEqual({ total: 2, running: 1, problem: 1, warning: 0, job: 0 })
    expect(byName.ordo).toEqual({ total: 12, running: 10, problem: 1, warning: 0, job: 1 })
    expect(byName['web-site']).toEqual({ total: 1, running: 1, problem: 0, warning: 0, job: 0 })
  })

  it('derives the header badge from the rows\' control kind', () => {
    const byName = Object.fromEntries(groupByProject(rows()).map((s) => [s.project, s.control]))
    expect(byName.ordo).toBe('Ordo')
    // No media-stack row offers stop, so the managed badge carries its suffix.
    expect(byName['media-stack']).toBe('Managed · restart only')
    expect(byName['web-site']).toBe('Read-only')
    expect(groupByProject([row({ control: 'managed', actions: ['restart', 'stop'] })])[0].control).toBe('Managed')
  })

  it('sums measured usage per section and stays null-safe without pressure', () => {
    const pressure = {
      caddy: { cpu_pct: 0.1, mem_gb: 0.2 },
      comfyui: { cpu_pct: 3.1, mem_gb: 6.8 },
      // Keyed by card id, not compose: the Open WebUI row resolves it through the fallback.
      webui: { cpu_pct: 1.5, mem_gb: 0.6 },
    }
    const withPressure = Object.fromEntries(groupByProject(rows(), pressure).map((s) => [s.project, s]))
    expect(withPressure.ordo.cpu).toBeCloseTo(4.7)
    expect(withPressure.ordo.memGb).toBeCloseTo(7.6)
    expect(withPressure['media-stack'].cpu).toBeNull()
    expect(withPressure['media-stack'].memGb).toBeNull()

    const without = Object.fromEntries(groupByProject(rows()).map((s) => [s.project, s]))
    for (const section of Object.values(without)) {
      expect(section.cpu).toBeNull()
      expect(section.memGb).toBeNull()
    }
    // A null entry measures nothing either.
    const partial = groupByProject(rows(), { caddy: { cpu_pct: null, mem_gb: null } })[1]
    expect(partial.cpu).toBeNull()
    expect(partial.memGb).toBeNull()
  })

  it('does not reorder or mutate its input', () => {
    const input = rows()
    const before = input.map((r) => r.id)
    groupByProject(input)
    expect(input.map((r) => r.id)).toEqual(before)
  })
})

describe('sectionOrder', () => {
  function section(project, { problems = 0, size = 1 } = {}) {
    return { project, counts: { total: size, running: 0, problem: problems, warning: 0, job: 0 },
             rows: new Array(size).fill(null) }
  }

  it('puts failing projects first, more problems ahead of fewer', () => {
    const ordered = sectionOrder([section('b', { problems: 1 }), section('a', { problems: 2 }), section('c')])
    expect(ordered.map((s) => s.project)).toEqual(['a', 'b', 'c'])
  })

  it('sits ordo second even when it has more containers than the others', () => {
    const ordered = sectionOrder([section('big', { size: 50 }), section('ordo'), section('small')])
    expect(ordered.map((s) => s.project)).toEqual(['ordo', 'big', 'small'])
  })

  it('orders the rest by container count desc, ties by name', () => {
    const ordered = sectionOrder([section('zeta', { size: 3 }), section('alpha', { size: 3 }), section('mid', { size: 9 })])
    expect(ordered.map((s) => s.project)).toEqual(['mid', 'alpha', 'zeta'])
  })

  it('lets a failing ordo join the failing tier', () => {
    const ordered = sectionOrder([section('ordo', { problems: 1 }), section('other', { problems: 3 })])
    expect(ordered.map((s) => s.project)).toEqual(['other', 'ordo'])
  })
})

describe('sortRows', () => {
  const ordo = () => rows().filter((r) => r.project === 'ordo')

  it('defaults to severity then name, with finished jobs last', () => {
    expect(sortRows(ordo()).map((r) => r.name)).toEqual([
      'Codebase Memory', 'caddy', 'ComfyUI', 'llama.cpp (CPU fallback)', 'llama.cpp (GPU)',
      'mcp-searxng', 'model-gateway', 'N8N', 'Open WebUI', 'ops-controller', 'Qdrant', 'evals',
    ])
    // Descending flips the health order but keeps the jobs last.
    expect(sortRows(ordo(), { dir: 'desc' }).map((r) => r.name)).toEqual([
      'caddy', 'ComfyUI', 'llama.cpp (CPU fallback)', 'llama.cpp (GPU)', 'mcp-searxng',
      'model-gateway', 'N8N', 'Open WebUI', 'ops-controller', 'Qdrant', 'Codebase Memory', 'evals',
    ])
  })

  it('puts the failed row first in its section', () => {
    const media = rows().filter((r) => r.project === 'media-stack')
    expect(sortRows(media)[0].name).toBe('media-indexer')
  })

  it('keeps an evicted resident behind the real warnings when a render borrows the GPU', () => {
    // The backend sends the evicted resident as stopped with lent: true, never a "lent" verdict;
    // its severity is info, so it sorts behind the genuine stopped rows, not among them.
    const evicted = ordo().map((r) => {
      if (r.id === 'ordo:llamacpp') return { ...r, verdict: 'stopped', lent: true }
      if (r.id === 'ordo:caddy' || r.id === 'ordo:ops-controller') return { ...r, verdict: 'stopped' }
      return r
    })
    expect(sortRows(evicted).map((r) => r.name)).toEqual([
      'Codebase Memory', 'caddy', 'ops-controller', 'llama.cpp (GPU)', 'ComfyUI',
      'llama.cpp (CPU fallback)', 'mcp-searxng', 'model-gateway', 'N8N', 'Open WebUI', 'Qdrant', 'evals',
    ])
    // Descending flips the health order but keeps the lent row behind the running ones.
    expect(sortRows(evicted, { dir: 'desc' }).map((r) => r.name)).toEqual([
      'ComfyUI', 'llama.cpp (CPU fallback)', 'mcp-searxng', 'model-gateway', 'N8N', 'Open WebUI',
      'Qdrant', 'llama.cpp (GPU)', 'caddy', 'ops-controller', 'Codebase Memory', 'evals',
    ])
  })

  it('sorts by name in both directions', () => {
    const names = ordo().map((r) => r.name)
    expect(sortRows(ordo(), { key: 'name' }).map((r) => r.name)).toEqual([...names].sort((a, b) => a.localeCompare(b)))
    expect(sortRows(ordo(), { key: 'name', dir: 'desc' }).map((r) => r.name)).toEqual(
      [...names].sort((a, b) => a.localeCompare(b)).reverse(),
    )
  })

  it('sorts by uptime recent first, unparseable uptimes last, in both directions', () => {
    const recentFirst = sortRows(ordo(), { key: 'uptime' }).map((r) => r.name)
    expect(recentFirst[0]).toBe('N8N') // 15 minutes
    expect(recentFirst.slice(-2)).toEqual(['Codebase Memory', 'evals']) // no uptime text
    const oldestFirst = sortRows(ordo(), { key: 'uptime', dir: 'desc' }).map((r) => r.name)
    expect(oldestFirst[0]).toBe('caddy') // 2 days
    expect(oldestFirst.slice(-2)).toEqual(['Codebase Memory', 'evals'])
  })

  it('sorts by cpu and memory from the pressure map, unmeasured rows last', () => {
    const pressure = { caddy: { cpu_pct: 0.1, mem_gb: 0.2 }, comfyui: { cpu_pct: 3.1, mem_gb: 6.8 }, webui: { cpu_pct: 1.5, mem_gb: 0.6 } }
    const measured = ['caddy', 'Open WebUI', 'ComfyUI']
    expect(sortRows(ordo(), { key: 'cpu' }, pressure).slice(0, 3).map((r) => r.name)).toEqual(measured)
    expect(sortRows(ordo(), { key: 'cpu', dir: 'desc' }, pressure).slice(0, 3).map((r) => r.name)).toEqual([...measured].reverse())
    expect(sortRows(ordo(), { key: 'mem' }, pressure).slice(0, 3).map((r) => r.name)).toEqual(measured)
    expect(sortRows(ordo(), { key: 'mem', dir: 'desc' }, pressure).slice(0, 3).map((r) => r.name)).toEqual([...measured].reverse())
    // Without a pressure map nothing is measured: the input order stands.
    expect(sortRows(ordo(), { key: 'cpu' }).map((r) => r.name)).toEqual(ordo().map((r) => r.name))
  })

  it('leaves the input array in place', () => {
    const input = ordo()
    const before = input.map((r) => r.id)
    sortRows(input, { key: 'name' })
    expect(input.map((r) => r.id)).toEqual(before)
  })
})

describe('displayIdentifier', () => {
  it('shows the compose service when it differs from the display name', () => {
    const byId = Object.fromEntries(rows().map((r) => [r.id, r]))
    expect(displayIdentifier(byId['ordo:comfyui'])).toBe('comfyui')
    expect(displayIdentifier(byId['ordo:open-webui'])).toBe('open-webui')
    expect(displayIdentifier(byId['ordo:llamacpp'])).toBe('llamacpp')
  })

  it('strips the project prefix and -1 suffix from the container otherwise', () => {
    const byId = Object.fromEntries(rows().map((r) => [r.id, r]))
    expect(displayIdentifier(byId['ordo:caddy'])).toBe('caddy')
    expect(displayIdentifier(byId['host:media-server'])).toBe('media-server')
    expect(displayIdentifier(byId['host:web-site-web-1'])).toBe('web')
    expect(displayIdentifier(row({ project: 'blog', container: 'blog-web-dev-1', name: 'blog-web-dev-1' }))).toBe('web-dev')
    expect(displayIdentifier(row({ project: 'data-lake', container: 'data-lake-etl-1', name: 'data-lake-etl-1' }))).toBe('etl')
    // No prefix to strip and no -1 suffix: the container stands as is.
    expect(displayIdentifier(row({ project: 'adguard', container: 'adguard', name: 'adguard' }))).toBe('adguard')
  })

  it('returns null for probe-only cards that carry neither compose nor container', () => {
    expect(displayIdentifier(row({ name: 'External Tool', control: 'link' }))).toBeNull()
  })
})

describe('projectNote', () => {
  it('carries the read-only reason once on its section', () => {
    const webSite = groupByProject(rows()).find((s) => s.project === 'web-site')
    expect(webSite.note).toBe('add web-site to managed_projects in ordo.yaml')
    expect(projectNote(webSite)).toBe(webSite.note)
    expect(groupByProject(rows()).find((s) => s.project === 'ordo').note).toBeNull()
  })

  it('repeats a shared reason once across many rows and joins differing ones', () => {
    const same = row({ control: 'none', reason: 'add p to managed_projects in ordo.yaml' })
    const section = { rows: [same, { ...same, id: 'y' }, { ...same, id: 'z' }] }
    expect(projectNote(section)).toBe('add p to managed_projects in ordo.yaml')
    const mixed = { rows: [{ ...same, reason: 'one' }, { ...same, reason: 'two' }, { ...same, reason: 'one' }] }
    expect(projectNote(mixed)).toBe('one; two')
    expect(projectNote({ rows: [row()] })).toBeNull()
  })
})

describe('statusCounts', () => {
  it('counts the strip numbers from the fixture', () => {
    expect(statusCounts(rows())).toEqual({ total: 15, running: 12, failed: 1, unhealthy: 1, stopped: 0, jobs: 1 })
  })

  it('keeps a lent row out of stopped and counts starting as neither running nor stopped', () => {
    expect(statusCounts([
      row({ verdict: 'stopped', lent: true }), row({ verdict: 'starting' }), row({ verdict: 'up' }),
    ])).toEqual({ total: 3, running: 1, failed: 0, unhealthy: 0, stopped: 0, jobs: 0 })
  })

  it('keeps a lent row out of every problem bucket, whatever its verdict', () => {
    // A crash mid-lease arrives as failed with lent: true; severity reads it as information, so
    // the strip must not count it as failed while the section header calls it info.
    expect(statusCounts([
      row({ verdict: 'failed', lent: true }),
      row({ verdict: 'unhealthy', lent: true }),
      row({ verdict: 'stopped', lent: true }),
      row({ verdict: 'failed' }),
      row({ verdict: 'up' }),
    ])).toEqual({ total: 5, running: 1, failed: 1, unhealthy: 0, stopped: 0, jobs: 0 })
  })
})
