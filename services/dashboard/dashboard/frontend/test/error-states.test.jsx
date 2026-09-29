// A backend that fails is stated on the page, never drawn as a blank page or as zeros: each page
// names what is unavailable and why, and a page that throws is contained by the shell.
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App from '../src/App.jsx'
import { HostStepsProvider } from '../src/components/HostSteps.jsx'
import MediaPage from '../src/pages/MediaPage.jsx'
import ModelsPage from '../src/pages/ModelsPage.jsx'
import OverviewPage from '../src/pages/OverviewPage.jsx'
import PerformancePage from '../src/pages/PerformancePage.jsx'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

// As main.jsx mounts it: App brings its own toasts and banners.
const renderApp = () => render(<HostStepsProvider><App /></HostStepsProvider>)

const DOWN = { status: 503, body: { detail: 'The control plane is not answering' } }
const networkFailure = () => { throw new TypeError('Failed to fetch') }

describe('a page whose backend is down says so', () => {
  it.each([
    ['Overview', OverviewPage, 'GET /api/overview', DOWN,
      'The dashboard API is not answering: The control plane is not answering'],
    ['Services', ServicesPage, 'GET /api/services/table', DOWN,
      'The service list is unavailable: The control plane is not answering'],
    ['Models', ModelsPage, 'GET /api/models', DOWN,
      'Models are unavailable: The control plane is not answering'],
    ['Media', MediaPage, 'GET /api/media', { status: 503, body: { detail: 'ComfyUI is not answering' } },
      'ComfyUI is not answering. After a restart it takes one to five minutes to come back.'],
    ['Performance', PerformancePage, 'GET /api/perf/grafana', networkFailure,
      'Could not check Grafana: Network error contacting /api/perf/grafana'],
    ['Overview (network down)', OverviewPage, 'GET /api/overview', networkFailure,
      'The dashboard API is not answering: Network error contacting /api/overview'],
  ])('%s', async (_name, Page, request, failure, message) => {
    mockApi({ [request]: failure })
    renderInShell(<Page />)
    const status = await screen.findByText(message)
    expect(status.closest('[role="status"]')).not.toBeNull()
  })

  it('Services keeps its table when only CPU and memory are unavailable', async () => {
    mockApi({ 'GET /api/hardware/service-pressure': DOWN })
    renderInShell(<ServicesPage />)
    await screen.findByText('CPU and memory are unavailable right now.')
    expect(screen.getAllByText('Open WebUI').length).toBeGreaterThan(0)
  })

  it('Services warns that states may be stale when the control plane is not answering', async () => {
    mockApi({ 'GET /api/services/table': () => ({ groups: [], control_plane: false }) })
    renderInShell(<ServicesPage />)
    await screen.findByText('The control plane is not answering; states may be stale.')
  })

  it('Media keeps the rest of the page when only the scheduler is down', async () => {
    mockApi({ 'GET /api/orchestration/gpu/history': { status: 502, body: { detail: 'scheduler unreachable' } } })
    renderInShell(<MediaPage />)
    await screen.findByText('The scheduler is not answering.')
    expect(await screen.findByText('Rendering now')).toBeTruthy()
  })

  it('Performance says Grafana is not running instead of drawing an empty frame', async () => {
    mockApi({ 'GET /api/perf/grafana': { available: false, path: '/grafana/d/ordo-llm-gpu/ordo-performance' } })
    renderInShell(<PerformancePage />)
    await screen.findByText(/Grafana is not running/)
    expect(screen.queryByTitle('Ordo performance (Grafana)')).toBeNull()
  })
})

describe('the app shell', () => {
  it('contains a page that throws instead of going blank', async () => {
    // A response missing the fields the page reads makes it throw while rendering.
    mockApi({ 'GET /api/overview': {} })
    vi.spyOn(console, 'error').mockImplementation(() => {})
    history.replaceState(null, '', '#overview')
    renderApp()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('This page hit an error and stopped rendering.')
    expect(screen.getByRole('tab', { name: 'Services' })).toBeTruthy()
  })

  it('asks a local operator to sign in when a change is refused', async () => {
    // The auth middleware's refusal in local mode (dashboard/app.py).
    const LOCAL_401 = 'Sign in with the link `ordo up` printed, or send Authorization: Bearer <OPS_CONTROLLER_TOKEN>'
    let signedIn = true
    mockApi({
      'GET /api/auth/session': () => ({ mode: 'local', signed_in: signedIn }),
      'POST /api/ops/services/open-webui/restart': () => {
        signedIn = false
        return { status: 401, body: { detail: LOCAL_401 } }
      },
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    history.replaceState(null, '', '#services')
    renderApp()

    const [restart] = await screen.findAllByRole('button', { name: 'Restart Open WebUI' })
    expect(screen.queryByRole('heading', { name: 'Sign in to make changes' })).toBeNull()
    restart.click()

    await screen.findByRole('heading', { name: 'Sign in to make changes' })
    await screen.findByText(`Open WebUI: ${LOCAL_401}`)
  })
})
