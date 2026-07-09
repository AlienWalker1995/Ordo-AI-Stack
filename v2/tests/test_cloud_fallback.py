"""A too-big job must not starve smaller ones; cloud_fallback routes it, else it's rejected."""
from ordo.scheduler import Job, Scheduler


def test_too_big_job_does_not_starve_smaller_jobs():
    # Regression: a head that can never fit used to `break` and block everything behind it forever.
    s = Scheduler(32)
    s.submit(Job("huge", 100, "media"))       # 100GB on a 32GB card — impossible
    s.submit(Job("chat", 4, "chat"))          # small, queued behind it
    admitted, _ = s.pump()
    assert admitted == ["chat"]               # the small job runs; it is NOT starved
    assert "huge" not in s.running_ids


def test_reject_when_no_cloud_fallback():
    s = Scheduler(32, cloud_fallback=False)
    s.submit(Job("huge", 100, "media"))
    s.pump()
    st = s.status()
    assert st["rejected"] == ["huge"] and st["cloud_routed"] == []


def test_route_to_cloud_when_enabled():
    s = Scheduler(32, cloud_fallback=True)
    s.submit(Job("huge", 100, "media"))
    s.pump()
    st = s.status()
    assert st["cloud_routed"] == ["huge"] and st["rejected"] == []


def test_drain_cloud_routed_is_once_only():
    s = Scheduler(32, cloud_fallback=True)
    s.submit(Job("a", 100))
    s.submit(Job("b", 200))
    s.pump()
    assert set(s.drain_cloud_routed()) == {"a", "b"}
    assert s.drain_cloud_routed() == []        # drained -> the agent won't double-dispatch
    assert s.status()["cloud_routed"] == []


def test_broker_does_not_start_cloud_routed_jobs():
    from ordo.broker import Broker, MockBackend
    s = Scheduler(32, cloud_fallback=True)
    b = Broker(s, MockBackend())
    b.request(Job("huge", 100, "media"))       # too big -> routed, never started locally
    b.request(Job("chat", 4, "chat"))
    assert b.backend.started == ["chat"]        # only the fitting job hit the backend
    assert s.status()["cloud_routed"] == ["huge"]
