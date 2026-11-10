// The attention list: each item can be dismissed until its condition clears, and the dismissed
// items stay one click away with a Restore.
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import OverviewPage from '../src/pages/OverviewPage.jsx'
import { fixture, mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

function attentionSection() {
  return screen.getByRole('heading', { name: 'Needs attention' }).closest('section')
}

describe('the attention list', () => {
  it('dismisses an item by its id and reloads the overview', async () => {
    const api = mockApi({ 'POST /api/attention/dismiss': { ok: true } })
    renderInShell(<OverviewPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss Disk 82% full' }))
    await waitFor(() => expect(api.sent('POST', '/api/attention/dismiss')).toHaveLength(1))
    expect(api.sent('POST', '/api/attention/dismiss')[0].body).toEqual({ id: 'disk:warning' })
    await waitFor(() => expect(api.sent('GET', '/api/overview').length).toBeGreaterThan(1))
  })

  it('keeps dismissed items behind a toggle, each with a Restore', async () => {
    const api = mockApi({ 'POST /api/attention/restore': { ok: true } })
    renderInShell(<OverviewPage />)
    const toggle = await screen.findByRole('button', { name: '1 dismissed (show)' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('web-dev-1 exited with an error')).toBeNull()

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const dismissed = within(attentionSection()).getByText('web-dev-1 exited with an error').closest('li')
    fireEvent.click(within(dismissed).getByRole('button', { name: 'Restore web-dev-1 exited with an error' }))
    await waitFor(() => expect(api.sent('POST', '/api/attention/restore')).toHaveLength(1))
    expect(api.sent('POST', '/api/attention/restore')[0].body).toEqual({ id: 'host-failed:web-dev-1' })
  })

  it('shows only the toggle when everything that needs attention was dismissed', async () => {
    const o = fixture('/api/overview')
    mockApi({ 'GET /api/overview': { ...o, attention: [], status: { level: 'ok', text: 'All systems normal' } } })
    renderInShell(<OverviewPage />)
    expect(await screen.findByRole('button', { name: '1 dismissed (show)' })).toBeTruthy()
    expect(within(attentionSection()).queryByRole('button', { name: /^Dismiss/ })).toBeNull()
  })

  it('says so when a dismissal fails', async () => {
    mockApi({ 'POST /api/attention/dismiss': { status: 503, body: { detail: 'The control plane is not answering' } } })
    renderInShell(<OverviewPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss Disk 82% full' }))
    expect(await screen.findByText(/Could not dismiss "Disk 82% full"/)).toBeTruthy()
  })
})
