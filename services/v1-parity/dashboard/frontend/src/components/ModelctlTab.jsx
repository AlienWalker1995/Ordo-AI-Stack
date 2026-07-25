// Model Control tab — port of the legacy "Model Control" flag UI + the Model Registry
// table (with GPU assignment). Two sections, each an editable control plane:
//
// 1. Flag cards over /api/model-config (llama.cpp launch params as feature flags):
//    - active-model select + "Apply & restart" (POST {confirm, overrides}) with a dirty
//      counter and a pending "Applying…" state; on success toasts the recreated services.
//    - grouped flag rows (Core / Context / Attention / MTP / Generation / Multimodal /
//      Advanced), each with an enum/bool/number/text input, an override|inherited pill,
//      and a reset (↺) to baseline. Edits stay LOCAL until Apply — the panel never
//      auto-refreshes under the user (would clobber pending edits).
//
// 2. Registry table over /api/registry/models + /api/registry/gpus:
//    - per-model row: id, kind badge, service, source file, status (Set active / Active /
//      Enabled-Disabled), and a GPU-assignment dropdown. Set-active and GPU-assign both
//      recreate the service (brief downtime) and are confirm-gated; GPU-assign surfaces a
//      409 capacity conflict as an error toast. Reloads registry + gpus after each change.
//
// Config data is loaded once (with manual reload) rather than polled, because both
// sections are editable and a background refresh would discard in-flight edits.
import { useMemo, useState } from 'react'
import { api, useFetch } from '../api.js'
import { useToast } from './Toast.jsx'

// Legacy MC_GROUPS ordering + labels (drives the flag-card section order).
const MC_GROUPS = [
  ['core', 'Core'],
  ['context', 'Context extension'],
  ['attention', 'Attention / KV'],
  ['mtp', 'Speculative (MTP)'],
  ['gen', 'Generation caps'],
  ['multimodal', 'Multimodal'],
  ['advanced', 'Advanced'],
]

const INPUT =
  'h-9 rounded-sm border border-border bg-bg px-3 text-[0.8125rem] text-fg outline-none transition-colors focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-40'
const BTN =
  'inline-flex h-9 items-center justify-center whitespace-nowrap rounded-sm border border-border bg-surface px-4 text-[0.8125rem] font-medium tracking-[0.02em] text-fg transition-all hover:border-accent/30 hover:bg-accent/[0.07] hover:text-accent disabled:cursor-not-allowed disabled:opacity-40'
const LABEL = 'mb-2 block text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted'
const SECTION = 'mb-5 rounded-lg border border-border bg-card p-6 shadow-card'
const H2 = 'section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted'
const DESC = 'mb-5 text-[0.8125rem] leading-[1.5] text-muted'
const WARN_BANNER = 'flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium'

// ---------------------------------------------------------------------------
// Section 1: model-config flag cards
// ---------------------------------------------------------------------------
function ModelConfigCard() {
  const toast = useToast()
  const { data, error, loading, reload } = useFetch(() => api.get('/api/model-config'), [])

  const [dirty, setDirty] = useState({})   // { KEY: value | null(reset) }
  const [modelSel, setModelSel] = useState(null) // active-model override (LLAMACPP_MODEL)
  const [applying, setApplying] = useState(false)

  const flags = data?.flags || []
  const eff = data?.effective || {}
  const ov = data?.overrides || {}
  const activeModel = data?.active_model || ''
  const models = data?.models || []

  const byGroup = useMemo(() => {
    const g = {}
    flags.forEach((f) => { (g[f.group] = g[f.group] || []).push(f) })
    return g
  }, [flags])

  const dirtyCount = Object.keys(dirty).length + (modelSel != null && modelSel !== activeModel ? 1 : 0)

  const setFlag = (key, value) => setDirty((d) => ({ ...d, [key]: value }))
  const resetFlag = (key) => setDirty((d) => ({ ...d, [key]: null }))

  // Current displayed value for a flag: pending edit wins; reset→baseline default;
  // else the effective value; else the flag default.
  const flagValue = (f) => {
    if (f.key in dirty) {
      return dirty[f.key] === null ? (f.default ?? '') : dirty[f.key]
    }
    return eff[f.key] !== undefined ? eff[f.key] : (f.default ?? '')
  }

  const apply = async () => {
    const overrides = { ...dirty }
    if (modelSel != null && modelSel !== activeModel) overrides.LLAMACPP_MODEL = modelSel
    if (!Object.keys(overrides).length) { toast('No changes to apply'); return }
    if (!confirm('Apply & restart?\n\nThe effective config is rendered to .env and llamacpp is recreated (+ model-gateway when context changes). Brief downtime.')) return
    setApplying(true)
    try {
      const d = await api.post('/api/model-config', { confirm: true, overrides })
      toast(`Applied — recreating ${(d.recreated || []).join(', ') || 'llamacpp'}`, 'success')
      setDirty({})
      setModelSel(null)
      reload()
    } catch (e) {
      toast(`Apply failed: ${e.message || e}`, 'error')
    } finally {
      setApplying(false)
    }
  }

  const renderInput = (f) => {
    const val = flagValue(f)
    const common = { className: INPUT + ' min-w-0 flex-1', disabled: applying, 'data-key': f.key }
    if (f.kind === 'enum' && f.choices) {
      return (
        <select {...common} value={String(val ?? '')} onChange={(e) => setFlag(f.key, e.target.value)}>
          {f.choices.map((c) => <option key={String(c)} value={String(c)}>{String(c)}</option>)}
        </select>
      )
    }
    if (f.kind === 'bool') {
      return (
        <select {...common} value={String(val) === '1' ? '1' : '0'} onChange={(e) => setFlag(f.key, e.target.value)}>
          <option value="1">on</option>
          <option value="0">off</option>
        </select>
      )
    }
    const type = (f.kind === 'int' || f.kind === 'float') ? 'number' : 'text'
    return (
      <input
        {...common}
        type={type}
        value={val == null ? '' : String(val)}
        onChange={(e) => setFlag(f.key, e.target.value)}
      />
    )
  }

  return (
    <section className={SECTION}>
      <h2 className={H2}>Model Control</h2>
      <p className={DESC}>
        All llama.cpp launch parameters as feature flags. Baseline = <code className="text-accent-soft">.env</code> defaults;
        per-model overrides live in the registry. "Apply &amp; restart" renders the effective config to
        <code className="mx-1 text-accent-soft">.env</code> and recreates llamacpp (+ model-gateway when context changes).
      </p>

      {error ? (
        <div className={WARN_BANNER} role="status">Failed to load model config ({error.status || 'error'}).</div>
      ) : loading && !data ? (
        <div className="space-y-2"><div className="skeleton h-[0.9rem]" /><div className="skeleton h-[0.9rem] w-2/3" /></div>
      ) : (
        <>
          {/* Active model + Apply */}
          <div className="mb-5 flex flex-wrap items-center gap-3 rounded-md border border-border-subtle bg-bg-elevated p-4">
            <span className="text-[0.8125rem] font-semibold text-fg">Active model</span>
            <select
              className={INPUT + ' min-w-[14rem]'}
              value={modelSel ?? activeModel}
              disabled={applying}
              onChange={(e) => setModelSel(e.target.value)}
              aria-label="Active llama.cpp model"
            >
              {models.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            <button type="button" className={BTN} disabled={applying} onClick={apply}>
              {applying ? 'Applying…' : 'Apply & restart'}
            </button>
            <span className="text-[0.75rem] text-muted" aria-live="polite">
              {dirtyCount ? `${dirtyCount} change${dirtyCount > 1 ? 's' : ''} pending` : 'no changes'}
            </span>
          </div>

          {/* Flag groups */}
          {MC_GROUPS.map(([g, label]) => {
            const fs = (byGroup[g] || []).filter((f) => f.key !== 'LLAMACPP_MODEL')
            if (!fs.length) return null
            return (
              <div key={g} className="mb-5">
                <div className="mb-2 text-[0.7rem] font-semibold uppercase tracking-[0.05em] text-muted">{label}</div>
                <div className="flex flex-col gap-1.5">
                  {fs.map((f) => {
                    const overridden = (f.key in ov) || (f.key in dirty && dirty[f.key] !== null) ||
                      (g === 'mtp' && eff.MTP_ENABLED === '1')
                    const shortKey = f.key.replace('LLAMACPP_', '').toLowerCase()
                    return (
                      <div key={f.key} className="flex items-center gap-2 rounded-md border border-border-subtle bg-bg-elevated px-3 py-2">
                        <span
                          className="flex-[0_0_13rem] font-mono text-[0.75rem] text-fg"
                          title={f.help || ''}
                        >
                          {shortKey}{f.help && <span className="ml-1 cursor-help opacity-55" title={f.help}>ⓘ</span>}
                        </span>
                        {renderInput(f)}
                        <span
                          className={
                            'flex-[0_0_5rem] rounded-full px-2 py-0.5 text-center text-[0.62rem] font-semibold ' +
                            (overridden ? 'bg-accent/20 text-accent' : 'bg-border text-muted')
                          }
                        >
                          {overridden ? 'override' : 'inherited'}
                        </span>
                        <button
                          type="button"
                          className="shrink-0 cursor-pointer border-none bg-transparent text-base text-muted transition-colors hover:text-fg disabled:opacity-40"
                          title="Reset to baseline"
                          aria-label={`Reset ${shortKey} to baseline`}
                          disabled={applying}
                          onClick={() => resetFlag(f.key)}
                        >
                          ↺
                        </button>
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Section 2: model registry + GPU assignment
// ---------------------------------------------------------------------------
function kindBadgeClass(kind) {
  const k = (kind || '').toLowerCase()
  if (k.includes('llm') || k.includes('gguf') || k.includes('chat')) return 'border-accent/30 bg-accent/[0.1] text-accent'
  if (k.includes('diffusion') || k.includes('stable') || k.includes('comfy')) return 'border-fuchsia-400/30 bg-fuchsia-400/[0.1] text-fuchsia-300'
  if (k.includes('embed')) return 'border-success/30 bg-success/[0.1] text-success'
  if (k.includes('tts')) return 'border-warning/30 bg-warning/[0.1] text-warning'
  if (k.includes('stt') || k.includes('whisper')) return 'border-warning/30 bg-warning/[0.1] text-warning'
  return 'border-border text-muted'
}

function RegistryCard() {
  const toast = useToast()
  const { data, error, loading, reload } = useFetch(async () => {
    const [models, gpus] = await Promise.all([
      api.get('/api/registry/models'),
      api.get('/api/registry/gpus'),
    ])
    return { models: models.models || {}, gpus: gpus.gpus || {} }
  }, [])

  const [busy, setBusy] = useState({}) // { modelId: true }
  const models = data?.models || {}
  const gpus = data?.gpus || {}
  const entries = Object.entries(models)
  const gpuEntries = Object.entries(gpus)

  const setActive = async (id) => {
    if (!confirm(`This recreates the ${id} service. Set "${id}" as the active model?`)) return
    setBusy((b) => ({ ...b, [id]: true }))
    try {
      await api.post(`/api/registry/models/${encodeURIComponent(id)}/enable`, { confirm: true })
      toast(`${id} set active — service recreating`, 'success')
      reload()
    } catch (e) {
      toast(`Failed to set active: ${e.message || e}`, 'error')
    } finally {
      setBusy((b) => { const n = { ...b }; delete n[id]; return n })
    }
  }

  const assignGpu = async (id, gpuUuid, current) => {
    if (!gpuUuid || gpuUuid === (current || '')) return
    if (!confirm(`Assign GPU to model "${id}"? This recreates the service (brief downtime).`)) { reload(); return }
    setBusy((b) => ({ ...b, [id]: true }))
    try {
      await api.post(`/api/registry/models/${encodeURIComponent(id)}/assign-gpu`, { gpu_uuid: gpuUuid, confirm: true })
      toast(`GPU assigned to ${id} — service recreating`, 'success')
      reload()
    } catch (e) {
      const msg = e.status === 409 ? `GPU capacity conflict: ${e.message}` : `Assignment failed: ${e.message || e}`
      toast(msg, 'error')
      reload()
    } finally {
      setBusy((b) => { const n = { ...b }; delete n[id]; return n })
    }
  }

  const TH = 'px-3 py-2 text-left text-[0.65rem] font-semibold uppercase tracking-[0.08em] text-muted'
  const TD = 'px-3 py-2.5 align-middle text-[0.8rem] text-fg-muted'

  return (
    <section className={SECTION}>
      <h2 className={H2}>Model Registry</h2>
      <p className={DESC}>
        All registered models: kind, service, source file, GPU assignment, and enable state.
        Reassigning GPU or setting active recreates the service (brief downtime).
      </p>

      {error ? (
        <div className={WARN_BANNER} role="status">Failed to load model registry ({error.status || 'error'}).</div>
      ) : loading && !data ? (
        <div className="space-y-2"><div className="skeleton h-[0.9rem]" /><div className="skeleton h-[0.9rem] w-3/4" /></div>
      ) : entries.length === 0 ? (
        <div className="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-[0.8125rem] text-muted">
          No models registered yet. <code className="text-accent-soft">POST /api/registry/models</code> to define one.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border-subtle">
          <table className="w-full border-collapse text-left">
            <thead className="border-b border-border-subtle bg-bg-elevated">
              <tr>
                <th className={TH}>Model ID</th>
                <th className={TH}>Kind</th>
                <th className={TH}>Service</th>
                <th className={TH}>Source file</th>
                <th className={TH}>Status</th>
                <th className={TH}>GPU assignment</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([id, m]) => {
                const srcFile = String((m.source && m.source.file) || m.source_file || 'n/a')
                const srcShort = srcFile.length > 42 ? '…' + srcFile.slice(-42) : srcFile
                const isMulti = m.runtime === 'multi-model'
                const rowBusy = !!busy[id]
                const curGpu = m.gpu_uuid || ''
                return (
                  <tr key={id} className="border-b border-border-subtle last:border-0 hover:bg-white/[0.02]">
                    <td className={TD}><code className="text-[0.78rem] text-fg">{id}</code></td>
                    <td className={TD}>
                      <span className={'inline-flex items-center rounded-full border px-2 py-0.5 text-[0.65rem] font-semibold ' + kindBadgeClass(m.kind)}>
                        {m.kind || 'n/a'}
                      </span>
                    </td>
                    <td className={TD + ' text-[0.75rem]'}>{m.service || 'n/a'}</td>
                    <td className={TD}><span className="font-mono text-[0.72rem]" title={srcFile}>{srcShort}</span></td>
                    <td className={TD}>
                      {isMulti ? (
                        <span className={'inline-flex items-center rounded-full border px-2 py-0.5 text-[0.65rem] font-semibold ' + (m.enabled ? 'border-success/30 bg-success/[0.1] text-success' : 'border-border text-muted')}>
                          {m.enabled ? 'Enabled' : 'Disabled'}
                        </span>
                      ) : m.enabled ? (
                        <span className="inline-flex items-center rounded-full border border-success/30 bg-success/[0.1] px-2 py-0.5 text-[0.65rem] font-semibold text-success">Active</span>
                      ) : (
                        <button type="button" className={BTN + ' h-7 px-2.5 text-[0.72rem]'} disabled={rowBusy} title="Set active model (recreates service)" onClick={() => setActive(id)}>
                          Set active
                        </button>
                      )}
                    </td>
                    <td className={TD}>
                      <select
                        className={INPUT + ' h-8 min-w-[12rem] text-[0.75rem]'}
                        value={curGpu}
                        disabled={rowBusy}
                        aria-label={`GPU assignment for ${id}`}
                        onChange={(e) => assignGpu(id, e.target.value, curGpu)}
                      >
                        <option value="">None</option>
                        {gpuEntries.map(([uuid, g]) => (
                          <option key={uuid} value={uuid}>{(g.name || uuid)} ({g.total_gb ?? '?'} GB)</option>
                        ))}
                      </select>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export default function ModelctlTab() {
  return (
    <>
      <ModelConfigCard />
      <RegistryCard />
    </>
  )
}
