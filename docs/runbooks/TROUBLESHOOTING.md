# Troubleshooting Runbook

## Quick Diagnostics

```bash
# Service status
docker compose ps

# Recent logs
docker compose logs --tail=50

# Health checks
curl -s http://localhost:8080/api/health | jq
curl -s http://localhost:11435/health | jq
curl -s http://localhost:8811/mcp
```

## Common Issues

### Dashboard unreachable (8080)

- **Check**: `docker compose logs dashboard`
- **Causes**: Port conflict, container crash, auth misconfiguration
- **Fix**: Ensure port 8080 is free; check `DASHBOARD_AUTH_TOKEN` / `DASHBOARD_PASSWORD` if set

### Ollama container unhealthy / failed to start

- **Cause:** The official Ollama image has no `curl` or `wget`. A healthcheck that uses `curl` will always fail.
- **Fix:** Use a healthcheck that runs `ollama list` (see docker-compose.yml). If you use a custom image, ensure the healthcheck command exists in the container.

### Model Gateway 502 / Ollama connection refused

- **Check**: `docker compose logs model-gateway` and `docker compose logs ollama`
- **Causes**: Ollama not ready, wrong `OLLAMA_URL`
- **Fix**: Wait for Ollama healthcheck; ensure `OLLAMA_URL=http://ollama:11434` in model-gateway env

### MCP Gateway / tools not loading

- **Check**: `data/mcp/servers.txt` exists and has entries; `docker compose logs mcp-gateway`
- **Causes**: Empty servers.txt, registry.json parse error, Docker socket permission
- **Fix**: Add servers via dashboard or `echo "n8n,playwright,comfyui" > data/mcp/servers.txt`; ensure `registry.json` is valid JSON if present

### OpenClaw: "Tool not found: gateway__comfyui_*" / ComfyUI MCP failing

**Symptoms:** OpenClaw agent tries to call ComfyUI MCP tools (`gateway__comfyui_list_models`, `gateway__comfyui_generate_image`, etc.) and gets "Tool not found". Same pattern for `gateway__playwright`, `gateway__list`, etc.

**Root causes (check in order):**

1. **ComfyUI MCP server not running** — The ComfyUI MCP container must connect to ComfyUI at startup. If ComfyUI (`comfyui:8188`) is not reachable, the MCP server exits immediately.
   - **Check:** `docker compose ps comfyui` — ComfyUI must be running.
   - **Check:** `docker compose logs mcp-gateway` — look for comfyui container spawn errors or "ComfyUI is not available".
   - **Fix:** Start ComfyUI first: `docker compose up -d comfyui`, wait for it to be healthy, then restart: `docker compose restart mcp-gateway`.

2. **ComfyUI MCP image not built** — The gateway uses `ai-toolkit-comfyui-mcp:latest` from the custom registry.
   - **Check:** `docker images ai-toolkit-comfyui-mcp`
   - **Fix:** `docker compose build comfyui-mcp-image` then `docker compose up -d mcp-gateway`

3. **Custom registry not loaded** — The gateway loads `data/mcp/registry-custom.yaml` for the ComfyUI server.
   - **Check:** `data/mcp/registry-custom.yaml` exists and contains `servers.comfyui.image: ai-toolkit-comfyui-mcp:latest`
   - **Fix:** Run `.\scripts\ensure_dirs.ps1` to bootstrap; restart mcp-gateway.

4. **Tool naming mismatch** — Docker MCP Gateway may prefix tools with `servername__` (double underscore). The joenorton/comfyui-mcp-server exposes `list_models`, `view_image`, etc. (no comfyui_ prefix). If the gateway uses tool-name-prefix, tools become `comfyui__list_models`, `comfyui__view_image`. OpenClaw would then see `gateway__comfyui__list_models` (double underscore before `list_models`).
   - **Check:** Dashboard → MCP tab, or call the gateway's tools/list endpoint to see actual tool names.
   - **Fix:** Update `data/openclaw/workspace/AGENTS.md` to document the correct names (e.g. `gateway__comfyui__list_models` if your gateway uses that format).

**Quick diagnostic:**
```powershell
# Is ComfyUI running?
docker compose ps comfyui

# Is the ComfyUI MCP image built?
docker images ai-toolkit-comfyui-mcp

# What does the MCP gateway log?
docker compose logs mcp-gateway --tail=100

# Is servers.txt correct?
Get-Content data\mcp\servers.txt
```

### MCP filesystem: "ENOENT no such file or directory, stat ''"

- **Cause:** The `filesystem` MCP server expects a root directory to be configured. When the gateway starts it without a path (default), it tries to stat an empty path and fails.
- **Fix:** Either remove `filesystem` from `data/mcp/servers.txt` if you don't need file access (e.g. use `n8n,playwright,comfyui` only), or configure the filesystem server with a root directory via the Docker MCP Gateway / registry if your gateway version supports per-server env or volume mounts. Other tools (playwright, n8n) will still work.

### Open WebUI can't reach models

- **Check**: `OPENAI_API_BASE` or `OLLAMA_BASE_URL` in open-webui env
- **Fix**: Point to model gateway: `OPENAI_API_BASE=http://model-gateway:11435/v1` or use `OLLAMA_BASE_URL=http://ollama:11434`

### Ops controller / Start-Stop not working

- **Check**: `OPS_CONTROLLER_TOKEN` set in dashboard and ops-controller; `docker compose logs ops-controller`
- **Causes**: Token mismatch, Docker socket not mounted
- **Fix**: Set same token in both; ensure `/var/run/docker.sock` is mounted in ops-controller

### ComfyUI on Mac Silicon (M1/M2/M3)

- **Cause:** The default ComfyUI image (`yanwk/comfyui-boot:cpu`) is amd64-only and does not run natively on Apple Silicon.
- **Fix:** Run hardware detection so the stack uses an ARM64 image:
  ```bash
  ./scripts/detect_hardware.py
  ```
  On macOS with Apple Silicon this selects `apple_silicon` and writes a compute override that uses `thiagoin/comfyui:arm64` with `platform: linux/arm64`. Then start the stack: `docker compose up -d`. ComfyUI will pull and run the ARM64 image that works on Mac.

### ComfyUI / N8N out of memory

- **Check**: `docker stats`
- **Fix**: Increase `deploy.resources.limits.memory` in docker-compose.yml or add swap

### ComfyUI killed (signal 9) when loading LTX / LTXAV

- **Symptoms**: Logs show `Requested to load LTXAV` (or LTX) then `Killed`; container restarts.
- **Cause**: LTX models use weight offloading to RAM with `--lowvram`. The OOM killer terminates when the container limit is too low.
- **Fix**: Run `./scripts/detect_hardware.py` — it detects host RAM and sets ComfyUI memory limit (GPU modes: 60% of RAM, 12–24G; CPU: 4–16G). To override: set `COMFYUI_MEMORY_LIMIT=24G` in `.env` before re-running detect. Restart: `docker compose up -d comfyui`.

### vLLM not appearing in model list

- **Check**: `VLLM_URL` set in model-gateway env; vLLM service running and healthy
- **Fix**: Add vLLM service to compose if needed; set `VLLM_URL=http://vllm:8000`

### n8n: "Permissions 0777 ... too wide" / "EPERM: operation not permitted"

- **Cause:** n8n tries to tighten permissions on its config file; in some volume setups (e.g. bind mount) the container cannot change ownership/permissions.
- **Fix:** Usually safe to ignore; n8n still runs. To silence the check, set `N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false` in the n8n service environment in docker-compose.

### n8n: "Failed to start Python task runner ... Python 3 is missing"

- **Cause:** n8n's internal Python task runner is for debugging; production expects an external Python runner.
- **Fix:** No action required. Use the JS Task Runner for workflows; for Python nodes, see [n8n external task runner docs](https://docs.n8n.io/hosting/configuration/task-runners/#setting-up-external-mode).

### OpenClaw usage not showing in dashboard throughput

- **Cause:** Throughput is only recorded when requests go through the **model gateway**. If OpenClaw uses the **ollama** provider (direct), traffic bypasses the gateway.
- **Fix:** In OpenClaw Settings → Model, select a model from the **gateway** provider (e.g. `gateway/ollama/deepseek-r1:7b`). Config sync ensures the gateway provider exists in `data/openclaw/openclaw.json`; refresh the model list or restart OpenClaw if you only see `ollama/` models.

### OpenClaw chat blank / no response

- **Symptoms:** Assistant bubbles stay empty; `chat.send 53ms` in openclaw-gateway logs; simple prompts like "What is 2+2?" fail.
- **Cause:** Model gateway was forwarding `gateway/ollama/model:tag` or `ollama/model:tag` directly to Ollama; Ollama expects the bare model name (e.g. `qwen2.5:7b`).
- **Fix:** Ensure model-gateway is rebuilt and restarted with the prefix-stripping fix:
  ```powershell
  docker compose build model-gateway --no-cache
  docker compose up -d model-gateway
  ```
- **Verify:** Direct test should succeed:
  ```powershell
  Invoke-RestMethod -Uri "http://localhost:11435/v1/chat/completions" -Method POST -ContentType "application/json" -Body '{"model":"gateway/ollama/qwen2.5:7b","messages":[{"role":"user","content":"Hi"}],"stream":false}' -TimeoutSec 120
  ```
  If you see `choices` with `finish_reason: stop`, the gateway is working. Then retry OpenClaw chat (refresh the page if needed).

## OpenClaw gateway tool

### "Missing raw parameter" (config.patch)

**Cause:** The gateway tool's `config.patch` action requires a `raw` parameter — a JSON string of the partial config to merge. The agent invoked config.patch without supplying it.

**Fix:** When using `gateway` with `action: "config.patch"`, the agent must pass:
- `raw` — JSON string of the config fragment to merge (e.g. `'{"agents":{"defaults":{"model":{"primary":"gateway/ollama/deepseek-r1:7b"}}}}'`)
- Optionally `baseHash` — from a prior `config.get` snapshot (tool fetches if omitted)

**Guidance for agents:** Add to AGENTS.md or SOUL.md: *"When using gateway config.patch, always pass `raw` as a JSON string of the partial config to merge."*

### "Gateway restart is disabled" (restart)

**Cause:** OpenClaw's `commands.restart` is `false` by default (security). The agent tried to restart the gateway but it's not allowed.

**Fix (if you want the agent to restart the gateway):** Add to `data/openclaw/openclaw.json`:

```json
"commands": {
  "restart": true
}
```

**Security note:** Enabling this lets the agent restart the OpenClaw gateway. Use only if you trust the agent and understand the implications.

### "Device token mismatch" (browser or agent backend)

**Cause:** A client (browser or agent backend) has a stored device token that no longer matches the gateway — e.g. after a gateway/container restart, config change, or container rebuild.

**Fix (manual re-pair / reissue):** You do **not** need to run full `docker compose up` again. Restarting only the OpenClaw Gateway container is enough after fixing tokens. Follow these steps:

1. **Run the built-in fix** (same config volume as the gateway):
   ```powershell
   docker compose --profile openclaw-cli run --rm openclaw-cli doctor --fix
   ```
   This detects token mismatches and regenerates matching tokens in `data/openclaw`.

2. **Restart only the gateway** so it loads the updated tokens:
   ```powershell
   docker compose restart openclaw-gateway
   ```
   Wait until logs show the gateway is listening (e.g. `docker compose logs -f openclaw-gateway`).

3. **Re-pair clients** as needed:
   - **Browser:** Open the gateway URL (e.g. http://localhost:18789) in a private/incognito window or clear site data for that URL, then reconnect and paste the gateway token from the project root `.env` (`OPENCLAW_GATEWAY_TOKEN`). Approve the device if prompted.
   - **Agent backend:** Reconnect from the agent (e.g. Cursor / OpenClaw integration); if it shows a new device/pairing request, approve it (see step 4).

4. **Optional — list/approve/remove devices:**  
   When running the CLI via Docker, the CLI uses local discovery and hits the wrong host. Use the helper script (from repo root), which passes `--url ws://openclaw-gateway:18789` and `--token` from the project root `.env`:
   ```powershell
   .\openclaw\scripts\run-cli.ps1 devices list
   # Approve a pending device (replace DEVICE_ID with the actual id from the list):
   .\openclaw\scripts\run-cli.ps1 devices approve DEVICE_ID
   # Or remove a stuck device so it can re-pair:
   .\openclaw\scripts\run-cli.ps1 devices remove DEVICE_ID
   ```
   If you see `gateway closed (1006)` or "Gateway target: ws://172.18.0.x", you are not using the script (or the script failed to read the token). Use `run-cli.ps1` so the CLI targets the gateway container.

**Prevention:** Pin the gateway token so it does not change on restart: set `OPENCLAW_GATEWAY_TOKEN` in the project root `.env` and keep it unchanged. See [ClawTank: device token mismatch](https://clawtank.dev/blog/openclaw-device-token-mismatch-fix).

### OpenClaw browser tool: agent stuck in a loop ("Opening browser..." / "Navigating...")

**Cause:** Known OpenClaw bug: the browser tool schema marks `targetUrl` as optional, but the runtime requires it for `open` and `navigate`. The agent omits `targetUrl`, gets "targetUrl required", and retries repeatedly (often for minutes).

**Fix:** Ensure the agent always passes `targetUrl` when using the browser tool. In this repo, `openclaw/workspace/AGENTS.md` instructs the agent to always pass `targetUrl` for browser open/navigate. If the agent still loops, try phrasing the user request so the URL is explicit (e.g. "Open https://www.ai-ml-news.com/today and summarize the top AI headlines") or use MCP web search (DuckDuckGo) instead for news lookup. Upstream: [openclaw/openclaw#14700](https://github.com/openclaw/openclaw/issues/14700), [#19964](https://github.com/openclaw/openclaw/issues/19964).

### OpenClaw agent says it will do something but then stops (no tool call)

**Cause:** The agent acknowledged the request but did not actually invoke the MCP tool (e.g. DuckDuckGo). Common with smaller models or when the prompt doesn't force a tool call.

**Fix:** (1) In `openclaw/workspace/AGENTS.md` we instruct the agent to use Playwright or fetch_content for web content. Sync the workspace (`docker compose up openclaw-workspace-sync`) and start a new session. (2) Phrase the request so a tool is required: e.g. "Use Playwright to navigate to example.com and take a snapshot." (3) Check gateway logs: `docker compose logs openclaw-gateway` for MCP or tool errors. (4) Confirm the MCP gateway is reachable from the OpenClaw container and that `playwright` is in `data/mcp/servers.txt`. (5) **Model matters:** If the agent outputs text like "Calling MCP tool..." but never actually invokes the tool (no real tool_call), the model is simulating instead of calling. Switch to a model with better tool-use: `qwen3:14b-q4_K_M`, `deepseek-r1:7b`, or `qwen3-coder:30b` (if you have VRAM). Smaller models often announce tool calls in text without emitting them.

## Log Locations

| Service        | Logs                    |
|----------------|-------------------------|
| Dashboard      | `docker compose logs dashboard` |
| Model Gateway  | `docker compose logs model-gateway` |
| MCP Gateway    | `docker compose logs mcp-gateway` |
| Ops Controller | `docker compose logs ops-controller` |
| Audit          | `data/ops-controller/audit.log` |

### OpenClaw: "Invalid config ... Unrecognized key: mcp"

- **Cause:** Some OpenClaw versions do not support a top-level `mcp` key in `openclaw.json`. Adding it can make the gateway reject the config and fail WebSocket connections (code 4008).
- **Fix:** Remove the `mcp` block from `data/openclaw/openclaw.json` if present. Use the **openclaw-mcp-bridge** plugin instead: add the plugin config under `plugins.entries` in `openclaw.json` as described in [mcp/README.md](../../mcp/README.md#openclaw). This repo’s `data/openclaw/openclaw.json` is pre-configured with the plugin pointing at `http://mcp-gateway:8811`. See [openclaw/README.md](../../openclaw/README.md).

### openclaw-plugin-install fails (plugin manifest not found / exit 1)

- **Cause:** The `openclaw-mcp-bridge` npm package (v0.1.0) omits `openclaw.plugin.json` from its published files, so OpenClaw 2026.2.x fails validation. The gateway **still starts** (it no longer blocks on plugin-install).
- **Fix:** The stack runs without MCP tools in OpenClaw. To enable MCP, install from source or wait for an npm update. See [mcp/README.md](../../mcp/README.md#openclaw). Then add the plugin config and `docker compose restart openclaw-gateway`.

### OpenClaw Discord: bot not receiving or sending messages

- **Cause:** With `groupPolicy: "allowlist"`, no guilds are allowed until explicitly configured. The guild allowlist was empty.
- **Fix:** Add your Discord server (guild) and user to the allowlist in `data/openclaw/openclaw.json`:
  1. Enable Developer Mode in Discord (Settings → Advanced → Developer Mode).
  2. Right-click your server icon → Copy Server ID (guild ID).
  3. Right-click your avatar → Copy User ID.
  4. Edit `data/openclaw/openclaw.json` → `channels.discord.guilds` → add your guild ID as key, with `requireMention: false` and `users: ["YOUR_USER_ID"]`.
  5. Restart: `docker compose restart openclaw-gateway`.

### OpenClaw context truncation / repetitive output (27b model)

- **Cause:** Model gateway caps Ollama KV cache at 16K tokens (`OLLAMA_NUM_CTX=16384`). The qwen3.5-uncensored:27b model has a 200K context window; 16K truncation causes context loss and confused/repetitive output.
- **Fix:** Set `OLLAMA_NUM_CTX=0` in `.env` to use the model's default (full context). Restart: `docker compose up -d model-gateway`. Trade-off: larger KV cache uses more VRAM and can slow CPU inference.

## Escalation

- **Security**: See [SECURITY.md](../../SECURITY.md) (pre-deploy checklist, break-glass)
- **Architecture**: See [Product Requirements Document](../Product%20Requirements%20Document.md)
- **OpenClaw**: See [openclaw/README.md](../../openclaw/README.md)
