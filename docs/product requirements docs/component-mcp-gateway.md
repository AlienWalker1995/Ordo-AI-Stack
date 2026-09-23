# Component: MCP Tool Aggregation (LiteLLM MCP gateway)

## Purpose

`model-gateway` (LiteLLM v1.100.1) serves **`/mcp`** alongside `/v1/*`. It is the stack's only MCP
entrypoint: one URL, one authentication pattern, aggregating every `mcp-<server_id>` service the
caller's virtual key is granted. Internal clients on `ordo-net` call
`http://model-gateway:11435/mcp`; external and tailnet clients go through the Caddy `:443` front
door at `https://${CADDY_TAILNET_HOSTNAME}/mcp` (unstripped path), gated by a LiteLLM key instead of
Google SSO because a CLI or IDE client cannot do an interactive login.

There is no separate gateway container, no Docker socket in the tool path, and no static tool
token: LiteLLM answers 401 to a missing or invalid key, so the route cannot degrade to open.

## How a server is declared

An MCP server is a plugin manifest at `services/<id>/plugin.yaml` with `kind: mcp` and an `mcp:`
block. The manifest is the single source of truth: the renderer derives the compose service, the
LiteLLM registration and the dashboard's server list from it.

| Key | Meaning |
|-----|---------|
| `server_id` | Optional; defaults to the plugin id |
| `image` **or** `url` | Exactly one. `image` runs a container in the stack; `url` points at a hosted server and renders no service |
| `transport` | Required; `http` is the only accepted value in this release |
| `port` | Required with `image`; the container port serving `path` |
| `path` | Default `/mcp` |
| `network` | `internal` (default) or `stack` |
| `command` | Optional compose command override |
| `env` | Compose environment; `${VAR}` interpolates from `.env` / `secrets.env` |
| `secrets` | Required secret keys, same semantics as service plugins |
| `volumes` | Compose volume list (host binds and named volumes) |
| `depends_on` | Compose dependencies on stack services |
| `healthcheck` | Optional; overrides the renderer's default HTTP probe (only searxng needs it: that image has no `python3`) |
| `timeout` | Per-server LiteLLM tool timeout in seconds (default 60) |
| `auth` | Optional upstream auth LiteLLM presents (hosted servers) |
| `allowed_tools` | Optional; narrows what LiteLLM exposes from this server |
| `tools` | Informational, and the parity test's expectation |

Keys carried over from the retired Docker-gateway schema (`longLived`, `disableNetwork`, and
`PLACEHOLDER_*` values) are rejected by the renderer, as is an unknown key, a non-`http`
transport, a missing `port` alongside `image`, a bad `network` value and a duplicate `server_id`.

Full example (`services/qdrant-rag/plugin.yaml`):

```yaml
id: qdrant-rag
name: Qdrant RAG (MCP tools)
description: Semantic RAG search over the stack's Qdrant `documents` collection, via MCP.
kind: mcp
requires:
  nvidia: false
  ram_gb: 1
provides: [tools]
depends_on: [rag]
mcp:
  image: ordo/qdrant-rag-mcp:latest
  transport: http
  port: 9000
  network: stack          # qdrant:6333 + llamacpp-embed:8080
  env:
    QDRANT_URL: http://qdrant:6333
    EMBED_URL: http://llamacpp-embed:8080
    RAG_COLLECTION: documents
  tools: [qdrant_search, qdrant_status]
```

## How it renders

`ordo render` turns each enabled `kind: mcp` plugin into:

- A compose service **`mcp-<server_id>`**: `restart: unless-stopped`, `init: true`,
  `security_opt: [no-new-privileges:true]`, `cpus: "1"`, `mem_limit: 2g`, labels `ordo.mcp=true`,
  `ordo.mcp.server_id=<id>`, `ordo.mcp.plugin=<plugin id>`. **No `env_file`**: a server sees only
  the variables and secrets its own manifest declares, never the gateway's full env surface.
- Network placement on the top-level **`ordo-mcp-net`**, declared `internal: true`.
  `network: internal` renders `[ordo-mcp-net]`; `network: stack` renders
  `[ordo-mcp-net, ordo-net]`. `model-gateway` is the only other member, so nothing else in the
  stack can call an MCP server directly.
- A default healthcheck from `ordo.compose.default_mcp_healthcheck(port, path)`, unless the
  manifest supplies its own: `["CMD", "python3", "-c", ...]` running
  `urllib.request.urlopen('http://localhost:<port><path>', timeout=5)` where an `HTTPError` (any
  HTTP status) counts as healthy, because the MCP endpoint answers a bare GET with an error status
  and a response proves the listener is up; a `URLError` or socket error fails. `interval: 30s`,
  `timeout: 10s`, `retries: 3`, `start_period: 30s`. It lives in the renderer so the probe is one
  decision rather than a copy per manifest. The container probe proves the HTTP listener; the
  dashboard's LiteLLM outcome rows prove the MCP layer above it.
- `out/model-gateway/mcp_servers.yaml`: the LiteLLM `mcp_servers` fragment, mounted read-only at
  `/config` and merged into the proxy config by the entrypoint. The file is **required**: a render
  with no MCP plugins emits `mcp_servers: {}`, and a missing file is a fail-loud start error.
- `out/mcp/servers.json`: `{server_id, litellm_name, plugin_id, service, url, tools, network}` per
  server plus the registered-plugin map, read by the dashboard at
  `MCP_SERVERS_PATH=/mcp-config/servers.json`.

A hosted server (`url:` instead of `image:`) renders into both files but has no compose service.

## Naming

LiteLLM rejects a server name containing its tool-prefix separator (`-`), so each server gets a
derived **`litellm_name` = `server_id` with hyphens replaced by underscores**. That name is the
LiteLLM mapping key, the `server_id` field and `mcp_info.server_name`; compose service names, labels
and the plugin map keep the hyphenated `server_id`.

LiteLLM namespaces tools **`<litellm_name>-<tool>`**, and Hermes prefixes its own `gateway__`:

| Server | Compose service | LiteLLM name | Tool as Hermes sees it |
|--------|-----------------|--------------|------------------------|
| `memory-vault` | `mcp-memory-vault` | `memory_vault` | `gateway__memory_vault-read_note` |
| `codebase-memory` | `mcp-codebase-memory` | `codebase_memory` | `gateway__codebase_memory-list_projects` |
| `qdrant-rag` | `mcp-qdrant-rag` | `qdrant_rag` | `gateway__qdrant_rag-qdrant_search` |
| `searxng` | `mcp-searxng` | `searxng` | `gateway__searxng-searxng_web_search` |

## Auth and policy

- Every consumer holds its own **virtual key** `LITELLM_KEY_<ID>`, declared by a `litellm_key:`
  block in its manifest (`models:` and `mcp_servers:` grants) and provisioned by the
  `model-gateway-keys` one-shot from `out/model-gateway/keys.json`.
- `require_key_mcp_access_defined: true` means a key sees **only** the servers its
  `object_permission.mcp_servers` grant lists. No grant, no tools; no key, 401.
- The edge hands external clients `LITELLM_KEY_EDGE` for both `/llm/*` and `/mcp`. The dashboard
  keeps `LITELLM_MASTER_KEY`: it is the control plane (`/model/info`, `/v1/mcp/server/health`).
- Per-tool narrowing (`allowed_tools`) is supported by the schema and set by no manifest today.

## Health and observability

- `GET /v1/mcp/server/health` on the gateway reports every registered server.
- A `tools/list` call on `/mcp` carries `_meta["litellm.ai/server_outcomes"]`, giving per-server
  `status` and `tool_count`.
- The dashboard's `GET /api/mcp/health` combines both: a server is `ok` only when the health probe
  says `healthy` **and** its outcome is `ok` with `tool_count > 0`. There is no fallback to
  gateway-level status, which is what previously let three dead servers report green for weeks.
- Prometheus scrapes `model-gateway:11435/metrics`; `litellm_mcp_tool_calls_total{mcp_server_name,
  mcp_tool_name}` increments per tool call.
- ops-controller's `GET /mcp/containers` lists containers by the compose label `ordo.mcp=true`. That is
  inventory, not health.

## Adding a new server

1. **HTTP-native server:** manifest only. Point `image:` at a digest-pinned upstream (or a repo
   build context for a project image), set `transport: http`, `port`, `path`, `network`, and any
   `env` / `secrets` it needs. `mcp-searxng` is the worked example; prefer a stateless HTTP mode
   where the upstream offers one, because LiteLLM's outbound client re-initialises the upstream
   session per operation (BerriAI/litellm #25128) and cannot hold a session id.
2. **Hosted server:** manifest with `url:` and, if the upstream needs a token,
   `auth: {type: bearer_token, secret: <SECRET_KEY>}`. No container is rendered.
3. **stdio-only upstream:** manifest plus a ten-line Dockerfile block, one fixed recipe, that
   bridges stdio to stateless streamable HTTP:

```dockerfile
ARG MCP_PROXY_VERSION=0.12.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/mcp-proxy \
 && /opt/mcp-proxy/bin/pip install --no-cache-dir "mcp-proxy==${MCP_PROXY_VERSION}" "mcp==1.30.0" \
 && ln -s /opt/mcp-proxy/bin/mcp-proxy /usr/local/bin/mcp-proxy
# --pass-environment: mcp-proxy 0.12.0 otherwise starts the child with an EMPTY env (only HOME/PATH).
CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "9000", "--stateless", "--pass-environment", "--", "<upstream command>"]
```

   `mcp-proxy` (sparfenyuk) holds **one persistent stdio child for the process lifetime** and serves
   stateless streamable HTTP on `/mcp`. `mcp` is pinned alongside it because mcp-proxy 0.12.0
   declares `mcp>=1.17.0` unbounded and `mcp` 2.x breaks its imports. `supergateway` was rejected:
   its stateless mode spawns the child per request.

   Three servers use the recipe: [`services/codebase-memory/Dockerfile`](../../services/codebase-memory/Dockerfile),
   [`services/memory-vault/Dockerfile`](../../services/memory-vault/Dockerfile) and
   [`services/n8n/Dockerfile`](../../services/n8n/Dockerfile) (the Alpine variant, `apk` instead of
   `apt-get`, since its base image is Alpine).

In all three cases, finish by adding the plugin id to `ordo.yaml`'s `plugins:` list, then
`ordo render` and recreate `model-gateway`.

## Operations

- **Enable or disable a server:** edit `ordo.yaml`'s `plugins:` list, directly or through the
  MCP section of the dashboard's Settings drawer (which performs the same comment-preserving edit). The dashboard response
  carries `{"applied": false, "next": "ordo render + recreate model-gateway"}`.
- **No hot reload.** LiteLLM reads config-file MCP servers at startup, so a change takes effect
  only after `ordo render` plus a `model-gateway` recreate.
- **Long-running tools** get a per-server `timeout:` in the manifest (`comfyui` renders 1800s); the
  default is 60s.
- **A down server** degrades to a shorter tool list with an explicit `unreachable` outcome and a red
  badge in the dashboard. `/mcp` itself stays up, and `restart: unless-stopped` plus the healthcheck
  handle a crash-loop without operator intervention.
- **Images are pinned:** public images by digest, project images by build context. The rendered
  compose is image-only, so rebuilds use `docker build`, not `docker compose build`.

## Non-goals

- **End-user identity per MCP call.** Scoping is per consumer key, not per human.
- **Replacing n8n or ComfyUI.** The gateway invokes them; it does not own their authoring UIs.

---

**See also:** [Model Gateway](component-model-gateway.md), [Security & Trust Model](security-and-trust-model.md), [Index](index.md).
