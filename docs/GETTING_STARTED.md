# Getting Started

Quick paths to common workflows and Tailscale access.

## Workflows

### I want to chat

1. Start: `docker compose up -d ollama dashboard open-webui`
2. Pull a model via dashboard (Starter pack or pick one)
3. Open [localhost:3000](http://localhost:3000) — Open WebUI

No GPU required for chat (Ollama runs on CPU, slower but works).

### I want to generate images (LTX-2)

1. Run `./compose up -d` (auto-detects NVIDIA/AMD/Intel/CPU)
2. Pull LTX-2 models via dashboard (~60 GB, first run takes a while)
3. Open [localhost:8188](http://localhost:8188) — ComfyUI

### I want workflow automation

1. Start: `docker compose up -d ollama n8n`
2. Open [localhost:5678](http://localhost:5678) — n8n

### Full stack

**Recommended:** Run the bootstrap sequence (ensure directories, workspace seeds, then start the stack) as detailed in the [README.md Quickstart](../README.md#quickstart).

Alternatively: `docker compose up -d` — same services without the full bootstrap/rebuild step (use the `compose` wrapper if you want auto hardware detection).

**OpenClaw Control UI:** `http://localhost:6680/?token=<OPENCLAW_GATEWAY_TOKEN>`.

**Important:** Use `OPENCLAW_GATEWAY_TOKEN` (not `DASHBOARD_AUTH_TOKEN`) for OpenClaw authentication. See [configuration.md](configuration.md) for the full environment variable reference.

**Note on ports:** Port `:6680` is the main OpenClaw Control UI. Port `:6682` is the browser/CDP bridge only (used internally, not the main UI). If `docker compose` fails on OpenClaw services, see [TROUBLESHOOTING — OpenClaw](runbooks/TROUBLESHOOTING.md#openclaw) for log commands.

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

**Optional — [Agentic Design Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns) (MIT book text):** clone or copy the `.md` tree into `data/rag-input/` (for example `git clone --depth 1 https://github.com/Mathews-Tom/Agentic-Design-Patterns.git data/rag-input/agentic-design-patterns`), then run the steps above so `rag-ingestion` can index it. The companion **`agents/agentic-design-patterns.md`** in the OpenClaw workspace is a stack mapping only, not a substitute for the book.

### Direct Ollama (Cursor, CLI)

By default Ollama is backend-only (no host port). To expose it on the host (e.g. for Cursor or `ollama run` from your machine):

- Start with the Ollama-expose override:
  `docker compose -f docker-compose.yml -f overrides/ollama-expose.yml up -d`
- Use `http://localhost:11434` in Cursor or run `ollama run <model>` locally.

### Optional: vLLM (OpenAI-compatible server)

Use vLLM as an additional model provider (e.g. for Llama, Mistral via Hugging Face):

1. Start with the vLLM profile:
   `docker compose -f docker-compose.yml -f overrides/vllm.yml --profile vllm up -d`
2. Set in `.env`: `VLLM_URL=http://vllm:8000`
3. Restart model-gateway: `docker compose restart model-gateway`
4. In clients (Open WebUI, OpenClaw), choose models with prefix `vllm/<model-id>` (e.g. `vllm/meta-llama/Llama-3.2-3B-Instruct`).

See [overrides/vllm.yml](../overrides/vllm.yml) for `VLLM_MODEL` and resource limits.

## Tailscale access

For single user or small group over Tailscale:

1. Install Tailscale on the host running Ordo AI Stack
2. Services bind to `0.0.0.0` — reach them via `http://<tailscale-ip>:<port>`
3. **Single user:** `WEBUI_AUTH=False` is fine (only your devices on the mesh)
4. **Group (family/team):** Set `WEBUI_AUTH=True` in `.env` so each user has their own Open WebUI account

Traffic is encrypted by Tailscale (WireGuard). No TLS at the app layer needed for Tailscale-only access.

## Next steps

- [Configuration](configuration.md) — environment variables and OpenClaw workspace setup
- [Docker Runtime](docker-runtime.md) — core workspace, volumes, and image details
- [Data](data.md) — data schemas, lifecycle, and persistence rules
- [Architecture & PRD](Product%20Requirements%20Document.md) — platform design and components
- [Troubleshooting](runbooks/TROUBLESHOOTING.md) — common issues and fixes
- [MCP Gateway](../mcp/README.md) — web search, GitHub, etc.
