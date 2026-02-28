# Getting Started

Quick paths to common workflows and Tailscale access.

## Workflows

### I want to chat

1. Start: `docker compose up -d ollama dashboard open-webui`
2. Pull a model via dashboard (Starter pack or pick one)
3. Open [localhost:3000](http://localhost:3000) — Open WebUI

No GPU required for chat (Ollama runs on CPU, slower but works).

### I want to generate images (LTX-2)

1. Requires NVIDIA GPU
2. Start full stack: `docker compose up -d`
3. Pull LTX-2 models via dashboard (~60 GB, first run takes a while)
4. Open [localhost:8188](http://localhost:8188) — ComfyUI

### I want workflow automation

1. Start: `docker compose up -d ollama n8n`
2. Open [localhost:5678](http://localhost:5678) — n8n

### Full stack

`docker compose up -d` — all services (Ollama, Open WebUI, ComfyUI, n8n, OpenClaw, MCP Gateway).

## Tailscale access

For single user or small group over Tailscale:

1. Install Tailscale on the host running AI-toolkit
2. Services bind to `0.0.0.0` — reach them via `http://<tailscale-ip>:<port>`
3. **Single user:** `WEBUI_AUTH=False` is fine (only your devices on the mesh)
4. **Group (family/team):** Set `WEBUI_AUTH=True` in `.env` so each user has their own Open WebUI account

Traffic is encrypted by Tailscale (WireGuard). No TLS at the app layer needed for Tailscale-only access.

## Next steps

- [Repository structure](STRUCTURE.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [MCP Gateway](../mcp/README.md) — web search, GitHub, etc.
