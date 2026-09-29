---
name: ops-controller-api
description: Reference for ops-controller, the only path for container and GPU-lease operations from inside Hermes. Your scoped token, the routes it grants, what a 403 means, and the tools that wrap them.
---

# ops-controller API (Hermes' scoped access)

Shipped read-only in the agent image (repo: `services/hermes/skills/devops/ops-controller-api/`).
It replaces any older local copy of this skill.

## Your credential

- `OPS_CONTROLLER_TOKEN` in your environment is **your own scoped token** (the `hermes` principal),
  not the operator's admin token. In a `docker exec` shell without it, read
  `$OPS_CONTROLLER_TOKEN_FILE`.
- Send it as `Authorization: Bearer $OPS_CONTROLLER_TOKEN` to `http://ops-controller:9000`.
- **403 = outside your grant.** It is the boundary, not a broken token. Do not retry with another
  token, do not look for one: `out/secrets.env` and `out/secrets/` are hidden from you on purpose,
  and reading, copying or reconstructing a secret is off limits. Tell the operator what you need.
- Every write you make, and every refused call, is in the audit log under `principal: hermes`.

## Use the tools first

| Need | Tool |
|---|---|
| List containers (whole host, read-only) | `list_containers()` |
| An Ordo container's shape (image, state, health, mounts, networks, ports) | `inspect_container(name)` |
| An Ordo container's logs | `container_logs(name, tail)` |
| Bounce a wedged Ordo container | `restart_container(name, confirm=true)` |
| Apply an .env / image / volume change to an Ordo service | `compose_up(service, confirm=true)` |
| Install / remove an optional Ordo service | `enable_service(plugin_id, confirm=true)`, `disable_service(plugin_id, confirm=true)` |
| Another stack you maintain (listed in ordo.yaml `managed_projects:`) | `project_containers(project)`, `project_logs(project, name)`, `restart_project_container(project, name, confirm=true)` |

## Routes your token may call (everything else answers 403)

Reads:

- GET /status (GPU lease state)
- GET /containers
- GET /containers/{name}
- GET /containers/{name}/logs
- GET /services
- GET /services/{id}/logs
- GET /stats/services (per-service CPU and memory)
- GET /plugins
- GET /model-config
- GET /gpus
- GET /registry/models
- GET /registry/gpus
- GET /jobs/history
- GET /diagnostics/dstate
- GET /models/download/status
- GET /projects
- GET /projects/{project}/containers
- GET /projects/{project}/containers/{name}/logs (audited)

Writes (each needs `{"confirm": true}` where the tool says so):

- POST /jobs, POST /jobs/heartbeat, POST /jobs/complete: the GPU lease. ComfyUI renders go through
  the gate (`$COMFYUI_URL`), which takes the lease for you.
- POST /services/{id}/restart and POST /services/{id}/recreate: one Ordo service, lease-checked.
- POST /containers/{name}/restart: one Ordo container, lease-checked.
- POST /plugins/{id}/enable and POST /plugins/{id}/disable.
- POST /model-config: a model switch.
- POST /models/download: HTTPS from the allowlisted model hosts only.
- POST /projects/{project}/containers/{name}/restart: a managed project's container; at most 3 per
  container per hour (then 429), and refused (409) for a container that could use the leased GPU.

## Not yours (the operator does these on the host)

- Stack-wide lifecycle: `/apply`, `/compose/*`, stopping or starting a service. The operator runs
  `ordo apply` or `ordo up` on the host.
- Building images: `ordo build <service>` on the host, then `ordo apply`.
- Code execution inside a container (ComfyUI node requirement installs, `docker exec`): ask the
  operator.
- Anything under `out/`: rendered deploy state, read-only to you. Never edit it.

## Picking the verb

- Process wedged, or a bind-mounted file changed: `restart_container`.
- `.env`, environment, image, volumes or network changed: `compose_up` (a recreate). A restart does
  not pick those up.
- Service not rendered yet: `enable_service`, never `compose_up`.
- A 409 on a restart or recreate usually means a GPU lease holds the card: check GET /status and
  try again after it is released. Never work around the lease.
