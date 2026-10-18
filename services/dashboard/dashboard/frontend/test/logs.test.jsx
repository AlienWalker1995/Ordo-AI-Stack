import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider, useToast } from '../src/components/Toast.jsx'
import { formatElapsed } from '../src/lib/format.js'
import LogsPage from '../src/pages/LogsPage.jsx'
import ModelsPage from '../src/pages/ModelsPage.jsx'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

const sample = {
  generated_at: 1000,
  dependencies: [
    { dep: 'QDRANT', target: null, status: 'failing', last_ok: null, last_failure: { code: 'DEP-QDRANT-TIMEOUT', at: 999, detail: 'late' }, consecutive_failures: 2 },
    { dep: 'PROBE', target: 'langfuse', status: 'failing', last_ok: null, last_failure: { code: 'DEP-PROBE-UNREACHABLE', at: 998, detail: 'refused' }, consecutive_failures: 3 },
    { dep: 'PROBE', target: 'open-webui', status: 'ok', last_ok: 997, last_failure: null, consecutive_failures: 0 },
  ],
  recent: [
    { at: 999, code: 'DEP-QDRANT-TIMEOUT', dep: 'QDRANT', target: '/collections/documents', elapsed_ms: 12345.6, trace: 'abc123', detail: 'ReadTimeout' },
    { at: 998, code: 'DEP-PROBE-UNREACHABLE', dep: 'PROBE', target: 'langfuse', elapsed_ms: 3.4, trace: 'def456', detail: 'ConnectError: refused' },
  ],
  legend: {
    kinds: [{ kind: 'TIMEOUT', meaning: 'the dependency did not answer within the client\'s timeout' }],
    dependencies: [{ dep: 'QDRANT', meaning: 'Qdrant, the vector store' }],
  },
}

afterEach(() => vi.restoreAllMocks())

describe('LogsPage', () => {
  it('shows the honest empty state', async () => {
    mockApi()
    renderInShell(<LogsPage />)
    await screen.findByText('No dependency failures recorded since the dashboard started')
  })

  it('shows a row per probe target, and filters failures by code, dependency and trace id', async () => {
    mockApi({ 'GET /api/diagnostics': sample })
    renderInShell(<LogsPage />)
    expect((await screen.findAllByText('Failing')).length).toBeGreaterThan(0)
    // Each PROBE target is its own row, in the table as in the cards.
    const [statusTable] = screen.getAllByRole('table')
    expect(within(statusTable).getByText('langfuse')).toBeTruthy()
    expect(within(statusTable).getByText('open-webui')).toBeTruthy()
    expect(screen.getAllByText('DEP-QDRANT-TIMEOUT').length).toBeGreaterThan(0)
    expect(screen.getAllByText('abc123').length).toBeGreaterThan(0)
    const input = screen.getByLabelText('Filter failures')
    fireEvent.change(input, { target: { value: 'absent' } })
    await screen.findByText('No failures match this filter')
    fireEvent.change(input, { target: { value: 'def456' } })
    expect((await screen.findAllByText('ConnectError: refused')).length).toBeGreaterThan(0)
    expect(screen.queryAllByText('ReadTimeout')).toHaveLength(0)
  })

  it('formats elapsed times the same way everywhere', async () => {
    mockApi({ 'GET /api/diagnostics': sample })
    renderInShell(<LogsPage />)
    expect((await screen.findAllByText('12.3 s')).length).toBe(2)   // the table and the card
    expect(screen.getAllByText('3 ms').length).toBe(2)
  })

  it('explains the codes with two short lists, not every pair', async () => {
    mockApi({ 'GET /api/diagnostics': sample })
    renderInShell(<LogsPage />)
    await screen.findByText('Kinds')
    expect(screen.getByText('Dependencies')).toBeTruthy()
    expect(screen.getByText('Qdrant, the vector store')).toBeTruthy()
    expect(screen.getByText('the dependency did not answer within the client\'s timeout')).toBeTruthy()
  })
})

describe('formatElapsed', () => {
  it('reads whole milliseconds under 10 s and seconds with one decimal after', () => {
    expect(formatElapsed(0)).toBe('0 ms')
    expect(formatElapsed(734.4)).toBe('734 ms')
    expect(formatElapsed(9999.4)).toBe('9999 ms')
    expect(formatElapsed(9999.6)).toBe('10.0 s')
    expect(formatElapsed(12345)).toBe('12.3 s')
    expect(formatElapsed(null)).toBe('—')
  })
})

function ToastHarness() {
  const toast = useToast()
  return <button onClick={() => toast('Request failed', 'error', { code: 'DEP-OPS-TIMEOUT', traceId: 'trace42' })}>Show toast</button>
}

function toastTexts() {
  return within(screen.getByRole('region', { name: 'Notifications' })).queryAllByRole('button').map((b) => b.textContent)
}

// A failed request as the dashboard answers one: the DEP code in the body, the trace id in a header.
const FAILED = { status: 503, body: { detail: 'ops-controller is down', code: 'DEP-OPS-UNREACHABLE' }, headers: { 'X-Trace-Id': 'trace77' } }

describe('toast diagnostics', () => {
  it('shows the failure code and trace id', () => {
    render(<ToastProvider><ToastHarness /></ToastProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Show toast' }))
    const note = screen.getByRole('button', { name: 'Dismiss notification: Request failed' })
    expect(note.textContent).toContain('DEP-OPS-TIMEOUT')
    expect(note.textContent).toContain('Trace trace42')
  })

  it('a failed Services action names the code and trace id', async () => {
    // A 503 leaves the outcome unknown (checked against the table), so the control plane's own 500.
    mockApi({ 'POST /api/ops/services/open-webui/restart': { ...FAILED, status: 500, body: { detail: 'ops-controller is down', code: 'DEP-OPS-HTTP5XX' } } })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderInShell(<ServicesPage />)
    const [restart] = await screen.findAllByRole('button', { name: 'Restart Open WebUI' })
    fireEvent.click(restart)
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Restart' }))
    await screen.findByRole('button', { name: 'Dismiss notification: Open WebUI: ops-controller is down' })
    expect(toastTexts()[0]).toContain('DEP-OPS-HTTP5XX · Trace trace77')
  })

  it('a failed model delete names the code and trace id', async () => {
    mockApi({ 'POST /api/models/delete': FAILED })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderInShell(<ModelsPage />)
    const [remove] = await screen.findAllByRole('button', { name: 'Delete gemma-4-31B-it-Q5_K_M.gguf' })
    fireEvent.click(remove)
    await screen.findByRole('button', { name: 'Dismiss notification: ops-controller is down' })
    expect(toastTexts()[0]).toContain('DEP-OPS-UNREACHABLE · Trace trace77')
  })

  it('a failed benchmark names the code and trace id', async () => {
    mockApi({ 'POST /api/throughput/benchmark': { ...FAILED, body: { detail: 'Model gateway answered HTTP 503', code: 'DEP-GATEWAY-HTTP5XX' } } })
    renderInShell(<ModelsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Run a benchmark' }))
    await screen.findByRole('button', { name: 'Dismiss notification: Benchmark failed: Model gateway answered HTTP 503' })
    expect(toastTexts()[0]).toContain('DEP-GATEWAY-HTTP5XX · Trace trace77')
  })
})
