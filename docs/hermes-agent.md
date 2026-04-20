# Hermes Agent (host-mode)

Phase-1 evaluation of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
as the stack's assistant-agent layer. Installed alongside OpenClaw; OpenClaw is not decommissioned
until phase 2.

## Why

OpenClaw has not reached a reliably working state on this stack. Hermes overlaps functionally
(messaging, MCP, cron, OpenAI-compatible) and adds a self-improving skill/learning system
(FTS5 session search, Honcho user modeling, autonomous skill creation from experience).

## Platform requirements

- **WSL2** (recommended) or **Git Bash** on Windows; Linux or macOS on POSIX hosts.
- Python **3.11** (installed automatically by `uv` into the Hermes venv — no system Python change).
- `uv` from astral.sh (installed automatically by the bootstrap if missing).
- The stack running (or startable via `docker compose up -d`).

## Running

From the repo root:

```bash
./scripts/start-hermes-host.sh
```

On first run this clones `vendor/hermes-agent/`, installs it into a dedicated venv, starts the
Docker stack, and launches the Hermes CLI. Subsequent runs skip the clone and install steps.

## Stopping

- `Ctrl-C` exits the Hermes CLI.
- The Docker stack keeps running. Stop it with `docker compose down` when desired.

## State

| Path | Contents |
|---|---|
| `vendor/hermes-agent/` | Upstream repo clone, pinned to a specific commit SHA |
| `vendor/hermes-agent/.venv/` | Python 3.11 venv managed by `uv` |
| `data/hermes/` | Hermes `HERMES_HOME` — config, skills, FTS5 sessions |

All three are gitignored. To fully reset:

```bash
rm -rf vendor/hermes-agent data/hermes
./scripts/start-hermes-host.sh
```

## Known egress

- **Honcho user modeling**: No disable flag exists. Hermes only activates Honcho if
  `~/.honcho/config.json` exists. Since the bootstrap script does not create one, Honcho
  remains dormant in phase 1. If you later enable Honcho, audit the outbound destinations
  before committing to it.
- **`uv` install**: First run fetches `https://astral.sh/uv/install.sh` if `uv` is not already
  present. Install `uv` ahead of time (e.g. `winget install --id=astral-sh.uv -e`) if outbound
  access is blocked.
- **`hermes-agent` clone**: First run clones from GitHub. Pin to a specific SHA via
  `HERMES_PINNED_SHA` in `.env` to freeze upstream.

## Relationship to OpenClaw

Hermes and OpenClaw share the same model-gateway and cannot run simultaneously cleanly.
`scripts/start-hermes-host.sh` defensively stops any in-flight `openclaw-gateway` /
`openclaw-ui-proxy` containers and any host-mode `openclaw gateway` process before launching.

OpenClaw files, services, and `data/openclaw/` remain in the repo untouched during phase 1. They
are the safety net: if Hermes does not pan out, `./scripts/start-openclaw-host.sh` is still there.

Phase 2 (separate spec and plan) decommissions OpenClaw once Hermes is validated.

## Configuration keys

The bootstrap script calls `hermes config set` to persist these (discovered from
`vendor/hermes-agent/hermes_cli/config.py`):

| Key | Value | Purpose |
|---|---|---|
| `providers.ordo.base_url` | `http://localhost:11435/v1` | OpenAI-compatible endpoint (model-gateway / LiteLLM) |
| `providers.ordo.api_key` | `LITELLM_MASTER_KEY` (default `local`) | Bearer key |
| `model` | `ordo:local-chat` | Primary model slug (provider:id format) |
| `mcp_servers.gateway.url` | `http://localhost:8811/mcp` | MCP streamable-http endpoint |

If Hermes rejects a key at runtime (e.g. upstream renamed something), check current config:

```bash
./vendor/hermes-agent/.venv/bin/hermes config --help
./vendor/hermes-agent/.venv/bin/hermes config show
```

Then update `scripts/start-hermes-host.sh` Phase 8 accordingly.

## Validation checklist

After `./scripts/start-hermes-host.sh`:

- [ ] Hermes CLI launches to its TUI.
- [ ] Hermes reports the local gateway model as available (slash-command or equivalent).
- [ ] Hermes MCP tool listing shows tools from mcp-gateway (ComfyUI, Tavily, n8n, GitHub,
      orchestration).
- [ ] Ask Hermes to read a repo file (e.g. `cat README.md`) — confirms host filesystem access.
- [ ] Ask Hermes to call a Tavily search or a ComfyUI tool — confirms MCP roundtrip.
- [ ] Exit. Confirm `data/hermes/` now contains config/session files.

## Refreshing the pin

`HERMES_PINNED_SHA` is set near the top of `scripts/start-hermes-host.sh` (and may be overridden
via `.env`). To upgrade:

```bash
cd vendor/hermes-agent
git fetch origin
git log --oneline origin/main -20
# pick a new SHA
```

Update `HERMES_PINNED_SHA` in the script (or `.env`), re-run the bootstrap. If the new version
changes config key names, inspect `vendor/hermes-agent/hermes_cli/config.py` and update Phase 8
of the script accordingly.
