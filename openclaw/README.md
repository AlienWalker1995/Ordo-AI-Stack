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

Open **http://localhost:18789/** in your browser. Paste the gateway token into Settings → Token. In Settings → Model, choose an Ollama model (e.g. `ollama/deepseek-r1:7b`) — models are auto-discovered from the local Ollama instance. MCP tools (web search, etc.) from the gateway at `http://mcp-gateway:8811/mcp` are configured as a tool list — the agent can call them automatically. **Existing config?** Add the `mcp` block from [mcp/README.md](../mcp/README.md#openclaw) to your `data/openclaw/openclaw.json`.

**Not reachable?** When using the main AI-toolkit compose, the gateway is configured with `OPENCLAW_GATEWAY_BIND=lan` so it accepts connections from the host. If you run OpenClaw standalone from `openclaw/`, add `OPENCLAW_GATEWAY_BIND=lan` to your `.env`. Then verify: `docker compose ps` (gateway running), `docker compose logs openclaw-gateway` (no errors).

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

## CLI Commands

```powershell
# Get dashboard URL and token
docker compose run --rm openclaw-cli dashboard --no-open

# List devices
docker compose run --rm openclaw-cli devices list

# Approve a device
docker compose run --rm openclaw-cli devices approve <device-id>

# WhatsApp QR login
docker compose run --rm openclaw-cli channels login
```

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
