// Models tab — port of the legacy "Model Hub" → LLM (llama.cpp) panel. Manages the
// on-disk GGUF files and the active chat model. Consumes:
//   - GET  /api/llm/models          — installed GGUFs on disk ({name,size})
//   - GET  /api/llm/ps              — model-gateway's advertised model (active badge)
//   - POST /api/llm/pull + GET /api/llm/pull/status — download w/ live progress bar
//   - POST /api/llm/delete          — remove a GGUF from disk (confirm)
//   - POST /api/active-model        — switch the active model (recreates llamacpp, ~30–60s)
//   - GET/POST /api/config/default-model — the Open WebUI default-on-open model
//
// Behaviour preserved from the vanilla shell: pull progress bar + streamed log with
// resume-on-mount (a pull in flight when the tab loads re-attaches to /pull/status),
// delete confirm, active-model confirm + a pending "restarting" banner that polls
// /api/llm/ps until the switch lands (up to 75s), and the ".env" / HF-repo pull input.
//
// Deliberately scoped to llama.cpp/GGUF: the legacy Model Hub's diffusion path
// (/api/models/download → ComfyUI category) belongs to the still-stubbed ComfyUI tab,
// not here. The /api/llm/unload endpoint is a documented 501 relic (Ollama-era; llama.cpp
// serves one active model, switched via active-model / Model Control) — matching the
// legacy UI, there is intentionally no unload control.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, usePolling } from '../api.js'
import { useToast } from './Toast.jsx'

const EMBED_RE = /embed|bge|mxbai|arctic-embed|granite-embedding|paraphrase-multilingual/
const isEmbeddingModel = (name) => EMBED_RE.test((name || '').toLowerCase())

function formatSize(bytes) {
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB'
  if (bytes >= 1e6) return (bytes / 1e6).toFixed(0) + ' MB'
  return (bytes / 1e3).toFixed(0) + ' KB'
}

// Best-effort active-model detection, ported verbatim from the legacy loadModels():
// the gateway's advertised id, stripped of a .gguf suffix and any :tag.
function activeBareFromPs(ps) {
  return ((ps?.models?.[0]?.name) || '').replace(/\.gguf$/i, '').split(':')[0]
}
function isActive(modelName, activeBare) {
  if (!activeBare) return false
  const bare = modelName.replace(/\.gguf$/i, '')
  return bare === activeBare || bare.split(':')[0] === activeBare
}

const INPUT =
  'h-9 rounded-sm border border-border bg-bg px-3 text-[0.8125rem] text-fg outline-none transition-colors focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-40'
const BTN =
  'inline-flex h-9 items-center justify-center whitespace-nowrap rounded-sm border border-border bg-surface px-4 text-[0.8125rem] font-medium tracking-[0.02em] text-fg transition-all hover:border-accent/30 hover:bg-accent/[0.07] hover:text-accent disabled:cursor-not-allowed disabled:opacity-40'
const LABEL = 'mb-2 block text-label text-muted'
const PANEL = 'rounded-md border border-border-subtle bg-bg-elevated p-5'

export default function ModelsTab() {
  const toast = useToast()

  // Snapshot poller: installed GGUFs + the gateway's active model (15s, paused when hidden).
  const { data, error, refresh } = usePolling(async () => {
    const [models, ps] = await Promise.all([
      api.get('/api/llm/models'),
      api.get('/api/llm/ps').catch(() => null),
    ])
    return { models, ps }
  }, 15000)

  const models = data?.models?.models || []
  const gatewayOk = data?.models?.ok !== false
  const activeBare = activeBareFromPs(data?.ps)
  const llms = models.filter((m) => !isEmbeddingModel(m.name))

  // ---- Active-model switch (confirm + pending banner, polls /ps until it lands) ----
  const [activeSel, setActiveSel] = useState('')
  const [switching, setSwitching] = useState(null) // model name being activated, or null
  const switchWatchRef = useRef(false)

  // Keep the select defaulted to the current active model (until the user changes it).
  useEffect(() => {
    if (activeSel) return
    const cur = llms.find((m) => isActive(m.name, activeBare))
    if (cur) setActiveSel(cur.name)
    else if (llms.length) setActiveSel(llms[0].name)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeBare, llms.length])

  const watchSwitch = useCallback(async (model) => {
    if (switchWatchRef.current) return
    switchWatchRef.current = true
    const targetBare = model.replace(/\.gguf$/i, '')
    const deadline = Date.now() + 75000
    try {
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 5000))
        let ps
        try { ps = await api.get('/api/llm/ps') } catch { continue }
        if (isActive(model, activeBareFromPs(ps)) || activeBareFromPs(ps) === targetBare.split(':')[0]) {
          toast(`${model} is now the active model`, 'success')
          return
        }
      }
      toast(`${model} switch is taking longer than expected — llama.cpp may still be loading`)
    } finally {
      switchWatchRef.current = false
      setSwitching(null)
      refresh()
    }
  }, [toast, refresh])

  const setActive = async () => {
    const model = activeSel
    if (!model) { toast('Select a model first', 'error'); return }
    if (!confirm(`Set "${model}" as the active model?\n\nllama.cpp will be recreated and the inference server will restart (~30–60 seconds).`)) return
    setSwitching(model)
    try {
      const d = await api.post('/api/active-model', { model })
      if (d.ok) {
        toast(`Activating ${model} — llama.cpp restarting…`)
        watchSwitch(model)
      } else {
        setSwitching(null)
        toast((d.errors && d.errors.join('; ')) || 'Switch failed', 'error')
      }
    } catch (e) {
      setSwitching(null)
      toast(`Switch failed: ${e.message || e}`, 'error')
    }
  }

  // ---- Default (Open WebUI) model ----
  // Kept as a local one-shot fetch (not the snapshot poller) so a failing/absent
  // endpoint never blocks the rest of the tab.
  const [defaultSel, setDefaultSel] = useState('')
  const [defaultBusy, setDefaultBusy] = useState(false)
  const [defCfg, setDefCfg] = useState(null)
  const reloadDef = useCallback(() => {
    api.get('/api/config/default-model').then(setDefCfg).catch(() => setDefCfg(null))
  }, [])
  useEffect(() => { reloadDef() }, [reloadDef])
  useEffect(() => {
    if (defaultSel) return
    const cur = defCfg?.default_model
    if (cur) setDefaultSel(cur)
    else if (llms.length) setDefaultSel(llms[0].name)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defCfg, llms.length])

  const setDefault = async () => {
    const model = defaultSel
    if (!model) { toast('Select a model first', 'error'); return }
    if (!confirm(`Set "${model}" as the Open WebUI default model?\n\nOpen WebUI will be recreated to pick it up.`)) return
    setDefaultBusy(true)
    try {
      const d = await api.post('/api/config/default-model', { model })
      toast(d.webui_recreated ? `Default set to ${model} — Open WebUI recreated` : `Default set to ${model}`, 'success')
      reloadDef()
    } catch (e) {
      toast(`Failed to set default: ${e.message || e}`, 'error')
    } finally {
      setDefaultBusy(false)
    }
  }

  // ---- Delete ----
  const [deleting, setDeleting] = useState({})
  const deleteModel = async (name) => {
    if (!confirm(`Delete "${name}" from disk?\n\nThis removes the GGUF file and cannot be undone.`)) return
    setDeleting((s) => ({ ...s, [name]: true }))
    try {
      const d = await api.post('/api/llm/delete', { model: name })
      toast(d.message || 'Model deleted', 'success')
      refresh()
    } catch (e) {
      toast(`Delete failed: ${e.message || e}`, 'error')
    } finally {
      setDeleting((s) => { const n = { ...s }; delete n[name]; return n })
    }
  }

  // ---- Pull with progress (resume-on-mount) ----
  const [pull, setPull] = useState(null) // {model, output, pct} | null
  const [modelInput, setModelInput] = useState('')
  const pollingRef = useRef(false)

  const pollPull = useCallback(() => {
    if (pollingRef.current) return
    pollingRef.current = true
    let errs = 0
    const tick = () => {
      api.get('/api/llm/pull/status').then((s) => {
        errs = 0
        setPull({ model: s.model || '', output: s.output || '', pct: s.pct == null ? 0 : s.pct })
        if (s.running) { setTimeout(tick, 1500); return }
        pollingRef.current = false
        setPull((p) => (p ? { ...p, pct: 100 } : p))
        if (s.success === false) {
          const line = (s.output || '').split('\n').filter(Boolean).pop() || 'Pull failed'
          toast(`Pull failed: ${line}`, 'error')
        } else {
          toast(`Pull finished${s.model ? `: ${s.model}` : ''}`, 'success')
        }
        setTimeout(() => setPull(null), 4000)
        refresh()
      }).catch(() => {
        if (++errs >= 20) {
          pollingRef.current = false
          toast('Lost connection to pull status', 'error')
          setPull(null)
          return
        }
        setTimeout(tick, 2000)
      })
    }
    tick()
  }, [toast, refresh])

  // Resume a pull already in flight when the tab mounts.
  useEffect(() => {
    api.get('/api/llm/pull/status').then((s) => {
      if (s.running) { setPull({ model: s.model || '', output: `Resuming pull: ${s.model || ''}…`, pct: s.pct || 0 }); pollPull() }
    }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const startPull = async (name) => {
    const model = (name || '').trim()
    if (!model) { toast('Enter a Hugging Face repo id, a .gguf URL, or .env', 'error'); return }
    setPull({ model, output: `Pulling ${model}…`, pct: 0 })
    try {
      await api.post('/api/llm/pull', { model })
      if (name !== '.env') setModelInput('')
      pollPull()
    } catch (e) {
      setPull(null)
      toast(`Pull failed: ${e.message || e}`, 'error')
    }
  }

  const pulling = pull != null
  const listBusy = models.length === 0 && !data

  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        LLM — llama.cpp
      </h2>
      <p className="mb-5 text-[0.8125rem] leading-[1.5] text-muted">
        On-disk GGUF models for llama.cpp. Switch the active chat model (recreates the
        inference server), set the Open WebUI default, or pull a new model from Hugging Face.
      </p>

      {!gatewayOk && (
        <div className="mb-4 flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
          Model gateway unreachable — start with <code className="ml-1 rounded-sm border border-border-subtle bg-bg px-1.5 py-0.5 text-[0.75rem] text-accent-soft">docker compose up -d</code>
        </div>
      )}
      {error && !data && (
        <div className="mb-4 flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
          Could not load models — check that the dashboard API is up.
        </div>
      )}

      {/* Active + default model controls */}
      {llms.length > 0 && (
        <div className="mb-6 grid gap-4 md:grid-cols-2">
          <div className={PANEL}>
            <span className={LABEL}>Active model — all chat consumers use this</span>
            <div className="flex flex-wrap items-center gap-2">
              <select
                className={INPUT + ' min-w-[12rem] flex-1'}
                value={activeSel}
                disabled={!!switching}
                onChange={(e) => setActiveSel(e.target.value)}
                aria-label="Active model"
              >
                {llms.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
              </select>
              <button type="button" className={BTN} disabled={!!switching || !activeSel} onClick={setActive}>
                {switching ? 'Switching…' : 'Set active'}
              </button>
            </div>
            {switching && (
              <div className="mt-3 flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.06] px-3 py-2 text-[0.8rem] text-warning" role="status" aria-live="polite">
                <span className="status-dot pending" aria-hidden="true" />
                Activating {switching} — llama.cpp restarting (~30–60s)…
              </div>
            )}
          </div>

          <div className={PANEL}>
            <span className={LABEL}>Open WebUI default (on open)</span>
            <div className="flex flex-wrap items-center gap-2">
              <select
                className={INPUT + ' min-w-[12rem] flex-1'}
                value={defaultSel}
                disabled={defaultBusy}
                onChange={(e) => setDefaultSel(e.target.value)}
                aria-label="Open WebUI default model"
              >
                {llms.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
              </select>
              <button type="button" className={BTN} disabled={defaultBusy || !defaultSel} onClick={setDefault}>
                {defaultBusy ? 'Setting…' : 'Set default'}
              </button>
            </div>
            {defCfg?.default_model && (
              <p className="mt-2 text-[0.75rem] text-muted">Current: <code className="text-accent-soft">{defCfg.default_model}</code></p>
            )}
          </div>
        </div>
      )}

      {/* Installed GGUFs */}
      <div className="mb-6">
        <span className={LABEL}>Installed GGUFs</span>
        {listBusy ? (
          <div className="space-y-2">
            <div className="skeleton h-[0.9rem]" />
            <div className="skeleton h-[0.9rem] w-2/3" />
            <div className="skeleton h-[0.9rem]" />
          </div>
        ) : models.length === 0 ? (
          <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">
            No models yet. Enter a Hugging Face repo below and click Pull.
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            {models.map((m) => {
              const active = isActive(m.name, activeBare)
              return (
                <div
                  key={m.name}
                  className="flex items-center gap-3 rounded-sm border border-border-subtle bg-bg-elevated px-4 py-2.5 transition-colors hover:border-accent/20"
                >
                  <span className="min-w-0 flex-1 truncate font-mono text-[0.8rem] text-fg" title={m.name}>{m.name}</span>
                  {active && (
                    <span className="inline-flex items-center rounded-full border border-success/30 bg-success/[0.1] px-2 py-0.5 text-micro font-semibold text-success">active</span>
                  )}
                  <span className="shrink-0 text-[0.75rem] tabular-nums text-muted">{formatSize(m.size || 0)}</span>
                  <button
                    type="button"
                    className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-sm border border-transparent text-muted transition-colors hover:border-danger/40 hover:bg-danger/10 hover:text-danger disabled:cursor-not-allowed disabled:opacity-40"
                    title={`Delete ${m.name}`}
                    aria-label={`Delete ${m.name}`}
                    disabled={!!deleting[m.name]}
                    onClick={() => deleteModel(m.name)}
                  >
                    ×
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Pull */}
      <div className="border-t border-border-subtle pt-5">
        <label className={LABEL} htmlFor="llm-pull-input">Pull a GGUF from Hugging Face</label>
        <div className="flex flex-wrap items-center gap-2">
          <input
            id="llm-pull-input"
            type="text"
            className={INPUT + ' min-w-[16rem] flex-1'}
            placeholder="HF repo (org/name), a huggingface.co/…/.gguf URL, or .env"
            aria-label="Hugging Face repo or URL to pull"
            autoComplete="off"
            value={modelInput}
            disabled={pulling}
            onChange={(e) => setModelInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); startPull(modelInput) } }}
          />
          <button
            type="button"
            className={BTN}
            title="Runs gguf-puller with GGUF_MODELS from .env"
            disabled={pulling}
            onClick={() => startPull('.env')}
          >
            Pull .env models
          </button>
          <button type="button" className={BTN} disabled={pulling || !modelInput.trim()} onClick={() => startPull(modelInput)}>
            {pulling ? 'Pulling…' : 'Pull'}
          </button>
        </div>

        {pull && (
          <div className="mt-4 rounded-md border border-border-subtle bg-bg-elevated p-4" role="region" aria-label="Pull progress">
            <div className="h-2 w-full overflow-hidden rounded-full border border-border-subtle bg-bg">
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-300"
                style={{ width: `${pull.pct || 0}%` }}
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pull.pct || 0}
              />
            </div>
            <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[0.7rem] leading-[1.5] text-fg-muted" aria-live="polite">
              {pull.output || 'Preparing…'}
            </pre>
          </div>
        )}
      </div>
    </section>
  )
}
