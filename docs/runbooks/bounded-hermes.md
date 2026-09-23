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
resident llama.cpp. The rejected enforcing alternative (a guarded socket
proxy) is recorded in `docs/design/hermes-owns-docker.md`.

Anything Hermes does over the raw socket is not audited by Ordo.

## First-class ops tools (the `ops-router` plugin)

`services/hermes/plugins/ops-router/` exposes the control plane's container
verbs as Hermes tools. They wrap `OpsClient` (`services/hermes/ops_client.py`)
and call `ops-controller` (`OPS_CONTROLLER_URL=http://ops-controller:9000`):

| Tool | ops-controller route |
|---|---|
| `list_containers` | `GET /containers` |
| `container_logs` | `GET /containers/{name}/logs` |
| `restart_container` | `POST /containers/{name}/restart` |
| `compose_restart`, `compose_up` | `POST /services/{name}/recreate` |

`OpsClient` refuses stack-wide compose verbs (`service=None`) before making
a request; the render pipeline owns stack lifecycle:

```python
ops = OpsClient()
ops.compose_restart()                                   # OpsClientError: stack-wide disabled
ops.compose_restart(service="open-webui", confirm=True) # OK
```

`OpsClient` requires `OPS_CONTROLLER_TOKEN` to be non-empty and sends it as
a Bearer header. `ops-controller` itself does not check it (auth is Caddy's
job at the edge, see the `ordo/control.py` module docstring).

## Audit log

`ops-controller` appends JSONL records to `data/ops-controller/audit.log`
(`AUDIT_LOG_PATH=/data/audit.log`) for `env/set`, image pulls, ComfyUI
node-requirement installs and GPU-assign attempts:

```bash
tail -f data/ops-controller/audit.log | jq
```

Container and service lifecycle verbs are not audited. Rotation: at 50MB the
file rolls to `audit.1.log`; one historical generation is kept
(`ordo/audit.py`).

## Adding a new control-plane verb

1. Write a failing test in `tests/substrate/` for the new route.
2. Implement the handler on the control plane in `ordo/control.py` and add
   it to `ControlPlane.route()`. Call `self._audit(...)` if it mutates state.
3. Add a method on `OpsClient` in `services/hermes/ops_client.py` and, if
   Hermes should call it as a tool, register it in the `ops-router` plugin.
4. Rebuild the `ops-controller` and `agent-hermes` images, then recreate
   both services.

## Recovery: ops-controller down

The ops-router tools fail; the rest of the stack stays up. From the host
(the rendered compose lives in `out/`):

```bash
cd out
docker compose -p ordo --env-file .env --env-file secrets.env restart ops-controller
```

## Recovery: ops_client misconfigured

Symptom: every ops-router tool fails with `OPS_CONTROLLER_TOKEN env var is
empty`. Fix: fill `OPS_CONTROLLER_TOKEN` in `out/secrets.env` (see
[secrets.md](secrets.md)), then from `out/`:
`docker compose -p ordo --env-file .env --env-file secrets.env up -d agent`.
