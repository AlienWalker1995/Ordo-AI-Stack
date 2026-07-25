// Placeholder for tabs not yet ported from the legacy vanilla-JS shell. Each of these
// is a seam a later iteration fills; the legacy dashboard remains available at
// /legacy-index.html for the full, unported functionality in the meantime.

export default function StubTab({ title, endpoints = [], note }) {
  return (
    <section className="flex">
      <div className="w-full max-w-[640px] rounded-lg border border-border bg-card p-8 shadow-card">
        <h2 className="mb-3 font-display text-[1.5625rem] font-bold uppercase tracking-[0.03em] text-fg">
          {title}
        </h2>
        <p className="mb-4 inline-flex items-center rounded-full border border-warning/20 bg-warning/[0.08] px-3 py-0.5 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-warning">
          Coming in a later iteration
        </p>
        <p className="mb-4 text-[0.8125rem] leading-[1.65] text-fg-muted">
          {note || 'This panel is being ported to React. Until then, use the legacy dashboard.'}
        </p>
        {endpoints.length > 0 && (
          <div className="mb-5">
            <span className="mb-2 block text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-muted">
              Backend endpoints this tab will consume:
            </span>
            <ul className="flex list-none flex-wrap gap-2">
              {endpoints.map((e) => (
                <li key={e}>
                  <code className="rounded-sm border border-border-subtle bg-bg px-2 py-0.5 text-[0.7rem] text-accent-soft">
                    {e}
                  </code>
                </li>
              ))}
            </ul>
          </div>
        )}
        <a
          className="inline-flex items-center justify-center rounded-sm border border-border bg-surface px-5 py-2 text-[0.8125rem] font-semibold text-fg no-underline transition-colors hover:border-accent/30 hover:bg-accent/[0.07] hover:text-accent"
          href="/legacy-index.html"
        >
          Open legacy dashboard
        </a>
      </div>
    </section>
  )
}

export const HardwareTab = () => (
  <StubTab title="Hardware / GPU Detail" endpoints={['/api/hardware', '/api/hardware/service-pressure', '/api/gpu/*']} />
)
export const ThroughputTab = () => (
  <StubTab title="Throughput" endpoints={['/api/throughput/stats', '/api/throughput/service-usage']} />
)
export const OrchestrationTab = () => (
  <StubTab title="Orchestration / Jobs" endpoints={['/api/orchestration', '/api/jobs', '/api/orchestration/readiness']} />
)
export const RagTab = () => (
  <StubTab title="RAG Status" endpoints={['/api/rag/status']} />
)
export const ComfyTab = () => (
  <StubTab title="ComfyUI Models & Packs" endpoints={['/api/comfyui/*']} />
)
