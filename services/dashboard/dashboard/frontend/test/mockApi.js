// A fake dashboard backend for component tests: `fetch` answers each /api/* request from the
// fixture file at the same path (test/fixtures/api/models.json answers /api/models), unless the
// test overrides that request. Every request is recorded, so a test can assert what was sent and,
// just as important, what was not.
import { vi } from 'vitest'

const FIXTURES = import.meta.glob('./fixtures/api/**/*.json', { eager: true, import: 'default' })

/** The fixture body for an API path, deep-copied so a test cannot change it for the next one. */
export function fixture(path) {
  const body = FIXTURES[`./fixtures${path}.json`]
  if (body === undefined) throw new Error(`No fixture for ${path}`)
  return structuredClone(body)
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/**
 * Install the fake backend as the global fetch.
 *
 * `overrides` maps 'METHOD /api/path' to how that request is answered:
 *   - a plain value: sent as a 200 JSON body;
 *   - { status, body }: sent with that status (an error, for one);
 *   - a function (request) => either of those, or throws to simulate a network failure.
 * A request with neither an override nor a fixture gets a 404, and is listed in `unhandled`.
 */
export function mockApi(overrides = {}) {
  const requests = []
  const unhandled = []

  const fetchMock = vi.fn(async (url, options = {}) => {
    const method = options.method || 'GET'
    const path = new URL(url, 'http://dashboard.test').pathname
    const request = { method, path, body: options.body ? JSON.parse(options.body) : undefined }
    requests.push(request)

    const key = `${method} ${path}`
    let answer = overrides[key]
    if (typeof answer === 'function') answer = answer(request)
    if (answer === undefined) {
      const body = FIXTURES[`./fixtures${path}.json`]
      if (body === undefined) {
        unhandled.push(key)
        return jsonResponse(404, { detail: `No mock for ${key}` })
      }
      return jsonResponse(200, structuredClone(body))
    }
    if (answer && typeof answer === 'object' && 'status' in answer && 'body' in answer) {
      return jsonResponse(answer.status, answer.body)
    }
    return jsonResponse(200, answer)
  })

  vi.stubGlobal('fetch', fetchMock)
  return {
    requests,
    unhandled,
    /** Requests sent with this method to this path. */
    sent: (method, path) => requests.filter((r) => r.method === method && r.path === path),
  }
}
