// The Overview KPI band: every tile carries its value as text (never colour alone), attention
// sits above the band, and a tile whose source is missing is absent rather than drawn as zero.
import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import OverviewPage from '../src/pages/OverviewPage.jsx'
import { formatTokens, kpiTiles, tokensFromSeries } from '../src/pages/overviewModel.js'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

// A tile is found by the data-source tooltip its StatTile carries.
const tile = (source) => document.querySelector(`[title="${source}"]`)

describe('the KPI band', () => {
  it('renders every tile with its value as text', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const overview = fixture('/api/overview')
    const series = fixture('/api/perf/series')
    const tiles = kpiTiles(overview, series)
    expect(tiles.map((t) => t.key)).toEqual(['containers', 'chat', 'speed', 'tokens', 'vram', 'disk'])
    // Wait for the last tile, which needs the perf series as well as the overview.
    await screen.findByText('Tokens generated, 24 h')
    for (const t of tiles) {
      const el = tile(t.title)
      expect(el, `${t.key} tile`).not.toBeNull()
      const shown = t.median ? `${Math.round(t.median.value)} tok/s` : t.value
      expect(el.textContent, `${t.key} value as text`).toContain(shown)
    }
    // The context lines carry the evidence, not just a colour.
    expect(tile('services.by_project from /api/overview').textContent).toContain('1 failed in media-stack')
    expect(tile('chat from /api/overview').textContent).toContain('local-chat')
    expect(tile('inference-engine-gpu: tokens over time spent generating (ordo_engine_*)').textContent)
      .toContain('while generating, last hour')
    expect(tile('the chat model\'s GPU from /api/overview').textContent).toContain('resident: comfyui, local-chat')
    expect(tile('host.disk from /api/overview').textContent).toContain('1.64 / 2.00 TB')
  })

  it('shows the token estimate from the rate series', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const expected = formatTokens(tokensFromSeries(fixture('/api/perf/series')))
    await screen.findByText(expected)
    expect(screen.getByText('estimated from 5 min rates')).toBeTruthy()
  })

  it('promotes attention above the KPI band, linked to Services', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const heading = await screen.findByRole('heading', { name: 'Needs attention' })
    const strip = heading.closest('section')
    const band = tile('services.by_project from /api/overview').parentElement
    expect(strip.compareDocumentPosition(band) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const item = within(strip).getByText('media-server exited with an error')
    expect(item.parentElement.textContent).toContain('media-stack: Exited (1) 2 days ago')
    const link = within(strip).getByRole('link', { name: 'View in Services' })
    expect(link.getAttribute('href')).toBe('#services')
    // The host-level item (no container behind it) gets no link.
    expect(within(strip).getAllByRole('link')).toHaveLength(1)
  })

  it('leaves a tile undrawn when its source is missing', async () => {
    const overview = {
      ...fixture('/api/overview'),
      services: { up: 3, total: 5 },
      chat: { state: 'primary', model: { ...fixture('/api/overview').engines[0], speeds: null }, reason: '' },
      gpus: [],
      host: {},
    }
    mockApi({ 'GET /api/overview': overview, 'GET /api/perf/series': { available: false, series: {} } })
    renderInShell(<OverviewPage />)
    await screen.findByText('Chat answered by')
    expect(tile('services.by_project from /api/overview')).toBeNull()
    expect(screen.queryByText('Chat speed')).toBeNull()
    expect(tile('integrated /api/perf/series samples, every engine')).toBeNull()
    expect(tile('the chat model\'s GPU from /api/overview')).toBeNull()
    expect(tile('host.disk from /api/overview')).toBeNull()
  })

  it('shows no attention block when nothing needs attention', async () => {
    mockApi({
      'GET /api/overview': () => ({
        ...fixture('/api/overview'),
        attention: [],
        status: { level: 'ok', text: 'All systems normal' },
      }),
      // No drift finding either, or withDrift would rebuild the calm verdict into a count.
      'GET /api/drift': { available: true, findings: [] },
    })
    renderInShell(<OverviewPage />)
    await screen.findByText('All systems normal')
    expect(screen.queryByRole('heading', { name: 'Needs attention' })).toBeNull()
  })
})
