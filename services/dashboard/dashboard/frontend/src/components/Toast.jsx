// Toast notifications, ported from the legacy `toast(msg, type)` helper: bottom-right
// stack, auto-dismiss after 5s, click to dismiss. type is '' | 'success' | 'error'. An error toast
// for a failed request passes the ApiError's code and trace id (errorMeta) so the operator can
// find it on the Logs page.
import { createContext, useCallback, useContext, useRef, useState } from 'react'

/** @typedef {{ code?: string | null, traceId?: string | null }} ToastMeta */
/** @typedef {(msg: string, type?: '' | 'success' | 'error', meta?: ToastMeta) => void} Toast */

/** @type {import('react').Context<Toast>} */
const ToastContext = createContext(/** @type {Toast} */ (() => {}))

/** The code and trace id a failed request's ApiError carries, for a toast's third argument. */
export function errorMeta(error) {
  return { code: error?.code ?? null, traceId: error?.traceId ?? null }
}

export function useToast() {
  return useContext(ToastContext)
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const idRef = useRef(0)

  const remove = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  const toast = useCallback((msg, type = '', metadata = {}) => {
    const id = ++idRef.current
    setToasts((t) => [...t, { id, msg, type, code: metadata.code, traceId: metadata.traceId }])
    setTimeout(() => remove(id), 5000)
  }, [remove])

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div
        className="fixed bottom-6 right-6 z-[500] flex flex-col gap-2 max-md:inset-x-4 max-md:bottom-4"
        role="region"
        aria-label="Notifications"
        aria-live="polite"
      >
        {toasts.map((t) => (
          // A real button so keyboard users can dismiss it (Enter/Space come free); aria-live is
          // declared once on the container above, not here, to avoid double announcement.
          <button
            key={t.id}
            type="button"
            title="Dismiss"
            aria-label={`Dismiss notification: ${t.msg}`}
            className={
              'min-w-[200px] max-w-[340px] cursor-pointer animate-toast-in rounded-sm border border-l-[3px] border-border bg-surface px-5 py-3 text-left text-[0.8125rem] shadow-card-lg ' +
              (t.type === 'success'
                ? 'border-l-success bg-success/[0.08]'
                : t.type === 'error'
                  ? 'border-l-danger bg-danger/10'
                  : 'border-l-accent')
            }
            onClick={() => remove(t.id)}
          >
            {t.msg}
            {(t.code || t.traceId) && <span className="mt-1 block text-caption font-normal text-muted">
              {[t.code, t.traceId && `Trace ${t.traceId}`].filter(Boolean).join(' · ')}
            </span>}
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
