// A service made of several containers is one row on the Services page: its containers open beneath
// it, and the row shows the worst of their states, so a failed part is never out of sight.
import { fireEvent, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

function container(compose, overrides = {}) {
  return {
    id: `ordo:${compose}`, name: compose, project: 'ordo', container: `ordo-${compose}-1`, compose,
    card_id: null, verdict: 'up', uptime: '2 days', status: 'Up 2 days', open_url: null, error: null,
    lent: false, actions: ['restart', 'stop'], control: 'ordo', reason: null, parent: null, group_verdict: null,
    ...overrides,
  }
}

// Langfuse: a card with two containers under it, one of them failed. Monitoring: no single card, so
// a row of its own over two healthy containers.
function groupedTable() {
  const table = fixture('/api/services/table')
  table.rows.push(
    container('langfuse-card', {
      id: 'link:langfuse', name: 'Langfuse', container: null, compose: null, card_id: 'langfuse',
      open_url: 'https://langfuse.example.test/', actions: [], control: 'link', group_verdict: 'failed',
    }),
    container('langfuse-web', { parent: 'link:langfuse' }),
    container('langfuse-db', { parent: 'link:langfuse', verdict: 'failed', status: 'Exited (1)', uptime: null, actions: ['start'] }),
    container('monitoring', {
      id: 'group:monitoring', name: 'Monitoring', container: null, compose: null, actions: [], control: 'group',
      group_verdict: 'up',
    }),
    container('prometheus', { parent: 'group:monitoring' }),
    container('grafana', { parent: 'group:monitoring' }),
  )
  return table
}

async function tableRows() {
  const [table] = await screen.findAllByRole('table')
  return within(table).getAllByRole('row').slice(1)
}

function rowNamed(rows, name) {
  return rows.find((row) => within(row).queryByText(name, { exact: true }))
}

describe('Services: one row per service', () => {
  it('lists a service once, with its containers closed beneath it', async () => {
    const table = groupedTable()
    mockApi({ 'GET /api/services/table': table })
    renderInShell(<ServicesPage />)
    const rows = await tableRows()

    expect(rows).toHaveLength(table.rows.filter((row) => !row.parent).length)
    for (const part of ['langfuse-web', 'langfuse-db', 'prometheus', 'grafana']) expect(rowNamed(rows, part)).toBeUndefined()
    const langfuse = within(rowNamed(rows, 'Langfuse'))
    expect(langfuse.getByText('2 containers · 1 failed')).toBeTruthy()
    // The card's probe passes, but a part has failed: the service row says so.
    expect(langfuse.getByText('Failed')).toBeTruthy()
    const monitoring = within(rowNamed(rows, 'Monitoring'))
    expect(monitoring.getByText('2 containers')).toBeTruthy()
    expect(monitoring.getByText('Running')).toBeTruthy()
    expect(monitoring.queryByRole('button', { name: /^Logs/ })).toBeNull()
  })

  it('opens and closes a service to show its containers with their own controls', async () => {
    mockApi({ 'GET /api/services/table': groupedTable() })
    renderInShell(<ServicesPage />)
    await tableRows()

    const [toggle] = screen.getAllByRole('button', { name: 'Show the 2 containers of Langfuse' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)

    let rows = await tableRows()
    const db = within(rowNamed(rows, 'langfuse-db'))
    expect(db.getByText('Failed')).toBeTruthy()
    expect(db.getByRole('button', { name: 'Start langfuse-db' })).toBeTruthy()
    expect(rowNamed(rows, 'langfuse-web')).toBeTruthy()
    expect(rowNamed(rows, 'prometheus')).toBeUndefined()

    const [hide] = screen.getAllByRole('button', { name: 'Hide the 2 containers of Langfuse' })
    expect(hide.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(hide)
    rows = await tableRows()
    expect(rowNamed(rows, 'langfuse-db')).toBeUndefined()
  })

  it('finds a container by name and opens its service to show it', async () => {
    mockApi({ 'GET /api/services/table': groupedTable() })
    renderInShell(<ServicesPage />)
    await tableRows()

    fireEvent.change(screen.getByLabelText('Filter services'), { target: { value: 'grafana' } })

    const rows = await tableRows()
    expect(rows.map((row) => within(row).queryByText('Monitoring', { exact: true }) ? 'Monitoring' : null).filter(Boolean)).toEqual(['Monitoring'])
    expect(rowNamed(rows, 'grafana')).toBeTruthy()
    expect(rowNamed(rows, 'prometheus')).toBeUndefined()
    expect(rows).toHaveLength(2)
  })
})
