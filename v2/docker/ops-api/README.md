# ops-api (V1-parity dashboard backend)

V2's dashboard backend service (`ordo-v2/ops-api:latest`, compose service `ops-api`). It is the
feature-rich FastAPI control API the **reinstated V1-parity dashboard** (`ordo-v2/dashboard-v1`,
service `dashboard`) talks to: `/model-config` (flag cards), `/registry/*` (model↔GPU registry),
`/services/*` (per-service recreate), `/gpu/*`, `/mcp/*`, `/audit`, `/models/*` (GGUF mgmt),
`/guardian/*` (status, benign when disabled). The dashboard reads `OPS_CONTROLLER_URL=http://ops-api:9000`.

Do not confuse this with the `ordo serve` scheduler: that is the separate `ops-controller` service
(image `ordo-v2/ops-controller:latest`), which stays the sole GPU/stack-lifecycle authority. `ops-api`
is only the dashboard's data/action backend.

## What it is (and why it's safe)

Built from a **COPY of V1's `ops-controller`** code with the reactive guardian and watchdogs neutered
by config (not code), so the outage class that motivated V2 can't recur:

- **Guardian OFF** — opt-in via `COMFYUI_SERIALIZE_LLAMACPP` (default `0`), left unset, so the
  guardian thread never starts; `/guardian/status` returns a benign `{"enabled":false,"state":"disabled"}`.
- **Whole-stack compose mutations OFF** — `OPS_COMPOSE_MUTATIONS_ENABLED` (default OFF) keeps
  `/compose/{up,down,restart}` a static **501**. The `ordo serve` scheduler is the only thing that
  ever changes stack lifecycle.
- **Per-service recreate ON** — `OPS_SERVICE_RECREATE_ENABLED=1` enables ONLY single-service recreate
  (the dashboard's Model Control "apply & restart" and default-model buttons). It replays the existing
  rendered `out/` compose in place (both env files, `--no-deps`, all profiles) — no re-render.
- **GPU visibility** — reserves a GPU with the `utility` capability (`count: all`, no uuid pin) so
  `nvidia-smi` is injected and it enumerates BOTH cards for the registry/GPU widgets.
- SDK start/stop/restart (and the recreate CLI) are scoped to `COMPOSE_PROJECT=ordo-v2`, so they can
  only ever touch V2.

## Build
```
docker build -t ordo-v2/ops-api:latest docker/ops-api
```

The `Dockerfile` in this directory is the authoritative build (V1 ops-controller code + the minimal,
clearly-commented V2 kill-switch patch). See [`AUDIT.md`](../../AUDIT.md) "Dashboard reinstatement"
and [`PARITY-VALIDATION.md`](../../PARITY-VALIDATION.md) for the feature-by-feature live validation.

## Auth / secrets
`OPS_CONTROLLER_TOKEN` (Bearer for external/orchestration callers) and `HF_TOKEN` come from
`secrets.env` at runtime, never baked. On-network dashboard requests use the trusted-proxy branch
(`X-Forwarded-Email` injected by caddy+oauth2-proxy), not raw Bearer.
