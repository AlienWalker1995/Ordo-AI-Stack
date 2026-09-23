// Grafana tab — same-origin iframe embed of the Ordo llama.cpp & GPU dashboard, served
// under /grafana/ (behind the SSO front door). Ported from the legacy loadGrafanaTab():
// embeds the ordo-llm-gpu dashboard in kiosk mode with a 10s refresh, plus an
// "open in new tab" link to the full (non-kiosk) view.
//
// Requires the `monitoring` compose profile. If Grafana isn't running the iframe simply
// fails to load; a static note under the frame explains that gracefully (we can't detect
// a cross-origin load failure from script, so the note is always present rather than
// conditional — same pragmatic choice the legacy shell made).
const GRAFANA_BASE = location.origin + '/grafana/d/ordo-llm-gpu/ordo-llm-gpu'
const EMBED_SRC = GRAFANA_BASE + '?kiosk&refresh=10s'
const OPEN_HREF = GRAFANA_BASE + '?refresh=10s'

export default function GrafanaTab() {
  return (
    <section className="mb-5 rounded-lg border border-border bg-card p-6 shadow-card">
      <h2 className="section-rule mb-4 flex items-center gap-3 text-[0.62rem] font-bold uppercase tracking-[0.18em] text-muted">
        Grafana — llama.cpp &amp; GPU
      </h2>
      <p className="mb-4 text-[0.8125rem] leading-[1.5] text-muted">
        Real-time performance. Requires the <code className="rounded-sm border border-border-subtle bg-bg px-1.5 py-0.5 text-[0.75rem] text-accent-soft">monitoring</code> profile.{' '}
        <a href={OPEN_HREF} target="_blank" rel="noopener">Open full Grafana in a new tab ↗</a>
      </p>

      <div className="h-[78vh] w-full overflow-hidden rounded border border-border bg-bg-elevated">
        <iframe
          src={EMBED_SRC}
          title="Grafana — llama.cpp & GPU metrics"
          className="h-full w-full border-0"
          loading="lazy"
        />
      </div>

      <p className="mt-3 text-[0.75rem] leading-[1.5] text-muted">
        Blank frame? Grafana is only available with the <code className="text-accent-soft">monitoring</code> profile
        (<code className="text-accent-soft">docker compose --profile monitoring up -d</code>). If it's running,{' '}
        <a href={OPEN_HREF} target="_blank" rel="noopener">open it directly</a>.
      </p>
    </section>
  )
}
