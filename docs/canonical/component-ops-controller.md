---
authority: canonical
status: current
owner: operator
last_verified: 2026-11-12
verified_against: 1c97d8f3608b
---

# Component: Ops Controller

## Purpose

The Ordo control plane (`ordo serve`, `ordo/control/api.py`). Drives the GPU/job broker and
scheduler, performs the drift-safe model switch (writes the declarative `ordo.yaml`
source, then re-renders `.env` + compose + Hermes ctx in one pass so they can never
disagree), and owns the compose-lifecycle API the dashboard and agent call (start, stop,
restart, recreate, logs, audit). It holds `docker.sock`, and the
`DockerBackend` guard (`ordo/control/broker.py`) scopes every container call to the `<project>-*`
prefix, so it cannot reach containers outside this compose project.

## API Reference

**Base URL:** `http://ops-controller:9000` (internal network; no host port)

**Auth:** Every call except `/health`, `/healthz`, `/metrics` and `/lease` must carry
`Authorization: Bearer <token>`; anything else gets `401` with `WWW-Authenticate: Bearer`, and the
process refuses to start without a token (`ordo/control/api.py`, `ordo/control/serve.py`). Two
principals exist (`ordo/control/principals.py`): `admin`, proved by `OPS_CONTROLLER_TOKEN` (sent by
the dashboard, comfyui-mcp and gpu-gate), and `hermes`, proved by `OPS_CONTROLLER_TOKEN_HERMES`
(the agent's `OPS_CONTROLLER_TOKEN` env carries this scoped token) and limited to a route
allowlist; a route outside it gets `403`. It publishes no host port.

**Scheduler and model switch**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health`, `/healthz` | GET | Liveness |
| `/status` | GET | GPU/scheduler state + the current rendered manifest (`render_error` in its place when the source does not render; the GPU state is always served) |
| `/model-config` | GET | Source model, resolved active model, its file and projector, every file the render loads (`model_files`), tier, ctx size, catalog |
| `/model-config` | POST | Switch active model (`{"model": "<id>"|"auto"}`); rewrites `ordo.yaml`, re-renders and applies (`apply` in the response); a failed apply rolls the source back |
| `/apply` | POST | Recreate the changed set of the current render (`{"dry_run": true}` returns the plan; else `confirm: true`): `recreated`, `stopped` (services the render no longer defines that were not already stopped), `orphans` (the same, already stopped: left as they are), `removed_jobs` (stopped one-shot job containers the render moved past, removed, never started), `running_jobs` (the same, running: left alone), `changes`, `restart_required_on_host`, `host_reasons`, `host_command`, `warnings` (when open-webui was recreated, `ordo doctor`'s model-gateway probe failed or could not run; the recreate stands) |
| `/plugins` | GET | Installable plugins and their state |
| `/plugins/{id}/enable`, `/plugins/{id}/disable` | POST | Add/remove an allowlisted plugin in `ordo.yaml`, re-render and apply (`confirm: true` required; `dry_run: true` returns the planned change); a disable under `plugins: auto` is refused (409) |
| `/jobs` | POST | Request GPU capacity for a job (`id`, `vram_gb`) |
| `/jobs/complete` | POST | Release a completed job (`id`) |
| `/jobs/heartbeat` | POST | Heartbeat a running job (`id`) |
| `/jobs/history` | GET | Last 100 finished leases, newest first |

**Compose lifecycle**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/services` | GET | Compose services + state |
| `/services/{id}/start`, `/stop`, `/restart`, `/recreate` | POST | Lifecycle verb (`confirm: true` required; `dry_run: true` returns the planned action). 403 for `agent` and `ops-controller` (cycled from the host), 400 for a name that is not a compose service name |
| `/services/{id}/logs` | GET | Both streams of the service's container, timestamps removed. `tail` 1-1000 (default 200), `since` an RFC 3339 timestamp or a duration (`10m`); anything else is 400. Answers `logs`, `tail`, `since`, `last_ts` (pass it back as `since` for only the newer lines; a poll that finds nothing new answers the incoming `since` again, so the cursor never goes backwards or null) and `truncated` (the body is capped at 256 KiB, newest lines kept). 404 when the service has no container. The same parameters and fields apply to `/projects/{project}/containers/{name}/logs` |
| `/containers` | GET | Every container on the host: name, state, image, compose project and service, health, docker's status text, and `link` (its launcher link from Homepage-convention labels: `homepage.href` required, `homepage.name`, `homepage.description`, `homepage.group` optional; null without `homepage.href`) |
| `/containers/{name}/logs` | GET | Tail one container's logs |
| `/containers/{name}/restart` | POST | Restart one container (`confirm: true`) |
| `/stats/services` | GET | Per-service CPU/memory stats, plus `containers`: every running container on the host by name (`cpu_pct`, `mem_gb`, `mem_pct`), from the same single `docker stats` sample |
| `/compose/up`, `/compose/down`, `/compose/restart` | POST | Compose verbs (`confirm: true`); the whole project, or one service's lifecycle group with an optional `service` body key |

**Registry, downloads and diagnostics**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/registry/models` | GET | Every model the current render serves (file, GPU pin, ctx, projector), derived on each call |
| `/gpus` | GET | Live per-card VRAM (MiB), utilization and temperature: NVIDIA via nvidia-smi, AMD via sysfs (`ordo/render/gpu_live.py`); the dashboard's GPU widgets read this |
| `/registry/gpus` | GET | The same live cards (GiB), with the models the render pins to each |
| `/gpu/assign`, `/registry/models/{id}/assign-gpu` | POST | 410: GPU pins are render-time (`ordo.yaml`) |
| `/models/download`, `/models/download/status` | POST, GET | Resumable download of one allowlisted-host URL into the ComfyUI models volume |
| `/comfyui/install-node-requirements` | POST | pip-install a custom-node pack's `requirements.txt` inside the comfyui container |
| `/diagnostics/dstate` | GET | Processes stuck in uninterruptible sleep |
| `/doctor` | GET | Read-only drift checks, judged as `ordo doctor` judges them: this process's substrate digest vs the one out/manifest.json records, the open-webui model-gateway probe when it runs, and the alert-delivery verdict from the materialized secret files (`alerting`). `{"ok", "checks": [{"check", "ok", "detail"}]}`; the dashboard Overview shows the failed ones |
| `/audit` | GET | Audit log tail (`limit`, default 50) |

**Safety:** Every mutating lifecycle, compose, plugin and pip call requires `{"confirm": true}` (JSON true: `"true"`, `1` or `"yes"` is refused).
Plugin enable/disable is limited to the `INSTALLABLE_PLUGINS` allowlist plus every `kind: mcp` plugin (`ordo/control/plugin_install.py`);
core substrate services cannot be added or removed through it.

## Audit Log

`ordo/control/audit.py` writes one fsync'd JSONL line to `AUDIT_LOG_PATH` (`/data/audit.log` in the
container, `data/ops-controller/audit.log` on the host) for every state-changing call, whatever
its outcome: success, dry run, `401`/`403`/`409`/`400` refusal or failure. Reads are not recorded,
with two exceptions: `GET /projects/{project}/containers/{name}/logs` (action `project.logs`) and any
read the principal allowlist refuses (`403`). `ControlPlane.handle()` wraps `route()`, so a new
route is audited without opting in; the HTTP layer in `app()` also records the `401`, `403` and
bad-JSON `400` refusals that never reach it. The log rotates at 10 MB and keeps five generations (`audit.1.log` ... `audit.5.log`).
Export: `GET /audit?limit=N` (`1 <= N <= 1000`, newest first, across the generations).

```json
{"ts":1790281806.1,"caller":"dashboard","action":"restart","target":"searxng","result":"ok","method":"POST","path":"/services/searxng/restart","status":200,"dry_run":false,"confirm":true,"principal":"admin"}
```

`caller` is the request's `X-Actor` header (`unknown` when absent). Every client sends one, but it
is self-declared; the record's `principal` field says which token (`admin` or `hermes`) proved
the caller. Full schema: [data.md](../data.md#audit-log).

## Design Principle

**Recovery, not hot path.** Normal model and tool traffic flows agent clients → model
gateway and agent clients → the same gateway's `/mcp` endpoint directly. Ops controller
arbitrates GPU capacity, performs model switches and runs operator lifecycle actions; no user
request should require ops-controller success to complete a chat or tool call.

## Non-Goals

- Being in the hot path for chat/tool requests
- Direct UI: all interactions go through the dashboard, the agent, or the scheduler's own clients

## Dependencies

- Docker socket (`/var/run/docker.sock`), guard-scoped to `<project>-*`
- Rendered config dir mounted read-write at `/config` (source `ordo.yaml` + rendered `out/`), the single write path for a model switch
- `${DATA_PATH}/ops-controller` at `/data` (audit log)
