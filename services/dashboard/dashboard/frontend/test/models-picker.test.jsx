// The model picker: what can be switched to first, the rest of the catalog under "Not downloaded".
import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ModelsPage from '../src/pages/ModelsPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

describe('the model picker', () => {
  it('groups downloaded models first and lists the rest disabled', async () => {
    mockApi()
    renderInShell(<ModelsPage />)
    const picker = await screen.findByLabelText('Switch the GPU model')
    const groups = within(picker).getAllByRole('group')
    expect(groups.map((g) => g.getAttribute('label'))).toEqual(['Downloaded', 'Not downloaded'])
    const catalog = fixture('/api/models').catalog
    const downloaded = within(groups[0]).getAllByRole('option')
    expect(downloaded.map((o) => o.value)).toEqual(catalog.filter((c) => c.installed).map((c) => c.id))
    const running = downloaded.find((o) => o.textContent.endsWith('(running)'))
    expect(running.disabled).toBe(true)
    expect(within(groups[1]).getAllByRole('option').every((o) => o.disabled)).toBe(true)
  })

  it('leaves out the second group when everything is downloaded', async () => {
    const models = fixture('/api/models')
    models.catalog = models.catalog.map((c) => ({ ...c, installed: true }))
    mockApi({ 'GET /api/models': models })
    renderInShell(<ModelsPage />)
    const picker = await screen.findByLabelText('Switch the GPU model')
    expect(within(picker).getAllByRole('group').map((g) => g.getAttribute('label'))).toEqual(['Downloaded'])
  })

  it('leaves out the first group when nothing is downloaded', async () => {
    const models = fixture('/api/models')
    models.catalog = models.catalog.map((c) => ({ ...c, installed: false, active: false }))
    mockApi({ 'GET /api/models': models })
    renderInShell(<ModelsPage />)
    const picker = await screen.findByLabelText('Switch the GPU model')
    const groups = within(picker).getAllByRole('group')
    expect(groups.map((g) => g.getAttribute('label'))).toEqual(['Not downloaded'])
    expect(within(groups[0]).getAllByRole('option').every((o) => o.disabled)).toBe(true)
  })
})
