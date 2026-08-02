// MCP tab — full port of the legacy "MCP Gateway" panel. Consumes /api/mcp/servers
// (enabled + catalog + dynamic flag) and /api/mcp/health (gateway + per-server status),
// and drives /api/mcp/add + /api/mcp/remove. Behaviour preserved from the vanilla shell:
//   - a gateway status badge (ok / unreachable / unknown),
//   - enabled servers as chips, each with a live per-server health dot
//     (green ok / yellow degraded / red fail) and a remove (×) button,
//   - add from catalog (servers not already enabled) OR by free-text name/URL,
//   - poll health every 15s (paused when hidden).
// Add/remove surface the backend's persist result as a toast: "persisted to ordo.yaml"
// when {persistent:true}, otherwise the live-only {note} explaining it won't survive a
// re-render. When {dynamic:false} the add/remove controls are disabled with a hint that
// the config isn't mounted read-write (matching the legacy static-mode fallback).
import { useState } from 'react'
import { api, usePolling } from '../api.js'
import { useToast } from './Toast.jsx'

// Per-server dot: green when running, yellow when degraded (reachable-but-not-ok),
// red when hard-failed/unreachable. Mirrors the legacy chip-status logic exactly.
function serverDotClass(info) {
  if (!info) return ''
  if (info.ok) return 'ok'
  const status = info.status || info.error || 'unknown'
  const degraded = status !== 'unknown' && status !== 'unreachable'
  return degraded ? 'pending' : 'fail'
}

function serverTitle(info) {
  if (!info) return 'no health data'
  return info.status || (info.ok ? 'running' : info.error || 'unknown')
}

export default function McpTab() {
  const toast = useToast()
  const [customInput, setCustomInput] = useState('')
  const [selectValue, setSelectValue] = useState('')
  const [busy, setBusy] = useState(false)

  // One poller drives the server list + gateway/per-server health (15s, paused when hidden).
  const { data, error, refresh } = usePolling(async () => {
    const [servers, health] = await Promise.all([
      api.get('/api/mcp/servers'),
      api.get('/api/mcp/health').catch(() => null),
    ])
    return { servers, health }
  }, 15000)

  const enabled = data?.servers?.enabled || []
  const catalog = data?.servers?.catalog || []
  const dynamic = data?.servers?.dynamic === true
  const health = data?.health || null
  const healthById = {}
  ;(health?.servers || []).forEach((s) => {
    healthById[s.id] = s
    const tail = s.id.split('/').pop()
    if (!(tail in healthById)) healthById[tail] = s
  })
  const addable = catalog.filter((s) => !enabled.includes(s))

  // Toast helper: turn the backend's {status, persistent, note} into the right message.
  const surfaceToggle = (verb, server, res) => {
    if (res.status === 'already_enabled') { toast(`${server} already enabled`); return }
    if (res.status === 'already_removed') { toast(`${server} already removed`); return }
    if (res.persistent) {
      toast(`${server} ${verb} — persisted to ordo.yaml`, 'success')
    } else {
      toast(res.note || `${server} ${verb} — live only (will not survive a re-render)`, 'success')
    }
  }

  const addServer = async (server) => {
    const v = (server || '').trim()
    if (!v) { toast('Enter a server name (e.g. hugging-face) or paste a Docker Hub URL', 'error'); return false }
    setBusy(true)
    try {
      const res = await api.post('/api/mcp/add', { server: v })
      surfaceToggle('enabled', v, res)
      refresh()
      return true
    } catch (e) {
      toast(e.message || 'Failed to add', 'error')
      return false
    } finally {
      setBusy(false)
    }
  }

  const removeServer = async (server) => {
    setBusy(true)
    try {
      const res = await api.post('/api/mcp/remove', { server })
      surfaceToggle('removed', server, res)
      refresh()
    } catch (e) {
      toast(e.message || 'Failed to remove', 'error')
    } finally {
      setBusy(false)
    }
  }

  const onAddFromCatalog = async () => {
    if (!selectValue) { toast('Choose a tool from the dropdown or add by name below', 'error'); return }
    if (await addServer(selectValue)) setSelectValue('')
  }
  const onAddCustom = async () => {
    if (await addServer(customInput)) setCustomInput('')
  }

  const gatewayBadge = (() => {
    if (!health) return { cls: 'border-border text-muted', label: '—', title: 'Gateway status unknown' }
    if (health.ok) return { cls: 'border-success/30 bg-success/[0.08] text-success', label: 'gateway ok', title: 'Gateway reachable' }
    return { cls: 'border-danger/30 bg-danger/10 text-danger', label: 'gateway unreachable', title: health.gateway_error || 'Gateway unreachable' }
  })()

  const INPUT =
    'h-9 rounded-sm border border-border bg-bg px-3 text-[0.8125rem] text-fg outline-none transition-colors focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-40'
  const BTN =
    'inline-flex h-9 items-center justify-center whitespace-nowrap rounded-sm border border-border bg-surface px-4 text-[0.8125rem] font-medium tracking-[0.02em] text-fg transition-all hover:border-accent/30 hover:bg-accent/[0.07] hover:text-accent disabled:cursor-not-allowed disabled:opacity-40'
  const LABEL = 'mb-2 block text-label text-muted'

  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        MCP Gateway
      </h2>
      <p className="mb-4 text-[0.8125rem] leading-[1.5] text-muted">
        Shared tools for Open WebUI, N8N, and Cursor. Add or remove servers below —
        changes hot-reload on the gateway in ~10s (no container restart).
      </p>

      {error && !data ? (
        <div className="flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] font-medium" role="status">
          Could not load MCP servers — check that the dashboard API is up.
        </div>
      ) : (
        <>
          {/* Enabled servers + gateway status */}
          <div className="mb-6">
            <div className={LABEL + ' flex items-center gap-2'}>
              <span>Enabled tools</span>
              <span
                className={'inline-flex items-center rounded-full border px-2 py-0.5 text-[0.65rem] font-semibold normal-case tracking-normal ' + gatewayBadge.cls}
                title={gatewayBadge.title}
              >
                {gatewayBadge.label}
              </span>
            </div>
            <div className="flex min-h-10 flex-wrap items-center gap-2">
              {enabled.length === 0 && !data ? (
                <span className="skeleton h-6 w-40" />
              ) : enabled.length === 0 ? (
                <span className="text-[0.8125rem] italic text-muted">None — add from catalog or paste a URL below</span>
              ) : (
                enabled.map((s) => {
                  const info = healthById[s] || healthById[s.split('/').pop()]
                  return (
                    <span
                      key={s}
                      className="inline-flex items-center gap-2 rounded-sm border border-border bg-surface py-1 pl-2.5 pr-1.5 text-[0.8125rem] text-fg transition-colors hover:border-accent/30"
                      title={s}
                    >
                      <span className={`status-dot ${serverDotClass(info)}`.trim()} aria-hidden="true" title={serverTitle(info)} />
                      <span className="max-w-[16rem] truncate">{s}</span>
                      <button
                        type="button"
                        className="ml-0.5 inline-flex h-5 w-5 items-center justify-center rounded-sm border border-transparent text-muted transition-colors hover:border-danger/40 hover:bg-danger/10 hover:text-danger disabled:cursor-not-allowed disabled:opacity-40"
                        aria-label={`Remove ${s}`}
                        title={dynamic ? `Remove ${s}` : 'Read-only mode — cannot remove'}
                        disabled={!dynamic || busy}
                        onClick={() => removeServer(s)}
                      >
                        ×
                      </button>
                    </span>
                  )
                })
              )}
            </div>
          </div>

          {/* Add controls */}
          {dynamic ? (
            <div className="space-y-5 border-t border-border-subtle pt-5">
              <div>
                <label className={LABEL} htmlFor="mcp-add-select">Add from catalog</label>
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    id="mcp-add-select"
                    className={INPUT + ' min-w-[14rem] flex-1'}
                    value={selectValue}
                    disabled={busy}
                    onChange={(e) => setSelectValue(e.target.value)}
                  >
                    <option value="">Choose a tool…</option>
                    {addable.map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                  <button type="button" className={BTN} disabled={busy || !selectValue} onClick={onAddFromCatalog}>Add</button>
                </div>
              </div>

              <div>
                <label className={LABEL} htmlFor="mcp-custom-input">Or add by name or URL</label>
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    id="mcp-custom-input"
                    type="text"
                    className={INPUT + ' min-w-[14rem] flex-1'}
                    placeholder="e.g. hugging-face, mcp/firecrawl, or a Docker Hub URL"
                    aria-label="Custom MCP server name or Docker Hub URL"
                    value={customInput}
                    disabled={busy}
                    onChange={(e) => setCustomInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); onAddCustom() } }}
                  />
                  <button type="button" className={BTN} disabled={busy || !customInput.trim()} onClick={onAddCustom}>Add</button>
                </div>
              </div>
            </div>
          ) : (
            <div className="border-t border-border-subtle pt-5">
              <div className="rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-4 py-3 text-[0.8125rem] leading-[1.5] text-fg-muted">
                MCP config isn't mounted read-write (static mode) — add/remove is disabled here.
                Manage servers from the repo root:{' '}
                <code className="rounded-sm border border-border-subtle bg-bg px-1.5 py-0.5 text-[0.75rem] text-accent-soft">./scripts/mcp_add.sh &lt;server&gt;</code>
                {' / '}
                <code className="rounded-sm border border-border-subtle bg-bg px-1.5 py-0.5 text-[0.75rem] text-accent-soft">./scripts/mcp_remove.sh &lt;server&gt;</code>
              </div>
            </div>
          )}

          <div className="mt-6 border-t border-border-subtle pt-4 text-[0.8125rem] leading-[1.6] text-muted">
            <p>
              Connect: Open WebUI → Admin Settings → External Tools → MCP (Streamable HTTP).{' '}
              <a href="https://hub.docker.com/mcp" target="_blank" rel="noopener">Browse 200+ tools in the Docker MCP Catalog ↗</a>
            </p>
          </div>
        </>
      )}
    </section>
  )
}
