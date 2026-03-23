# ComfyUI assets — orchestration (OpenClaw + AI-toolkit)

**When to use:** Installing or updating **custom nodes**, **Python deps**, verifying nodes load, or explaining why **`docker`** / **`docker compose exec`** from the gateway fails.

**Read this file** (with **`docker-ops.md`**) before promising “I will install pip packages in ComfyUI” from inside the OpenClaw gateway.

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

- Run **`docker`**, **`docker compose`**, or **`docker exec`** — no socket in **`openclaw-gateway`** (`Permission denied` / `not found` is expected).
- Use **`gateway__run_command`** — **not** a real tool; do not invent it.
- Read **`docker-compose.yml`** from **`/home/node/`** or **`/app/`** — the compose project lives on the **host** repo, not inside this image.
- **`pip install`** into the **`comfyui`** container’s Python — that runs **inside** the ComfyUI image; the gateway only shares **files** via the bind mount, not the venv.

---

## What the gateway agent can do

1. **Read / write / unpack** under **`workspace/comfyui-custom-nodes/`** (same as host `data/comfyui-storage/ComfyUI/custom_nodes/`).
2. **Restart `comfyui`** via **Dashboard API** (requires **`DASHBOARD_AUTH_TOKEN`** in the gateway environment — see **`docker-ops.md`**):
   - `POST` **`$DASHBOARD_URL/api/ops/services/comfyui/restart`** with **`Authorization: Bearer $DASHBOARD_AUTH_TOKEN`**.
3. **Verify** ComfyUI over HTTP from the backend network (no Docker):
   - e.g. **`wget -q -O - http://comfyui:8188/object_info`** and search for node class names.

---

## Python dependencies for a custom node pack

After the pack’s folder exists under **`comfyui-custom-nodes/`**, dependencies must be installed **in the ComfyUI container**:

**Host (operator)** — from the AI-toolkit repo root:

```bash
./scripts/comfyui/install_node_requirements.sh "<path-under-custom_nodes>"
```

```powershell
.\scripts\comfyui\install_node_requirements.ps1 -NodePath "<path-under-custom_nodes>"
```

Example: if Juno lives in `custom_nodes/juno-comfyui-nodes-main`, pass **`juno-comfyui-nodes-main`**. The script runs **`docker compose exec comfyui pip install -r .../requirements.txt`**.

Then **restart `comfyui`** (Dashboard API or host `docker compose restart comfyui`).

**Tell the user** to run the script if **`DASHBOARD_AUTH_TOKEN`** is unset or you cannot complete **`exec`**-equivalent work — do not loop on **`docker`** errors inside the gateway.

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
