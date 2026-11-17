# Hermes Docker Access: Operator Runbook

## Mental model

Hermes (the `agent` service) has full Docker control of the host. Its
manifest, `services/hermes/agent.yaml`, mounts `/var/run/docker.sock` and
adds the container to group `0` so the unprivileged `hermes` user can use
the socket. The docker CLI ships in the image.

The guardrails are prompting, not enforcement: `services/hermes/seed/SOUL.md`
carries the rules (take a scheduler GPU lease before any GPU work, never
destroy its own container or persistent volumes). A raw socket bypasses the
GPU lease, so a leaseless GPU container can still co-tenant the GPU with the
resident model. The rejected enforcing alternative (a guarded socket
proxy) is recorded in `docs/design/hermes-owns-docker.md`.

Anything Hermes does over the raw socket is not audited by Ordo.

Removing the socket is decided: the agent's Docker work moves to ops-controller, one step at a time.
Until its last step lands, the socket stays mounted.
The seeded `SOUL.md` and the `ops-router` tools tell Hermes to use the control
plane; an operator's live `SOUL.md` may grant more. The live `SOUL.md` sits in
the `hermes-home` volume and is not re-seeded; edit it there.

## First-class ops tools (the `ops-router` plugin)

`services/hermes/plugins/ops-router/` exposes the control plane's container
verbs as Hermes tools. They wrap `OpsClient` (`services/hermes/ops_client.py`)
and call `ops-controller` (`OPS_CONTROLLER_URL=http://ops-controller:9000`):

| Tool | ops-controller route | Scope |
|---|---|---|
| `list_containers` | `GET /containers` | every container on the host, read-only |
| `container_logs` | `GET /containers/{name}/logs` | `ordo` project only |
| `inspect_container` | `GET /containers/{name}` | `ordo` project only; field allowlist, never the environment or labels |
| `restart_container` | `POST /containers/{name}/restart` | `ordo` project only, lease-checked |
| `compose_restart`, `compose_up` | `POST /services/{name}/recreate` | one `ordo` service, lease-checked |
| `project_containers` | `GET /projects`, `GET /projects/{project}/containers` | projects in `ordo.yaml` `managed_projects:`, read-only |
| `project_logs` | `GET /projects/{project}/containers/{name}/logs` | managed projects, at most 1000 lines; every read is audited (`project.logs`) |
| `restart_project_container` | `POST /projects/{project}/containers/{name}/restart` | managed projects; confirm-gated, 3 per container per hour (then 429; the budget is in ops-controller's memory and resets when ops-controller restarts), refused (409) when its device requests or `NVIDIA_VISIBLE_DEVICES` expose the leased GPU, unless `CUDA_VISIBLE_DEVICES` pins it to other cards (`ordo/control/managed.py` `gpu_refusal`) |

`OpsClient` refuses stack-wide compose verbs (`service=None`) before making
a request; the render pipeline owns stack lifecycle:

```python
ops = OpsClient()
ops.compose_restart()                                   # OpsClientError: stack-wide disabled
ops.compose_restart(service="open-webui", confirm=True) # OK
```

`OpsClient` requires `OPS_CONTROLLER_TOKEN` to be non-empty and sends it as
a Bearer header. `ops-controller` checks it on every path except `/health`
and `/healthz` (constant-time compare, token file re-read per request; see
`ControlPlane.app` in `ordo/control/api.py`) and answers 401 without it.

## Audit log

`ops-controller` appends one JSONL record to `data/ops-controller/audit.log`
(`AUDIT_LOG_PATH=/data/audit.log`) for every state-changing call, refusals
included; Hermes's calls carry `"caller":"hermes"` (schema:
[data.md](../data.md#audit-log)):

```bash
tail -f data/ops-controller/audit.log | jq
```

Rotation: at 10 MB the file rolls to `audit.1.log`; five generations are kept
(`ordo/control/audit.py`).

## What the agent does with the docker CLI

The agent image installs a `docker` wrapper at `/usr/local/bin/docker` (ahead of `/usr/bin` on `PATH`).
It appends one JSON line per call to `/opt/data/logs/docker-calls.jsonl` (`ORDO_DOCKER_CALL_LOG`
overrides the path) and then runs the real CLI unchanged. It exists to measure what the agent really
needs before its Docker socket is removed. Values of `-e`/`--env`, `--build-arg` and login passwords
are redacted, an argument over 500 characters is cut, and the log rolls to `.1` at 20 MB.

```bash
docker exec --user hermes <agent container> python3 /opt/ordo/docker_call_summary.py
docker exec --user hermes <agent container> python3 /opt/ordo/docker_call_summary.py --json
```

The summary lists total calls and the date range, calls per verb (`compose up`, `exec`, `run`, ...),
images used by `run`, `create`, `pull` and `build`, `exec` targets (container plus the first word of
the command) and the target containers of `logs`, `inspect`, `restart`, `stop`, `start`, `rm`, `kill`
and `cp`.

Limit: only calls through the `docker` CLI are logged. A process that talks to the socket directly (for
example `curl --unix-socket`, or a Docker SDK) is not logged, and neither is a call that names
`/usr/bin/docker` by its full path.

## Adding a new control-plane verb

1. Write a failing test in `tests/substrate/` for the new route.
2. Implement the handler in the concern module that owns it under
   `ordo/control/` (the map is in `ordo/control/api.py`'s docstring), give
   `ControlPlane` a one-line delegate, and add a `Route` to `ROUTES` in
   `ordo/control/routes.py` (`exclusive=True` for a verb that changes the
   stack). A `POST` is audited automatically; add its `(action, target)`
   mapping to `_AUDIT_PATH_VERBS` or `_AUDIT_BODY_VERBS` in
   `ordo/control/call_audit.py` so the record names it (otherwise it is
   recorded as `unknown`).
3. If Hermes should call it, add the route to `HERMES_ROUTES` in
   `ordo/control/principals.py` (a privilege grant: the `hermes` token is
   refused with 403 on anything else), then
   add a method on `OpsClient` in `services/hermes/ops_client.py` and, if
   Hermes should call it as a tool, register it in the `ops-router` plugin.
4. Rebuild the `ops-controller` and `agent-hermes` images, then recreate
   both services.

## Recovery: ops-controller down

The ops-router tools fail; the rest of the stack stays up. From the repo root:

```bash
ordo recreate ops-controller
```

## Recovery: ops_client misconfigured

Symptom: every ops-router tool fails with `OPS_CONTROLLER_TOKEN env var is
empty`. Fix: `ordo secrets list` shows whether the store holds `OPS_CONTROLLER_TOKEN`;
`ordo secrets materialize` writes it into `out/secrets.env` (see [secrets.md](secrets.md)), then from the repo root: `ordo recreate agent`. It is
`--no-deps` and lease-checked: the agent's dependency closure contains `inference-engine-gpu`, so a
bare compose `up -d agent` would start the evicted GPU resident beside a leased render.
