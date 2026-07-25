// Thin same-origin fetch wrapper for the dashboard's FastAPI /api/* backend.
//
// The dashboard is served same-origin behind Google SSO (Caddy forward_auth). The
// browser already carries the SSO session, so requests just need `credentials:
// 'same-origin'`; there is NO auth logic here — /api/auth/config returns
// {auth_required:false} when the SSO header is present. CSP connect-src is 'self',
// so every URL below is a root-relative /api/* path (never cross-origin).

import { useCallback, useEffect, useRef, useState } from 'react'

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function request(path, { method = 'GET', body, headers, signal } = {}) {
  const opts = {
    method,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(headers || {}) },
    signal,
  }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = typeof body === 'string' ? body : JSON.stringify(body)
  }

  let res
  try {
    res = await fetch(path, opts)
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError(`Network error contacting ${path}`, 0, null)
  }

  const ct = res.headers.get('content-type') || ''
  const isJson = ct.includes('application/json')
  const payload = isJson ? await res.json().catch(() => null) : await res.text().catch(() => null)

  if (!res.ok) {
    const detail = (payload && (payload.detail || payload.error)) || res.statusText
    throw new ApiError(typeof detail === 'string' ? detail : `HTTP ${res.status}`, res.status, payload)
  }
  return payload
}

export const api = {
  get: (path, opts) => request(path, { ...opts, method: 'GET' }),
  post: (path, body, opts) => request(path, { ...opts, method: 'POST', body }),
  del: (path, opts) => request(path, { ...opts, method: 'DELETE' }),
  request,
}

// One-shot fetch with loading/error state. `deps` re-runs the fetch when they change.
export function useFetch(fetcher, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const reload = useCallback(async (signal) => {
    setLoading(true)
    try {
      const d = await fetcherRef.current(signal)
      if (!signal || !signal.aborted) {
        setData(d)
        setError(null)
      }
    } catch (e) {
      if (e.name !== 'AbortError') setError(e)
    } finally {
      if (!signal || !signal.aborted) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    reload(ctrl.signal)
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading, reload: () => reload() }
}

// Auto-refreshing fetch. Polls `fetcher` every `intervalMs`. Pauses while the tab is
// hidden (no point hammering the backend for an off-screen panel) and resumes on focus.
export function usePolling(fetcher, intervalMs = 5000, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const tick = useCallback(async () => {
    try {
      const d = await fetcherRef.current()
      setData(d)
      setError(null)
    } catch (e) {
      if (e.name !== 'AbortError') setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let timer = null
    let stopped = false
    const schedule = () => {
      if (stopped) return
      timer = setTimeout(async () => {
        if (!document.hidden) await tick()
        schedule()
      }, intervalMs)
    }
    tick()
    schedule()
    const onVis = () => { if (!document.hidden) tick() }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVis)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps])

  return { data, error, loading, refresh: tick }
}
