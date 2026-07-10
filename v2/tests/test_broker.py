"""Broker reconciles scheduler decisions into container start/stop; Docker backend is scoped."""
from ordo.broker import Broker, DockerBackend, MockBackend
from ordo.scheduler import Job, Scheduler


def _broker(vram=32):
    return Broker(Scheduler(vram), MockBackend())


def test_starts_admitted_job():
    b = _broker()
    b.request(Job("media", 20, "media"))
    assert b.backend.started == ["media"]


def test_co_run_starts_both():
    b = _broker()
    b.request(Job("media", 20, "media"))
    b.request(Job("chat", 4, "chat"))
    assert b.backend.started == ["media", "chat"]     # chat slips beside media


def test_queue_then_start_on_completion():
    b = _broker()
    b.request(Job("m1", 20, "media"))
    b.request(Job("m2", 20, "media"))
    assert b.backend.started == ["m1"]                # m2 didn't fit → not started
    b.complete("m1")
    assert "m1" in b.backend.stopped
    assert b.backend.started == ["m1", "m2"]          # completing m1 admits m2


def test_evicted_idle_model_is_stopped():
    s = Scheduler(32)
    s.cache_idle("old_model", 24)                     # free = 8; chat needs 12 → must evict
    b = Broker(s, MockBackend())
    b.request(Job("chat", 12, "chat"))
    assert "old_model" in b.backend.stopped
    assert "chat" in b.backend.started


def test_docker_backend_scopes_to_its_project_via_compose_service_name():
    d = DockerBackend(project="ordo-v2")
    # a bare compose service name passes through — the container is resolved by the compose-project
    # LABEL filter (in _resolve), which structurally can only match containers in this project.
    assert d._guard("llamacpp") == "llamacpp"
    assert d._guard("codebase-memory-ui") == "codebase-memory-ui"        # dashes in a service name are fine
    # a fully-qualified container name for THIS project is normalized back to the bare service
    # (strips the project prefix AND the -N replica suffix) so the label filter matches exactly.
    assert d._guard("ordo-v2-llamacpp-1") == "llamacpp"
    assert d._guard("ordo-v2-agent") == "agent"
    # anything shaped like a raw path / not a bare service name is refused (belt-and-braces)
    import pytest
    for bad in ("../etc", "a/b", " spaced ", ""):
        with pytest.raises(ValueError):
            d._guard(bad)


# ── media-lease: the broker STOPS the resident for the lease and RESTARTS it on completion ───────

def _broker_with_resident(total=32, resident_gb=25):
    s = Scheduler(total)
    s.cache_idle("llamacpp", resident_gb)
    return Broker(s, MockBackend())


def test_media_lease_stops_resident_then_restarts_it_on_completion():
    b = _broker_with_resident()
    b.request(Job("reel", 18, "media"))                 # evicts llamacpp -> stop; starts reel
    assert "llamacpp" in b.backend.stopped
    assert "reel" in b.backend.started
    b.backend.started.clear()                            # focus on what completion does
    b.complete("reel")                                   # media done -> restore the resident
    assert "reel" in b.backend.stopped                   # the job container is stopped
    assert b.backend.started == ["llamacpp"]             # resident RESTARTED by the broker


def test_no_resident_restart_between_back_to_back_renders():
    b = _broker_with_resident()
    b.request(Job("reel1", 18, "media"))                # reel1 runs (llamacpp stopped)
    b.request(Job("reel2", 18, "media"))                # reel2 queued
    b.backend.started.clear()
    b.complete("reel1")                                  # reel2 admitted — resident NOT restarted
    assert "reel2" in b.backend.started
    assert "llamacpp" not in b.backend.started           # anti-thrash: still down for reel2
    b.backend.started.clear()
    b.complete("reel2")
    assert b.backend.started == ["llamacpp"]             # only after the queue fully drains


def test_sweep_leases_restarts_resident_for_a_stranded_job():
    s = Scheduler(32)
    s.cache_idle("llamacpp", 25)
    b = Broker(s, MockBackend())
    b.request(Job("reel", 18, "media", est_seconds=30))  # TTL = 60s
    b.backend.started.clear()
    s.tick(70)                                           # past the 60s TTL (serve loop ticks the clock)
    swept = b.sweep_leases()
    assert swept == ["reel"]
    assert "reel" in b.backend.stopped                   # stranded job's container stopped
    assert b.backend.started == ["llamacpp"]             # resident self-healed back up


def test_sweep_is_noop_when_no_lease_expired():
    b = _broker_with_resident()
    b.request(Job("reel", 18, "media", est_seconds=600))
    b.backend.started.clear()
    b.backend.stopped.clear()
    b.scheduler.tick(5)
    assert b.sweep_leases() == []
    assert b.backend.started == [] and b.backend.stopped == []


class _ResolvingBackend(MockBackend):
    """Mimics DockerBackend: start/stop are a NO-OP for a name that isn't a known container (an
    abstract lease job), and act only on names that resolve. This locks in the live-caught defect:
    an external media lease (`lease-test`) has no container of its own, so the broker must NOT try
    (and fail) to `docker start/stop` it — only the real resident (`llamacpp`) is a container."""
    def __init__(self, real_containers: set[str]):
        super().__init__()
        self._real = real_containers

    def start(self, name: str) -> None:
        if name in self._real:
            super().start(name)

    def stop(self, name: str) -> None:
        if name in self._real:
            super().stop(name)


def test_abstract_lease_job_never_touched_only_resident_is():
    # the exact live scenario: a media LEASE reserves VRAM (evicting the resident) but owns no
    # container; only the resident is stopped/started by the broker.
    s = Scheduler(32)
    s.cache_idle("llamacpp", 25)
    b = Broker(s, _ResolvingBackend(real_containers={"llamacpp"}))
    b.request(Job("lease-test", 18, "media"))            # evicts llamacpp; lease-test has no container
    assert b.backend.stopped == ["llamacpp"]             # resident stopped — NOT lease-test
    assert b.backend.started == []                        # abstract lease started nothing
    b.complete("lease-test")                              # release the lease
    assert b.backend.started == ["llamacpp"]             # resident restarted; lease-test never touched
    assert "lease-test" not in b.backend.stopped and "lease-test" not in b.backend.started
