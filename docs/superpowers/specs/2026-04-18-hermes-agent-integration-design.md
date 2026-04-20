# Hermes Agent Integration — Design

**Date:** 2026-04-18
**Status:** Approved for planning
**Phase:** 1 of 2 (install + validate; decommission of OpenClaw is phase 2)

## Motivation

OpenClaw is the stack's current assistant-agent layer (Discord/Telegram, MCP bridge, cron, host-mode entry via `scripts/start-openclaw-host.sh`), but it has never reached a reliably working state on this operator's hardware. NousResearch's [hermes-agent](https://github.com/NousResearch/hermes-agent) overlaps functionally (messaging, MCP, cron, OpenAI-compatible endpoint support) and adds a self-improving skill/learning system (FTS5 session search, Honcho user modeling, autonomous skill creation from experience). Phase 1 installs Hermes alongside OpenClaw and validates it. Phase 2 (separate spec, not covered here) removes OpenClaw.

## Non-goals

- Replacing OpenClaude CLI (different purpose — a local-model Claude Code clone, not an assistant agent).
- Any messaging platform integrations (Discord, Telegram, Slack, WhatsApp, Signal, Email). Phase 2+.
- Dashboard status card for Hermes. Phase 2+.
- PowerShell-native bootstrap (`.ps1`). Phase 2+.
- OpenClaw removal or modification.
- Bringing up Hermes's optional Tinker-Atropos RL submodule.

## Approach

Hermes runs as a **host Python process**, not a Docker service. This matches the `start-openclaw-host.sh` pattern the operator adopted specifically to give the agent "complete context of the PC" (full filesystem, installed tools, Docker CLI, git). A new `scripts/start-hermes-host.sh` bootstrap mirrors the OpenClaw host script structure so the two are symmetric and the operator's muscle memory transfers.

The Docker stack (model-gateway, mcp-gateway, dashboard, llama.cpp, etc.) continues to run as-is. Hermes reaches those services on `localhost` using the published ports.

OpenClaw's services, scripts, workspace templates, `data/openclaw/`, and overrides remain untouched. They are not started. Their continued presence is a deliberate safety net: if Hermes fails to pan out, `scripts/start-openclaw-host.sh` is still there.

## Architecture

### Components

| Component | Role | Location |
|---|---|---|
| `vendor/hermes-agent/` | Upstream repo clone, pinned to a commit SHA | Host filesystem; gitignored |
| `vendor/hermes-agent/.venv/` | Isolated Python 3.11 venv created by `uv` | Gitignored |
| `data/hermes/` | Hermes `HERMES_HOME` — config, skills, FTS5 sessions, Honcho state | Gitignored |
| `scripts/start-hermes-host.sh` | Bootstrap: install → bring stack up → configure → launch Hermes CLI | Tracked |
| `docs/hermes-agent.md` | Operator notes | Tracked |

### Data flow

```
Operator
   │
   │ ./scripts/start-hermes-host.sh
   ▼
Host Hermes CLI  ──(OPENAI_API_BASE)──▶  localhost:11435  ──▶  Docker model-gateway  ──▶  llamacpp
        │
        └────────────(MCP streamable-http)────────────▶  localhost:8811  ──▶  Docker mcp-gateway
                                                                                    │
                                                                                    ├─ ComfyUI MCP
                                                                                    ├─ Tavily
                                                                                    ├─ n8n
                                                                                    ├─ GitHub
                                                                                    └─ orchestration
```

### Isolation boundaries

- **Python environment isolation**: Hermes lives in its own `uv`-managed venv under `vendor/hermes-agent/.venv/`. The host's system Python and the stack's Python 3.12 test environment are untouched.
- **State isolation**: All Hermes state is under `data/hermes/`. Removing Hermes = delete `data/hermes/` and `vendor/hermes-agent/`.
- **Config isolation**: Hermes gets its endpoints via `hermes config set` calls in the bootstrap, not by reading `.env` directly. The bootstrap is the only seam between `.env` and Hermes config.

## Platform support

Hermes officially supports Linux, macOS, WSL2, and Termux (Android). The operator runs Windows 11.

**Primary path — WSL2**: The bootstrap script is bash (`.sh`) and assumes a POSIX environment. Running under WSL2 is the expected mode. Docker Desktop's WSL2 integration exposes `localhost:11435` / `localhost:8811` into the WSL2 distro transparently.

**Secondary path — Git Bash**: The script will likely work in Git Bash because the interesting commands (`curl`, `docker`, `python`, `uv`) are platform-neutral. But `uv`-installed Hermes on Windows-native Python is an unsupported upstream config; breakage is the operator's call.

**Not in scope**: Native PowerShell `.ps1` equivalent. If the operator wants one later, it's phase 2+.

## Bootstrap script — `scripts/start-hermes-host.sh`

Mirrors `scripts/start-openclaw-host.sh` structure. Phases:

1. **Load config**: `set -a; source .env; set +a`.
2. **Ensure `uv`**: If `uv` not on PATH, `curl -LsSf https://astral.sh/uv/install.sh | sh`. Fail loud if network is blocked.
3. **Clone Hermes if missing**: `git clone https://github.com/NousResearch/hermes-agent.git vendor/hermes-agent`, then `git -C vendor/hermes-agent checkout <PINNED_SHA>`. `PINNED_SHA` is a variable at the top of the script; the implementation step picks a specific commit and substitutes it in.
4. **Install Hermes if venv missing**: `cd vendor/hermes-agent && uv venv && uv pip install -e ".[all]"`. Skip if `.venv/bin/hermes` (or `.venv/Scripts/hermes.exe`) already exists.
5. **Bring stack up**: `docker compose up -d` (from repo root).
6. **Stop any OpenClaw host/docker processes** (defensive — prevents port/state collisions):
   - `docker compose stop openclaw-gateway openclaw-ui-proxy 2>/dev/null || true`
   - `pkill -f "openclaw gateway" 2>/dev/null || true`
7. **Wait for dependencies**:
   - Model gateway: `curl -sf http://localhost:${MODEL_GATEWAY_PORT:-11435}/v1/models -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-local}"` until 200.
   - Dashboard: `curl -sf http://localhost:8080/api/health` until 200.
   - MCP gateway: `curl -sf http://localhost:${MCP_GATEWAY_PORT:-8811}/health` (or equivalent) until 200.
8. **Export Hermes envs**:
   - `export HERMES_HOME="${BASE_PATH}/data/hermes"`
   - `export OPENAI_API_BASE="http://localhost:${MODEL_GATEWAY_PORT:-11435}/v1"`
   - `export OPENAI_API_KEY="${LITELLM_MASTER_KEY:-local}"`
   - Any additional vars that Hermes's source requires (implementation step audits `vendor/hermes-agent/` for the exact names).
9. **Persist endpoint config**: Run `hermes config set` commands for the model endpoint, API key, and MCP server URL (`http://localhost:${MCP_GATEWAY_PORT:-8811}/mcp`). Exact key names — implementation step reads `vendor/hermes-agent/` to find them (see Open Questions).
10. **Disable Honcho**: If a config key exists to disable outbound Honcho user modeling, set it. If no such key exists, document it in `docs/hermes-agent.md` as a known egress concern and leave for phase 2+.
11. **Launch**: `cd ${BASE_PATH}` (so Hermes's working dir is the repo root, matching OpenClaw), then `exec vendor/hermes-agent/.venv/bin/hermes`.

## State and gitignore

Add to `.gitignore`:

```
vendor/hermes-agent/
data/hermes/
```

`data/hermes/` is created by the bootstrap script if missing (`mkdir -p`).

## Configuration — `.env.example` additions

Add a new section near the existing OpenClaw block:

```
# --- Hermes Agent (phase-1 assistant agent evaluation) ---
# HERMES_HOME overrides the default data dir if you want it outside the repo
# HERMES_HOME=/path/to/hermes/home
```

No new required vars in phase 1. Hermes uses the existing `LITELLM_MASTER_KEY`, `MODEL_GATEWAY_PORT`, `MCP_GATEWAY_PORT`, `BASE_PATH` — symmetric with OpenClaw host mode.

## Operator docs — `docs/hermes-agent.md`

Covers:

- What Hermes is and why we're evaluating it (one paragraph; link to upstream repo)
- Platform requirements: WSL2 recommended; Python 3.11 + `uv` managed automatically by the bootstrap script
- How to run: `./scripts/start-hermes-host.sh`
- How to stop: `Ctrl-C` exits the CLI; Docker stack keeps running (stop with `docker compose down` if desired)
- Where state lives: `data/hermes/`
- How to wipe and reinstall: `rm -rf vendor/hermes-agent data/hermes` then rerun bootstrap
- Known egress: Honcho user modeling (pending phase-1 verification of whether it can be disabled in-config)
- Relationship to OpenClaw: running Hermes does not stop/modify the OpenClaw services/scripts, but running `start-hermes-host.sh` defensively stops any in-flight OpenClaw gateway to avoid model-gateway contention
- Phase-2 roadmap pointer: Discord/Telegram wiring, dashboard card, OpenClaw removal

## Testing

### Static / CI

`tests/test_start_hermes_host.py` — runs in the existing pytest suite:

- `scripts/start-hermes-host.sh` exists and has a `#!/usr/bin/env bash` shebang (skip executable-bit check; not preserved on Windows NTFS)
- Shell parses cleanly under `bash -n`
- Referenced env var defaults match `.env.example` (e.g. `MODEL_GATEWAY_PORT:-11435`, `MCP_GATEWAY_PORT:-8811`, `LITELLM_MASTER_KEY:-local`)
- Does **not** actually run the script (no hermes install in CI)

### Manual smoke

After `./scripts/start-hermes-host.sh`:

1. Hermes CLI launches to its TUI.
2. `/models` (or Hermes equivalent) lists the local gateway model.
3. `/mcp` (or Hermes equivalent for MCP tool discovery) shows tools from mcp-gateway: ComfyUI, Tavily, n8n, GitHub, orchestration.
4. Ask Hermes to read a repo file (e.g. `cat README.md`) — confirms host filesystem access.
5. Ask Hermes to call a Tavily search or a ComfyUI tool — confirms MCP roundtrip.
6. Exit. Confirm `data/hermes/` now has config/state files.

These are documented in `docs/hermes-agent.md` as the validation checklist.

## File changes summary

| Path | Action |
|---|---|
| `scripts/start-hermes-host.sh` | New — bootstrap script |
| `docs/hermes-agent.md` | New — operator notes |
| `tests/test_start_hermes_host.py` | New — static lint tests |
| `.env.example` | Modify — add Hermes section |
| `.gitignore` | Modify — add `vendor/hermes-agent/`, `data/hermes/` |
| `README.md` | Modify — one-line pointer to `docs/hermes-agent.md` |

Total new: 3 files. Total modified: 3 files. No deletions.

## OpenClaw: explicit non-changes

The following are deliberately **not** touched in phase 1:

- `docker-compose.yml` `openclaw-*` services
- `openclaw/` directory
- `overrides/openclaw-*.yml`
- `scripts/start-openclaw-host.sh`, `scripts/fix_openclaw_workspace_permissions.*`, `scripts/validate_openclaw_config.py`
- `data/openclaw/` operator data
- Tests that reference OpenClaw
- Dashboard OpenClaw references

Phase 2 (future separate spec) decommissions them. Until then, running OpenClaw and running Hermes are mutually exclusive activities at the operator level — never simultaneously.

## Risks

1. **Hermes exact config key names are undocumented**. The README points at `hermes config set <k> <v>` but doesn't enumerate keys. Implementation step must read `vendor/hermes-agent/` source (likely `hermes/config.py` or similar) to find the canonical names for OpenAI endpoint URL, API key, and MCP server URL. Mitigation: the plan includes a discovery task to enumerate keys before writing the `hermes config set` commands.
2. **`uv` auto-install reaches the network**. If the operator's host blocks `astral.sh`, step 2 fails. Mitigation: bootstrap prints a clear instruction to `winget install --id=astral-sh.uv -e` or equivalent if auto-install is not desired.
3. **Honcho egress**. User modeling may call NousResearch infrastructure. Mitigation: phase 1 documents this in `docs/hermes-agent.md` as a known behavior; phase 2 audits and pins local if needed.
4. **FTS5 availability**. Hermes's session search needs SQLite with FTS5. WSL2 Ubuntu's system SQLite has FTS5; Windows-native Python 3.11 from python.org also has it. Should not bite in practice, but if it does the error surfaces immediately at CLI startup.
5. **Upstream drift**. hermes-agent is under active research development. Mitigation: pin to a commit SHA, refresh deliberately via phase-2 work rather than chasing `main`.
6. **Disk and bandwidth**. Full install with `.[all]` extras may pull hundreds of MB of Python packages. Documented in operator notes; no mitigation beyond the existing local-install disk budget.

## Open questions for the implementer

1. **Exact Hermes config keys** for model endpoint, API key, and MCP server URL. Resolve by reading `vendor/hermes-agent/` source once cloned; the plan treats this as the first concrete task.
2. **Pinned commit SHA**. Pick the latest green commit at implementation time; record it at the top of `scripts/start-hermes-host.sh` as `PINNED_SHA=…`.
3. **Honcho disable key**. Same investigation as (1): find it in source, set it in bootstrap if it exists.
4. **MCP streamable-http support in Hermes**. README mentions MCP integration but does not confirm the transport. If Hermes only supports stdio MCP, the plan needs a small adapter or switches to installing individual MCP servers directly in Hermes config. Resolve at investigation time.
