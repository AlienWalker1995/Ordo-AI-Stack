// ComfyUI tab — model/pack management + a status/restart control. Port of the legacy
// loadComfyuiPanel/loadComfyuiModels/loadComfyuiPacks + the pack-download flow + the
// ComfyUI status/restart verbs. Consumes:
//   - GET  /api/comfyui/models                    — installed models on disk ({name,category,size_mb})
//   - GET  /api/comfyui/packs                     — available model packs + install counts
//   - POST /api/comfyui/pull?packs=…              — download selected packs (background)
//   - GET  /api/comfyui/pull/status               — pull progress ({running,output,success})
//   - POST /api/comfyui/install-node-requirements — pip install a custom-node's requirements (confirm)
//   - GET  /api/orchestration/comfyui/status      — container state + queue reachability + rolled-up up
//   - POST /api/orchestration/comfyui/restart     — restart the ComfyUI container (confirm)
// The pull is a long-running action: it streams into a log and polls status (no % from the
// backend, so the progress is an indeterminate "running" indicator), resuming on mount if a
// pull is already in flight. Restart confirms, then polls status until the container settles.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, usePolling } from '../api.js'
import { useToast } from './Toast.jsx'

function formatSize(bytes) {
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB'
  if (bytes >= 1e6) return (bytes / 1e6).toFixed(0) + ' MB'
  return (bytes / 1e3).toFixed(0) + ' KB'
}
const mBytes = (m) => Number(m.size_bytes ?? (m.size_mb || 0) * 1024 * 1024)
// ComfyUI ships 0-byte `put_*_here` placeholder files — not real models.
const isPlaceholder = (name) => /^put_.*_here(\.\w+)?$/i.test(name || '')

const BTN =
  'inline-flex h-9 items-center justify-center whitespace-nowrap rounded-sm border border-border bg-surface px-4 text-[0.8125rem] font-medium tracking-[0.02em] text-fg transition-all hover:border-accent/30 hover:bg-accent/[0.07] hover:text-accent disabled:cursor-not-allowed disabled:opacity-40'
const INPUT =
  'h-9 rounded-sm border border-border bg-bg px-3 text-[0.8125rem] text-fg outline-none transition-colors focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-40'
const PANEL = 'rounded-md border border-border-subtle bg-bg-elevated p-5'
const SUBHEAD = 'mb-2 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted'
const BADGE = 'inline-flex items-center rounded-full border px-2 py-0.5 text-[0.62rem] font-semibold uppercase tracking-[0.06em]'

// --- Status + restart control ---------------------------------------------------
function StatusControl() {
  const toast = useToast()
  const { data, refresh } = usePolling(() => api.get('/api/orchestration/comfyui/status'), 15000)
  const [restarting, setRestarting] = useState(false)
  const watchRef = useRef(false)

  const state = data?.container_state || 'unknown'
  const up = data?.up === true
  const reachable = data?.queue?.reachable === true

  const watchRestart = useCallback(async () => {
    if (watchRef.current) return
    watchRef.current = true
    const deadline = Date.now() + 90000
    try {
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 4000))
        let s
        try { s = await api.get('/api/orchestration/comfyui/status') } catch { continue }
        if (s.up) { toast('ComfyUI is back up', 'success'); return }
      }
      toast('ComfyUI restart is taking longer than expected — check the Services tab')
    } finally {
      watchRef.current = false
      setRestarting(false)
      refresh()
    }
  }, [toast, refresh])

  const restart = async () => {
    if (!confirm('Restart the ComfyUI container?\n\nIn-flight renders will be interrupted.')) return
    setRestarting(true)
    try {
      await api.post('/api/orchestration/comfyui/restart', { confirm: true })
      toast('Restarting ComfyUI…')
      watchRestart()
    } catch (e) {
      setRestarting(false)
      toast(`Restart failed: ${e.message || e}`, 'error')
    }
  }

  const badgeCls = restarting
    ? 'border-warning/30 bg-warning/10 text-warning'
    : up
      ? 'border-success/30 bg-success/10 text-success'
      : 'border-danger/30 bg-danger/10 text-danger'

  return (
    <div className={`${PANEL} mb-6`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className={`status-dot ${restarting ? 'pending' : up ? 'ok' : 'fail'}`} aria-hidden="true" />
          <div>
            <span className="text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">ComfyUI service</span>
            <div className="mt-0.5 flex items-center gap-2">
              <span className={`${BADGE} ${badgeCls}`}>{restarting ? 'restarting' : up ? 'up' : state}</span>
              {!restarting && (
                <span className="text-[0.72rem] text-muted">
                  container {state} · queue {reachable ? 'reachable' : 'unreachable'}
                </span>
              )}
            </div>
          </div>
        </div>
        <button type="button" className={BTN} disabled={restarting} onClick={restart}>
          {restarting ? 'Restarting…' : '↻ Restart'}
        </button>
      </div>
    </div>
  )
}

// --- Installed models (grouped by category) -------------------------------------
function ModelsList() {
  const { data, error } = usePolling(() => api.get('/api/comfyui/models'), 30000)

  const raw = data?.ok ? (data.models || []) : []
  const models = raw.filter((m) => !isPlaceholder(m.name) && mBytes(m) > 0)
  const totalBytes = models.reduce((sum, m) => sum + mBytes(m), 0)

  const byCat = {}
  models.forEach((m) => {
    const cat = m.category || 'Uncategorized'
    if (!byCat[cat]) byCat[cat] = []
    byCat[cat].push(m)
  })
  const cats = Object.keys(byCat).sort()

  return (
    <div className="mb-6">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className={SUBHEAD + ' mb-0'}>Installed models</h3>
        {models.length > 0 && (
          <span className="font-mono text-[0.72rem] text-muted">{formatSize(totalBytes)} across {models.length} models</span>
        )}
      </div>
      {error && !data ? (
        <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-4 text-[0.8125rem] text-muted">Failed to load ComfyUI models.</div>
      ) : !data ? (
        <div className="space-y-2">{[0, 1].map((i) => <div key={i} className="skeleton h-9 w-full" />)}</div>
      ) : models.length === 0 ? (
        <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">
          No ComfyUI models installed. Select packs below and download.
        </div>
      ) : (
        <div className="space-y-4">
          {cats.map((cat) => {
            const items = byCat[cat].sort((a, b) => (a.name || '').localeCompare(b.name || ''))
            const catBytes = items.reduce((s, m) => s + mBytes(m), 0)
            return (
              <div key={cat} className="overflow-hidden rounded-md border border-border-subtle">
                <div className="flex items-center justify-between gap-3 border-b border-border-subtle bg-bg-elevated px-4 py-2">
                  <span className="text-[0.78rem] font-semibold text-fg">{cat.replace(/_/g, ' ')}</span>
                  <span className="font-mono text-[0.7rem] text-muted">{items.length} file{items.length === 1 ? '' : 's'} · {formatSize(catBytes)}</span>
                </div>
                <div>
                  {items.map((m) => (
                    <div key={m.name} className="flex items-center gap-3 border-b border-border-subtle px-4 py-2 last:border-b-0">
                      <span className="min-w-0 flex-1 truncate font-mono text-[0.78rem] text-fg-muted" title={m.name}>{m.name}</span>
                      <span className="shrink-0 font-mono text-[0.72rem] tabular-nums text-muted">{formatSize(mBytes(m))}</span>
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// --- Packs + download-with-progress ---------------------------------------------
function PacksPanel() {
  const toast = useToast()
  const { data, error, refresh } = usePolling(() => api.get('/api/comfyui/packs'), 30000)
  const packs = data?.ok ? (data.packs || {}) : {}
  const defaults = new Set(data?.defaults || [])
  const packNames = Object.keys(packs)

  const [selected, setSelected] = useState(null) // Set once user interacts; null = use defaults
  const [pull, setPull] = useState(null) // {output, running} | null
  const pollingRef = useRef(false)

  const isChecked = (name) => {
    if (selected) return selected.has(name)
    const p = packs[name]
    const all = p && p.installed_count >= p.model_count && p.model_count > 0
    return defaults.has(name) && !all
  }
  const toggle = (name) => {
    setSelected((prev) => {
      const next = new Set(prev || packNames.filter(isChecked))
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }
  const checkedNames = packNames.filter(isChecked)

  const pollPull = useCallback(() => {
    if (pollingRef.current) return
    pollingRef.current = true
    let errs = 0
    const tick = () => {
      api.get('/api/comfyui/pull/status').then((s) => {
        errs = 0
        setPull({ output: s.output || 'Preparing…', running: !!s.running })
        if (s.running) { setTimeout(tick, 1500); return }
        pollingRef.current = false
        toast(s.success ? 'ComfyUI models ready' : 'Download failed', s.success ? 'success' : 'error')
        setPull((p) => (p ? { ...p, output: (p.output || '') + '\n' + (s.success ? '✓ Done.' : '✗ Failed.'), running: false } : p))
        setTimeout(() => setPull(null), 5000)
        refresh()
      }).catch(() => {
        if (++errs >= 20) { pollingRef.current = false; toast('Lost connection to pull status', 'error'); setPull(null); return }
        setTimeout(tick, 3000)
      })
    }
    tick()
  }, [toast, refresh])

  // Resume a pull already in flight when the tab mounts.
  useEffect(() => {
    api.get('/api/comfyui/pull/status').then((s) => {
      if (s.running) { setPull({ output: s.output || 'Resuming download…', running: true }); pollPull() }
    }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const download = async () => {
    if (!checkedNames.length) { toast('Select at least one pack', 'error'); return }
    setPull({ output: 'Starting download…', running: true })
    try {
      const qs = '?packs=' + encodeURIComponent(checkedNames.join(','))
      await api.post('/api/comfyui/pull' + qs)
      pollPull()
    } catch (e) {
      setPull(null)
      toast(`Failed to start download: ${e.message || e}`, 'error')
    }
  }

  const pulling = pull != null && pull.running

  return (
    <div className="mb-6">
      <h3 className={SUBHEAD}>Model packs</h3>
      {error && !data ? (
        <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-4 text-[0.8125rem] text-muted">Failed to load packs.</div>
      ) : !data ? (
        <div className="space-y-2">{[0, 1].map((i) => <div key={i} className="skeleton h-12 w-full" />)}</div>
      ) : packNames.length === 0 ? (
        <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">No packs available.</div>
      ) : (
        <>
          <div className="space-y-1.5">
            {packNames.map((name) => {
              const p = packs[name]
              const all = p.installed_count >= p.model_count && p.model_count > 0
              const partial = p.installed_count > 0 && !all
              return (
                <label key={name} className="flex cursor-pointer items-start gap-3 rounded-sm border border-border-subtle bg-bg-elevated px-4 py-2.5 transition-colors hover:border-accent/20">
                  <input
                    type="checkbox"
                    className="mt-1 shrink-0 accent-accent"
                    checked={isChecked(name)}
                    disabled={pulling}
                    onChange={() => toggle(name)}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="text-[0.82rem] font-medium text-fg">{name}</span>
                      {all
                        ? <span className={`${BADGE} border-success/30 bg-success/10 text-success`}>installed</span>
                        : partial
                          ? <span className={`${BADGE} border-border-subtle bg-bg text-muted`}>{p.installed_count}/{p.model_count} files</span>
                          : null}
                    </span>
                    {p.description && <span className="mt-0.5 block text-[0.72rem] leading-[1.4] text-fg-muted">{p.description}</span>}
                  </span>
                </label>
              )
            })}
          </div>

          <div className="mt-3 flex items-center gap-3">
            <button type="button" className={BTN} disabled={pulling || checkedNames.length === 0} onClick={download}>
              {pulling ? 'Downloading…' : `Download${checkedNames.length ? ` (${checkedNames.length})` : ''}`}
            </button>
            {pulling && (
              <span className="flex items-center gap-2 text-[0.8rem] text-warning">
                <span className="status-dot pending" aria-hidden="true" />
                Downloading models…
              </span>
            )}
          </div>

          {pull && (
            <div className="mt-4 rounded-md border border-border-subtle bg-bg-elevated p-4" role="region" aria-label="Download progress">
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[0.7rem] leading-[1.5] text-fg-muted" aria-live="polite">
                {pull.output || 'Preparing…'}
              </pre>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// --- Install custom-node requirements (secondary control) -----------------------
function NodeRequirements() {
  const toast = useToast()
  const [node, setNode] = useState('')
  const [busy, setBusy] = useState(false)
  const [log, setLog] = useState('')

  const install = async () => {
    const path = node.trim()
    if (!path) { toast('Enter a custom-node folder name', 'error'); return }
    if (!confirm(`Run "pip install -r requirements.txt" for custom_nodes/${path}?\n\nThis installs Python packages into the ComfyUI container.`)) return
    setBusy(true)
    setLog('Installing…')
    try {
      const d = await api.post('/api/comfyui/install-node-requirements', { node_path: path, confirm: true })
      setLog(typeof d === 'string' ? d : (d.output || d.detail || JSON.stringify(d, null, 2)))
      toast('Requirements installed', 'success')
    } catch (e) {
      setLog(`Error: ${e.message || e}`)
      toast(`Install failed: ${e.message || e}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="border-t border-border-subtle pt-5">
      <h3 className={SUBHEAD}>Custom-node requirements</h3>
      <p className="mb-3 text-[0.8125rem] leading-[1.5] text-muted">
        Install a custom node's Python dependencies (<code className="text-accent-soft">pip install -r requirements.txt</code>) inside the ComfyUI container.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          className={INPUT + ' min-w-[16rem] flex-1'}
          placeholder="custom_nodes folder name (e.g. ComfyUI-Manager)"
          aria-label="Custom-node folder name"
          autoComplete="off"
          value={node}
          disabled={busy}
          onChange={(e) => setNode(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); install() } }}
        />
        <button type="button" className={BTN} disabled={busy || !node.trim()} onClick={install}>
          {busy ? 'Installing…' : 'Install requirements'}
        </button>
      </div>
      {log && (
        <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border-subtle bg-bg-elevated p-4 font-mono text-[0.7rem] leading-[1.5] text-fg-muted" aria-live="polite">
          {log}
        </pre>
      )}
    </div>
  )
}

export default function ComfyTab() {
  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        ComfyUI
      </h2>
      <p className="mb-5 text-[0.8125rem] leading-[1.5] text-muted">
        Diffusion model and pack management for ComfyUI, plus a service status/restart control.
        Downloads run in the background — leave the tab and come back; a pull in flight resumes.
      </p>
      <StatusControl />
      <ModelsList />
      <PacksPanel />
      <NodeRequirements />
    </section>
  )
}
