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

### Discord — “This channel is not allowed” / slash commands fail in `#general`

OpenClaw defaults to **`channels.discord.groupPolicy: "allowlist"`**. With allowlist, **your server must appear under `channels.discord.guilds`**, or **every guild channel** (including `#general`) is denied for messages **and native slash commands** like `/new`.

1. Get your **guild (server) ID** from a channel URL: `https://discord.com/channels/<GUILD_ID>/<CHANNEL_ID>` — use **`GUILD_ID`** (not the channel id unless you are configuring per-channel overrides).
2. **AI-toolkit:** set **`OPENCLAW_DISCORD_GUILD_IDS=<GUILD_ID>`** in `.env` (comma-separated for multiple servers), then:
   ```bash
   docker compose run --rm openclaw-config-sync
   docker compose up -d openclaw-gateway
   ```
   `merge_gateway_config.py` will add `channels.discord.guilds.<id>` with `requireMention: false` for new entries. If you already have a `guilds` block, add the id by hand or merge carefully.
3. **Manual:** edit `data/openclaw/openclaw.json` and add:
   ```json
   "channels": {
     "discord": {
       "guilds": {
         "YOUR_GUILD_ID": { "requireMention": false }
       }
     }
   }
   ```
   If a guild has a **`channels`** sub-object listing only some channel names/ids, **unlisted channels are denied** — remove the per-channel list or add `#general` (see [OpenClaw Discord docs](https://docs.openclaw.ai/channels/discord)).

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

### Browser tool, `web_fetch`, and `elevated` (Docker gateway)

Symptoms from **Control UI / webchat** (e.g. “No supported browser”, “Sandbox browser is unavailable”, `web_fetch` **Blocked hostname or private/internal**, **`elevated is not available`**):

| Symptom | Cause | What to do |
|--------|--------|------------|
| **`browser`** / sandbox browser errors | The **`openclaw-gateway`** image does not ship Chrome/Edge; merged config often **`deny`**s the built-in **`browser`** tool. | Use **Tavily MCP** (**`gateway__tavily__tavily_search`**, **`gateway__tavily__tavily_extract`**, **`gateway__call`** + **`tavily_*`**) for web search and page content; **`web_fetch`** for simple HTML. **Pixel screenshots** are not available without a headless browser MCP — this stack uses **[Tavily](https://app.tavily.com)** instead of Playwright. Internal URLs: **`http://dashboard:8080`**, not **`localhost`**. **`tavily`** must be in **`data/mcp/servers.txt`** and **`TAVILY_API_KEY`** in **`.env`**. |
| **“Chrome binary isn’t available” / built-in `browser`** | The model chose OpenClaw’s **`browser`** tool (or **`canvas`** / nodes expectations). No Chromium in the gateway container. | Use **`gateway__tavily__…`** or **`gateway__duckduckgo__search`** — not **`browser`**. Confirm **`http://mcp-gateway:8811/mcp`** in **`openclaw.json`** (bridge) and **`docker compose ps mcp-gateway`** is healthy. |
| **`web_fetch`** blocked private URL | By design (SSRF protection). | **Tavily** / **`exec`+`curl`** to internal hostnames, or stack DNS names (**`http://dashboard:8080`**). |
| **`elevated is not available`** (webchat) | OpenClaw gates **`elevated`** per channel/provider. | **Optional:** **`OPENCLAW_ELEVATED_ALLOW_WEBCHAT=1`**, **`openclaw-config-sync`**, restart gateway. Does not add a browser binary. |
| **Tavily errors / empty tools** | Missing **`TAVILY_API_KEY`**, or **`tavily`** not in **`servers.txt`**. | Set **`TAVILY_API_KEY`** from [app.tavily.com](https://app.tavily.com) in root **`.env`**, restart **`mcp-gateway`**. See **[mcp/README.md](../../mcp/README.md)**. |

Workspace contract: **`openclaw/workspace/TOOLS.md`** and **`AGENTS.md`** (browser bullet).

### OpenClaw — unrestricted `exec` inside the gateway container (downloads, `apt`, etc.)

By default OpenClaw uses conservative **exec** approvals / **`host=sandbox`** behavior. To let the agent run shell commands on the **gateway container** with **`security=full`** and **`ask=off`**, and enable **elevated** for **webchat** and **Discord**:

1. Set **`OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER=1`** in `.env`.
2. Run **`docker compose run --rm openclaw-config-sync`** and restart **`openclaw-gateway`**.

The gateway still runs as the image user (**`node`**) unless you add root:

3. For **`apt install`** and similar, the process usually must be **root**. Use the optional override:
   ```bash
   docker compose -f docker-compose.yml -f overrides/openclaw-gateway-root.yml up -d openclaw-gateway
   ```
   (Re-run **`openclaw-config-sync`** with the same **`-f`** list so env is applied.)

**Security:** this is appropriate only on **trusted** machines. The agent can alter the container filesystem, install packages, and exfiltrate data it can reach on Docker networks. It does **not** grant host-root outside the container unless you mount dangerous volumes.

**Config shape:** OpenClaw **2026.3.x** expects **`tools.elevated.allowFrom.<provider>`** to be a **string array** (e.g. **`["*"]`** for all senders), not **`true`**. If you see **`expected array, received boolean`**, run **`openclaw-config-sync`** again — **`merge_gateway_config.py`** rewrites legacy booleans to **`["*"]`**.

Lighter option (elevated for Control UI only): **`OPENCLAW_ELEVATED_ALLOW_WEBCHAT=1`** without full exec relaxation.

### OpenClaw cron jobs and Discord delivery

Symptoms in **job run history** or the Control UI often **do not match** what you see in Discord. Treat **Discord** as ground truth when they disagree.

| Symptom | Likely meaning | What to do |
|---|---|---|
| `status: "ok"` but `deliveryStatus: "not-delivered"` (often with **`sessionTarget: "isolated"`**) | Cron run finished, but the **delivery hook** did not attach the final reply — can be a **tracking gap**, not “nothing posted”. | Check the **channel** for a new message. If the summary is there, ignore the flag. If not, see rows below. |
| `error: "⚠️ ✉️ Message failed"` | **Discord API rejected** the `message` tool call (permissions, content rules, size, rate limit). | Confirm the bot has **Send Messages** (and **Embed Links** if you use links) in that channel. Shorten the post (Discord default **2000 characters** per message). Split into two messages if needed. |
| `error: "Discord recipient is required…"` | The **`message`** tool was called **without** `to`, or with wrong shape. | Use **`to: "channel:<snowflake>"`** exactly (e.g. `channel:1483464800464797697`). Do not paste only the numeric ID. |
| **`model 'default' not found`** (cron / scheduled job) | **`data/openclaw/cron/jobs.json`** had **`payload.model": "default"`** — that is not a real gateway model id. | Set **`payload.model`** to the same string as **`agents.defaults.model.primary`** in **`openclaw.json`** (e.g. **`gateway/nemotron-cascade-2:latest`**). After changing the primary model, update cron jobs to match. |
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

### OpenClaw Control UI — “Update available” / **Updating…** stalls

In **Docker**, the gateway binary lives in the **image**. The Control UI **Update** action runs an **npm/git-style** update that **cannot replace** `/app` inside the container, so it often **spins forever** on **Updating…**.

**Do this instead:** pull a newer image and recreate the gateway:

```bash
docker compose pull openclaw-gateway
docker compose up -d openclaw-gateway
```

**This repo:** `openclaw-config-sync` runs **`merge_gateway_config.py`**, which sets **`update.checkOnStart: false`** and **`update.auto.enabled: false`** unless **`OPENCLAW_ALLOW_IN_APP_UPDATE=1`** is set in `.env`. Dismiss the banner with **×** if it still appears after restart; see [Updating](https://docs.openclaw.ai/updating) for native (non-Docker) installs.

**`GET requires an Mcp-Session-Id header` or 400 on `http://…:8811/mcp`:** Expected for raw `curl`/browser GET. The MCP gateway speaks the MCP transport; use **`gateway__call`** from OpenClaw or a proper MCP client — not a naked GET probe.

**`missing_brave_api_key` on native `web_search`:** This repo’s **`openclaw.json`** sets **`tools.web.search.enabled: false`** so native **`web_search`** is off — use **`gateway__call`** with **`duckduckgo__search`** (MCP) for web search. To use Brave or another built-in provider instead, set **`tools.web.search.enabled: true`**, configure a provider per [OpenClaw web tools](https://docs.openclaw.ai/tools/web), and set the matching API key in `.env`.

### OpenClaw workspace — `EACCES` / `permission denied` on `MEMORY.md` (or other `*.md`)

The gateway runs as **`node` (uid 1000)**. If workspace files were created **as root** (e.g. manual `docker run`, editor as admin, or an old sync without ownership fix), **`edit` / `write` on `MEMORY.md` fails** inside the container.

**Model catalog / `agents` writes:** Errors such as **`EACCES` on `.../agents/main/agent/models.json*.tmp`** mean the same ownership problem under **`data/openclaw/agents/`** (not only `workspace/`). **`openclaw-workspace-sync`** re-`chown`s the **entire** **`data/openclaw`** tree to **1000:1000**.

**Fix (recommended):** Re-run workspace sync so bind-mounted files are **`chown`’d to 1000:1000** (compose does this after seeding):

```bash
docker compose run --rm openclaw-workspace-sync
docker compose up -d openclaw-gateway
```

**Host-only (Linux):** from the repo, `sudo chown -R 1000:1000 data/openclaw`.

**Windows (host file ACL):** ensure your user (or “Users”) has **Modify** on `data\openclaw` (including `workspace` and `agents`); remove inherited deny if any. If a file was created as Administrator, delete it once or take ownership, then re-run sync.

### Dashboard API — `Bearer token required` / `401`

When **`DASHBOARD_AUTH_TOKEN`** is set in `.env`, most **`/api/*`** routes require:

`Authorization: Bearer <DASHBOARD_AUTH_TOKEN>`

Automated tools and agents calling the dashboard from **inside** the stack should pass this header (see **`TOOLS.md`** §F). **`GET /api/health`** and **`GET /api/dependencies`** stay unauthenticated unless your build changed that.

### ComfyUI — Manager blocks git / pip (`security level` / `This action is not allowed`)

ComfyUI listens on **non-loopback** addresses in Docker, so ComfyUI-Manager defaults to **`normal-`** security and **blocks** high-risk actions (git URL install, pip, some channels). **`scripts/ensure_dirs`** seeds **`data/comfyui-storage/ComfyUI/user/__manager/config.ini`** with **`security_level = weak`** (only if that file does not exist yet). After upgrading ComfyUI, confirm the Manager config path in the startup log and edit **`config.ini`** there if installs still fail. Optional: set **`GITHUB_PERSONAL_ACCESS_TOKEN`** in **`.env`** — compose passes it as **`GITHUB_TOKEN`** to **`comfyui`** for GitHub API rate limits.

### ComfyUI — LTX 2.3 video and `clip input is invalid: None`

**`ltx-2.3-22b`** is not a generic SD1.5 checkpoint graph: plain **`CLIPTextEncode`** off **`CheckpointLoaderSimple`** often yields **no CLIP**. Use the **LTX / Gemma text path** your ComfyUI build documents (e.g. **`LTXAVTextEncoderLoader`** + **`CLIPTextEncodeFlux`** wired to that CLIP), or **`gateway__call`** with **`tool`**: **`comfyui__run_workflow`** (and a **`workflow_id`** that matches your nodes) — see **`TOOLS.md`** and packaged workflows under **`data/comfyui-workflows/`**.

### ComfyUI — MCP `install_custom_node_requirements` / `restart_comfyui` missing or token error

These tools are registered by the **ComfyUI MCP** image (`comfyui-mcp`). They call **ops-controller** and require **`OPS_CONTROLLER_TOKEN`** in `.env`.

- **`mcp-gateway`** must receive **`OPS_CONTROLLER_TOKEN`** and a **`registry-custom.yaml`** whose **`registry.comfyui.env`** includes **`OPS_CONTROLLER_TOKEN`** with value **`PLACEHOLDER_OPS_CONTROLLER_TOKEN`** (repo template: **`mcp/gateway/registry-custom.yaml`**). The gateway **entrypoint** substitutes the token into **`registry-custom.docker.yaml`** and passes that file as **`--additional-catalog`**. If you created **`data/mcp/registry-custom.yaml`** before this layout, **merge** those lines from the repo template or delete the file and re-run **`scripts/ensure_dirs`** so a fresh copy is created (then re-add **`comfyui`** to **`servers.txt`** if needed).
- Rebuild the ComfyUI MCP image after pulling: **`docker compose build comfyui-mcp-image`** (or **`docker compose build comfyui-mcp`**) and restart **`mcp-gateway`** and **`openclaw-gateway`**.

### ComfyUI — `Tool not found` for `gateway__list_comfyui_model_packs` / `gateway__pull_comfyui_models`

The ComfyUI server name must appear **between** `gateway` and the tool id.

| Wrong (not registered) | Correct flat tool | `gateway__call` inner `tool` |
|------------------------|-------------------|-------------------------------|
| `gateway__list_comfyui_model_packs` | `gateway__comfyui__list_comfyui_model_packs` | `comfyui__list_comfyui_model_packs` |
| `gateway__pull_comfyui_models` | `gateway__comfyui__pull_comfyui_models` | `comfyui__pull_comfyui_models` |
| `gateway__gateway__comfyui__…` | (never double-prefix) | one `comfyui__…` only |

**OpenClaw CLI** has **no** `list-model-packs` / `pull-model-pack` subcommands — those errors are expected. **`openclaw gateway <anything>`** also fails (the `gateway` command takes **no** extra arguments in current CLI).

**“Value not in list” / empty ComfyUI dropdowns** for LTX-2.3 (Gemma, projection, VAE, UNET, upscaler): weights are not on disk yet. Pull packs **`ltx-2.3-t2v-basic`** and **`ltx-2.3-extras`** (see **`scripts/comfyui/models.json`**), with **`HF_TOKEN`** if Hugging Face gates the repo. **Host fallback:** `docker compose --profile comfyui-models run --rm comfyui-model-puller` (set **`COMFYUI_PACKS`** / env per **`docker-compose.yml`**) or use the **dashboard** model download UI.

**Workflow stops at `Requested to load VideoVAE` / `docker logs comfyui` shows `Killed`:** The Linux **OOM killer** hit the **ComfyUI container memory limit** (not a CUDA “out of memory” message). LTX + VideoVAE loading spikes **host RAM** (mmap, buffers). **Fix:** set **`COMFYUI_MEMORY_LIMIT`** in **`.env`** (e.g. **`64G`**) and re-run **`python scripts/detect_hardware.py`** to rewrite **`overrides/compute.yml`**, then **`docker compose up -d comfyui`**. Or raise the limit only in **`overrides/compute.yml`** under **`comfyui`** → **`deploy.resources.limits.memory`**. Defaults from **`detect_hardware.py`** were increased for GPU; older overrides may still cap too low.

**LTX Gemma / `cudaErrorInvalidValue` in `sd1_clip.py` / `lt.py` (`torch.cat(...).to(intermediate_device())`):** By default **`intermediate_device()`** is **CPU**, so ComfyUI **GPU → CPU** copies the Gemma output. On some drivers / **RTX 50xx** + **PyTorch cu128** + **pinned allocator** (`pinned_use_cuda_host_register`), that copy can fail with **`cudaErrorInvalidValue`** (often after a prior CUDA error). **Try in order:** (1) **Restart ComfyUI** so the CUDA context is clean. (2) In **`.env`**, set **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** (omit **`pinned_use_cuda_host_register`** — **`overrides/compute.yml`** supports **`${PYTORCH_CUDA_ALLOC_CONF:-…}`**). **Restart comfyui.** (3) Add **`--gpu-only`** to **`COMFYUI_CLI_ARGS`** so intermediates stay on GPU (uses more VRAM). (4) One-off debug: **`CUDA_LAUNCH_BLOCKING=1`** in the comfyui service env for a clearer stack trace.

**Every `gateway__comfyui__*` including `gateway__comfyui__pull_comfyui_models` fails:** MCP tool discovery is empty or the forked bridge never registered flat tools. **`gateway__call`** with **`tool`**: **`comfyui__pull_comfyui_models`** and matching **`args`** still requires the same registry — fix **mcp-gateway** + **ComfyUI MCP** (see **`agents/docker-ops.md`** checklist: **`comfyui`** in **`servers.txt`**, `docker compose build` for **comfyui-mcp** image, **`openclaw-plugin-config`** for forked bridge, restart **`mcp-gateway`** and **`openclaw-gateway`**). **Wrong `read` path:** agent docs live at **`agents/docker-ops.md`** under the workspace root — not **`/app/agents/…`** or **`workspace/agents/…`**.

### ComfyUI — `mcp-gateway` lists only **30** tools (no ComfyUI spawn)

**Symptom:** `docker compose logs mcp-gateway` shows **`> 30 tools listed`** (or similar) and **`Running mcp/...`** lines for **duckduckgo**, **n8n**, **tavily** only — **no** line spawning **`ai-toolkit-comfyui-mcp`** / **no** `> comfyui: (N tools)`. Then **every** `comfyui__*` tool is missing for OpenClaw — not a wrong tool name.

**Cause:** The Docker **MCP Gateway** (`docker/mcp-gateway`) is not actually **starting** the ComfyUI MCP server from **`data/mcp/registry-custom.docker.yaml`**, so those tools never appear in **`tools/list`** and the forked bridge cannot register **`gateway__comfyui__…`**.

**Root cause:** The gateway wrapper must pass the custom file as **`--additional-catalog`**, not **`--additional-registry`**. With **`--servers`** set, Docker MCP Gateway **does not** load **`registry.yaml`** paths for server definitions — only **catalog** merges define images/env for named servers ([`configuration.go` / `readOnce`](https://github.com/docker/mcp-gateway/blob/main/pkg/gateway/configuration.go)). **`--additional-registry`** was effectively ignored for **`comfyui`**, so only catalog servers (~30 tools) appeared.

**YAML:** The fragment must use the catalog key **`registry:`** (not **`servers:`**), same as [Docker’s MCP catalog](https://desktop.docker.com/mcp/catalog/v3/catalog.yaml). Fix **`mcp/gateway/registry-custom.yaml`** / **`data/mcp/registry-custom.yaml`**, ensure **`gateway-wrapper.sh`** uses **`--additional-catalog`**, then restart **`mcp-gateway`**.

**Verify:**

```bash
docker compose logs mcp-gateway 2>&1 | findstr /i "tools listed Running comfyui"
```

You want either a **spawn** line for ComfyUI or a **tool count > 30** once ComfyUI is included.

**If only 30 tools:** use **dashboard** / **ops-controller** for pulls (**`POST /api/comfyui/pull`**, etc.) or **`docker compose --profile comfyui-models run --rm comfyui-model-puller`** — same backends as MCP; see **`agents/docker-ops.md`**.

**Debug:** set **`MCP_GATEWAY_VERBOSE=1`** in **`.env`** (compose passes it into **`mcp-gateway`**) and restart **`mcp-gateway`** — gateway wrapper adds **`--verbose`** so Docker MCP logs **why** a server was skipped. Ensure **`ai-toolkit-comfyui-mcp:latest`** exists (**`docker compose build comfyui-mcp-image`** or service **`comfyui-mcp`**) and **`/var/run/docker.sock`** is mounted on **`mcp-gateway`**.

### ComfyUI — `Tool not found` for `gateway__run_workflow` / OpenClaw

ComfyUI MCP is **only** behind **`mcp-gateway`** (`data/mcp/servers.txt` must list **`comfyui`**). There is no separate OpenClaw `servers.comfyui` URL.

- **Invalid top-level ids:** **`gateway__run_workflow`**, **`gateway__generate_image`** — not registered.
- **Use flat tools (forked bridge):** **`gateway__comfyui__run_workflow`**, **`gateway__comfyui__list_workflows`**, **`gateway__comfyui__generate_image`**, etc.
- **Or `gateway__call`** with **`tool`**: **`comfyui__run_workflow`** (namespaced name from the gateway, not bare **`run_workflow`**).

After changing **`servers.txt`**, wait for gateway reload (~10s) or **`docker compose restart mcp-gateway openclaw-gateway`**.

If ComfyUI tools are missing entirely, confirm **`MCP_GATEWAY_SERVERS`** in compose/dashboard includes **`comfyui`** and **`docker compose build comfyui-mcp-image`** has succeeded (gateway spawns the ComfyUI MCP image).

### ComfyUI — `missing_node_type` / UI workflow JSON

Community packs (e.g. Juno) often ship **ComfyUI UI** format (`"nodes": [ ... ]` with `type`, `widgets_values`). The **`/prompt`** API needs **API** format (top-level keys are node ids, each value has **`class_type`** and **`inputs`**). **Do not** paste UI JSON into MCP. In ComfyUI: load the graph → **Save (API format)** → put that file under **`data/comfyui-workflows/`**. The comfyui-mcp server rejects UI exports with a clear error.

**FL2V / first–last-frame** graphs need **input images**; for **text-only** video use a **T2V** or **I2V/T2V Basic** workflow (see the workflow’s title/description), not FL2V.

### ComfyUI — workflows in subfolders

`list_workflows` includes **`*.json`** under **`data/comfyui-workflows/`** recursively. Use **`workflow_id`** as the **POSIX path without `.json`**, e.g. `juno-comfyui-workflows-main/juno-comfyui-workflows-main/ltx-video/LTX-2.3_-_T2V_Basic` (slashes, not backslashes).

### ComfyUI — custom nodes / `pip` in the ComfyUI venv (host or dashboard)

Custom node packs belong under **`data/comfyui-storage/ComfyUI/custom_nodes/`** on the host (the **`comfyui`** service loads that tree). The OpenClaw gateway does **not** bind-mount that path into its workspace — install or edit packs on the **host** (or use ComfyUI / ComfyUI Manager in the browser on **`comfyui`**).

### ComfyUI — `docker: Permission denied` / agent cannot run `docker exec`

The **OpenClaw gateway** has **no** Docker socket. **`docker`**, **`docker compose exec`**, and **`gateway__run_command`** will not work for **`comfyui`**.

1. **Python deps for a node folder:** **`POST`** **`http://dashboard:8080/api/comfyui/install-node-requirements`** with **`Authorization: Bearer <DASHBOARD_AUTH_TOKEN>`** and JSON **`{"node_path":"<folder-under-custom_nodes>","confirm":true}`**. Requires **`OPS_CONTROLLER_TOKEN`**. **`comfyui`** must be running.
2. **Restart** **`comfyui`:** **`POST`** **`/api/ops/services/comfyui/restart`** with the same Bearer token.
3. **Host fallback:** **`scripts/comfyui/install_node_requirements.sh`** / **`.ps1`**, or **`docker compose restart comfyui`**.

See **`agents/docker-ops.md`** (in the OpenClaw workspace) for compose and ops-controller usage.

## Escalation

- **Security**: See [SECURITY.md](../../SECURITY.md)
- **Architecture**: See [Product Requirements Document](../Product%20Requirements%20Document.md)
- **OpenClaw**: See **OpenClaw** section above and [openclaw/README.md](../../openclaw/README.md).
