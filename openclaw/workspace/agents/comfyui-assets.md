# ComfyUI assets — orchestration (OpenClaw + AI-toolkit)

**When to use:** Installing or updating **custom nodes**, **Python deps**, verifying nodes load, or explaining why **`docker`** / **`docker compose exec`** from the gateway fails.

**Read this file** (with **`docker-ops.md`**) for the full ComfyUI lifecycle: files in **`comfyui-custom-nodes/`**, **MCP** tools **`install_custom_node_requirements`** / **`restart_comfyui`** (same gateway as **`run_workflow`**), optional Dashboard HTTP fallback.

---

## One mental model

| Layer | Role |
|--------|------|
| **Host** (`data/comfyui-storage/ComfyUI/custom_nodes/`) | Single source of truth on disk. |
| **OpenClaw** (`workspace/comfyui-custom-nodes/`) | **Same bind mount** as the row above — not a copy step. |
| **`comfyui` container** (`/root/ComfyUI/custom_nodes/`) | **Same host directory** — ComfyUI loads nodes from here. |

**Workflow JSON** for MCP (`run_workflow`) lives elsewhere: **`/comfyui-workflows/`** in the gateway → host `data/comfyui-workflows/`. Do not confuse **workflow packs** with **custom node** installs.

---

## What the gateway agent cannot do

- Run **`docker`**, **`docker compose`**, or **`docker exec`** directly — no socket in **`openclaw-gateway`** (`Permission denied` / `not found` is expected).
- Use **`gateway__run_command`** — **not** a real tool; do not invent it.
- Read **`docker-compose.yml`** from **`/home/node/`** or **`/app/`** — the compose project lives on the **host** repo, not inside this image.

---

## What the gateway agent can do (manage ComfyUI end-to-end)

**Preferred — MCP (same as other tools):** use **`gateway__call`** with the ComfyUI server’s inner tool names (see **`TOOLS.md`**):

- **`install_custom_node_requirements`** — args **`node_path`**, **`confirm: true`** (runs **`pip install -r`** via ops-controller).
- **`restart_comfyui`** — args **`confirm: true`** (restarts the **`comfyui`** service).

Flat tools may appear as **`gateway__comfyui__install_custom_node_requirements`** and **`gateway__comfyui__restart_comfyui`**. Requires **`OPS_CONTROLLER_TOKEN`** in `.env` so the ComfyUI MCP container can call ops-controller (see **`mcp/registry-custom.yaml`**; gateway substitutes the token at startup).

1. **Read / write / unpack** under **`workspace/comfyui-custom-nodes/`** (same as host `data/comfyui-storage/ComfyUI/custom_nodes/`).
2. **Verify** nodes: **`wget -q -O - http://comfyui:8188/object_info`** and search for expected class names.

**Fallback — Dashboard HTTP** (if MCP tools unavailable): **`POST`** **`$DASHBOARD_URL/api/comfyui/install-node-requirements`** and **`POST`** **`…/api/ops/services/comfyui/restart`** with **`Authorization: Bearer $DASHBOARD_AUTH_TOKEN`** — see **`docker-ops.md`**.

Example (**`exec`** wget fallback):

```bash
wget -q -O - \
  --header="Authorization: Bearer $DASHBOARD_AUTH_TOKEN" \
  --header="Content-Type: application/json" \
  --post-data='{"node_path":"juno-comfyui-nodes-main","confirm":true}' \
  "$DASHBOARD_URL/api/comfyui/install-node-requirements"
```

---

## Python dependencies (host fallback)

If Dashboard auth or ops is unavailable, from the **host** repo root:

```bash
./scripts/comfyui/install_node_requirements.sh "<path-under-custom_nodes>"
```

```powershell
.\scripts\comfyui\install_node_requirements.ps1 -NodePath "<path-under-custom_nodes>"
```

Do **not** loop on **`docker`** errors **inside** the gateway — use **MCP** or the Dashboard **`POST`** when **`OPS_CONTROLLER_TOKEN`** is configured.

---

## Folder layout (Juno and similar)

Upstream often documents **`ComfyUI/custom_nodes/Juno-ComfyUI/`**. If the archive unpacks as **`juno-comfyui-nodes-main/...`**, either:

- Move or rename so a **single** package directory with **`__init__.py`** at the expected level sits under **`custom_nodes/`**, or  
- Keep the nested name but confirm **`object_info`** lists the **Juno/** node types after restart.

If **`comfyui-custom-nodes`** entries are **root-owned** and the **`node`** user cannot write, fix ownership on the **host** (see **TROUBLESHOOTING** — OpenClaw workspace permissions) or re-run **`openclaw-workspace-sync`**.

---

## LiteLLM / proxy nodes (Nano Banana, Veo, etc.)

Workflows may reference **`http://localhost:4000`**. Inside the **`comfyui`** container, **`localhost`** is **that container**, not the host or another stack service.

- Point **Juno Proxy Config** at **`http://host.docker.internal:4000`** (host proxy; Docker Desktop) **or** at **`http://<service-name>:<port>`** if you add a LiteLLM service to compose.

---

## Long-running “setup” cron jobs

If a cron job was created to **poll** Juno setup, **remove or disable** it in OpenClaw’s job list when setup is done — otherwise reminders keep firing after the agent “stops heartbeat.”

---

## Escalation

- Service lifecycle, logs, model downloads: **`docker-ops.md`**
- MCP **`run_workflow`**, UI vs API JSON, **`workflow_id`**: **`TOOLS.md`**, **TROUBLESHOOTING**
