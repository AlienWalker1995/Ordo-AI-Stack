// The Overview body below the KPI band: the Model usage and Capacity panels, and the grouped
// activity feed with its kind chips, counted rows and GPU lease rows.
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import OverviewPage from '../src/pages/OverviewPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

describe('the body panels', () => {
  it('draws Model usage and Capacity side by side from the live-shaped fixtures', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const usage = (await screen.findByRole('heading', { name: 'Model usage' })).closest('section')
    const capacity = screen.getByRole('heading', { name: 'Capacity' }).closest('section')
    // One grid holds both panels; the spec's desktop layout puts them in two columns.
    expect(usage.parentElement).toBe(capacity.parentElement)
    expect(usage.parentElement.className).toContain('lg:grid-cols-2')
    // Model usage: the token trend label, the per-caller gap line, and the active models line.
    expect(within(usage).getByText('Tokens per hour, last 24 h')).toBeTruthy()
    expect(within(usage).getByText('Per-caller usage arrives with live work tracking.')).toBeTruthy()
    expect(within(usage).getByText(/Active:/).parentElement.textContent).toContain('qwen3.8-27b-turbo')
    // Capacity: every GPU card, the host meters with their absolute figures, the knowledge line.
    expect(within(capacity).getByText('RTX 5090')).toBeTruthy()
    expect(within(capacity).getByText('GTX 1070')).toBeTruthy()
    expect(within(capacity).getByText('46.8 / 109.7 GB')).toBeTruthy()
    expect(within(capacity).getByText('1,640.3 / 1,999.8 GB')).toBeTruthy()
    expect(within(capacity).getByText('2,483').parentElement.textContent).toBe('Knowledge 2,483 documents indexed')
  })

  it('reads the Model usage sources as unavailable when they are', async () => {
    mockApi({
      'GET /api/perf/series': { available: false },
      'GET /api/models': { status: 503, body: { detail: 'The control plane is not answering' } },
    })
    renderInShell(<OverviewPage />)
    // The note appears once per sparkline slot that lost its history.
    expect((await screen.findAllByText('History needs the monitoring profile')).length).toBeGreaterThan(0)
    expect(await screen.findByText('Active models unavailable.')).toBeTruthy()
  })

  it('reads the Capacity panel as unavailable without GPUs or host metrics', async () => {
    mockApi({ 'GET /api/overview': { ...fixture('/api/overview'), gpus: [], host: {} } })
    renderInShell(<OverviewPage />)
    expect(await screen.findByText('No GPUs reported.')).toBeTruthy()
    expect(screen.getByText('Host metrics unavailable.')).toBeTruthy()
  })
})

describe('the grouped activity feed', () => {
  it('shows a GPU lease with its holder, duration and eviction note', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const row = await screen.findByText('GPU lent to ComfyUI')
    expect(row.closest('li').textContent).toContain('· 3 m')
    expect(row.closest('li').textContent).toContain('chat model evicted')
    // The tooltip carries the lease's own start to end, so the span stays readable afterwards.
    expect(row.getAttribute('title')).toMatch(/ to /)
    expect(row.getAttribute('title')).toContain('3 m')
  })

  it('collapses consecutive identical events into one counted row', async () => {
    const repeats = Array.from({ length: 8 }, (_, i) => ({
      ts: 1790680000 - i * 60, kind: 'action', title: 'Restarted comfyui', severity: 'info',
      holder: null, started: null, ended: null, duration_s: null, evicted_chat: false,
    }))
    mockApi({ 'GET /api/activity': { items: [...repeats, ...fixture('/api/activity').items] } })
    renderInShell(<OverviewPage />)
    const row = await screen.findByText('Restarted comfyui')
    expect(row.closest('li').textContent).toContain('×8')
  })

  it('reads a run of leases as GPU time in all', async () => {
    const leases = [0, 1, 2].map((i) => ({
      ts: 1790680000 - i * 600, kind: 'gpu', title: 'GPU lent to ComfyUI', severity: 'ok',
      holder: 'ComfyUI', started: 1790679820 - i * 600, ended: 1790680000 - i * 600, duration_s: 180,
      evicted_chat: i === 2,
    }))
    mockApi({ 'GET /api/activity': { items: leases } })
    renderInShell(<OverviewPage />)
    const row = (await screen.findByText('GPU lent to ComfyUI')).closest('li')
    expect(row.textContent).toContain('×3')
    expect(row.textContent).toContain('· 9 m in all')
    expect(row.textContent).toContain('chat model evicted')
  })

  it('offers Show more while the first answer came back full, however few groups it makes', async () => {
    const repeats = Array.from({ length: 25 }, (_, i) => ({
      ts: 1790680000 - i * 60, kind: 'action', title: 'Restarted comfyui', severity: 'info',
      holder: null, started: null, ended: null, duration_s: null, evicted_chat: false,
    }))
    const api = mockApi({ 'GET /api/activity': { items: repeats } })
    renderInShell(<OverviewPage />)
    const feed = (await screen.findByRole('heading', { name: 'Activity' })).closest('section')
    expect(within(feed).getAllByRole('listitem')).toHaveLength(1)
    fireEvent.click(within(feed).getByRole('button', { name: 'Show more' }))
    await waitFor(() => expect(api.sent('GET', '/api/activity').at(-1).query.limit).toBe('100'))
  })

  it('filters the feed by kind behind the chips', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    await screen.findByText('GPU lent to ComfyUI')
    const chips = screen.getByRole('group', { name: 'Filter activity by kind' })
    fireEvent.click(within(chips).getByRole('button', { name: 'Renders' }))
    expect(screen.getByText('Render finished · image')).toBeTruthy()
    expect(screen.queryByText('GPU lent to ComfyUI')).toBeNull()
    fireEvent.click(within(chips).getByRole('button', { name: 'GPU leases' }))
    expect(screen.getByText('GPU lent to ComfyUI')).toBeTruthy()
    expect(screen.queryByText('Render finished · image')).toBeNull()
    fireEvent.click(within(chips).getByRole('button', { name: 'Service changes' }))
    expect(screen.getByText('Restarted inference-engine-gpu')).toBeTruthy()
    expect(screen.queryByText('Switched model to qwen3.8-27b-turbo')).toBeNull()
    fireEvent.click(within(chips).getByRole('button', { name: 'Model changes' }))
    expect(screen.getByText('Switched model to qwen3.8-27b-turbo')).toBeTruthy()
    expect(screen.queryByText('Restarted inference-engine-gpu')).toBeNull()
  })

  it('widens the request to the endpoint cap on Show more', async () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      ts: 1790680000 - i * 3600, kind: 'action', title: `Restarted svc-${i}`, severity: 'info',
      holder: null, started: null, ended: null, duration_s: null, evicted_chat: false,
    }))
    const api = mockApi({ 'GET /api/activity': { items: many } })
    renderInShell(<OverviewPage />)
    const feed = (await screen.findByRole('heading', { name: 'Activity' })).closest('section')
    expect(within(feed).getAllByRole('listitem')).toHaveLength(10)
    fireEvent.click(within(feed).getByRole('button', { name: 'Show more' }))
    await within(feed).findByText('Restarted svc-11')
    expect(api.sent('GET', '/api/activity').at(-1).query.limit).toBe('100')
    expect(within(feed).getAllByRole('listitem')).toHaveLength(12)
  })
})
