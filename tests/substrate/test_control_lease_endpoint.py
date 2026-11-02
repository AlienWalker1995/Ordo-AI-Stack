"""GET /lease and the cached chat probe behind it (ordo/control/chat_probe.py).

The model gateway's fallback gate reads /lease with a 2 s timeout while GPU leases move, so the
endpoint answers from memory: the scheduler's evicted residents and the probe's last answer.
"""
from __future__ import annotations

import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from ordo.control.api import UNAUTHENTICATED_PATHS, ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.chat_probe import ChatHealthProbe
from ordo.control.scheduler import Job, Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
TOKEN = "lease-token-41aa"
CHAT_URL = "http://inference-engine-gpu:8080/health"


def _plane(tmp_path, sched: Scheduler, chat_health_url: str | None = CHAT_URL) -> ControlPlane:
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}))
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=sched,
                        broker=Broker(sched, MockBackend()), chat_health_url=chat_health_url)


def _evicting_scheduler() -> Scheduler:
    sched = Scheduler(32)
    sched.cache_idle("inference-engine-gpu", 27.5)
    sched.submit(Job("gate-comfyui", 30.0, "media", est_seconds=600))
    sched.pump()
    return sched


def _probe_answering(plane: ControlPlane, up: bool) -> None:
    plane.chat_probe._fetch = lambda url, timeout: up
    plane.chat_probe.refresh()


def test_lease_is_open_without_a_token(tmp_path):
    plane = _plane(tmp_path, Scheduler(32))
    _probe_answering(plane, True)
    client = TestClient(plane.app(auth_token=TOKEN), raise_server_exceptions=False)
    response = client.get("/lease")
    assert response.status_code == 200
    assert "/lease" in UNAUTHENTICATED_PATHS
    # Only the read is opened: everything else still needs the bearer token.
    assert client.get("/status").status_code == 401


def test_not_evicted_and_chat_up(tmp_path):
    plane = _plane(tmp_path, Scheduler(32))
    _probe_answering(plane, True)
    assert plane.lease_state() == {"evicted_residents": [], "gpu_chat_up": True}


def test_evicted_resident_is_listed(tmp_path):
    plane = _plane(tmp_path, _evicting_scheduler())
    _probe_answering(plane, False)
    assert plane.lease_state() == {"evicted_residents": ["inference-engine-gpu"], "gpu_chat_up": False}


def test_chat_up_is_null_before_the_first_probe_and_when_unconfigured(tmp_path):
    assert _plane(tmp_path, Scheduler(32)).lease_state()["gpu_chat_up"] is None
    assert _plane(tmp_path, Scheduler(32), chat_health_url=None).lease_state()["gpu_chat_up"] is None


def test_lease_served_over_http_matches_the_state(tmp_path):
    plane = _plane(tmp_path, _evicting_scheduler())
    _probe_answering(plane, True)
    body = TestClient(plane.app(auth_token=TOKEN)).get("/lease").json()
    assert body == {"evicted_residents": ["inference-engine-gpu"], "gpu_chat_up": True}


def test_lease_answers_in_milliseconds_with_a_hung_chat_engine(tmp_path):
    """The endpoint never waits on the engine: a probe blocked for seconds does not slow it."""
    plane = _plane(tmp_path, _evicting_scheduler())
    _probe_answering(plane, False)
    plane.chat_probe._fetch = lambda url, timeout: time.sleep(30) or True  # never called by a read
    client = TestClient(plane.app(auth_token=TOKEN))
    client.get("/lease")  # warm the app
    timings = []
    for _ in range(50):
        started = time.perf_counter()
        assert client.get("/lease").status_code == 200
        timings.append(time.perf_counter() - started)
    timings.sort()
    median_ms, worst_ms = timings[25] * 1000, timings[-1] * 1000
    print(f"GET /lease latency over 50 calls: median {median_ms:.2f} ms, max {worst_ms:.2f} ms")
    assert worst_ms < 250


# --- the background probe ---

class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_probe_is_none_when_unconfigured_and_never_fetches():
    def boom(url, timeout):
        raise AssertionError("an unconfigured probe must not fetch")

    probe = ChatHealthProbe(None, fetch=boom)
    probe.refresh()
    assert probe.configured is False
    assert probe.up() is None


def test_probe_reports_the_last_answer_and_goes_stale():
    clock = _Clock()
    answers = iter([True, False])
    probe = ChatHealthProbe(CHAT_URL, interval=3.0, fetch=lambda url, timeout: next(answers), clock=clock)
    assert probe.up() is None            # nothing probed yet
    probe.refresh()
    assert probe.up() is True
    clock.now += 10                      # within 4 intervals
    assert probe.up() is True
    probe.refresh()
    assert probe.up() is False
    clock.now += 13                      # past 4 intervals: the probe thread is gone, so unknown
    assert probe.up() is None


def test_probe_passes_its_short_timeout_to_the_fetch():
    seen = []
    probe = ChatHealthProbe(CHAT_URL, timeout=1.5, fetch=lambda url, timeout: seen.append((url, timeout)) or True)
    probe.refresh()
    assert seen == [(CHAT_URL, 1.5)]


def test_an_unreachable_engine_is_down_not_an_error():
    probe = ChatHealthProbe("http://127.0.0.1:9/health", timeout=0.5)
    probe.refresh()
    assert probe.up() is False
