// api.js reports a failed response's reason wherever the backend put it (detail, detail.error,
// error), and says "HTTP <code>" when the body carries no message — never the browser's
// statusText, which is protocol-dependent ("Conflict" on HTTP/1.1, empty over h2/h3).
import { describe, expect, it, vi } from 'vitest'
import { api } from '../src/api.js'

/** Stub fetch with one failing response; `statusText` as an HTTP/1.1 server would send it. */
function failWith(status, { statusText = '', body = '', contentType = 'text/plain' } = {}) {
  const fetchMock = vi.fn(async () => new Response(body, { status, statusText, headers: { 'content-type': contentType } }))
  vi.stubGlobal('fetch', fetchMock)
}

describe('api.js error messages', () => {
  it.each([
    ['Conflict', 409],
    ['Internal Server Error', 500],
  ])('says "HTTP %i", not "%s", when the body carries no message', async (statusText, status) => {
    failWith(status, { statusText })
    await expect(api.get('/api/nope')).rejects.toMatchObject({ message: `HTTP ${status}`, status })
  })

  it('prefers the body message over the status code', async () => {
    failWith(409, {
      statusText: 'Conflict',
      body: JSON.stringify({ detail: 'another operation is in progress' }),
      contentType: 'application/json',
    })
    await expect(api.get('/api/nope')).rejects.toMatchObject({ message: 'another operation is in progress', status: 409 })
  })
})
