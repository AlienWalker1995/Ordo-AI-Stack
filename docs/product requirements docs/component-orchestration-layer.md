# Orchestration Layer

## Purpose
MCP tools for multi-service workflows (ComfyUI workflow management and versioning), served through the model gateway's `/mcp` endpoint. No UI of its own.

## Current Implementation

The orchestration layer today is an **MCP server** (`services/orchestration/server.py`) with hardcoded tool definitions. The dashboard exposes orchestration functionality via HTTP routes (`routes_orchestration.py`) and includes readiness checks (`orchestration_readiness.py`) and a SQLite workflow version store (`orchestration_db.py`).

### What exists now
- MCP-based orchestration tools served by the `mcp-orchestration` service, declared in `services/orchestration/plugin.yaml` and registered with LiteLLM's MCP gateway at render time.
- Dashboard HTTP routes that wrap orchestration operations for the UI.
- Workflow versions (save, diff, promote, roll back) in the dashboard data directory.

### What does NOT exist yet
- Plugin Registry via `manifest.json` — services are hardcoded, not dynamically discovered.
- Event bus (Kafka or otherwise) — no message broker in the stack.
- JSON-RPC `POST /orchestrate` API — the current surface is MCP tools + dashboard HTTP routes.
- Automatic rollback/retry with compensating actions.

## Integration with Existing Stack
- **Agent clients** (Hermes, etc.) – Invoke the orchestrator through the gateway's namespaced MCP tools (`gateway__orchestration-<tool>`) or the dashboard HTTP routes.
- **Dashboard** – The Media page shows GPU lease history and restarts ComfyUI via `routes_orchestration.py`; the orchestration MCP server uses the same routes for workflows.

---

**See also:** [Index](index.md).
