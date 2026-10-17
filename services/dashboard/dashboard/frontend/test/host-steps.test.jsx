// "Finish on the host" is a command only the operator can run. Unlike a toast it must stay on
// screen until dismissed: through time passing, a reload, and further changes asking for it again.
import React from 'react'
import { within } from '@testing-library/dom'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { HostStepBanners, HostStepsProvider, STORAGE_KEY, mergeThreeWay, useHostSteps } from '../src/components/HostSteps.jsx'

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

  it('keeps a step another tab added (merge on write, storage event)', () => {
    // Two provider trees share one localStorage, like two tabs of the same browser. The second
    // tab's write must merge with the first instead of erasing it, and each tab adopts the other's
    // steps through the `storage` event (browsers fire it only in the OTHER tabs, so jsdom tests
    // dispatch it the way a second tab would see it). render() binds its queries to document.body,
    // so per-tree lookups go through within().
    const first = render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="tab-a" />
        <AddStepButton step={{ reason: 'a', command: 'cmd-a' }} />
      </HostStepsProvider>,
    )
    const second = render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="tab-b" />
        <AddStepButton step={{ reason: 'b', command: 'cmd-b' }} />
      </HostStepsProvider>,
    )
    fireEvent.click(within(first.container).getByRole('button', { name: 'Add step' }))
    fireEvent.click(within(second.container).getByRole('button', { name: 'Add step' }))

    // The second tab's write merged with the first instead of replacing it.
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY))
    expect(stored.v).toBe(1)
    expect(stored.steps.map((s) => s.command).sort()).toEqual(['cmd-a', 'cmd-b'])

    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY })) })
    expect(within(first.container).getAllByText('cmd-a')).toHaveLength(1)
    expect(within(first.container).getAllByText('cmd-b')).toHaveLength(1)
    expect(within(second.container).getAllByText('cmd-a')).toHaveLength(1)
    expect(within(second.container).getAllByText('cmd-b')).toHaveLength(1)
  })

  it('propagates a dismiss to the other tab without resurrecting it', () => {
    const first = render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="tab-a" />
        <AddStepButton step={STEP} />
      </HostStepsProvider>,
    )
    const second = render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="tab-b" />
        <AddStepButton step={STEP} />
      </HostStepsProvider>,
    )
    fireEvent.click(within(first.container).getByRole('button', { name: 'Add step' }))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY })) })
    expect(screen.queryAllByRole('heading', { name: 'Finish on the host' })).toHaveLength(2)

    fireEvent.click(within(first.container).getByRole('button', { name: `Dismiss: run ${STEP.command} on the host` }))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY })) })
    // The other tab adopted the dismiss through the storage event; nothing resurrects.
    expect(within(second.container).queryAllByRole('heading', { name: 'Finish on the host' })).toHaveLength(0)
    expect(screen.queryAllByRole('heading', { name: 'Finish on the host' })).toHaveLength(0)
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY))
    expect(stored.steps).toEqual([])
  })

  it('adopts pending steps from the pre-versioning key on first load', () => {
    // Older builds stored a bare array under `ordo.hostSteps`; the upgrade must not clear the
    // queue, and the old key is retired once adopted. Invalid entries degrade as before.
    localStorage.setItem('ordo.hostSteps', JSON.stringify([
      { id: '1760000000000-legacy', reason: 'The switch changed agent.', command: STEP.command },
      { command: 'no-id' },
    ]))
    loadPage()
    expect(banners()).toHaveLength(1)
    expect(screen.getByText(STEP.command)).toBeTruthy()
    expect(localStorage.getItem('ordo.hostSteps')).toBeNull()
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY))
    expect(stored.v).toBe(1)
    expect(stored.steps.map((s) => s.command)).toEqual([STEP.command])
  })

  it('keeps a pending legacy step when React double-invokes the initializers (StrictMode)', () => {
    // StrictMode calls the state initializer twice; the legacy-key migration used to retire the
    // old key on the first call, so the second call saw an empty store and the step vanished.
    // The read is pure now and the old key retires on the first write instead.
    localStorage.setItem('ordo.hostSteps', JSON.stringify([{ id: '1760000000000-s', command: 'cmd-s' }]))
    render(
      <React.StrictMode>
        <HostStepsProvider>
          <HostStepBanners idPrefix="page-host-step" />
        </HostStepsProvider>
      </React.StrictMode>,
    )
    expect(banners()).toHaveLength(1)
    expect(localStorage.getItem('ordo.hostSteps')).toBeNull()
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY))
    expect(stored.steps.map((s) => s.command)).toEqual(['cmd-s'])
  })

  it('does not resurrect a step another tab dismissed while this tab was mid-write', () => {
    // Tab A dismisses; before tab B processes the storage event, B adds its own step. B's write
    // must apply what changed since B last synced against fresh storage, not replay B's whole
    // in-memory list - which still holds the dismissed step - or the step comes back.
    const first = render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="tab-a" />
        <AddStepButton step={STEP} />
      </HostStepsProvider>,
    )
    const second = render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="tab-b" />
        <AddStepButton step={{ reason: 'b', command: 'cmd-b' }} />
      </HostStepsProvider>,
    )
    fireEvent.click(within(first.container).getByRole('button', { name: 'Add step' }))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY })) })
    expect(screen.queryAllByRole('heading', { name: 'Finish on the host' })).toHaveLength(2)

    fireEvent.click(within(first.container).getByRole('button', { name: `Dismiss: run ${STEP.command} on the host` }))
    fireEvent.click(within(second.container).getByRole('button', { name: 'Add step' }))

    // B's write applied what changed since B last synced against fresh storage: the dismissed
    // step stays out of the stored queue and B's own step lands. The write also converged B's
    // view onto the merged result, so the dismissed step is gone from B's banners too - a second
    // write before the event arrives cannot replay it.
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY))
    expect(stored.steps.map((s) => s.command)).toEqual(['cmd-b'])
    expect(within(second.container).queryAllByRole('heading', { name: 'Finish on the host' })).toHaveLength(1)

    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY })) })
    expect(within(first.container).queryAllByRole('heading', { name: 'Finish on the host' })).toHaveLength(1)
    expect(within(second.container).queryAllByRole('heading', { name: 'Finish on the host' })).toHaveLength(1)
    expect(within(first.container).getByText('cmd-b')).toBeTruthy()
    expect(within(second.container).getByText('cmd-b')).toBeTruthy()
  })
  it('keeps a pending step when site data is cleared while the tab is open', () => {
    // Clearing site data removes the key; the open tab still shows the banner. The removal is no
    // information, so the tab keeps its step and its next write puts it back in storage.
    render(
      <HostStepsProvider>
        <HostStepBanners idPrefix="page-host-step" />
        <AddStepButton step={{ reason: 'a', command: 'cmd-a' }} />
        <AddStepButton step={{ reason: 'b', command: 'cmd-b' }} />
      </HostStepsProvider>,
    )
    const [addA, addB] = screen.getAllByRole('button', { name: 'Add step' })
    fireEvent.click(addA)
    localStorage.removeItem(STORAGE_KEY)
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY })) })
    expect(banners()).toHaveLength(1)

    fireEvent.click(addB)
    expect(banners()).toHaveLength(2)
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY))
    expect(stored.steps.map((s) => s.command)).toEqual(['cmd-a', 'cmd-b'])
  })
})

describe('mergeThreeWay', () => {
  it('keeps every step this tab holds when the store is missing (site data cleared)', () => {
    // The .v1 key is gone: storage says nothing about what was dismissed, so nothing is. This
    // holds whether or not the tab changed since its last sync.
    const x = { id: '1760000000000-x', command: 'cmd-x' }
    const y = { id: '1760000000001-y', command: 'cmd-y' }
    expect(mergeThreeWay([x], [x], null).map((s) => s.command)).toEqual(['cmd-x'])
    expect(mergeThreeWay([x], [x, y], null).map((s) => s.command)).toEqual(['cmd-x', 'cmd-y'])
  })

  it('reads an empty stored queue as a dismissal of the last step', () => {
    // Another tab dismissed the only step: the key holds an empty queue. An untouched copy this
    // tab still holds drops, with or without a step added here since the last sync.
    const x = { id: '1760000000000-x', command: 'cmd-x' }
    const y = { id: '1760000000001-y', command: 'cmd-y' }
    expect(mergeThreeWay([x], [x], [])).toEqual([])
    expect(mergeThreeWay([x], [x, y], []).map((s) => s.command)).toEqual(['cmd-y'])
  })

  it('keeps the newer copy when both tabs added the same command', () => {
    const older = { id: '1760000000000-c', command: 'cmd-c' }
    const newer = { id: '1760000000001-c', command: 'cmd-c' }
    expect(mergeThreeWay([], [older], [newer]).map((s) => s.id)).toEqual([newer.id])
    expect(mergeThreeWay([], [newer], [older]).map((s) => s.id)).toEqual([newer.id])
  })

  it('keeps a re-added step even though this tab dismissed the older copy', () => {
    // This tab dismissed cmd-c (base holds the old copy, mine does not); the other tab then
    // asked for the same command again - a fresh request with a newer id. The stale dismiss must
    // not swallow it.
    const old = { id: '1760000000000-c', command: 'cmd-c' }
    const fresh = { id: '1760000000001-c', command: 'cmd-c' }
    expect(mergeThreeWay([old], [], [fresh]).map((s) => s.id)).toEqual([fresh.id])
  })

  it('lets a dismissal of the exact stored copy win', () => {
    // Same id on both sides: this is the copy we dismissed, not a fresh request.
    const c = { id: '1760000000000-c', command: 'cmd-c' }
    expect(mergeThreeWay([c], [], [c])).toEqual([])
  })
})
