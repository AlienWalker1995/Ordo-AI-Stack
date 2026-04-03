# OpenClaw — Personal AI Assistant

[OpenClaw](https://docs.openclaw.ai) is a self-hosted personal AI assistant that runs in Docker. This folder provides a ready-to-use setup integrated with the Ordo AI Stack project.

**Manual sync (re-run `merge_gateway_config`, workspace seed, MCP plugin config):** see [Configuration — Re-run OpenClaw sync](../docs/configuration.md#re-run-openclaw-sync-manual) for exact `docker compose run --rm …` service names (there is no `openclaw-merge-config`).

## Quick Start

### 1. Prepare directories and workspace

From the **repo root** (e.g. `F:\ordo-ai-stack`), the **main** path is **`.\ordo-ai-stack.ps1 initialize`** (runs `ensure_dirs`, OpenClaw workspace seeding, then full compose — see root [README.md](../README.md)).

Manual equivalent:

```powershell
$env:BASE_PATH = "F:/ordo-ai-stack"
.\scripts\ensure_dirs.ps1
.\openclaw\scripts\ensure_openclaw_workspace.ps1
```

Linux/Mac: `openclaw/scripts/ensure_openclaw_workspace.sh` (same behavior as the `.ps1`).

### 2. Configure environment

```powershell
cd openclaw
copy .env.example .env
```

Edit `.env` and set:

- `BASE_PATH` — repo root (e.g. `F:/ordo-ai-stack`)
- `OPENCLAW_GATEWAY_TOKEN` — generate with `openssl rand -hex 32`
- **Ollama** — enabled by default when using the main compose; models from `ollama` are auto-discovered
- Optional cloud APIs: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `OPENROUTER_API_KEY`
- **Discord / Telegram (optional):** set `DISCORD_TOKEN` and/or `TELEGRAM_BOT_TOKEN` in the **project root** `.env` (same file the main compose uses). On startup, `openclaw-config-sync` runs `openclaw/scripts/merge_gateway_config.py`, which rewrites channel entries in `data/openclaw/openclaw.json` to OpenClaw **SecretRef** form (`source`, **`provider: default`**, `id` — required on OpenClaw 2026.3.x) so bot tokens are not stored as plaintext in the JSON. The same sync step now also normalizes `data/openclaw/cron/jobs.json` so Discord announce jobs use `delivery.to: "channel:<id>"` instead of a brittle bare snowflake. See [OPENCLAW_SECURE.md](OPENCLAW_SECURE.md) §4 and [TROUBLESHOOTING — OpenClaw](../docs/runbooks/TROUBLESHOOTING.md#openclaw) if the gateway reports invalid channel token.
- **Plugin trust:** `openclaw-config-sync` also pins `plugins.allow` to include `openclaw-mcp-bridge`, so the MCP bridge is treated as an explicit trusted stack component instead of an opportunistic local plugin.

### 3. Start OpenClaw

```powershell
docker compose up -d openclaw-gateway
```

### 4. Onboarding (first run)

```powershell
docker compose run --rm openclaw-cli onboard --no-install-daemon
```

Use the token from `.env` when prompted.

### 5. Access the UI

With the **main** `docker-compose.yml`, the web Control UI is on the **gateway** port (default **6680**). Open **`http://localhost:6680/?token=<OPENCLAW_GATEWAY_TOKEN>`** (token from `.env`). In Settings → Model, choose a model from the **gateway** provider (e.g. `gateway/ollama/deepseek-r1:7b`) — this routes through the Model Gateway so dashboard monitoring shows performance. MCP tools (web search, etc.) from the gateway at `http://mcp-gateway:8811/mcp` are exposed via a **forked** **openclaw-mcp-bridge** (see [`extensions/openclaw-mcp-bridge/README-ORDO-AI-STACK.md`](extensions/openclaw-mcp-bridge/README-ORDO-AI-STACK.md)) — namespaced tools such as `gateway__duckduckgo__search` are registered as first-class OpenClaw tools, not only `gateway__call`. **Do not use :6682** for this UI — that port is the browser/CDP bridge only.

If you use **`overrides/openclaw-secure.yml`**, the mapped gateway port is typically **18789** on localhost — see [OPENCLAW_SECURE.md.example](OPENCLAW_SECURE.md.example).

**Existing config?** Ensure `plugins.entries["openclaw-mcp-bridge"]` is set as in [mcp/README.md](../mcp/README.md#openclaw); this repo’s `data/openclaw/openclaw.json` already includes it.

**Updating the gateway (Docker):** Default image is **`ghcr.io/openclaw/openclaw`** (official [GHCR package](https://github.com/openclaw/openclaw/pkgs/container/openclaw)); compose now pins **`2026.4.1`** by default. Override with **`OPENCLAW_IMAGE`** in `.env` if needed. The Control UI **Update** button runs **`openclaw update`**-style flows (npm/git) that **cannot replace** the gateway binary inside the image, so it often **hangs on “Updating…”**. This stack disables in-app update checks in **`openclaw.json`** (`update.checkOnStart` / `update.auto.enabled`) via **`openclaw/scripts/merge_gateway_config.py`**. To upgrade: **`docker compose pull`** then **`docker compose up -d openclaw-gateway`**, or bump the pinned tag in **`docker-compose.yml`**. Set **`OPENCLAW_ALLOW_IN_APP_UPDATE=1`** in `.env` only if you intentionally re-enable UI-driven updates (still unlikely to work in a standard image-only setup). See [Updating](https://docs.openclaw.ai/updating) for native installs.

**Not reachable?** When using the main Ordo AI Stack compose, the gateway is configured with `OPENCLAW_GATEWAY_BIND=lan` so it accepts connections from the host. If you run OpenClaw standalone from `openclaw/`, add `OPENCLAW_GATEWAY_BIND=lan` to your `.env`. Then verify: `docker compose ps` (gateway running), `docker compose logs openclaw-gateway` (no errors).

**Dashboard performance monitoring:** To see OpenClaw throughput in the dashboard (Token Throughput section), use the **gateway** provider for models. In Settings → Model, pick a model prefixed with `gateway/` (e.g. `gateway/ollama/deepseek-r1:7b`). If you only see `ollama/` models, add the gateway provider to `data/openclaw/openclaw.json` — copy the `gateway` block from `openclaw/openclaw.json.example` into `models.providers`.

**Security & Tailscale:** To bind the UI to localhost only (Tailscale Serve recommended), see [OPENCLAW_SECURE.md.example](OPENCLAW_SECURE.md.example).

## Workspace Files

Layering (see templates in `openclaw/workspace/`). **In git:** only `*.md.example` templates (plus `agents/*.md` and `health_check.sh`) are tracked; personalized top-level `SOUL.md`, `AGENTS.md`, etc. are listed in `.gitignore` — your working copies live under `data/openclaw/workspace/` or are created locally from the examples.

| File | Purpose |
|------|---------|
| `SOUL.md` | Identity, tone, boundaries — minimal operational detail |
| `USER.md` | Operator profile and preferences (optional; from `USER.md.example`) |
| `IDENTITY.md` | Optional display name / avatar notes (from `IDENTITY.md.example`) |
| `AGENTS.md` | Operating policy; **starts with non-negotiables** (MCP names, Discord/cron truth, message length) so they survive bootstrap truncation |
| `TOOLS.md` | **Short canonical contract:** single MCP gateway (**`gateway__call`** / flat **`gateway__…`** tools), core service URLs, cron+Discord; service/API ops in **`agents/docker-ops.md`** and **TROUBLESHOOTING** |
| `MEMORY.md` | Curated long-term notes (main session) |
| `HEARTBEAT.md` | Optional operator checklist (from `HEARTBEAT.md.example`) |
| `memory/` | Dated episodic notes (`YYYY-MM-DD.md`) |

Sub-agents read **AGENTS.md** and **TOOLS.md**, not `SOUL.md` — put **non-negotiable tool + Discord rules at the top of `AGENTS.md`**; keep **`TOOLS.md` short** (canonical only).

**Seeding:** `ensure_openclaw_workspace.ps1` (Windows) or `ensure_openclaw_workspace.sh` (Linux/Mac) copies missing files from `openclaw/workspace/` (or `*.example`) into `data/openclaw/workspace/`.

**Docker sync (`openclaw-workspace-sync`):** For each workspace `*.md`, copies from the repo **only if the file does not already exist** in `data/openclaw/workspace/` (so local edits persist). **`TOOLS.md`** is **upgraded from `TOOLS.md.example`** when it is missing or still a **short stub** (detected by missing contract text). Set **`OPENCLAW_SKIP_TOOLS_MD_UPGRADE=1`** to keep a deliberately minimal `TOOLS.md`. **`health_check.sh`** and **`agents/`** are still refreshed from the repo on every sync. After seeding, the sync container runs **`chown -R 1000:1000`** on `/workspace` so the gateway user **`node`** can edit **`MEMORY.md`** and other files. If you still see **`EACCES`** on writes, run **`scripts/fix_openclaw_workspace_permissions`** (or `docker compose run --rm openclaw-workspace-sync`) and restart `openclaw-gateway` — see [TROUBLESHOOTING — OpenClaw workspace](../docs/runbooks/TROUBLESHOOTING.md#openclaw-workspace--eacces--permission-denied-on-memorymd-or-other-md).

## Data Paths

- **Config:** `data/openclaw/` (openclaw.json, agents, etc.)
- **Workspace:** `data/openclaw/workspace/` (files above, plus `memory/`)

### Native `web_search` (Brave, etc.)

The template **`openclaw.json.example`** sets **`tools.web.search.enabled: false`** so OpenClaw does not expose native **`web_search`**. Use the MCP gateway: **`gateway__call`** with **`duckduckgo__search`** for web search (see **`TOOLS.md`**). To opt into built-in search, see [OpenClaw web tools](https://docs.openclaw.ai/tools/web) and set **`enabled: true`** plus a provider API key.

## Discord (default channel)

Discord is the default client for interacting with OpenClaw.

**Recommended (Ordo AI Stack compose):** put the bot token in the **repo root** `.env` as `DISCORD_TOKEN`. Compose maps it to `DISCORD_BOT_TOKEN` inside `openclaw-gateway`, and `merge_gateway_config.py` (run by `openclaw-config-sync` before the gateway starts) updates `openclaw.json` to reference that env var instead of saving the token in the file.

**Important:** if the official GHCR image still reports **`Unsupported channel: discord`** from `openclaw channels login --channel discord --verbose`, the repo config is not the blocker; use a source-built image (`OPENCLAW_IMAGE=openclaw:local`) or another verified image tag before debugging guild allowlists or slash-command routing.

**Alternative:** interactive login via CLI (writes config for you):

```powershell
docker compose run --rm openclaw-cli channels login
```

See [OpenClaw Discord docs](https://docs.openclaw.ai/channels/discord) for bot token, guild/channel restrictions, and configuration.

**Telegram:** set `TELEGRAM_BOT_TOKEN` in the root `.env`; the gateway container receives it and the merge step can apply the same SecretRef pattern for `channels.telegram`. See upstream [Telegram channel docs](https://docs.openclaw.ai/channels/telegram).

**Guild allowlist:** set **`OPENCLAW_DISCORD_GUILD_IDS`** (server snowflake from the channel URL) so slash commands work in server channels — see [TROUBLESHOOTING — Discord “channel not allowed”](../docs/runbooks/TROUBLESHOOTING.md#discord--this-channel-is-not-allowed--slash-commands-fail-in-general).

**Unrestricted `exec` in the container (e.g. downloads, `apt`):** set **`OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER=1`**, re-run **`openclaw-config-sync`**, restart the gateway. For package installs you usually also need **`overrides/openclaw-gateway-root.yml`** (`user: "0:0"`). See [TROUBLESHOOTING — unrestricted exec](../docs/runbooks/TROUBLESHOOTING.md#openclaw--unrestricted-exec-inside-the-gateway-container-downloads-apt-etc).

## Scheduled jobs (cron) and Discord

If a **daily/hourly job** shows **`deliveryStatus: not-delivered`** but **`status: ok`**, the run may still have **posted to Discord** — OpenClaw’s cron hook does not always mark delivery for **isolated** sessions. **Check the channel first.**

If nothing appears: look for **`⚠️ ✉️ Message failed`** (permissions, **2000-character** limit, rate limits) or **wrong `to`** on the **`message`** tool (`channel:<id>` form). See [TROUBLESHOOTING — OpenClaw cron + Discord](../docs/runbooks/TROUBLESHOOTING.md#openclaw-cron-jobs-and-discord-delivery).

## CLI Commands

When the stack runs in Docker, the CLI must target the gateway by service name. Use the helper script (from repo root), which passes `--url ws://openclaw-gateway:6680` (container gateway port in the default compose) and token from the project root `.env`:

```powershell
# List devices (requires gateway running)
.\openclaw\scripts\run-cli.ps1 devices list

# Approve a device (replace DEVICE_ID with the id from the list)
.\openclaw\scripts\run-cli.ps1 devices approve DEVICE_ID

# Remove a device
.\openclaw\scripts\run-cli.ps1 devices remove DEVICE_ID
```

For other commands (e.g. `dashboard --no-open`) that don't need to call the gateway, you can run `docker compose --profile openclaw-cli run --rm openclaw-cli ...` directly.

## Build from Source (Optional)

To use the latest OpenClaw from source instead of the pre-built image:

```powershell
git clone https://github.com/openclaw/openclaw.git
cd openclaw
./docker-setup.sh
```

Then set `OPENCLAW_IMAGE=openclaw:local` in `.env` and point `OPENCLAW_CONFIG_DIR` / `OPENCLAW_WORKSPACE_DIR` to this project’s data paths.

## Docs

- [OPENCLAW_SECURE.md](OPENCLAW_SECURE.md) — trust model, channel env vars, backups
- [docs/runbooks/SECURITY_HARDENING.md](../docs/runbooks/SECURITY_HARDENING.md) — §11 `openclaw.json` / SecretRefs
- [OpenClaw Docker Guide](https://docs.openclaw.ai/install/docker)
- [Setup](https://docs.openclaw.ai/setup)
- [Agent Workspace](https://docs.openclaw.ai/concepts/agent-workspace)
