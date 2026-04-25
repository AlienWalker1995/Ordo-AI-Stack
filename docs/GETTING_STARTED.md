# Getting Started

Common workflows, the Tailscale + SSO front door, and how to access services.

## Workflows

### I want to chat

1. Bring up the stack: `make up` (decrypts SOPS secrets, then `docker compose up -d`).
2. Wait for `llamacpp` to load the model (`docker compose logs -f llamacpp` until you see `loading model tensors` finish — Qwen3.6 35B-A3B Q4_K_M takes a few minutes on first start).
3. Open the dashboard via SSO at `https://<your-tailnet-host>.<tailnet>.ts.net/`, or jump straight to **Open WebUI** at `http://<tailnet-host>:3000` over Tailscale.

llama.cpp is GPU-only by default (the `overrides/compute.yml` adds `--gpus all` via the NVIDIA reservations stanza). To run CPU-only, drop `llamacpp` from the up list and point the model gateway at a hosted backend.

### I want to generate images (LTX-2)

1. `make up` (auto-detects NVIDIA / AMD / Intel / CPU).
2. Pull LTX-2 model packs from the dashboard's "Model pulls" panel (~60 GB; first run takes a while).
3. Open ComfyUI at `http://<tailnet-host>:8188` over Tailscale.

### I want workflow automation

1. `make up`.
2. Open n8n at `http://<tailnet-host>:5678` over Tailscale.

### Full stack

`make up` brings up everything in `docker-compose.yml` plus whatever's in your `COMPOSE_FILE` (default: `docker-compose.yml;overrides/compute.yml` on Windows, `:` separator on Linux/macOS).

**Hermes dashboard:** `http://<tailnet-host>:9119/` (or whatever `HERMES_DASHBOARD_PORT` is set to). Setup, Discord wiring, upgrade notes: [hermes-agent.md](hermes-agent.md).

### RAG (documents in chat)

Use local files as context in **Open WebUI** via Qdrant + the `rag-ingestion` service.

1. **Embedding model:** `llamacpp-embed` already serves `nomic-embed-text-v1.5.Q4_K_M.gguf` from `models/gguf/` once the stack is up. Drop a different `.gguf` into `models/gguf/` and override `EMBED_MODEL` if you want a different embedder.
2. **Start the RAG profile** (adds Qdrant + `rag-ingestion`):
   ```bash
   docker compose --profile rag up -d
   ```
3. **Drop documents** under `data/rag-input/` (paths follow your `DATA_PATH` / `BASE_PATH`; default is `<repo>/data/rag-input/`). Supported types include `.txt`, `.md`, `.pdf`, and common code extensions — see `rag-ingestion/ingest.py` for `SUPPORTED_EXTENSIONS`.
4. **Open WebUI** → enable RAG for chat (vector DB is pointed at Qdrant via compose).
5. **Check status:** dashboard `GET /api/rag/status`, or the dashboard UI's RAG panel — collection name defaults to `documents` (`RAG_COLLECTION`).

Env knobs (optional, in `.env`): `EMBED_MODEL`, `RAG_COLLECTION`, `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP` — see `.env.example` **RAG** section. The dashboard's RAG section shows Qdrant collection point count when the stack can reach Qdrant.

**Optional — [Agentic Design Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns) (MIT book text):** clone or copy the `.md` tree into `data/rag-input/` (for example `git clone --depth 1 https://github.com/Mathews-Tom/Agentic-Design-Patterns.git data/rag-input/agentic-design-patterns`), then run the steps above so `rag-ingestion` can index it.

### Optional: vLLM (additional OpenAI-compatible backend)

Use vLLM as a second model provider (e.g. for Llama, Mistral via Hugging Face) alongside llama.cpp:

1. `docker compose -f docker-compose.yml -f overrides/vllm.yml --profile vllm up -d`
2. Set in `.env`: `VLLM_URL=http://vllm:8000`
3. Restart model gateway: `docker compose restart model-gateway`
4. In clients (Open WebUI, Hermes), choose models prefixed `vllm/<model-id>` (e.g. `vllm/meta-llama/Llama-3.2-3B-Instruct`).

See [overrides/vllm.yml](../overrides/vllm.yml) for `VLLM_MODEL` and resource limits.

## Tailscale + SSO front door

The default deployment is **Tailscale-only** with a Caddy + oauth2-proxy SSO front door for the dashboard.

### How it works

1. Tailscale is the network gate — services bind to `${CADDY_BIND}` (your tailnet IP) so nothing is reachable from the public internet.
2. Caddy serves HTTPS on `${CADDY_TAILNET_HOSTNAME}` (e.g. `ordo.<tailnet>.ts.net`) using a Tailscale-issued cert at `auth/caddy/certs/tailnet.{crt,key}`.
3. Every request to `/dash/`, `/api/*`, or `/favicon.svg` goes through `forward_auth` to oauth2-proxy. Unauthenticated users are 302'd to Google sign-in; authenticated users have `X-Forwarded-Email` injected for the dashboard's trust-proxy mode.
4. The other UIs (Open WebUI, n8n, ComfyUI, Hermes, MCP, Qdrant) are SPAs that emit absolute `/assets/` and `/api/` URLs and use client-side routing — they can't be cleanly sub-path-mounted under one host. They're reached **directly on the tailnet** at their host ports. Tailscale itself is the gate.

### One-time setup

Full walk-through: [docs/runbooks/auth.md](runbooks/auth.md). Summary:

1. Create a Google OAuth 2.0 Web client; capture client ID + secret into `secrets/.env.sops` as `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET`.
2. Generate a 32-raw-byte cookie secret: `LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 32` → `OAUTH2_PROXY_COOKIE_SECRET` in `secrets/.env.sops`.
3. Issue the Tailscale cert into `auth/caddy/certs/`.
4. Set `CADDY_BIND` in `.env` to your tailnet IP (`tailscale ip -4`).
5. Replace `auth/oauth2-proxy/emails.txt` locally with your real allowlist; `git update-index --skip-worktree auth/oauth2-proxy/emails.txt` to keep your real address out of commits.
6. `make up`.

### Group access

Add more emails to `auth/oauth2-proxy/emails.txt` (one per line) and `docker compose restart oauth2-proxy`. Sessions for removed emails remain valid until cookie expiry (24h max); to force-invalidate, also rotate `OAUTH2_PROXY_COOKIE_SECRET` (`make rotate-internal-tokens`).

### Without SSO (single-user, fully local)

If you really only ever hit the dashboard from `localhost`, you can skip Caddy + oauth2-proxy and keep the dashboard's host port published on `127.0.0.1:8080`. Set `DASHBOARD_TRUST_PROXY_HEADERS=false` and put `DASHBOARD_AUTH_TOKEN` in your bearer header. This isn't the default; read [SECURITY.md](../SECURITY.md) before turning it on.

## Next steps

- [Configuration](configuration.md) — environment variables and service setup
- [Data](data.md) — data schemas, lifecycle, and persistence rules
- [Hermes Agent](hermes-agent.md) — agent setup, Discord wiring, upgrade notes
- [Auth runbook](runbooks/auth.md) — SSO setup, recovery, cert renewal
- [Secrets runbook](runbooks/secrets.md) — SOPS+age, Docker secrets, rotation
- [PRD index](product%20requirements%20docs/index.md) — platform design and components
- [MCP Gateway](../mcp/README.md) — web search, GitHub, etc.
