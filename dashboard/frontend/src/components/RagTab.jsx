// RAG status tab — port of the legacy loadRagStatus panel. Consumes GET /api/rag/status
// ({ok, collection, points_count, status} | {ok:false, error}) and renders a single status
// card (collection · points · health badge) plus a short note on how to ingest documents.
// Polls every 30s (usePolling pauses while the tab is hidden). Read-only — ingestion happens
// out-of-band (drop files into the vault / Open WebUI Documents), so there is no action here.
import { api, usePolling } from '../api.js'

// Health-word → badge kind, matching the legacy regex classification.
function statusBadge(status) {
  const s = String(status || '').toLowerCase()
  if (/green|ok|ready|healthy/.test(s)) return 'border-success/30 bg-success/10 text-success'
  if (/red|error|down|fail|unreach/.test(s)) return 'border-danger/30 bg-danger/10 text-danger'
  return 'border-warning/30 bg-warning/10 text-warning'
}

const BADGE = 'inline-flex items-center rounded-full border px-2.5 py-0.5 text-[0.7rem] font-semibold uppercase tracking-[0.06em]'

export default function RagTab() {
  const { data, error } = usePolling(() => api.get('/api/rag/status'), 30000)

  const ok = data?.ok === true
  const status = data?.status != null ? String(data.status) : 'unknown'

  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        RAG
      </h2>
      <p className="mb-5 text-[0.8125rem] leading-[1.5] text-muted">
        Qdrant vector store backing retrieval-augmented generation. Documents are embedded
        and indexed into a collection that chat consumers query for grounded answers.
      </p>

      {error && !data ? (
        <div className="flex items-center gap-2 rounded-sm border border-border-subtle border-l-[3px] border-l-danger bg-danger/[0.06] px-4 py-3 text-[0.8125rem] font-medium" role="status">
          Could not load RAG status — check that the dashboard API is up.
        </div>
      ) : !data ? (
        <div className="skeleton h-24 w-full" />
      ) : ok ? (
        <div className="rounded-md border border-border-subtle border-l-[3px] border-l-success bg-bg-elevated p-5">
          <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
            <div>
              <span className="block text-[0.6rem] font-bold uppercase tracking-[0.12em] text-muted">Collection</span>
              <code className="mt-1 block font-mono text-[0.85rem] text-accent-soft">{data.collection || 'documents'}</code>
            </div>
            <div>
              <span className="block text-[0.6rem] font-bold uppercase tracking-[0.12em] text-muted">Points indexed</span>
              <span className="mt-1 block font-mono text-[0.95rem] font-semibold tabular-nums text-fg">
                {(data.points_count ?? 0).toLocaleString()}
              </span>
            </div>
            <div>
              <span className="block text-[0.6rem] font-bold uppercase tracking-[0.12em] text-muted">Status</span>
              <span className={`mt-1 ${BADGE} ${statusBadge(status)}`}>{status}</span>
            </div>
          </div>
          {(data.points_count ?? 0) === 0 && (
            <p className="mt-4 border-t border-border-subtle pt-3 text-[0.8125rem] leading-[1.55] text-fg-muted">
              The collection is empty. Add documents (see below) and the ingestion pipeline
              will embed and index them here.
            </p>
          )}
        </div>
      ) : (
        <div className="rounded-md border border-border-subtle border-l-[3px] border-l-warning bg-warning/[0.04] p-5 text-[0.8125rem] leading-[1.6] text-fg-muted">
          Qdrant unreachable or RAG not running.
          {data.error && <span className="mt-1 block font-mono text-[0.75rem] text-danger">{data.error}</span>}
          <span className="mt-2 block">
            Start with <code className="rounded-sm border border-border-subtle bg-bg px-1.5 py-0.5 text-accent-soft">docker compose --profile rag up -d</code> and ensure Qdrant is healthy.
          </span>
        </div>
      )}

      {/* How to ingest */}
      <div className="mt-6 border-t border-border-subtle pt-5">
        <h3 className="mb-2 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">How to add documents</h3>
        <ul className="ml-4 list-disc space-y-1.5 text-[0.8125rem] leading-[1.55] text-fg-muted">
          <li>Drop files into the memory vault — the ingestion pipeline embeds and indexes them automatically.</li>
          <li>Upload through <span className="font-medium text-fg">Open WebUI → Documents</span> to add sources to the collection.</li>
        </ul>
      </div>
    </section>
  )
}
