---
authority: canonical
status: current
owner: operator
last_verified: 2026-11-12
verified_against: 1c97d8f3608b
---

# ADR-0001: The agent holds no Docker socket

## Status

Accepted, 2026-11-12. Not yet implemented: the socket is still mounted until card 4 below is done.

Replaces the earlier grant of full Docker control guarded by prompt rules (`docs/design/hermes-owns-docker.md`, a design record, not current architecture).

## Context

The agent (Hermes, the `agent` service) reads untrusted input: chat messages, web pages, search results, MCP tool output. Prompt injection has no reliable filter, so the agent has to be treated as something that will, at some point, act on an attacker's text.

A Docker socket is root on the host. An agent with the socket can start a container that mounts the host filesystem, read every other container's environment, or exec into the control plane. Guardrails in a prompt are advice, not enforcement, and do not hold against injected instructions. A raw socket also bypasses the GPU lease: a container started with GPU access never passes through the scheduler, which is the crash class the lease exists to prevent.

The state today:

| Thing | State | Where |
|---|---|---|
| Docker socket in the agent | Mounted, usable by the `hermes` user | `services/hermes/agent.yaml` (`/var/run/docker.sock`) |
| Code mirror | The operator's code root, read-write, at `/c/dev` | `services/hermes/agent.yaml` (`CODE_ROOT`) |
| Ops-controller credential | The scoped `hermes` token only, never the admin token | `services/hermes/agent.yaml` (`OPS_CONTROLLER_TOKEN_HERMES`) |
| Route allowlist | Observation, the GPU lease, per-service restart and recreate, plugin and model switches, and status, logs and restart for managed projects | `HERMES_ROUTES` in `ordo/control/principals.py` |
| Agent tools over those routes | The `ops-router` plugin | `services/hermes/plugins/ops-router/`, [runbook](../runbooks/bounded-hermes.md) |
| Test | Asserts the agent has the socket and can use it | `tests/test_hermes_docker_access.py` |

Two of these still give a tricked agent a way to admin power, and the socket is only one of them:

1. The socket itself. The scoped `hermes` principal means nothing while the socket sits beside it.
2. The read-write code mirror, when the tree it mounts also holds the render output (`out/`, with the secret store's materialized values) or the checkout that `ordo deploy` follows. Anything the agent writes there is later run or served by the host.

Removing the socket without closing the second path changes nothing that matters.

The agent uses Docker for three things:

| Use | Replacement | State |
|---|---|---|
| Observe and recover: list, logs, inspect, restart, recreate | The `ops-router` tools over `HERMES_ROUTES` | Built |
| Run a command inside another container (`docker exec`) | That service's own HTTP API where it has one. Otherwise a named task: one route per operation, with a fixed command and validated arguments | To build |
| Start throwaway containers and build images (browser automation, test environments, eval runs, previews) | A sandbox-container route on ops-controller, with the limits in rule 3 | To build |

## Decision

The agent loses its Docker socket. Every Docker action it takes goes through ops-controller, under the scoped `hermes` principal, where it is allowlisted, lease-checked and audited. Nothing the agent can do today is dropped: each use of the socket gets a replacement first, and the socket is removed last.

The principle: a tricked agent must be harmless. What the agent can reach is the control, not what it is told.

The rules below are what a PR is judged against.

1. **No generic exec.** No route runs a caller-supplied command in a caller-chosen container. Exec into ops-controller or into the agent itself is admin power, so a generic exec route is the socket again under another name. Each operation the agent needs inside another container is its own named route.
2. **Every grant is a line in `HERMES_ROUTES`.** The allowlist is code, baked into the ops-controller image. A new route for the agent is a privilege grant and is reviewed as one.
3. **Sandbox containers are fenced by the route, not by the caller.** A container the agent starts through ops-controller:
   - mounts no host path and no Docker socket; it may use a scratch volume that belongs to the agent;
   - is never privileged, adds no capabilities, and uses no host network, PID or IPC namespace;
   - gets a GPU only under a live scheduler lease held by the caller;
   - joins its own network, not the stack network, unless the route names the one service it may reach;
   - carries memory, CPU and PID limits and an expiry, after which ops-controller removes it;
   - is labelled as agent-started, and its creation, exit and removal are audited.
4. **The agent cannot write what the host runs or trusts.** The render output, the materialized secrets and the checkout `ordo deploy` follows sit outside every path mounted read-write into the agent. Read-only overlays under the mirror do not meet this rule: on a case-insensitive host mount, a differently cased path reaches the same files and skips the overlay.
5. **The agent keeps read-write access to code it works on.** Repository editing is core functionality. It happens in worktrees and clones that nothing on the host runs without a reviewed merge.
6. **No new holder of the socket.** ops-controller and the monitoring plugin's read-only socket proxy are the only services that mount it. A new service that wants Docker asks ops-controller.

## Consequences

What stays the same:

- The agent edits code, opens PRs and maintains the stack and the operator's managed projects.
- The agent starts containers and builds images for its own work, inside the limits of rule 3.
- ops-controller keeps the socket. It has no host port, scopes every call to the projects it manages, and audits every state-changing call.

What changes:

- The GPU lease becomes enforced for the agent instead of advised.
- Everything the agent does to Docker is in the ops-controller audit log. Today its socket traffic is recorded nowhere.
- A kind of Docker work nobody anticipated needs a new route before the agent can do it. This is the cost accepted: new reach for the agent arrives as a reviewed change, not as improvisation at run time.
- The agent's skills that call the docker CLI have to move to the `ops-router` tools, the named tasks or the sandbox route.

## Backlog

Four cards, in order. Each ships on its own and leaves the agent fully working.

| # | Card | Done when |
|---|---|---|
| 1 | Move the render output, the materialized secrets and the deployed checkout out of the agent's read-write mounts (rule 4) | From inside the agent, no path reaches a secret value or a file the host executes, and `ordo doctor` checks it |
| 2 | Measure: log every Docker API call the agent makes for at least seven days, changing nothing | There is a list of the verbs, images and exec targets the agent really uses |
| 3 | Build the named tasks and the sandbox-container route for that measured list, add them to `HERMES_ROUTES` and `ops-router`, and move the agent's skills onto them | A further seven days pass in which the logged socket traffic is zero |
| 4 | Remove the socket mount from `services/hermes/agent.yaml`; invert `tests/test_hermes_docker_access.py` to assert the agent has no socket | The test passes against a live stack and the agent's eval suite shows no regression |

Card 1 is first because it is the larger hole and depends on nothing else. Card 2 comes before card 3 because the replacement surface should match what the agent does, not what its skill text mentions.
