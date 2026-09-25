"""The scheduler's lease and eviction state survives an ops-controller restart.

Before this, that state lived only in ops-controller's memory. Restarting it mid-lease lost it:
the evicted resident (llama.cpp) was never restored, or was restored beside the running render
(two tenants on one card crashed the host on 2026-08-08). The broker now writes the state to disk
on every transition and the next process adopts it before serving. Nothing here touches docker.
"""
from __future__ import annotations

import json

import pytest

from ordo.broker import Broker, MockBackend
from ordo.scheduler import Job, Scheduler
from ordo.scheduler_state import RECOVERY_JOB_ID, STATE_VERSION, SchedulerStateStore, StateUnreadable


class Clock:
    """A settable wall clock, so a test can say "the controller was down for 20 minutes"."""

    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class RunningBackend(MockBackend):
    """MockBackend whose list_services reports exactly the given services as running."""

    def __init__(self, running: set[str] | None = None, fail: bool = False):
        super().__init__()
        self.running = set(running or ())
        self.fail = fail

    def list_services(self) -> dict:
        if self.fail:
            raise RuntimeError("docker unreachable")
        return {"services": [{"id": s, "state": "running"} for s in sorted(self.running)]}


def _controller(path, clock, backend=None, total=32.0, resident_gb=25.0):
    """What `ordo serve` builds at startup: residents registered from the render, then the
    persisted state adopted before the first request is served."""
    sched = Scheduler(total)
    sched.cache_idle("llamacpp", resident_gb)
    broker = Broker(sched, backend or MockBackend(), state_store=SchedulerStateStore(path, now_fn=clock))
    return sched, broker


# --- the file ----------------------------------------------------------------------------------


def test_every_transition_is_written_to_disk(tmp_path):
    path = tmp_path / "scheduler-state.json"
    clock = Clock()
    _sched, broker = _controller(path, clock)

    broker.request(Job("gate-comfyui", 18, "media", est_seconds=600))
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == STATE_VERSION
    assert [r["id"] for r in doc["running"]] == ["gate-comfyui"]
    assert doc["evicted"] == {"llamacpp": 25.0}

    broker.complete("gate-comfyui")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["running"] == [] and doc["evicted"] == {}


def test_deadlines_are_stored_as_wall_clock_and_heartbeats_move_them(tmp_path):
    path = tmp_path / "scheduler-state.json"
    clock = Clock(5_000.0)
    sched, broker = _controller(path, clock)
    broker.request(Job("gate-comfyui", 18, "media", est_seconds=600))  # TTL 1200s
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["running"][0]["lease_deadline"] == pytest.approx(5_000.0 + 1200.0)
    assert doc["running"][0]["started_at"] == pytest.approx(5_000.0)

    sched.tick(100)
    clock.now += 100
    assert broker.heartbeat("gate-comfyui")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["running"][0]["lease_deadline"] == pytest.approx(5_100.0 + sched.heartbeat_ttl)


def test_the_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "scheduler-state.json"
    _sched, broker = _controller(path, Clock())
    broker.request(Job("gate-comfyui", 18, "media"))
    broker.complete("gate-comfyui")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scheduler-state.json"]


def test_a_failed_write_is_reported_not_raised(tmp_path):
    """A full disk must not take the control plane down, but the state on disk is then stale,
    so the controller stops claiming it is persisted (the host bring-up reads that claim)."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    _sched, broker = _controller(blocker / "scheduler-state.json", Clock())
    broker.request(Job("gate-comfyui", 18, "media"))
    assert broker.state_persisted is False


class CountingStore(SchedulerStateStore):
    def __init__(self, path, now_fn, fail: bool = False):
        super().__init__(path, now_fn=now_fn)
        self.saves = 0
        self.fail = fail

    def save(self, snapshot: dict) -> None:
        if self.fail:
            raise OSError("disk full")
        self.saves += 1
        super().save(snapshot)


def test_an_idle_lease_loop_tick_does_not_rewrite_the_file(tmp_path):
    """The lease loop sweeps every few seconds; the file changes only on a transition."""
    sched = Scheduler(32)
    sched.cache_idle("llamacpp", 25)
    store = CountingStore(tmp_path / "scheduler-state.json", Clock())
    broker = Broker(sched, MockBackend(), state_store=store)
    broker.request(Job("gate-comfyui", 18, "media", est_seconds=600))
    saves = store.saves
    for _ in range(5):
        sched.tick(10)
        broker.sweep_leases()
    assert store.saves == saves


def test_the_lease_loop_retries_a_failed_write(tmp_path):
    sched = Scheduler(32)
    store = CountingStore(tmp_path / "scheduler-state.json", Clock(), fail=True)
    broker = Broker(sched, MockBackend(), state_store=store)
    broker.request(Job("gate-comfyui", 18, "media"))
    assert broker.state_persisted is False
    store.fail = False
    broker.sweep_leases()
    assert broker.state_persisted is True
    assert json.loads(store.path.read_text(encoding="utf-8"))["running"][0]["id"] == "gate-comfyui"


def test_no_store_means_not_persisted():
    broker = Broker(Scheduler(32), MockBackend())
    broker.request(Job("gate-comfyui", 18, "media"))
    assert broker.state_persisted is False


# --- restart mid-lease --------------------------------------------------------------------------


def test_restart_mid_lease_keeps_the_resident_evicted_and_restores_it_on_drain(tmp_path):
    path = tmp_path / "scheduler-state.json"
    clock = Clock()
    _old, old_broker = _controller(path, clock)
    old_broker.request(Job("gate-comfyui", 18, "media", est_seconds=600))
    assert "llamacpp" in old_broker.backend.stopped

    # ops-controller is recreated 30s later; the render is still running.
    clock.now += 30
    sched, broker = _controller(path, clock)
    broker.restore_state()
    assert broker.state_persisted is True
    assert sched.running_ids == ["gate-comfyui"]
    assert sched.evicted_residents == {"llamacpp": 25.0}
    assert "llamacpp" not in sched.idle_cached
    assert broker.backend.started == [], "the resident was restored beside the running render"
    assert sched.status()["leased"] is True
    # the TTL survived the restart, minus the time that passed
    assert sched.status()["running"][0]["lease_ttl_s"] == pytest.approx(1200.0 - 30.0, abs=0.2)
    assert sched.status()["running"][0]["held_s"] == pytest.approx(30.0, abs=0.2)

    # the gate's heartbeat is still recognized (no 404, so no re-acquire) ...
    assert broker.heartbeat("gate-comfyui") is True
    # ... and when it releases, the resident comes back
    broker.complete("gate-comfyui")
    assert broker.backend.started == ["llamacpp"]
    assert sched.evicted_residents == {}


def test_a_queued_request_survives_the_restart(tmp_path):
    path = tmp_path / "scheduler-state.json"
    clock = Clock()
    _old, old_broker = _controller(path, clock)
    old_broker.request(Job("render-a", 18, "media", est_seconds=600))
    old_broker.request(Job("render-b", 18, "media", est_seconds=600))

    sched, broker = _controller(path, clock)
    broker.restore_state()
    assert sched.running_ids == ["render-a"]
    assert sched.queued_ids == ["render-b"]
    broker.complete("render-a")
    assert sched.running_ids == ["render-b"]
    assert "llamacpp" not in broker.backend.started, "restored between back-to-back renders"
    broker.complete("render-b")
    assert broker.backend.started[-1] == "llamacpp"


def test_a_lease_whose_ttl_passed_while_down_is_swept_and_the_resident_restored(tmp_path):
    path = tmp_path / "scheduler-state.json"
    clock = Clock()
    _old, old_broker = _controller(path, clock)
    old_broker.request(Job("gate-comfyui", 18, "media", est_seconds=30))  # TTL 60s

    clock.now += 600  # down for ten minutes: the holder never heartbeated, so it is gone
    sched, broker = _controller(path, clock)
    broker.restore_state()
    assert sched.running_ids == []
    assert sched.evicted_residents == {}
    assert broker.backend.started == ["llamacpp"]
    assert json.loads(path.read_text(encoding="utf-8"))["running"] == []


def test_an_evicted_resident_with_no_lease_left_is_restored_on_startup(tmp_path):
    """A controller that died between completing the last job and restoring the resident."""
    path = tmp_path / "scheduler-state.json"
    clock = Clock()
    path.write_text(json.dumps({"version": STATE_VERSION, "saved_at": clock.now, "running": [],
                                "queued": [], "evicted": {"llamacpp": 25.0}, "rejected": []}),
                    encoding="utf-8")
    sched, broker = _controller(path, clock)
    broker.restore_state()
    assert broker.backend.started == ["llamacpp"]
    assert sched.evicted_residents == {}


def test_no_state_file_is_a_clean_start(tmp_path):
    path = tmp_path / "scheduler-state.json"
    sched, broker = _controller(path, Clock())
    broker.restore_state()
    assert sched.running_ids == [] and sched.evicted_residents == {}
    assert sched.idle_cached == {"llamacpp": 25.0}
    assert broker.backend.started == [] and broker.backend.stopped == []
    assert path.exists()


# --- unreadable state: start conservative -------------------------------------------------------


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps({"version": STATE_VERSION + 1, "running": [], "queued": [], "evicted": {}}),
    json.dumps({"version": STATE_VERSION, "running": [{"id": "x"}], "queued": [], "evicted": {}}),
    json.dumps(["a", "list"]),
])
def test_the_loader_refuses_what_it_cannot_trust(tmp_path, content):
    path = tmp_path / "scheduler-state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateUnreadable):
        SchedulerStateStore(path, now_fn=Clock()).load()


def test_corrupt_state_keeps_a_stopped_resident_evicted_under_a_recovery_lease(tmp_path, capsys):
    """The lost file may have been hiding a live render, and llama.cpp being down is the sign of
    one. Restoring it could put two tenants on one card, so it stays down under a recovery lease
    that live holders re-file beside (their heartbeat 404s) and that expires on its own."""
    path = tmp_path / "scheduler-state.json"
    path.write_text("{truncated", encoding="utf-8")
    sched, broker = _controller(path, Clock(), backend=RunningBackend(running=set()))
    broker.restore_state()
    assert sched.evicted_residents == {"llamacpp": 25.0}
    assert sched.running_ids == [RECOVERY_JOB_ID]
    assert broker.backend.started == []
    assert "scheduler state" in capsys.readouterr().err.lower()

    # a live gate re-files after its heartbeat 404s; the resident stays down while it renders
    assert broker.heartbeat("gate-comfyui") is False
    broker.request(Job("gate-comfyui", 18, "media"))  # a render big enough to have evicted it
    sched.tick(sched.heartbeat_ttl + 1)
    broker.sweep_leases()
    assert sched.running_ids == ["gate-comfyui"]
    assert "llamacpp" not in broker.backend.started
    broker.complete("gate-comfyui")
    assert broker.backend.started[-1] == "llamacpp"


def test_corrupt_state_restores_the_resident_once_the_recovery_lease_expires(tmp_path):
    path = tmp_path / "scheduler-state.json"
    path.write_text("{truncated", encoding="utf-8")
    sched, broker = _controller(path, Clock(), backend=RunningBackend(running=set()))
    broker.restore_state()
    sched.tick(sched.heartbeat_ttl + 1)
    assert broker.sweep_leases() == [RECOVERY_JOB_ID]
    assert broker.backend.started == ["llamacpp"]


def test_corrupt_state_leaves_a_running_resident_alone(tmp_path):
    """llama.cpp running means nothing evicted it: stopping it would be a chat outage for a lease
    that does not exist."""
    path = tmp_path / "scheduler-state.json"
    path.write_text("{truncated", encoding="utf-8")
    sched, broker = _controller(path, Clock(), backend=RunningBackend(running={"llamacpp"}))
    broker.restore_state()
    assert sched.running_ids == [] and sched.evicted_residents == {}
    assert sched.idle_cached == {"llamacpp": 25.0}
    assert broker.backend.stopped == []


def test_corrupt_state_with_docker_unreadable_assumes_the_worst(tmp_path):
    path = tmp_path / "scheduler-state.json"
    path.write_text("{truncated", encoding="utf-8")
    sched, broker = _controller(path, Clock(), backend=RunningBackend(fail=True))
    broker.restore_state()
    assert sched.evicted_residents == {"llamacpp": 25.0}
    assert sched.running_ids == [RECOVERY_JOB_ID]
    assert broker.backend.started == []
