# Audit Event Schema

**Status:** Draft (First PR)  
**See:** [Product Requirements Document](../Product%20Requirements%20Document.md)

## Overview

Audit events record privileged actions (service lifecycle, model ops, MCP changes) for security and operational review. Stored in `data/ops-controller/audit.log` (JSONL, append-only).

## Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ts` | string | Yes | ISO8601 UTC timestamp (e.g. `2025-02-28T12:34:56.789Z`) |
| `action` | string | Yes | Action type (see below) |
| `resource` | string | Yes | Target (service id, model name, tool name) |
| `actor` | string | Yes | Caller (`dashboard`, `cli`, `api`) |
| `result` | string | Yes | `ok` or `error` |
| `detail` | string | No | Error message or extra context |
| `correlation_id` | string | No | Request ID for tracing |
| `metadata` | object | No | Additional fields |

## Action Types

| Action | Resource | Example |
|--------|----------|---------|
| `start` | service id | `ollama` |
| `stop` | service id | `open-webui` |
| `restart` | service id | `mcp-gateway` |
| `pull` | service ids | `ollama,open-webui` |
| `logs` | service id | `dashboard` |
| `mcp_add` | tool name | `duckduckgo` |
| `mcp_remove` | tool name | `fetch` |
| `model_pull` | model name | `deepseek-r1:7b` |
| `model_delete` | model name | `llama3.2` |
| `comfyui_pip_install` | custom_nodes subpath | `juno-comfyui-nodes-main` |

## Examples

**Success:**
```json
{"ts":"2025-02-28T12:34:56.789Z","action":"restart","resource":"ollama","actor":"dashboard","result":"ok","detail":""}
```

**Error:**
```json
{"ts":"2025-02-28T12:35:01.123Z","action":"restart","resource":"unknown-svc","actor":"dashboard","result":"error","detail":"Service unknown-svc not in allowlist"}
```

**With correlation:**
```json
{"ts":"2025-02-28T12:36:00.000Z","action":"logs","resource":"model-gateway","actor":"dashboard","result":"ok","correlation_id":"req-abc123"}
```

**With metadata (e.g. logs tail count):**
```json
{"ts":"2025-02-28T12:37:00.000Z","action":"logs","resource":"dashboard","actor":"dashboard","result":"ok","detail":"","correlation_id":"req-xyz","metadata":{"tail":100}}
```

## Storage

- **Path:** `data/ops-controller/audit.log`
- **Format:** One JSON object per line (JSONL)
- **Rotation:** When file size exceeds `AUDIT_LOG_MAX_BYTES` (default 10MB), the ops-controller renames the current log to `audit.log.1` and starts a new file. Only one rotated file is kept. Set `AUDIT_LOG_MAX_BYTES` in the ops-controller environment to change the limit (e.g. `5242880` for 5MB).
- **Export:** `GET /audit?limit=50` (ops-controller, auth required)
