# Component: Dashboard UI

## Purpose
A web-based control plane that provides a single pane of glass for:
- Seeing stack health, GPU tenancy, recent renders and GPU leases, and what needs attention (Overview)
- Managing Docker-Compose services (start/stop/restart, logs; ComfyUI restarts from its card) (Services)
- Switching the GPU chat model and managing GGUF files on disk (Models)
- Viewing token throughput and GPU metrics via the embedded Grafana "Ordo performance" dashboard at `/grafana/` (Performance)
- A Ctrl/Cmd K command palette to jump to a page or open a tool

## API Reference

**Base URL:** `http://dashboard:8080` (internal `ordo-net` only — no host port; reached via the Caddy edge at `https://${CADDY_TAILNET_HOSTNAME}:8444/`, its dedicated SSO-gated port under the port-per-service model — see [Architecture & Principles](architecture-and-principles.md))

**Auth:** Operators reach the dashboard through the Caddy edge (oauth2-proxy + Google SSO with an email allowlist). In the table below, `Y` marks routes that need a principal (`dashboard/auth.py`): the edge SSO identity (`X-Forwarded-Email`, trusted only when the TCP peer is `caddy`) or `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` for internal callers. That is every non-GET route plus everything under `/api/ops/` and `/api/orchestration/` except `/readiness`. `None` marks health and read-only views, open to anything on `ordo-net`.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | None | Dashboard + upstream service health (container healthcheck) |
| `/api/hardware/service-pressure` | GET | None | Per-container CPU/RAM; VRAM null until ops-controller measures it; unmeasured values are null with a `reason` |
| `/api/overview` | GET | None | Status line, GPUs and who holds them, chat engine, attention items, host (Overview page) |
| `/api/activity` | GET | None | Renders, GPU leases and operator actions, newest first |
| `/api/services/table` | GET | None | Every container on the host as one flat list: verdict, control kind (`ordo`, `managed`, `host`, `link`, `none`) and allowed actions (Services page) |
| `/api/ops/services/{id}/start` | POST | Y | Start service (via ops-controller) |
| `/api/ops/services/{id}/stop` | POST | Y | Stop service |
| `/api/ops/services/{id}/restart` | POST | Y | Restart service |
| `/api/ops/services/{id}/logs` | GET | Y | Tail service logs |
| `/api/ops/projects/{project}/containers/{name}/restart` | POST | Y | Restart a managed project's container (ops-controller, `managed_projects:`) |
| `/api/ops/projects/{project}/containers/{name}/logs` | GET | Y | Tail a managed project's container logs |
| `/api/models` | GET | None | Chat server slots (gpu, cpu, embed), catalog, GGUF files on disk (Models page) |
| `/api/models/switch` | POST | Y | Switch the GPU model: catalog id -> ops-controller `POST /model-config` (renders and recreates what the render changed; `restart_required_on_host` + `host_command` for what only the host can restart); refused while a GPU render lease is held |
| `/api/models/delete` | POST | Y | Delete a GGUF file; refuses a file any server depends on |
| `/api/throughput/record` | POST | `X-Throughput-Token` when `THROUGHPUT_RECORD_TOKEN` is set | Record a model call (called by model-gateway) |
| `/api/throughput/benchmark` | POST | Y | Short generation against `local-chat` |
| `/api/perf/series` | GET | None | Tokens per second per chat server (Prometheus) |
| `/api/perf/grafana` | GET | None | Whether Grafana is up + the embed path (Performance page) |
| `/api/orchestration/readiness` | GET | None | Orchestration readiness check |
| `/api/orchestration/workflows*`, `/validate`, `/outputs` | GET/POST | Y | Workflow store used by the orchestration MCP server (`services/orchestration/server.py`) |
| `/api/orchestration/comfyui/restart`, `/comfyui/status` | POST/GET | Y | Restart ComfyUI / its status (called by the orchestration MCP server; the Services card restarts ComfyUI through the generic service action) |
| `/api/orchestration/registry/models`, `/registry/gpus` | GET | Y | Served models and GPU pins derived from the render, proxied from ops-controller |
| `/api/diagnostics` | GET | None | Dependency health and recent failures: per-dependency status, the failure ring newest first, and the `DEP-<DEP>-<KIND>` code table (see [Diagnostics](#diagnostics)) |

To add a ComfyUI model, use the ComfyUI MCP tool `download_comfyui_model` (url + category); pull a GGUF with `ordo fetch <catalog-id>` (downloaded and checksum-verified into the `models-gguf` volume).

## Diagnostics

A dependency failure never 500s a page: the data degrades to "unavailable", and the failure is
logged at `WARNING` as one line carrying a stable code and the request's trace id (by the
instrumented helpers — the DEP table below marks which are live):

```
DEP_FAIL code=DEP-OPS-TIMEOUT trace=9f2c1ab4e6d70831 target=/model-config elapsed_ms=3000 detail=ops-controller did not answer GET /model-config within 3 s
```

Every response carries `X-Trace-Id` (the global 500 body includes it as `trace_id`), and calls to
ops-controller send the same id as `X-Request-ID` so the two audit logs correlate. A sane inbound
`X-Request-ID` (`[A-Za-z0-9._-]{1,64}`) is reused as the trace id; it is correlation metadata, not
authentication — any client may set it (spoofable/collidable), and the regex only bounds length and
charset against log injection. Failures are kept in an in-memory ring (cap 200, single-process) plus
per-dependency last-ok / last-failure / consecutive-failure state; `GET /api/diagnostics` serves it.
Details are short by contract: the exception class plus a one-line summary, no tokens, headers or
bodies, URLs stripped, truncated to 200 characters.

Codes are `DEP-<DEP>-<KIND>`. The DEP part names the dependency:

| DEP | Dependency |
|-----|------------|
| `OPS` | ops-controller (control plane) |
| `GATEWAY` | model-gateway (LiteLLM) — planned |
| `PROMETHEUS` | Prometheus |
| `GRAFANA` | Grafana |
| `COMFYUI` | ComfyUI (through the GPU admission gate) |
| `LLAMACPP` | the llama.cpp chat/embedding servers |
| `QDRANT` | Qdrant — planned |
| `EDGE` | the Caddy SSO edge — planned |
| `PROBE` | a service liveness probe — planned |
| `FS` | the filesystem (state files, model dir) — planned |

`— planned` marks dependencies no current code path instruments yet: today only `OPS` (via
`_ops_request`) and `PROMETHEUS`, `GRAFANA`, `COMFYUI`, `LLAMACPP` (via `_get_json`) record
failures. The rest read `unknown` in `GET /api/diagnostics` until the remaining outbound helpers
(slice 2) record them.

The KIND part names how it failed:

| KIND | Meaning |
|------|---------|
| `UNREACHABLE` | the connection failed before a response (refused, DNS, protocol error) |
| `TIMEOUT` | the dependency did not answer within the client's timeout |
| `HTTP5XX` | the dependency answered with a 5xx status |
| `BADPAYLOAD` | the response body could not be parsed as JSON |
| `AUTH` | the dependency rejected the credentials (HTTP 401/403) |
| `NOTCONFIGURED` | the dependency's URL or token is not set |

A 4xx other than AUTH is not a failure kind: the dependency answered, so the central helpers mark
it ok instead of recording it, and no `DEP-*-HTTP4XX` code exists.

`GET /api/diagnostics` answers `{"generated_at", "dependencies": [{dep, status: ok|failing|unknown, last_ok, last_failure: {code, at, detail}, consecutive_failures}], "recent": [...newest first...], "codes": [{code, meaning}]}`.

## Core Responsibilities

- **Docker Lifecycle** – Calls the Ops Controller API (`/services/{id}/start|stop|restart|recreate`) with `OPS_CONTROLLER_TOKEN` (from `out/secrets.env`) as a Bearer header. The UI never mounts `docker.sock`; it uses the controller as a proxy.
- **Model Management** – The Models page lists what each chat server runs and the GGUF files on disk. A switch names a catalog entry; ops-controller rewrites `ordo.yaml`, re-renders and recreates what the render changed (one switch at a time; ops-controller rolls the source back on a failed apply). Dashboard state lives in `data/dashboard/`, mounted as `/data/dashboard`.
- **MCP servers** – Managed outside the dashboard: enable or disable them in the LiteLLM UI or `ordo.yaml`'s `plugins:` list (applied by `ordo apply`). Per-server health and tool counts come from LiteLLM (`/v1/mcp/server/health`, `tools/list` outcomes). Tools are namespaced `<litellm_name>-<tool>` (Hermes adds its own prefix, e.g. `gateway__memory_vault-read_note`).

## Security Model
- The dashboard publishes no host port; it is reached through the Caddy edge or over the internal `ordo-net` docker network. Calls to the Ops Controller carry `OPS_CONTROLLER_TOKEN` (from `out/secrets.env`) as an `Authorization: Bearer …` header, which ops-controller verifies.
- Because it forwards with that token, the dashboard acts only for a caller who could act directly: an operator signed in through the edge, or an internal caller holding `OPS_CONTROLLER_TOKEN` (see **Auth** above). Anything else on `ordo-net` gets `401` on those routes and nothing is forwarded.

## Non-Goals
- Direct end-user chat UI. The chat UI lives in Open WebUI; the dashboard is for *operations*.
- Storage of model weights. Models are stored in the persistent Docker volumes defined in the rendered `out/docker-compose.yml`.

## Dependencies
- `docker compose` (v2) installed on the host.
- `OPERATIONS` environment variables:
  - `OPS_CONTROLLER_TOKEN` – auth for the Ops Controller.
- The `dashboard` service itself (FastAPI app in `services/dashboard/dashboard/` with a React/Vite SPA in `services/dashboard/dashboard/frontend/`) runs inside the Ordo AI Stack.

## Typical Use Flow
1. From a tailnet device, open `https://${CADDY_TAILNET_HOSTNAME}:8444/` and complete Google sign-in (the legacy `/dash` path 302s here for old bookmarks).
2. The SSO front door (Caddy + oauth2-proxy) signs you in; the dashboard trusts the identity Caddy forwards for its operator actions.
3. Use the Services page to stop or restart a service if an issue is suspected.
4. Switch the GPU chat model on the Models page (the model must already be on disk).
5. Enable or disable an MCP server in the LiteLLM UI or `ordo.yaml`'s `plugins:` list, then `ordo apply` — it recreates `model-gateway`, which is how the new MCP set takes effect.

---

**See also:** [Orchestration Layer](component-orchestration-layer.md).
