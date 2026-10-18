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

  it('labels containers without a compose project so the tile cannot end in a dangling "in "', () => {
    // service_counts files containers with no compose project under the key "".
    const byProject = { '': { total: 2, up: 1, failed: 1, unhealthy: 1 }, ordo: { total: 1, up: 1, failed: 0, unhealthy: 0 } }
    const counts = countContainers(byProject)
    expect(counts.failedProjects).toEqual(['no project'])
    expect(counts.unhealthyProjects).toEqual(['no project'])
    const tile = kpiTiles({ services: { by_project: byProject } }, undefined).find((t) => t.key === 'containers')
    expect(tile.sub).toBe('1 failed in no project · 1 unhealthy in no project')
  })
})

describe('tokensFromSeries', () => {
  it('integrates each rate over the gap to the next sample, gpu and cpu together', () => {
    // Samples at the request's own 300 s step: every gap counts in full, gpu and cpu summed.
    const series = { gpu: [[0, 40], [300, 50]], cpu: [[0, 1], [300, 2]] }
    expect(tokensFromSeries(series)).toBe(40 * 300 + 1 * 300)
  })

  it('is missing when neither role has two samples', () => {
    expect(tokensFromSeries({ gpu: [[0, 40]], cpu: [] })).toBeNull()
    expect(tokensFromSeries(undefined)).toBeNull()
    expect(tokensFromSeries({ available: false })).toBeNull()
  })

  it('ignores gaps that run backwards in time', () => {
    expect(tokensFromSeries({ gpu: [[0, 10], [300, 20], [150, 30]] })).toBe(10 * 300)
  })

  it('caps each interval at the request step, so a multi-step gap cannot extrapolate', () => {
    // 5-minute samples: 200 tok/s until t=3600, then the model is evicted for 3 h and the
    // series resumes at t=14400. The last known rate must not span the 10800 s gap — only one
    // step of it counts, or a quiet day would show ~2.9 M tokens instead of ~780 k.
    const gpu = []
    for (let t = 0; t <= 3600; t += 300) gpu.push([t, 200])
    gpu.push([14400, 200])
    expect(tokensFromSeries({ gpu })).toBe(13 * 200 * 300)
  })

  it('caps an all-sparse series at the request step, not the data\'s own gaps', () => {
    // Two isolated bursts 3 h apart and nothing else: a step derived from the data would be
    // 3 h and extrapolate 40 tok/s across the whole silent span (~432 k); the request's
    // 300 s step counts one interval per burst instead.
    expect(tokensFromSeries({ gpu: [[0, 40], [10800, 40]] })).toBe(40 * 300)
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
    expect(tokens.value).toBe('~12 k')
    expect(vram.value).toBe('90%')
    expect(vram.sub).toContain('99% compute')
    expect(vram.sub).toContain('resident: GPU chat, ComfyUI')
    expect(disk.value).toBe('76%')
    expect(disk.sub).toBe('1.52 / 2.00 TB')
  })

  it('leads the VRAM context line with the GB figure so it survives truncation', () => {
    const vram = kpiTiles(FULL_OVERVIEW, SERIES).find((t) => t.key === 'vram')
    expect(vram.sub).toBe('30.8 / 34.2 GB · 99% compute · resident: GPU chat, ComfyUI')
  })

  it('omits the holders part when the GPU has neither tenants nor borrowers', () => {
    const vram = kpiTiles({ ...FULL_OVERVIEW, gpus: [{ ...FULL_OVERVIEW.gpus[0], tenants: [], borrowed_by: [] }] }, SERIES)
      .find((t) => t.key === 'vram')
    expect(vram.sub).toBe('30.8 / 34.2 GB · 99% compute')
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
