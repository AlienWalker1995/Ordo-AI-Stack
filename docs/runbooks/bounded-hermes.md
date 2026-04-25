# Bounded Hermes — Operator Runbook

## Mental model

Hermes used to hold `/var/run/docker.sock` directly, giving it (and any
prompt-injection of it) full Docker daemon access — `docker exec` into
any container, `docker inspect` env vars (including high-value tokens),
recreate containers with arbitrary mounts.

Plan C narrows the surface: Hermes no longer has the socket. When it
needs to restart a service, fetch logs, or manage the compose stack,
it makes an HTTP call to `ops-controller`, which is the single holder
of the socket. Every privileged call is audited.

## What Hermes can still do

Via `hermes/ops_client.py` (the wrapper that talks to ops-controller):

- `OpsClient().list_containers()` → `GET /containers`
- `OpsClient().container_logs(name, tail=N)` → `GET /containers/{name}/logs`
- `OpsClient().restart_container(name)` → `POST /containers/{name}/restart`
- `OpsClient().compose_up(service=…)` / `compose_down(...)` /
  `compose_restart(...)` → `POST /compose/{verb}`

Whole-stack compose ops require an explicit `confirm=True`:

```python
ops = OpsClient()
ops.compose_restart()                       # 400: confirm required
ops.compose_restart(service="open-webui")   # OK — single service
ops.compose_restart(confirm=True)           # OK — whole stack
```

## What Hermes can no longer do

- `docker exec` into other containers — by design. Specific named verbs
  only. If you find yourself wanting `exec`, add a named verb to
  `ops-controller/main.py` instead of reintroducing arbitrary shell.
- `docker inspect` other containers — high-value tokens that live in
  Docker secrets are now invisible to Hermes even with prompt injection.
- Mount new volumes, create containers from arbitrary images, or invoke
  any Docker SDK call ops-controller doesn't explicitly expose.

## UX caveat (vendored upstream)

Hermes' built-in docker tools (in `vendor/hermes-agent/`,
upstream-pinned) will fail when they try `/var/run/docker.sock`. There
are three ways to bridge that gap:

1. **Manual via `OpsClient`** (today's path). From the host or any
   shell with `OPS_CONTROLLER_TOKEN` in env, invoke directly:
   ```python
   from hermes.ops_client import OpsClient
   OpsClient().restart_container("open-webui")
   ```
2. **Hermes plugin** (future). Register a `pre_tool_call` hook
   (similar to `hermes/plugins/push-through/`) that intercepts the
   built-in docker / terminal tools and routes them through
   `OpsClient`. Smaller blast radius than forking upstream.
3. **Fork upstream** (last resort). Maintain a fork of
   `NousResearch/hermes-agent` that swaps `tools/environments/docker.py`
   to call `OpsClient`. Highest maintenance debt.

The compose `${OPS_CONTROLLER_TOKEN:?required}` failsafe ensures
Hermes can never start without the token — option 2 or 3 always has a
working `OpsClient` to delegate to.

## Audit log

```bash
tail -f data/ops-controller/audit.jsonl | jq
```

Each line is one privileged call:

```json
{"ts": 1745611200.123, "caller": "hermes", "action": "container.restart",
 "target": "open-webui", "result": "ok"}
```

Rotation: when `audit.jsonl` exceeds 50MB, it's renamed to
`audit.1.jsonl` and a fresh `audit.jsonl` starts. One historical
generation; older data is dropped. Increase `AUDIT_LOG_MAX_BYTES` (or
the constructor default in `ops-controller/audit.py`) to retain more.

## Adding a new privileged verb

1. Write a failing test in `ops-controller/test_endpoints.py`.
2. Implement the endpoint in `ops-controller/main.py`. Pattern:
   `_: None = Depends(verify_token)` → do work → `_audit.record(...)`
   → return.
3. Add a method on `OpsClient` in `hermes/ops_client.py`.
4. Migrate any caller that needs it.
5. Test, commit, restart `ops-controller` and `hermes-gateway`.

Resist `exec`. Specific verbs only.

## Recovery — ops-controller down

When ops-controller is down, Hermes can't perform any privileged
action. The stack itself stays up; only Hermes-driven ops are blocked.
From the host directly:

```bash
docker compose restart ops-controller
```

The host shell retains full Docker access (this is intentional — the
host operator is still trusted).

## Recovery — Hermes ops_client misconfigured

Symptom: every Hermes-initiated privileged op fails with
`OPS_CONTROLLER_TOKEN env var is empty` or 401 from ops-controller.

Fix: confirm `OPS_CONTROLLER_TOKEN` in
`~/.ai-toolkit/runtime/.env` matches the value ops-controller uses.
Both read from the same SOPS-encrypted source (`secrets/.env.sops`).
After fix: `docker compose restart hermes-gateway hermes-dashboard`.

## Verifying Hermes is bounded

```bash
pytest tests/test_hermes_socket_absent.py -v
```

Six tests: socket absent (gateway + dashboard), root-group elevation
absent (both), ops-controller reachable, OPS_CONTROLLER_TOKEN/URL
present in env. The suite skips if Hermes containers aren't running.
