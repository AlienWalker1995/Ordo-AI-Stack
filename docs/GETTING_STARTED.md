# Getting Started

> ⚠️ **See [`operator-guide.md`](operator-guide.md) for the authoritative getting-started + operator doc.** Ordo is defined and operated entirely from the render substrate. A fresh install is two commands: `ordo init` (the wizard; accept its offer to render) and `ordo up --core` (or `--all`), which builds the stack's own images on first run. The workflow commands below assume you've rendered the stack once (`out/`) and run `ordo` from the repo root; `ordo up` builds any image it needs that is not built yet.

Quick paths to common workflows for a single homelab operator. A fresh install is local-only: the chat UI and the dashboard publish on `127.0.0.1:8443` / `:8444`, and no Caddy or oauth2-proxy is rendered. With remote access on (`ordo remote enable`, see [Tailscale + SSO front door](#tailscale--sso-front-door) below), Caddy is up on `${CADDY_TAILNET_HOSTNAME}`: `:443` is the landing page (plus `/oauth2`, `/llm/*`, `/mcp`, n8n's webhook/OAuth passthroughs, and 302s from legacy subpaths) and each UI service has its own SSO-gated port (`:8443` Open WebUI, `:8444` dashboard, `:8445` n8n, `:8446` ComfyUI, `:8447` Hermes, `:8448` codebase-memory, `:8449` LiteLLM admin UI, `:8450` Langfuse), and you can sign in with a Google account on the allowlist (`site: SSO_ALLOWED_EMAILS` in `out/ordo.yaml`). One sign-in covers every port.

## Workflows

### I want to chat

1. Start: `ordo up --all`. It downloads the chat model the render picked into the `models-gguf` volume on the first run, checksum-verified (`ordo fetch <catalog-id>` fetches another catalog model; a model switch goes through the dashboard's Models page).
2. Open Open WebUI: `http://127.0.0.1:8443/` locally, or `https://${CADDY_TAILNET_HOSTNAME}:8443/` with remote access on.

No GPU required for chat (llama.cpp runs on CPU, slower but works).

### I want to generate images (LTX-2)

1. Render the stack (`python -m ordo --source out/ordo.yaml render --out out` from the repo root; hardware, including NVIDIA/AMD/Intel/CPU, is auto-detected), then `ordo up --all` (brings up every rendered profile, ComfyUI included)
2. Pull LTX-2 models via the dashboard (~60 GB, first run takes a while)
3. Open `https://${CADDY_TAILNET_HOSTNAME}:8446/` — ComfyUI

### I want workflow automation

1. Enable the `automation` plugin in `out/ordo.yaml`'s `plugins:`, render, then `ordo up --all` (a named `ordo up n8n` is `--no-deps`, so it would not start the model gateway n8n calls).
2. Open `https://${CADDY_TAILNET_HOSTNAME}:8445/` — n8n (the UI lives on this port; n8n's public webhook/OAuth-callback base, `N8N_WEBHOOK_URL=https://${CADDY_TAILNET_HOSTNAME}/n8n`, is unchanged and stays on `:443`)

### Full stack

**Recommended:** follow the [`operator-guide.md`](operator-guide.md) bring-up (`ordo render` → `ordo up --all`): hardware auto-detection, model selection, and plugin gating all happen at render time. With remote access on, Caddy + oauth2-proxy come up alongside the AI services and front-door them automatically, each on its own SSO-gated port.

Alternatively, `ordo up --all` alone brings up the same services without re-rendering, if `out/` is already current.

**Hermes dashboard:** `https://${CADDY_TAILNET_HOSTNAME}:8447/`. See [hermes-agent.md](hermes-agent.md) for setup and Discord configuration.

### RAG (documents in chat)

Use local files as context in **Open WebUI** via Qdrant + the `rag-ingestion` service.

1. **Start the RAG profile** (adds Qdrant + `rag-ingestion`). `ordo up` downloads the embedding model (**`nomic-embed-text`**) into the `models-gguf` volume that `llamacpp-embed` serves from the first time, checksum-verified (see [data.md: Model Pull](data.md#model-pull)):
   ```bash
   ordo up qdrant llamacpp-embed rag-ingestion
   ```
2. **Drop documents** under `data/rag-input/` (paths come from your `DATA_PATH` / `BASE_PATH`; default is `<repo>/data/rag-input/`). Supported types include `.txt`, `.md`, `.pdf`, and common code extensions — see `services/rag/ingest.py` for `SUPPORTED_EXTENSIONS`.
3. **Open WebUI** → enable RAG for chat (vector DB is already pointed at Qdrant in compose).
4. **Check status:** the dashboard Overview page (`GET /api/overview`, `knowledge.documents`); the collection name defaults to `documents` (`RAG_COLLECTION`).

Env knobs (optional): `EMBED_MODEL`, `RAG_COLLECTION`, `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP` — set via the `overrides:` block in `ordo.yaml` (tracked template: [`ordo.example.yaml`](../ordo.example.yaml)) and re-render. The dashboard **RAG** section shows Qdrant collection point count when the stack can reach Qdrant. See the PRD **WS6: RAG Pipeline** for the full picture.

**Optional — [Agentic Design Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns) (MIT book text):** clone or copy the `.md` tree into `data/rag-input/` (for example `git clone --depth 1 https://github.com/Mathews-Tom/Agentic-Design-Patterns.git data/rag-input/agentic-design-patterns`), then run the steps above so `rag-ingestion` can index it.

### Host tools (Cursor, CLI on the host machine)

`model-gateway` has no host port publish (core services publish nothing — see `ordo/render/compose.py`). Host and tailnet tools reach it through the Caddy front door at `/llm/`, which bypasses SSO for programmatic clients and instead requires the LiteLLM bearer key:

- Point Cursor or any OpenAI-compatible client at `https://${CADDY_TAILNET_HOSTNAME}/llm/v1` with `Authorization: Bearer ${LITELLM_MASTER_KEY}`.
- This works from the host and from any tailnet device — there is no `127.0.0.1:11435` shortcut. Publishing the port directly to the host requires a deliberate `ordo.yaml` override (a `services.model-gateway.ports` entry re-rendered through `ordo render`), which is not the shipped default.

## Tailscale + SSO front door

Single homelab operator with a small Google-account allowlist for friends / family / co-workers — that's the deployment model. Caddy is the only service that publishes host ports; every other service is reached through it, one dedicated port per UI (port-per-service model, since 2026-07-24 — each prebuilt SPA is served at the root it was compiled for, instead of being mounted under a subpath):

| Port | Service |
| --- | --- |
| `:443` | Front door — landing page, `/oauth2` (the one Google callback), `/llm/*` (LiteLLM API, Bearer), `/mcp` (Bearer), n8n's webhook/OAuth passthroughs (`/n8n/webhook/*`, `/n8n/rest/oauth2-credential/callback`), and 302s from every legacy subpath |
| `:8443` | Open WebUI (chat) |
| `:8444` | Dashboard (+ `/grafana/` embed) |
| `:8445` | n8n (UI) |
| `:8446` | ComfyUI |
| `:8447` | Hermes (served at this port's root) |
| `:8448` | codebase-memory (served at this port's root) |
| `:8449` | LiteLLM admin UI (served at this port's root) |
| `:8450` | Langfuse (served at this port's root) |

The edge (Caddy + oauth2-proxy) stays off until remote access is enabled. `ordo remote enable` is the one path: it writes the `site:` keys (`CADDY_BIND`, `CADDY_TAILNET_HOSTNAME`, `CADDY_TAILNET_DOMAIN`), the Google OAuth client pair and the email allowlist, renders, and offers the Tailscale cert under the file names Caddy loads.

1. Install Tailscale on the host running Ordo AI Stack and on each device that needs access.
2. Create the Google OAuth client per [docs/runbooks/auth.md](runbooks/auth.md). No new redirect URI is needed for the port model; the OAuth callback stays on `:443`.
3. From the repo root: `ordo remote enable` (hostname, bind address, OAuth client, allowlisted emails; accept the cert offer). For the bind address, the tailnet IPv4 from `tailscale ip -4` binds Caddy to that interface only; `0.0.0.0` is also a supported posture (binds all interfaces, still tailnet-dark since nothing else is published).
4. `ordo up --all`.
5. Browse to `https://${CADDY_TAILNET_HOSTNAME}/` from any tailnet device for the landing page, or go straight to a service's port (e.g. `https://${CADDY_TAILNET_HOSTNAME}:8443/` for Open WebUI). Caddy terminates TLS with the Tailscale-issued cert, oauth2-proxy enforces Google sign-in against the allowlist (`site: SSO_ALLOWED_EMAILS` in `out/ordo.yaml`), and one sign-in covers every port — the SSO cookie is domain-scoped and the post-login redirect carries `{host}` (portless), so a single wildcard `--whitelist-domain=.<domain>` covers every port and clean sidecar name at once. Old subpath bookmarks (`/chat`, `/dash`, `/n8n`, `/comfy`, `/hermes`, `/codebase-memory`, `/grafana`) still work — `:443` 302s them to the matching port.

Traffic between tailnet devices is WireGuard-encrypted; Caddy adds app-layer TLS for the Google OAuth flow and the SSO cookie. Open WebUI's own auth (`WEBUI_AUTH`) is off by default because the proxy already gates it; flip to `True` only if you want per-user workspaces inside Open WebUI on top of the shared SSO gate.

> The Tailscale front door above is **one of three swappable access layers** — the same rendered
> stack can also sit behind a self-hosted public domain or a cloud VM, all keeping the same Google SSO
> gate. See [Access & deployment models](deployment-models.md) for what each requires and how they
> differ (only the Tailscale model is wired by default).

## Next steps

- [Access & deployment models](deployment-models.md) — the three swappable edge layers (Tailscale / domain / cloud)
- [Configuration](configuration.md) — environment variables and service setup
- [Data](data.md) — data schemas, lifecycle, and persistence rules
- [Hermes Agent](hermes-agent.md) — agent setup, Discord wiring, upgrade notes
- [PRD index](product%20requirements%20docs/index.md) — platform design and components
- [MCP tools](../services/model-gateway/README.md): LiteLLM's `/mcp` endpoint aggregating the registered MCP servers (web search, n8n, ComfyUI, code and memory tools)
