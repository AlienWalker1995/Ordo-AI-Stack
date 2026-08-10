# gpu-gate — GPU admission gate

A generic reverse proxy that acquires GPU **residency** from `ops-controller` *before* it lets a
job-submission request reach the service behind it.

Build:

```
docker build -f services/gpu-gate/Dockerfile -t ordo/gpu-gate:latest services/gpu-gate
```

## Why a gate rather than a lease call

Most GPU consumers can ask for the card themselves: a one-shot job wraps itself in
`assets/lease-exec.py` (ltx-trainer), and a resident is granted the card by the broker
(llama.cpp). Neither works for a long-running server with a submission API and dynamic demand:

* it cannot take residency at start — it would hold the card while idle;
* it cannot be trusted to take residency per job — the work is submitted by whoever is holding
  the mouse, and ComfyUI's node-graph web UI has never heard of the scheduler.

On 2026-08-08 that gap let a hand-queued render share the 5090 with the resident llama.cpp for
about four hours at ~98% VRAM until the Windows graphics kernel bugchecked (`0x113`).

## Why it is proactive

The tempting fix is to poll the server's queue and take residency when it goes non-empty. That
does not close the hole: ComfyUI begins executing the moment Queue Prompt is pressed, so a poller
acquires *after* the render is already running on a card that still holds the LLM.

This is not theoretical. Measured on the live stack, 2026-08-10:

| | leaseless render (LLM resident) | same card, residency granted first |
|---|---|---|
| MiniMax-H3 sampling | 452–1577 s/it, 4/20 steps in 44 min | — |
| flux-schnell 512² | — | 6.29 it/s, prompt executed in 17.9 s |

The leaseless render had already committed to an offloaded plan (`0.00 MB loaded, 19984 MB
offloaded`); evicting the LLM *afterwards* did not rescue it. Only acquiring first works.

## What it is not

It is **not** an arbiter. It never inspects VRAM, never decides who wins, and never starts or
stops another container. It is a client of `ops-controller` (`POST /jobs`, `/jobs/heartbeat`,
`/jobs/complete`) using the same `ORDO_LEASE_*` env vocabulary as `assets/lease-exec.py`. Two
things racing to arbitrate one card is the documented deadlock that retired the old ComfyUI
guardian.

## One residency, not one per prompt

The gate holds a single lease under a stable job id while the upstream has outstanding work. The
upstream then drains its **own** queue from that grant. Queuing every prompt with the arbiter
would make each one a peer of unrelated GPU work and interleave them onto the card — the opposite
of what residency is for. This is the same split the scheduler applies to llama.cpp: residency is
queued, individual inference calls are not.

## Configuration

Nothing here is hand-written in practice. Every value is rendered from the upstream service's
`gpu_arbitration:` block by `ordo/compose.py::_gpu_gate`, so the declaration and the running gate
cannot disagree. See `ordo/gpu.py` for the schema and `services/comfyui/plugin.yaml` for the
first consumer.

| env | meaning |
|---|---|
| `GATE_UPSTREAM` | base URL of the service being fronted |
| `GATE_LISTEN_PORT` | gate's port (defaults to the upstream's, so it is a drop-in) |
| `GATE_SUBMIT_PATHS` / `GATE_SUBMIT_METHODS` | which requests start GPU work |
| `GATE_QUEUE_PATH` / `GATE_QUEUE_STYLE` | how to tell whether work is outstanding |
| `GATE_DRAIN_SECONDS` | queue must read empty this long before residency is released |
| `GATE_MAX_HOLD_SECONDS` | cap on one continuous hold (wedged-upstream guard; `0` disables) |
| `OPS_CONTROLLER_URL` / `OPS_CONTROLLER_TOKEN` | the arbiter |
| `ORDO_LEASE_*` | the residency request, shared with `assets/lease-exec.py` |

`GET /_gpu_gate/health` and `GET /_gpu_gate/status` are the gate's own endpoints; everything else
is proxied, including the WebSocket the UI uses for execution progress.

## Failure behaviour

Every one of these is deliberate, and each has a test in `tests/test_gpu_gate.py`.

| situation | behaviour |
|---|---|
| arbiter unreachable / refused / timed out | submission **refused** with `503` in the upstream's own error envelope, so the web UI shows it in its normal error dialog. Never proceeds unleased. |
| submitter gives up (acquire timeout) | the queued request is **withdrawn**. Leaving it would see it admitted later, evicting the resident for a render that never happened — observed live 2026-08-10 as ~10 orphaned entries that stranded llama.cpp until drained by hand. |
| gate crashes mid-render | heartbeats stop; the scheduler's TTL sweep force-completes the lease and restores the resident. No leak. |
| gate restarts | stable job id + an unconditional release at startup clears a lease its previous incarnation stranded, without waiting out the TTL. |
| arbiter restarts and forgets the lease | heartbeat 404s → re-acquire, rather than letting the resident be restored into VRAM the render is still using. |
| upstream unreachable | treated as *not* busy, so a dead upstream drains and gives the card back. |
| upstream **wedged** (alive, reports work, makes no progress) | the one case the scheduler's TTL cannot catch, because a live gate keeps heartbeating. Capped by `GATE_MAX_HOLD_SECONDS`: residency is released, the backstop is suppressed until the queue drains, and the condition is logged as an alarm. This is a real trade-off, not a clean win — releasing lets the resident back onto a card that may still have a wedged render on it — so the cap is set far above any legitimate render and its expiry means *go look at the upstream*. |

## Known limitation

The backstop (acquire late if the upstream is busy while the gate holds nothing) **narrows** the
bypass window; it cannot close it, because by then the work has started. Only routing every
caller through the gate closes it. The render pins `COMFYUI_URL` at the gate for mcp-gateway,
comfyui-mcp, the dashboard and ops-api, and the Caddy edge enters through the gate — those are
the known callers, and `tests/substrate/test_gpu_arbitration.py` asserts they stay pointed at it.
A caller that reaches the container directly on the project network is still a bypass, and shows
up as `bypass_detected` in `/_gpu_gate/status`.
