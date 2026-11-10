// The Live work panel: what is running on the local engines now, named the way the engines list
// names them, the CPU fallback's prompt progress, and each caller's last few minutes.
import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import OverviewPage from '../src/pages/OverviewPage.jsx'
import { liveWork } from '../src/pages/overviewModel.js'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

async function panel() {
  return (await screen.findByRole('heading', { name: 'Live work' })).closest('section')
}

describe('the Live work panel', () => {
  it('lists each running request with its caller, engine, prompt size and time', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const live = within(await panel())
    expect(await live.findByText('2 requests running')).toBeTruthy()
    const hermes = live.getAllByText('hermes')[0].closest('li')
    expect(hermes.textContent).toContain('local-chat on GPU')
    expect(hermes.textContent).toContain('62,149 prompt tokens · 1 m')
    const webui = live.getByText('open-webui').closest('li')
    expect(within(webui).getByText('Fallback')).toBeTruthy()
  })

  it("shows the CPU fallback's prompt progress, cached tokens counted as read", async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const live = within(await panel())
    expect(await live.findByText(/: 12,000 of 40,000 tokens$/)).toBeTruthy()
  })

  it("sums each caller's last minutes and calls out failures", async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const live = within(await panel())
    expect(await live.findByRole('heading', { name: 'Last 5 minutes, by caller' })).toBeTruthy()
    const row = live.getByText('28 requests').closest('li')
    expect(row.textContent).toContain('1 failed')
    expect(row.textContent).toContain('2,244,094 in · 38,192 out')
    expect(row.textContent).toContain('median 15.9 s')
  })

  it('says plainly when nothing is running and nobody called', async () => {
    mockApi({ 'GET /api/work': { ...fixture('/api/work'), inflight: [], recent: { window_s: 300, callers: [] },
      engines: { 'inference-engine-gpu': { inflight: 0, prefill: null } } } })
    renderInShell(<OverviewPage />)
    const live = within(await panel())
    expect(await live.findByText('Nothing running now')).toBeTruthy()
    expect(live.getByText('No requests in the last 5 minutes.')).toBeTruthy()
  })

  it('says so when live work cannot be read', async () => {
    mockApi({ 'GET /api/work': { status: 503, body: { detail: 'down' } } })
    renderInShell(<OverviewPage />)
    const live = within(await panel())
    expect(await live.findByText(/^Live work is unavailable/)).toBeTruthy()
  })
})

describe('liveWork', () => {
  it("names an engine the registry does not list by the gateway's deployment", () => {
    const { running } = liveWork({ inflight: [{ call_id: 'x', caller: 'evals', deployment: 'vendor-engine', prompt_tokens: 5, running_s: 1 }] }, [])
    expect(running[0].where).toBe('vendor-engine')
  })

  it('skips prefill for an engine that is not reading a prompt', () => {
    const work = { engines: { 'inference-engine-cpu': { prefill: { available: true, processing: false, prompt_tokens: 0, processed: 0, cached: 0 } } } }
    expect(liveWork(work, []).prefill).toEqual([])
  })
})
