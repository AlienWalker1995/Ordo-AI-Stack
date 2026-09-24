// Local-mode sign-in. Without the SSO edge there is no operator identity, so the dashboard asks
// for the sign-in token `ordo up` prints (DASHBOARD_LOCAL_LOGIN_TOKEN in out/secrets.env) and
// swaps it for a session cookie. Browsing stays open; the prompt only gates changes. With the
// edge on, /api/auth/session reports mode "edge" and this renders nothing.
import { useCallback, useEffect, useState } from 'react'
import { api, UNAUTHORIZED_EVENT } from '../api.js'
import { BTN_PRIMARY, INPUT } from './ui.jsx'
import { useToast } from './Toast.jsx'

const SIGN_IN_FRAGMENT = /^#sign-in=(.+)$/

// `ordo up` prints http://127.0.0.1:8444/#sign-in=<token>. Read it at module load, before the
// page router rewrites the hash, and drop it from the address bar and the history entry.
function takeFragmentToken() {
  const match = SIGN_IN_FRAGMENT.exec(location.hash || '')
  if (!match) return ''
  history.replaceState(null, '', location.pathname + location.search)
  try {
    return decodeURIComponent(match[1])
  } catch {
    return ''
  }
}

let fragmentToken = takeFragmentToken()

export default function LocalSignIn() {
  const toast = useToast()
  const [needed, setNeeded] = useState(false)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const checkSession = useCallback(async () => {
    try {
      const s = await api.get('/api/auth/session')
      setNeeded(s.mode === 'local' && !s.signed_in)
    } catch {
      setNeeded(false)   // an unreachable backend is reported by the pages themselves
    }
  }, [])

  const signIn = useCallback(async (value) => {
    setBusy(true)
    setError('')
    try {
      await api.post('/api/auth/local/sign-in', { token: value })
      setToken('')
      setNeeded(false)
      toast('Signed in', 'success')
    } catch (e) {
      setError(e.status === 401 ? 'That token does not match this stack.' : e.message)
      setNeeded(true)
    } finally {
      setBusy(false)
    }
  }, [toast])

  useEffect(() => {
    const pending = fragmentToken
    fragmentToken = ''
    if (pending) signIn(pending)
    else checkSession()
    // A refused action (an expired session, a rotated token) re-checks and brings the prompt back.
    window.addEventListener(UNAUTHORIZED_EVENT, checkSession)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, checkSession)
  }, [checkSession, signIn])

  if (!needed) return null

  const onSubmit = (e) => {
    e.preventDefault()
    if (token.trim()) signIn(token.trim())
  }

  return (
    <section aria-labelledby="local-sign-in-title"
             className="mb-5 rounded-md border border-border border-l-[3px] border-l-accent bg-bg-elevated px-4 py-3">
      <h2 id="local-sign-in-title" className="text-heading text-fg">Sign in to make changes</h2>
      <p className="mt-1 text-body text-fg-muted">
        This stack runs without remote access, so there is no Google sign-in. Open the link <code className="font-mono text-caption text-fg">ordo up</code> printed,
        or paste <code className="font-mono text-caption text-fg">DASHBOARD_LOCAL_LOGIN_TOKEN</code> from <code className="font-mono text-caption text-fg">out/secrets.env</code>.
      </p>
      <form className="mt-3 flex flex-wrap items-center gap-2" onSubmit={onSubmit}>
        <label htmlFor="local-sign-in-token" className="sr-only">Sign-in token</label>
        <input id="local-sign-in-token" type="password" autoComplete="off" spellCheck={false}
               className={INPUT + ' w-80 max-w-full font-mono placeholder:font-sans placeholder:text-muted'} placeholder="Sign-in token"
               value={token} onChange={(e) => setToken(e.target.value)} disabled={busy}
               aria-invalid={error ? 'true' : undefined} aria-describedby={error ? 'local-sign-in-error' : undefined} />
        <button type="submit" className={BTN_PRIMARY} disabled={busy || !token.trim()}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      {error && <p id="local-sign-in-error" role="alert" className="mt-2 text-caption text-danger">{error}</p>}
    </section>
  )
}
