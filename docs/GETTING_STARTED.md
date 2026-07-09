# Getting Started

> ⚠️ **LEGACY (V1) — superseded by v2/ on 2026-07-09.** Production now runs from the v2 substrate; the authoritative getting-started + operator doc is **[`../v2/README.md`](../v2/README.md)** (bring-up via `ordo render` → `docker compose -p ordo-v2 … up`, see [`../v2/CUTOVER.md`](../v2/CUTOVER.md)). The `./compose` / `docker compose` bring-up steps below drive the retired V1 stack and are kept for historical reference / rollback only. See [`LEGACY-CLEANUP.md`](LEGACY-CLEANUP.md).

Quick paths to common workflows for a single homelab operator. The stack assumes you've completed the one-time auth setup ([docs/runbooks/auth.md](runbooks/auth.md)) and secrets setup ([docs/runbooks/secrets.md](runbooks/secrets.md)), so the Caddy front door is up at `https://${CADDY_TAILNET_HOSTNAME}/` and you can sign in with a Google account on `auth/oauth2-proxy/emails.txt`.

## Workflows

### I want to chat

1. Start: `docker compose up -d caddy oauth2-proxy llamacpp dashboard open-webui`
2. Pull a model via the dashboard (`https://${CADDY_TAILNET_HOSTNAME}/dash/` → Starter pack, or pick one)
3. Open `https://${CADDY_TAILNET_HOSTNAME}/` — Open WebUI

No GPU required for chat (llama.cpp runs on CPU, slower but works).

### I want to generate images (LTX-2)

1. Run `./compose up -d` (auto-detects NVIDIA/AMD/Intel/CPU; brings up Caddy + oauth2-proxy + AI services)
2. Pull LTX-2 models via the dashboard (~60 GB, first run takes a while)
3. Open `https://${CADDY_TAILNET_HOSTNAME}/comfy/` — ComfyUI

### I want workflow automation

1. Start: `docker compose up -d caddy oauth2-proxy llamacpp n8n`
2. Open `https://${CADDY_TAILNET_HOSTNAME}/n8n/` — n8n

### Full stack

**Recommended:** Run the bootstrap sequence (ensure directories, workspace seeds, then start the stack) as detailed in the [README.md Quickstart](../README.md#quickstart). Caddy + oauth2-proxy come up alongside the AI services and front-door them automatically.

Alternatively: `docker compose up -d` — same services without the full bootstrap/rebuild step (use the `compose` wrapper if you want auto hardware detection).

**Hermes dashboard:** `https://${CADDY_TAILNET_HOSTNAME}/hermes/`. See [hermes-agent.md](hermes-agent.md) for setup and Discord configuration.

### RAG (documents in chat)

Use local files as context in **Open WebUI** via Qdrant + the `rag-ingestion` service.

1. **Provide the embedding model** (once): place the embedding GGUF (**`nomic-embed-text`**, or your `EMBED_MODEL`) under `models/gguf/` so the `llamacpp-embed` service can serve it.
2. **Start the RAG profile** (adds Qdrant + `rag-ingestion`):
   ```bash
   docker compose --profile rag up -d
   ```
3. **Drop documents** under `data/rag-input/` (paths come from your `DATA_PATH` / `BASE_PATH`; default is `<repo>/data/rag-input/`). Supported types include `.txt`, `.md`, `.pdf`, and common code extensions — see `rag-ingestion/ingest.py` for `SUPPORTED_EXTENSIONS`.
4. **Open WebUI** → enable RAG for chat (vector DB is already pointed at Qdrant in compose).
5. **Check status:** dashboard `GET /api/rag/status` or open the dashboard UI — collection name defaults to `documents` (`RAG_COLLECTION`).

Env knobs (optional, in `.env`): `EMBED_MODEL`, `RAG_COLLECTION`, `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP` — see `.env.example` **RAG** section. The dashboard **RAG** section shows Qdrant collection point count when the stack can reach Qdrant. See the PRD **WS6: RAG Pipeline** for the full picture.

**Optional — [Agentic Design Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns) (MIT book text):** clone or copy the `.md` tree into `data/rag-input/` (for example `git clone --depth 1 https://github.com/Mathews-Tom/Agentic-Design-Patterns.git data/rag-input/agentic-design-patterns`), then run the steps above so `rag-ingestion` can index it.

### Host tools (Cursor, CLI on the host machine)

The llama.cpp backend is internal (no host port). Host tools reach the models through the model-gateway's OpenAI-compatible API on `127.0.0.1:11435`:

- Point Cursor or any OpenAI-compatible client at `http://localhost:11435/v1`.
- This is bound to `127.0.0.1` on the host machine only — not to the tailnet. Tailnet peers reach models through the SSO-gated front door (Open WebUI at `/`, or via the dashboard's model surface).

## Tailscale + SSO front door

Single homelab operator with a small Google-account allowlist for friends / family / co-workers — that's the deployment model. UI services don't publish host ports; everything goes through Caddy on the tailnet.

1. Install Tailscale on the host running Ordo AI Stack and on each device that needs access.
2. Issue a Tailscale cert for your chosen hostname: `tailscale cert ordo.<tailnet>.ts.net` (writes to `auth/caddy/certs/`).
3. Set `CADDY_BIND` to the tailnet IPv4 from `tailscale ip -4`, and `CADDY_TAILNET_HOSTNAME` to the hostname you certified.
4. Set up the Google OAuth client and email allowlist per [docs/runbooks/auth.md](runbooks/auth.md).
5. Browse to `https://${CADDY_TAILNET_HOSTNAME}/` from any tailnet device — Caddy terminates TLS with the Tailscale-issued cert, oauth2-proxy enforces Google sign-in against `auth/oauth2-proxy/emails.txt`, then the front door routes to Open WebUI (root), the dashboard (`/dash/`), n8n (`/n8n/`), ComfyUI (`/comfy/`), and Hermes (`/hermes/`).

Traffic between tailnet devices is WireGuard-encrypted; Caddy adds app-layer TLS for the Google OAuth flow and the SSO cookie. Open WebUI's own auth (`WEBUI_AUTH`) is off by default because the proxy already gates it; flip to `True` only if you want per-user workspaces inside Open WebUI on top of the shared SSO gate.

## Next steps

- [Configuration](configuration.md) — environment variables and service setup
- [Data](data.md) — data schemas, lifecycle, and persistence rules
- [Hermes Agent](hermes-agent.md) — agent setup, Discord wiring, upgrade notes
- [PRD index](product%20requirements%20docs/index.md) — platform design and components
- [MCP Gateway](../mcp/README.md) — web search, GitHub, etc.
