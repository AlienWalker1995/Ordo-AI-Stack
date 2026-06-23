# codebase-memory-ui

Optional long-lived service that serves the **3D interactive code knowledge-graph**
from the same index the headless `codebase-memory` MCP builds — so you can *browse*
the graph, not just have Hermes query it.

It runs the upstream UI-variant binary (`codebase-memory-mcp --ui=true --port=9749`),
which serves the visualization as a thread alongside the MCP server.

## Two upstream quirks this image handles
1. **The UI HTTP server binds `127.0.0.1` only** (`src/ui/httpd.c`) — unreachable from
   other containers. `entrypoint.sh` runs a `socat` bridge `0.0.0.0:9750 → 127.0.0.1:9749`
   so Caddy can route to it.
2. **The process is an MCP stdio server** — with no client attached it would read EOF
   on stdin and exit. The entrypoint keeps stdin open (`tail -f /dev/null | …`) so the
   UI stays up as a service.

## Index (in-process)
The UI **indexes the source tree in its own long-lived process** and visualizes that
in-memory graph. It mounts the code root **read-only** at `/c/dev` (`${CODE_ROOT}`) for
this, plus the `codebase-memory-cache` volume at `/cache` for config.

> The upstream binary does **not** reliably flush its graph index to `CBM_CACHE_DIR`
> across container exits, so the cache volume is **not** a shared index — the gateway
> MCP and the UI each index independently. Practical consequence: **the UI's graph is
> in-memory, so after a container restart you must re-index** (browse the UI's index
> action, or `POST /rpc` `index_repository`). Indexing honors `.gitignore` + `.cbmignore`
> (e.g. `secrets/`, `data/` are excluded — verified).

## Exposure (SSO)
The UI is an **absolute-asset SPA** — it requests `/assets/*`, `/api/*`, and `/rpc` at
the origin root, and `/api/*` collides with Open WebUI's root catch-all. So it can't be a
subpath on `:443`; it gets its **own origin on Caddy's `:8443`** listener, behind the same
Google SSO. See the `:8443` block in `auth/caddy/Caddyfile`, the extra
`--whitelist-domain=…:8443` on `oauth2-proxy`, and `overrides/codebase-memory-ui.yml`
(which publishes the port).

## Enable
```
# include the override + the codebase-memory profile
COMPOSE_FILE=docker-compose.yml;overrides/compute.yml;overrides/codebase-memory-ui.yml
docker compose --profile codebase-memory up -d --build
```
Then browse **`https://<CADDY_TAILNET_HOSTNAME>:8443/`** (Google SSO). Index a repo first
(via Hermes `index_repository`, or the UI's own "index" action) or the graph will be empty.

## Note
The UI exposes server actions (`/api/index`, `/api/process-kill`, …) to the browser; it's
SSO-gated to the single operator and network-isolated, which is the acceptable trust model
here. Pin/bump the binary via `CBM_VERSION` + `CBM_UI_SHA256` in the `Dockerfile`.
