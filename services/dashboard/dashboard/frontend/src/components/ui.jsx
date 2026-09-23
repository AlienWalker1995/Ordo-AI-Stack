// Shared building blocks for every page: panels, status marks, meters, buttons, the side
// drawer and the sparkline. All colour comes from the Tailwind tokens (tailwind.config.js).
import { useEffect, useRef } from 'react'

export const BTN =
  'inline-flex h-8 items-center justify-center gap-1.5 whitespace-nowrap rounded-sm border border-border bg-surface px-3 text-label text-fg transition-colors hover:border-accent/40 hover:bg-accent/[0.07] hover:text-accent disabled:cursor-not-allowed disabled:opacity-40'
export const BTN_DANGER =
  'inline-flex h-8 items-center justify-center gap-1.5 whitespace-nowrap rounded-sm border border-border bg-surface px-3 text-label text-fg transition-colors hover:border-danger/50 hover:bg-danger/10 hover:text-danger disabled:cursor-not-allowed disabled:opacity-40'
export const BTN_PRIMARY =
  'inline-flex h-8 items-center justify-center gap-1.5 whitespace-nowrap rounded-sm border border-accent/50 bg-accent/[0.12] px-3 text-label font-semibold text-accent-soft transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40'
export const INPUT =
  'h-8 rounded-sm border border-border bg-bg px-3 text-body text-fg outline-none transition-colors focus:border-accent/60 disabled:cursor-not-allowed disabled:opacity-40'

// A titled block. `level` separates page sections (panel) from cards inside them.
export function Panel({ title, action, children, className = '' }) {
  return (
    <section className={'rounded-md border border-border-subtle bg-bg-elevated p-4 ' + className}>
      {(title || action) && (
        <header className="mb-3 flex items-center justify-between gap-3">
          {title && <h2 className="text-micro font-bold uppercase tracking-[0.14em] text-muted">{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

const DOT_CLASS = {
  ok: 'bg-success',
  up: 'bg-success',
  done: 'bg-success/60',
  info: 'bg-accent',
  starting: 'bg-warning',
  warning: 'bg-warning',
  unhealthy: 'bg-danger',
  failed: 'bg-danger',
  critical: 'bg-danger',
  stopped: 'bg-muted/60',
  unknown: 'bg-muted/60',
}

export function Dot({ tone = 'unknown', label }) {
  return (
    <span className="inline-flex items-center">
      <span className={'h-2 w-2 shrink-0 rounded-full ' + (DOT_CLASS[tone] || DOT_CLASS.unknown)} aria-hidden="true" />
      {label && <span className="sr-only">{label}</span>}
    </span>
  )
}

const CHIP_CLASS = {
  neutral: 'border-border text-fg',
  accent: 'border-accent/40 bg-accent/[0.08] text-accent-soft',
  warning: 'border-warning/40 bg-warning/[0.08] text-warning',
  danger: 'border-danger/40 bg-danger/[0.08] text-danger',
  success: 'border-success/40 bg-success/[0.08] text-success',
}

export function Chip({ tone = 'neutral', children, title }) {
  return (
    <span title={title} className={'inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-caption font-semibold ' + (CHIP_CLASS[tone] || CHIP_CLASS.neutral)}>
      {children}
    </span>
  )
}

// A thin meter. `tone` is the fill colour; the track stays neutral.
export function Meter({ value, tone = 'accent', label }) {
  const fill = { accent: 'bg-accent', warning: 'bg-warning', danger: 'bg-danger', muted: 'bg-muted' }[tone] || 'bg-accent'
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-border-subtle" role="meter" aria-label={label}
         aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value || 0)}>
      <div className={'h-full rounded-full transition-[width] duration-500 ' + fill} style={{ width: `${Math.max(0, Math.min(100, value || 0))}%` }} />
    </div>
  )
}

// Stated plainly instead of drawing zeros: a dependency that did not answer is not "0".
export function Unavailable({ children }) {
  return (
    <div role="status" className="rounded-sm border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] px-3 py-2 text-body text-fg-muted">
      {children}
    </div>
  )
}

export function Skeleton({ className = 'h-4 w-full' }) {
  return <span className={'skeleton block ' + className} aria-hidden="true" />
}

// Sparkline of [[epochSeconds, value], ...]: area fill, emphasised endpoint, scaled to its own max.
export function Sparkline({ points, tone = 'accent', label, height = 32 }) {
  const stroke = { accent: 'text-accent', warning: 'text-warning', success: 'text-success' }[tone] || 'text-accent'
  if (!points || points.length < 2) {
    return <div className="flex items-end text-caption text-muted" style={{ height }}>No samples yet</div>
  }
  const w = 200
  const h = height
  const xs = points.map((p) => p[0])
  const ys = points.map((p) => p[1])
  const x0 = Math.min(...xs)
  const x1 = Math.max(...xs)
  const yMax = Math.max(...ys, 0.0001)
  const px = (t) => ((t - x0) / Math.max(1, x1 - x0)) * w
  const py = (v) => h - 2 - (v / yMax) * (h - 6)
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${px(p[0]).toFixed(1)} ${py(p[1]).toFixed(1)}`).join(' ')
  const last = points[points.length - 1]
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className={'block w-full ' + stroke} style={{ height }} role="img" aria-label={label}>
      <path d={`${line} L${w} ${h} L0 ${h} Z`} fill="currentColor" opacity="0.14" />
      <path d={line} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      <circle cx={px(last[0])} cy={py(last[1])} r="2.2" fill="currentColor" />
    </svg>
  )
}

// A right-hand drawer: Escape closes it, focus moves in on open and returns on close.
export function Drawer({ open, title, onClose, children, width = 'max-w-2xl' }) {
  const panelRef = useRef(null)
  const returnTo = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    returnTo.current = document.activeElement
    panelRef.current?.focus()
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      returnTo.current?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-[400] flex justify-end bg-black/50" onClick={onClose}>
      <aside
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={'flex h-full w-full flex-col border-l border-border bg-bg-elevated shadow-card-lg outline-none ' + width}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between gap-3 border-b border-border-subtle px-5 py-3">
          <h2 className="text-title text-fg">{title}</h2>
          <button type="button" className={BTN} onClick={onClose} aria-label="Close">Close</button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </aside>
    </div>
  )
}
