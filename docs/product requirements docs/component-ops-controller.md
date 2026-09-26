# Component: Ops Controller

## Purpose

The Ordo control plane (`ordo serve`, `ordo/control/api.py`). Drives the GPU/job broker and
scheduler, performs the drift-safe model switch (writes the declarative `ordo.yaml`
source, then re-renders `.env` + compose + Hermes ctx in one pass so they can never
disagree), and owns the compose-lifecycle API the dashboard and agent call (start, stop,
restart, recreate, logs, image pulls, audit). It holds `docker.sock`, and the
`DockerBackend` guard (`ordo/control/broker.py`) scopes every container call to the `<project>-*`
prefix, so it cannot reach containers outside this compose project.

## API Reference

**Base URL:** `http://ops-controller:9000` (internal network; no host port)

**Auth:** None enforced by ops-controller itself (`ordo/control/api.py` has no auth check). It
publishes no host port and is reachable only on the internal network; callers (dashboard,
agent, comfyui-mcp, gpu-gate) send `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` from
`out/secrets.env` by convention.

**Scheduler and model switch**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health`, `/healthz` | GET | Liveness |
| `/status` | GET | GPU/scheduler state + the current rendered manifest |
| `/model-config` | GET | Source model, resolved active model, its file and projector, every file the render loads (`model_files`), tier, ctx size, catalog |
| `/model-config` | POST | Switch active model (`{"model": "<id>"|"auto"}`); rewrites `ordo.yaml`, re-renders and applies (`apply` in the response); a failed apply rolls the source back |
| `/apply` | POST | Recreate the changed set of the current render (`{"dry_run": true}` returns the plan; else `confirm: true`): `recreated`, `stopped`, `removed_jobs` (stopped one-shot job containers the render moved past, removed, never started), `running_jobs` (the same, running: left alone), `changes`, `restart_required_on_host`, `host_reasons`, `host_command`, `warnings` (when open-webui was recreated, `ordo doctor`'s model-gateway probe failed or could not run; the recreate stands) |
| `/plugins` | GET | Installable plugins and their state |
| `/plugins/{id}/enable`, `/plugins/{id}/disable` | POST | Add/remove an allowlisted plugin in `ordo.yaml`, re-render and apply; a disable under `plugins: auto` is refused (409) |
| `/jobs` | POST | Request GPU capacity for a job (`id`, `vram_gb`) |
| `/jobs/complete` | POST | Release a completed job (`id`) |
| `/jobs/heartbeat` | POST | Heartbeat a running job (`id`) |
| `/jobs/history` | GET | Last 100 finished leases, newest first |

**Compose lifecycle**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/services` | GET | Compose services + state |
| `/services/{id}/start`, `/stop`, `/restart`, `/recreate` | POST | Lifecycle verb (`confirm: true` required; `dry_run: true` returns the planned action) |
| `/services/{id}/logs` | GET | Tail logs (100 lines) |
| `/containers` | GET | Project containers |
| `/containers/{name}/logs` | GET | Tail one container's logs |
| `/containers/{name}/restart` | POST | Restart one container (`confirm: true`) |
| `/stats/services` | GET | Per-service CPU/memory stats |
| `/compose/up`, `/compose/down`, `/compose/restart` | POST | Whole-project compose verbs (`confirm: true`) |

**Registry, downloads and diagnostics**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/registry/models` | GET | Every model the current render serves (file, GPU pin, ctx, projector), derived on each call |
| `/gpus` | GET | Live per-card VRAM (MiB), utilization and temperature: NVIDIA via nvidia-smi, AMD via sysfs (`ordo/render/gpu_live.py`); the dashboard's GPU widgets read this |
| `/registry/gpus` | GET | The same live cards (GiB), with the models the render pins to each |
| `/gpu/assignments` | GET | Current GPU pins |
| `/gpu/assign`, `/registry/models/{id}/assign-gpu` | POST | 410: GPU pins are render-time (`ordo.yaml`) |
| `/models/download`, `/models/download/status` | POST, GET | Resumable download of one allowlisted-host URL into the ComfyUI models volume |
| `/comfyui/install-node-requirements` | POST | pip-install a custom-node pack's `requirements.txt` inside the comfyui container |
| `/diagnostics/dstate` | GET | Processes stuck in uninterruptible sleep |
| `/audit` | GET | Audit log tail (`limit`, default 50) |

**Safety:** Every mutating lifecycle, compose and pip call requires `{"confirm": true}`.
Plugin enable/disable is limited to the `INSTALLABLE_PLUGINS` allowlist in `ordo/control/api.py`;
core substrate services cannot be added or removed through it.

## Audit Log

`ordo/control/audit.py` writes one fsync'd JSONL line to `AUDIT_LOG_PATH` (`/data/audit.log` in the
container, `data/ops-controller/audit.log` on the host) for every state-changing call, whatever
its outcome: success, dry run, `401`/`409`/`400` refusal or failure. Reads are not recorded. The
one writer is `ControlPlane.handle()`, which wraps `route()`, so a new route is audited without
opting in. The log rotates at 10 MB and keeps five generations (`audit.1.log` ... `audit.5.log`).
Export: `GET /audit?limit=N` (`1 <= N <= 1000`, newest first, across the generations).

```json
{"ts":1790281806.1,"caller":"dashboard","action":"restart","target":"n8n","result":"ok","method":"POST","path":"/services/n8n/restart","status":200,"dry_run":false,"confirm":true}
```

`caller` is the request's `X-Actor` header (`unknown` when absent). Every client sends one, but it
is self-declared: all callers hold the same bearer token. Full schema: [data.md](../data.md#audit-log).

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
