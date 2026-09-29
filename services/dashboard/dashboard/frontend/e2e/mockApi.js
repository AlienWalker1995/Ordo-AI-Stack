// The fake backend for the smoke suite: every /api/* request is answered from the fixture file at
// the same path under test/fixtures (the files the component tests use, and whose shape
// tests/test_dashboard_frontend_fixtures.py checks against the backend). Media thumbnails get a
// tiny PNG and the embedded Grafana a blank page. Anything else is a 404 recorded in `unhandled`,
// so a page that starts calling an endpoint the fixtures do not cover fails the suite.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'test', 'fixtures')

// A 1x1 PNG.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)

function readFixture(pathname) {
  try {
    return readFileSync(join(FIXTURES, `${pathname}.json`), 'utf-8')
  } catch {
    return null
  }
}

/** Route the page's API and Grafana traffic to fixtures. Returns the list of unanswered requests. */
export async function mockApi(page) {
  const unhandled = []

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (request.method() === 'GET' && pathname === '/api/media/view') {
      return route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
    }
    const body = request.method() === 'GET' ? readFixture(pathname) : null
    if (body === null) {
      unhandled.push(`${request.method()} ${pathname}`)
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"no fixture"}' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body })
  })

  await page.route('**/grafana/**', (route) => route.fulfill({
    status: 200,
    contentType: 'text/html',
    body: '<!doctype html><title>Grafana</title><body></body>',
  }))

  return unhandled
}
