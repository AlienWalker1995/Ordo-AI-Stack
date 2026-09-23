# model-gateway (LiteLLM config-wrapper)

The Ordo stack's `model-gateway` core service. This is the small config-wrapper build
(`ordo/model-gateway:latest`), a pinned LiteLLM base plus the stack's config: the
canonical **`local-chat`** alias, the `local-embed` alias, the throughput callback, and the
entrypoint that templates the config placeholders at startup.

## Model list (what `/v1/models` and `/model/info` advertise)

| id | mode | backend | notes |
|----|------|---------|-------|
| `local-chat` | chat | `llamacpp` (GPU) | Canonical auto-routing alias; fails over to the CPU pin on GPU eviction. |
| *\<gpu model\>* | chat | `llamacpp` (GPU) | Explicit pin of the GPU deployment — no failover. Name derived from `LLAMACPP_MODEL` (basename, lowercased; e.g. `qwen3.8-27b-q6_k`). |
| *\<cpu model\>*`-cpu` | chat | `llamacpp-cpu` | Always-on CPU fallback (opt-in `cpu-fallback` profile). Name derived from `LLAMACPP_CPU_MODEL`. |
| `local-embed` | embedding | `llamacpp-embed` | nomic-embed-text-v1.5, 768-dim, ctx 8192. |

Every local model's `input_cost_per_token` / `output_cost_per_token` (in both `litellm_params`,
what LiteLLM actually bills against, and `model_info`, what `/model/info` and the UI display) is
electricity-derived from `ordo.yaml`'s `cost:` block (rig power draw, the operator's $/kWh rate
and measured tokens/sec) rather than hardcoded. `local-embed` only carries an input cost
(embedding has no output tokens). Leave `cost:` unset and every model prices at $0/token, same
as before this existed.

The two pin-alias names are **derived at startup from the deployed GGUF filenames** — a
model swap renames them automatically, so nothing version-named is hardcoded in the config.
Only `local-chat` and `local-embed` are stable ids; anything that must survive a model swap
should use those.

Every entry carries a fully populated `model_info` block (mode, context window, output cap,
capability flags, description, backing GGUF) — that block IS the gateway's model
documentation for clients. Deployment-variable values are placeholder-templated by the
entrypoint from the same `.env` the backend llama-server containers read, so the advertised
metadata tracks the running deployment instead of drifting:

| placeholder | env var (default) |
|-------------|-------------------|
| `__CTX_SIZE__` | `LLAMACPP_CTX_SIZE` (262144) |
| `__N_PREDICT__` | `LLAMACPP_N_PREDICT` (65536) |
| `__CPU_CTX_SIZE__` | `LLAMACPP_CPU_CTX` (131072) — rendered from the same resolved window as `LLAMACPP_CTX_SIZE`, so the failover's advertised window can't diverge from the primary's |
| `__GPU_WEIGHTS__` | `LLAMACPP_MODEL` (model.gguf) |
| `__CPU_WEIGHTS__` | `LLAMACPP_CPU_MODEL` (Qwen3.6-35B-A3B-UD-Q4_K_M.gguf) |
| `__EMBED_WEIGHTS__` | `LLAMACPP_EMBED_MODEL` (nomic-embed-text-v1.5.Q4_K_M.gguf) |
| `__GPU_IMAGE__` | `LLAMACPP_IMAGE` (llama.cpp) |
| `__GPU_MODEL_NAME__` | derived: `LLAMACPP_MODEL` basename, lowercased, `.gguf` stripped |
| `__CPU_MODEL_NAME__` | derived: `LLAMACPP_CPU_MODEL` basename, lowercased, + `-cpu` |
| `__GPU_SUPPORTS_VISION__` | derived: `true` iff `LLAMACPP_MMPROJ` is non-empty |
| `__LOCAL_INPUT_COST_PER_TOKEN__` | `LOCAL_INPUT_COST_PER_TOKEN` (0) |
| `__LOCAL_OUTPUT_COST_PER_TOKEN__` | `LOCAL_OUTPUT_COST_PER_TOKEN` (0) |

`supports_vision` is derived from whether the GPU server actually loads an mmproj. The
remaining `supports_*` flags (tools, reasoning) describe the llama-server invocation
(`--jinja`, `--reasoning-format`) rather than a model family, and are static in the config.

The Ordo stack references it as a **project buildable image** (`ordo/model-gateway:latest`) — pinned by
its build context, not pulled from a registry — so `ordo preflight` reports a missing one as
"build first", never "Docker will pull". This is why the Ordo stack does NOT reference the unconfigured
upstream `ghcr.io/berriai/litellm:main` directly: that image has no `local-chat` alias.

## Tracing (Langfuse)

When the `langfuse` plugin is enabled, the gateway traces every LLM call it serves (every consumer,
tagged with the virtual key alias as `litellm.key_alias`) to Langfuse under the environment
`gateway`, using LiteLLM's `langfuse_otel` callback. The callback is **not** in
`litellm_config.yaml`: the renderer sets `LITELLM_EXTRA_CALLBACKS=langfuse_otel` plus
`LANGFUSE_OTEL_HOST`, `LANGFUSE_TRACING_ENVIRONMENT` and
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content` on this service only while the plugin
is enabled (`ordo/compose.py::GATEWAY_LANGFUSE_ENV`), and the entrypoint's `add_callbacks.py` step
appends the callback at start. The project key pair comes from `secrets.env`. Without the plugin
the variable is unset and the gateway boots on the template's callbacks alone; with the plugin but
without keys the callback is skipped with a warning, never fatal. `no_content` only drops LiteLLM's
duplicate `raw_gen_ai_request` child span: the generation still carries its input and output.
`LITELLM_OTEL_V2` stays off (on 1.100.1 it loses the environment and exports every Postgres call
as a span). What gets sent, and why Hermes's calls also appear under `hermes`, is in
`services/langfuse/README.md`.

Health probes are not traced: every local deployment's `model_info.health_check_params` is
`{"no-log": true}`, which LiteLLM merges into the background (and `/health`) probe request only, so
the probes skip the logging callbacks while spend tracking and health-based routing keep working.
Never move `no-log` into `litellm_params`: there it would silence tracing for real traffic too.

`litellm_settings.redact_user_api_key_info: true` is set. It strips `user_api_key_*` metadata for
the integrations that honour it (Langfuse SDK, LangSmith, Logfire); `langfuse_otel` does not consult
it in 1.100.1.

## Spend-log retention

`general_settings.maximum_spend_logs_retention_period: "90d"` with
`maximum_spend_logs_cleanup_cron: "30 4 * * *"` makes LiteLLM's in-process scheduler delete
`LiteLLM_SpendLogs` rows (and the tool-index rows derived from them) older than 90 days, every day
at 04:30 UTC, in bounded batches under a statement timeout (LiteLLM's defaults for batch size,
batch count and run budget). This is open-source LiteLLM, no license needed. The **daily aggregate
tables** (`LiteLLM_DailyUserSpend`, `LiteLLM_DailyTeamSpend`, `LiteLLM_DailyTagSpend` and friends)
and the per-key `spend` totals are **not** touched, so long-term cost history per key, model and
day survives the purge; only the per-request log rows expire.

## Build
```
docker build -t ordo/model-gateway:latest services/model-gateway
```

## Signing in
LiteLLM's admin UI (keys, teams, spend, MCP servers, models) is at `https://llm.<tailnet>/ui/`
(or `https://<host>:8449/ui/` when the tailnet-names sidecars are disabled), behind the edge's
own Google SSO gate like every other UI in the stack. Once past that gate, LiteLLM asks for its
OWN login, which is one of two paths:

1. **Google SSO** (the "Sign in with Google" button on LiteLLM's login page) - the same Google
   identity as the edge, not a second password. Works whenever `edge` is enabled and
   `PROXY_BASE_URL` can be derived (see below); the renderer maps the stack's EXISTING Google
   OAuth client onto `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (`ordo/render.py::
   litellm_google_sso_env`), so no new secret or Google app registration is needed.
   - LiteLLM promotes exactly one identity to `proxy_admin`: the one matching the optional
     `site.LITELLM_ADMIN_IDENTITY` key, rendered as `PROXY_ADMIN_ID`
     (`check_and_update_if_proxy_admin_id`). **That value is the Google account's OpenID `sub`,
     not its email** - sign in with Google once first (you land as a view-only internal user),
     then read the `user_id` LiteLLM created for you from the `litellm-db` Postgres database
     (`LiteLLM_UserTable`, matched by `user_email`), and put that `user_id` in
     `site.LITELLM_ADMIN_IDENTITY`. Leave it unset and every Google sign-in stays view-only.
   - Free for up to 5 billable users (LiteLLM's own SSO limit, `_raise_if_sso_exceeds_free_user_
     limit`); more needs an Enterprise license. This deployment has one human, so no license is
     needed.
2. **Master key** (username `admin`, password = `LITELLM_MASTER_KEY` from `out/secrets.env`;
   never write the key value anywhere) - LiteLLM's `/login` route builds this form
   unconditionally, so it keeps working exactly as before whether or not Google SSO is
   configured. **Keep this as the break-glass path**: if Google is ever unreachable, it is the
   only way into the admin UI.

The gateway sets `FORWARDED_ALLOW_IPS=*` so uvicorn trusts the `X-Forwarded-Proto`/
`X-Forwarded-Host` headers Caddy adds from its project-network address; without it, the
post-login redirect (either path) comes back `http://` on a TLS-only port and sign-in fails.

### One manual step: Google Cloud Console
Add this authorized redirect URI to the SAME OAuth client already used for the edge's
oauth2-proxy (Google Cloud Console -> APIs & Services -> Credentials -> that OAuth 2.0 Client ID
-> Authorized redirect URIs):
```
https://llm.tail63bdfc.ts.net/sso/callback
```
General form: `<PROXY_BASE_URL>/sso/callback` (see `ordo/render.py::litellm_google_sso_env` for
how `PROXY_BASE_URL` is derived on a different edge shape). The edge's oauth2-proxy gate still
runs first - a request passes Google once for the front door, then again for LiteLLM's own SSO
handshake; these are two separate OAuth round-trips against the same client.

## Files
- `Dockerfile` — pins `ghcr.io/berriai/litellm:v1.100.1@sha256:a3715fa7ad8387941ab697259bd2881d68931657247a41984f90fae6d11c62bf` (bump deliberately to a specific vX.Y.Z + digest), installs the config + helpers.
- `litellm_config.yaml` — the model list + per-model `model_info` documentation (no secrets;
  all `__*__` placeholders are entrypoint-substituted at runtime; the master key is read
  straight from the environment via `os.environ/LITELLM_MASTER_KEY`).
- `entrypoint.sh` — refuses a missing or weak `LITELLM_MASTER_KEY` (shape `sk-<32+ chars>`),
  renders the template with the deployment metadata from `.env`, then merges the rendered
  MCP server fragment (`/config/mcp_servers.yaml`) into the config, then appends any
  render-selected callbacks (`LITELLM_EXTRA_CALLBACKS`).
- `merge_mcp_config.py` — folds the render-emitted `mcp_servers:` fragment into the rendered
  config; exits 2 when the fragment is missing or malformed (never boots an empty tool set).
- `add_callbacks.py`: appends the plugin-dependent logging callbacks named in
  `LITELLM_EXTRA_CALLBACKS` (today `langfuse_otel`) to `litellm_settings.callbacks`; a callback
  whose credentials are unset is skipped with a warning.
- `throughput_callback.py` — posts per-completion tok/s + TTFT samples to the dashboard.
- `bootstrap_keys.py`: idempotent LiteLLM virtual-key provisioning from the rendered
  `out/model-gateway/keys.json` (runs as the `model-gateway-keys` one-shot).

`LITELLM_MASTER_KEY` and `THROUGHPUT_RECORD_TOKEN` are supplied at runtime from the
operator-managed `secrets.env` (never baked).
