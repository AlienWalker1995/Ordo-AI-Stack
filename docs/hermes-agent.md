# Hermes Agent (Docker-mode)

Hermes is the stack's assistant-agent layer and the default agent. The stack models an agent as a **data manifest** ([`../services/hermes/agent.yaml`](../services/hermes/agent.yaml)) that the renderer wires into a single `agent` compose service; the agent contract (chat via model-gateway, tools via the model-gateway `/mcp` endpoint, GPU via the ops-controller `/jobs` scheduler, `.env` read-only) is in [`agents.md`](agents.md). The image builds from `services/hermes/` via `ordo build agent` and is selected by `agent: hermes` in `ordo.yaml`.

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is the stack's assistant-agent layer. It runs as one compose service, `agent`, that comes up with the rest of the stack. Inside it, upstream's s6 supervisor runs the gateways (`gateway-<profile>`: Discord / Telegram messaging, cron) and, with the `hermes-dashboard` plugin on, the dashboard: the web UI and the backend every Hermes desktop app connects to (port 9119, published by Caddy at `https://${CADDY_TAILNET_HOSTNAME}:8447/` with Hermes's own Google OIDC sign-in, served at its origin root).

## Running

Bring up the whole stack from the repo root (there is no committed root `docker-compose.yml` — the stack is rendered, not hand-run):

```bash
python -m ordo.cli render --out out   # renders ordo.yaml -> out/{.env,docker-compose.yml,secrets.env.example,…}
ordo up --all                          # refuses while a GPU lease holds the card
```

That's it. Hermes starts automatically, waits for model-gateway / model-gateway-keys / dashboard to be healthy, then registers messaging platforms (if configured) and serves the web UI.

Web UI: `https://${CADDY_TAILNET_HOSTNAME}:8447/` (Hermes's own Google OIDC sign-in, its own dedicated Caddy port, served at the origin root — see [docs/runbooks/auth.md](runbooks/auth.md)). The old `https://${CADDY_TAILNET_HOSTNAME}/hermes*` URL still works — Caddy's `:443` front door 302s it to `:8447/`.
Logs: `docker compose -p ordo logs -f agent` (each gateway also logs to `/opt/data/logs/gateways/<profile>/`)
Restart (also picks up changed env or secrets), from the repo root: `ordo recreate agent`
Stop only Hermes: `docker compose -p ordo stop agent`

(All `docker compose` commands below assume you're in `out/`, the rendered output directory, with `-p ordo`.)

## State

All persistent state (the "brain") lives in the **`hermes-home` named volume**, mounted at
`/opt/data` (upstream's `HERMES_HOME`), and again at `/home/hermes/.hermes` and
`/workspace/data/hermes`, the absolute paths the brain's own skills and jobs use; all three are one store. It moved off the `data/hermes/` host bind 2026-08-05
(#143) after a Windows-side write flipped the 9p-synthesized permissions and silently
killed cron for 22h — the volume removes that failure mode.

| Path (inside the volume) | Contents |
|---|---|
| `config.yaml` | Hermes config (endpoints, Discord behavior, skills preferences) |
| `state.db`, `sessions/` | Conversation history (SQLite, FTS5-indexed) |
| `memories/` | `MEMORY.md` and `USER.md`, loaded into every turn (24,000-char cap each) |
| `profiles/<name>/` | Extra agent profiles, each a complete home of its own (see Profiles) |
| `skills/` | Installed and auto-generated skills |
| `cron/` | Scheduled jobs |
| `logs/` | Hermes's own log files (separate from `docker compose logs`) |

Edit brain files via `docker exec ordo-agent-1 /command/s6-setuidgid hermes …` (or the
`\\wsl$\docker-desktop\...\volumes\ordo_hermes-home\_data` path from Windows). To start
from a clean slate: `docker compose -p ordo down`, `docker volume rm ordo_hermes-home`,
`ordo up --all` (from the repo root).

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

The Discord bot token is a file-based secret (`/run/secrets/discord_bot_token`, per `services/hermes/agent.yaml`'s `secret_files` entry): its value is the `DISCORD_BOT_TOKEN` key in the secret store, and `ordo secrets materialize` writes it to `out/secrets/discord_bot_token`, which the render bind-mounts read-only. Set it with `ordo secrets set DISCORD_BOT_TOKEN --from-stdin`, then `ordo recreate agent` (see [secrets runbook](runbooks/secrets.md)).

After editing `ordo.yaml`:

```bash
python -m ordo.cli render --out out   # re-render
ordo recreate agent                   # recreate with new env
```

### Verifying

```bash
docker compose -p ordo logs --tail=50 agent | grep -i discord
```

Expected: `[Discord] Connected as <botname>#<discriminator>`. If the bot appears in Discord as offline, check the Message Content Intent — that's the #1 cause.

## Configuration endpoints (seeded automatically)

The image's boot hook (`services/hermes/ordo-init.sh`, run by upstream's s6 boot as `/etc/cont-init.d/50-ordo`) seeds `config.yaml` inside the `ordo_hermes-home` volume (`/opt/data/config.yaml`) on every start through `hermes config set`, so the Docker-network endpoints are correct:

```yaml
providers:
  ordo:                    # the stack's gateway as a named provider: the model picker lists its /v1/models
    base_url: http://model-gateway:11435/v1
    api_key: <LITELLM_KEY_HERMES>
model:
  provider: custom:ordo
  default: local-chat
mcp_servers:
  gateway:
    url: http://model-gateway:11435/mcp
    headers:
      Authorization: Bearer <LITELLM_KEY_HERMES>
```

It also sets the per-turn budgets, the compression context length, every `auxiliary.<task>.provider` to `main`, and the provider timeouts (see `ordo-init.sh`). On a home's first start it also hides reasoning and interim assistant updates from replies, once (marker `.ordo-response-display-seeded-v1`), so later display changes stick. Any other keys you add manually (skills, memory providers, display preferences) are preserved across restarts.

## Execute-don't-propose behavior

The image seeds an opinionated `SOUL.md` on first run that pushes the agent toward Claude Code-style behavior: execute via tools, never return a plan for approval, only stop when the work is verifiably done. It is ordinary configuration: edit `SOUL.md` to change it.

First-run seeding is gated by `/opt/data/.ordo-push-through-seeded` (named after a retired plugin). After that sentinel exists, the boot hook never re-seeds, so your `SOUL.md` edits stick.

To replace your existing `SOUL.md` with the shipped opinionated default (one-liner — also reuses the seed inside the image):

```bash
docker compose -p ordo exec agent sh -c "cp /opt/ordo-seed/SOUL.md /opt/data/SOUL.md"
```

The seeded `SOUL.md` routes container work through the `ops-router` tools. The host Docker socket is still mounted; see [`runbooks/bounded-hermes.md`](runbooks/bounded-hermes.md) for the privilege model.

## Claude Code as a sub-agent

The image ships the Claude Code CLI (pinned in `services/hermes/Dockerfile`) and upstream's bundled
`claude-code` skill with this stack's notes spliced in (`services/hermes/skill-overlays/claude-code/`),
so Hermes can hand a coding task to `claude -p` and review the result. It runs on the operator's Claude
subscription, the official client, not an API key.

1. On a machine with a browser and Claude Code installed: `claude setup-token` (prints a long-lived token).
2. On the host: `ordo secrets set CLAUDE_CODE_OAUTH_TOKEN --from-stdin`, paste it, then `ordo apply`.

Only `/usr/local/bin/claude` (the wrapper, `services/hermes/claude-code`) reads the token, per call, from
its file secret; it is never in the agent's environment. Without it, `claude` exits 78 and prints these
steps. Claude Code's settings and sessions live in `/opt/data/claude-code`. Its managed settings (`/etc/claude-code/managed-settings.json`, from `services/hermes/claude-code-managed-settings.json`) pin every run to the subscription at Anthropic, even one that skips the wrapper. Bump the CLI by changing
`CLAUDE_CODE_VERSION` in the Dockerfile.

## Codex as a sub-agent

The same for OpenAI's Codex CLI (`CODEX_VERSION`) and upstream's `codex` skill
(`services/hermes/skill-overlays/codex/`), on the operator's ChatGPT subscription. ChatGPT's sign-in
refreshes its own tokens, so it is not a secret in the store: sign in once, from the host:

```
docker exec -it --user hermes ordo-agent-1 codex login --device-auth
```

Open the printed link in any browser and enter the code. The sign-in lands in `/opt/data/codex` (the
brain volume, which the dashboard shares; the brain backup skips it). Until then `codex` exits 78 with
that command. The wrapper (`services/hermes/codex`) drops any `OPENAI_API_KEY` so a run always uses the
subscription. Sign out with the same `docker exec` and `codex logout`.

## Desktop app and Chat tab sessions

The dashboard runs the agent sessions of its Chat tab and of every Hermes desktop app, and it runs
in the `agent` container (upstream's s6 `dashboard` service, `HERMES_DASHBOARD=1` from the
`hermes-dashboard` plugin). So:

- A session has the same tools from the desktop app as from Discord: the Docker socket, `/c/dev`,
  `/workspace/data`, Claude Code and Codex.
- The desktop app's gateway controls (status, start, stop, restart, per-profile gateways, logs) act on
  the real gateways through s6. Restarting a gateway from there is safe; `ordo recreate agent` restarts
  the whole container, dashboard included, so check Hermes is idle first.
- Sign-in: Google, through Hermes's bundled OIDC provider (setup: [runbooks/auth.md](runbooks/auth.md)).
  Only `/api/status` answers without signing in. The stack dashboard's Hermes card probes it through Caddy (`edge_check`), so the card goes red when the site does.

Hermes 0.21.7 to 0.22.0 do not recognise the gateway's own launcher command line, so a dashboard
reports a running gateway as stopped. `GATEWAY_HEALTH_URL` (in `agent.yaml`) points its health rung at
the gateway's API server until the upstream fix ships.

## Profiles (more than one bot)

`hermes profile create <name>` adds a profile under `/opt/data/profiles/<name>/`: its own `SOUL.md`, memory, skills and sessions. One gateway serves every profile (upstream's multiplexer); do not run a second gateway per profile. A profile created while the gateway runs is served after the next restart (`ordo recreate agent`, or restart `gateway-default` inside the container); `/opt/data/gateway_state.json` lists `served_profiles`.

- **Route a channel to a profile** in the default `config.yaml`:
  ```yaml
  gateway:
    profile_routes:
    - name: <name>-channel
      platform: discord
      chat_id: "<channel id>"
      profile: <name>
  ```
  The boot hook only sets individual keys, so routes survive restarts and recreates.
- **Trim the clone.** A new profile copies the default's `MEMORY.md` and `.env`; cut both to what that profile needs and give it its own `SOUL.md`.

## Updating Hermes

The image is upstream's published `nousresearch/hermes-agent` plus this stack's layer, and it never modifies Hermes: stack behavior is configuration in the boot hook, the `ops-router` plugin, the repo-owned ComfyUI skill notes and the seed `SOUL.md`. The base is pinned by digest in `services/hermes/Dockerfile`, next to `HERMES_PINNED_SHA` (the upstream commit that digest was built from; the build fails if they disagree). To upgrade:

1. `docker buildx imagetools inspect nousresearch/hermes-agent:stable` for the new digest; pull it and read `revision` from `/etc/hermes/image-provenance.json`.
2. Replace the digest and `HERMES_PINNED_SHA` together in `services/hermes/Dockerfile`, and commit.
3. `ordo apply` from the repo root (it builds the image, renders and recreates `agent`).

Upstream migrates `config.yaml` on the first boot of a new release and logs each change; read `docker logs ordo-agent-1` after an upgrade.

## Troubleshooting

**Service is `unhealthy`:**

```bash
docker compose -p ordo logs agent | tail -50
docker exec ordo-agent-1 /command/s6-svstat /run/service/dashboard /run/service/gateway-default
```

**Web UI returns 502 / connection refused at `:8447`:**
- Check the dashboard service is up and answering: `docker exec ordo-agent-1 /command/s6-svstat /run/service/dashboard` and `docker exec ordo-agent-1 curl -s localhost:9119/api/status`.
- Check `HERMES_DASHBOARD=1` in the agent: `docker exec ordo-agent-1 printenv HERMES_DASHBOARD` (empty means the `hermes-dashboard` plugin is off).
- Check Caddy logs for routing errors on the `:8447` site block: `docker compose -p ordo logs caddy | grep -i hermes`.

**Discord bot shows online but doesn't reply:**
- Message Content Intent disabled in Developer Portal.

**Clean restart (throws away all sessions + skills):** see the clean-slate steps under State.

