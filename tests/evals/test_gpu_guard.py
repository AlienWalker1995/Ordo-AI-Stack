"""E15 (round-6 fix): the GPU-lease guard (ordo_evals.gpu_guard) - pure functions taking an
ops-controller `/status` body, exercised with fixtures shaped exactly like `ControlPlane.status()`
(ordo/control.py) and `Scheduler.status()` (ordo/scheduler.py) actually return, never a live stack."""
from __future__ import annotations

import pytest
from ordo_evals import gpu_guard
from ordo_evals.checks import ProbeError

IDLE_STATUS = {
    "manifest": {"model": {"id": "qwen3.8-27b-uncensored-q6"}},
    "gpu": {"state": "idle", "total_vram_gb": 32.0, "free_vram_gb": 32.0, "running": [], "queued": [],
           "waiting_on_vram": False, "eta_seconds": 0.0, "idle_cached": {"llamacpp": 20.0},
           "evicted_residents": {}, "rejected": []},
}

NO_SCHEDULER_STATUS = {"manifest": {"model": {"id": "qwen3.8-27b-uncensored-q6"}}, "gpu": {"state": "no-scheduler"}}


def _busy_status(**gpu_overrides):
    gpu = {"state": "busy", "total_vram_gb": 32.0, "free_vram_gb": 4.0, "running": [], "queued": [],
          "waiting_on_vram": False, "eta_seconds": None, "idle_cached": {}, "evicted_residents": {},
          "rejected": []}
    gpu.update(gpu_overrides)
    return {"manifest": {"model": {"id": "qwen3.8-27b-uncensored-q6"}}, "gpu": gpu}


# ── gpu_lease_state ──────────────────────────────────────────────────────────────

def test_idle_scheduler_is_not_leased():
    leased, detail = gpu_guard.gpu_lease_state(IDLE_STATUS)
    assert leased is False and detail == "idle"


def test_no_scheduler_configured_is_not_leased():
    """A deployment with no GPU scheduler at all has nothing to guard against - see
    ordo/control.py's ControlPlane.status(): {"state": "no-scheduler"} when self.scheduler is None."""
    leased, detail = gpu_guard.gpu_lease_state(NO_SCHEDULER_STATUS)
    assert leased is False and "no GPU scheduler" in detail


def test_a_running_job_is_leased():
    status = _busy_status(running=[{"id": "gate-comfyui", "kind": "media", "remaining_s": 30.0,
                                    "lease_ttl_s": 900.0, "held_s": 5.0}])
    leased, detail = gpu_guard.gpu_lease_state(status)
    assert leased is True and "gate-comfyui" in detail


def test_a_queued_job_is_leased():
    status = _busy_status(queued=[{"id": "gate-comfyui", "kind": "media", "vram_gb": 12.0}])
    leased, detail = gpu_guard.gpu_lease_state(status)
    assert leased is True and "1 GPU job(s) queued" in detail


def test_an_evicted_resident_is_leased_even_with_no_running_or_queued_job():
    """The window right after a media job evicted llamacpp but before the scheduler shows it
    running/queued (or right after it completed and the resident hasn't been restored yet) -
    evicted_residents alone must trigger the guard, not just running/queued."""
    status = _busy_status(evicted_residents={"llamacpp": 20.0})
    leased, detail = gpu_guard.gpu_lease_state(status)
    assert leased is True and "llamacpp" in detail


# ── served_model_for_item ─────────────────────────────────────────────────────────

class _FakeProbes:
    def __init__(self, status=None, fail=False):
        self._status = status
        self._fail = fail

    def ops_status(self):
        if self._fail:
            raise ProbeError("ops-controller unreachable")
        return self._status


def test_served_model_is_the_declared_gpu_model_when_clear():
    served, note = gpu_guard.served_model_for_item(_FakeProbes(IDLE_STATUS), gpu_served_model="qwen-gpu")
    assert served == "qwen-gpu" and note is None


def test_served_model_is_the_cpu_fallback_sentinel_when_leased():
    status = _busy_status(evicted_residents={"llamacpp": 20.0})
    served, note = gpu_guard.served_model_for_item(_FakeProbes(status), gpu_served_model="qwen-gpu")
    assert served == gpu_guard.CPU_FALLBACK_BACKEND and note is None


def test_served_model_is_unknown_with_a_note_when_ops_controller_is_unreachable():
    """Ground truth unreadable must never be silently counted as a clean GPU answer - same rule
    checks.py's module docstring states for every other out-of-band check in this package."""
    served, note = gpu_guard.served_model_for_item(_FakeProbes(fail=True), gpu_served_model="qwen-gpu")
    assert served == gpu_guard.UNKNOWN_BACKEND
    assert note is not None and "could not check" in note


@pytest.mark.parametrize("gpu_served_model", ["qwen-gpu", "qwen3.8-27b-uncensored-q6"])
def test_served_model_round_trips_whatever_the_run_declared(gpu_served_model):
    served, _ = gpu_guard.served_model_for_item(_FakeProbes(IDLE_STATUS), gpu_served_model=gpu_served_model)
    assert served == gpu_served_model
