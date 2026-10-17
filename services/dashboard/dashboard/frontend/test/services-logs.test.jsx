// The Services logs drawer reads the newest tail once, then only what came after it, appends it,
// keeps the newest 1000 lines, follows the end only while the reader is there, and never shows one
// row's lines in another's drawer.
import { act, fireEvent, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { formatLogTime } from '../src/pages/serviceLogs.js'
import { mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

const WEBUI_LOGS = '/api/ops/services/open-webui/logs'
const QDRANT_LOGS = '/api/ops/services/qdrant/logs'
const T1 = '2026-10-17T04:10:21.044180797Z'
const T2 = '2026-10-17T04:10:24.500000000Z'
const T3 = '2026-10-17T04:10:27.000000001Z'

// An answer in the shape ops-controller's logs routes send (ordo/control/logs.py).
function logsAnswer(lines, lastTs, extra = {}) {
  return { service: 'open-webui', logs: lines.map((line) => `${line}\n`).join(''), tail: 300, since: null,
           last_ts: lastTs, truncated: false, ...extra }
}

function numbered(prefix, count, from = 0) {
  return Array.from({ length: count }, (_, i) => `${prefix}-${from + i}`)
}

// A request held in flight until the test answers it.
function held() {
  let answer
  const promise = new Promise((resolve) => { answer = resolve })
  return { promise, answer }
}

async function advance(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

// The wide layout's row (both layouts are in the DOM; jsdom applies no CSS).
function tableRow(name) {
  const [table] = screen.getAllByRole('table')
  return within(table).getAllByRole('row').find((row) => within(row).queryByText(name, { exact: true }))
}

// `label` is what the row's buttons call it: a managed container carries its project.
async function openLogs(name, label = name) {
  await advance()
  const button = within(tableRow(name)).getByRole('button', { name: `Logs for ${label}` })
  button.focus()
  fireEvent.click(button)
  await advance()
  return button
}

// jsdom's Blob has no text(); FileReader reads it.
function blobText(blob) {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.readAsText(blob)
  })
}

function closeDrawer() {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
}

function logText() {
  return screen.getByRole('log').textContent
}

// jsdom lays nothing out, so the log region is given a size: `content` px of text in a 100 px box.
function giveLogGeometry(content) {
  const el = screen.getByRole('log')
  const box = { content, top: 0 }
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => box.content })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 100 })
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => box.top, set: (v) => { box.top = v } })
  return box
}

describe('Services: the logs drawer', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    vi.setSystemTime(new Date('2026-10-17T04:11:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete document.hidden
  })

  it('reads the tail once, then only what came after the last answer, and shows each line once', async () => {
    const after = { first: logsAnswer(['a', 'b'], T1), [T1]: logsAnswer(['c'], T2), [T2]: logsAnswer([], null) }
    const api = mockApi({ [`GET ${WEBUI_LOGS}`]: (request) => after[request.query.since || 'first'] })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')

    expect(api.sent('GET', WEBUI_LOGS).map((r) => r.query)).toEqual([{ tail: '300' }])
    expect(logText()).toBe('a\nb')

    await advance(3000)
    await advance(3000)
    expect(api.sent('GET', WEBUI_LOGS).map((r) => r.query)).toEqual([
      { tail: '300' }, { tail: '1000', since: T1 }, { tail: '1000', since: T2 },
    ])
    expect(logText()).toBe('a\nb\nc')
    // An empty answer moves no cursor: the next read asks from the same line.
    await advance(3000)
    expect(api.sent('GET', WEBUI_LOGS).at(-1).query).toEqual({ tail: '1000', since: T2 })
    expect(screen.getByText(`3 lines, newest at ${formatLogTime(T2)}`)).toBeTruthy()
  })

  it('keeps the newest 1000 lines and says how many older ones were dropped', async () => {
    const after = { first: logsAnswer(numbered('first', 300), T1), [T1]: logsAnswer(numbered('next', 800), T2) }
    mockApi({ [`GET ${WEBUI_LOGS}`]: (request) => after[request.query.since || 'first'] || logsAnswer([], null) })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')
    expect(screen.queryByText(/dropped/)).toBeNull()

    await advance(3000)
    const lines = logText().split('\n')
    expect(lines).toHaveLength(1000)
    expect(lines[0]).toBe('first-100')
    expect(lines.at(-1)).toBe('next-799')
    expect(screen.getByText('Showing the newest 1,000 lines; 100 older were dropped.')).toBeTruthy()
  })

  it('says when the control plane cut an answer', async () => {
    mockApi({ [`GET ${WEBUI_LOGS}`]: logsAnswer(['tail end'], T1, { truncated: true }) })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')
    expect(screen.getByText(/^Some lines are missing/)).toBeTruthy()
  })

  it('aborts the read in flight on a new target and never shows the old row\'s lines', async () => {
    const webui = held()
    const qdrant = held()
    const api = mockApi({ [`GET ${WEBUI_LOGS}`]: () => webui.promise, [`GET ${QDRANT_LOGS}`]: () => qdrant.promise })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')
    expect(screen.getByText('Loading logs…')).toBeTruthy()

    closeDrawer()
    await openLogs('Qdrant')
    const [webuiRead] = api.sent('GET', WEBUI_LOGS)
    expect(webuiRead.signal.aborted).toBe(true)
    expect(api.sent('GET', QDRANT_LOGS)[0].signal.aborted).toBe(false)
    expect(screen.getByRole('dialog', { name: 'Qdrant logs' })).toBeTruthy()
    expect(logText()).toBe('Loading logs…')

    // The old read answering late changes nothing.
    webui.answer(logsAnswer(['stale webui line'], T1))
    await advance()
    expect(screen.queryByText(/stale webui line/)).toBeNull()

    qdrant.answer(logsAnswer(['qdrant ready'], T1))
    await advance()
    expect(logText()).toBe('qdrant ready')
  })

  it('says so when there is no output yet', async () => {
    mockApi({ [`GET ${WEBUI_LOGS}`]: logsAnswer([], null) })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')
    expect(logText()).toBe('No log output yet')
    expect(screen.getByRole('button', { name: 'Copy' }).hasAttribute('disabled')).toBe(true)
  })

  it('keeps the text when a refresh fails and gives the reason', async () => {
    let fail = false
    mockApi({
      [`GET ${WEBUI_LOGS}`]: () => (fail
        ? { status: 503, body: { detail: 'ops-controller did not answer GET /services/open-webui/logs within 15 s' } }
        : logsAnswer(['still here'], T1)),
    })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')

    fail = true
    await advance(3000)
    expect(logText()).toBe('still here')
    expect(screen.getByText('Could not refresh: ops-controller did not answer GET /services/open-webui/logs within 15 s'))
      .toBeTruthy()

    fail = false
    await advance(3000)
    expect(screen.queryByText(/Could not refresh/)).toBeNull()
  })

  it('stops reading while the tab is hidden and reads at once when it is back', async () => {
    let hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    const api = mockApi({ [`GET ${WEBUI_LOGS}`]: logsAnswer(['line'], T1) })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')
    const reads = () => api.sent('GET', WEBUI_LOGS).length
    expect(reads()).toBe(1)

    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    await advance(30000)
    expect(reads()).toBe(1)

    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    await advance()
    expect(reads()).toBe(2)
    await advance(3000)
    expect(reads()).toBe(3)
  })

  it('follows the end only while the reader is there, and offers a jump when scrolled up', async () => {
    const after = { first: logsAnswer(['one'], T1), [T1]: logsAnswer(['two'], T2), [T2]: logsAnswer(['three', 'four'], T3) }
    mockApi({ [`GET ${WEBUI_LOGS}`]: (request) => after[request.query.since || 'first'] || logsAnswer([], null) })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')
    const follow = screen.getByRole('button', { name: 'Follow logs' })
    expect(follow.getAttribute('aria-pressed')).toBe('true')

    const box = giveLogGeometry(500)
    await advance(3000)
    expect(box.top).toBe(500)

    // Scrolled up: the view stays put and the new lines are counted.
    box.top = 120
    fireEvent.scroll(screen.getByRole('log'))
    expect(follow.getAttribute('aria-pressed')).toBe('false')
    box.content = 700
    await advance(3000)
    expect(box.top).toBe(120)
    const jump = screen.getByRole('button', { name: 'Jump to latest (2 new)' })

    fireEvent.click(jump)
    expect(box.top).toBe(700)
    expect(screen.queryByRole('button', { name: /Jump to latest/ })).toBeNull()
    expect(follow.getAttribute('aria-pressed')).toBe('true')
    // The label does not change with the state; only aria-pressed does.
    expect(follow.textContent).toBe('Follow logs')
  })

  it('copies and downloads the buffer as plain text', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const blobs = []
    URL.createObjectURL = vi.fn((blob) => { blobs.push(blob); return 'blob:logs' })
    URL.revokeObjectURL = vi.fn()
    const clicked = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() { clicked.push(this) })
    mockApi({ [`GET ${WEBUI_LOGS}`]: logsAnswer(['alpha', 'beta'], T1) })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await advance()
    expect(writeText).toHaveBeenCalledWith('alpha\nbeta\n')

    fireEvent.click(screen.getByRole('button', { name: 'Download' }))
    expect(blobs).toHaveLength(1)
    expect(blobs[0].type).toBe('text/plain')
    expect(await blobText(blobs[0])).toBe('alpha\nbeta\n')
    expect(clicked.map((a) => [a.download, a.href])).toEqual([['ordo-open-webui-logs.txt', 'blob:logs']])
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:logs')
  })

  it('names a managed container\'s download by its project', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:logs')
    URL.revokeObjectURL = vi.fn()
    const clicked = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() { clicked.push(this) })
    mockApi({ 'GET /api/ops/projects/media-stack/containers/media-server/logs': logsAnswer(['listening'], T1) })
    renderInShell(<ServicesPage />)
    await openLogs('media-server', 'media-server (media-stack)')
    fireEvent.click(screen.getByRole('button', { name: 'Download' }))
    expect(clicked.map((a) => a.download)).toEqual(['media-stack-media-server-logs.txt'])
  })

  it('gives the log region its role and focus, and returns focus to Logs on close', async () => {
    mockApi({ [`GET ${WEBUI_LOGS}`]: logsAnswer(['line'], T1) })
    renderInShell(<ServicesPage />)
    const logsButton = await openLogs('Open WebUI')

    const log = screen.getByRole('log', { name: 'Open WebUI log output' })
    expect(log.getAttribute('aria-live')).toBe('off')
    expect(log.tabIndex).toBe(0)
    const drawer = within(screen.getByRole('dialog', { name: 'Open WebUI logs' }))
    expect(drawer.getAllByRole('button').map((b) => b.textContent)).toEqual(['Close', 'Follow logs', 'Copy', 'Download'])

    closeDrawer()
    expect(document.activeElement).toBe(logsButton)
  })

  it('works against an ops-controller without the cursor: the whole tail each time, no repeats', async () => {
    // Before ordo/control/logs.py: no last_ts, since ignored, the same tail every time.
    const api = mockApi({ [`GET ${WEBUI_LOGS}`]: { service: 'open-webui', logs: 'a\nb\n' } })
    renderInShell(<ServicesPage />)
    await openLogs('Open WebUI')
    await advance(3000)
    await advance(3000)
    expect(api.sent('GET', WEBUI_LOGS).map((r) => r.query)).toEqual([{ tail: '300' }, { tail: '300' }, { tail: '300' }])
    expect(logText()).toBe('a\nb')
    expect(screen.getByText('2 lines')).toBeTruthy()
  })
})
