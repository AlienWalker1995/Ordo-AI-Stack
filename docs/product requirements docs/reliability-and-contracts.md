# Reliability Layer & Service Contracts (Strategic)

This section captures the developer/operator view of what Ordo AI Stack needs to be operationally strong. It is **product requirements**, not an implementation spec.

## Positioning: OpenClaw as Mesh Client

OpenClaw is **not** the center of the architecture; it is **one consumer** of:

- **Model Gateway** (`:11435`) — single OpenAI-compatible surface to Ollama / vLLM.
- **MCP Gateway** (`:8811`) — shared tools via `openclaw-mcp-bridge` and other clients.
- **Browser / CDP bridges** — optional capability; easy to misconfigure.

**Effective paths (simplified):**

```
User → OpenClaw UI/Gateway → Model Gateway → Ollama / vLLM
User → OpenClaw UI/Gateway → OpenClaw MCP bridge → MCP Gateway → tool servers
```

Reliability is constrained by **every hop**. Weak contracts anywhere surface as "the assistant is flaky."

## What "Very Strong" Means

1. **Preflight** — OpenClaw (and tooling) knows whether downstream services are **healthy** before attempting dependent actions.
2. **Isolation** — One bad MCP server, one model timeout, or one broken bridge does **not** poison all tool or model usage.
3. **Traceability** — Every user-visible failure is attributable to a **layer** (OpenClaw, model gateway, provider, MCP gateway, tool server, browser bridge).
4. **Recovery** — **Automatic** retry/backoff for transient failures; **explicit** operator steps for auth/config/provider-down cases.
5. **Versioned contracts** — Config and service expectations are **validated** so semantics do not silently drift.
6. **Security ↔ reliability** — Guardrails do not break legitimate recovery paths; reliability mechanisms do not weaken security.

## The Gap: First-Class Service Contract Layer

**A. Explicit readiness states (machine-readable)**
Not only "container running." Callers need signals: model gateway can answer **now**; which providers are live; default model; warm vs cold hints; MCP gateway reachable; which tools/servers are healthy; browser bridge reachable; failure is **retryable** vs **operator required**.

**B. Consume dependency state before action selection**
Policy examples: do not start a model call if no live provider; do not invoke a degraded tool without surfacing degradation; do not assume browser actions if the bridge is down.

## Reliability Spine (Building Blocks)

### 1) Dependency Registry (Canonical)

One registry listing every runtime dependency: model gateway, backends (Ollama/vLLM), MCP gateway, MCP servers/tools, browser bridge, optional RAG, optional ops controller.

Per dependency: name, endpoint, auth mode, health endpoint(s), version, timeout budget, retry policy, circuit-breaker policy, fallback target, last healthy timestamp, degraded reason. Rendered in dashboard and consumed by gateways/agents.

### 2) Health Depth: L1 / L2 / L3

| Level | Meaning (Examples) |
|-------|---------------------|
| **L1** | Reachability: TCP/HTTP up; auth token accepted |
| **L2** | Functional: model gateway lists models; MCP enumerates tools; browser bridge creates session |
| **L3** | Transactional: tiny inference succeeds; safe tool noop; minimal browser metadata fetch |

Most stacks stop at L1; L2/L3 are required for "feels reliable."

### 3) Timeouts, Retries, Backoff

Per **class** of operation (model list vs chat stream vs MCP discovery vs tool exec vs browser): budgets, retry only on **network / 502 / 503 / gateway timeout**, no retry on **auth / schema / policy**, exponential backoff with jitter.

## Model Gateway Reliability

- **Provider metadata:** type (Ollama/vLLM), supported APIs, concurrency, warm/cold hints, latency signals, last failure, context limits.
- **Fallback chains:** Prefer capability-based routing when a target is unavailable or overloaded.
- **Warmup / preflight:** Optional prewarm of default model; queue depth or cold-start flags in health.
- **Streaming robustness:** Preserve stream semantics; clean recovery when upstream closes; annotate incomplete generations.
- **Schema normalization:** Stable error envelope, finish reasons, tool-call shapes, token accounting.

## MCP Gateway Reliability

- **Curated tool surface:** enabled tools only; healthy tools only; tools allowed for this client/agent; stable schemas; versioned metadata.
- **Per-tool / per-server circuit breakers:** failure counts; open/half-open/closed; quarantine; auto recheck; user-visible degradation.
- **Schema validation:** On registration and periodically—reject or quarantine drift.
- **Execution classes:** fast stateless vs network-bound vs long-running vs side-effecting—distinct timeouts, confirmation policy, retries.
- **Provenance:** `request_id`, `session_id`, `agent_id`, tool name, server version, duration, result code, failure category.

## Browser / Bridge Reliability

Typical failures: stale sessions, expired auth, headless crashes, navigation hangs, DOM mismatch.

Requirements: explicit browser session lifecycle APIs; health check separate from OpenClaw UI; idle timeouts / leases; diagnostics events; recycle crashed sessions; operator metrics. Browser automation is optional, not a hard dependency for chat.

## Observability (Elite Bar)

- **Golden trace:** One `request_id` from OpenClaw ingress through model selection, model gateway, provider, MCP bridge, MCP gateway, tool server, optional browser bridge.
- **Failure taxonomy:** network, auth, config, dependency unavailable, timeout, schema mismatch, policy denied, provider overload, internal bug—actionable signal.
- **SLO-style reporting:** model gateway success rate, p50/p95 latency, MCP tool success rate, top failing tools, OpenClaw request success rate.

## Configuration Management

- Version and validate config on startup; deprecations and safe auto-migration.
- OpenClaw integration checks: bridge plugin present, model gateway endpoint, tokens, default model exists, browser port assumptions.
- Stack "doctor" command: one diagnostic entrypoint for the whole stack.

## Roadmap Summary (Maps to M7)

| Phase | Focus | Outcome |
|-------|--------|---------|
| **1** | Visibility | Typed readiness, registry, correlation, taxonomy, dashboard dependency page, OpenClaw startup validation, E2E smoke tests |
| **2** | Degradation & recovery | Fallbacks, circuit breakers, warm/cold reporting, retries, auto-disable bad tools, ops-assisted restart, browser recycle |
| **3** | Operator-grade | SLOs, pinned bundles, rollback, `BASE_PATH` backup/restore, config migrations, release/integration matrix |

## What Not to Do

- Turn Ordo AI Stack into a monolithic OpenClaw fork.
- Make the dashboard a required runtime dependency for normal inference or tools.
- Put ops-controller on the hot path for every user action.
- Add more services before dependency contracts are hardened.
- Rely on restart as the primary reliability strategy.
