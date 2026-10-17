// Every action that stops, restarts, removes or deletes something asks first, and a cancelled
// confirmation sends nothing. The request only goes out once the operator says yes.
import { fireEvent, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ModelsPage from '../src/pages/ModelsPage.jsx'
import ServicesPage from '../src/pages/ServicesPage.jsx'
import { mockApi } from './mockApi.js'
import { renderInShell } from './renderInShell.jsx'

function answerConfirm(answer) {
  return vi.spyOn(window, 'confirm').mockReturnValue(answer)
}

// Both Services layouts are in the DOM (CSS picks one per width, which jsdom does not apply), and
// they share one action component; the first match is enough to drive it.
async function firstButton(name) {
  const [button] = await screen.findAllByRole('button', { name })
  return button
}

describe('Services: Stop and Restart', () => {
  it.each([
    ['Stop', 'stop'],
    ['Restart', 'restart'],
  ])('%s asks first and sends nothing when cancelled', async (label, action) => {
    const api = mockApi()
    const confirm = answerConfirm(false)
    renderInShell(<ServicesPage />)

    fireEvent.click(await firstButton(`${label} Open WebUI`))

    expect(confirm).toHaveBeenCalledWith(`${label} Open WebUI?`)
    expect(api.sent('POST', `/api/ops/services/open-webui/${action}`)).toEqual([])
  })

  it.each([
    ['Stop', 'stop', 'stopped'],
    ['Restart', 'restart', 'restarted'],
  ])('%s goes out once confirmed', async (label, action, done) => {
    const api = mockApi({
      [`POST /api/ops/services/open-webui/${action}`]: { ok: true, service: 'open-webui', action: done },
    })
    answerConfirm(true)
    renderInShell(<ServicesPage />)

    fireEvent.click(await firstButton(`${label} Open WebUI`))

    await screen.findByText(`Open WebUI: ${done}`)
    expect(api.sent('POST', `/api/ops/services/open-webui/${action}`)).toHaveLength(1)
  })

  it('Start is not destructive and does not ask', async () => {
    const api = mockApi({ 'POST /api/ops/services/evals/start': { ok: true, service: 'evals', action: 'started' } })
    const confirm = answerConfirm(false)
    renderInShell(<ServicesPage />)

    const evalsCard = (await screen.findAllByText('evals'))[0].closest('li')
    fireEvent.click(within(evalsCard).getByRole('button', { name: 'Start' }))

    await screen.findByText('evals: started')
    expect(confirm).not.toHaveBeenCalled()
    expect(api.sent('POST', '/api/ops/services/evals/start')).toHaveLength(1)
  })

  it('offers no controls for a service the control plane will not cycle', async () => {
    mockApi()
    renderInShell(<ServicesPage />)
    await screen.findAllByText('ops-controller')
    expect(screen.queryByRole('button', { name: 'Stop ops-controller' })).toBeNull()
    expect(screen.getAllByText('Host only').length).toBeGreaterThan(0)
  })
})

describe('Models: deleting a model file', () => {
  const UNUSED = 'gemma-4-31B-it-Q5_K_M.gguf'

  it('asks first and sends nothing when cancelled', async () => {
    const api = mockApi()
    const confirm = answerConfirm(false)
    renderInShell(<ModelsPage />)

    fireEvent.click(await firstButton(`Delete ${UNUSED}`))

    expect(confirm).toHaveBeenCalledWith(`Delete ${UNUSED} from disk? This cannot be undone.`)
    expect(api.sent('POST', '/api/models/delete')).toEqual([])
  })

  it('deletes the named file once confirmed', async () => {
    const api = mockApi({ 'POST /api/models/delete': { ok: true, deleted: UNUSED } })
    answerConfirm(true)
    renderInShell(<ModelsPage />)

    fireEvent.click(await firstButton(`Delete ${UNUSED}`))

    await screen.findByText(`Deleted ${UNUSED}`)
    expect(api.sent('POST', '/api/models/delete')).toEqual([
      { method: 'POST', path: '/api/models/delete', body: { file: UNUSED } },
    ])
  })

  it('cannot delete a file a running server depends on', async () => {
    mockApi()
    renderInShell(<ModelsPage />)
    const buttons = await screen.findAllByRole('button', { name: 'Delete Qwen3.8-27B-TurboFCFusion-Q6_K.gguf' })
    for (const button of buttons) expect(button.disabled).toBe(true)
  })
})

describe('Models: switching the GPU model', () => {
  async function chooseAndSwitch(model) {
    const select = await screen.findByLabelText('Switch the GPU model')
    fireEvent.change(select, { target: { value: model } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch' }))
  }

  it('asks first and sends nothing when cancelled', async () => {
    const api = mockApi()
    const confirm = answerConfirm(false)
    renderInShell(<ModelsPage />)

    await chooseAndSwitch('gemma4-31b')

    expect(confirm.mock.calls[0][0]).toMatch(/^Switch the GPU chat model to gemma4-31b\?/)
    expect(api.sent('POST', '/api/models/switch')).toEqual([])
  })

  it('switches once confirmed and leaves the host step on screen', async () => {
    const api = mockApi()
    answerConfirm(true)
    renderInShell(<ModelsPage />)

    await chooseAndSwitch('gemma4-31b')

    await screen.findByRole('heading', { name: 'Finish on the host' })
    expect(screen.getByText('ordo apply --only agent')).toBeTruthy()
    expect(api.sent('POST', '/api/models/switch')).toEqual([
      { method: 'POST', path: '/api/models/switch', body: { model: 'gemma4-31b' } },
    ])
  })
})
