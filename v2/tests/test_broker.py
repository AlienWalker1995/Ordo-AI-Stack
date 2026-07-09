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


def test_docker_backend_cannot_touch_live_stack():
    d = DockerBackend(project="ordo-v2")
    # any name — even a live container — is forced under the v2 project prefix
    assert d._guard("comfyui") == "ordo-v2-comfyui"
    assert d._guard("ordo-ai-stack-llamacpp-1").startswith("ordo-v2-")
    assert d._guard("ordo-v2-agent") == "ordo-v2-agent"
