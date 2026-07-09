# V1 → V2 service parity matrix

Maps every service defined in the live stack's `docker-compose.yml`
(`C:\dev\ordo-ai-stack`, 31 service definitions) to its disposition in the V2 substrate. V2 is
**data-driven**: the core is 6 fixed services (`compose.py`), and everything else is a
`plugin.yaml` manifest the renderer composes in when the hardware fits + it's requested. So "port"
here means "a plugin manifest declares it", not "hand-written into a compose file".

**Categories**
- **A — already covered** by a V2 core service or a pre-existing plugin.
- **B — ported now** as a new `kind=service` plugin manifest.
- **C — MCP** one-shot image builders → mapped to `kind=mcp` plugin entries with real image refs.
- **D — obsolete in V2 by design** (documented why).

## Counts
| Category | Count | Services |
|---|---|---|
| A — core / existing plugin | 12 | llamacpp, llamacpp-embed*, model-gateway, ops-controller, dashboard, comfyui, stt, tts, gpu-exporter, prometheus, grafana, hermes-gateway(agent) |
| B — ported now (new service plugin) | 9 | worker, open-webui, n8n, qdrant, rag-ingestion, searxng, codebase-memory-ui, hermes-dashboard, caddy+oauth2-proxy(edge) |
| C — MCP (kind=mcp, real images) | 2 registered | qdrant-rag(=qdrant-rag-mcp-image), searxng(mcp) ; comfyui-mcp/-image + orchestration-mcp-image + codebase-memory-mcp-image tracked below |
| D — obsolete by design | 4 | gguf-puller, comfyui-model-puller, comfyui-manager-setup, (reactive guardian pieces — see notes) |

\* `llamacpp-embed` ships **inside the `rag` plugin** in V2 (grouped with qdrant + rag-ingestion),
not as a core service — it's only needed when RAG is enabled.

## Full matrix

| V1 service | Cat | V2 disposition | Status |
|---|---|---|---|
| `llamacpp` | A | Core service (`compose.py`). Image pinned via catalog `backend_image` (patched build). | done (prior slice) |
| `llamacpp-embed` | A/B | Ported into the **`rag`** plugin (embedding server for retrieval). Upstream llama.cpp `:server`, `--embeddings`. | ported |
| `model-gateway` | A | Core service. Now a **project buildable image** `ordo-v2/model-gateway:latest` (LiteLLM + the `local-chat` alias config) — see `docker/model-gateway/`. | image parity fixed |
| `ops-controller` | A | **V2-native** core control plane (`ordo serve` = `ordo-v2/ops-controller:latest`). | done (prior slice) |
| `dashboard` | A | **V2-native** core control-plane SPA (`ordo-v2/dashboard:latest`). | done (prior slice) |
| `worker` | B | **`worker`** plugin (`ordo-v2/worker:latest`, profile `media`, depends comfyui). | ported |
| `open-webui` | B | **`open-webui`** plugin (`ghcr.io/open-webui/open-webui:v0.10.1`, profile `webui`, depends rag). | ported |
| `comfyui` | A | Pre-existing **`comfyui`** plugin (media backend). | done (prior slice) |
| `comfyui-mcp` | C | Long-lived ComfyUI MCP — served by the mcp-gateway from the ComfyUI MCP image; registered via the mcp-gateway config (rendered registry). Tracked; not a standalone V2 compose service (gateway-spawned model). | tracked |
| `comfyui-mcp-image` | C/D | One-shot **image builder** (`command: ["true"]`) — no runtime role. In V2 the image is a build step (`docker build`), not a compose service. | obsolete as a *service* (build step) |
| `orchestration-mcp-image` | C/D | One-shot image builder for the orchestration MCP (dashboard-verb adapter). Build step, not a service. | obsolete as a *service* |
| `qdrant-rag-mcp-image` | C | One-shot builder for the Qdrant RAG MCP → V2 **`qdrant-rag`** `kind=mcp` plugin, image `ordo-v2/qdrant-rag-mcp:latest`. | ported (MCP) |
| `codebase-memory-mcp-image` | C/D | One-shot builder for the codebase-memory MCP (gateway-spawned stdio). Build step; the MCP itself is gateway-spawned, the **UI** is ported below. | obsolete as a *service* |
| `codebase-memory-ui` | B | **`codebase-memory-ui`** plugin (`ordo-v2/codebase-memory-ui:latest`, profile `codebase-memory`). | ported |
| `n8n` | B | **`automation`** plugin (`docker.n8n.io/n8nio/n8n:2.28.3`, profile `automation`). | ported |
| `mcp-gateway` | A | Core service. Now a **project buildable image** `ordo-v2/mcp-gateway:latest` (docker/mcp-gateway + reload wrapper) — see `docker/mcp-gateway/`. | image parity fixed |
| `oauth2-proxy` | B | **`edge`** plugin (with caddy), profile `edge`. | ported |
| `caddy` | B | **`edge`** plugin (front door, the ONLY host-port publish `:443`), profile `edge`. | ported |
| `stt` | A | Pre-existing **`voice`** plugin service (faster-whisper, secondary-GPU pin). | done (prior slice) |
| `tts` | A | Pre-existing **`voice`** plugin service (Kokoro, secondary-GPU pin). | done (prior slice) |
| `qdrant` | B | **`rag`** plugin service (`qdrant/qdrant:v1.18.2`, profile `rag`). | ported |
| `rag-ingestion` | B | **`rag`** plugin service (`ordo-v2/rag-ingestion:latest`, profile `rag`). | ported |
| `hermes-gateway` | A | The **agent** core service (`ordo-v2/agent-hermes:latest`, via the agent registry; Hermes default). | done (prior slice) |
| `hermes-dashboard` | B | **`hermes-dashboard`** plugin (same agent image, `hermes dashboard`, profile `hermes-ui`). | ported |
| `searxng` | B | **`searxng-web`** plugin (self-hosted SearXNG service, profile `search`). | ported |
| `gpu-exporter` | A | Pre-existing **`monitoring`** plugin service. | done (prior slice) |
| `prometheus` | A | Pre-existing **`monitoring`** plugin service. | done (prior slice) |
| `grafana` | A | Pre-existing **`monitoring`** plugin service. | done (prior slice) |
| `gguf-puller` | D | **Obsolete by design.** V1 profile-`models` one-shot puller → replaced by **`ordo fetch`** (checksum-mandatory offline model provisioning, prior slice). No compose service needed. |
| `comfyui-model-puller` | D | **Obsolete by design.** One-shot ComfyUI model puller (profile `comfyui-models`) → the operator's ComfyUI image / `ordo fetch`-style provisioning; not a resident service. |
| `comfyui-manager-setup` | D | **Obsolete by design.** One-shot `git clone` of ComfyUI-Manager before comfyui starts → belongs to the operator's ComfyUI image build (idempotent Setup), not a compose service (matches the "Hermes orchestrates; avoid one-shot compose services" principle). |

## MCP servers (kind=mcp) — real images, no placeholders

The prior slice shipped `qdrant-rag` + `searxng` MCP plugins with **placeholder** digests
(`0000…`/`1111…`). Fixed to the images V1 actually runs:

| MCP plugin | V1 image | V2 image | Pin |
|---|---|---|---|
| `qdrant-rag` | `ordo-ai-stack-qdrant-rag-mcp:latest` (locally built) | `ordo-v2/qdrant-rag-mcp:latest` | build context (project image) |
| `searxng` | `isokoliuk/mcp-searxng` (unpinned float) | `isokoliuk/mcp-searxng@sha256:ec0ca986…` | **digest-pinned** (registry manifest) |

Locally-built project MCP images are exempt from the digest-pin gate (they're pinned by build
context, like `llamacpp-patched`); public MCP images must be digest-pinned (the V1 online-catalog
drift/leak source). `ordo preflight` → "all enabled MCP images digest-pinned: all pinned".

## Category-D rationale (why obsolete, not ported)

- **Model pullers** (`gguf-puller`, `comfyui-model-puller`) — one-shot `restart: no` jobs, not
  resident services. V2 provisions models via **`ordo fetch`** (mandatory sha256; offline-capable),
  a first-class command from a prior slice — a strictly better fix than re-porting a compose one-shot.
- **`comfyui-manager-setup`** — a `git clone` shim that runs once before ComfyUI. Its job belongs in
  the operator's ComfyUI image build (idempotent Setup that self-heals on recreate), per the
  "Hermes orchestrates; avoid image-time compose one-shots" principle — not a substrate service.
- **`*-mcp-image` builders** — `command: ["true"]` containers whose *only* purpose is to `build:`
  an image tag. In V2 an image is produced by `docker build` (see `docker/*/README.md`), so these
  have **no runtime service role**. The images they built map to `kind=mcp` plugins (qdrant-rag) or
  gateway-spawned MCPs (comfyui / orchestration / codebase-memory) registered in the rendered
  `mcp-registry.yaml`.
- **Reactive guardian** — the V1 ops-controller's ComfyUI↔llamacpp VRAM-serialization guardian
  (queue-poll → pause llamacpp during a ComfyUI render) is **superseded** by V2's **scheduler**
  (FIFO admission + co-run-when-it-fits + LRU idle-evict, prior slice): a proactive decision engine
  replaces the reactive stop/start, so the eviction-deadlock class is designed out, not re-ported.
