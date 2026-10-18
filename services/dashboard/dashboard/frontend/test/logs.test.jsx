import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ToastProvider, useToast } from '../src/components/Toast.jsx'
import LogsPage from '../src/pages/LogsPage.jsx'
import { mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

const sample = {
  generated_at: 1000,
  dependencies: [{ dep: 'QDRANT', status: 'failing', last_ok: null, last_failure: { code: 'DEP-QDRANT-TIMEOUT', at: 999, detail: 'late' }, consecutive_failures: 2 }],
  recent: [{ at: 999, code: 'DEP-QDRANT-TIMEOUT', dep: 'QDRANT', target: '/collections/documents', elapsed_ms: 3000, trace: 'abc123', detail: 'ReadTimeout' }],
  codes: [{ code: 'DEP-QDRANT-TIMEOUT', meaning: 'dependency timed out' }],
}

describe('LogsPage', () => {
  it('shows the honest empty state', async () => {
    mockApi()
    renderInShell(<LogsPage />)
    await screen.findByText('No dependency failures recorded since the dashboard started')
  })

  it('shows rows and filters by code, dependency, and trace id', async () => {
    mockApi({ 'GET /api/diagnostics': sample })
    renderInShell(<LogsPage />)
    expect(await screen.findByText('Status: failing')).toBeTruthy()
    expect(screen.getAllByText('DEP-QDRANT-TIMEOUT').length).toBeGreaterThan(0)
    expect(screen.getByText('abc123')).toBeTruthy()
    const input = screen.getByLabelText('Filter failures')
    fireEvent.change(input, { target: { value: 'absent' } })
    await screen.findByText('No failures match this filter')
    fireEvent.change(input, { target: { value: 'abc123' } })
    expect((await screen.findAllByText('ReadTimeout')).length).toBeGreaterThan(0)
  })
})

function ToastHarness() {
  const toast = useToast()
  return <button onClick={() => toast('Request failed', 'error', { code: 'DEP-OPS-TIMEOUT', traceId: 'trace42' })}>Show toast</button>
}

describe('toast diagnostics', () => {
  it('shows the failure code and trace id', () => {
    render(<ToastProvider><ToastHarness /></ToastProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Show toast' }))
    const note = screen.getByRole('button', { name: 'Dismiss notification: Request failed' })
    expect(note.textContent).toContain('DEP-OPS-TIMEOUT')
    expect(note.textContent).toContain('Trace trace42')
  })
})
