# Docker stack ops (models, ComfyUI, MCP)

**Path:** In the OpenClaw gateway, read this file as **`agents/docker-ops.md`** (workspace root = OpenClaw workspace mount). Do **not** use **`/app/agents/…`** or **`workspace/agents/…`** — those paths are wrong in the container.

Use this when **`AGENTS.md`** / **`TOOLS.md`** point here for **compose**, **model pulls**, and **ops-controller** — not from guessed OpenClaw CLI names.

## Ollama / chat models (OpenClaw `gateway/…`)

- **Dashboard:** `http://dashboard:8080` (from host, map port if needed) — pull Ollama models and sync **`openclaw.json`** when offered.
- **Compose profile `models`:** `docker compose --profile models run --rm model-puller` — set **`MODELS`** in **`.env`** (comma-separated tags).
- **API:** **`model-gateway`** proxies to Ollama; **`ollama pull`** on the host only affects models if **`ollama`** is the same volume as compose.

## ComfyUI weights (large HF downloads)

**MCP (correct tool ids):**

- **`gateway__comfyui__list_comfyui_model_packs`**
- **`gateway__comfyui__pull_comfyui_models`** with **`packs`**, **`confirm: true`**
- **`gateway__comfyui__get_comfyui_model_pull_status`**

**Or** **`gateway__call`** with **`tool`**: **`comfyui__pull_comfyui_models`** and **`args`**: **`{ "packs": "ltx-2.3-t2v-basic,ltx-2.3-extras", "confirm": true }`** (same **`comfyui__…`** prefix as **`comfyui__run_workflow`** in **mcp/docs/comfyui-openclaw.md** — not the bare Python function name alone).

### If *all* `gateway__comfyui__*` tools are “Tool not found”

Flat tools only register after the MCP bridge **discovers** tools from **`mcp-gateway`**. **`gateway__call`** also fails if the registry is empty.

1. **Host:** `docker compose ps` — **`mcp-gateway`**, **`comfyui`**, **`openclaw-gateway`** up; **`data/mcp/servers.txt`** includes **`comfyui`** (comma-separated).
2. **Rebuild / plugin:** `docker compose build comfyui-mcp-image` (or your compose service name for ComfyUI MCP), then `docker compose --profile openclaw-setup run --rm openclaw-plugin-config` so the **forked** **`openclaw-mcp-bridge`** from **`openclaw/extensions/`** installs into the extensions volume, then **`docker compose restart mcp-gateway openclaw-gateway`**.
3. **Wait** ~10–30s after **`mcp-gateway`** is healthy so tool lists are non-empty.
4. **Fallback:** run **`comfyui-model-puller`** on the host (below) or **dashboard** pull with **`DASHBOARD_AUTH_TOKEN`**.

**ACP / subagent errors** (“target agent is not configured”) are **unrelated** to MCP tool names — do not use subagents for ComfyUI pulls unless **`acp.defaultAgent`** is configured.

**LTX-2.3 “Basic” (Kijai-style graphs):** packs **`ltx-2.3-t2v-basic`**, **`ltx-2.3-extras`** — see **`scripts/comfyui/models.json`**.

**Without MCP:** dashboard authenticated routes, or host:

`COMFYUI_PACKS=ltx-2.3-t2v-basic,ltx-2.3-extras docker compose --profile comfyui-models run --rm comfyui-model-puller`

(override **`COMFYUI_PACKS`** as needed; **`HF_TOKEN`** in **`.env`** for gated repos.)

## What does *not* work

- **`openclaw list-model-packs`**, **`openclaw pull-model-pack`**, **`openclaw gateway …`** with extra args — **not** valid commands for this.
- **`gateway__list_comfyui_*`** (missing **`comfyui`** segment) — **Tool not found**; use **`gateway__comfyui__…`**.
- **`read`** on **`/comfyui/models`** from the OpenClaw gateway — that path is not the workspace; ComfyUI data lives on the **`comfyui`** / **`models`** volume (see **`docker-compose.yml`**).

## See also

- **`TOOLS.md`** — full MCP naming.
- **`docs/runbooks/TROUBLESHOOTING.md`** — ComfyUI MCP, tokens, **`Tool not found`** tables.
