// The launcher: every web UI to open, grouped as the backend orders them, each a link that opens
// in a new tab with its health and description; the command palette offers the same apps.
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import CommandPalette from '../src/components/CommandPalette.jsx'
import OverviewPage from '../src/pages/OverviewPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

async function launcher() {
  return (await screen.findByRole('heading', { name: 'Apps' })).closest('section')
}

describe('the Apps launcher', () => {
  it('groups the apps in the order the backend sends them', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const apps = within(await launcher())
    expect(apps.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['Ordo', 'Media'])
    const media = apps.getByRole('heading', { name: 'Media' }).closest('section')
    expect(within(media).getAllByRole('link').map((a) => a.textContent)).toEqual(['Jellyfin, upMovies and shows', 'Radarr, status unknown'])
  })

  it('opens each app in a new tab, with its health for screen readers', async () => {
    mockApi()
    renderInShell(<OverviewPage />)
    const apps = within(await launcher())
    const langfuse = apps.getByRole('link', { name: /Langfuse/ })
    expect(langfuse.getAttribute('href')).toBe('https://langfuse.example.test/')
    expect(langfuse.getAttribute('target')).toBe('_blank')
    expect(langfuse.getAttribute('rel')).toBe('noreferrer')
    expect(langfuse.textContent).toContain('Langfuse, down')
  })

  it('is left out when there is nothing to open', async () => {
    mockApi({ 'GET /api/overview': { ...fixture('/api/overview'), apps: [] } })
    renderInShell(<OverviewPage />)
    await screen.findByRole('heading', { name: 'Capacity' })
    expect(screen.queryByRole('heading', { name: 'Apps' })).toBeNull()
  })
})

describe('the command palette', () => {
  it('offers every app, labelled with its group', async () => {
    mockApi()
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    render(<CommandPalette open onClose={() => {}} pages={[]} onNavigate={() => {}} />)
    const jellyfin = await screen.findByText('Open Jellyfin')
    expect(jellyfin.closest('[role="option"], li, button')?.textContent).toContain('Media')
    fireEvent.click(jellyfin)
    expect(open).toHaveBeenCalledWith('http://media.example.test:8096/', '_blank', 'noopener')
    open.mockRestore()
  })
})
