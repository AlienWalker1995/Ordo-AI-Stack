// Media: what ComfyUI is rendering, what it made recently, and who has held the GPU.
//   GET    /api/media                                   running job, queue depth, recent renders
//   GET    /api/media/view                              one output file (thumbnails, players)
//   GET    /api/orchestration/gpu/history               finished GPU leases
//   POST   /api/orchestration/comfyui/restart           restart ComfyUI
//   GET    /api/comfyui/models                          installed ComfyUI model files
//   DELETE /api/comfyui/models/{category}/{filename}
import { useMemo, useState } from 'react'
import { api, usePolling } from '../api.js'
import { BTN, BTN_DANGER, Chip, Dot, Panel, Skeleton, Unavailable } from '../components/ui.jsx'
import { useToast } from '../components/Toast.jsx'
import { clock, formatBytes, timeAgo } from '../lib/format.js'
import { lease } from './leases.js'

function viewUrl(output) {
  const q = new URLSearchParams({ filename: output.filename, subfolder: output.subfolder || '', type: output.type || 'output' })
  return `/api/media/view?${q}`
}

function OutputTile({ render, output }) {
  const url = viewUrl(output)
  let body
  if (output.media === 'image') {
    body = <img src={url} alt={output.filename} loading="lazy" className="aspect-square w-full rounded-sm bg-bg object-cover" />
  } else if (output.media === 'video') {
    body = <video src={url} preload="metadata" muted controls className="aspect-square w-full rounded-sm bg-bg object-cover" aria-label={output.filename} />
  } else if (output.media === 'audio') {
    body = (
      <div className="grid aspect-square w-full min-w-0 grid-cols-[minmax(0,1fr)] content-center gap-2 rounded-sm bg-bg p-2">
        <span className="text-center text-caption text-muted">Audio</span>
        <audio src={url} preload="none" controls className="w-full min-w-0 max-w-full" aria-label={output.filename} />
      </div>
    )
  } else {
    body = <div className="grid aspect-square w-full place-content-center rounded-sm bg-bg text-caption text-muted">{output.media}</div>
  }
  return (
    <figure className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1">
      {body}
      <figcaption className="flex items-center justify-between gap-2 text-micro text-muted">
        <span className="truncate font-mono" title={output.filename}>{output.filename}</span>
        <span className="shrink-0">{timeAgo(render.ts)}</span>
      </figcaption>
    </figure>
  )
}

function Recent({ renders }) {
  const tiles = []
  const failures = []
  for (const r of renders) {
    if (!r.ok) failures.push(r)
    for (const o of r.outputs.slice(0, 2)) tiles.push({ r, o })
  }
  return (
    <div className="grid gap-3">
      {failures.length > 0 && (
        <p className="flex items-center gap-2 text-caption text-muted">
          <Dot tone="critical" /> {failures.length} of the last {renders.length} renders failed
        </p>
      )}
      {tiles.length === 0
        ? <p className="text-body text-muted">No outputs yet.</p>
        : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6 [&>*]:min-w-0">
            {tiles.slice(0, 18).map(({ r, o }) => <OutputTile key={`${r.prompt_id}-${o.filename}`} render={r} output={o} />)}
          </div>
        )}
    </div>
  )
}

function Leases({ history }) {
  if (!history.length) return <p className="text-body text-muted">No GPU leases recorded.</p>
  return (
    <ol className="grid gap-1.5">
      {history.slice(0, 12).map((h, i) => {
        const l = lease(h)
        return (
          <li key={`${h.id}-${h.submitted || i}`} className="grid grid-cols-[3.25rem_auto_1fr_auto] items-center gap-2.5">
            <time className="font-mono text-caption tabular-nums text-muted">{clock(h.ended || h.started)}</time>
            <Dot tone={l.tone} />
            <span className="truncate text-body text-fg">{l.holder}<span className="ml-2 text-caption text-muted">{l.outcome}</span></span>
            <span className="font-mono text-caption tabular-nums text-muted">{l.duration}</span>
          </li>
        )
      })}
    </ol>
  )
}

function InstalledModels() {
  const toast = useToast()
  const { data, error, reload } = useInstalledModels()
  const [busy, setBusy] = useState(null)
  const byCategory = useMemo(() => {
    const out = {}
    for (const m of data?.models || []) (out[m.category] = out[m.category] || []).push(m)
    return out
  }, [data])

  const remove = async (m) => {
    if (!window.confirm(`Delete ${m.name} from ${m.category}? This cannot be undone.`)) return
    setBusy(m.name)
    try {
      await api.del(`/api/comfyui/models/${encodeURIComponent(m.category)}/${encodeURIComponent(m.name)}`)
      toast(`Deleted ${m.name}`, 'success')
      reload()
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setBusy(null)
    }
  }

  if (error) return <Unavailable>Installed models are unavailable: {error.message}</Unavailable>
  if (!data) return <Skeleton className="h-16 w-full" />
  const categories = Object.keys(byCategory).sort()
  // size_mb is binary megabytes (MiB) from the backend scan.
  const totalBytes = (data.models || []).reduce((n, m) => n + (m.size_mb || 0) * 1024 * 1024, 0)
  return (
    <div className="grid gap-2">
      <p className="text-caption text-muted">{(data.models || []).length} files · {formatBytes(totalBytes)}</p>
      {categories.map((c) => (
        <details key={c} className="rounded-sm border border-border-subtle">
          <summary className="flex cursor-pointer items-center justify-between gap-3 px-3 py-2 text-body text-fg">
            <span>{c.replace(/_/g, ' ')}</span>
            <span className="text-caption text-muted">{byCategory[c].length}</span>
          </summary>
          <ul className="border-t border-border-subtle">
            {byCategory[c].map((m) => (
              <li key={m.name} className="flex items-center justify-between gap-3 border-b border-border-subtle px-3 py-1.5 last:border-b-0">
                <span className="min-w-0 truncate font-mono text-caption text-fg" title={m.name}>{m.name}</span>
                <span className="flex shrink-0 items-center gap-3">
                  <span className="font-mono text-caption tabular-nums text-muted">{formatBytes((m.size_mb || 0) * 1024 * 1024)}</span>
                  <button type="button" className={BTN_DANGER} disabled={busy === m.name} onClick={() => remove(m)}>Delete</button>
                </span>
              </li>
            ))}
          </ul>
        </details>
      ))}
    </div>
  )
}

// Installed models change rarely: refresh every five minutes, and right after a delete.
function useInstalledModels() {
  const { data, error, refresh } = usePolling(() => api.get('/api/comfyui/models'), 300000)
  return { data, error, reload: refresh }
}

export default function MediaPage() {
  const toast = useToast()
  const media = usePolling(() => api.get('/api/media'), 5000)
  const history = usePolling(() => api.get('/api/orchestration/gpu/history'), 30000)
  const [restarting, setRestarting] = useState(false)

  const restart = async () => {
    if (!window.confirm('Restart ComfyUI?\n\nQueued and running renders are lost. It takes one to five minutes to come back.')) return
    setRestarting(true)
    try {
      await api.post('/api/orchestration/comfyui/restart', { confirm: true })
      toast('ComfyUI is restarting. It is expected to be unreachable for a few minutes.', 'success')
    } catch (e) {
      toast(`Restart failed: ${e.message}`, 'error')
    } finally {
      setRestarting(false)
    }
  }

  const m = media.data
  return (
    <div className="grid gap-4 [&>*]:min-w-0">
      <Panel title="ComfyUI" action={<button type="button" className={BTN} disabled={restarting} onClick={restart}>Restart</button>}>
        {!m
          ? (media.error ? <Unavailable>ComfyUI is not answering. After a restart it takes one to five minutes to come back.</Unavailable> : <Skeleton className="h-6 w-64" />)
          : (
            <div className="flex flex-wrap items-center gap-3">
              <Dot tone={m.running ? 'info' : 'ok'} />
              <span className="text-body text-fg">{m.running ? 'Rendering now' : 'Idle'}</span>
              {m.pending > 0 && <Chip tone="accent">{m.pending} queued</Chip>}
              {m.running && <span className="font-mono text-caption text-muted">{m.running.prompt_id.slice(0, 8)}</span>}
            </div>
          )}
      </Panel>
      <Panel title="Recent outputs">
        {m ? <Recent renders={m.recent} /> : <Skeleton className="h-40 w-full" />}
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
        <Panel title="GPU leases">
          {history.data ? <Leases history={history.data.history || []} />
            : history.error ? <Unavailable>The scheduler is not answering.</Unavailable> : <Skeleton className="h-24 w-full" />}
        </Panel>
        <Panel title="Installed ComfyUI models"><InstalledModels /></Panel>
      </div>
    </div>
  )
}
