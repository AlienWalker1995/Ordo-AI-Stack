// Models: what each chat server is running, switching the GPU model the one safe way, and the
// model files on disk.
//   GET  /api/models                slots (gpu, cpu, embed), catalog (installed, active), files (in_use)
//   POST /api/models/switch         catalog id -> render -> recreate llama.cpp + gateway
//   POST /api/models/delete         refuses a file any server depends on
//   POST /api/throughput/benchmark  a short generation against local-chat
import { useState } from 'react'
import { api, usePolling } from '../api.js'
import { BTN, BTN_DANGER, BTN_PRIMARY, Chip, INPUT, Panel, Skeleton, Unavailable } from '../components/ui.jsx'
import { useToast } from '../components/Toast.jsx'
import { formatBytes, formatRate } from '../lib/format.js'

function Slot({ title, file, children }) {
  return (
    <div className="grid content-start gap-1.5 rounded-sm border border-border-subtle bg-bg p-3">
      <span className="text-micro font-bold uppercase tracking-[0.12em] text-muted">{title}</span>
      <span className="break-all font-mono text-caption text-fg">{file || 'Not reported'}</span>
      {children}
    </div>
  )
}

function Speed({ p50 }) {
  return (
    <span className="text-caption text-muted">
      Median speed <span className="font-mono tabular-nums text-fg">{formatRate(p50)}</span> tok/s
    </span>
  )
}

function SwitchModel({ data, onSwitched }) {
  const toast = useToast()
  const installed = data.catalog.filter((c) => c.installed)
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)
  const target = data.catalog.find((c) => c.id === choice)

  const run = async () => {
    if (!target) return
    if (!window.confirm(`Switch the GPU chat model to ${target.id}?\n\nllama.cpp and the model gateway restart; chat runs on the CPU fallback for about a minute while the new model loads.`)) return
    setBusy(true)
    try {
      const r = await api.post('/api/models/switch', { model: target.id })
      toast(`Switched to ${r.active_model}. Restarted ${r.recreated.join(', ')}.`, 'success')
      if (r.hermes_restart_needed) {
        toast('The context window changed: restart Hermes so it picks up the new size.', 'error')
      }
      setChoice('')
      onSwitched()
    } catch (e) {
      toast(`Switch failed: ${e.message}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-2">
      <label htmlFor="model-switch" className="text-label text-muted">Switch the GPU model</label>
      <div className="flex flex-wrap gap-2">
        <select id="model-switch" className={INPUT + ' min-w-0 flex-1 basis-64'} value={choice} disabled={busy}
                onChange={(e) => setChoice(e.target.value)}>
          <option value="">Choose a model from the catalog…</option>
          {data.catalog.map((c) => (
            <option key={c.id} value={c.id} disabled={!c.installed || c.active}>
              {c.id}{c.active ? ' (running)' : !c.installed ? ' (not downloaded)' : ''}
            </option>
          ))}
        </select>
        <button type="button" className={BTN_PRIMARY} disabled={busy || !target} onClick={run}>
          {busy ? 'Switching…' : 'Switch'}
        </button>
      </div>
      <p className="text-caption text-muted">
        {installed.length} of {data.catalog.length} catalog models are downloaded. Download another with{' '}
        <code className="text-fg-muted">ordo fetch &lt;catalog id&gt;</code> on the host.
      </p>
    </div>
  )
}

function Benchmark() {
  const toast = useToast()
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      setResult(await api.post('/api/throughput/benchmark', { model: 'local-chat' }))
    } catch (e) {
      toast(`Benchmark failed: ${e.message}`, 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" className={BTN} disabled={busy} onClick={run}>{busy ? 'Running…' : 'Run a benchmark'}</button>
      {result && (
        <span className="text-caption text-muted">
          <span className="font-mono tabular-nums text-fg">{formatRate(result.output_tokens_per_sec)}</span> tok/s ·{' '}
          {result.output_tokens} tokens in {(result.total_duration_ms / 1000).toFixed(1)} s
        </span>
      )}
    </div>
  )
}

function Files({ files, onDeleted }) {
  const toast = useToast()
  const [busy, setBusy] = useState(null)
  const total = files.reduce((n, f) => n + (f.size || 0), 0)
  const remove = async (name) => {
    if (!window.confirm(`Delete ${name} from disk? This cannot be undone.`)) return
    setBusy(name)
    try {
      await api.post('/api/models/delete', { file: name })
      toast(`Deleted ${name}`, 'success')
      onDeleted()
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setBusy(null)
    }
  }
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse">
        <thead>
          <tr className="text-left text-micro uppercase tracking-[0.08em] text-muted">
            <th className="py-2 pr-2 font-semibold">File</th>
            <th className="w-[6rem] px-2 py-2 text-right font-semibold">Size</th>
            <th className="w-[6rem] px-2 py-2 font-semibold">Status</th>
            <th className="w-[6rem] py-2 pl-2"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr key={f.name} className="border-t border-border-subtle">
              <td className="break-all py-2 pr-2 font-mono text-caption text-fg">{f.name}</td>
              <td className="whitespace-nowrap px-2 py-2 text-right font-mono text-caption tabular-nums text-fg-muted">{formatBytes(f.size)}</td>
              <td className="px-2 py-2">{f.in_use ? <Chip tone="accent">In use</Chip> : <span className="text-caption text-muted">Unused</span>}</td>
              <td className="py-2 pl-2 text-right">
                <button type="button" className={BTN_DANGER} disabled={f.in_use || busy === f.name}
                        title={f.in_use ? 'A running model server depends on this file' : `Delete ${f.name}`}
                        onClick={() => remove(f.name)}>Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-border">
            <td className="py-2 pr-2 text-caption text-muted">{files.length} files</td>
            <td className="px-2 py-2 text-right font-mono text-caption tabular-nums text-fg-muted">{formatBytes(total)}</td>
            <td colSpan={2} />
          </tr>
        </tfoot>
      </table>
    </div>
  )
}

export default function ModelsPage() {
  const models = usePolling(() => api.get('/api/models'), 15000)
  const d = models.data

  if (!d) {
    return models.error
      ? <Unavailable>Models are unavailable: {models.error.message}</Unavailable>
      : <Skeleton className="h-64 w-full" />
  }

  return (
    <div className="grid gap-4 [&>*]:min-w-0">
      <Panel title="Running now">
        <div className="grid gap-3 md:grid-cols-3 [&>*]:min-w-0">
          <Slot title="GPU chat" file={d.gpu.file}>
            <div className="flex flex-wrap items-center gap-1.5">
              {d.gpu.catalog_id && <Chip>{d.gpu.catalog_id}</Chip>}
              {!d.gpu.loaded && <Chip tone="warning" title="A render has the GPU, or the server is restarting">Not loaded now</Chip>}
            </div>
            <Speed p50={d.gpu.p50} />
            {d.gpu.ctx && <span className="text-caption text-muted">Context <span className="font-mono tabular-nums text-fg">{d.gpu.ctx.toLocaleString()}</span> tokens</span>}
          </Slot>
          <Slot title="CPU fallback" file={d.cpu.file}>
            <span className="text-caption text-muted">Answers chat while a render has the GPU.</span>
            <Speed p50={d.cpu.p50} />
          </Slot>
          <Slot title="Embeddings" file={d.embed.file}>
            <span className="text-caption text-muted">Indexes documents for search.</span>
          </Slot>
        </div>
        <div className="mt-4 grid gap-4 border-t border-border-subtle pt-4">
          <SwitchModel data={d} onSwitched={models.refresh} />
          <Benchmark />
        </div>
      </Panel>
      <Panel title="Model files on disk">
        <Files files={d.files} onDeleted={models.refresh} />
      </Panel>
    </div>
  )
}
