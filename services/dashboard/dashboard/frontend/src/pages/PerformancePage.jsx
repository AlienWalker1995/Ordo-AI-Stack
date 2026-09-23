// Performance: the Grafana dashboard (llama.cpp GPU and CPU fallback, GPU hardware) embedded
// same-origin at /grafana/, with range presets. When Grafana is not running the page says so
// instead of drawing an empty frame.
//   GET /api/perf/grafana   {available, path}
import { useState } from 'react'
import { api, useFetch } from '../api.js'
import { Skeleton, Unavailable } from '../components/ui.jsx'

const RANGES = [
  { id: '30m', label: '30 min' },
  { id: '3h', label: '3 h' },
  { id: '24h', label: '24 h' },
  { id: '7d', label: '7 days' },
]

export default function PerformancePage() {
  const grafana = useFetch(() => api.get('/api/perf/grafana'), [])
  const [range, setRange] = useState('3h')

  if (!grafana.data) {
    return grafana.error
      ? <Unavailable>Could not check Grafana: {grafana.error.message}</Unavailable>
      : <Skeleton className="h-96 w-full" />
  }
  if (!grafana.data.available) {
    return (
      <Unavailable>
        Grafana is not running. Performance history needs the monitoring plugin
        (Prometheus, Grafana and the GPU exporter).
      </Unavailable>
    )
  }

  const embed = `${grafana.data.path}&from=now-${range}&to=now`
  const full = grafana.data.path.replace(/&kiosk/, '').replace(/&theme=dark/, '')

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="radiogroup" aria-label="Time range" className="inline-flex rounded-sm border border-border p-0.5">
          {RANGES.map((r) => (
            <button key={r.id} type="button" role="radio" aria-checked={range === r.id}
                    className={'h-7 rounded-[4px] px-3 text-label transition-colors ' +
                      (range === r.id ? 'bg-accent/[0.14] text-accent-soft' : 'text-fg-muted hover:text-fg')}
                    onClick={() => setRange(r.id)}>
              {r.label}
            </button>
          ))}
        </div>
        <a href={full} target="_blank" rel="noreferrer" className="text-label no-underline hover:underline">
          Open in Grafana <span aria-hidden="true">↗</span>
        </a>
      </div>
      <iframe
        key={embed}
        title="Ordo performance (Grafana)"
        src={embed}
        className="h-[calc(100vh-12rem)] min-h-[32rem] w-full rounded-md border border-border-subtle bg-bg"
      />
    </div>
  )
}
