# codebase-memory-ui

Optional long-lived service that serves the **3D interactive code knowledge-graph**
from the same index the headless `codebase-memory` MCP builds — so you can *browse*
the graph, not just have Hermes query it.

It runs the upstream UI-variant binary (`codebase-memory-mcp --ui=true --port=9749`),
which serves the visualization as a thread alongside the MCP server.

## Two upstream quirks this image handles
1. **Absolute-asset SPA that binds `127.0.0.1` only.** The UI binds `127.0.0.1:9749`
   and serves `/assets`, `/api`, `/rpc`, `/font-files` at the origin root with no
   base-path option. The image runs **nginx** (on `0.0.0.0:9750`) which proxies to the
   UI and `sub_filter`-rewrites those baked paths to the `/codebase-memory/` prefix
   (see `nginx.conf`) — so Caddy serves it under that subpath on the shared `:443` SSO
   origin without colliding with Open WebUI's root.
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
Served at **`https://<host>/codebase-memory/`** on the shared `:443` origin, behind the
existing Google SSO — no dedicated port. Caddy routes `/codebase-memory/*` to this
container (the `@codebasememory` handle in `auth/caddy/Caddyfile`); nginx rewrites the
SPA's absolute paths so everything stays under the prefix. The dashboard's services
section links here via `SSO_ROUTES`.

## Enable
```
# set CODE_ROOT in .env first (host path of your repos), then:
docker compose --profile codebase-memory up -d --build
```
Then browse **`https://<CADDY_TAILNET_HOSTNAME>/codebase-memory/`** (Google SSO). Index a
repo first (the UI's "index" action, or `POST /codebase-memory/rpc` `index_repository`)
or the graph will be empty.

## Note
The UI exposes server actions (`/api/index`, `/api/process-kill`, …) to the browser; it's
SSO-gated to the single operator and network-isolated, which is the acceptable trust model
here. Pin/bump the binary via `CBM_VERSION` + `CBM_UI_SHA256` in the `Dockerfile`.
