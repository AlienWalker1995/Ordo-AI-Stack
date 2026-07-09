# Hermes Agent (Docker-mode)

> ⚠️ **Partially LEGACY (V1) — reconcile with v2/ (cutover 2026-07-09).** Hermes is still the stack's assistant-agent layer and **is the default agent** in v2 — but v2 models an agent as a **data manifest** ([`../v2/agents/hermes/agent.yaml`](../v2/agents/hermes/agent.yaml)) that the renderer wires into a single `agent` compose service (the hermes web UI ships as the separate `hermes-dashboard` service-plugin, profile `hermes-ui`). The agent contract (chat via model-gateway, tools via mcp-gateway, GPU via the ops-controller `/jobs` scheduler, `.env` read-only) is documented in [`../v2/agents/README.md`](../v2/agents/README.md). Hermes' persistent brain still lives under **`data/hermes/`** (one root, the primary checkout) and the Discord/`SOUL.md`/state notes below remain accurate. The `docker compose up -d` / `hermes/Dockerfile` bring-up + upgrade commands are V1-flavored — under v2 the agent image is built per [`../v2/CUTOVER.md`](../v2/CUTOVER.md) (`docker build … -t ordo-v2/agent-hermes:latest`) and selected via `agent: hermes` in `v2/ordo.yaml`. See [`LEGACY-CLEANUP.md`](LEGACY-CLEANUP.md).

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is the stack's assistant-agent layer. It runs as two compose services — `hermes-gateway` (Discord / Telegram messaging) and `hermes-dashboard` (web UI at :9119) — that come up with the rest of the stack.

## Running

```bash
docker compose up -d
```

That's it. Hermes starts automatically, waits for model-gateway / mcp-gateway / dashboard to be healthy, then registers messaging platforms (if configured) and serves the web UI.

Web UI: `https://${CADDY_TAILNET_HOSTNAME}/hermes/` (Google SSO front door — see [docs/runbooks/auth.md](runbooks/auth.md)).
Logs: `docker compose logs -f hermes-gateway hermes-dashboard`
Restart: `docker compose restart hermes-gateway`
Stop only Hermes: `docker compose stop hermes-gateway hermes-dashboard`

## State

All persistent state lives in `data/hermes/`:

| Path | Contents |
|---|---|
| `data/hermes/config.yaml` | Hermes config (endpoints, Discord behavior, skills preferences) |
| `data/hermes/sessions/` | Conversation history |
| `data/hermes/memories/` | FTS5-indexed memories |
| `data/hermes/skills/` | Installed and auto-generated skills |
| `data/hermes/cron/` | Scheduled jobs |
| `data/hermes/logs/` | Hermes's own log files (separate from `docker compose logs`) |

`data/hermes/` is gitignored. To start from a clean slate: `docker compose down`, `rm -rf data/hermes/*`, `docker compose up -d`.

## Discord setup

Same flow as before — the env vars move into the container via `docker-compose.yml`, not into a host-side `.env` Hermes reads.

### One-time Discord Developer Portal setup

1. Open <https://discord.com/developers/applications>, create an application.
2. **Bot → Token:** click *Reset Token*, copy. This becomes `DISCORD_BOT_TOKEN`.
3. **Bot → Privileged Gateway Intents:** enable **Message Content Intent** (required — without this the bot receives empty message text) and **Server Members Intent**.
4. **OAuth2 → URL Generator:** scopes `bot` + `applications.commands`; permissions `274878286912` (View Channels, Send Messages, Read Message History, Embed Links, Attach Files, Send Messages in Threads, Add Reactions). Copy the URL; use it to invite the bot to your server.
5. Discord → Settings → Advanced → enable **Developer Mode**. Right-click your own username → *Copy User ID*. This becomes `DISCORD_ALLOWED_USERS`.

### `.env` entries

Add to `.env`:

```
DISCORD_BOT_TOKEN=<token-from-step-2>
DISCORD_ALLOWED_USERS=<your-user-id-from-step-5>
DISCORD_REQUIRE_MENTION=false
```

The Discord bot token is loaded via Docker secrets (`/run/secrets/discord_token`, sourced from `secrets/discord_token.sops` after `make decrypt-secrets`). The legacy inline `DISCORD_TOKEN=` in `.env` is no longer accepted — re-encrypt it under SOPS or set `DISCORD_BOT_TOKEN_FILE=/run/secrets/discord_token` directly. See [docs/runbooks/secrets.md](runbooks/secrets.md).

After editing `.env`:

```bash
docker compose up -d hermes-gateway   # recreate with new env
```

### Verifying

```bash
docker compose logs --tail=50 hermes-gateway | grep -i discord
```

Expected: `[Discord] Connected as <botname>#<discriminator>`. If the bot appears in Discord as offline, check the Message Content Intent — that's the #1 cause.

## Configuration endpoints (seeded automatically)

The container's entrypoint seeds `data/hermes/config.yaml` on every start so the Docker-network endpoints are correct:

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

Persistent state lives in the host bind mount `${BASE_PATH:-.}/data/hermes/`, mounted at `/home/hermes/.hermes` inside the container. The legacy named Docker volume `ordo-ai-stack_hermes-data` still exists for rollback (see the `volumes:` section at the bottom of `docker-compose.yml`) but is not in the live path; host-side edits under `data/hermes/` are what the running containers see.

First-run seeding is gated by `/home/hermes/.hermes/.ordo-push-through-seeded`. After that sentinel exists, the entrypoint never re-seeds — your toggles stick.

To turn the nudge off:

```bash
docker compose exec hermes-gateway hermes plugins disable push-through
```

To opt back in:

```bash
docker compose exec hermes-gateway hermes plugins enable push-through
```

To replace your existing `SOUL.md` with the shipped opinionated default (one-liner — also reuses the seed inside the image):

```bash
docker compose exec hermes-gateway sh -c "cp /opt/ordo-seed/SOUL.md /home/hermes/.hermes/SOUL.md"
```

If `hermes plugins enable push-through` returns non-zero on container start (older Hermes builds), the seeding block swallows the error and writes the sentinel anyway — enable manually with the command above.

Design rationale and known limitations: `docs/superpowers/specs/2026-04-21-hermes-push-through-design.md`.

## Updating Hermes

The Hermes upstream SHA is pinned in `hermes/Dockerfile` as `ARG HERMES_PINNED_SHA=...`. To upgrade:

1. Check recent commits: `git ls-remote https://github.com/NousResearch/hermes-agent.git main` — pick a SHA.
2. Edit `hermes/Dockerfile`, change the `ARG HERMES_PINNED_SHA` default.
3. `docker compose build hermes-gateway hermes-dashboard` (rebuilds both with the new pin).
4. `docker compose up -d hermes-gateway hermes-dashboard` (recreates).

You can also override without editing the file: `docker compose build --build-arg HERMES_PINNED_SHA=<sha> hermes-gateway`.

## Troubleshooting

**Service is `unhealthy`:**

```bash
docker compose logs hermes-gateway | tail -50
docker compose logs hermes-dashboard | tail -50
```

**Web UI returns 502 / connection refused at the Caddy front door:**
- Check that the dashboard container is running: `docker compose ps hermes-dashboard`.
- Confirm hermes-dashboard is on `proxy-net` so Caddy can reach it: `docker inspect ordo-ai-stack-hermes-dashboard-1 --format '{{json .NetworkSettings.Networks}}'`.
- Check Caddy logs for routing errors: `docker compose logs caddy | grep -i hermes`.

**Discord bot shows online but doesn't reply:**
- Message Content Intent disabled in Developer Portal.

**Clean restart (throws away all sessions + skills):**
```bash
docker compose down
rm -rf data/hermes/*
docker compose up -d
```

