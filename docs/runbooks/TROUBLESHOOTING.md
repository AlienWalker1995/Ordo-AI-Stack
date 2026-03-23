# Troubleshooting Runbook

## Quick Diagnostics

**Windows + Git Bash:** `docker compose exec <service> cat /path/...` can break because MSYS turns `/home/...` into `C:/Program Files/Git/...`. Use **PowerShell**, **`cmd`**, or prefix: `MSYS_NO_PATHCONV=1 docker compose exec openclaw-gateway cat /home/node/.openclaw/openclaw.json`. Alternatively read the bind-mounted file on the host: `data/openclaw/openclaw.json`.

```bash
# Service status
docker compose ps

# Recent logs
docker compose logs --tail=50

# Health checks (host → published ports)
curl -s http://localhost:8080/api/health | jq
curl -s http://localhost:11435/health | jq
# MCP URL: bare GET often fails (MCP session/protocol). Prefer OpenClaw `gateway__call` or a real MCP client — do not treat HTTP 400 here as “gateway down”.
# curl -s http://localhost:8811/mcp

# Full dependency matrix (same data as Dashboard → Dependencies)
curl -s http://localhost:8080/api/dependencies | jq

# Model Gateway: L2 readiness — HTTP 200 when models are listed; 503 if backends are down or no models
curl -s http://localhost:11435/ready | jq
# Optional: print only status code
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:11435/ready
```

**One-shot host probe + OpenClaw config check** (stack must be up for HTTP checks):

```bash
./scripts/doctor.sh
```

```powershell
.\scripts\doctor.ps1
```

**OpenClaw `openclaw.json`** (typical path: `data/openclaw/openclaw.json`):

```bash
python scripts/validate_openclaw_config.py data/openclaw/openclaw.json
```

The Dashboard **Dependencies** section runs probes **from inside the dashboard container** (Docker DNS names). Optional services (for example Qdrant without the `rag` profile, or ComfyUI if not started) may correctly show as unreachable until those profiles or services are enabled.

**`doctor` script (host):** Uses a long timeout for `GET /api/dependencies` and sends `DASHBOARD_AUTH_TOKEN` from the environment or repo `.env` when probing the dashboard. **WARN** on **HTTP 404** for `/api/dependencies` or `/ready` usually means the **running Docker image is older than the repo** — rebuild `dashboard` and/or `model-gateway` (see README). **FAIL** on Ollama or MCP usually means those services are not listening on localhost (e.g. compose not running or different ports).

## If services fail

Check logs for the failing service:

```bash
docker compose logs <service-name>
```

| Service        | Logs                    |
|----------------|-------------------------|
| Dashboard      | `docker compose logs dashboard` |
| Model Gateway  | `docker compose logs model-gateway` |
| MCP Gateway    | `docker compose logs mcp-gateway` |
| Ops Controller | `docker compose logs ops-controller` |
| OpenClaw gateway | `docker compose logs openclaw-gateway` (see **OpenClaw** below) |

## OpenClaw

### Control UI (correct URL)

- **Use:** `http://localhost:6680/?token=<OPENCLAW_GATEWAY_TOKEN>` — token from the project root `.env` (`OPENCLAW_GATEWAY_TOKEN`).
- **Do not use:** `:6682` for the **Control UI**. Host port **6682** is mapped to a **socat** sidecar that forwards to OpenClaw’s **loopback-only browser/CDP bridge** inside the container. Opening `http://localhost:6682` in a browser is **not** the main gateway Control UI and will confuse debugging.

**Summary:** **6680** = gateway + Control UI (what you want). **6682** = browser/CDP bridge path only.

### Logs (when `docker compose` reports errors involving OpenClaw)

```bash
# Gateway (main process)
docker compose logs --tail=100 openclaw-gateway

# Follow live while reproducing an issue
docker compose logs -f openclaw-gateway

# Sidecar: exposes internal browser UI port to host (6682 → 6685 → 127.0.0.1:6682)
docker compose logs --tail=50 openclaw-ui-proxy

# One-shot config merge before gateway starts (model list, token injection)
docker compose logs openclaw-config-sync

# Workspace file copy (runs before gateway)
docker compose logs openclaw-workspace-sync
```

If the gateway fails healthchecks or exits: confirm **`openclaw-config-sync`** completed successfully (the gateway waits on it). If **`OPENCLAW_GATEWAY_TOKEN`** is missing in `.env`, set one (`openssl rand -hex 32`) and restart: `docker compose up -d openclaw-gateway`.

### Discord / channel token (SecretRef)

If logs show **`Config invalid`** / **`channels.discord.token: Invalid input`** (or Telegram equivalent):

1. OpenClaw **2026.3.x** expects env SecretRefs to include **`"provider": "default"`** alongside `"source": "env"` and `"id": "DISCORD_BOT_TOKEN"` (or your env key id).
2. Ensure the repo’s `openclaw/scripts/merge_gateway_config.py` emits that shape, then run config sync and restart:
   ```bash
   docker compose up -d model-gateway
   docker compose run --rm openclaw-config-sync
   docker compose up -d openclaw-gateway
   ```
3. Or hand-edit `data/openclaw/openclaw.json` under `channels.discord.token` (and `channels.telegram.botToken` if used) to add `"provider": "default"`.
4. Confirm **`DISCORD_TOKEN`** in `.env` is the real bot token (compose passes it as **`DISCORD_BOT_TOKEN`** inside the gateway).

See [SECURITY_HARDENING.md](SECURITY_HARDENING.md) §11 (OpenClaw secrets).

More detail: [openclaw/README.md](../../openclaw/README.md).

### OpenClaw cron jobs and Discord delivery

Symptoms in **job run history** or the Control UI often **do not match** what you see in Discord. Treat **Discord** as ground truth when they disagree.

| Symptom | Likely meaning | What to do |
|---|---|---|
| `status: "ok"` but `deliveryStatus: "not-delivered"` (often with **`sessionTarget: "isolated"`**) | Cron run finished, but the **delivery hook** did not attach the final reply — can be a **tracking gap**, not “nothing posted”. | Check the **channel** for a new message. If the summary is there, ignore the flag. If not, see rows below. |
| `error: "⚠️ ✉️ Message failed"` | **Discord API rejected** the `message` tool call (permissions, content rules, size, rate limit). | Confirm the bot has **Send Messages** (and **Embed Links** if you use links) in that channel. Shorten the post (Discord default **2000 characters** per message). Split into two messages if needed. |
| `error: "Discord recipient is required…"` | The **`message`** tool was called **without** `to`, or with wrong shape. | Use **`to: "channel:<snowflake>"`** exactly (e.g. `channel:1483464800464797697`). Do not paste only the numeric ID. |
| Agent says “search unavailable” / `Tool not found` for `gateway__duckduckgo__search` | With the **stock** npm bridge, that id was never registered (only `gateway__call`). This repo’s **forked** bridge registers namespaced tools — reinstall plugin per [openclaw/extensions/openclaw-mcp-bridge/README-AI-TOOLKIT.md](../../openclaw/extensions/openclaw-mcp-bridge/README-AI-TOOLKIT.md). Otherwise use **`gateway__call`** with **`tool: "duckduckgo__search"`**. |

**Job payload tips (ai-daily-news style):**

1. Require a **real `message` tool call** as the last step; **do not** rely on markdown “delivery notes” or code blocks — those are not Discord posts.
2. If the model still skips `message`, tighten the job text: “You MUST call the message tool once with `to='channel:…'` and the full summary body.”
3. For persistent **`not-delivered`** with no message in Discord, try changing the job’s **session target** in the OpenClaw UI (e.g. away from **isolated** if your version supports it) — see [OpenClaw docs](https://docs.openclaw.ai) for Jobs/scheduler.

### MCP tools — `Tool not found` / `Mcp-Session-Id` / `missing_brave_api_key`

**`Tool not found` for names like `gateway__duckduckgo__search`:** On **upstream** `openclaw-mcp-bridge`, only **`gateway__call`** exists at the top level for a given MCP server; inner MCP names go in **`tool`** + **`args`**. This repo ships a **fork** ([`openclaw/extensions/openclaw-mcp-bridge`](../../openclaw/extensions/openclaw-mcp-bridge/README-AI-TOOLKIT.md)) that also registers each namespaced MCP tool as its own OpenClaw tool — run `docker compose run --rm openclaw-plugin-config` after pull, then restart **`openclaw-gateway`**. Use **one** URL: the Docker MCP gateway (`http://mcp-gateway:8811/mcp`); ComfyUI tools are aggregated there (e.g. **`gateway__comfyui__run_workflow`** or **`gateway__call`** with **`tool`**: **`comfyui__run_workflow`**). **`gateway__comfyui__generate_image`**-style names may still be wrong if ComfyUI uses a different inner tool id.

**Same error for `gateway__n8n__workflow_list`:** Identical mistake — use **`gateway__call`** with inner `tool: "n8n__workflow_list"` (and valid n8n API auth if that tool requires it).

**Not “MCP / DuckDuckGo disabled”:** If **`duckduckgo`** appears in **`data/mcp/servers.txt`** and **`mcp-gateway`** is healthy, the DuckDuckGo MCP server is in the stack.

**Long `AGENTS.md` and bootstrap truncation:** OpenClaw may inject only the **first ~20 000 characters** of **`AGENTS.md`**. Put MCP invocation rules early, or rely on **`TOOLS.md`** (shorter) for the contract.

If **`data/openclaw/workspace/TOOLS.md`** is an old short stub: **`openclaw-workspace-sync`** (and **`scripts/fix_openclaw_workspace_permissions`**) now **replace** it with **`TOOLS.md.example`** when the file lacks the current contract marker (`gateway__duckduckgo__search`). Set **`OPENCLAW_SKIP_TOOLS_MD_UPGRADE=1`** in `.env` to disable this. You can also run **`openclaw/scripts/upgrade_tools_md_from_example.ps1`** (Windows) or **`.sh`** (Linux/Mac) from the repo.

**`GET requires an Mcp-Session-Id header` or 400 on `http://…:8811/mcp`:** Expected for raw `curl`/browser GET. The MCP gateway speaks the MCP transport; use **`gateway__call`** from OpenClaw or a proper MCP client — not a naked GET probe.

**`missing_brave_api_key` on native `web_search`:** This repo’s **`openclaw.json`** sets **`tools.web.search.enabled: false`** so native **`web_search`** is off — use **`gateway__call`** with **`duckduckgo__search`** (MCP) for web search. To use Brave or another built-in provider instead, set **`tools.web.search.enabled: true`**, configure a provider per [OpenClaw web tools](https://docs.openclaw.ai/tools/web), and set the matching API key in `.env`.

### OpenClaw workspace — `EACCES` / `permission denied` on `MEMORY.md` (or other `*.md`)

The gateway runs as **`node` (uid 1000)**. If workspace files were created **as root** (e.g. manual `docker run`, editor as admin, or an old sync without ownership fix), **`edit` / `write` on `MEMORY.md` fails** inside the container.

**Fix (recommended):** Re-run workspace sync so bind-mounted files are **`chown`’d to 1000:1000** (compose does this after seeding):

```bash
docker compose run --rm openclaw-workspace-sync
docker compose up -d openclaw-gateway
```

**Host-only (Linux):** from the repo, `sudo chown -R 1000:1000 data/openclaw/workspace`.

**Windows (host file ACL):** ensure your user (or “Users”) has **Modify** on `data\openclaw\workspace`; remove inherited deny if any. If a file was created as Administrator, delete it once or take ownership, then re-run sync.

### Dashboard API — `Bearer token required` / `401`

When **`DASHBOARD_AUTH_TOKEN`** is set in `.env`, most **`/api/*`** routes require:

`Authorization: Bearer <DASHBOARD_AUTH_TOKEN>`

Automated tools and agents calling the dashboard from **inside** the stack should pass this header (see **`TOOLS.md`** §F). **`GET /api/health`** and **`GET /api/dependencies`** stay unauthenticated unless your build changed that.

### ComfyUI — LTX 2.3 video and `clip input is invalid: None`

**`ltx-2.3-22b`** is not a generic SD1.5 checkpoint graph: plain **`CLIPTextEncode`** off **`CheckpointLoaderSimple`** often yields **no CLIP**. Use the **LTX / Gemma text path** your ComfyUI build documents (e.g. **`LTXAVTextEncoderLoader`** + **`CLIPTextEncodeFlux`** wired to that CLIP), or **`gateway__call`** with **`tool`**: **`run_workflow`** (and a **`workflow_id`** that matches your nodes) — see **`TOOLS.md`** and packaged workflows under **`data/comfyui-workflows/`**.

### ComfyUI — MCP `install_custom_node_requirements` / `restart_comfyui` missing or token error

These tools are registered by the **ComfyUI MCP** image (`comfyui-mcp`). They call **ops-controller** and require **`OPS_CONTROLLER_TOKEN`** in `.env`.

- **`mcp-gateway`** must receive **`OPS_CONTROLLER_TOKEN`** and a **`registry-custom.yaml`** that includes **`OPS_CONTROLLER_TOKEN: PLACEHOLDER_OPS_CONTROLLER_TOKEN`** (repo template: **`mcp/registry-custom.yaml`**). The gateway **entrypoint** substitutes the token into **`registry-custom.docker.yaml`**. If you created **`data/mcp/registry-custom.yaml`** before this layout, **merge** those lines from the repo template or delete the file and re-run **`scripts/ensure_dirs`** so a fresh copy is created (then re-add **`comfyui`** to **`servers.txt`** if needed).
- Rebuild the ComfyUI MCP image after pulling: **`docker compose build comfyui-mcp-image`** (or **`docker compose build comfyui-mcp`**) and restart **`mcp-gateway`** and **`openclaw-gateway`**.

### ComfyUI — `Tool not found` for `gateway__comfyui__run_workflow` / OpenClaw

The MCP bridge registers **`gateway__call`** first. For ComfyUI, pass the **inner** tool name from the ComfyUI MCP server (usually plain names: **`run_workflow`**, **`list_workflows`**, **`generate_image`**):

- **`gateway__call`** with **`tool`**: **`run_workflow`** and **`args`**: `{ "workflow_id": "…", … }` — this resolves to **`gateway__run_workflow`** (not `gateway__comfyui__run_workflow`).

If **`gateway__run_workflow`** still fails, the Docker MCP gateway may prefix tool names with the backend id — try **`tool`**: **`comfyui__run_workflow`**. After changing MCP servers, restart **`openclaw-gateway`** so flat tools re-register.

### ComfyUI — `missing_node_type` / UI workflow JSON

Community packs (e.g. Juno) often ship **ComfyUI UI** format (`"nodes": [ ... ]` with `type`, `widgets_values`). The **`/prompt`** API needs **API** format (top-level keys are node ids, each value has **`class_type`** and **`inputs`**). **Do not** paste UI JSON into MCP. In ComfyUI: load the graph → **Save (API format)** → put that file under **`data/comfyui-workflows/`**. The comfyui-mcp server rejects UI exports with a clear error.

**FL2V / first–last-frame** graphs need **input images**; for **text-only** video use a **T2V** or **I2V/T2V Basic** workflow (see the workflow’s title/description), not FL2V.

### ComfyUI — workflows in subfolders

`list_workflows` includes **`*.json`** under **`data/comfyui-workflows/`** recursively. Use **`workflow_id`** as the **POSIX path without `.json`**, e.g. `juno-comfyui-workflows-main/juno-comfyui-workflows-main/ltx-video/LTX-2.3_-_T2V_Basic` (slashes, not backslashes).

### ComfyUI — custom nodes installed in the wrong place (OpenClaw vs ComfyUI)

`exec`/`read`/`edit` in the **OpenClaw gateway** container must **not** install into **`/app/ComfyUI/...`** — that path is **not** the ComfyUI service. Use **`workspace/comfyui-custom-nodes/`**, which binds to **`data/comfyui-storage/ComfyUI/custom_nodes/`** (the same directory the **`comfyui`** container loads). Restart **`comfyui`** after adding nodes. See **`workspace/agents/docker-ops.md`**.

### ComfyUI — `docker: Permission denied` / agent cannot run `docker exec`

The **OpenClaw gateway** has **no** Docker socket. **`docker`**, **`docker compose exec`**, and **`gateway__run_command`** will not work for **`comfyui`**.

1. **Files:** place or edit custom node trees under **`data/comfyui-storage/ComfyUI/custom_nodes/`** (same as **`workspace/comfyui-custom-nodes/`** in the gateway).
2. **Python requirements (from OpenClaw):** **`POST`** **`http://dashboard:8080/api/comfyui/install-node-requirements`** with **`Authorization: Bearer <DASHBOARD_AUTH_TOKEN>`** and JSON **`{"node_path":"<folder-under-custom_nodes>","confirm":true}`**. Requires **`OPS_CONTROLLER_TOKEN`** (dashboard → ops-controller). **`comfyui`** must be running.
3. **Restart** **`comfyui`:** **`POST`** **`/api/ops/services/comfyui/restart`** with the same Bearer token.
4. **Host fallback:** **`scripts/comfyui/install_node_requirements.sh`** / **`.ps1`**, or **`docker compose restart comfyui`**.

Full playbook: **`openclaw/workspace/agents/comfyui-assets.md`** (synced into **`data/openclaw/workspace/agents/`** when **`openclaw-workspace-sync`** runs).

## Escalation

- **Security**: See [SECURITY.md](../../SECURITY.md)
- **Architecture**: See [Product Requirements Document](../Product%20Requirements%20Document.md)
- **OpenClaw**: See **OpenClaw** section above and [openclaw/README.md](../../openclaw/README.md).
