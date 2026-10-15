"""ops-controller restarts an idle ComfyUI that holds a lot of RAM (ordo/control/comfyui_reclaim.py).

It must restart only between leases, after the GPU has been quiet for a while, when ComfyUI is over
the threshold and its own queue is empty, with the last checks and the restart under the broker's
operation lock. Anything it cannot read counts as busy.
"""
from __future__ import annotations

import threading

from ordo.control import comfyui_reclaim as reclaim
from ordo.control.comfyui_reclaim import ComfyUIReclaim


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeScheduler:
    def __init__(self) -> None:
        self.leased = False

    def status(self) -> dict:
        return {"leased": self.leased}


class FakeBackend:
    def __init__(self) -> None:
        self.stats: dict | Exception = {"services": {"comfyui": {"running": True, "mem_gb": 20.0}}}
        self.queue: tuple[int, str] | Exception = (0, "0\n")
        self.probes = 0

    def service_stats(self) -> dict:
        if isinstance(self.stats, Exception):
            raise self.stats
        return self.stats

    def exec_in_service(self, service: str, command: list[str]) -> tuple[int, str]:
        assert service == "comfyui" and command == reclaim.QUEUE_PROBE
        self.probes += 1
        if isinstance(self.queue, Exception):
            raise self.queue
        return self.queue


class FakeBroker:
    def __init__(self) -> None:
        self.scheduler = FakeScheduler()
        self.backend = FakeBackend()
        self.operation_lock = threading.RLock()


def make(result: dict | None = None):
    broker, clock, restarts = FakeBroker(), Clock(), []

    def restart() -> dict:
        restarts.append(broker.operation_lock._is_owned())  # held during the restart
        return result or {"ok": True, "service": "comfyui", "action": "restarted"}

    sweeper = ComfyUIReclaim(broker, restart, clock=clock)
    clock.now += reclaim.QUIET_SECONDS  # quiet since the process started
    return sweeper, broker, clock, restarts


def test_restarts_an_idle_comfyui_over_the_threshold_under_the_operation_lock():
    sweeper, _broker, _clock, restarts = make()
    line = sweeper.sweep()
    assert restarts == [True]
    assert line == "restarted ComfyUI to release the 20.0 GiB it held while idle"
    assert sweeper.reclaims == 1


def test_waits_a_quiet_period_after_a_restart_before_the_next_one():
    sweeper, _broker, clock, restarts = make()
    sweeper.sweep()
    clock.now += reclaim.QUIET_SECONDS - 1
    assert sweeper.sweep() is None
    clock.now += 1
    sweeper.sweep()
    assert len(restarts) == 2


def test_never_while_the_gpu_is_leased_and_waits_a_quiet_period_after_the_lease():
    sweeper, broker, clock, restarts = make()
    broker.scheduler.leased = True
    assert sweeper.sweep() is None
    broker.scheduler.leased = False
    clock.now += reclaim.QUIET_SECONDS - 1
    assert sweeper.sweep() is None
    assert restarts == []
    clock.now += 1
    sweeper.sweep()
    assert restarts == [True]


def test_a_fresh_process_waits_a_quiet_period_first():
    broker, clock, restarts = FakeBroker(), Clock(), []
    sweeper = ComfyUIReclaim(broker, lambda: restarts.append(1) or {"ok": True}, clock=clock)
    assert sweeper.sweep() is None
    assert restarts == []


def test_below_the_threshold_it_does_nothing_and_does_not_probe_the_queue():
    sweeper, broker, _clock, restarts = make()
    broker.backend.stats = {"services": {"comfyui": {"running": True, "mem_gb": reclaim.RECLAIM_MIN_GB - 0.1}}}
    assert sweeper.sweep() is None
    assert restarts == [] and broker.backend.probes == 0


def test_unreadable_or_absent_comfyui_counts_as_busy():
    for stats in (RuntimeError("docker stats failed"), {"services": {}},
                  {"services": {"comfyui": {"running": False, "mem_gb": 30.0}}},
                  {"services": {"comfyui": {"running": True}}}):
        sweeper, broker, _clock, restarts = make()
        broker.backend.stats = stats
        assert sweeper.sweep() is None
        assert restarts == []


def test_a_busy_or_unreadable_queue_blocks_the_restart():
    for queue in ((0, "1\n"), (1, ""), (0, "not a number"), FileNotFoundError("comfyui")):
        sweeper, broker, _clock, restarts = make()
        broker.backend.queue = queue
        assert sweeper.sweep() is None
        assert restarts == []


def test_a_lease_admitted_after_the_first_check_is_seen_under_the_lock():
    sweeper, broker, _clock, restarts = make()
    real_stats = broker.backend.service_stats

    def stats_then_lease() -> dict:
        broker.scheduler.leased = True  # a lease lands while docker stats runs
        return real_stats()

    broker.backend.service_stats = stats_then_lease
    assert sweeper.sweep() is None
    assert restarts == []


def test_skips_while_another_operation_holds_the_lock():
    sweeper, broker, _clock, restarts = make()
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with broker.operation_lock:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    try:
        assert sweeper.sweep() is None
    finally:
        release.set()
        thread.join()
    assert restarts == []


def test_a_refused_restart_is_reported_and_not_counted():
    sweeper, _broker, _clock, restarts = make({"error": "comfyui holds a GPU lease"})
    line = sweeper.sweep()
    assert restarts == [True]
    assert line == "ComfyUI held 20.0 GiB while idle, but its restart failed: comfyui holds a GPU lease"
    assert sweeper.reclaims == 0
