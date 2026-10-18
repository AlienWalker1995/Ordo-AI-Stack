// The KPI band's pure model: container counts across projects, the token estimate from the rate
// series, medians passed through untouched, and the omit-don't-zero rule for missing sources.
import { describe, expect, it } from 'vitest'
import { countContainers, diskFacts, formatTokens, kpiTiles, speedByRole, tokensFromSeries, vramFacts } from '../src/pages/overviewModel.js'

describe('countContainers', () => {
  it('sums every project, ordo and non-ordo alike', () => {
    const byProject = {
      ordo: { total: 46, up: 46, failed: 0, unhealthy: 0 },
      'min-max': { total: 6, up: 5, failed: 1, unhealthy: 0 },
      adguard: { total: 1, up: 1, failed: 0, unhealthy: 0 },
    }
    expect(countContainers(byProject)).toEqual({
      up: 52, total: 53, failed: 1, unhealthy: 0,
      failedProjects: ['min-max'], unhealthyProjects: [],
    })
  })

  it('names every project with a failed or an unhealthy container', () => {
    const byProject = {
      ordo: { total: 3, up: 2, failed: 0, unhealthy: 1 },
      'min-max': { total: 2, up: 0, failed: 2, unhealthy: 0 },
    }
    const counts = countContainers(byProject)
    expect([counts.failed, counts.unhealthy]).toEqual([2, 1])
    expect(counts.failedProjects).toEqual(['min-max'])
    expect(counts.unhealthyProjects).toEqual(['ordo'])
  })

  it('is missing when the field is absent, null or empty', () => {
    expect(countContainers(undefined)).toBeNull()
    expect(countContainers(null)).toBeNull()
    expect(countContainers({})).toBeNull()
  })
})

describe('tokensFromSeries', () => {
  it('integrates each rate over the gap to the next sample, gpu and cpu together', () => {
    // Two hourly samples: 40 tok/s held for the hour, then 50; cpu 1 then 2.
    const series = { gpu: [[0, 40], [3600, 50]], cpu: [[0, 1], [3600, 2]] }
    expect(tokensFromSeries(series)).toBe(40 * 3600 + 1 * 3600)
  })

  it('is missing when neither role has two samples', () => {
    expect(tokensFromSeries({ gpu: [[0, 40]], cpu: [] })).toBeNull()
    expect(tokensFromSeries(undefined)).toBeNull()
    expect(tokensFromSeries({ available: false })).toBeNull()
  })

  it('ignores gaps that run backwards in time', () => {
    expect(tokensFromSeries({ gpu: [[0, 10], [3600, 20], [1800, 30]] })).toBe(10 * 3600)
  })
})

describe('formatTokens', () => {
  it('marks the estimate and scales to k and M', () => {
    expect(formatTokens(1_200_000)).toBe('~1.2 M')
    expect(formatTokens(45_000)).toBe('~45 k')
    expect(formatTokens(900)).toBe('~900')
    expect(formatTokens(null)).toBeNull()
  })
})

describe('speedByRole', () => {
  it('passes provisional medians through untouched', () => {
    const chat = {
      gpu: { p50: 198.7, n: 500, provisional: false, model: 'a.ninfer' },
      cpu: { p50: 10.1, n: 35, provisional: true, model: 'b.gguf' },
    }
    const speeds = speedByRole(chat)
    expect(speeds.gpu).toBe(chat.gpu)
    expect(speeds.cpu.provisional).toBe(true)
    expect(speeds.cpu.n).toBe(35)
  })

  it('drops a role without a median', () => {
    expect(speedByRole({ gpu: { n: 0 }, cpu: { p50: 9.2, n: 3 } })).toEqual({ cpu: { p50: 9.2, n: 3 } })
    expect(speedByRole(undefined)).toEqual({})
  })
})

describe('vramFacts and diskFacts', () => {
  it('reads the first GPU with usable totals and rounds the percentage', () => {
    const facts = vramFacts([{ name: 'RTX 5090', vram_used_gb: 30.8, vram_total_gb: 34.2, util_pct: 99, tenants: ['GPU chat'] }])
    expect(facts.pct).toBe(90)
    expect(facts.name).toBe('RTX 5090')
  })
  it('is missing without a GPU or without host disk numbers', () => {
    expect(vramFacts([])).toBeNull()
    expect(vramFacts(undefined)).toBeNull()
    expect(diskFacts({})).toBeNull()
    expect(diskFacts(undefined)).toBeNull()
  })
  it('warns at the existing thresholds', () => {
    expect(diskFacts({ disk_pct: 80 }).warn).toBe(true)
    expect(diskFacts({ disk_pct: 79.9 }).warn).toBe(false)
  })
})

const FULL_OVERVIEW = {
  services: { up: 4, total: 5, by_project: {
    ordo: { total: 3, up: 3, failed: 0, unhealthy: 0 },
    'min-max': { total: 2, up: 1, failed: 1, unhealthy: 0 },
  } },
  chat: { engine: 'gpu', reason: '', gpu: { p50: 153.2, n: 500, provisional: false, first_ts: 1792303930, last_ts: 1792326263, model: 'm.ninfer' } },
  gpus: [{ uuid: 'g1', name: 'RTX 5090', vram_used_gb: 30.8, vram_total_gb: 34.2, util_pct: 99, temp_c: 60, tenants: ['GPU chat', 'ComfyUI'], borrowed_by: [] }],
  host: { disk_pct: 76.0, disk_used_gb: 1520.0, disk_total_gb: 1999.8 },
}
const SERIES = { gpu: [[0, 40], [3600, 50]], cpu: [[0, 1], [3600, 2]] }

describe('kpiTiles', () => {
  it('draws the six tiles in the spec order from a full payload', () => {
    const tiles = kpiTiles(FULL_OVERVIEW, SERIES)
    expect(tiles.map((t) => t.key)).toEqual(['containers', 'engine', 'speed', 'tokens', 'vram', 'disk'])
    const [containers, engine, speed, tokens, vram, disk] = tiles
    expect(containers.value).toBe('4 / 5')
    expect(containers.sub).toBe('1 failed in min-max')
    expect(containers.tone).toBe('danger')
    expect(engine.value).toBe('GPU')
    expect(speed.median).toMatchObject({ value: 153.2, n: 500, provisional: false })
    expect(speed.sparkline).toBe(SERIES.gpu)
    expect(tokens.value).toBe('~148 k')
    expect(vram.value).toBe('90%')
    expect(vram.sub).toContain('99% compute')
    expect(vram.sub).toContain('resident: GPU chat, ComfyUI')
    expect(disk.value).toBe('76%')
    expect(disk.sub).toBe('1.52 / 2.00 TB')
  })

  it('omits a tile whose source is missing', () => {
    const bare = { services: {}, chat: { engine: 'gpu', reason: '' }, gpus: [], host: {} }
    expect(kpiTiles(bare, undefined).map((t) => t.key)).toEqual(['engine'])
    expect(kpiTiles(undefined, undefined)).toEqual([])
  })

  it('tones the values at their thresholds', () => {
    const cpu = kpiTiles({ ...FULL_OVERVIEW, chat: { engine: 'cpu', reason: 'GPU busy', cpu: { p50: 9.2, n: 3, provisional: true } } }, SERIES)
    const engine = cpu.find((t) => t.key === 'engine')
    expect(engine.value).toBe('CPU fallback')
    expect(engine.tone).toBe('warning')
    expect(engine.sub).toBe('GPU busy')
    // A provisional median passes through to the Median primitive, which carries the mark.
    const speed = cpu.find((t) => t.key === 'speed')
    expect(speed).toBeUndefined()
    const none = kpiTiles({ ...FULL_OVERVIEW, chat: { engine: 'none', reason: 'no server' } }, SERIES)
    expect(none.find((t) => t.key === 'engine').tone).toBe('danger')
    const hot = kpiTiles({ ...FULL_OVERVIEW, gpus: [{ ...FULL_OVERVIEW.gpus[0], vram_used_gb: 33.5 }] }, SERIES)
    expect(hot.find((t) => t.key === 'vram').tone).toBe('warning')
    const full = kpiTiles({ ...FULL_OVERVIEW, host: { ...FULL_OVERVIEW.host, disk_pct: 91 } }, SERIES)
    expect(full.find((t) => t.key === 'disk').tone).toBe('warning')
  })
})
