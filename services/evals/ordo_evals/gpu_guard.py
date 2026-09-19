"""GPU-lease guard (E15, round-6 fix): a preflight refusal, a cheap between-suite re-check, and a
per-item served-backend classification, all built on the SAME ground truth - ops-controller's
`/status` scheduler block (`checks.Probes.ops_status()`, already used by `runner._served_model` and
by `checks._check_ops_model`). No new secret and no new endpoint: `services/evals/plugin.yaml`
already points `OPS_CONTROLLER_URL` at the scheduler service (`ordo/control.py`'s `ControlPlane`,
compose service `ops-controller`), which - by design, per its own module docstring ("No auth here:
the dashboard is localhost-only and this is the full control plane behind it") - takes no auth at
all. (The dashboard's `OPS_CONTROLLER_TOKEN` env var is for the unrelated `ops-api` service -
`services/v1-parity/dashboard.yaml` points the dashboard's own `OPS_CONTROLLER_URL` at
`http://ops-api:9000`, not at this one - so there is nothing to invent or wire here.)

Why this is the right ground truth for "is llama.cpp about to be (or already) starved of the GPU":
`services/gpu-gate/gate.py` sits in front of every GPU-work submission API (ComfyUI's the canonical
one) and takes scheduler residency BEFORE the work starts, holding it (with heartbeats) until the
upstream's own queue has drained - so a non-idle scheduler (a running or queued job, or a resident
already evicted to free VRAM) means llama.cpp may right now be stopped, and LiteLLM's configured
fallback (`services/model-gateway/litellm_config.yaml`'s `router_settings.fallbacks`) may be serving
`local-chat` from the slow CPU deployment instead - the exact iteration-4 failure
(`docs/superpowers/plans/2026-09-19-evals-fix-round-6-brief.md`) this module exists to catch.

Verified read-only against the running stack (2026-09-19), not guessed: the model suites call
LiteLLM directly, so their per-item served-backend signal is free and exact -
`sample.output.model`, populated by Inspect from the raw completion response's own `model` field.
llama.cpp sets that field to the path of the model IT has loaded (confirmed distinct between the
GPU and CPU deployments: `GET /v1/models` on the GPU container reports
`/models/Qwen3.8-27B-Uncensored-Q6_K.gguf`, the CPU container `/models/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`),
and does not rewrite it to match the request's own `model` value; LiteLLM's
`convert_to_model_response_object` (`litellm/litellm_core_utils/llm_response_utils/
convert_dict_to_response.py`) passes the upstream `model` field through unchanged
(`model_response_object.model = response_object["model"]`). See `suites/common.py`'s `sample_item`.

Hermes, which the harness suites drive, has NO equivalent per-turn signal to reuse: its own
`/v1/chat/completions` response `model` field is a pure echo of the REQUEST's `model` parameter
(`gateway/platforms/api_server.py`: `model_name = body.get("model", self._model_name)`, read
directly off the running `ordo-agent-1` image), and its `state.db` `sessions.model` column records
that same constant, configured value (confirmed against real run evidence:
`data/evals/runs/loop3-20260918-1644` and `loop4-20260919-0025` both show `trajectory.model ==
"local-chat"` for every harness item, including loop4's items that iteration 4's own investigation
showed were served by the CPU fallback) - neither ever changes when LiteLLM fails the underlying
call over to the CPU deployment. So for the harness suites, `served_model_for_item` below is the
best available substitute: not the literal serving model's name, but whether the GPU was clear (the
declared model, presumed serving) or leased (the CPU-fallback sentinel) at the moment this item's
Hermes turn finished - checked with the same ops-controller call the preflight and mid-run guards
already make, not an extra secret or a new per-item network dependency.
"""
from __future__ import annotations

from typing import Any

from ordo_evals.checks import ProbeError

# Sentinel served_model values for harness items (which have no real per-request model field - see
# the module docstring). Distinct from any real catalog id or gguf path, so the run-level
# distinct-backend check (runner._backend_integrity_reason) can never mistake one for a legitimate
# model identifier.
CPU_FALLBACK_BACKEND = "cpu-fallback (gpu leased)"
UNKNOWN_BACKEND = "unknown (gpu status unavailable)"


def gpu_lease_state(status: dict[str, Any]) -> tuple[bool, str]:
    """(leased, detail) from an ops-controller `/status` body (`checks.Probes.ops_status()`).
    `leased` is True when the scheduler shows a running or queued job, or a resident has been
    evicted to free VRAM for one - any of these can mean llama.cpp is stopped or about to be.
    `state: "no-scheduler"` (no GPU on this deployment, or the control plane started without one) is
    not a lease - there is no GPU contention to guard against."""
    gpu = status.get("gpu") or {}
    if gpu.get("state") == "no-scheduler":
        return False, "no GPU scheduler configured"
    evicted = gpu.get("evicted_residents") or {}
    if evicted:
        return True, f"resident(s) evicted to free VRAM: {sorted(evicted)}"
    running = gpu.get("running") or []
    if running:
        return True, f"{len(running)} GPU job(s) running: {[j.get('id') for j in running]}"
    queued = gpu.get("queued") or []
    if queued:
        return True, f"{len(queued)} GPU job(s) queued: {[j.get('id') for j in queued]}"
    return False, "idle"


def served_model_for_item(probes: Any, *, gpu_served_model: str) -> tuple[str, str | None]:
    """(served_model, note) for one harness item, checked right after its Hermes turn completes (see
    the module docstring for why this - not Hermes's own response - is the signal used):
    `gpu_served_model` (the run's declared active GPU model, `runner._served_model`) when the
    scheduler was clear; `CPU_FALLBACK_BACKEND` when it was leased; `UNKNOWN_BACKEND` with a note
    when ops-controller could not be reached at all (never silently counted as a clean GPU answer -
    the same "ground truth unreadable is not a pass" rule every other check in this package follows,
    see checks.py's module docstring)."""
    try:
        status = probes.ops_status()
    except ProbeError as exc:
        return UNKNOWN_BACKEND, f"could not check GPU lease state for served_model ({exc})"
    leased, _detail = gpu_lease_state(status)
    return (CPU_FALLBACK_BACKEND if leased else gpu_served_model), None
