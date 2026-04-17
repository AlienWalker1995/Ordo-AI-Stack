# Compute Pressure: Per-Service Attribution

**Date:** 2026-04-17
**Status:** Design approved; ready for implementation plan.

## Problem

The dashboard's `COMPUTE PRESSURE` panel today answers "what's on the GPU right now?" — NVML per-PID VRAM, labeled by a small PID-to-service heuristic (`LLM`, `ComfyUI`, `Embed`, `Python`, or a raw `pid:xxx`). That does not answer the question the user actually asks when the machine feels slow: **which toolkit service is hogging compute?**

Most toolkit services (OpenClaw, Open WebUI, Qdrant, n8n, MCP Gateway, model-gateway) never touch the GPU but absolutely can saturate CPU or RAM. They currently appear nowhere in the panel. When a GPU service is running, it shows up as a raw Python PID if the heuristic doesn't hit.

## Goal

Replace the GPU-only view with a per-service pressure roster covering **CPU%, RAM%, and VRAM%** for every toolkit service, sorted so whoever is hogging rises to the top.

## Scope

In scope:

- New ops-controller endpoint that merges per-container Docker stats with NVML per-PID VRAM.
- New dashboard endpoint that proxies it (no-auth, read-only — matches `/api/hardware`).
- Redesigned `#compute-pressure` section showing one row per toolkit service with CPU/RAM/VRAM bars.
- Graceful degradation on Windows/WSL2 where per-PID VRAM is unavailable.

Out of scope:

- Disk I/O, network I/O, per-container logs.
- Historical charts / time-series storage.
- Alerting or thresholds beyond visual sort order.
- OpenClaude (it's a client installer, not a host service).

## Architecture

### Trust boundary (unchanged)

The dashboard container does not mount `/var/run/docker.sock`. All Docker interaction goes through `ops-controller` over HTTP with `OPS_CONTROLLER_TOKEN`. This spec preserves that boundary.

### Data flow

```
browser ──(3s poll, api() wrapper)──> dashboard:/api/hardware/service-pressure
                                              │
                                              │ (HTTP + token)
                                              ▼
                                      ops-controller:/stats/services
                                              │
                                      ┌───────┴────────┐
                                      ▼                ▼
                              docker stats        NVML (pynvml)
                              docker top <id>     + per-PID VRAM
                                      │                │
                                      └───────┬────────┘
                                              ▼
                                      merged JSON payload
```

### Components

**ops-controller: `GET /stats/services`** (new)

- Auth: `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` (same pattern as existing ops-controller routes).
- For each container matching the toolkit compose project:
  - Use `docker stats --no-stream --format '{{json .}}'` for CPU%, mem usage, mem limit, mem%.
  - If the service has `has_gpu: true` in a static service catalog (currently `llamacpp`, `comfyui`): call `docker top <container_id>` to list host PIDs; look up each PID in the NVML per-PID VRAM table; sum into `vram_gb`.
- Returns the full roster (including non-running services with zeroed rows and `running: false`).
- NVML init is best-effort: if pynvml is absent or fails, VRAM fields stay at 0 and the response sets `vram_aggregate_unavailable: true`.
- Timeout: 3s total wall-clock. Individual `docker top` failures are logged and skipped, not fatal.

**dashboard: `GET /api/hardware/service-pressure`** (new)

- No auth — read-only, matches existing `/api/hardware` and `/api/hardware/gpu-processes`.
- Calls ops-controller over the backend Docker network using the existing `OPS_CONTROLLER_URL` + `OPS_CONTROLLER_TOKEN` env.
- On ops-controller error / timeout: returns `{"services": [], "gpu": null, "vram_aggregate_unavailable": true}` with HTTP 200. Never 500s — the panel is a widget, not a critical path.
- Replaces the existing `/api/hardware/gpu-processes` call site. `_gpu_processes()` and `_pid_to_service_label()` in `dashboard/app.py` are removed.

**dashboard: service catalog additions** (modification)

`dashboard/services_catalog.py` — add `has_gpu: bool` to each `SERVICES` entry. Initial values: `llamacpp=True`, `comfyui=True`, all others `False`. Used by ops-controller (which imports nothing from the dashboard — the flag lives in a small shared JSON or is duplicated in an ops-controller-local constant; the plan will pick the lighter approach).

**frontend: `#compute-pressure` section** (redesigned)

- Removes: stacked VRAM bar, legend, `cp-rows` keyed by PID labels.
- Adds: one row per service in the catalog. Each row contains three mini-bars (CPU, RAM, VRAM), each with a track and a filled portion proportional to percent. For `has_gpu:false` services the VRAM slot renders as an empty (zero-width) track so all rows remain column-aligned.
- Sort: `max(cpu_pct, mem_pct, vram_pct)` descending. `running:false` rows all sort to the bottom (their max is 0) and render greyed.
- Retained: LLM degradation score strip at the bottom (already useful, uses separate data source).
- Retained: `cp-util-badge` (aggregate GPU util), `cp-live-dot`, visibilitychange polling pause, `api()` auth wrapper.
- Fallback mode (`vram_aggregate_unavailable: true`): show one extra grey "GPU (aggregate)" row above the service list using `gpu.used_gb` / `gpu.total_gb`, and hide the VRAM bar inside each service row. CPU/RAM rows render normally.

## Data contract

```json
{
  "gpu": {
    "total_gb": 24.0,
    "used_gb": 8.3,
    "utilization_pct": 42
  },
  "host": {
    "cpu_cores": 16,
    "ram_total_gb": 64.0
  },
  "vram_aggregate_unavailable": false,
  "services": [
    {
      "id": "comfyui",
      "name": "ComfyUI",
      "cpu_pct": 12.4,
      "mem_gb": 3.1,
      "mem_pct": 4.8,
      "vram_gb": 7.2,
      "vram_pct": 30.0,
      "has_gpu": true,
      "running": true
    }
  ]
}
```

Field notes:

- `cpu_pct` — value straight from `docker stats` (can exceed 100 on multi-core; UI caps bar width at 100 but shows the raw number as the label).
- `mem_pct` — percent of container memory limit *or* host RAM if no limit set. `docker stats` already normalizes this.
- `vram_pct` — percent of total VRAM (not just "used"), to match existing panel convention.
- `has_gpu` — static per service. When false, consumers should hide the VRAM bar.
- `running` — false means `docker stats` returned no entry for that container. UI greys the row.

## Error handling

| Failure | Behavior |
|---|---|
| `docker stats` missing / timeout | Return `{"services": [...all zeroed, running:false], ...}`. Log at warn. |
| `docker top <id>` fails for one container | Skip its VRAM merge; CPU/RAM still populated. Log at debug. |
| pynvml unavailable / init fails | `vram_aggregate_unavailable: true`, all `vram_gb: 0`. CPU/RAM unaffected. |
| Per-PID VRAM unsupported (Windows/WSL2) | `vram_aggregate_unavailable: true`. UI falls back to single aggregate GPU row. |
| ops-controller unreachable from dashboard | Dashboard returns empty payload with `vram_aggregate_unavailable: true`. UI shows "stats unavailable" pill. |

## Testing

**ops-controller unit tests**

- Mock `docker stats` output (three running containers); mock `docker top`; mock pynvml per-PID VRAM. Verify merged JSON shape.
- Mock `docker stats` returning only two of the three catalog services. Verify the missing one appears with `running: false` and all zeros.
- Mock pynvml raising on init. Verify `vram_aggregate_unavailable: true` and CPU/RAM still correct.
- Mock a GPU service whose PIDs don't appear in NVML's table (process exited between calls). Verify `vram_gb: 0` without an error.

**dashboard unit tests**

- Mock ops-controller 200 with sample payload. Verify `/api/hardware/service-pressure` returns matching shape.
- Mock ops-controller 500 / timeout. Verify dashboard returns 200 with empty/degraded payload.
- Verify removal of `_gpu_processes` / `_pid_to_service_label` doesn't break remaining hardware routes.

**Integration**

- `docker compose up -d dashboard ops-controller`, curl `/api/hardware/service-pressure`. Assert the full catalog appears.
- Start ComfyUI + submit a render → its row jumps to the top.
- Kill ComfyUI → its row goes grey with `running: false`, drops to the bottom.

**Manual UI**

- Verify sorted order updates as load shifts.
- Verify idle services render cleanly (not collapsed, just zero-filled).
- Verify Windows/WSL2 fallback renders the aggregate GPU row and hides per-service VRAM bars.

## Open questions / decisions deferred to plan

- Where `has_gpu` lives (shared JSON vs. duplicated constant in ops-controller) — plan can pick the lighter option after looking at ops-controller layout.
- Whether `docker stats --no-stream` is fast enough on Windows Docker Desktop for a 3s cadence (it usually is, ~500ms-1s). If slow, plan can add a short cache in ops-controller.

## Files likely to change

- `ops-controller/` — new route + merge logic + tests.
- `dashboard/app.py` — new proxy route; remove `_gpu_processes` / `_pid_to_service_label` / `/api/hardware/gpu-processes`.
- `dashboard/services_catalog.py` — add `has_gpu` flag.
- `dashboard/static/index.html` — redesign `#compute-pressure` section (markup, CSS, refresh function).
- `tests/` — new unit + integration tests.
- `CHANGELOG.md` — entry.
