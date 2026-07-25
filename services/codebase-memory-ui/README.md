# codebase-memory-ui

> ℹ️ **Live in the Ordo stack.** This is a running production service. In the Ordo stack it's the `codebase-memory-ui` **service plugin** (image `ordo/codebase-memory-ui:latest`, profile `codebase-memory`), built from **this directory** (`services/codebase-memory-ui/`, its self-contained build context). Enablement is via `ordo.yaml` (plugin gating) rather than the V1 `docker compose --profile …` command shown below. Since the 2026-07-24 edge convergence it gets its **own dedicated SSO-gated port (`:8448`) and is served at its origin ROOT** — the container's nginx is now a plain pass-through (no `sub_filter`). See [`docs/history/PARITY.md`](../../docs/history/PARITY.md).

## Build
Project buildable image (no public registry to digest-pin against — pinned by this build context),
so `ordo preflight` reports a missing one as "build first":
```
docker build -t ordo/codebase-memory-ui:latest services/codebase-memory-ui
```

Optional long-lived service that serves the **3D interactive code knowledge-graph**
from the same index the headless `codebase-memory` MCP builds — so you can *browse*
the graph, not just have Hermes query it.

It runs the upstream UI-variant binary (`codebase-memory-mcp --ui=true --port=9749`),
which serves the visualization as a thread alongside the MCP server.

## Two upstream quirks this image handles
1. **Absolute-asset SPA that binds `127.0.0.1` only.** The UI binds `127.0.0.1:9749`
   and serves `/assets`, `/api`, `/rpc` at the origin root with no base-path option.
   The image runs **nginx** (on `0.0.0.0:9750`) purely to expose that loopback-bound UI
   on the container network — it **proxies straight through with no path rewriting**
   (see `nginx.conf`). Because the service now has its own dedicated port (`:8448`) and
   is served at the origin ROOT, the absolute asset paths resolve as-is; there is no
   `sub_filter` and no `/codebase-memory/` prefix anymore. (The 3D node-label fonts are
   fetched from an external CDN — the unicode-font-resolver on jsdelivr — not this origin;
   since nothing is rewritten at all, that CDN URL is untouched.)
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
Served at **`https://<host>:8448/`** — its own dedicated SSO-gated Caddy port, at the
origin ROOT, behind the existing Google SSO. Caddy's `:8448` site block is a plain
`import sso_service codebase-memory-ui:9750` (see `auth/caddy/Caddyfile`) — no path
rewriting at the edge, and the container's nginx proxies straight through, so the SPA's
absolute asset paths resolve directly. The old `https://<host>/codebase-memory/` URL
still works — Caddy's `:443` front door 302s it to `:8448/`. The dashboard's services
section links here via `SSO_ROUTES`.

## Enable
Plugin gating happens at render time via `ordo.yaml`, not a V1 `--profile` flag. Set `CODE_ROOT`
under the `site:` block and force the plugin on (or leave `plugins: auto`, which enables it
whenever the host meets `requires:` in the co-located [`plugin.yaml`](plugin.yaml)):
```yaml
# ordo.yaml
site:
  CODE_ROOT: /home/me/dev
plugins: [codebase-memory-ui]   # or: auto
```
Then re-render and bring the stack up:
```
ordo render
cd out && docker compose -p ordo up -d
```
Then browse **`https://<CADDY_TAILNET_HOSTNAME>:8448/`** (Google SSO). Index a
repo first (the UI's "index" action, or `POST /rpc` `index_repository`)
or the graph will be empty.

## Note
The UI exposes server actions (`/api/index`, `/api/process-kill`, …) to the browser; it's
SSO-gated to the single operator and network-isolated, which is the acceptable trust model
here. Pin/bump the binary via `CBM_VERSION` + `CBM_UI_SHA256` in the `Dockerfile`.
