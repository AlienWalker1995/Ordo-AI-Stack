// The Services page says how far its numbers can be trusted: a refresh that failed keeps the last
// list on screen marked with when it was read, CPU and memory nobody measured read as a dash that
// says why, and pressure nothing has replaced for 60 s is marked stale like the list.
import { act, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

const TABLE = '/api/services/table'
const PRESSURE = '/api/hardware/service-pressure'
const STALE = /^Last updated \d\d:\d\d:\d\d, retrying$/
const PRESSURE_STALE = /^CPU and memory last updated \d\d:\d\d:\d\d, retrying$/

// Let timers run for `ms` and every response and render they cause settle.
async function advance(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

function tableRow(name) {
  const [table] = screen.getAllByRole('table')
  return within(table).getAllByRole('row').find((row) => within(row).queryByText(name, { exact: true }))
}

// Pressure as the backend answers it, with Open WebUI's row changed.
function pressureWith(patch) {
  const pressure = fixture(PRESSURE)
  Object.assign(pressure.services.find((s) => s.id === 'webui'), patch)
  return pressure
}

describe('Services: stale and unmeasured data', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('marks the list stale after a failed refresh and clears it on the next success', async () => {
    const answers = { failing: false }
    mockApi({
      [`GET ${TABLE}`]: () => (answers.failing ? { status: 503, body: { detail: 'ops-controller did not answer' } } : fixture(TABLE)),
    })
    renderInShell(<ServicesPage />)
    await advance()
    expect(screen.queryByText(STALE)).toBeNull()

    answers.failing = true
    await advance(10000)
    const note = screen.getByText(STALE)
    expect(note.getAttribute('title')).toBe('ops-controller did not answer')
    // The list stays, described by the note.
    expect(tableRow('Open WebUI')).toBeTruthy()
    const [table] = screen.getAllByRole('table')
    expect(table.getAttribute('aria-describedby').split(' ')).toContain('services-stale')
    expect(document.getElementById('services-stale').contains(note)).toBe(true)

    answers.failing = false
    await advance(10000)
    expect(screen.queryByText(STALE)).toBeNull()
    expect(screen.getAllByRole('table')[0].hasAttribute('aria-describedby')).toBe(false)
  })

  it('shows an unmeasured value as a dash that says why', async () => {
    mockApi({ [`GET ${PRESSURE}`]: pressureWith({ cpu_pct: null, mem_gb: null, reason: 'source unavailable' }) })
    renderInShell(<ServicesPage />)
    await advance()

    const dashes = within(tableRow('Open WebUI')).getAllByTitle('not measured: source unavailable')
    expect(dashes.map((d) => d.textContent)).toEqual(['—', '—'])
  })

  it('says once why CPU and memory are unavailable', async () => {
    mockApi({ [`GET ${PRESSURE}`]: { ...fixture(PRESSURE), unavailable: true, reason: 'ops-controller is not answering' } })
    renderInShell(<ServicesPage />)
    await advance()

    expect(screen.getAllByText('CPU and memory are unavailable: ops-controller is not answering')).toHaveLength(1)
  })

  it('marks pressure nothing has replaced for 60 s as stale', async () => {
    let reads = 0
    mockApi({
      // The first read answers; the next one hangs.
      [`GET ${PRESSURE}`]: () => (reads++ === 0 ? fixture(PRESSURE) : new Promise(() => {})),
    })
    renderInShell(<ServicesPage />)
    await advance()

    await advance(55000)
    expect(screen.queryByText(PRESSURE_STALE)).toBeNull()
    await advance(10000)
    expect(screen.getByText(PRESSURE_STALE).getAttribute('title')).toBe('no new reading for over 60 s')
  })
})
