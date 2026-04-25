# Hermes Agent (Docker-mode)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is the stack's assistant-agent layer. It runs as two compose services — `hermes-gateway` (Discord / Telegram messaging) and `hermes-dashboard` (web UI at :9119) — that come up with the rest of the stack.

## Running

```bash
docker compose up -d
```

That's it. Hermes starts automatically, waits for model-gateway / mcp-gateway / dashboard to be healthy, then registers messaging platforms (if configured) and serves the web UI.

Web UI: <http://localhost:9119/>
Logs: `docker compose logs -f hermes-gateway hermes-dashboard`
Restart: `docker compose restart hermes-gateway`
Stop only Hermes: `docker compose stop hermes-gateway hermes-dashboard`

## State

All persistent state lives in the named Docker volume `ordo-ai-stack_hermes-data`, mounted at `/home/hermes/.hermes` inside the container:

| Path inside container | Contents |
|---|---|
| `/home/hermes/.hermes/config.yaml` | Hermes config (endpoints, Discord behavior, skills preferences) |
| `/home/hermes/.hermes/sessions/` | Conversation history |
| `/home/hermes/.hermes/memories/` | FTS5-indexed memories |
| `/home/hermes/.hermes/skills/` | Installed and auto-generated skills |
| `/home/hermes/.hermes/cron/` | Scheduled jobs |
| `/home/hermes/.hermes/logs/` | Hermes's own log files (separate from `docker compose logs`) |

The host `data/hermes/` directory is leftover from before the volume migration (`5bd23fd`) — do not edit it expecting Hermes to see your changes. To inspect live state: `docker compose exec hermes-gateway ls /home/hermes/.hermes/`. To start from a clean slate: `docker compose down`, `docker volume rm ordo-ai-stack_hermes-data`, `docker compose up -d`.

## Discord setup

Same flow as before — the env vars move into the container via `docker-compose.yml`, not into a host-side `.env` Hermes reads.

### One-time Discord Developer Portal setup

1. Open <https://discord.com/developers/applications>, create an application.
2. **Bot → Token:** click *Reset Token*, copy.
3. **Bot → Privileged Gateway Intents:** enable **Message Content Intent** (required — without this the bot receives empty message text) and **Server Members Intent**.
4. **OAuth2 → URL Generator:** scopes `bot` + `applications.commands`; permissions `274878286912` (View Channels, Send Messages, Read Message History, Embed Links, Attach Files, Send Messages in Threads, Add Reactions). Copy the URL; use it to invite the bot to your server.
5. Discord → Settings → Advanced → enable **Developer Mode**. Right-click your own username → *Copy User ID*.

### Wire the token through SOPS

The Discord bot token lives in the file-form Docker secret `secrets/discord_token.sops`, **not** in `.env`. To set it:

```bash
echo -n "<token-from-step-2>" | \
  sops --encrypt --age "$(grep '^# public key:' ~/.config/sops/age/keys.txt | awk '{print $4}')" \
       --input-type=binary --output-type=binary /dev/stdin \
     > secrets/discord_token.sops
make decrypt-secrets
docker compose restart hermes-gateway
```

Add the user-ID and behavior knobs to plaintext `.env`:

```
DISCORD_ALLOWED_USERS=<your-user-id-from-step-5>
DISCORD_REQUIRE_MENTION=false
```

Inside the container, `hermes/entrypoint.sh` reads `/run/secrets/discord_token` (mounted by the compose `secrets:` block) into `DISCORD_BOT_TOKEN` before the SDK starts — so the token never appears in `docker inspect hermes-gateway`. Full secrets flow: [docs/runbooks/secrets.md](runbooks/secrets.md).

### Verifying

```bash
docker compose logs --tail=50 hermes-gateway | grep -i discord
```

Expected: `[Discord] Connected as <botname>#<discriminator>`. If the bot appears in Discord as offline, check the Message Content Intent — that's the #1 cause.

## Configuration endpoints (seeded automatically)

The container's entrypoint seeds `/home/hermes/.hermes/config.yaml` on every start so the Docker-network endpoints are correct:

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

## Privileged container ops

Hermes mounts `/var/run/docker.sock` directly, so its built-in docker tools (`docker compose restart …`, `docker exec …`, `docker inspect …`) all work natively. There's also an audited HTTP path through ops-controller — see [bounded-hermes runbook](runbooks/bounded-hermes.md) for the rationale and for the bounded-Hermes design that was prototyped and rolled back.

If you want to call the audited path explicitly from your own scripts:

```python
from hermes.ops_client import OpsClient
OpsClient().restart_container("open-webui")
```

Calls land in `data/ops-controller/audit.jsonl` (rotated at 50 MB). The plain `docker compose restart open-webui` still works through the mounted socket and is **not** audited.

## Execute-don't-propose behavior (push-through)

The image ships a small bundled plugin called `push-through` and seeds an opinionated `SOUL.md` on first run. Together they push the agent toward Claude Code-style behavior: execute via tools, never return a plan for approval, only stop when the work is verifiably done.

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

**Web UI returns 502 / connection refused:**
- Check that the dashboard container is running: `docker compose ps hermes-dashboard`.
- Port 9119 collision with an old host-mode process: `netstat -ano | grep :9119` and kill the PID.

**Discord bot shows online but doesn't reply:**
- Message Content Intent disabled in Developer Portal.

**Clean restart (throws away all sessions + skills):**
```bash
docker compose down
docker volume rm ordo-ai-stack_hermes-data
docker compose up -d
```

