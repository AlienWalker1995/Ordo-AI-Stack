// The Services page groups every container on the host under its service: a service's containers
// nest beneath its row behind a disclosure toggle, and a filter over name and project opens a
// service whose container matches. The shared fixture here carries no group rows, so these tests
// cover the flat fallback and the controls each row's control kind allows.
import { act, fireEvent, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { INPUT } from '../src/components/ui.jsx'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

const ROWS = fixture('/api/services/table').rows

// Both layouts are in the DOM (CSS picks one per width, which jsdom does not apply): the table's
// rows are the wide layout, so each fixture row must appear there exactly once.
async function tableRows() {
  const [table] = await screen.findAllByRole('table')
  return within(table).getAllByRole('row').slice(1) // the header row first
}

function rowNamed(rows, name) {
  return rows.find((row) => within(row).queryByText(name, { exact: true }))
}

describe('Services: one flat list', () => {
  it('renders every row once and no group headers', async () => {
    mockApi()
    const { container } = renderInShell(<ServicesPage />)
    const rows = await tableRows()

    expect(rows).toHaveLength(ROWS.length)
    expect(container.querySelector('details')).toBeNull()
    expect(container.querySelector('summary')).toBeNull()
    for (const header of ['Apps', 'Inference', 'Platform', 'Tools (MCP)', 'Network', 'Jobs']) {
      expect(screen.queryByText(header, { exact: true })).toBeNull()
    }
    expect(within(rowNamed(rows, 'media-server')).getByText('media-stack')).toBeTruthy()
  })

  it('filters by project', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    await tableRows()

    fireEvent.change(screen.getByLabelText('Filter services'), { target: { value: 'MEDIA-stack' } })

    const rows = await tableRows()
    expect(rows).toHaveLength(ROWS.filter((r) => r.project === 'media-stack').length)
    expect(rowNamed(rows, 'media-server')).toBeTruthy()
    expect(rowNamed(rows, 'Open WebUI')).toBeUndefined()
  })

  it('says when nothing matches and clears the filter', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    await tableRows()

    fireEvent.change(screen.getByLabelText('Filter services'), { target: { value: 'nothing-like-this' } })

    expect(screen.getByText('No services match "nothing-like-this"')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(await tableRows()).toHaveLength(ROWS.length)
    expect(screen.getByLabelText('Filter services').value).toBe('')
    // The button that had focus is gone, so focus goes back to the filter.
    expect(document.activeElement).toBe(screen.getByLabelText('Filter services'))
  })

  it('shows Hermes once, as its card with the agent container behind it', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    const rows = await tableRows()

    expect(rows.filter((row) => within(row).queryByText('Hermes Agent', { exact: true }))).toHaveLength(1)
    // The compose name shows only under the card's name, not as a row of its own.
    expect(rows.filter((row) => within(row).queryByText('agent', { exact: true }))).toEqual([rowNamed(rows, 'Hermes Agent')])
    const hermes = within(rowNamed(rows, 'Hermes Agent'))
    expect(hermes.getByRole('link', { name: /Hermes Agent/ }).getAttribute('href')).toBe('https://hermes.example.test/')
    expect(hermes.getByText('Running')).toBeTruthy()
    expect(hermes.getByText('5 hours')).toBeTruthy()
    expect(hermes.getByText('Host only')).toBeTruthy()
    expect(hermes.getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(['Logs for Hermes Agent'])
  })

  it('names every state in words beside its dot', async () => {
    const table = fixture('/api/services/table')
    Object.assign(table.rows.find((r) => r.id === 'ordo:evals'), { verdict: 'unknown', actions: [] })
    Object.assign(table.rows.find((r) => r.id === 'ordo:inference-engine-gpu'), { verdict: 'stopped', lent: true, actions: [] })
    mockApi({ 'GET /api/services/table': table })
    renderInShell(<ServicesPage />)
    const rows = await tableRows()

    expect(within(rowNamed(rows, 'evals')).getByText('Unknown')).toBeTruthy()
    expect(within(rowNamed(rows, 'llama.cpp (GPU)')).getByText('Lent to a render')).toBeTruthy()
    for (const row of rows) {
      expect(row.cells[0].textContent.trim()).not.toBe('')
    }
  })

  it('gives the table column headers', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    await tableRows()

    const headers = screen.getAllByRole('columnheader')
    expect(headers).toHaveLength(7)
    for (const header of headers) expect(header.getAttribute('scope')).toBe('col')
  })

  it('leaves the global focus ring on the filter input', () => {
    // `outline-none` would override index.css's :focus-visible ring and leave only a border tint.
    expect(INPUT.split(' ')).not.toContain('outline-none')
  })

  it('gives each control kind its own controls', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    const rows = await tableRows()

    const ordo = within(rowNamed(rows, 'Open WebUI'))
    expect(ordo.getByRole('button', { name: 'Restart Open WebUI' })).toBeTruthy()
    expect(ordo.getByRole('button', { name: 'Stop Open WebUI' })).toBeTruthy()
    expect(ordo.getByRole('button', { name: 'Logs for Open WebUI' })).toBeTruthy()

    const managed = within(rowNamed(rows, 'media-server'))
    expect(managed.getAllByRole('button').map((b) => b.getAttribute('aria-label')))
      .toEqual(['Logs for media-server (media-stack)', 'Restart media-server (media-stack)'])

    const host = within(rowNamed(rows, 'ops-controller'))
    expect(host.getByText('Host only')).toBeTruthy()
    expect(host.getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(['Logs for ops-controller'])

    const link = within(rowNamed(rows, 'Codebase Memory'))
    expect(link.getByText('Link only')).toBeTruthy()
    expect(link.queryAllByRole('button')).toEqual([])

    const none = within(rowNamed(rows, 'web-site-web-1'))
    expect(none.getByText('Not managed: add web-site to managed_projects in ordo.yaml')).toBeTruthy()
    expect(none.queryAllByRole('button')).toEqual([])
  })

  it("spells out a project's not-managed reason once, not on every one of its rows", async () => {
    const table = fixture('/api/services/table')
    const base = table.rows.find((r) => r.name === 'web-site-web-1')
    const at = table.rows.indexOf(base)
    table.rows.splice(at + 1, 0, { ...base, id: 'host:web-site-db-1', name: 'web-site-db-1', container: 'web-site-db-1' })
    mockApi({ 'GET /api/services/table': table })
    renderInShell(<ServicesPage />)
    const rows = await tableRows()
    expect(within(rowNamed(rows, 'web-site-web-1')).getByText('Not managed: add web-site to managed_projects in ordo.yaml')).toBeTruthy()
    const second = within(rowNamed(rows, 'web-site-db-1')).getByText(/^Not managed/)
    expect(second.getAttribute('title')).toBe('add web-site to managed_projects in ordo.yaml')
    // Screen readers still hear the whole reason.
    expect(second.textContent).toBe('Not managed: add web-site to managed_projects in ordo.yaml')
  })

  it('restarts a managed container through its project route once confirmed', async () => {
    const path = '/api/ops/projects/media-stack/containers/media-server/restart'
    const api = mockApi({ [`POST ${path}`]: { ok: true, project: 'media-stack', container: 'media-server', action: 'restarted' } })
    renderInShell(<ServicesPage />)

    const [restart] = await screen.findAllByRole('button', { name: 'Restart media-server (media-stack)' })
    fireEvent.click(restart)
    const dialog = screen.getByRole('alertdialog', { name: 'Restart media-server (media-stack)?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))

    expect((await screen.findAllByText('Restarting…')).length).toBeGreaterThan(0)
    expect(api.sent('POST', path)).toHaveLength(1)
  })

  it('reads a managed container\'s logs through its project route', async () => {
    const path = '/api/ops/projects/media-stack/containers/media-server/logs'
    const api = mockApi({ [`GET ${path}`]: { project: 'media-stack', container: 'media-server', tail: 300, logs: 'listening' } })
    renderInShell(<ServicesPage />)

    const [logs] = await screen.findAllByRole('button', { name: 'Logs for media-server (media-stack)' })
    fireEvent.click(logs)

    await screen.findByText('listening')
    expect(api.sent('GET', path).length).toBeGreaterThan(0)
  })
})

describe('Services: CPU and memory of other compose projects', () => {
  it("reads another project's container from the host-wide stats, by container name", async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    const rows = await tableRows()
    const media = within(rowNamed(rows, 'media-server'))
    expect(await media.findByText('4.2%')).toBeTruthy()
    expect(media.getByText('1.1 GB')).toBeTruthy()
  })
})

describe('Services: the project chips', () => {
  it('shows one project at a time, and All again', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    const group = await screen.findByRole('group', { name: 'Project' })
    const all = within(group).getByRole('button', { name: /^All/ })
    expect(all.getAttribute('aria-pressed')).toBe('true')
    const before = (await tableRows()).length
    fireEvent.click(within(group).getByRole('button', { name: /^web-site/ }))
    const rows = await tableRows()
    expect(rows.length).toBeLessThan(before)
    expect(rows.every((r) => within(r).queryByText('web-site'))).toBe(true)
    fireEvent.click(all)
    expect((await tableRows()).length).toBe(before)
  })

  it('marks a project with a failed container', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    const group = await screen.findByRole('group', { name: 'Project' })
    expect(within(group).getByRole('button', { name: /^media-stack/ }).textContent).toContain('1 failing')
  })
})

describe('Services: the project chips, edge cases', () => {
  it('counts a container outside any compose project under All', async () => {
    const table = fixture('/api/services/table')
    const base = table.rows.find((r) => r.name === 'web-site-web-1')
    table.rows.push({ ...base, id: 'host:adhoc', name: 'adhoc', container: 'adhoc', project: null, compose: null })
    mockApi({ 'GET /api/services/table': table })
    renderInShell(<ServicesPage />)
    const group = await screen.findByRole('group', { name: 'Project' })
    const top = table.rows.filter((r) => !r.parent).length
    expect(within(group).getByRole('button', { name: /^All/ }).textContent).toBe(`All ${top}`)
  })

  it('keeps the chips while a project is picked, even if it is the last one left', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    try {
      const table = fixture('/api/services/table')
      let rows = table.rows
      mockApi({ 'GET /api/services/table': () => ({ ...table, rows }) })
      renderInShell(<ServicesPage />)
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const group = screen.getByRole('group', { name: 'Project' })
      fireEvent.click(within(group).getByRole('button', { name: /^web-site/ }))
      // Every other project leaves the host; the next poll (10 s) brings the shorter list.
      rows = table.rows.filter((r) => r.project === 'web-site')
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
      const still = screen.getByRole('group', { name: 'Project' })
      expect(within(still).getByRole('button', { name: /^web-site/ }).getAttribute('aria-pressed')).toBe('true')
      fireEvent.click(within(still).getByRole('button', { name: /^All/ }))
      expect(screen.queryByRole('group', { name: 'Project' })).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the text and the project together from the no-match state', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    const group = await screen.findByRole('group', { name: 'Project' })
    const before = (await tableRows()).length
    fireEvent.click(within(group).getByRole('button', { name: /^web-site/ }))
    fireEvent.change(screen.getByLabelText('Filter services'), { target: { value: 'nothing-matches-this' } })
    expect(await screen.findByText('No services match "nothing-matches-this" in web-site')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect((await tableRows()).length).toBe(before)
  })
})
