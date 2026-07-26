# Bounded Hermes — Operator Runbook

## Mental model

By default an agent holding `/var/run/docker.sock` has full Docker daemon
access — `docker exec` into any container, `docker inspect` env vars
(including high-value tokens), recreate containers with arbitrary mounts.
Any prompt-injection inherits all of it.

The bounded model removes the socket from Hermes. When it needs to restart
a service, fetch logs, or manage the compose stack, it makes an HTTP call
to `ops-api` (`OPS_API_URL=http://ops-api:9000`) — the single socket
holder — and every privileged call is audited. (The `ordo serve`
scheduler at `ops-controller:9000` is a separate service serving only
`/status`, `/model-config`, `/jobs*`, `/health`; it has none of these
verbs.)

## What Hermes can do (via `services/hermes/ops_client.py`)

- `OpsClient().list_containers()` → `GET /containers`
- `OpsClient().container_logs(name, tail=N)` → `GET /containers/{name}/logs`
- `OpsClient().restart_container(name)` → `POST /containers/{name}/restart`
- `OpsClient().compose_up/compose_down/compose_restart(service=…)` →
  `POST /services/{service}/recreate` (up/restart) or `/stop` (down)

Stack-wide compose mutations (`service=None`) are a deliberate 501 — the
render pipeline owns compose lifecycle, not ad-hoc mutation. `OpsClient`
raises `OpsClientError` immediately if you omit `service`:

```python
ops = OpsClient()
ops.compose_restart()                       # OpsClientError: stack-wide disabled
ops.compose_restart(service="open-webui")   # OK
```

## What Hermes cannot do

- `docker exec` into other containers — specific named verbs only. If you
  need `exec`, add a named verb to `ops-api` (below), never reintroduce
  arbitrary shell.
- `docker inspect` other containers — tokens in Docker secrets stay
  invisible to Hermes even under prompt injection.
- Mount new volumes, create containers from arbitrary images, or make any
  Docker SDK call `ops-api` doesn't explicitly expose.

## UX caveat (vendored upstream)

Hermes' built-in docker tools (`vendor/hermes-agent/`, upstream-pinned)
fail when they hit `/var/run/docker.sock`. Bridge the gap one of three
ways:

1. **Manual via `OpsClient`** (today's path) — from any shell with
   `OPS_CONTROLLER_TOKEN` in env:
   ```python
   from hermes.ops_client import OpsClient
   OpsClient().restart_container("open-webui")
   ```
2. **Hermes plugin** — a `pre_tool_call` hook (see
   `services/hermes/plugins/push-through/`) that intercepts the built-in
   docker/terminal tools and routes them through `OpsClient`. Smaller
   blast radius than forking.
3. **Fork upstream** (last resort) — maintain a fork that swaps
   `tools/environments/docker.py` to call `OpsClient`. Highest
   maintenance debt.

The compose `${OPS_CONTROLLER_TOKEN:?required}` failsafe ensures Hermes
never starts without the token, so option 2/3 always has a working
`OpsClient` to delegate to.

## Audit log

```bash
tail -f data/ops-controller/audit.jsonl | jq
```
Each line is one privileged call:
```json
{"ts": 1745611200.123, "caller": "hermes", "action": "container.restart",
 "target": "open-webui", "result": "ok"}
```
Rotation: at `AUDIT_LOG_MAX_BYTES` (default 50MB) `audit.jsonl` rolls to
`audit.1.jsonl`; one historical generation is kept.

## Adding a new privileged verb

1. Write a failing test in `tests/substrate/` for the new endpoint.
2. Implement it in the `ops-api` service source. Pattern:
   `_: None = Depends(verify_token)` → do work → `_audit.record(...)` →
   return.
3. Add a method on `OpsClient` in `services/hermes/ops_client.py`.
4. Migrate any caller that needs it.
5. Test, commit, then from `out/`: `docker compose -p ordo restart
   ops-api agent`.

Resist `exec`. Specific verbs only.

## Recovery — ops-api down

Hermes-driven ops are blocked; the stack itself stays up. From the host
(the rendered compose lives in `out/`):
```bash
cd out
docker compose -p ordo restart ops-api
```
The host shell keeps full Docker access — the host operator is trusted.

## Recovery — Hermes ops_client misconfigured

Symptom: every Hermes-initiated op fails with `OPS_CONTROLLER_TOKEN env
var is empty` or 401 from ops-api. Fix: confirm `OPS_CONTROLLER_TOKEN` in
`out/secrets.env` matches the value ops-api uses (both render from
`out/secrets.env.example`). Then from `out/`: `docker compose -p ordo
restart agent hermes-dashboard`.

## Verifying Hermes is bounded

```bash
pytest tests/test_hermes_socket_absent.py -v
```
Checks: socket absent (gateway + dashboard), root-group elevation absent,
ops-controller reachable, `OPS_CONTROLLER_TOKEN`/`URL` present in env. The
suite skips if Hermes containers aren't running.
