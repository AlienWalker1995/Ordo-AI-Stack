// A Services action is pending until the table shows it took effect: each row has its own state,
// "done" is said only once a table read confirms it, an answer that does not say whether the action
// ran (a timeout, a 503) is checked against the table instead of reported as a failure, and after
// 180 s without the target state the row says so and can act again.
import { act, fireEvent, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { isConfirmed, uptimeSeconds } from '../src/pages/serviceActions.js'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

const TABLE = '/api/services/table'

// The table as the backend answers it, with some rows changed: { 'ordo:evals': { verdict: 'up' } }.
function tableWith(changes = {}) {
  const table = fixture(TABLE)
  for (const [id, patch] of Object.entries(changes)) Object.assign(table.rows.find((row) => row.id === id), patch)
  return table
}

const RUNNING_EVALS = { verdict: 'up', uptime: '2 seconds', status: 'Up 2 seconds', actions: ['restart', 'stop'] }
const STOPPED_WEBUI = { verdict: 'stopped', uptime: null, status: 'Exited (143) 1 second ago', actions: ['start'] }

// A request held in flight until the test answers it.
function held() {
  let answer
  const promise = new Promise((resolve) => { answer = resolve })
  return { promise, answer }
}

// Let timers run for `ms` and every response and render they cause settle.
async function advance(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

// Both layouts are in the DOM (CSS picks one per width, which jsdom does not apply); the table row
// is the wide layout's, enough to drive and read one row.
function tableRow(name) {
  const [table] = screen.getAllByRole('table')
  return within(table).getAllByRole('row').find((row) => within(row).queryByText(name, { exact: true }))
}

function button(rowName, name) {
  return within(tableRow(rowName)).getByRole('button', { name })
}

// Click a row's Stop or Restart and say yes in the dialog.
function confirmAction(rowName, label) {
  fireEvent.click(button(rowName, `${label} ${rowName}`))
  fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: label }))
}

function toastTexts() {
  return within(screen.getByRole('region', { name: 'Notifications' })).queryAllByRole('button').map((b) => b.textContent)
}

// The test's view of the backend: `table` is what GET /api/services/table answers now.
function backend(overrides = {}) {
  const state = { table: tableWith() }
  const api = mockApi({ [`GET ${TABLE}`]: () => state.table, ...overrides })
  return { state, api, tableReads: () => api.sent('GET', TABLE).length }
}

describe('Services: an action is pending until the table confirms it', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
  })
  afterEach(() => {
    vi.useRealTimers()
    delete document.hidden
  })

  it('keeps two pending rows independent', async () => {
    const evalsStart = held()
    const webuiRestart = held()
    backend({
      'POST /api/ops/services/evals/start': () => evalsStart.promise,
      'POST /api/ops/services/open-webui/restart': () => webuiRestart.promise,
    })
    renderInShell(<ServicesPage />)
    await advance()

    fireEvent.click(button('evals', 'Start evals'))
    confirmAction('Open WebUI', 'Restart')
    await advance()

    expect(within(tableRow('evals')).getByText('Starting…')).toBeTruthy()
    expect(within(tableRow('Open WebUI')).getByText('Restarting…')).toBeTruthy()
    expect(button('Open WebUI', 'Stop Open WebUI').getAttribute('aria-busy')).toBe('true')
    // A row with nothing pending stays live.
    expect(button('Qdrant', 'Restart Qdrant').hasAttribute('aria-disabled')).toBe(false)

    // The first row's answer settles only that row.
    evalsStart.answer({ status: 409, body: { detail: 'another operation is in progress' } })
    await advance()
    expect(button('evals', 'Start evals').hasAttribute('aria-disabled')).toBe(false)
    expect(within(tableRow('Open WebUI')).getByText('Restarting…')).toBeTruthy()
    expect(button('Open WebUI', 'Restart Open WebUI').getAttribute('aria-disabled')).toBe('true')
  })

  it('says done only once the table shows the target state', async () => {
    const { state, api } = backend({ 'POST /api/ops/services/open-webui/stop': { ok: true } })
    renderInShell(<ServicesPage />)
    await advance()

    confirmAction('Open WebUI', 'Stop')
    await advance()
    expect(api.sent('POST', '/api/ops/services/open-webui/stop')).toHaveLength(1)

    // Answered, but the table still shows it running: the row waits, nothing is reported.
    await advance(2000)
    expect(within(tableRow('Open WebUI')).getByText('Stopping…')).toBeTruthy()
    expect(toastTexts()).toEqual([])

    state.table = tableWith({ 'ordo:open-webui': STOPPED_WEBUI })
    await advance(2000)
    expect(toastTexts()).toEqual(['Open WebUI: stopped'])
    expect(within(tableRow('Open WebUI')).getByText('Stopped')).toBeTruthy()
    expect(button('Open WebUI', 'Start Open WebUI').hasAttribute('aria-disabled')).toBe(false)
  })

  it.each([
    ['a 503 from the proxy', () => ({ status: 503, body: { detail: 'ops-controller did not answer within 180 s' } })],
    ['a dropped connection', () => { throw new TypeError('Failed to fetch') }],
  ])('treats %s as "may still be running" and confirms from the table', async (_name, failure) => {
    const { state } = backend({ 'POST /api/ops/services/open-webui/restart': failure })
    renderInShell(<ServicesPage />)
    await advance()

    confirmAction('Open WebUI', 'Restart')
    await advance(2000)
    expect(toastTexts()).toEqual([])
    expect(within(tableRow('Open WebUI')).getByText('Restarting…')).toBeTruthy()

    state.table = tableWith({ 'ordo:open-webui': { uptime: '3 seconds', status: 'Up 3 seconds (health: starting)', verdict: 'starting' } })
    await advance(2000)
    expect(toastTexts()).toEqual(['Open WebUI: restarted'])
  })

  // The reason is read wherever the body carries it: FastAPI's detail, a detail object's error, or
  // ops-controller's own error.
  it.each([
    [{ detail: 'another operation is in progress' }],
    [{ detail: { error: 'another operation is in progress' } }],
    [{ error: 'another operation is in progress' }],
  ])('shows the reason in %j and gives the row its buttons back', async (body) => {
    backend({ 'POST /api/ops/services/open-webui/restart': { status: 409, body } })
    renderInShell(<ServicesPage />)
    await advance()

    confirmAction('Open WebUI', 'Restart')
    await advance()

    expect(toastTexts()).toEqual(['Open WebUI: another operation is in progress'])
    expect(within(tableRow('Open WebUI')).getByText('Restart failed: another operation is in progress')).toBeTruthy()
    expect(within(tableRow('Open WebUI')).getByText('Running')).toBeTruthy()
    for (const name of ['Restart Open WebUI', 'Stop Open WebUI']) {
      expect(button('Open WebUI', name).hasAttribute('aria-disabled')).toBe(false)
    }
  })

  it('gives up after 180 s and says what it last saw', async () => {
    backend({ 'POST /api/ops/services/evals/start': { ok: true } })
    renderInShell(<ServicesPage />)
    await advance()

    fireEvent.click(button('evals', 'Start evals'))
    await advance()
    await advance(175000)
    expect(within(tableRow('evals')).getByText('Starting…')).toBeTruthy()

    // Reads run every 5 s after the first minute, so one lands on 180 s.
    await advance(5000)
    const message = 'No change confirmed after 180 s; last seen: Finished (Exited (0) 3 hours ago)'
    expect(toastTexts()).toEqual([`evals: ${message}`])
    expect(within(tableRow('evals')).getByText(`Start: ${message}`)).toBeTruthy()
    expect(button('evals', 'Start evals').hasAttribute('aria-disabled')).toBe(false)
  })

  it('clears the give-up note once a later read shows the target state', async () => {
    const { state } = backend({ 'POST /api/ops/services/evals/start': { ok: true } })
    renderInShell(<ServicesPage />)
    await advance()

    fireEvent.click(button('evals', 'Start evals'))
    await advance()
    await advance(175000)
    expect(within(tableRow('evals')).getByText('Starting…')).toBeTruthy()

    // Reads run every 5 s after the first minute, so one lands on 180 s.
    await advance(5000)
    const message = 'No change confirmed after 180 s; last seen: Finished (Exited (0) 3 hours ago)'
    expect(toastTexts()).toEqual([`evals: ${message}`])
    expect(within(tableRow('evals')).getByText(`Start: ${message}`)).toBeTruthy()

    // It comes up at 200 s: the page's own 10 s poll settles it and the stale note goes.
    state.table = tableWith({ 'ordo:evals': RUNNING_EVALS })
    await advance(10000)
    expect(toastTexts()).toContain('evals: started')
    expect(within(tableRow('evals')).queryByText(`Start: ${message}`)).toBeNull()
    expect(within(tableRow('evals')).getByText('Running')).toBeTruthy()
  })

  it('reads the table every 2 s for the first minute, then every 5 s', async () => {
    const { tableReads } = backend({ 'POST /api/ops/services/evals/start': { ok: true } })
    renderInShell(<ServicesPage />)
    await advance()
    fireEvent.click(button('evals', 'Start evals'))
    await advance()

    // Over 20 s: ten confirmation reads and two of the page's own 10 s polls.
    let before = tableReads()
    await advance(20000)
    expect(tableReads() - before).toBe(12)

    await advance(45000)
    before = tableReads()
    await advance(20000)
    expect(tableReads() - before).toBe(6)
  })

  it('stops reading while the tab is hidden and reads at once when it is back', async () => {
    let hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    const { state, tableReads } = backend({ 'POST /api/ops/services/evals/start': { ok: true } })
    renderInShell(<ServicesPage />)
    await advance()
    fireEvent.click(button('evals', 'Start evals'))
    await advance()

    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    const before = tableReads()
    state.table = tableWith({ 'ordo:evals': RUNNING_EVALS })
    await advance(30000)
    expect(tableReads()).toBe(before)
    expect(toastTexts()).toEqual([])

    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    await advance()
    expect(toastTexts()).toEqual(['evals: started'])
  })

  it('confirms a restart by its start time when the row carries one', async () => {
    const { state } = backend({ 'POST /api/ops/services/open-webui/restart': { ok: true } })
    state.table = tableWith({ 'ordo:open-webui': { started_at: '2026-10-15T08:00:00Z' } })
    renderInShell(<ServicesPage />)
    await advance()

    confirmAction('Open WebUI', 'Restart')
    await advance(2000)
    expect(toastTexts()).toEqual([])

    // Same uptime text (docker rounds), new start time: it restarted.
    state.table = tableWith({ 'ordo:open-webui': { started_at: '2026-10-17T08:00:00Z' } })
    await advance(2000)
    expect(toastTexts()).toEqual(['Open WebUI: restarted'])
  })

  it('confirms a restart by its uptime going back when there is no start time', async () => {
    const { state } = backend({ 'POST /api/ops/services/open-webui/restart': { ok: true } })
    renderInShell(<ServicesPage />)
    await advance()

    confirmAction('Open WebUI', 'Restart')
    state.table = tableWith({ 'ordo:open-webui': { status: 'Up 2 days (healthy)' } })
    await advance(4000)
    expect(toastTexts()).toEqual([])

    state.table = tableWith({ 'ordo:open-webui': { uptime: 'Less than a second', status: 'Up Less than a second (health: starting)', verdict: 'starting' } })
    await advance(2000)
    expect(toastTexts()).toEqual(['Open WebUI: restarted'])
  })
})

describe('Services: the confirmation dialog', () => {
  it('cancels on Escape, sends nothing and returns focus to the button that opened it', async () => {
    const api = mockApi()
    renderInShell(<ServicesPage />)
    const [stop] = await screen.findAllByRole('button', { name: 'Stop Open WebUI' })

    stop.focus()
    fireEvent.click(stop)
    const dialog = screen.getByRole('alertdialog', { name: 'Stop Open WebUI?' })
    expect(within(dialog).getByText('It stays stopped until you start it again.')).toBeTruthy()
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }))

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(document.activeElement).toBe(stop)
    expect(api.sent('POST', '/api/ops/services/open-webui/stop')).toEqual([])
  })

  it('keeps Tab inside the dialog', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    fireEvent.click((await screen.findAllByRole('button', { name: 'Restart Open WebUI' }))[0])
    const dialog = screen.getByRole('alertdialog')
    const [cancel, restart] = within(dialog).getAllByRole('button')

    restart.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(restart)
  })
})

describe('Services: button names', () => {
  it('names an ordo row\'s buttons by the row and anything else\'s by row and project', async () => {
    const table = tableWith({ 'host:media-indexer': { actions: ['start'] } })
    const api = mockApi({ [`GET ${TABLE}`]: table, 'POST /api/ops/projects/media-stack/containers/media-indexer/start': { ok: true } })
    renderInShell(<ServicesPage />)
    await screen.findAllByRole('table')

    const names = (rowName) => within(tableRow(rowName)).getAllByRole('button').map((b) => b.getAttribute('aria-label'))
    expect(names('Open WebUI')).toEqual(['Logs for Open WebUI', 'Restart Open WebUI', 'Stop Open WebUI'])
    expect(names('evals')).toEqual(['Logs for evals', 'Start evals'])
    expect(names('media-server')).toEqual(['Logs for media-server (media-stack)', 'Restart media-server (media-stack)'])
    expect(names('media-indexer')).toEqual(['Logs for media-indexer (media-stack)', 'Start media-indexer (media-stack)'])

    // A managed row's start, once offered, goes to its project route.
    fireEvent.click(button('media-indexer', 'Start media-indexer (media-stack)'))
    expect(await within(tableRow('media-indexer')).findByText('Starting…')).toBeTruthy()
    expect(api.sent('POST', '/api/ops/projects/media-stack/containers/media-indexer/start')).toHaveLength(1)
  })
})

describe('Services: what confirms an action', () => {
  const up = { verdict: 'up', uptime: '2 days', status: 'Up 2 days' }

  it('reads docker\'s uptime text as seconds', () => {
    expect(uptimeSeconds('Less than a second')).toBe(0)
    expect(uptimeSeconds('5 seconds')).toBe(5)
    expect(uptimeSeconds('About a minute')).toBe(60)
    expect(uptimeSeconds('About an hour')).toBe(3600)
    expect(uptimeSeconds('2 days')).toBe(172800)
    expect(uptimeSeconds('Paused')).toBeNull()
    expect(uptimeSeconds(null)).toBeNull()
  })

  it.each([
    ['start', { verdict: 'done' }, { verdict: 'starting' }, true],
    ['start', { verdict: 'done' }, { verdict: 'failed' }, false],
    ['stop', up, { verdict: 'stopped' }, true],
    ['stop', up, { verdict: 'done' }, true],
    ['stop', up, up, false],
    ['restart', up, { verdict: 'up', uptime: '4 seconds', status: 'Up 4 seconds' }, true],
    ['restart', up, up, false],
    ['restart', up, { verdict: 'failed', uptime: null, status: 'Restarting (1) 2 seconds ago' }, false],
    ['restart', { ...up, started_at: 'a' }, { ...up, started_at: 'b' }, true],
    ['restart', { ...up, started_at: 'a' }, { verdict: 'up', uptime: '1 second', started_at: 'a' }, false],
    // Per-second texts: `before` is up to a poll stale, so by the first confirming read the
    // restarted container's new uptime may have grown to or past the old value.
    ['restart', { verdict: 'up', uptime: '3 seconds', status: 'Up 3 seconds' }, { verdict: 'up', uptime: '3 seconds', status: 'Up 3 seconds' }, true],
    ['restart', { verdict: 'up', uptime: 'Less than a second', status: 'Up Less than a second' }, { verdict: 'starting', uptime: '2 seconds', status: 'Up 2 seconds (health: starting)' }, true],
    ['restart', { verdict: 'up', uptime: '1 second', status: 'Up 1 second' }, { verdict: 'up', uptime: '2 seconds', status: 'Up 2 seconds' }, true],
    // Coarser texts plateau: the same "About an hour" twice is not proof of a restart.
    ['restart', { verdict: 'up', uptime: 'About an hour', status: 'Up About an hour' }, { verdict: 'up', uptime: 'About an hour', status: 'Up About an hour' }, false],
  ])('%s from %j to %j: %s', (action, before, row, confirmed) => {
    expect(isConfirmed(action, before, row)).toBe(confirmed)
  })
})
