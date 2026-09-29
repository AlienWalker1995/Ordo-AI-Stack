"""The control plane keeps answering while a long operation runs.

ops-controller serves one uvicorn worker. A recreate can wait up to 900 s on docker, and when that
call ran on the event loop, /health, /status and the GPU-lease heartbeats of every other client
stalled behind it: a lease holder could miss its heartbeat and the dashboard looked dead. These
tests drive the real ASGI app with a backend whose operation blocks until the test releases it.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
import yaml

from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Job, Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
TOKEN = "concurrency-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CONFIRM = {"confirm": True}

# How long the blocked operation waits before giving up on its own, so a regression fails the test
# instead of hanging it.
BLOCK_SECONDS = 5.0


class BlockingBackend(MockBackend):
    """A MockBackend whose recreate blocks until `release` is set (or BLOCK_SECONDS pass)."""

    def __init__(self) -> None:
        super().__init__()
        self.compose_doc = {"services": {"slow": {"image": "busybox"}, "llamacpp": {"image": "llama"}}}
        self.entered = threading.Event()
        self.release = threading.Event()
        self.events: list[str] = []

    def recreate_service(self, service: str) -> None:
        self.events.append(f"recreate {service} begins")
        self.entered.set()
        self.release.wait(BLOCK_SECONDS)
        self.events.append(f"recreate {service} ends")
        super().recreate_service(service)

    def stop(self, service: str) -> None:
        self.events.append(f"stop {service}")
        super().stop(service)


def _control_plane(tmp_path) -> tuple[ControlPlane, BlockingBackend, Scheduler]:
    source = tmp_path / "ordo.yaml"
    source.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}))
    scheduler = Scheduler(32)
    scheduler.cache_idle("llamacpp", 20)
    backend = BlockingBackend()
    broker = Broker(scheduler, backend)
    cp = ControlPlane(source, CATALOG, REGISTRY, tmp_path / "out", scheduler=scheduler, broker=broker)
    return cp, backend, scheduler


async def _wait_until_entered(backend: BlockingBackend) -> None:
    for _ in range(int(BLOCK_SECONDS * 100)):
        if backend.entered.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the slow operation never started")


def _client(cp: ControlPlane) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=cp.app(auth_token=TOKEN)),
                             base_url="http://ops-controller", headers=AUTH, timeout=BLOCK_SECONDS * 3)


def _recreate_still_running(backend: BlockingBackend) -> bool:
    return backend.entered.is_set() and "recreate slow ends" not in backend.events


def test_health_status_and_heartbeat_answer_while_a_recreate_runs(tmp_path):
    cp, backend, scheduler = _control_plane(tmp_path)
    # A lease holder that must keep heartbeating through someone else's long operation.
    scheduler.submit(Job("gate-comfyui", vram_gb=4.0, kind="media"))
    scheduler.pump()

    async def scenario():
        answered_during_recreate = {}
        async with _client(cp) as client:
            slow = asyncio.create_task(client.post("/services/slow/recreate", json=CONFIRM))
            await _wait_until_entered(backend)
            try:
                for name, call in (("health", lambda: client.get("/health")),
                                   ("status", lambda: client.get("/status")),
                                   ("heartbeat", lambda: client.post("/jobs/heartbeat",
                                                                     json={"id": "gate-comfyui"}))):
                    response = await call()
                    answered_during_recreate[name] = (response.status_code, _recreate_still_running(backend))
            finally:
                backend.release.set()
            slow_response = await slow
        return answered_during_recreate, slow_response

    answered, slow_response = asyncio.run(scenario())

    # Each fast call answered 200 while the recreate was still blocked on docker, not after it.
    assert answered == {"health": (200, True), "status": (200, True), "heartbeat": (200, True)}
    assert slow_response.status_code == 200
    assert backend.recreate_calls == ["slow"]


def test_a_second_stack_verb_is_refused_with_409_while_one_runs(tmp_path):
    cp, backend, _ = _control_plane(tmp_path)

    async def scenario():
        async with _client(cp) as client:
            slow = asyncio.create_task(client.post("/services/slow/recreate", json=CONFIRM))
            await _wait_until_entered(backend)
            try:
                second = await client.post("/services/llamacpp/restart", json=CONFIRM)
                still_running = _recreate_still_running(backend)
            finally:
                backend.release.set()
            return second, still_running, await slow

    second, still_running, first = asyncio.run(scenario())

    assert still_running
    assert second.status_code == 409
    assert "in progress" in second.json()["error"]
    assert backend.restarted == []  # the refused verb did nothing
    assert first.status_code == 200


def test_a_lease_request_waits_for_a_running_verb_before_evicting(tmp_path):
    # llama.cpp holds 20 of 32 GB, so a 16 GB lease evicts it. Evicting it in the middle of a verb
    # that is (re)starting services would race compose; the lease waits for the verb instead.
    cp, backend, scheduler = _control_plane(tmp_path)

    async def scenario():
        async with _client(cp) as client:
            slow = asyncio.create_task(client.post("/services/slow/recreate", json=CONFIRM))
            await _wait_until_entered(backend)
            lease = asyncio.create_task(client.post("/jobs", json={"id": "gate-comfyui", "vram_gb": 16}))
            try:
                await asyncio.sleep(0.3)
                evicted_mid_verb = "stop llamacpp" in backend.events
            finally:
                backend.release.set()
            return evicted_mid_verb, await slow, await lease

    evicted_mid_verb, verb, lease = asyncio.run(scenario())

    assert not evicted_mid_verb
    assert verb.status_code == 200 and lease.status_code == 200
    assert backend.events == ["recreate slow begins", "recreate slow ends", "stop llamacpp"]
    assert scheduler.running_ids == ["gate-comfyui"]


def test_the_lease_sweep_skips_a_tick_while_a_verb_holds_the_operation_lock(tmp_path):
    cp, backend, scheduler = _control_plane(tmp_path)
    broker = cp.broker
    scheduler.submit(Job("stranded", vram_gb=16.0, est_seconds=1.0))
    broker.reconcile()
    scheduler.tick(10.0)  # the lease has expired

    with broker.operation_lock:
        swept_during_verb = _in_other_thread(broker.sweep_leases)
    assert swept_during_verb == []
    assert scheduler.running_ids == ["stranded"]

    assert broker.sweep_leases() == ["stranded"]
    assert "llamacpp" in backend.started  # restored once the lease was swept


def _in_other_thread(call):
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("value", call()))
    worker.start()
    worker.join(BLOCK_SECONDS)
    return result["value"]


def test_enforce_evictions_never_stops_a_resident_restored_while_it_looked(tmp_path):
    # The race: enforce_evictions read llama.cpp as evicted, a lease completing on another thread
    # restored (started) it, and the container listing then showed it running. Stopping it there
    # strands it down with nothing left to restore it.
    scheduler = Scheduler(32)
    scheduler.cache_idle("llamacpp", 20)
    scheduler.submit(Job("render", vram_gb=16.0))
    scheduler.pump()  # evicts llamacpp
    scheduler.complete("render")

    class RestoreDuringListing(MockBackend):
        def list_services(self) -> dict:
            scheduler.take_restorable()  # the concurrent restore lands while docker is listed
            return {"services": [{"id": "llamacpp", "state": "running"}]}

    backend = RestoreDuringListing()
    broker = Broker(scheduler, backend)

    assert broker.enforce_evictions() == []
    assert backend.stopped == []


def test_two_concurrent_downloads_cannot_both_start(tmp_path, monkeypatch):
    cp, _, _ = _control_plane(tmp_path)
    monkeypatch.setattr(cp, "_run_model_download", lambda *args: None)  # no network in tests
    body = {"url": "https://huggingface.co/org/repo/resolve/main/model.safetensors", "category": "checkpoints"}

    first = cp.models_download(dict(body))
    second = cp.models_download(dict(body))

    assert first["status"] == "started"
    assert second["_status"] == 409
