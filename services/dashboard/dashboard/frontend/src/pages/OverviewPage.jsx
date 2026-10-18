// Overview: is it healthy, what is happening, what needs me. One screen, answered in that order.
//   GET /api/overview        status line, GPUs and who holds them, chat engine, attention, host
//   GET /api/drift           what `ordo doctor` would flag (ops-controller's read-only GET /doctor)
//   GET /api/activity        renders, GPU leases and operator actions, newest first
//   GET /api/perf/series     tokens generated per second by each chat server, last 24 h
import { usePolling } from '../api.js'
import { api } from '../api.js'
import { Banner, Chip, Dot, Median, Meter, Panel, Skeleton, Sparkline, StatTile, Unavailable } from '../components/ui.jsx'
import { clock, formatGb, pct, timeAgo } from '../lib/format.js'
import { kpiTiles } from './overviewModel.js'

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
          {gpu.util_pct != null ? ` · ${gpu.util_pct}% busy` : ''}
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

function ChatEngine({ chat, series }) {
  const onCpu = chat.engine === 'cpu'
  const domain = seriesDomain(series)
  const headline = {
    gpu: 'Chat is on the GPU',
    cpu: 'Chat is on the CPU fallback',
    none: 'No chat server is running',
    unknown: 'Chat engine unknown',
  }[chat.engine]
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Dot tone={chat.engine === 'gpu' ? 'ok' : onCpu ? 'warning' : 'critical'} />
        <span className="text-heading text-fg">{headline}</span>
        {chat.reason && <span className="text-caption text-muted">{chat.reason}</span>}
      </div>
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
    </div>
  )
}

const SEVERITY_TONE = { critical: 'critical', warning: 'warning' }

// Promoted to full width directly under the status line: what needs a person, most severe first.
// Rendered only when non-empty; when nothing needs attention the status line already says so.
// A container item links to Services, where its row carries the project filter.
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

function Activity({ items }) {
  if (!items.length) return <p className="text-body text-muted">No recent activity.</p>
  return (
    <ol className="grid gap-1.5">
      {items.map((i, idx) => (
        <li key={`${i.ts}-${idx}`} className="grid grid-cols-[3.25rem_auto_1fr] items-center gap-2.5">
          <time className="font-mono text-caption tabular-nums text-muted" title={timeAgo(i.ts)}>{clock(i.ts)}</time>
          <Dot tone={FEED_TONE[i.severity] || 'info'} />
          <span className="truncate text-body text-fg">{i.title}</span>
        </li>
      ))}
    </ol>
  )
}

function Host({ host, knowledge }) {
  const disk = host.disk_pct
  /** @type {Array<[label: string, value: string, warn?: boolean]>} */
  const facts = [
    ['CPU', host.cpu_pct != null ? `${Math.round(host.cpu_pct)}%` : '—'],
    ['Memory', host.ram_used_gb != null ? `${formatGb(host.ram_used_gb)} / ${formatGb(host.ram_total_gb)} GB` : '—'],
    ['Disk', host.disk_used_gb != null ? `${formatGb(host.disk_used_gb)} / ${formatGb(host.disk_total_gb)} GB` : '—', disk >= 80],
    ['Knowledge', knowledge.documents != null ? `${knowledge.documents.toLocaleString()} documents indexed` : 'unavailable'],
  ]
  return (
    <dl className="flex flex-wrap gap-x-6 gap-y-1 text-caption">
      {facts.map(([k, v, warn]) => (
        <div key={k} className="flex gap-1.5">
          <dt className="text-muted">{k}</dt>
          <dd className={'font-mono tabular-nums ' + (warn ? 'text-warning' : 'text-fg')}>{v}</dd>
        </div>
      ))}
    </dl>
  )
}

export default function OverviewPage() {
  const overview = usePolling(() => api.get('/api/overview'), 5000)
  const activity = usePolling(() => api.get('/api/activity'), 15000)
  const series = usePolling(() => api.get('/api/perf/series'), 60000)
  // Slower than the overview: each call runs the open-webui probe inside its container.
  const drift = usePolling(() => api.get('/api/drift'), 60000)
  const o = overview.data
  const driftCount = drift.data?.available ? drift.data.findings.length : 0

  if (!o) {
    return overview.error
      ? <Unavailable>The dashboard API is not answering: {overview.error.message}</Unavailable>
      : <div className="grid gap-4"><Skeleton className="h-6 w-72" /><Skeleton className="h-64 w-full" /></div>
  }

  return (
    <div className="grid gap-4 [&>*]:min-w-0">
      <StatusLine overview={o} driftCount={driftCount} lastUpdated={overview.lastUpdated} />
      <AttentionStrip items={o.attention} />
      <Drift drift={drift.data} />
      <KpiBand tiles={kpiTiles(o, series.data)} />
      <div className="grid gap-4 lg:grid-cols-[1.35fr_1fr] [&>*]:min-w-0">
        <Panel title="GPUs right now">
          <div className="grid gap-4">
            <div className="grid gap-3">
              {o.gpus.length ? o.gpus.map((g) => <GpuCard key={g.uuid || g.name} gpu={g} />)
                : <Unavailable>No GPUs reported.</Unavailable>}
            </div>
            <ChatEngine chat={o.chat} series={series.data} />
          </div>
        </Panel>
        <div className="grid content-start gap-4">
          <Panel title="Recent activity">
            {activity.data ? <Activity items={activity.data.items.slice(0, 8)} />
              : activity.error ? <Unavailable>Activity is unavailable.</Unavailable>
                : <Skeleton className="h-24 w-full" />}
          </Panel>
        </div>
      </div>
      <Host host={o.host} knowledge={o.knowledge} />
      {o.links.length > 0 && (
        <nav aria-label="Open a tool" className="flex flex-wrap gap-2">
          {o.links.map((l) => (
            <a key={l.url} href={l.url} target="_blank" rel="noreferrer"
               className="inline-flex h-8 items-center gap-1.5 rounded-sm border border-border px-3 text-label text-fg no-underline transition-colors hover:border-accent/40 hover:text-accent">
              {l.name}<span aria-hidden="true" className="text-muted">↗</span>
            </a>
          ))}
        </nav>
      )}
    </div>
  )
}
