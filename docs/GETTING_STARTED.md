# Getting Started

Quick paths to common workflows for a single homelab operator. The stack assumes you've completed the one-time auth setup ([docs/runbooks/auth.md](runbooks/auth.md)) and secrets setup ([docs/runbooks/secrets.md](runbooks/secrets.md)), so the Caddy front door is up at `https://${CADDY_TAILNET_HOSTNAME}/` and you can sign in with a Google account on `auth/oauth2-proxy/emails.txt`.

## Workflows

### I want to chat

1. Start: `docker compose up -d caddy oauth2-proxy ollama dashboard open-webui`
2. Pull a model via the dashboard (`https://${CADDY_TAILNET_HOSTNAME}/dash/` → Starter pack, or pick one)
3. Open `https://${CADDY_TAILNET_HOSTNAME}/` — Open WebUI

No GPU required for chat (Ollama runs on CPU, slower but works).

### I want to generate images (LTX-2)

1. Run `./compose up -d` (auto-detects NVIDIA/AMD/Intel/CPU; brings up Caddy + oauth2-proxy + AI services)
2. Pull LTX-2 models via the dashboard (~60 GB, first run takes a while)
3. Open `https://${CADDY_TAILNET_HOSTNAME}/comfy/` — ComfyUI

### I want workflow automation

1. Start: `docker compose up -d caddy oauth2-proxy ollama n8n`
2. Open `https://${CADDY_TAILNET_HOSTNAME}/n8n/` — n8n

### Full stack

**Recommended:** Run the bootstrap sequence (ensure directories, workspace seeds, then start the stack) as detailed in the [README.md Quickstart](../README.md#quickstart). Caddy + oauth2-proxy come up alongside the AI services and front-door them automatically.

Alternatively: `docker compose up -d` — same services without the full bootstrap/rebuild step (use the `compose` wrapper if you want auto hardware detection).

**Hermes dashboard:** `https://${CADDY_TAILNET_HOSTNAME}/hermes/`. See [hermes-agent.md](hermes-agent.md) for setup and Discord configuration.

### RAG (documents in chat)

Use local files as context in **Open WebUI** via Qdrant + the `rag-ingestion` service.

1. **Pull the embedding model** (once): use the dashboard or `docker compose run --rm model-puller` so **`nomic-embed-text`** (or your `EMBED_MODEL`) is available in Ollama.
2. **Start the RAG profile** (adds Qdrant + `rag-ingestion`):
   ```bash
   docker compose --profile rag up -d
   ```
3. **Drop documents** under `data/rag-input/` (paths come from your `DATA_PATH` / `BASE_PATH`; default is `<repo>/data/rag-input/`). Supported types include `.txt`, `.md`, `.pdf`, and common code extensions — see `rag-ingestion/ingest.py` for `SUPPORTED_EXTENSIONS`.
4. **Open WebUI** → enable RAG for chat (vector DB is already pointed at Qdrant in compose).
5. **Check status:** dashboard `GET /api/rag/status` or open the dashboard UI — collection name defaults to `documents` (`RAG_COLLECTION`).

Env knobs (optional, in `.env`): `EMBED_MODEL`, `RAG_COLLECTION`, `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP` — see `.env.example` **RAG** section. The dashboard **RAG** section shows Qdrant collection point count when the stack can reach Qdrant. See the PRD **WS6: RAG Pipeline** for the full picture.

**Optional — [Agentic Design Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns) (MIT book text):** clone or copy the `.md` tree into `data/rag-input/` (for example `git clone --depth 1 https://github.com/Mathews-Tom/Agentic-Design-Patterns.git data/rag-input/agentic-design-patterns`), then run the steps above so `rag-ingestion` can index it.

### Direct Ollama (Cursor, CLI on the host machine)

By default Ollama is backend-only (no host port — host MCP clients should go through `127.0.0.1:11435` model-gateway instead). To expose Ollama directly on the host for tools that speak Ollama's native API:

- Start with the Ollama-expose override:
  `docker compose -f docker-compose.yml -f overrides/ollama-expose.yml up -d`
- Use `http://localhost:11434` in Cursor or run `ollama run <model>` locally.

Note: this exposes Ollama on `127.0.0.1` to the host machine only — not to the tailnet. Tailnet peers reach models through the SSO-gated front door (Open WebUI at `/`, or via the dashboard's model surface).

### Optional: vLLM (OpenAI-compatible server)

Use vLLM as an additional model provider (e.g. for Llama, Mistral via Hugging Face):

1. Start with the vLLM profile:
   `docker compose -f docker-compose.yml -f overrides/vllm.yml --profile vllm up -d`
2. Set in `.env`: `VLLM_URL=http://vllm:8000`
3. Restart model-gateway: `docker compose restart model-gateway`
4. In clients (Open WebUI, Hermes), choose models with prefix `vllm/<model-id>` (e.g. `vllm/meta-llama/Llama-3.2-3B-Instruct`).

See [overrides/vllm.yml](../overrides/vllm.yml) for `VLLM_MODEL` and resource limits.

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
