# ComfyUI, OpenClaw, and the MCP gateway

Single Streamable HTTP endpoint: **`http://mcp-gateway:8811/mcp`**. OpenClaw uses the **openclaw-mcp-bridge** plugin with that URL only. Brittle behavior is usually **timing** and **tool naming**, not “wrong MCP.”

## Reliability

### Why things flake

1. **Docker MCP Gateway** loads catalog servers (`duckduckgo`, `n8n`, `comfyui`, …) **lazily** — tool lists can be empty briefly after 8811 listens.
2. **Flat OpenClaw tools** (`gateway__comfyui__…`) register after the bridge **discovers** tools. The forked bridge **retries** if zero tools were seen (avoids “never registered” flat tools).
3. **Names** are not guessable: the gateway exposes ids like `comfyui__run_workflow`; OpenClaw flat tools are **`gateway__` + that id**. If the id differs, **`gateway__comfyui__run_workflow`** may not exist.
4. **Video** workloads (LTX, long clips) fail for **VRAM / graph / models**, not MCP.

### Prefer `gateway__call`

Use **`gateway__call`** with **`tool`** set to the **exact** name from the live MCP tool list (injected into agent context). Works even when flat tools failed.

```json
{
  "tool": "comfyui__run_workflow",
  "args": {
    "workflow_id": "generate_image",
    "prompt": "…",
    "width": 720,
    "height": 1280
  }
}
```

**Invalid:** `gateway__run_workflow`, `gateway__generate_image` (not top-level tools).

### Fallbacks

- **Dashboard:** `POST /api/comfyui/install-node-requirements`, service restart, model routes — **`DASHBOARD_AUTH_TOKEN`**.
- **n8n:** MCP Client → **`http://mcp-gateway:8811/mcp`** for visual, stable orchestration.
- **ComfyUI UI:** operators run graphs manually; agents manage files/models.

### Checklist when tools are missing

1. **`data/mcp/servers.txt`** includes **`comfyui`** (comma-separated); wait ~10s or restart **`mcp-gateway`**.
2. **`docker compose ps`** — **`mcp-gateway`**, **`comfyui`**, **`openclaw-gateway`** up; **`comfyui-mcp-image`** built.
3. Restart **`openclaw-gateway`** after bridge/plugin changes.

---

## Parity with n8n (aware, manage, create, run, consume)

**Goal:** Treat ComfyUI similarly to n8n via the **same MCP gateway** plus **disk-backed workflow JSON** (ComfyUI is graph-on-disk, not a REST CRUD API). Those files live under **`data/comfyui-workflows/`** on the host for **comfyui-mcp** / ComfyUI — **not** under **`data/openclaw/workspace/`**.

n8n’s catalog server (name: **`n8n`**, with **`N8N_API_KEY`**) exposes many tools. ComfyUI’s surface is **graphs + checkpoints + GPU** — parity is **conceptual**.

| Intent | n8n | ComfyUI (this stack) |
|--------|-----|----------------------|
| **Discover** | `search_nodes`, `n8n_*` API tools | **`comfyui__list_workflows`**; per-workflow tools for **`PARAM_*`** graphs |
| **Run** | `n8n_trigger_webhook_workflow`, etc. | **`comfyui__run_workflow`** or **`comfyui__generate_image`**-style tools |
| **Create / edit** | MCP + n8n API | **Write** API-format JSON on the host under **`data/comfyui-workflows/`** (comfyui-mcp: **`/workflows`**). OpenClaw does **not** mount this path inside **`…/openclaw/workspace/`** — use MCP from the agent, or edit files on the host. |
| **Delete** | n8n API | Delete the JSON file |
| **Manage deps** | — | **`comfyui__install_custom_node_requirements`**, **`comfyui__restart_comfyui`** ( **`OPS_CONTROLLER_TOKEN`** ) |
| **Models** | — | Dashboard **`/api/comfyui/models`**, **`/api/comfyui/pull`**, puller profile |
| **Outputs** | n8n execution API | **`run_workflow`** response; optional Comfy **`/history`** via **`wget`** if needed |
| **Schedule** | n8n triggers | OpenClaw **cron** + **`gateway__call`**, or **n8n** calling the same gateway |

### Agent workflow

1. **`list_workflows`** before assuming a **`workflow_id`** exists.
2. **Create** graphs as **API** JSON on disk (not UI `nodes`/`links` exports).
3. **`run_workflow`** with overrides (`prompt`, `width`, `height`, …).
4. **Read** structured return for asset paths/URLs.

### Optional future MCP helpers

Thin wrappers in **comfyui-mcp** around Comfy HTTP: queue status, **`get_history`** — not implemented.

---

## Third-party: [ComfyUI-OpenClaw](https://github.com/rookiestar28/ComfyUI-OpenClaw) (embedded pack)

**Different** from this stack: a **custom node** that runs **inside** ComfyUI with **`/openclaw/*`**, connectors, webhooks, and a large security model. **AI-toolkit** keeps **OpenClaw in Docker** + **MCP gateway**. Do not mix the two concepts without an explicit integration plan.

---

## Related

- [automated-social-content-pipeline.md](../../docs/architecture/automated-social-content-pipeline.md) — video → social posting
- [README.md](../README.md) (MCP module), [TROUBLESHOOTING.md](../../docs/runbooks/TROUBLESHOOTING.md), **`agents/docker-ops.md`** (OpenClaw workspace)
