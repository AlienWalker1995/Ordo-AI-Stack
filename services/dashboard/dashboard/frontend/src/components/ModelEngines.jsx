// The model engines the registry lists, one row each: what it serves, on what device, whether it
// is serving and, where it is measured, how fast it works with its last 24 h of output. The
// Overview shows them in full; the Models page shows them `compact` (no figures or history, which
// the Overview carries), beside the model switch and the files on disk. Nothing here names an
// engine: everything comes from the overview's or /api/models's `engines`.
import { Chip, Dot, Sparkline, Unavailable } from './ui.jsx'
import { deviceLabel } from '../pages/overviewModel.js'

// Every engine's history shares one time axis so the rows line up.
/** @param {Record<string, [number, number][]> | undefined} series @returns {[number, number] | undefined} */
function seriesDomain(series) {
  const stamps = Object.values(series || {}).flat().map((p) => p[0])
  return stamps.length ? [Math.min(...stamps), Math.max(...stamps)] : undefined
}

// What an engine is doing, as a dot and a word. `lent` is the system working (a render borrowed
// its GPU), so it reads as a warning, never as a failure.
const ENGINE_STATE = {
  serving: { tone: 'ok', label: 'Serving' },
  starting: { tone: 'starting', label: 'Starting' },
  lent: { tone: 'warning', label: 'GPU lent' },
  stopped: { tone: 'stopped', label: 'Stopped' },
  down: { tone: 'critical', label: 'Down' },
}

/** @param {number | null | undefined} v */
function rate(v) {
  return v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1)
}

// One figure beside an engine's history: the number first, its meaning muted after it.
/** @param {{ value: string, label: string, title: string }} props */
function Figure({ value, label, title }) {
  return (
    <span className="whitespace-nowrap" title={title}>
      <span className="font-mono tabular-nums text-fg">{value}</span>{' '}
      <span className="text-muted">{label}</span>
    </span>
  )
}

// The figures ops-controller measures for an engine (ordo_engine_*). Speeds are tokens over the
// time spent on them, so a queue never reads as a slow model; a figure with no samples is absent.
/** @param {{ speeds: Record<string, number> }} props */
function EngineFigures({ speeds }) {
  const figures = []
  if (typeof speeds.generation_tps === 'number') {
    figures.push(<Figure key="gen" value={rate(speeds.generation_tps)} label="tok/s generating"
                         title="Tokens per second while generating, last hour" />)
  }
  if (typeof speeds.prompt_tps === 'number') {
    figures.push(<Figure key="prompt" value={rate(speeds.prompt_tps)} label="tok/s prompt"
                         title="Prompt tokens computed per second, cache hits excluded, last hour" />)
  }
  if (typeof speeds.cache_reuse === 'number') {
    figures.push(<Figure key="cache" value={`${Math.round(speeds.cache_reuse * 100)}%`} label="cached"
                         title="Share of prompt tokens reused from the prefix cache, last hour" />)
  }
  if (speeds.waiting) {
    figures.push(<Figure key="wait" value={String(speeds.waiting)} label="waiting"
                         title="Requests queued for this engine now" />)
  }
  if (!figures.length) return <span className="text-caption text-muted">No samples in the last hour</span>
  return <span className="flex flex-wrap gap-x-3 gap-y-1 text-caption">{figures}</span>
}

// One row per model engine the registry lists: what it serves, on what device, whether it is
// serving, and (where it is measured) how fast it works and its output over the last 24 h.
/** @param {{ engines: Array<object>, series?: object | null, ratesAvailable?: boolean, compact?: boolean }} props */
export default function ModelEngines({ engines, series, ratesAvailable = true, compact = false }) {
  if (!engines.length) return <Unavailable>No model engines reported.</Unavailable>
  const domain = seriesDomain(series?.series)
  return (
    <ul className="grid divide-y divide-border-subtle">
      {engines.map((e) => {
        const state = ENGINE_STATE[e.state] || ENGINE_STATE.stopped
        const facts = [e.file, e.ctx ? `${Math.round(e.ctx / 1000)}k ctx` : ''].filter(Boolean).join(' · ')
        return (
          <li key={e.model} className="grid min-w-0 gap-2 py-3 first:pt-0 last:pb-0">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <Dot tone={state.tone} label={state.label} />
              <span className="truncate font-mono text-body text-fg" title={e.engine}>{e.model}</span>
              <Chip>{deviceLabel(e.device)}</Chip>
              <Chip>{e.kind}</Chip>
              {e.fallback_for && (
                <Chip tone={e.standing_in ? 'warning' : 'neutral'}
                      title={`Answers for ${e.fallback_for} while it is not serving`}>
                  {e.standing_in ? `Standing in for ${e.fallback_for}` : `Fallback for ${e.fallback_for}`}
                </Chip>
              )}
              <span className="ml-auto text-caption text-muted" title={e.reason || undefined}>
                {state.label}{e.reason ? ` · ${e.reason}` : ''}
              </span>
            </div>
            {e.measured && !compact && (
              ratesAvailable
                ? (
                  <div className="grid gap-1">
                    <EngineFigures speeds={e.speeds || {}} />
                    {series?.available === false
                      ? <span className="text-caption text-muted">History needs the monitoring profile</span>
                      : <Sparkline points={series?.series?.[e.engine] || []} domain={domain}
                                   tone={e.standing_in ? 'warning' : 'accent'}
                                   label={`${e.model} tokens generated per second, last 24 hours`} />}
                  </div>
                )
                : <span className="text-caption text-muted">Speeds need the monitoring profile</span>
            )}
            {facts && <span className="truncate text-micro text-muted" title={facts}>{facts}</span>}
          </li>
        )
      })}
    </ul>
  )
}
