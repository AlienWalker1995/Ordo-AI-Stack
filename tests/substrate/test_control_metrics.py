"""ops-controller's GET /metrics: the series the alert rules read (monitoring/prometheus/rules/).

Before this, nothing exported the GPU lease, the containers' health and restart counts, the disks or
the edge certificate, so none of the incidents they would have caught (a Docker disk at 100%,
llama.cpp left evicted after a stale lease, a crash loop, an expired certificate) could alert.
"""
from __future__ import annotations

import re
import shutil
import subprocess
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
    """llamacpp evicted by a running media lease, llamacpp-embed still resident, one job queued."""
    sched = Scheduler(32)
    sched.cache_idle("llamacpp", 27.5)
    sched.cache_idle("llamacpp-embed", 1.0)
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
    assert got[("ordo_gpu_resident_evicted", (("resident", "llamacpp"),))] == 1
    assert got[("ordo_gpu_resident_evicted", (("resident", "llamacpp-embed"),))] == 0


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
    rows = [{"id": "llamacpp", "state": "running", "health": "healthy"},
            {"id": "mcp-comfyui", "state": "restarting", "health": None},
            {"id": "gpu-exporter", "state": "running", "health": "unhealthy"}]
    text = metrics.render(metrics.Inputs(
        containers=rows, restarts={"mcp-comfyui": 7, "llamacpp": 0},
        disks={"docker": metrics.DiskUsage(size=1_081_101_176_832, free=160_000_000_000, avail=141_733_474_304)},
        tls_certs={"edge": 1_797_981_483.0}))
    got = _samples(text)
    assert got[("ordo_container_running", (("service", "llamacpp"),))] == 1
    assert got[("ordo_container_running", (("service", "mcp-comfyui"),))] == 0
    assert got[("ordo_container_unhealthy", (("service", "gpu-exporter"),))] == 1
    assert got[("ordo_container_unhealthy", (("service", "llamacpp"),))] == 0
    assert got[("ordo_container_restarts_total", (("service", "mcp-comfyui"),))] == 7
    assert "# TYPE ordo_container_restarts_total counter" in text
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
        scheduler=_leased_scheduler().status(), containers=[], restarts={},
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
    backend = MockBackend({"services": {"llamacpp": {"image": "example/llamacpp"},
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
    assert got[("ordo_gpu_resident_evicted", (("resident", "llamacpp"),))] == 1
    assert got[("ordo_container_restarts_total", (("service", "mcp-comfyui"),))] == 3
    assert got[("ordo_container_running", (("service", "llamacpp"),))] == 1
    assert got[("ordo_filesystem_size_bytes", (("mount", "host"),))] == 200
    assert got[("ordo_filesystem_free_bytes", (("mount", "docker"),))] == 10
    assert got[("ordo_tls_cert_not_after_timestamp_seconds", (("cert", "edge"),))] == 1_797_981_483


def test_a_failing_docker_still_serves_the_rest(plane, monkeypatch):
    def broken():
        raise TimeoutError("docker ps timed out after 30 seconds")

    monkeypatch.setattr(plane.broker.backend, "service_restarts", broken)
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


def _serve(status: int):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(("status", "up"), [(200, True), (503, False)])
def test_the_collector_probes_the_chat_health_endpoint(status, up):
    server = _serve(status)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/health"
        collector = metrics.MetricsCollector(None, None, {}, {}, chat_health_url=url)
        assert collector._chat_up() is up
    finally:
        server.shutdown()


def test_an_unreachable_chat_service_is_down_not_an_error():
    collector = metrics.MetricsCollector(None, None, {}, {}, chat_health_url="http://127.0.0.1:9/health")
    assert collector._chat_up() is False
    assert metrics.MetricsCollector(None, None, {}, {})._chat_up() is None
