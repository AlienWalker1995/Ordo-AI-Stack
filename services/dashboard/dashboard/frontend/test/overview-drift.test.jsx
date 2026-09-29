// The Overview's Drift callout: what `ordo doctor` would flag (GET /api/drift, from ops-controller's
// read-only GET /doctor) is shown on the page an operator watches, and nothing is drawn when there
// is nothing to flag or the check could not run.
import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import OverviewPage from '../src/pages/OverviewPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

describe('the Drift callout', () => {
  it('lists each finding under a Drift heading', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const heading = await screen.findByRole('heading', { name: 'Drift' })
    const callout = heading.closest('section')
    const [finding] = fixture('/api/drift').findings
    // The report line as ops-controller wrote it, with its `command` spans set as code.
    expect(callout.textContent).toContain(finding.detail.replaceAll('`', ''))
    const code = [...callout.querySelectorAll('code')].map((c) => c.textContent)
    expect(code).toEqual(['ordo recreate ops-controller', 'ordo doctor'])
  })

  it.each([
    ['no findings', { available: true, findings: [] }],
    ['the check could not run', { available: false, findings: [] }],
    ['the endpoint failing', { status: 503, body: { detail: 'down' } }],
  ])('is absent with %s', async (_name, answer) => {
    mockApi({ 'GET /api/drift': answer })
    renderInShell(<OverviewPage />)
    await screen.findByRole('heading', { name: 'GPUs right now' })
    expect(screen.queryByRole('heading', { name: 'Drift' })).toBeNull()
  })
})

// The header's summary counts drift with what needs attention, so it never reads "All systems
// normal" above a Drift callout.
describe('the status line with drift', () => {
  const calm = () => ({ ...fixture('/api/overview'), attention: [], status: { level: 'ok', text: 'All systems normal' } })

  it('counts a drift finding when nothing else needs attention', async () => {
    mockApi({ 'GET /api/overview': calm })
    renderInShell(<OverviewPage />)
    await screen.findByRole('heading', { name: 'Drift' })
    expect(screen.getByText('1 thing needs you')).toBeTruthy()
    expect(screen.queryByText('All systems normal')).toBeNull()
    // The attention panel points at the callout instead of saying nothing needs you.
    expect(screen.getByText('Nothing else needs you. See Drift above.')).toBeTruthy()
    expect(screen.queryByText('Nothing needs you.')).toBeNull()
  })

  it('adds drift findings to the attention count', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    await screen.findByRole('heading', { name: 'Drift' })
    expect(screen.getByText('2 things need you')).toBeTruthy()
  })

  it.each([
    ['no findings', { available: true, findings: [] }],
    ['the check could not run', { available: false, findings: [] }],
  ])('is the backend verdict with %s', async (_name, drift) => {
    mockApi({ 'GET /api/overview': calm, 'GET /api/drift': drift })
    renderInShell(<OverviewPage />)
    expect(await screen.findByText('All systems normal')).toBeTruthy()
  })

  it('keeps "not answering" when the control plane is down', async () => {
    mockApi({
      'GET /api/overview': () => ({ ...calm(), status: { level: 'unknown', text: 'The control plane is not answering' } }),
    })
    renderInShell(<OverviewPage />)
    await screen.findByRole('heading', { name: 'Drift' })
    expect(screen.getByText('The control plane is not answering')).toBeTruthy()
  })
})
