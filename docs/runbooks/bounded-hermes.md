# Bounded Hermes — Operator Runbook

> **Status (April 2026): the socket-removal half of this design was rolled back.** Hermes
> still mounts `/var/run/docker.sock` directly today, because the upstream Hermes image
> (`vendor/hermes-agent/`, pinned by SHA) ships built-in `docker` / `docker compose`
> tools that fail when the socket isn't there, and the right fix (a Hermes plugin that
> intercepts those tools and re-routes them through `OpsClient`) hasn't shipped yet.
>
> What survived from this plan and is **live on main**: the audited HTTP API
> (`ops-controller` exposes `GET /containers`, `GET /containers/{name}/logs`,
> `POST /containers/{name}/restart`, `POST /compose/{up,down,restart}`), the JSONL
> audit log at `data/ops-controller/audit.jsonl` with 50 MB rotation, and
> `hermes/ops_client.py` for callers that want the audited path explicitly. The high-value
> tokens are still safer than they were thanks to Plan B (Docker secrets — see
> [secrets runbook](secrets.md)) — `docker inspect` no longer exposes them even though
> Hermes can still run it.
>
> The rest of this runbook is preserved as the bounded-Hermes design that was prototyped,
> so the path back to it is short when there's a clean way to bridge the upstream tools.

## Mental model

Hermes holds `/var/run/docker.sock` directly today, giving it (and any
prompt-injection of it) full Docker daemon access — `docker exec` into
any container, `docker inspect` env vars (Docker secrets aside), recreate
containers with arbitrary mounts.

The bounded-Hermes design narrows that surface: Hermes loses the socket and
makes an HTTP call to `ops-controller` for every privileged op. ops-controller
is the single holder of the socket and audits every call. **This is not
the live state today** — see the status banner above.

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

## What Hermes would no longer do (under bounded-Hermes — not live)

- `docker exec` into other containers — by design. Specific named verbs
  only. If you find yourself wanting `exec`, add a named verb to
  `ops-controller/main.py` instead of reintroducing arbitrary shell.
- `docker inspect` other containers — high-value tokens that live in
  Docker secrets would be invisible to Hermes even with prompt injection.
  *Today, with the socket present, Hermes can still call `docker inspect`,
  but the Docker secrets layer (Plan B) keeps the high-value tokens out of
  what `inspect` returns regardless.*
- Mount new volumes, create containers from arbitrary images, or invoke
  any Docker SDK call ops-controller doesn't explicitly expose.

## Why bounded-Hermes was rolled back

Hermes' built-in docker tools (in `vendor/hermes-agent/`,
upstream-pinned) call `/var/run/docker.sock` directly — they don't
know about `OpsClient`. With the socket gone, every built-in `docker`
or `docker compose` tool call inside Hermes fails. We chose three
possible bridges and shipped none of them, so we restored the socket:

1. **Manual via `OpsClient`.** From the host or any shell with
   `OPS_CONTROLLER_TOKEN` in env, invoke directly:
   ```python
   from hermes.ops_client import OpsClient
   OpsClient().restart_container("open-webui")
   ```
   This still works today and is the **recommended path for any
   automation or skill that wants its container ops audited**.
2. **Hermes plugin.** Register a `pre_tool_call` hook (similar to
   `hermes/plugins/push-through/`) that intercepts the built-in docker
   / terminal tools and routes them through `OpsClient`. Smaller blast
   radius than forking upstream. Not yet built.
3. **Fork upstream.** Maintain a fork of `NousResearch/hermes-agent`
   that swaps `tools/environments/docker.py` to call `OpsClient`.
   Highest maintenance debt; option of last resort.

Until option 2 or 3 ships, Hermes mounts the socket and the audit
trail is opt-in (anyone — Hermes or otherwise — who calls through
`OpsClient` gets logged; direct socket use does not).

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

**Expected today: socket-absent assertions FAIL** because Plan C was
rolled back. The ops-controller-reachable + token-present assertions
should still pass and are useful as a smoke test of the audited path.
When the bridge plugin (option 2 above) ships, drop the socket again
and this whole suite should go green.
