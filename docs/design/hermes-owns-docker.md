# Design: Hermes owns Docker on its device (guarded)

**Status:** SUPERSEDED (2026-08-10) — the guarded socket-**proxy** design below was NOT built. The
shipped implementation mounts the raw `/var/run/docker.sock` directly into `services/hermes/agent.yaml`
with prompt-based guardrails in SOUL.md (the operator's "prompting, not tech" call), not the
lease-enforcing proxy proposed here. Retained as the design record of the rejected alternative; do not
read it as current architecture.
**Mandate:** operator, 2026-08-09 — "Hermes should have entire control over Docker, its
device; I want it to execute similar tasks without intervention" AND (prior message) "crashes
can never happen, this must be a resilient box."

## Problem

Hermes (the Discord agent, `ordo-agent-1`) has **no Docker access at all** — no socket mount.
Asked to stand up a website (2026-08-09), it could not, so it narrated `docker run` commands
for the operator to paste and reported success it never achieved (see the agent-logic audit).
The structural bug: Hermes is asked to operate its own host but has no hands.

## The load-bearing constraint

The GPU lease is **cooperative**, not enforced (`ordo/scheduler.py`, `ordo/broker.py`): a lease
STOPS the resident llama.cpp to free VRAM, runs the client workload, then restores it — but the
broker only governs jobs it is told about. A raw `docker run --gpus all` **bypasses the broker
entirely**: llama.cpp is never evicted, two CUDA tenants co-saturate the 5090, and the box
BSODs. This is exactly the 2026-08-08 crash (leaseless ComfyUI renders vs the scheduler
restoring llama.cpp).

⇒ **A raw Docker socket makes the GPU lease unenforceable.** "Entire raw control" and
"resilient / no-crash" are mutually exclusive. The design must reconcile them.

## Design: guarded socket proxy (transparent except GPU-without-lease)

Hermes gets full Docker control through a thin **policy proxy** in front of the real
`docker.sock`, NOT the raw socket. The proxy is a transparent pass-through for every Docker API
call with ONE gate:

- **Container create / update requesting GPU** (any of: `HostConfig.DeviceRequests` with an
  nvidia driver, `--gpus`, `NVIDIA_VISIBLE_DEVICES`/`CUDA_VISIBLE_DEVICES` env, or the nvidia
  runtime) → **REFUSED unless a live scheduler lease is held** by the caller. Error tells Hermes
  to take a lease first (the reel skill's existing `gpu-lease-acquired` path).
- **Everything else** (non-GPU build/run/deploy, stack management, logs, exec, volumes) →
  passes straight through. Entire control.

Additional rails (cheap, same proxy or compose policy):
- **Resource defaults** on Hermes-spawned containers (mem/CPU cap) so a runaway can't starve
  the host.
- **Self/state guards:** refuse `stop`/`rm`/`kill` of `ordo-agent-1` (Hermes' own container —
  known-delicate self-restart) and refuse `volume rm` of the persistent-state volumes
  (hermes-home, qdrant/couch DBs, models-gguf, comfyui-models). Hermes may restart/redeploy any
  SERVICE (it owns the stack) — the guard is only against wiping irreplaceable state and killing
  itself mid-turn.

Why a proxy and not "just don't pass `--gpus`": Hermes composes its own Docker calls; nothing
stops it adding `--gpus` unless something in the request path refuses it. The gate must live in
the socket path, not in Hermes' good intentions.

## Phasing (rails before keys — do NOT hand over control before the fence exists)

1. **Phase 1 — GPU-lease enforcement rail.** Stand up the guarded proxy with the GPU-gate
   policy; verify a leaseless `--gpus` create is refused and a leased one passes. This protects
   the box regardless of who drives, and closes the 2026-08-08 crash class.
2. **Phase 2 — the hands.** Mount the PROXY socket into `services/hermes/agent.yaml`; add the
   resource defaults + self/state guards. Now Hermes has full guarded Docker.
3. **Phase 3 — SOUL prompt directions (NOT a skill).** Update Hermes' core prompt (SOUL.md in
   the `hermes-home` brain volume; gateway restart from host after edit) with accurate,
   load-bearing instructions about the environment it actually runs in — because it flailed
   partly from not knowing where it lives:
   - **Where it runs:** inside `ordo-agent-1`; the operator/browser is on the Windows host, a
     separate network namespace. `localhost:<port>` inside the container is NOT the operator's
     localhost; container-internal paths (`/workspace/data/...`) are NOT host paths (host code
     is at `/c/dev`).
   - **What it can do now:** full Docker on its device via the guarded socket — build, run,
     deploy, manage the stack.
   - **The GPU rule:** any GPU/CUDA container must take a scheduler lease first (the proxy will
     refuse it otherwise); never run leaseless GPU work — it crashed the box on 2026-08-08.
   - **The honesty rule:** never tell the operator something is "done / it works / open this
     URL" without having executed a check and observed the result (e.g. curl the port, get 200).
     Report what was actually verified, and say plainly when something is outside its reach.
   This directly targets the audit's fabrication cascade without new code.

## Non-goals / decisions

- Deploy target for Hermes-built apps = **local throwaway port** (operator choice 2026-08-09),
  not tracked edge services. Reboot-durability is explicitly not required for these.
- The proxy is a project-buildable image, pinned by build sha (not `:latest`) per repo policy.
- This does not touch the min-max / public stacks.

## Validation

- Leaseless `docker run --gpus all hello-world` via the proxy → refused (assert).
- Leased GPU run → passes, llama.cpp evicted then restored (assert against lease-history).
- `docker rm ordo-agent-1` via proxy → refused; `docker restart open-webui` → allowed.
- Hermes end-to-end: given "make me a small site", it builds, runs, probes 200, returns a
  working local URL with NO operator commands — the acceptance test that this whole effort
  exists to pass.
