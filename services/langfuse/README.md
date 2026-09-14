# Langfuse: self-hosted tracing for the agent

Langfuse is an open-source LLM observability platform: it records what an agent actually did on a
turn (the prompts it sent, the completions it got back, the tools it called, how long each step
took and how many tokens it burned), and lets you search, compare and score those traces. In this
stack it answers questions the logs cannot: *why* did that Hermes turn take four minutes, which
tool call failed before the retry loop started, what exactly was in the context when the model went
sideways. It also carries datasets and evaluation runs, so a prompt or model change can be judged
against recorded traffic instead of vibes.

It is **optional and opt-in** (`default: false`): `plugins: auto` never enables it. Add `langfuse`
to the `plugins:` list in `ordo.yaml`, render, and bring the profile up.

## What runs

Six containers, all behind the `langfuse` compose profile, all digest-pinned, none publishing a
host port. This is upstream's supported topology, not sprawl:

| Service | Role |
|---|---|
| `langfuse-web` | The UI and the public API (ingest + `/api/public/*`). |
| `langfuse-worker` | Background ingestion: drains the queue into ClickHouse, runs the migrations. |
| `langfuse-db` | Postgres: projects, users, API keys, datasets, scores. |
| `langfuse-clickhouse` | Columnar store for traces and observations (the high-volume half). |
| `langfuse-redis` | BullMQ ingestion queue (`noeviction`: this is a queue, not a cache). |
| `langfuse-minio` | S3-compatible blob store for raw event payloads and media. |

## Where it lives

`https://langfuse.<your tailnet>.ts.net/` when the `tailnet-names` sidecar plugin is enabled,
otherwise `https://<CADDY_TAILNET_HOSTNAME>:8450/`. Either way it is behind the same Google SSO gate
as every other UI: `langfuse-web` publishes no host port, so the Caddy `:8450` listener is the only
route in.

That URL is **derived, not configured**: `ordo/render.py::langfuse_public_url` builds
`LANGFUSE_PUBLIC_URL` from the edge identity already in the render, and Langfuse uses it as
`NEXTAUTH_URL` to build its own post-login redirect. Set `site.LANGFUSE_PUBLIC_URL` in `ordo.yaml`
only if the browser reaches Langfuse through some other front door.

## Logging in

The plugin initialises itself headlessly, with no click-through setup wizard. On the first boot against
an empty database, `langfuse-web` creates the organisation `Ordo`, the project `Hermes`, an admin
user, and the project API key pair, all from `LANGFUSE_INIT_*`. On every later boot it logs that
they already exist and moves on.

- **Email:** `LANGFUSE_ADMIN_EMAIL` from `out/.env` (default `admin@ordo.local`; override it from
  `site:` in `ordo.yaml` if you want a real address).
- **Password:** `LANGFUSE_ADMIN_PASSWORD` from `out/secrets.env`.

Self-registration is off (`AUTH_DISABLE_SIGNUP: "true"`): there is exactly one account, and the SSO
gate in front of it is the real boundary.

## What Hermes sends

Hermes ships an `observability/langfuse` plugin from upstream. This stack supplies the two things it
needs (the SDK in the image (`services/hermes/Dockerfile`) and the project keys in its environment,
`services/hermes/agent.yaml`) and enables it once on first start with a key present
(`services/hermes/entrypoint.sh`). Traces go straight to `http://langfuse-web:3000` over the project
network, never through the edge.

Per turn you get a root span (`Hermes turn`) with a generation span per LLM call and a span per tool
call, carrying model, latency, token counts, and the inputs/outputs of each step.

**Capture and redaction.** The plugin truncates every captured field at `HERMES_LANGFUSE_MAX_CHARS`
(default 12000) and replaces `data:` URIs (pasted images and audio) with a redaction marker rather
than shipping the payload. Everything else is sent as-is: **prompts and tool arguments reach the
trace store verbatim**, which is the point of tracing but is also worth knowing before you paste a
credential into a chat. That store is on your own box, behind your own SSO gate, and Langfuse's
telemetry callhome is disabled (`TELEMETRY_ENABLED: "false"`). If you want less, turn
`HERMES_LANGFUSE_SAMPLE_RATE` or `HERMES_LANGFUSE_MAX_CHARS` down, or disable the plugin with
`hermes plugins disable observability/langfuse`: the sentinel means that choice survives restarts.

Tracing is **fail-open** and has no `depends_on`. With this plugin disabled the keys interpolate to
empty, the Hermes plugin short-circuits on its first hook, and the agent is unaffected. Langfuse
being down can never take Hermes down.

## Follow-up: LiteLLM's own callback

LiteLLM has a first-class Langfuse callback, and it is deliberately **not** enabled in this cut.
Every LLM call Hermes makes goes through LiteLLM, and Hermes already emits a generation span for it,
so turning the callback on would double-count each generation inside the `hermes` project and make
latency and token roll-ups wrong.

The right shape, when non-Hermes traffic (Open WebUI, n8n, external Cline/Cursor clients) is worth
tracing, is a **second Langfuse project** with its own key pair, with LiteLLM's callback pointed at
that project, not a second writer into `hermes`.

## Secrets and rotation

All ten keys live in `out/secrets.env` and are minted by `ordo init` / the wizard generators.

| Key | Rotatable? |
|---|---|
| `LANGFUSE_DB_PASSWORD`, `LANGFUSE_CLICKHOUSE_PASSWORD`, `LANGFUSE_REDIS_AUTH`, `LANGFUSE_MINIO_SECRET`, `LANGFUSE_NEXTAUTH_SECRET` | Yes, via `scripts/secrets/rotate-internal.sh` (the two databases also need an `ALTER USER` first; the script prints the steps). |
| `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY` | **Never.** `SALT` hashes the API keys Langfuse stores, `ENCRYPTION_KEY` encrypts its at-rest secrets. Rotating either makes stored keys unmatchable and stored data unreadable (the same rule as `LITELLM_SALT_KEY`). `ENCRYPTION_KEY` must also be exactly 64 hex characters or Langfuse refuses to boot. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Not from the file. Langfuse stores its own copy, so editing `secrets.env` only stops Hermes authenticating. Rotate in the UI (project settings, API keys), then copy the new pair in and restart the agent. |
| `LANGFUSE_ADMIN_PASSWORD` | Not from the file. It seeds the login only on the first boot against an empty database; afterwards the password lives hashed in Postgres. Change it in the UI. |

## Reading traces programmatically

A new v4 deployment runs the default `events_only` write mode, where the v3 read endpoints are
**gone**: `GET /api/public/traces`, `/observations`, `/sessions`, `/scores` and `/metrics` all
return 404 with a "not available … in Langfuse v4 events_only mode" body. That is the documented v4
behaviour, not a misconfiguration: only a deployment still migrating from v3 (`legacy` / `dual` write
mode) keeps them. The replacements:

| Want | Endpoint |
|---|---|
| Spans / generations / tool calls | `GET /api/public/v2/observations` |
| Scores | `GET /api/public/v3/scores` |
| Aggregates | `GET /api/public/v2/metrics?query=<json>` |
| Health | `GET /api/public/health` |

Authenticate with HTTP Basic, `LANGFUSE_PUBLIC_KEY` as the user and `LANGFUSE_SECRET_KEY` as the
password. Underneath, every span is a row in ClickHouse's `events_full`.

## Operating notes

- **First boot takes a couple of minutes.** `langfuse-web` runs the Postgres and ClickHouse
  migrations before it listens, which is why its healthcheck has a 120s `start_period`.
- **The app services do not listen on loopback.** Both bind only the container's own eth0 address,
  so `wget http://localhost:3000` inside the container is refused while every peer on the project
  network is served normally. The healthchecks probe `$(hostname)` for that reason, so do not
  "simplify" them back to `localhost` or the services go permanently unhealthy.
- **Media in the UI.** Both MinIO endpoints are internal, because this stack publishes no MinIO
  port. Media attachments therefore resolve only from inside the project network. Hermes sends none
  (it redacts `data:` URIs), so nothing here depends on it.
- **Restarting** means the whole profile: the app services gate on `service_healthy` datastores, so
  recreating `langfuse-web` alone against a stopped ClickHouse just fails. That is why the dashboard
  card is a link and a health probe, with no lifecycle buttons.
