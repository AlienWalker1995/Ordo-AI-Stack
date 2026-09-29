// Host steps: a command the operator must run on the host because the control plane could not
// finish a change itself (a model switch or an MCP toggle whose render changed a service only the
// host can restart; the backend names it as `host_command`). A toast would vanish after 5 s, so
// each step is a banner that stays until dismissed. Steps are kept in this browser's storage so a
// reload does not lose them; storage is best effort and the banners work without it.
import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { Banner, BTN } from './ui.jsx'

const STORAGE_KEY = 'ordo.hostSteps'

const HostStepsContext = createContext({ steps: [], addHostStep: () => {}, dismissHostStep: () => {} })

function readStoredSteps() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    return Array.isArray(parsed) ? parsed.filter((s) => s && typeof s.command === 'string') : []
  } catch {
    return []
  }
}

function writeStoredSteps(steps) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(steps))
  } catch {
    // Storage blocked (private window, previews): the banners still show for this visit.
  }
}

export function useHostSteps() {
  return useContext(HostStepsContext)
}

export function HostStepsProvider({ children }) {
  const [steps, setSteps] = useState(readStoredSteps)

  useEffect(() => { writeStoredSteps(steps) }, [steps])

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

// Every pending host step, newest first. Rendered above the page and inside Settings, so the
// step is visible where the change was made and on every page after.
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
