// Command palette (Ctrl/Cmd+K): jump to a page, open a tool, or open settings. Keyboard first:
// arrows move, Enter runs, Escape closes. Services and tool links come from the live overview.
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'

export default function CommandPalette({ open, onClose, pages, onNavigate, onOpenSettings }) {
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const [links, setLinks] = useState([])
  const inputRef = useRef(null)
  const returnTo = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    returnTo.current = document.activeElement
    setQuery('')
    setIndex(0)
    inputRef.current?.focus()
    let cancelled = false
    api.get('/api/overview').then((o) => { if (!cancelled) setLinks(o.links || []) }).catch(() => {})
    return () => {
      cancelled = true
      returnTo.current?.focus?.()
    }
  }, [open])

  const commands = useMemo(() => [
    ...pages.map((p) => ({ id: `page-${p.id}`, label: `Go to ${p.label}`, hint: 'Page', run: () => onNavigate(p.id) })),
    { id: 'settings', label: 'Open settings', hint: 'Settings', run: onOpenSettings },
    ...links.map((l) => ({ id: `link-${l.url}`, label: `Open ${l.name}`, hint: 'Tool', run: () => window.open(l.url, '_blank', 'noopener') })),
  ], [pages, links, onNavigate, onOpenSettings])

  const q = query.trim().toLowerCase()
  const matches = q ? commands.filter((c) => c.label.toLowerCase().includes(q)) : commands
  const active = Math.min(index, Math.max(0, matches.length - 1))

  if (!open) return null

  const runAt = (i) => {
    const cmd = matches[i]
    if (!cmd) return
    onClose()
    cmd.run()
  }

  const onKeyDown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); onClose() } else if (e.key === 'ArrowDown') {
      e.preventDefault(); setIndex((i) => Math.min(i + 1, matches.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); setIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault(); runAt(active)
    }
  }

  return (
    <div className="fixed inset-0 z-[450] flex items-start justify-center bg-black/50 px-4 pt-[12vh]" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label="Command palette"
           className="w-full max-w-lg overflow-hidden rounded-md border border-border bg-bg-elevated shadow-card-lg"
           onClick={(e) => e.stopPropagation()}>
        <label htmlFor="palette-input" className="sr-only">Search pages, tools and actions</label>
        <input id="palette-input" ref={inputRef} value={query} onKeyDown={onKeyDown}
               onChange={(e) => { setQuery(e.target.value); setIndex(0) }}
               placeholder="Search pages, tools and actions"
               role="combobox" aria-expanded="true" aria-controls="palette-list"
               aria-activedescendant={matches[active] ? `palette-${matches[active].id}` : undefined}
               className="h-11 w-full border-b border-border-subtle bg-transparent px-4 text-body text-fg outline-none placeholder:text-muted" />
        <ul id="palette-list" role="listbox" className="max-h-80 overflow-y-auto py-1">
          {matches.length === 0 && <li className="px-4 py-3 text-body text-muted">No matches</li>}
          {matches.map((c, i) => (
            <li key={c.id} id={`palette-${c.id}`} role="option" aria-selected={i === active}
                className={'flex cursor-pointer items-center justify-between gap-3 px-4 py-2 text-body ' +
                  (i === active ? 'bg-accent/[0.10] text-fg' : 'text-fg-muted')}
                onMouseEnter={() => setIndex(i)} onClick={() => runAt(i)}>
              <span>{c.label}</span>
              <span className="text-micro text-muted">{c.hint}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
