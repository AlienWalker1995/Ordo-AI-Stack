// The Models page lists the registry's engines like the Overview does, compactly: what each runs
// and whether it serves, without the speeds and history the Overview carries.
import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ModelsPage from '../src/pages/ModelsPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

describe('Models: running now', () => {
  it('lists every engine from /api/models, with no fixed GPU or CPU slot', async () => {
    mockApi()
    renderInShell(<ModelsPage />)
    const panel = (await screen.findByRole('heading', { name: 'Running now' })).closest('section')
    for (const e of fixture('/api/models').engines) {
      expect(within(panel).getByText(e.model, { exact: true })).toBeTruthy()
    }
    expect(within(panel).queryByText('GPU chat')).toBeNull()
    expect(within(panel).queryByText(/tok\/s generating/)).toBeNull()
    expect(within(panel).queryByText('No samples in the last hour')).toBeNull()
  })

  it('names the catalog model the GPU runs, beside the switch', async () => {
    mockApi()
    renderInShell(<ModelsPage />)
    const active = fixture('/api/models').active_model
    expect((await screen.findByText(active, { selector: 'code' })).closest('span').textContent).toBe(`Running ${active}`)
  })

  it('says so when the registry lists no engine', async () => {
    mockApi({ 'GET /api/models': { ...fixture('/api/models'), engines: [] } })
    renderInShell(<ModelsPage />)
    expect(await screen.findByText('No model engines reported.')).toBeTruthy()
  })
})
