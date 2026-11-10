// The colour theme: Auto follows the OS and switches when it does, a pinned choice survives a
// reload, and the Grafana embed follows the theme the page shows.
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import PerformancePage from '../src/pages/PerformancePage.jsx'
import { ThemeProvider, THEME_CHOICES, useTheme } from '../src/lib/theme.jsx'
import { mockApi } from './mockApi.js'

// A controllable prefers-color-scheme: `os.light` is what the OS says; `os.flip()` changes it.
function fakeOs(light) {
  const listeners = new Set()
  const os = {
    light,
    flip() {
      os.light = !os.light
      listeners.forEach((fn) => fn())
    },
  }
  vi.stubGlobal('matchMedia', (query) => ({
    get matches() { return query.includes('light') && os.light },
    addEventListener: (_type, fn) => listeners.add(fn),
    removeEventListener: (_type, fn) => listeners.delete(fn),
  }))
  return os
}

function Picker() {
  const { choice, setChoice, resolved } = useTheme()
  return (
    <>
      <select aria-label="Colour theme" value={choice} onChange={(e) => setChoice(e.target.value)}>
        {THEME_CHOICES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
      </select>
      <output aria-label="resolved">{resolved}</output>
    </>
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  delete document.documentElement.dataset.theme
})

describe('the colour theme', () => {
  it('follows the OS on Auto, including when the OS switches', () => {
    const os = fakeOs(true)
    render(<ThemeProvider><Picker /></ThemeProvider>)
    expect(document.documentElement.dataset.theme).toBe('light')
    act(() => os.flip())
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(screen.getByLabelText('resolved').textContent).toBe('dark')
  })

  it('keeps a pinned choice across a reload and ignores the OS while pinned', () => {
    const os = fakeOs(true)
    const first = render(<ThemeProvider><Picker /></ThemeProvider>)
    fireEvent.change(screen.getByLabelText('Colour theme'), { target: { value: 'dark' } })
    expect(document.documentElement.dataset.theme).toBe('dark')
    act(() => os.flip())
    act(() => os.flip())
    expect(document.documentElement.dataset.theme).toBe('dark')
    first.unmount()

    render(<ThemeProvider><Picker /></ThemeProvider>)
    expect(screen.getByLabelText('Colour theme').value).toBe('dark')
  })

  it('falls back to Auto when storage holds something else', () => {
    fakeOs(false)
    localStorage.setItem('ordo.theme', 'neon')
    render(<ThemeProvider><Picker /></ThemeProvider>)
    expect(screen.getByLabelText('Colour theme').value).toBe('auto')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('asks Grafana for the theme the page shows', async () => {
    fakeOs(true)
    mockApi()
    render(<ThemeProvider><PerformancePage /></ThemeProvider>)
    const frame = await screen.findByTitle('Ordo performance (Grafana)')
    expect(frame.getAttribute('src')).toContain('theme=light')
    expect(frame.getAttribute('src')).not.toContain('theme=dark')
  })

  it('keeps a one-argument setChoice on the default context, read before the provider mounts', () => {
    // Before any ThemeProvider mounts, consumers read this default value instead of the
    // provider's value. Its setChoice keeps the same one-argument shape as the provider's,
    // so setChoice(e.target.value) both runs and type-checks; a zero-arity stub still runs
    // but loses the argument in the inferred type, which is what broke the tsc gate.
    let ctx
    function Probe() {
      ctx = useTheme()
      return null
    }
    render(<Probe />)
    expect(ctx.choice).toBe('auto')
    expect(ctx.resolved).toBe('dark')
    expect(typeof ctx.setChoice).toBe('function')
    expect(ctx.setChoice.length).toBe(1)
    expect(() => ctx.setChoice('light')).not.toThrow()
  })
})
