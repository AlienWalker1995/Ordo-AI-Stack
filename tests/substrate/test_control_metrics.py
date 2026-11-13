"""ops-controller's GET /metrics: the series the alert rules read (monitoring/prometheus/rules/).

Before this, nothing exported the GPU lease, the containers' health and restart counts, the disks or
the edge certificate, so none of the incidents they would have caught (a Docker disk at 100%,
llama.cpp left evicted after a stale lease, a crash loop, an expired certificate) could alert.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control import metrics
from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Job, Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
ADMIN_TOKEN = "admin-token-5e21"
RULES = ROOT / "monitoring" / "prometheus" / "rules" / "ordo-alerts.yml"
SAMPLE = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>\S+)$')


def _samples(text: str) -> dict[tuple[str, tuple], float]:
    """{(name, sorted label pairs): value} for every sample line, failing on a malformed one."""
    found = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = SAMPLE.match(line)
        assert m, f"not an exposition sample: {line!r}"
        labels = tuple(sorted(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m.group("labels") or "")))
        found[(m.group("name"), labels)] = float(m.group("value"))
    return found


def _leased_scheduler() -> Scheduler:
    """inference-engine-gpu evicted by a running media lease, inference-engine-embed still resident, one job queued."""
    sched = Scheduler(32)
    sched.cache_idle("inference-engine-gpu", 27.5)
    sched.cache_idle("inference-engine-embed", 1.0)
    sched.submit(Job("gate-comfyui", 30.0, "media", est_seconds=600))
    sched.pump()
    sched.submit(Job("queued-render", 30.0, "media"))
    sched.tick(120)
    return sched


def test_the_lease_state_is_exported():
    text = metrics.render(metrics.Inputs(scheduler=_leased_scheduler().status()))
    got = _samples(text)
    assert got[("ordo_gpu_leases_running", ())] == 1
    assert got[("ordo_gpu_leases_queued", ())] == 1
    lease = (("kind", "media"), ("lease", "gate-comfyui"))
    assert got[("ordo_gpu_lease_held_seconds", lease)] == 120
    assert got[("ordo_gpu_lease_ttl_remaining_seconds", lease)] == 1080   # 2 x 600s estimate - 120s held
    assert got[("ordo_gpu_resident_evicted", (("resident", "inference-engine-gpu"),))] == 1
    assert got[("ordo_gpu_resident_evicted", (("resident", "inference-engine-embed"),))] == 0


def test_a_lease_past_its_ttl_reads_zero_remaining():
    sched = Scheduler(32)
    sched.submit(Job("stranded", 4.0, "media", est_seconds=10))
    sched.pump()
    sched.tick(60)   # past the 20s TTL, and no sweep has run
    got = _samples(metrics.render(metrics.Inputs(scheduler=sched.status())))
    assert got[("ordo_gpu_lease_ttl_remaining_seconds", (("kind", "media"), ("lease", "stranded")))] == 0


def test_an_idle_card_still_declares_every_family():
    """No lease is a fact, not a missing source: the families are present with their headers."""
    text = metrics.render(metrics.Inputs(scheduler=Scheduler(32).status()))
    assert "# TYPE ordo_gpu_lease_ttl_remaining_seconds gauge" in text
    assert _samples(text)[("ordo_gpu_leases_running", ())] == 0


def test_containers_restarts_disks_and_certs():
    rows = [{"id": "inference-engine-gpu", "state": "running", "health": "healthy"},
            {"id": "mcp-comfyui", "state": "restarting", "health": None},
            {"id": "gpu-exporter", "state": "running", "health": "unhealthy"},
            {"id": "couchdb", "state": "exited", "health": None, "status": "Exited (1) 3 minutes ago"},
            {"id": "model-gateway-keys", "state": "exited", "health": None, "status": "Exited (0) 2 days ago"}]
    text = metrics.render(metrics.Inputs(
        containers=rows, restarts={"mcp-comfyui": 7, "inference-engine-gpu": 0},
        disks={"docker": metrics.DiskUsage(size=1_081_101_176_832, free=160_000_000_000, avail=141_733_474_304)},
        tls_certs={"edge": 1_797_981_483.0}))
    got = _samples(text)
    assert got[("ordo_container_running", (("service", "inference-engine-gpu"),))] == 1
    assert got[("ordo_container_running", (("service", "mcp-comfyui"),))] == 0
    assert got[("ordo_container_unhealthy", (("service", "gpu-exporter"),))] == 1
    assert got[("ordo_container_unhealthy", (("service", "inference-engine-gpu"),))] == 0
    assert got[("ordo_container_restarts_total", (("service", "mcp-comfyui"),))] == 7
    assert "# TYPE ordo_container_restarts_total counter" in text
    # A crash is an error; a one-shot that finished (exit 0) and a running service are not.
    assert got[("ordo_container_exited_with_error", (("service", "couchdb"),))] == 1
    assert got[("ordo_container_exited_with_error", (("service", "model-gateway-keys"),))] == 0
    assert got[("ordo_container_exited_with_error", (("service", "inference-engine-gpu"),))] == 0
    # Byte counts past 1e12 are written exactly, never in rounded scientific notation.
    assert 'ordo_filesystem_size_bytes{mount="docker"} 1081101176832' in text
    assert got[("ordo_filesystem_avail_bytes", (("mount", "docker"),))] == 141_733_474_304
    assert got[("ordo_tls_cert_not_after_timestamp_seconds", (("cert", "edge"),))] == 1_797_981_483
    for collector in ("containers", "restarts", "disk_docker", "tls_cert_edge"):
        assert got[("ordo_metrics_collector_ok", (("collector", collector),))] == 1


def test_an_unreadable_source_is_a_failed_collector_never_a_zero():
    """A docker that did not answer must not read as "nothing running and nothing restarting"."""
    text = metrics.render(metrics.Inputs(containers=None, restarts=None,
                                         disks={"host": None}, tls_certs={"edge": None}))
    got = _samples(text)
    for collector in ("containers", "restarts", "disk_host", "tls_cert_edge"):
        assert got[("ordo_metrics_collector_ok", (("collector", collector),))] == 0
    assert not any(name.startswith(("ordo_container_", "ordo_filesystem_", "ordo_tls_")) for name, _ in got)


def test_label_values_are_escaped():
    text = metrics.render(metrics.Inputs(containers=[{"id": 'we"ird\\name', "state": "running"}]))
    assert 'ordo_container_running{service="we\\"ird\\\\name"} 1' in text


def test_every_ordo_series_the_rules_read_is_exported():
    """A renamed metric would leave its rule silently evaluating to nothing."""
    exported = set(re.findall(r"^# TYPE (ordo_\w+)", metrics.render(metrics.Inputs(
        scheduler=_leased_scheduler().status(), containers=[], restarts={}, immutable_changes={},
        disks={"docker": metrics.DiskUsage(1, 1, 1)}, tls_certs={"edge": 1.0}, gpu_chat_up=True,
        netns_repair={"repaired": 0, "failed": 0, "orphans": 0})),
        re.MULTILINE))
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    used = {name for group in rules["groups"] for rule in group["rules"]
            for name in re.findall(r"\bordo_\w+", rule["expr"])}
    assert used and used <= exported, f"rules read series /metrics does not export: {sorted(used - exported)}"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_a_certificate_expiry_is_read_from_the_pem(tmp_path):
    cert, key = tmp_path / "tailnet.crt", tmp_path / "tailnet.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "10",
                    "-subj", "/CN=test", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True, timeout=60)
    end = subprocess.run(["openssl", "x509", "-enddate", "-noout", "-in", str(cert)],
                         check=True, capture_output=True, text=True).stdout
    import ssl
    assert metrics.read_cert_not_after(str(cert)) == ssl.cert_time_to_seconds(end.strip().removeprefix("notAfter="))


def test_a_missing_certificate_raises_for_the_collector(tmp_path):
    with pytest.raises((OSError, ValueError)):
        metrics.read_cert_not_after(str(tmp_path / "absent.crt"))


# --------------------------------------------------------------------------- #
# The route.
# --------------------------------------------------------------------------- #


@pytest.fixture
def plane(tmp_path, monkeypatch):
    monkeypatch.setattr("ordo.control.api.AUDIT_LOG_PATH", tmp_path / "data" / "audit.log")
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                                   "model": "auto", "plugins": "auto"}), encoding="utf-8")
    sched = _leased_scheduler()
    backend = MockBackend({"services": {"inference-engine-gpu": {"image": "example/inference-engine-gpu"},
                                        "mcp-comfyui": {"image": "example/mcp"}}})
    backend.project_containers["mcp-comfyui"].restart_count = 3
    usage = {"/": metrics.DiskUsage(100, 10, 5), str(tmp_path / "out"): metrics.DiskUsage(200, 100, 90)}
    monkeypatch.setattr(metrics.DiskUsage, "of", classmethod(lambda cls, path: usage[path]))
    monkeypatch.setattr(metrics, "read_cert_not_after", lambda path: 1_797_981_483.0)
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=sched, broker=Broker(sched, backend),
                        disk_paths={"docker": "/", "host": str(tmp_path / "out")},
                        tls_cert_files={"edge": "/edge-certs/tailnet.crt"})


def test_metrics_text_gathers_every_source(plane):
    got = _samples(plane.metrics_text())
    assert got[("ordo_gpu_resident_evicted", (("resident", "inference-engine-gpu"),))] == 1
    assert got[("ordo_container_restarts_total", (("service", "mcp-comfyui"),))] == 3
    assert got[("ordo_container_running", (("service", "inference-engine-gpu"),))] == 1
    assert got[("ordo_filesystem_size_bytes", (("mount", "host"),))] == 200
    assert got[("ordo_filesystem_free_bytes", (("mount", "docker"),))] == 10
    assert got[("ordo_tls_cert_not_after_timestamp_seconds", (("cert", "edge"),))] == 1_797_981_483


def test_immutable_file_changes_are_exported_per_declaring_service():
    got = _samples(metrics.render(metrics.Inputs(immutable_changes={"agent": 3})))
    assert got[("ordo_container_immutable_files_changed", (("service", "agent"),))] == 3
    assert got[("ordo_metrics_collector_ok", (("collector", "immutable_paths"),))] == 1


def test_unreadable_immutable_changes_are_a_failed_collector_never_a_zero():
    text = metrics.render(metrics.Inputs(immutable_changes=None, containers=[]))
    assert "ordo_container_immutable_files_changed{" not in text
    assert _samples(text)[("ordo_metrics_collector_ok", (("collector", "immutable_paths"),))] == 0


def test_metrics_reads_the_hand_edited_files_of_a_declaring_container(plane):
    container = plane.broker.backend.project_containers["mcp-comfyui"]
    container.immutable_roots = ("/opt/app",)
    container.immutable_changes = ["/opt/app/a.py", "/opt/app/b.py"]
    plane.metrics.refresh_immutable_changes()
    got = _samples(plane.metrics_text())
    assert got[("ordo_container_immutable_files_changed", (("service", "mcp-comfyui"),))] == 2
    assert ("ordo_container_immutable_files_changed", (("service", "inference-engine-gpu"),)) not in got


def test_a_scrape_never_runs_docker_diff(plane, monkeypatch):
    """`docker diff` of the agent took 44 s on 2026-11-08. Read inline, it pushed every scrape past
    Prometheus's 30 s timeout and took the whole ops-controller target, and so every container alert,
    down. A scrape reads the reader thread's last reading from memory and nothing else."""
    monkeypatch.setattr(plane.broker.backend, "running_container_ids", lambda: {"agent": "id-1"})
    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", lambda: {"agent": 2})
    plane.metrics.refresh_immutable_changes()

    def diff_during_scrape():
        raise AssertionError("a scrape ran docker diff")

    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", diff_during_scrape)
    key = ("ordo_container_immutable_files_changed", (("service", "agent"),))
    assert _samples(plane.metrics_text())[key] == 2


def test_no_reading_yet_or_a_failed_one_is_a_failed_collector_never_a_zero(plane, monkeypatch):
    collector_ok = ("ordo_metrics_collector_ok", (("collector", "immutable_paths"),))
    assert _samples(plane.metrics_text())[collector_ok] == 0      # the thread has not read yet
    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", lambda: {"agent": 1})
    plane.metrics.refresh_immutable_changes()
    assert _samples(plane.metrics_text())[collector_ok] == 1

    def timed_out():
        raise TimeoutError("docker diff timed out after 180 seconds")

    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", timed_out)
    plane.metrics.refresh_immutable_changes()
    got = _samples(plane.metrics_text())
    assert got[collector_ok] == 0, "a failed reading clears the last count instead of serving it as current"
    assert ("ordo_container_immutable_files_changed", (("service", "agent"),)) not in got


def test_a_reading_the_thread_stopped_refreshing_goes_stale(plane, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", lambda: {"agent": 1})
    plane.metrics.refresh_immutable_changes()
    key = ("ordo_container_immutable_files_changed", (("service", "agent"),))
    clock[0] += metrics.IMMUTABLE_CHANGES_STALE_SECONDS
    assert _samples(plane.metrics_text())[key] == 1
    clock[0] += 1
    assert key not in _samples(plane.metrics_text())


def test_a_recreated_container_drops_its_old_count_and_wakes_the_reader(plane, monkeypatch):
    """After `ordo recreate hermes`, the step the alert's own description tells the operator to run,
    the old nonzero count must not keep paging a clean new container until the next reading."""
    ids = [{"agent": "old-container-id", "caddy": "caddy-id"}]
    monkeypatch.setattr(plane.broker.backend, "running_container_ids", lambda: dict(ids[0]))
    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", lambda: {"agent": 3, "caddy": 1})
    plane.metrics.refresh_immutable_changes()
    agent = ("ordo_container_immutable_files_changed", (("service", "agent"),))
    caddy = ("ordo_container_immutable_files_changed", (("service", "caddy"),))
    assert _samples(plane.metrics_text())[agent] == 3
    assert not plane.metrics.immutable_refresh_wanted.is_set()
    ids[0] = {"agent": "new-container-id", "caddy": "caddy-id"}   # `ordo recreate hermes`
    got = _samples(plane.metrics_text())
    assert agent not in got, "recreate: no stale page for the new container"
    assert got[caddy] == 1, "an unchanged container keeps its count"
    assert plane.metrics.immutable_refresh_wanted.is_set()


# --- the reader thread's loop (serve.py starts it) ------------------------------------------------- #

class _StopLoop(Exception):
    """Raised from a patched Event.wait to end the reader thread under test."""


def _loop_thread(plane, monkeypatch, stop_after_waits=2, wake_first_wait=False):
    """Run metrics.immutable_changes_loop in a thread; the patched Event.wait stops it after
    `stop_after_waits` calls and, when asked, simulates a scrape's recreate-wake on the first one.
    Returns the wait timeouts the loop requested."""
    delays = []
    real_wait = plane.metrics.immutable_refresh_wanted.wait

    def wait_and_track(timeout=None):
        delays.append(timeout)
        if wake_first_wait and len(delays) == 1:
            plane.metrics.immutable_refresh_wanted.set()     # a scrape saw a recreate mid-wait
        if len(delays) >= stop_after_waits:
            raise _StopLoop
        return real_wait(timeout)

    monkeypatch.setattr(plane.metrics.immutable_refresh_wanted, "wait", wait_and_track)

    def target():
        try:
            metrics.immutable_changes_loop(plane.metrics)
        except _StopLoop:
            pass

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the reader thread did not stop"
    return delays


def test_the_reader_thread_takes_a_reading_at_startup(plane, monkeypatch):
    """serve.py starts the thread before the first scrape: the first reading happens without waiting
    for the interval, so a scrape seconds after boot is not a failed collector."""
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_REFRESH_SECONDS", 0.5)
    started = time.monotonic()
    calls = []
    real = plane.metrics.refresh_immutable_changes

    def counting():
        calls.append(time.monotonic() - started)
        return real()

    monkeypatch.setattr(plane.metrics, "refresh_immutable_changes", counting)
    delays = _loop_thread(plane, monkeypatch, stop_after_waits=1)
    assert len(calls) == 1, "exactly the startup reading: nothing waits for the interval before it"
    assert calls[0] < 0.2, "the first reading is taken at once, not after the interval"
    assert delays == [0.5], "then the loop waits the full interval"


def test_a_recreate_seen_by_a_scrape_wakes_the_reader_before_the_interval(plane, monkeypatch):
    """A scrape that sees a recreate sets the event: the thread must take the next reading at once,
    not after the full interval."""
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_REFRESH_SECONDS", 10.0)
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_RETRY_SECONDS", 10.0)
    calls = []
    real = plane.metrics.refresh_immutable_changes

    def counting():
        calls.append(time.monotonic())
        return real()

    monkeypatch.setattr(plane.metrics, "refresh_immutable_changes", counting)
    _loop_thread(plane, monkeypatch, stop_after_waits=2, wake_first_wait=True)
    assert len(calls) == 2, "the wake took a second reading before the 10 s interval"
    assert calls[1] - calls[0] < 5.0, "the second reading came at the wake, not the interval"


def test_the_reader_clears_the_event_after_an_early_wake(plane, monkeypatch):
    """The wake must not stick: the loop clears the event after the early reading, so the next wait
    blocks for the full interval and a LATER recreate is what wakes it again."""
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_REFRESH_SECONDS", 0.5)
    _loop_thread(plane, monkeypatch, stop_after_waits=2, wake_first_wait=True)
    assert not plane.metrics.immutable_refresh_wanted.is_set(), \
        "the event is still set: every future wait returns at once and the interval is lost"


def test_the_reader_survives_a_raising_refresh(plane, monkeypatch):
    """An uncaught exception would kill the thread and leave the collector failed until the process
    restarts: the loop catches it and keeps taking readings."""
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_REFRESH_SECONDS", 0.5)
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_RETRY_SECONDS", 0.5)
    outcomes = []

    def flaky():
        if not outcomes:
            outcomes.append(False)
            raise RuntimeError("boom")
        outcomes.append(True)
        return True

    monkeypatch.setattr(plane.metrics, "refresh_immutable_changes", flaky)
    _loop_thread(plane, monkeypatch, stop_after_waits=2)
    assert outcomes == [False, True], "the loop kept running after refresh raised"


def test_a_failed_reading_backs_off_shorter_than_the_interval(plane, monkeypatch):
    """A transient docker hiccup must not blind the ImmutableFilesModified alert for the full
    interval: after a failed reading the loop retries after IMMUTABLE_CHANGES_RETRY_SECONDS, then
    returns to the normal cadence once the reading succeeds again."""
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_REFRESH_SECONDS", 10.0)
    monkeypatch.setattr(metrics, "IMMUTABLE_CHANGES_RETRY_SECONDS", 0.3)
    state = {"fail": True}

    def changes():
        if state["fail"]:
            raise TimeoutError("docker diff timed out after 180 seconds")
        return {"agent": 1}

    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", changes)
    delays = []
    real_wait = plane.metrics.immutable_refresh_wanted.wait

    def wait_and_track(timeout=None):
        delays.append(timeout)
        if len(delays) == 1:
            state["fail"] = False          # the hiccup clears while the thread backs off
        if len(delays) == 2:
            raise _StopLoop
        return real_wait(timeout)

    monkeypatch.setattr(plane.metrics.immutable_refresh_wanted, "wait", wait_and_track)

    def target():
        try:
            metrics.immutable_changes_loop(plane.metrics)
        except _StopLoop:
            pass

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the reader thread did not stop"
    assert delays == [metrics.IMMUTABLE_CHANGES_RETRY_SECONDS, metrics.IMMUTABLE_CHANGES_REFRESH_SECONDS]


def test_a_failing_docker_still_serves_the_rest(plane, monkeypatch):
    def broken():
        raise TimeoutError("docker ps timed out after 30 seconds")

    monkeypatch.setattr(plane.broker.backend, "service_restarts", broken)
    monkeypatch.setattr(plane.broker.backend, "immutable_path_changes", broken)
    monkeypatch.setattr(plane.broker.backend, "list_services", broken)
    got = _samples(plane.metrics_text())
    assert got[("ordo_metrics_collector_ok", (("collector", "restarts"),))] == 0
    assert got[("ordo_metrics_collector_ok", (("collector", "containers"),))] == 0
    assert got[("ordo_gpu_leases_running", ())] == 1


def test_the_route_is_plain_text_and_needs_no_token(plane):
    client = TestClient(plane.app(auth_token=ADMIN_TOKEN), raise_server_exceptions=False)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert ("ordo_gpu_leases_running", ()) in _samples(response.text)
    # Only the metrics read is opened: everything else still needs the bearer token.
    assert client.get("/status").status_code == 401
    assert client.post("/metrics").status_code == 404


# --- the GPU chat service's liveness, whichever engine runs it -------------------------------------

def test_gpu_chat_up_is_exported_only_when_probed():
    def value(up):
        return _samples(metrics.render(metrics.Inputs(gpu_chat_up=up))).get(("ordo_gpu_chat_up", ()))
    assert value(True) == 1 and value(False) == 0
    assert value(None) is None                      # no probe configured: no series, never a fake 0


def _collector_text(chat_up):
    return metrics.MetricsCollector(None, None, {}, {}, chat_up=chat_up).text()


def test_metrics_exports_the_cached_chat_up_without_probing():
    assert _samples(_collector_text(lambda: True))[("ordo_gpu_chat_up", ())] == 1
    assert _samples(_collector_text(lambda: False))[("ordo_gpu_chat_up", ())] == 0
    assert ("ordo_gpu_chat_up", ()) not in _samples(_collector_text(lambda: None))
    assert ("ordo_gpu_chat_up", ()) not in _samples(_collector_text(None))
