# Hermes Agent (Docker-mode)

> ⚠️ **Naming note — v2 is the only stack (V1 top-level tree retired 2026-07-24, commit `62540bf`). The repo was later flattened (2026-07-24, commit `2d4bd9c`): the `v2/` directory no longer exists — its contents live at the repo root; there is no v2, there is only Ordo.** Hermes is the stack's assistant-agent layer and **is the default agent**. The stack models an agent as a **data manifest** ([`../services/hermes/agent.yaml`](../services/hermes/agent.yaml)) that the renderer wires into a single `agent` compose service (the hermes web UI ships as the separate `hermes-dashboard` service-plugin, profile `hermes-ui`). The agent contract (chat via model-gateway, tools via mcp-gateway, GPU via the ops-controller `/jobs` scheduler, `.env` read-only) is documented in [`agents.md`](agents.md). Hermes' persistent brain still lives under **`data/hermes/`** (one root, the primary checkout) and the Discord/`SOUL.md`/state notes below remain accurate. The agent image is built from its co-located build context **`services/hermes/`** (`Dockerfile` alongside the `agent.yaml` manifest) via `docker build -t ordo/agent-hermes:latest ./services/hermes` and selected via `agent: hermes` in `ordo.yaml`. The stack is brought up entirely from the repo root: edit `ordo.yaml` (template `ordo.example.yaml`), render with `ordo render` (`python -m ordo.cli render --out out`), then `docker compose -p ordo … up` from `out/` — see [`operator-guide.md`](operator-guide.md).

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is the stack's assistant-agent layer. It runs as two compose services — `agent` (Discord / Telegram messaging) and `hermes-dashboard` (web UI, container port 9119, published by Caddy at `https://${CADDY_TAILNET_HOSTNAME}:8447/` — served at its origin root behind a plain SSO proxy, no forwarded-prefix base injection) — that come up with the rest of the stack.

## Running

Bring up the whole stack from the repo root (there is no committed root `docker-compose.yml` — the stack is rendered, not hand-run):

```bash
python -m ordo.cli render --out out   # renders ordo.yaml -> out/{.env,docker-compose.yml,secrets.env.example,…}
cd out
docker compose -p ordo up -d
```

That's it. Hermes starts automatically, waits for model-gateway / mcp-gateway / dashboard to be healthy, then registers messaging platforms (if configured) and serves the web UI.

Web UI: `https://${CADDY_TAILNET_HOSTNAME}:8447/` (Google SSO, its own dedicated Caddy port, served at the origin root — see [docs/runbooks/auth.md](runbooks/auth.md)). The old `https://${CADDY_TAILNET_HOSTNAME}/hermes*` URL still works — Caddy's `:443` front door 302s it to `:8447/`.
Logs: `docker compose -p ordo logs -f agent hermes-dashboard`
Restart: `docker compose -p ordo restart agent`
Stop only Hermes: `docker compose -p ordo stop agent hermes-dashboard`

(All `docker compose` commands below assume you're in `out/`, the rendered output directory, with `-p ordo`.)

## State

All persistent state lives in `data/hermes/`:

| Path | Contents |
|---|---|
| `config.yaml` in the `ordo_hermes-home` volume (`/home/hermes/.hermes/config.yaml`) | Hermes config (endpoints, Discord behavior, skills preferences) |
| `data/hermes/sessions/` | Conversation history |
| `data/hermes/memories/` | FTS5-indexed memories |
| `data/hermes/skills/` | Installed and auto-generated skills |
| `data/hermes/cron/` | Scheduled jobs |
| `data/hermes/logs/` | Hermes's own log files (separate from `docker compose logs`) |

`data/hermes/` is gitignored. To start from a clean slate: `docker compose -p ordo down`, `rm -rf data/hermes/*`, `docker compose -p ordo up -d`.

## Discord setup

Same flow as before — the env vars move into the container via the rendered `out/docker-compose.yml`, not into a host-side `.env` Hermes reads.

### One-time Discord Developer Portal setup

1. Open <https://discord.com/developers/applications>, create an application.
2. **Bot → Token:** click *Reset Token*, copy. This becomes `DISCORD_BOT_TOKEN`.
3. **Bot → Privileged Gateway Intents:** enable **Message Content Intent** (required — without this the bot receives empty message text) and **Server Members Intent**.
4. **OAuth2 → URL Generator:** scopes `bot` + `applications.commands`; permissions `274878286912` (View Channels, Send Messages, Read Message History, Embed Links, Attach Files, Send Messages in Threads, Add Reactions). Copy the URL; use it to invite the bot to your server.
5. Discord → Settings → Advanced → enable **Developer Mode**. Right-click your own username → *Copy User ID*. This becomes `DISCORD_ALLOWED_USERS`.

### Config entries

There is no root `.env` — Hermes' non-secret Discord vars are set under `site:` in `ordo.yaml` (template `ordo.example.yaml`), which flows verbatim into the rendered `out/.env`:

```yaml
site:
  DISCORD_ALLOWED_USERS: <your-user-id-from-step-5>
  DISCORD_REQUIRE_MENTION: false
```

The Discord bot token is loaded via Docker secrets (`/run/secrets/discord_token`), sourced as a file-based secret per `services/hermes/agent.yaml`'s `secret_files` entry: drop the token at `${OPERATOR_SECRETS_DIR:-$HOME/.ai-toolkit/runtime/secrets}/discord_token` (set `OPERATOR_SECRETS_DIR` under `site:` to relocate it). This is a plain-value token bind mount, not the SOPS/`secrets/*.sops` flow — that root secrets path was retired with the rest of V1.

After editing `ordo.yaml`:

```bash
python -m ordo.cli render --out out   # re-render
cd out && docker compose -p ordo up -d --no-deps agent   # recreate with new env
```

### Verifying

```bash
docker compose -p ordo logs --tail=50 agent | grep -i discord
```

Expected: `[Discord] Connected as <botname>#<discriminator>`. If the bot appears in Discord as offline, check the Message Content Intent — that's the #1 cause.

## Configuration endpoints (seeded automatically)

The container's entrypoint seeds `config.yaml` inside the `ordo_hermes-home` volume (`/home/hermes/.hermes/config.yaml`) on every start so the Docker-network endpoints are correct (the brain moved off the `data/hermes` bind, 2026-08-05 #143):

```yaml
model:
  provider: custom
  base_url: http://model-gateway:11435/v1
  api_key: <LITELLM_MASTER_KEY>
  default: local-chat
mcp_servers:
  gateway:
    url: http://mcp-gateway:8811/mcp
```

Any other keys you add manually (skills, memory providers, display preferences) are preserved across restarts — the entrypoint only touches the five keys above.

## Execute-don't-propose behavior (push-through)

The image ships a small bundled plugin called `push-through` and seeds an opinionated `SOUL.md` on first run. Together they push the agent toward Claude Code-style behavior: execute via tools, never return a plan for approval, only stop when the work is verifiably done.

Persistent state lives in the host bind mount `${BASE_PATH:-.}/data/hermes/`, mounted at `/home/hermes/.hermes` inside the container; host-side edits under `data/hermes/` are what the running containers see.

First-run seeding is gated by `/home/hermes/.hermes/.ordo-push-through-seeded`. After that sentinel exists, the entrypoint never re-seeds — your toggles stick.

To turn the nudge off:

```bash
docker compose -p ordo exec agent hermes plugins disable push-through
```

To opt back in:

```bash
docker compose -p ordo exec agent hermes plugins enable push-through
```

To replace your existing `SOUL.md` with the shipped opinionated default (one-liner — also reuses the seed inside the image):

```bash
docker compose -p ordo exec agent sh -c "cp /opt/ordo-seed/SOUL.md /home/hermes/.hermes/SOUL.md"
```

If `hermes plugins enable push-through` returns non-zero on container start (older Hermes builds), the seeding block swallows the error and writes the sentinel anyway — enable manually with the command above.

Design rationale: bounded push-through keeps Hermes off the docker socket; see `docs/runbooks/bounded-hermes.md` for the privilege model.

## Updating Hermes

The Hermes upstream SHA is pinned in `services/hermes/Dockerfile` as `ARG HERMES_PINNED_SHA=...`. To upgrade:

1. Check recent commits: `git ls-remote https://github.com/NousResearch/hermes-agent.git main` — pick a SHA.
2. Edit `services/hermes/Dockerfile`, change the `ARG HERMES_PINNED_SHA` default.
3. Rebuild the image from the `services/hermes/` build context: `docker build -t ordo/agent-hermes:latest ./services/hermes` (run from the repo root).
4. `cd out && docker compose -p ordo up -d agent hermes-dashboard` (recreates with the rebuilt image).

You can also override without editing the file: `docker build --build-arg HERMES_PINNED_SHA=<sha> -t ordo/agent-hermes:latest ./services/hermes`.

## Troubleshooting

**Service is `unhealthy`:**

```bash
docker compose -p ordo logs agent | tail -50
docker compose -p ordo logs hermes-dashboard | tail -50
```

**Web UI returns 502 / connection refused at `:8447`:**
- Check that the dashboard container is running: `docker compose -p ordo ps hermes-dashboard`.
- Confirm hermes-dashboard is on `ordo-net` so Caddy can reach it: `docker inspect ordo-hermes-dashboard-1 --format '{{json .NetworkSettings.Networks}}'`.
- Check Caddy logs for routing errors on the `:8447` site block: `docker compose -p ordo logs caddy | grep -i hermes`.

**Discord bot shows online but doesn't reply:**
- Message Content Intent disabled in Developer Portal.

**Clean restart (throws away all sessions + skills):**
```bash
docker compose -p ordo down
rm -rf data/hermes/*
docker compose -p ordo up -d
```

