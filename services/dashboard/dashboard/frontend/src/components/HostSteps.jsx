// Host steps: a command the operator must run on the host because the control plane could not
// finish a change itself (a model switch or an MCP toggle whose render changed a service only the
// host can restart; the backend names it as `host_command`). A toast would vanish after 5 s, so
// each step is a banner that stays until dismissed. Steps are kept in this browser's storage so a
// reload does not lose them; storage is best effort and the banners work without it.
//
// Two tabs share one storage key, so a write never replaces what another tab stored: it merges
// against the storage snapshot this tab last wrote or adopted (the base), keeping a step the
// other tab added since then and dropping one we deliberately dismissed, including one we still
// hold in memory while waiting for the other tab's `storage` event. A write also converges this
// tab's view onto the merged result, so a step the other tab added lands and one it dismissed
// stops being replayed without waiting for the event. Every tab adopts foreign writes through
// the `storage` event. A missing store (site data cleared) carries no information, so it never
// dismisses anything: the next write restores this tab's steps. A dismissal always leaves the
// key in place, holding the rest of the queue (possibly none). The stored shape is versioned
// ({ v: 1, steps: [...] }) and validated on read, so hand-edited JSON degrades to its valid
// entries instead of crashing the banners; a pending step left under the pre-versioning key is
// adopted on first load and the old key retired on the first write.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { Banner, BTN } from './ui.jsx'

export const STORAGE_KEY = 'ordo.hostSteps.v1'
// Pre-versioning builds stored a bare array here; adopted on first load and retired by the first
// successful write so a pending step survives the dashboard update.
const LEGACY_STORAGE_KEY = 'ordo.hostSteps'
const STORE_VERSION = 1

/** @typedef {{ id: string, reason?: string, command: string }} HostStep */
/**
 * @typedef {object} HostStepsValue
 * @property {HostStep[]} steps
 * @property {(step: { reason?: string, command?: string | null }) => void} addHostStep
 * @property {(id: string) => void} dismissHostStep
 */

/** @type {import('react').Context<HostStepsValue>} */
const HostStepsContext = createContext(/** @type {HostStepsValue} */ ({ steps: [], addHostStep: () => {}, dismissHostStep: () => {} }))

// Parse and validate a stored value; throws on malformed JSON so callers can tell "blocked or
// corrupt" from "empty". The versioned shape is { v: 1, steps: [...] }; a bare array is what the
// unversioned key used to hold (or what someone hand-edited into the new key). Anything else is
// not trusted at all.
function parseStored(raw) {
  const parsed = JSON.parse(raw)
  let list
  if (Array.isArray(parsed)) list = parsed
  else if (parsed && typeof parsed === 'object' && parsed.v === STORE_VERSION && Array.isArray(parsed.steps)) list = parsed.steps
  else return []
  return list.filter((s) => s && typeof s.command === 'string' && typeof s.id === 'string')
}

// The raw stored value: the versioned key, or - until the first write after the key moved -
// whatever the unversioned key still held. Pure on purpose: React may call the state initializer
// twice under StrictMode, so the read must not retire the legacy key; writeStoredSteps retires it
// after the new key holds the merged list.
function readRaw() {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (raw) return raw
  return localStorage.getItem(LEGACY_STORAGE_KEY)
}

// The stored steps, or null when nothing is stored under either key (a fresh browser or cleared
// site data): "no information", which is not the same as an empty queue.
function readStoredSteps() {
  try {
    const raw = readRaw()
    return raw ? parseStored(raw) : null
  } catch {
    return []
  }
}

// The step's creation time: ids are `<epoch ms>-<command>`, so the numeric prefix orders two
// copies of the same command written by different tabs.
function stepTime(step) {
  const m = /^(\d+)/.exec(String(step.id || ''))
  return m ? Number(m[1]) : 0
}

/**
 * Three-way merge by command. base is the storage snapshot this tab last wrote or adopted, mine
 * is this tab's current list, theirs is what storage holds now, or null when storage holds
 * nothing at all (the key is missing: site data was cleared). A missing store says nothing about
 * what was dismissed, so mine is kept as is. A command both sides carry resolves to the newer
 * copy. A command only storage carries is kept unless it is the exact copy (same id) this tab
 * had in base and dropped since - a deliberate dismiss; a different id is a fresh request for
 * the same command from the other tab and wins over the stale dismiss. A command only we carry
 * is kept unless we have not touched it since base and storage no longer has it: another tab
 * dismissed it while this tab still held it in memory, and the dismissal wins. That holds for an
 * empty stored queue too: dismissing the last step writes an empty queue under the key, which is
 * a dismissal, unlike a missing key.
 */
export function mergeThreeWay(base, mine, theirs) {
  const index = (list) => {
    const map = new Map()
    for (const s of list) map.set(s.command, s)
    return map
  }
  if (theirs === null) return mine
  const baseMap = index(base)
  const mineMap = index(mine)
  const theirsMap = index(theirs)
  const out = []
  for (const command of new Set([...baseMap.keys(), ...mineMap.keys(), ...theirsMap.keys()])) {
    const b = baseMap.get(command)
    const m = mineMap.get(command)
    const t = theirsMap.get(command)
    if (m && t) {
      out.push(stepTime(t) > stepTime(m) ? t : m)
    } else if (m) {
      if (b && m.id === b.id) continue
      out.push(m)
    } else if (t) {
      if (b && t.id === b.id) continue
      out.push(t)
    }
  }
  return out
}

function writeStoredSteps(base, steps) {
  try {
    const raw = readRaw()
    // An unreadable value (a torn write, a hand edit) carries no information, like a missing key:
    // keep this tab's list and let the setItem below overwrite it. Letting the parse error reach
    // the catch would skip the setItem and leave the corrupt value to fail every later write.
    let theirs = null
    if (raw) {
      try {
        theirs = parseStored(raw)
      } catch {
        theirs = null
      }
    }
    const merged = mergeThreeWay(base, steps, theirs)
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ v: STORE_VERSION, steps: merged }))
    // The pending step migrated from the pre-versioning key on the first read; retire the old key
    // here, after the new key holds the merged list, so a failed write drops nothing.
    localStorage.removeItem(LEGACY_STORAGE_KEY)
    return merged
  } catch {
    // Storage blocked (private window, previews): the banners still show for this visit.
    return steps
  }
}

export function useHostSteps() {
  return useContext(HostStepsContext)
}

export function HostStepsProvider({ children }) {
  const [steps, setSteps] = useState(() => readStoredSteps() ?? [])
  // The storage snapshot this tab last wrote or adopted: the base for the three-way merge, so a
  // write applies only what changed here since then and cannot resurrect a step another tab
  // dismissed in the meantime. Initialized from the state above rather than a second storage
  // read: the initializer already ran once (twice under StrictMode), and the read is pure.
  const base = useRef(steps)

  useEffect(() => {
    const merged = writeStoredSteps(base.current, steps)
    base.current = merged
    // Converge the view onto what the write stored: a step the other tab added lands here
    // immediately, and one it dismissed stops being replayed by our next write. The JSON guard
    // keeps the common no-change case from re-rendering; a changed view re-runs this effect once
    // more, where the merge is the identity and it settles.
    setSteps((current) => (JSON.stringify(merged) === JSON.stringify(current) ? current : merged))
  }, [steps])

  // Another tab wrote: adopt its view. Browsers fire `storage` only in the OTHER tabs, never the
  // one that wrote, so adopting cannot loop; adopting unchanged content skips the re-write.
  useEffect(() => {
    const onStorage = (e) => {
      // Only our key can change what we read; `null` is a clear or removal. Other keys -
      // including the retired legacy key - are skipped.
      if (e.key !== null && e.key !== STORAGE_KEY) return
      const next = readStoredSteps()
      // The key was removed (site data cleared): no information, so keep this tab's steps; the
      // next write restores them.
      if (next === null) return
      base.current = next
      setSteps((current) => (JSON.stringify(next) === JSON.stringify(current) ? current : next))
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // One banner per command: running it once covers every change that asked for it, so a newer
  // step with the same command replaces the older one instead of stacking a duplicate.
  const addHostStep = useCallback(({ reason, command }) => {
    if (!command) return
    setSteps((current) => [
      ...current.filter((s) => s.command !== command),
      { id: `${Date.now()}-${command}`, reason, command },
    ])
  }, [])

  const dismissHostStep = useCallback((id) => {
    setSteps((current) => current.filter((s) => s.id !== id))
  }, [])

  return (
    <HostStepsContext.Provider value={{ steps, addHostStep, dismissHostStep }}>
      {children}
    </HostStepsContext.Provider>
  )
}

// Every pending host step, newest first. Rendered above the page, so the step stays visible on
// every page after the change that asked for it.
export function HostStepBanners({ idPrefix }) {
  const { steps, dismissHostStep } = useHostSteps()
  if (!steps.length) return null
  return (
    <div className="grid gap-3" role="status">
      {[...steps].reverse().map((step) => (
        <Banner key={step.id} id={`${idPrefix}-${step.id}`} tone="warning" title="Finish on the host"
                action={(
                  <button type="button" className={BTN} onClick={() => dismissHostStep(step.id)}
                          aria-label={`Dismiss: run ${step.command} on the host`}>
                    Dismiss
                  </button>
                )}>
          {step.reason && <p className="mt-1 text-body text-fg-muted">{step.reason}</p>}
          <p className="mt-2 text-caption text-muted">Run this from the repo root on the host:</p>
          <code className="mt-1 block select-all break-all rounded-sm border border-border-subtle bg-bg px-3 py-2 font-mono text-caption text-fg">
            {step.command}
          </code>
        </Banner>
      ))}
    </div>
  )
}
