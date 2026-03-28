# Orchestration Layer

## Purpose
A lightweight, plug‑in‑driven orchestrator that stitches together all AI services (model‑gateway, MCP, n8n, custom plugins) into a unified workflow engine. It does **not** expose any direct UI; it only provides APIs that any service (including the dashboard, OpenClaw, or external clients) can call.

## Current Implementation

The orchestration layer today is an **MCP server** (`orchestration-mcp/server.py`) with hardcoded tool definitions. The dashboard exposes orchestration functionality via HTTP routes (`routes_orchestration.py`) and includes readiness checks (`orchestration_readiness.py`) and job tracking (`orchestration_jobs.py`).

### What exists now
- MCP-based orchestration tools registered in the MCP gateway via `registry-custom.yaml`.
- Dashboard HTTP routes that wrap orchestration operations for the UI.
- A `wait-orchestration` init container that polls `dashboard:8080/api/orchestration/readiness` before OpenClaw starts.
- Job tracking in the dashboard data directory.

### What does NOT exist yet
- Plugin Registry via `manifest.json` — services are hardcoded, not dynamically discovered.
- Event bus (Kafka or otherwise) — no message broker in the stack.
- JSON-RPC `POST /orchestrate` API — the current surface is MCP tools + dashboard HTTP routes.
- Automatic rollback/retry with compensating actions.

## Target Architecture

> The following describes the intended design. Items marked **(planned)** are not yet implemented.

### Key Concepts
- **Plugin Registry** **(planned)** – New services would register themselves via a `manifest.json` in a well‑known plugin folder. The orchestrator would load them on start‑up and watch for changes.
- **Execution Context** – Each workflow run receives a scoped context (environment variables, secrets from `.env`, and runtime state). The context is passed as part of the call so plugins can request data (e.g., token values) without seeing the full `.env`.
- **Event Bus** **(planned)** – Orchestrator would emit `workflow.started`, `workflow.completed`, and `workflow.failed` events via a message broker or in‑memory queue. Consumers could subscribe (e.g., a monitoring service).
- **Rollback & Retry** **(planned)** – The orchestrator would automatically retry failed steps up to `max_retries` and could roll back to a previous step if a compensating action is defined.

### Target Public API (planned)
```json
POST /orchestrate
{
  "method": "startWorkflow",
  "params": {
    "workflowId": "string",
    "input": { "key": "value" }
  },
  "id": 1
}
```
Response:
```json
{
  "result": {
    "runId": "uuid",
    "status": "running|completed|failed",
    "logs": ["msg 1", "msg 2"]
  },
  "id": 1
}
```

## Design Decisions (Why this shape?)
1. **Extensibility over rigidity** – We want any new AI service (e.g., an Azure OpenAI integration) to be added by dropping a manifest file, not by touching core code.
2. **Separation of concerns** – Orchestrator never holds long‑running state. It delegates execution to the underlying services (model‑gateway, n8n). The orchestrator's role is coordination, not execution.
3. **Security** – All calls are authenticated with the existing `OPS_CONTROLLER_TOKEN`. The token is passed in the HTTP `Authorization` header by the dashboard or OpenClaw CLI.
4. **Observability** – Every step is logged with trace IDs, making it easy to trace a user's request through the entire AI stack.

## Integration with Existing Stack
- **Model Gateway** – Calls to the orchestrator can chain model calls (e.g., call `model-a`, then `model-b`). The orchestrator forwards the request to the gateway with the appropriate provider name.
- **OpenClaw** – OpenClaw CLI can invoke the orchestrator via the `gateway__call` RPC when a user issues `/workflow` commands.
- **Dashboard** – The dashboard shows orchestration status, active jobs, and recent logs via `routes_orchestration.py`.
- **n8n** – Existing n8n workflows can be composed with the orchestrator for multi‑model orchestration, without rewriting node logic.
- **Ops Controller** – Handles lifecycle of the orchestrator container; restarts it automatically on failures.

## Example Workflow: "Create a multi‑modal response"
1. **Input** – User asks a question.
2. **Step 1** – Orchestrator calls `gateway__call` with `provider=ollama`, `tool=search` to fetch context.
3. **Step 2** – Orchestrator invokes the `comfyui` plugin to render an image.
4. **Step 3** – Orchestrator compiles a markdown summary using the LLM.
5. **Step 4** – Returns the full response to the caller (OpenClaw CLI or dashboard).

---

**See also:** [Index](index.md) for the broader goals that motivate this layer.
