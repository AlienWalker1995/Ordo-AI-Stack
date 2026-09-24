# n8n-mcp

MCP server that exposes n8n to Hermes: the full
[`czlonkowski/n8n-mcp`](https://github.com/czlonkowski/n8n-mcp) node and template catalog plus the
`n8n_*` verbs that drive the stack's own n8n instance (list/create/update/execute workflows).

The n8n compose **service** itself is provided by the separate `automation` (`kind: service`)
plugin; this directory only builds the MCP bridge.

## Build

```sh
ordo build mcp-n8n
```

## Why the bridge

The upstream image ships an HTTP mode (`MCP_MODE=http`), but that transport is **session-ful**: a
bare `tools/list` answers `400 No valid session ID provided and not an initialize request`, and the
image exposes no stateless switch. LiteLLM's outbound MCP client opens a fresh session per
operation and does not carry `Mcp-Session-Id` (BerriAI/litellm #25128), so its calls would never
reach the server. So the image runs the upstream's **stdio** server and bridges it to stateless
streamable HTTP with a pinned `mcp-proxy`, the same recipe as
[`../codebase-memory`](../codebase-memory/README.md) and [`../memory-vault`](../memory-vault/README.md):

```
CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "9000", "--stateless", "--pass-environment", "--", "node", "dist/mcp/index.js"]
```

`--pass-environment` is load-bearing: without it `mcp-proxy` starts the stdio child with an empty
environment, `N8N_API_URL`/`N8N_API_KEY` never arrive, and the server registers only its 7
documentation tools instead of the full 28 (the 21 `n8n_*` instance verbs disappear). The upstream
`ENTRYPOINT` is cleared deliberately, because its stdio branch does not forward `"$@"` and would
discard this CMD.

## Pins

- Upstream base: `ghcr.io/czlonkowski/n8n-mcp:2.84.1`, pinned by tag **and** digest in the
  `Dockerfile`. To bump, change both together.
- `mcp-proxy==0.12.0` + `mcp==1.30.0` (`mcp` is bounded on purpose: `mcp-proxy` declares
  `mcp>=1.17.0` unbounded and `mcp` 2.x breaks its imports).

## How it's wired

A long-lived compose service, `mcp-n8n`, serving streamable HTTP on **port 9000** at `/mcp`.
LiteLLM's MCP gateway on `model-gateway` dials `http://mcp-n8n:9000/mcp`. The manifest sets
`network: stack` because the server must reach `n8n:5678`. There is no bearer between LiteLLM and
this server: the bridge listens on the internal network only, and callers are authorised by their
LiteLLM virtual key's MCP grant.

`ordo render` emits the server into `out/model-gateway/mcp_servers.yaml` (the LiteLLM fragment its
entrypoint merges) and `out/mcp/servers.json` (the dashboard's server list).

## Environment

Declared in [`plugin.yaml`](plugin.yaml)'s `mcp.env` block:

| Variable | Value | Why |
|---|---|---|
| `N8N_API_URL` | `http://n8n:5678` | the stack's n8n instance |
| `N8N_API_KEY` | `${N8N_API_KEY}` | operator secret (n8n personal API token); without it only the 7 documentation tools register |
| `LOG_LEVEL` | `error` | the server speaks JSON-RPC on stdout, so a stray INFO byte corrupts the channel `mcp-proxy` reads |
| `N8N_DIAGNOSTICS_ENABLED` | `false` | telemetry off, same stdout hygiene |
| `DISABLE_TELEMETRY` | `true` | telemetry off, same stdout hygiene |

`N8N_API_KEY` is declared in the manifest's `secrets:` list, so it lands in
`secrets.env.example` and interpolates from `secrets.env` at compose time.

## How the tools appear

LiteLLM namespaces tools `<litellm_name>-<tool>`, where `litellm_name` is the server id with
hyphens replaced by underscores (here just `n8n`), so the tools are `n8n-<tool>` and Hermes, which
adds its own gateway prefix, sees `gateway__n8n-search_nodes`,
`gateway__n8n-n8n_list_workflows`, and so on.
