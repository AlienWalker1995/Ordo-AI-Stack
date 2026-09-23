// Overview: is it healthy, what is happening, what needs me. One screen, answered in that order.
//   GET /api/overview        status line, GPUs and who holds them, chat engine, attention, host
//   GET /api/activity        renders, GPU leases and operator actions, newest first
//   GET /api/perf/series     tokens generated per second by each chat server, last 24 h
import { usePolling } from '../api.js'
import { api } from '../api.js'
import { Chip, Dot, Meter, Panel, Skeleton, Sparkline, Unavailable } from '../components/ui.jsx'
import { clock, formatGb, formatRate, pct, timeAgo } from '../lib/format.js'

const LEVEL_TONE = { ok: 'ok', warning: 'warning', critical: 'critical', unknown: 'unknown' }

function StatusLine({ overview, lastUpdated }) {
  const { status, services } = overview
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <span className="flex items-center gap-2.5 text-title text-fg">
        <Dot tone={LEVEL_TONE[status.level]} />
        {status.text}
      </span>
      <span className="text-caption text-muted">
        {services.up} of {services.total} services up
        {lastUpdated ? ` · refreshed ${Math.max(0, Math.round((Date.now() - lastUpdated) / 1000))} s ago` : ''}
      </span>
    </div>
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

function ChatEngine({ chat, series }) {
  const onCpu = chat.engine === 'cpu'
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
      <div className="grid grid-cols-2 gap-4">
        {[
          { key: 'gpu', label: 'GPU', p50: chat.gpu_p50, tone: 'accent', points: series?.gpu },
          { key: 'cpu', label: 'CPU fallback', p50: chat.cpu_p50, tone: 'warning', points: series?.cpu },
        ].map((s) => (
          <div key={s.key} className="grid gap-1">
            <span className="text-caption text-muted">{s.label} · median speed</span>
            <span className="text-title tabular-nums text-fg">
              {formatRate(s.p50)} <span className="text-caption font-normal text-muted">tok/s</span>
            </span>
            {series?.available === false
              ? <span className="text-caption text-muted">History needs the monitoring profile</span>
              : <Sparkline points={s.points} tone={s.tone} label={`${s.label} tokens generated, last 24 hours`} />}
            <span className="text-micro text-muted">Output over the last 24 h</span>
          </div>
        ))}
      </div>
    </div>
  )
}

const SEVERITY_TONE = { critical: 'critical', warning: 'warning' }

function Attention({ items }) {
  if (!items.length) return <p className="text-body text-muted">Nothing needs you.</p>
  return (
    <ul className="grid gap-2">
      {items.map((a) => (
        <li key={a.title} className="grid grid-cols-[auto_1fr] items-baseline gap-x-2.5">
          <Dot tone={SEVERITY_TONE[a.severity] || 'warning'} label={a.severity} />
          <span className="text-body text-fg">
            {a.title}
            {a.detail && <span className="ml-2 text-caption text-muted">{a.detail}</span>}
          </span>
        </li>
      ))}
    </ul>
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
  const o = overview.data

  if (!o) {
    return overview.error
      ? <Unavailable>The dashboard API is not answering: {overview.error.message}</Unavailable>
      : <div className="grid gap-4"><Skeleton className="h-6 w-72" /><Skeleton className="h-64 w-full" /></div>
  }

  return (
    <div className="grid gap-4">
      <StatusLine overview={o} lastUpdated={overview.lastUpdated} />
      <div className="grid gap-4 lg:grid-cols-[1.35fr_1fr]">
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
          <Panel title="Needs attention"><Attention items={o.attention} /></Panel>
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
