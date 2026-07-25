// Placeholder for tabs not yet ported from the legacy vanilla-JS shell. Each of these
// is a seam a later iteration fills; the legacy dashboard remains available at
// /legacy-index.html for the full, unported functionality in the meantime.

export default function StubTab({ title, endpoints = [], note }) {
  return (
    <section className="stub">
      <div className="stub-card">
        <h2>{title}</h2>
        <p className="stub-badge">Coming in a later iteration</p>
        <p className="stub-note">
          {note || 'This panel is being ported to React. Until then, use the legacy dashboard.'}
        </p>
        {endpoints.length > 0 && (
          <div className="stub-endpoints">
            <span className="stub-endpoints-label">Backend endpoints this tab will consume:</span>
            <ul>
              {endpoints.map((e) => (
                <li key={e}><code>{e}</code></li>
              ))}
            </ul>
          </div>
        )}
        <a className="btn btn-secondary" href="/legacy-index.html">Open legacy dashboard</a>
      </div>
    </section>
  )
}

export const McpTab = () => (
  <StubTab title="MCP Management" endpoints={['/api/mcp/*']} />
)
export const ModelsTab = () => (
  <StubTab title="Model Control" endpoints={['/api/model-config', '/api/llm/*', '/api/active-model', '/api/registry/*']} />
)
export const HardwareTab = () => (
  <StubTab title="Hardware / GPU Detail" endpoints={['/api/hardware', '/api/hardware/service-pressure', '/api/gpu/*']} />
)
export const ThroughputTab = () => (
  <StubTab title="Throughput" endpoints={['/api/throughput/stats', '/api/throughput/service-usage']} />
)
export const OrchestrationTab = () => (
  <StubTab title="Orchestration / Jobs" endpoints={['/api/orchestration', '/api/jobs', '/api/orchestration/readiness']} />
)
export const GrafanaTab = () => (
  <StubTab
    title="Grafana"
    note="The full port embeds Grafana via an <iframe src='/grafana/'>. Deferred to a later iteration."
    endpoints={['/grafana/']}
  />
)
export const RagTab = () => (
  <StubTab title="RAG Status" endpoints={['/api/rag/status']} />
)
export const ComfyTab = () => (
  <StubTab title="ComfyUI Models & Packs" endpoints={['/api/comfyui/*']} />
)
export const DependenciesTab = () => (
  <StubTab title="Dependencies" endpoints={['/api/dependencies']} />
)
