# Component: Ops Controller

## Purpose

The Ordo control plane (`ordo serve`, `ordo/control.py`). Drives the GPU/job broker and
scheduler, performs the drift-safe model switch (writes the declarative `ordo.yaml`
source, then re-renders `.env` + compose + Hermes ctx in one pass so they can never
disagree), and owns the compose-lifecycle API the dashboard and agent call (start, stop,
restart, recreate, logs, image pulls, audit). It holds `docker.sock`, and the
`DockerBackend` guard (`ordo/broker.py`) scopes every container call to the `<project>-*`
prefix, so it cannot reach containers outside this compose project.

## API Reference

**Base URL:** `http://ops-controller:9000` (internal network; no host port)

**Auth:** None enforced by ops-controller itself (`ordo/control.py` has no auth check). It
publishes no host port and is reachable only on the internal network; callers (dashboard,
agent, comfyui-mcp, gpu-gate) send `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` from
`out/secrets.env` by convention.

**Scheduler and model switch**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health`, `/healthz` | GET | Liveness |
| `/status` | GET | GPU/scheduler state + the current rendered manifest |
| `/model-config` | GET | Source model, resolved active model, tier, ctx size, catalog |
| `/model-config` | POST | Switch active model (`{"model": "<id>"|"auto"}`); rewrites `ordo.yaml` and re-renders |
| `/plugins` | GET | Installable plugins and their state |
| `/plugins/{id}/enable`, `/plugins/{id}/disable` | POST | Add/remove an allowlisted plugin in `ordo.yaml` |
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
| `/registry/models` | GET | Runtime model registry (`/data/model-registry.json`) |
| `/registry/gpus` | GET | GPUs seen by nvidia-smi |
| `/gpu/assignments` | GET | Current GPU pins |
| `/gpu/assign`, `/registry/models/{id}/assign-gpu` | POST | 410: GPU pins are render-time (`ordo.yaml`) |
| `/models/download`, `/models/download/status` | POST, GET | Resumable download of one allowlisted-host URL into the ComfyUI models volume |
| `/comfyui/install-node-requirements` | POST | pip-install a custom-node pack's `requirements.txt` inside the comfyui container |
| `/diagnostics/dstate` | GET | Processes stuck in uninterruptible sleep |
| `/audit` | GET | Audit log tail (`limit`, default 50) |

**Safety:** Every mutating lifecycle, compose and pip call requires `{"confirm": true}`.
Plugin enable/disable is limited to the `INSTALLABLE_PLUGINS` allowlist in `ordo/control.py`;
core substrate services cannot be added or removed through it.

## Audit Log

`ordo/audit.py` writes one fsync'd JSONL line per privileged call to `AUDIT_LOG_PATH`
(`/data/audit.log` in the container, `data/ops-controller/audit.log` on the host), rotating to
`audit.1.log` at 50 MB. Export: `GET /audit?limit=N`. Audited today: `comfyui_pip_install` and
the `gpu_assign` 410s.

```json
{"ts": 1767225600.0, "caller": "dashboard", "action": "comfyui_pip_install", "target": "ComfyUI-Thing", "result": "ok", "detail": "", "metadata": {"exit_code": 0}}
```

Known limitation: `caller` is hardcoded to `"dashboard"`; multi-actor audit needs identity
propagation.

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
- `${DATA_PATH}/ops-controller` at `/data` (model registry + audit log)
