// "Finish on the host" is a command only the operator can run. Unlike a toast it must stay on
// screen until dismissed: through time passing, a reload, and further changes asking for it again.
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { HostStepBanners, HostStepsProvider, useHostSteps } from '../src/components/HostSteps.jsx'

const STEP = { reason: 'The switch to gemma4-31b changed agent, which cannot restart from the dashboard.',
               command: 'ordo apply --only agent' }

function AddStepButton({ step }) {
  const { addHostStep } = useHostSteps()
  return <button type="button" onClick={() => addHostStep(step)}>Add step</button>
}

// One page load: a fresh provider reads whatever the previous load left in storage.
function loadPage() {
  return render(
    <HostStepsProvider>
      <HostStepBanners idPrefix="page-host-step" />
      <AddStepButton step={STEP} />
    </HostStepsProvider>,
  )
}

const banners = () => screen.queryAllByRole('heading', { name: 'Finish on the host' })

afterEach(() => {
  vi.useRealTimers()
})

describe('host-step banner', () => {
  it('shows the command and the reason', () => {
    loadPage()
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    expect(banners()).toHaveLength(1)
    expect(screen.getByText(STEP.command)).toBeTruthy()
    expect(screen.getByText(STEP.reason)).toBeTruthy()
  })

  it('does not time out the way a toast does', () => {
    vi.useFakeTimers()
    loadPage()
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    act(() => { vi.advanceTimersByTime(60 * 60 * 1000) })
    expect(banners()).toHaveLength(1)
  })

  it('survives a reload', () => {
    const first = loadPage()
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    first.unmount()

    loadPage()
    expect(banners()).toHaveLength(1)
    expect(screen.getByText(STEP.command)).toBeTruthy()
  })

  it('shows one banner per command, however many changes ask for it', () => {
    loadPage()
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    expect(banners()).toHaveLength(1)
  })

  it('goes away when dismissed, and stays gone after a reload', () => {
    const first = loadPage()
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    fireEvent.click(screen.getByRole('button', { name: `Dismiss: run ${STEP.command} on the host` }))
    expect(banners()).toHaveLength(0)
    first.unmount()

    loadPage()
    expect(banners()).toHaveLength(0)
  })

  it('adds nothing when the change needs nothing on the host', () => {
    render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="page-host-step" />
        <AddStepButton step={{ reason: 'model-gateway restarted', command: null }} />
      </HostStepsProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    expect(banners()).toHaveLength(0)
  })

  it('still works when browser storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('SecurityError') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('SecurityError') })
    loadPage()
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    expect(banners()).toHaveLength(1)
  })
})
