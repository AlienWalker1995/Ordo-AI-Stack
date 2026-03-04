# OpenClaw — Personal AI Assistant

[OpenClaw](https://docs.openclaw.ai) is a self-hosted personal AI assistant that runs in Docker. This folder provides a ready-to-use setup integrated with the AI-toolkit project.

## Quick Start

### 1. Prepare directories and workspace

From the **repo root** (e.g. `F:\AI-toolkit`):

```powershell
$env:BASE_PATH = "F:/AI-toolkit"
.\scripts\ensure_dirs.ps1
.\openclaw\scripts\ensure_openclaw_workspace.ps1
```

### 2. Configure environment

```powershell
cd openclaw
copy .env.example .env
```

Edit `.env` and set:

- `BASE_PATH` — repo root (e.g. `F:/AI-toolkit`)
- `OPENCLAW_GATEWAY_TOKEN` — generate with `openssl rand -hex 32`
- **Ollama** — enabled by default when using the main compose; models from `ollama` are auto-discovered
- Optional cloud APIs: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `OPENROUTER_API_KEY`

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

Open **http://localhost:18789/** in your browser. Paste the gateway token into Settings → Token. In Settings → Model, choose a model from the **gateway** provider (e.g. `gateway/ollama/deepseek-r1:7b`) — this routes through the Model Gateway so dashboard monitoring shows performance. MCP tools (web search, etc.) from the gateway at `http://mcp-gateway:8811/mcp` are exposed via the **openclaw-mcp-bridge** plugin — the agent can call them automatically. **Existing config?** Ensure `plugins.entries["openclaw-mcp-bridge"]` is set as in [mcp/README.md](../mcp/README.md#openclaw); this repo’s `data/openclaw/openclaw.json` already includes it.

**Not reachable?** When using the main AI-toolkit compose, the gateway is configured with `OPENCLAW_GATEWAY_BIND=lan` so it accepts connections from the host. If you run OpenClaw standalone from `openclaw/`, add `OPENCLAW_GATEWAY_BIND=lan` to your `.env`. Then verify: `docker compose ps` (gateway running), `docker compose logs openclaw-gateway` (no errors).

**Dashboard performance monitoring:** To see OpenClaw throughput in the dashboard (Token Throughput section), use the **gateway** provider for models. In Settings → Model, pick a model prefixed with `gateway/` (e.g. `gateway/ollama/deepseek-r1:7b`). If you only see `ollama/` models, add the gateway provider to `data/openclaw/openclaw.json` — copy the `gateway` block from `openclaw/openclaw.json.example` into `models.providers`.

**Security & Tailscale:** To bind the UI to localhost only (Tailscale Serve recommended), see [OPENCLAW_SECURE.md](OPENCLAW_SECURE.md).

## Workspace Files

The agent reads these files at session start:

| File        | Purpose                                                |
|-------------|--------------------------------------------------------|
| `SOUL.md`   | Agent identity, tone, and boundaries                    |
| `AGENTS.md` | Session rules, memory system, safety guidelines        |
| `TOOLS.md`  | Your environment-specific notes (SSH, cameras, TTS, etc.) |

Templates live in `openclaw/workspace/`. On first setup, `ensure_openclaw_workspace.ps1` copies them to `data/openclaw/workspace/`. Edit them there—they persist in your data folder.

## Data Paths

- **Config:** `data/openclaw/` (openclaw.json, agents, etc.)
- **Workspace:** `data/openclaw/workspace/` (SOUL.md, AGENTS.md, TOOLS.md, memory/)

## Discord (default channel)

Discord is the default client for interacting with OpenClaw. Set up via:

```powershell
docker compose run --rm openclaw-cli channels login
```

See [OpenClaw Discord docs](https://docs.openclaw.ai/channels/discord) for bot token, guild/channel restrictions, and configuration.

## CLI Commands

When the stack runs in Docker, the CLI must target the gateway by service name. Use the helper script (from repo root), which passes `--url ws://openclaw-gateway:18789` and token from the project root `.env`:

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

- [OpenClaw Docker Guide](https://docs.openclaw.ai/install/docker)
- [Setup](https://docs.openclaw.ai/setup)
- [Agent Workspace](https://docs.openclaw.ai/concepts/agent-workspace)
