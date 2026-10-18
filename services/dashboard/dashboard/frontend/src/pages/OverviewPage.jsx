// Overview: is it healthy, what is happening, what needs me. One screen, answered in that order.
//   GET /api/overview        status line, GPUs and who holds them, chat engine, attention, host
//   GET /api/drift           what `ordo doctor` would flag (ops-controller's read-only GET /doctor)
//   GET /api/activity        renders, GPU leases and operator actions, newest first
//   GET /api/perf/series     tokens generated per second by each chat server, last 24 h
//   GET /api/models          which model file each role loads now
import { useRef, useState } from 'react'
import { usePolling } from '../api.js'
import { api } from '../api.js'
import { Banner, BTN, Chip, Dot, Median, Meter, Panel, Skeleton, Sparkline, StatTile, Unavailable } from '../components/ui.jsx'
import { clock, formatDuration, formatGb, pct, timeAgo } from '../lib/format.js'
import { ACTIVITY_CATEGORIES, activityDaySummary, filterActivity, groupActivity, kpiTiles } from './overviewModel.js'

const LEVEL_TONE = { ok: 'ok', warning: 'warning', critical: 'critical', unknown: 'unknown' }

// The backend's summary counts what needs attention (routes_console._status_line); drift comes
// from its own slower endpoint, so it is counted here. Drift alone is a warning; a control plane
// that is not answering stays "unknown" (its drift cannot be read then anyway). When the host-wide
// container list could not be read, the rebuilt count keeps the gap note, or it would quietly
// cover only what was still read.
function withDrift(status, attentionCount, driftCount, hostContainersDown) {
  if (!driftCount || status.level === 'unknown') return status
  const n = attentionCount + driftCount
  const gap = hostContainersDown ? ' · host containers unread' : ''
  return {
    level: status.level === 'critical' ? 'critical' : 'warning',
    text: `${n} thing${n !== 1 ? 's' : ''} need${n === 1 ? 's' : ''} you${gap}`,
  }
}

function StatusLine({ overview, driftCount, lastUpdated }) {
  const { services } = overview
  const status = withDrift(overview.status, overview.attention.length, driftCount,
                           overview.host_containers === 'down')
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <span className="flex items-center gap-2.5 text-title text-fg">
        <Dot tone={LEVEL_TONE[status.level]} />
        {status.text}
      </span>
      <span className="text-caption text-muted">
        {services.up} of {services.total} containers up
        {lastUpdated ? ` · refreshed ${Math.max(0, Math.round((Date.now() - lastUpdated) / 1000))} s ago` : ''}
      </span>
    </div>
  )
}

const CODE = 'font-mono text-caption text-fg'

// A doctor report line, with its `command` spans set as code.
function DoctorLine({ text }) {
  return text.split('`').map((part, i) => (i % 2 ? <code key={i} className={CODE}>{part}</code> : part))
}

// Shown only when a check `ordo doctor` runs has something to fix; nothing when all pass or the
// check could not run (the status line already says when the control plane is not answering).
function Drift({ drift }) {
  if (!drift?.available || !drift.findings.length) return null
  return (
    <Banner id="overview-drift" tone="warning" title="Drift">
      <ul className="mt-1 grid gap-1.5">
        {drift.findings.map((f) => (
          <li key={f.check} className="grid grid-cols-[auto_1fr] items-baseline gap-x-2.5">
            <Dot tone="warning" label="warning" />
            <span className="min-w-0 break-words text-body text-fg-muted"><DoctorLine text={f.detail} /></span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-caption text-muted">
        From the checks <code className={CODE}>ordo doctor</code> runs. Run it on the host for the full report.
      </p>
    </Banner>
  )
}

function GpuCard({ gpu }) {
  const used = pct(gpu.vram_used_gb, gpu.vram_total_gb)
  const lent = gpu.borrowed_by.length > 0
  return (
    <div className="grid gap-2 border-b border-border-subtle pb-3 last:border-b-0 last:pb-0">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-heading text-fg">{gpu.name}</span>
        <span className="font-mono text-caption tabular-nums text-muted">
          {formatGb(gpu.vram_used_gb)} / {formatGb(gpu.vram_total_gb)} GB
          {gpu.temp_c != null ? ` · ${gpu.temp_c} °C` : ''}
          {gpu.util_pct != null
            ? <span title="VRAM is held by the resident model; compute % is the live signal"> · {gpu.util_pct}% busy</span>
            : ''}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {lent
          ? gpu.borrowed_by.map((b) => <Chip key={b} tone="accent">Lent to {b}</Chip>)
          : gpu.tenants.map((t) => <Chip key={t}>{t}</Chip>)}
      </div>
      <Meter value={used} tone={used >= 95 ? 'warning' : 'accent'} label={`${gpu.name} memory used`} />
    </div>
  )
}

// Both chat servers share one time axis so their histories line up.
/** @returns {[number, number] | undefined} */
function seriesDomain(series) {
  const stamps = [...(series?.gpu || []), ...(series?.cpu || [])].map((p) => p[0])
  return stamps.length ? [Math.min(...stamps), Math.max(...stamps)] : undefined
}

// The median's evidence as a tooltip: how many samples, over what span.
function medianTitle(m) {
  if (!m.n) return 'No samples yet'
  const span = m.first_ts && m.last_ts ? `, ${clock(m.first_ts)} to ${clock(m.last_ts)}` : ''
  return `${m.n} sample${m.n !== 1 ? 's' : ''}${span}`
}

// The analyst's panel: how the models are doing and being used. Speed by role with its history,
// the combined token trend, the per-caller table when live work tracking lands (gap G4), and the
// files each role loads now.
/** @param {{ chat: object, series: object | null, models: object | null, modelsError: boolean }} props */
function ModelUsage({ chat, series, models, modelsError }) {
  const domain = seriesDomain(series)
  return (
    <div className="grid gap-4">
      <div className="grid grid-cols-2 gap-4 [&>*]:min-w-0">
        {[
          { key: 'gpu', label: 'GPU', tone: 'accent', points: series?.gpu },
          { key: 'cpu', label: 'CPU fallback', tone: 'warning', points: series?.cpu },
        ].map((s) => {
          // The median comes from the ROLE series (a model swap does not reset it); the label
          // names the file the server loads now.
          const m = chat[s.key] || {}
          return (
            <div key={s.key} className="grid gap-1">
              <span className="text-caption text-muted">{s.label} · median speed</span>
              <Median value={m.p50} n={m.n} provisional={m.provisional} title={medianTitle(m)} />
              {series?.available === false
                ? <span className="text-caption text-muted">History needs the monitoring profile</span>
                : <Sparkline points={s.points} tone={s.tone} domain={domain}
                             label={`${s.label} tokens generated, last 24 hours${m.model ? `, ${m.model}` : ''}`} />}
              <span className="truncate text-micro text-muted"
                    title={m.model ? `${m.model}, output over the last 24 h` : undefined}>
                {m.model || 'Output over the last 24 h'}
              </span>
            </div>
          )
        })}
      </div>
      <div className="grid gap-1">
        <span className="text-caption text-muted">Tokens per hour, last 24 h</span>
        {series?.available === false
          ? <span className="text-caption text-muted">History needs the monitoring profile</span>
          : <Sparkline points={series?.gpu || []} height={48} domain={domain}
                       compare={{ points: series?.cpu || [], tone: 'warning', label: 'CPU fallback' }}
                       label="GPU and CPU fallback tokens generated per second, last 24 hours" />}
        <span className="flex gap-3 text-micro text-muted">
          <span className="inline-flex items-center gap-1"><Dot tone="info" /> GPU</span>
          <span className="inline-flex items-center gap-1"><Dot tone="warning" /> CPU fallback</span>
        </span>
      </div>
      <Unavailable>Per-caller usage arrives with live work tracking.</Unavailable>
      <ActiveModels models={models} modelsError={modelsError} />
    </div>
  )
}

// Which file each role loads now, from /api/models: the "what is running" line.
/** @param {{ models: object | null, modelsError: boolean }} props */
function ActiveModels({ models, modelsError }) {
  if (!models) {
    return modelsError
      ? <p className="text-caption text-muted">Active models unavailable.</p>
      : <Skeleton className="h-4 w-40" />
  }
  const parts = []
  if (models.gpu?.catalog_id) {
    parts.push(models.gpu.ctx ? `${models.gpu.catalog_id} · ${Math.round(models.gpu.ctx / 1000)}k ctx` : models.gpu.catalog_id)
  }
  if (models.cpu?.file) parts.push(`CPU ${models.cpu.file}`)
  if (models.embed?.file) parts.push(`embeddings ${models.embed.file}`)
  if (!parts.length) return <p className="text-caption text-muted">Active models unavailable.</p>
  return (
    <p className="truncate text-caption text-fg-muted" title={parts.join(' · ')}>
      <span className="text-muted">Active:</span> {parts.join(' · ')}
    </p>
  )
}

// The attention thresholds the backend applies to these meters (console._RAM_WARN_PCT,
// _DISK_WARN_PCT): until /api/overview exposes them (gap G6) the page keeps the same numbers.
const HOST_WARN = { ram: 90, disk: 80 }

// Host CPU, memory and disk as labelled meters; a meter whose value is missing is absent rather
// than drawn at zero, and the detail line carries the absolute figures.
/** @param {{ host?: { cpu_pct?: number, ram_pct?: number, ram_used_gb?: number, ram_total_gb?: number, disk_pct?: number, disk_used_gb?: number, disk_total_gb?: number } }} props */
function HostMeters({ host }) {
  const meters = [
    { label: 'CPU', pct: host?.cpu_pct, warnAt: null, detail: null },
    { label: 'Memory', pct: host?.ram_pct, warnAt: HOST_WARN.ram,
      detail: host?.ram_used_gb != null && host?.ram_total_gb != null
        ? `${formatGb(host.ram_used_gb)} / ${formatGb(host.ram_total_gb)} GB` : null },
    { label: 'Disk', pct: host?.disk_pct, warnAt: HOST_WARN.disk,
      detail: host?.disk_used_gb != null && host?.disk_total_gb != null
        ? `${formatGb(host.disk_used_gb)} / ${formatGb(host.disk_total_gb)} GB` : null },
  ]
  const shown = meters.filter((m) => typeof m.pct === 'number')
  if (!shown.length) return <p className="text-caption text-muted">Host metrics unavailable.</p>
  return (
    <div className="grid gap-x-6 gap-y-3 sm:grid-cols-3">
      {shown.map((m) => (
        <div key={m.label} className="grid gap-1">
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-caption text-muted">{m.label}</span>
            <span className="font-mono text-caption tabular-nums text-fg">{Math.round(m.pct)}%</span>
          </div>
          <Meter value={m.pct} tone={m.warnAt != null && m.pct >= m.warnAt ? 'warning' : 'accent'} label={`${m.label} used`} />
          {m.detail && <span className="truncate text-micro text-muted" title={m.detail}>{m.detail}</span>}
        </div>
      ))}
    </div>
  )
}

// The operator's panel: what hardware is available and who holds it. GPU cards with their VRAM
// meters and tenants, the host meters, the knowledge index size.
/** @param {{ gpus: Array<object>, host: object, knowledge: object }} props */
function Capacity({ gpus, host, knowledge }) {
  return (
    <div className="grid gap-4">
      <div className="grid gap-3">
        {gpus.length ? gpus.map((g) => <GpuCard key={g.uuid || g.name} gpu={g} />)
          : <Unavailable>No GPUs reported.</Unavailable>}
      </div>
      <HostMeters host={host} />
      <p className="text-caption text-muted">
        Knowledge{' '}
        {knowledge.documents != null
          ? <><span className="font-mono tabular-nums text-fg">{knowledge.documents.toLocaleString()}</span> documents indexed</>
          : 'unavailable'}
      </p>
    </div>
  )
}

const SEVERITY_TONE = { critical: 'critical', warning: 'warning' }

// Promoted to full width directly under the status line: what needs a person, most severe first.
// Rendered only when non-empty; when nothing needs attention the status line already says so.
// A container item links to #services, which applies no project filter yet; the filter arrives
// with the Services filters slice.
function AttentionStrip({ items }) {
  if (!items.length) return null
  return (
    <section aria-labelledby="attention-heading" className="grid gap-2 rounded-md border border-border-subtle bg-bg-elevated p-4">
      <h2 id="attention-heading" className="sr-only">Needs attention</h2>
      {items.map((a) => (
        <div key={a.title} className="grid grid-cols-[auto_1fr_auto] items-baseline gap-x-2.5">
          <Dot tone={SEVERITY_TONE[a.severity] || 'warning'} label={a.severity} />
          <span className="min-w-0 break-words text-body text-fg">
            {a.title}
            {a.detail && <span className="ml-2 text-caption text-muted">{a.detail}</span>}
          </span>
          {a.service && (
            <a href="#services" className="shrink-0 text-caption font-semibold text-accent-soft hover:text-accent">View in Services</a>
          )}
        </div>
      ))}
    </section>
  )
}

// The six-tile band: one row on desktop, two columns on a phone. Tiles whose source is missing
// are simply absent (overviewModel omits them), so the band never shows placeholder zeros.
function KpiBand({ tiles }) {
  if (!tiles.length) return null
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      {tiles.map((t) => (
        <StatTile key={t.key} label={t.label} unit={t.unit} sub={t.sub} tone={t.tone} title={t.title} sparkline={t.sparkline}
                  value={t.median
                    ? <Median value={t.median.value} n={t.median.n} provisional={t.median.provisional} unit="tok/s" title={t.median.title} />
                    : t.value} />
      ))}
    </div>
  )
}

const FEED_TONE = { ok: 'ok', critical: 'critical', warning: 'warning', info: 'info' }
const ACTIVITY_FIRST_LIMIT = 25
const CATEGORY_LABEL = { all: 'All', render: 'Renders', gpu: 'GPU leases', service: 'Service changes', model: 'Model changes' }

// One row of the grouped feed: the newest member's time and facts, the run's count and span, and
// the lease's duration and eviction note when the row is a GPU lease (a run of leases reads its
// GPU time in all). A single lease's own start to end rides in the tooltip: the row's clock is the
// event's stamp, and the duration says how long the GPU was out.
/** @param {{ g: { ts: number, oldestTs: number, count: number, kind: string, title: string, severity: string, holder?: string | null, started?: number | null, ended?: number | null, duration_s?: number | null, evicted_chat?: boolean } }} props */
function ActivityRow({ g }) {
  const lease = g.kind === 'gpu'
  const duration = lease && g.duration_s != null
    ? `${formatDuration(g.duration_s)}${g.count > 1 ? ' in all' : ''}` : ''
  const leaseSpan = lease && g.count === 1 && g.started != null && g.ended != null
    ? `${clock(g.started)} to ${clock(g.ended)}` : ''
  const runSpan = g.count > 1 ? `${clock(g.oldestTs)} to ${clock(g.ts)}` : ''
  const tooltip = [g.title, g.count > 1 ? `${g.count} times` : '', leaseSpan || runSpan, duration,
    g.evicted_chat ? 'chat model evicted' : ''].filter(Boolean).join(' · ')
  return (
    <li className="grid grid-cols-[3.25rem_auto_minmax(0,1fr)] items-center gap-2.5">
      <time className="font-mono text-caption tabular-nums text-muted" title={timeAgo(g.ts)}>{clock(g.ts)}</time>
      <Dot tone={FEED_TONE[g.severity] || 'info'} />
      <span className="min-w-0 truncate text-body text-fg" title={tooltip}>
        {g.title}
        {g.count > 1 && <span className="text-caption text-muted"> ×{g.count}</span>}
        {duration && <span className="text-caption text-muted"> · {duration}</span>}
        {g.evicted_chat && <span className="text-caption text-warning"> · chat model evicted</span>}
        {runSpan && <span className="text-caption text-muted"> · {runSpan}</span>}
      </span>
    </li>
  )
}

// The grouped feed: consecutive identical events collapsed (overviewModel.groupActivity), the
// kind chips behind it, ten groups until "Show more" widens the request to the endpoint's cap.
// The button also shows while the first answer came back full: groups can collapse 25 items into
// a handful, and the older ones are still on the server.
/** @param {{ items: Array<object>, expanded: boolean, onShowMore: () => void }} props */
function ActivityFeed({ items, expanded, onShowMore }) {
  const [category, setCategory] = useState('all')
  const groups = filterActivity(groupActivity(items), category)
  const visible = expanded ? groups : groups.slice(0, 10)
  const summary = activityDaySummary(items)
  if (!items.length) return <p className="text-body text-muted">No recent activity.</p>
  return (
    <div className="grid gap-3">
      {summary && <p className="text-caption text-muted">{summary}</p>}
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter activity by kind">
        {ACTIVITY_CATEGORIES.map((c) => (
          <button key={c} type="button" className={BTN} aria-pressed={category === c} onClick={() => setCategory(c)}>
            {CATEGORY_LABEL[c]}
          </button>
        ))}
      </div>
      {visible.length ? (
        <ol className="grid gap-1.5">
          {visible.map((g, idx) => <ActivityRow key={`${g.ts}-${idx}`} g={g} />)}
        </ol>
      ) : (
        <p className="text-body text-muted">No {CATEGORY_LABEL[category].toLowerCase()} in the feed.</p>
      )}
      {!expanded && (groups.length > 10 || items.length >= ACTIVITY_FIRST_LIMIT) && (
        <button type="button" className={BTN + ' justify-self-start'} onClick={onShowMore}>Show more</button>
      )}
    </div>
  )
}

export default function OverviewPage() {
  const overview = usePolling(() => api.get('/api/overview'), 5000)
  // The feed starts at the endpoint's default (25 items); "Show more" widens it to the cap (100)
  // and refetches at once. The limit rides in a ref so the poll's fetcher always sends the
  // current one without restarting the interval.
  const activityLimit = useRef(ACTIVITY_FIRST_LIMIT)
  const [activityExpanded, setActivityExpanded] = useState(false)
  const activity = usePolling(() => api.get(`/api/activity?limit=${activityLimit.current}`), 15000)
  const series = usePolling(() => api.get('/api/perf/series'), 60000)
  // Slower than the overview: each call runs the open-webui probe inside its container.
  const drift = usePolling(() => api.get('/api/drift'), 60000)
  const models = usePolling(() => api.get('/api/models'), 60000)
  const o = overview.data
  const driftCount = drift.data?.available ? drift.data.findings.length : 0

  const showMoreActivity = () => {
    activityLimit.current = 100
    setActivityExpanded(true)
    void activity.refresh()
  }

  if (!o) {
    return overview.error
      ? <Unavailable error={overview.error}>The dashboard API is not answering: {overview.error.message}</Unavailable>
      : <div className="grid gap-4"><Skeleton className="h-6 w-72" /><Skeleton className="h-64 w-full" /></div>
  }

  return (
    <div className="grid gap-4 [&>*]:min-w-0">
      <StatusLine overview={o} driftCount={driftCount} lastUpdated={overview.lastUpdated} />
      <AttentionStrip items={o.attention} />
      <Drift drift={drift.data} />
      <KpiBand tiles={kpiTiles(o, series.data)} />
      <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
        <Panel title="Model usage">
          <ModelUsage chat={o.chat} series={series.data} models={models.data} modelsError={!!models.error} />
        </Panel>
        <Panel title="Capacity">
          <Capacity gpus={o.gpus} host={o.host} knowledge={o.knowledge} />
        </Panel>
      </div>
      <Panel title="Activity">
        {activity.data ? <ActivityFeed items={activity.data.items} expanded={activityExpanded} onShowMore={showMoreActivity} />
          : activity.error ? <Unavailable>Activity is unavailable.</Unavailable>
            : <Skeleton className="h-24 w-full" />}
      </Panel>
    </div>
  )
}
