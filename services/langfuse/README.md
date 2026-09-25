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
host port. This is upstream's supported topology, not sprawl. Two small retention helpers ride the
same profile (see [Retention](#retention)):

| Service | Role |
|---|---|
| `langfuse-web` | The UI and the public API (ingest + `/api/public/*`). |
| `langfuse-worker` | Background ingestion: drains the queue into ClickHouse, runs the migrations. |
| `langfuse-db` | Postgres: projects, users, API keys, datasets, scores. |
| `langfuse-clickhouse` | Columnar store for traces and observations (the high-volume half). |
| `langfuse-redis` | BullMQ ingestion queue (`noeviction`: this is a queue, not a cache). |
| `langfuse-minio` | S3-compatible blob store for raw event payloads and media. |
| `langfuse-retention` | Daily job deleting traces older than `LANGFUSE_RETENTION_DAYS` through the public API. |
| `langfuse-minio-lifecycle` | One-shot that sets the `langfuse` bucket's object expiry to the same age. |

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

## What the gateway sends

`model-gateway` (LiteLLM) traces every LLM call it serves, whoever the caller is: Hermes, Open
WebUI, n8n (the `automation` key), edge clients such as Cline or Cursor, and the dashboard's
probes. It uses LiteLLM's `langfuse_otel` callback, the one Langfuse recommends for v3/v4, exporting
over OTLP straight to `http://langfuse-web:3000/api/public/otel`.

Per call you get one `litellm_request` generation with the model, token usage, cost, latency, the
request messages as input and the completion as output. The consumer is on every trace as the
metadata attribute `litellm.key_alias` (`hermes`, `open-webui`, `automation`, `edge`), so traffic
is filterable per consumer. Streaming calls are traced the same way.

The wiring is decided by the renderer, not the gateway's config template: only while this plugin
is enabled does `ordo/compose.py` put `GATEWAY_LANGFUSE_ENV` on the `model-gateway` service, and the
gateway entrypoint then appends `langfuse_otel` to LiteLLM's callbacks. Without the plugin the
gateway renders and boots exactly as before. Tracing is fail-open here too: if the key pair is
missing, the callback is skipped with a warning rather than failing the gateway.

Things worth knowing about what LiteLLM 1.100.1 sends (checked against its installed source and a
live probe):

- **LiteLLM's own health checks are NOT traced.** The background health checks that drive the CPU
  failover (`local-chat` and `local-embed`, every 60 seconds) used to be 57 percent of all
  observations (key alias `litellm-internal-health-check`, about 2,700 a day). Every local
  deployment in `services/model-gateway/litellm_config.yaml` now carries
  `model_info.health_check_params: {"no-log": true}`: LiteLLM merges that dict into the probe
  request only (`proxy/health_check.py::_update_litellm_params_for_health_check`, run on a deep copy
  of the model list), and `no-log` makes `Logging.should_run_callback` skip every callback except
  the proxy's own spend tracking. The probes still run and still feed health-based routing; they
  just never reach Langfuse.
- **Embedding calls carry the vector.** The OTel attribute mapper writes each returned embedding
  into the observation metadata, so a RAG ingestion run produces large observations.
- **`redact_user_api_key_info: true` is set but does not apply to this callback.** In 1.100.1 only
  the Langfuse SDK, LangSmith and Logfire integrations honour it. The metadata also carries
  `user_api_key_hash` (a SHA-256 of the key, not the key) and the key's spend counters. No key value
  is exported (verified by scanning a live trace for every secret in the gateway's environment).
- **`LITELLM_OTEL_V2` is deliberately off.** On 1.100.1 the V2 exporter ignores
  `LANGFUSE_TRACING_ENVIRONMENT` (traces land in `default`), makes the root observation a proxy
  span with a NULL input, and exports every Postgres auth and spend-write call as an observation.

## Two environments, one project

Both writers use the same `hermes` project and key pair, separated by Langfuse **environment**:

| Environment | Writer | What it holds |
|---|---|---|
| `gateway` | LiteLLM `langfuse_otel` | One generation per LLM call, every consumer, with the key alias |
| `hermes` | Hermes's bundled plugin | Turn spans with their LLM-call and tool-call children |

**Every Hermes LLM call is recorded twice**: once as a `gateway` generation (key alias `hermes`)
and once as an `LLM call` span inside a `hermes` turn. Measured live: every `hermes` generation has
a `gateway` twin starting within 3 seconds. **Any dashboard, cost roll-up or token count must filter
by environment**, or it double-counts Hermes.

Which source to trust for which question:

| Question | Use | Why |
|---|---|---|
| Total LLM calls, tokens, cost, latency across the stack; per-consumer usage | `gateway` only, grouped by `litellm.key_alias` | One generation per call for every consumer, priced by the gateway that billed it |
| What Open WebUI, n8n, edge clients sent | `gateway`, filtered by key alias | Hermes's plugin never sees these calls |
| Why a Hermes turn behaved as it did: turn structure, tool calls, retries | `hermes` | Only the agent knows which LLM call belongs to which turn and which tool ran between them |
| Hermes token or cost totals | `gateway` with `key_alias = hermes` | Same numbers the gateway billed; do not add the `hermes` environment on top |

Why not trace each call once: LiteLLM 1.100.1 has no open-source way to exclude one virtual key
from a globally configured callback. Checked in the installed source:

- `litellm_disabled_callbacks` in key metadata is copied into the request in
  `proxy/litellm_pre_call_utils.py` but enforced only by
  `litellm_enterprise/enterprise_callbacks/callback_controls.py::EnterpriseCallbackControls`, whose
  `_should_allow_dynamic_callback_disabling` returns False unless the proxy is a premium deployment.
- Key and team `logging` metadata is in `proxy/_types.py::LiteLLM_ManagementEndpoint_MetadataFields_Premium`,
  and `convert_key_logging_metadata_to_callback` only ever appends callbacks.
- `POST /team/{team_id}/disable_logging` and `DELETE /team/{team_id}/callback/{name}`
  (`proxy/management_endpoints/team_callback_endpoints.py`) clear only callbacks registered on the
  team, never the global `litellm_settings.callbacks`.
- The request-level `no-log` flag would work, but it is per request and a Hermes-side header would
  be a client change; putting it on a key is not supported.

A custom filtering callback was ruled out as a second tracing implementation to maintain. (Traces
recorded before this change carry the old Hermes environment `ordo`.)

## Retention

The policy is **90 days** for everything tracing produces.

**Expected volume.** With health probes excluded, observations track real use only: one `gateway`
generation per LLM call, plus, for Hermes, one turn span and one span per LLM call and per tool
call in the `hermes` environment. Measured on this stack on 2026-09-17 with the same 30-minute
window query (observations grouped by environment and key alias):

| Window (UTC) | Health probes | Real traffic |
|---|---|---|
| 13:25 to 13:55, before the probe fix, active Hermes session | 46 | 143 (42 `gateway`, 101 `hermes`) |
| 13:58 to 14:28, after, idle | 0 | 0 |

So the steady state is **zero observations a day when idle** (the probes used to add about 2,200
to 2,900 a day regardless of use) and roughly **7,000 a day for a Hermes session sustained around
the clock** (143 per 30 minutes). Ninety days is therefore bounded at about 600,000 observations
under continuous heavy use and far less in practice, which a single-node ClickHouse holds
comfortably; that is the sizing the 90-day policy rests on.

Self-hosted Langfuse ships data retention as an Enterprise feature (it needs an
`LANGFUSE_EE_LICENSE_KEY`), and the maintainers discourage changing ClickHouse TTLs by hand. So
retention goes through the public API only:

- **Traces: `langfuse-retention`.** A tracked stdlib module (`langfuse_retention.py` in this
  directory) mounted into a digest-pinned python image. It lists every observation that started
  before the cutoff with `GET /api/public/v2/observations?toStartTime=<cutoff>` (the v4 read path;
  `GET /api/public/traces` is 404 in `events_only` mode), collects their distinct trace ids, and
  deletes them with `DELETE /api/public/traces` in batches of 1000 (the server's limit). Deleting a
  trace removes its observations and scores. A trace is judged by its oldest observation. Runs are
  idempotent and bounded (at most 100,000 traces per run; the rest go in the next run), log their
  counts, and fail with a non-zero exit on an API error.
- **Blobs: `langfuse-minio-lifecycle`.** Langfuse writes each raw ingestion event and media
  attachment to the `langfuse` bucket and never removes them, so a one-shot installs a MinIO
  lifecycle rule (`ordo-langfuse-retention`) expiring objects after the same number of days. It
  uses `mc ilm import`, which replaces the bucket's lifecycle configuration and so is safe to re-run.
  The one-shot's log ends with the `mc ilm rule ls` table.
- **LiteLLM spend logs** are pruned by LiteLLM itself; see `services/model-gateway/README.md`.

**Schedule.** Ordo has no general-purpose job scheduler: `ops-controller` only arbitrates GPU
leases, the existing periodic scripts run from Hermes cron (an LLM in the loop, which this delete
job must not have), n8n cannot start a container and would be a second implementation, and a host
Task Scheduler entry is untracked per-machine config. So `langfuse-retention` schedules itself in
its own always-on container, the same shape as LiteLLM's in-process spend-log cleanup: one run five
minutes after the container starts, then daily at 04:45 UTC (`LANGFUSE_RETENTION_RUN_AT`, set in
`plugin.yaml`). A failed run is retried at the next slot, and the container reports **unhealthy**
until a run succeeds. To run it by hand, from `out/`:

```bash
COMPOSE_PROFILES='*' docker compose -p ordo --env-file .env --env-file secrets.env \
  run --rm langfuse-retention python /app/langfuse_retention.py --once
```

Add `-e LANGFUSE_RETENTION_DAYS=<n>` to try a different cutoff without changing the policy.

## Secrets and rotation

All ten keys live in the secret store (materialized into `out/secrets.env`) and are minted by `ordo init` / the store's generators. See [the secrets runbook](../../docs/runbooks/secrets.md).

| Key | Rotatable? |
|---|---|
| `LANGFUSE_DB_PASSWORD`, `LANGFUSE_CLICKHOUSE_PASSWORD`, `LANGFUSE_REDIS_AUTH`, `LANGFUSE_MINIO_SECRET`, `LANGFUSE_NEXTAUTH_SECRET` | Yes, via `ordo secrets rotate --internal` (the two databases also need an `ALTER USER` first; the command prints the steps). |
| `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY` | **Never.** `SALT` hashes the API keys Langfuse stores, `ENCRYPTION_KEY` encrypts its at-rest secrets. Rotating either makes stored keys unmatchable and stored data unreadable (the same rule as `LITELLM_SALT_KEY`). `ENCRYPTION_KEY` must also be exactly 64 hex characters or Langfuse refuses to boot. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Not from the file. Langfuse stores its own copy, so editing `secrets.env` only stops the writers authenticating. Rotate in the UI (project settings, API keys), then `ordo secrets set` each half and run the `ordo recreate --reading` it prints. |
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
