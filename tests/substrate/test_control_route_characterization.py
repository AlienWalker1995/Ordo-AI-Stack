"""What every ops-controller route answers, pinned as a table.

A characterization test: each row is one call (plane, method, path, body, query) and what the
control plane answered for it when the table was recorded: the status, the payload's top-level
keys, the error message, the audit record the call left (action, target, result), and what the
call does while another stack-changing operation holds the operation lock (`refused` with 409,
`blocks` until it is released, or `runs`). It pins the routing, the confirm and dry-run rules,
the refusals, the audit subject and the `_exclusive` wrapping of every route at once, so a
restructuring of `ControlPlane` that changes any of them fails here, naming the row.
`GET /metrics`, which the HTTP binding serves outside `route()`, is pinned separately through
the app (`METRICS_EXPECTED`).

The planes: `full` (a scheduler and a broker over a MockBackend with a netns owner and member,
and one managed project), `leased` (the same, while a render holds the card and llamacpp is
evicted for it) and `bare` (no scheduler, no broker). Nothing here reaches the network, docker
or a GPU: DNS, `subprocess.run` for docker, the live GPU reader and the download worker are faked.
"""
from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from ordo.control.api import ControlPlane
from ordo.control.audit import AuditLog
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Job, Scheduler
from ordo.render import gpu_live
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
# The first catalog model a 32 GB card can hold: a model switch that succeeds.
SWITCHABLE = next(m.id for m in CATALOG.models if m.vram_gb <= 24)

COMPOSE = {"services": {
    "llamacpp": {"image": "llama"},
    "open-webui": {"image": "owui"},
    "caddy": {"image": "caddy"},
    "tailnet-chat": {"image": "ts", "network_mode": "service:caddy"},
}}
CARDS = [{"index": 0, "uuid": "GPU-00000000-0000-0000-0000-000000000001", "name": "Test GPU",
          "vram_total_mib": 32768, "vram_used_mib": 1024, "utilization_pct": 3}]
LOCK_WAIT_SECONDS = 1.0

C = {"confirm": True}
D = {"dry_run": True}

# (plane, method, path, body, query)
CALLS: list[tuple[str, str, str, dict | None, dict | None]] = [
    # reads
    ("full", "GET", "/status", None, None),
    ("full", "GET", "/model-config", None, None),
    ("full", "GET", "/plugins", None, None),
    ("full", "GET", "/jobs/history", None, None),
    ("full", "GET", "/doctor", None, None),
    ("full", "POST", "/doctor", {}, None),
    ("full", "GET", "/metrics", None, None),
    ("full", "GET", "/health", None, None),
    ("full", "GET", "/healthz", None, None),
    ("full", "GET", "/services", None, None),
    ("full", "GET", "/services/llamacpp/logs", None, None),
    ("full", "GET", "/containers", None, None),
    ("full", "GET", "/containers/ordo-llamacpp-1/logs", None, None),
    ("full", "GET", "/containers/ordo-llamacpp-1", None, None),
    ("full", "GET", "/containers/no-such-container", None, None),
    ("full", "GET", "/containers/a/b", None, None),
    ("full", "GET", "/stats/services", None, None),
    ("full", "GET", "/registry/models", None, None),
    ("full", "GET", "/registry/gpus", None, None),
    ("full", "GET", "/gpus", None, None),
    ("full", "GET", "/models/download/status", None, None),
    ("full", "GET", "/diagnostics/dstate", None, None),
    ("full", "GET", "/audit", None, None),
    ("full", "GET", "/audit", None, {"limit": "2"}),
    ("full", "GET", "/audit", None, {"limit": "x"}),
    ("full", "GET", "/audit", None, {"limit": "0"}),
    ("full", "GET", "/audit", None, {"limit": "1001"}),
    ("full", "GET", "/projects", None, None),
    ("full", "GET", "/projects/side/containers", None, None),
    ("full", "GET", "/projects/other/containers", None, None),
    ("full", "GET", "/projects/side/containers/side-web-1/logs", None, None),
    ("full", "GET", "/projects/side/containers/side-web-1/logs", None, {"tail": "5"}),
    ("full", "GET", "/projects/side/containers/side-web-1/logs", None, {"tail": "x"}),
    ("full", "GET", "/projects/side/containers/nope/logs", None, None),
    ("full", "GET", "/projects/side/containers/side-web-1/restart", None, None),
    ("full", "get", "/status", None, None),
    # the model switch and the apply
    ("full", "POST", "/model-config", {}, None),
    ("full", "POST", "/model-config", {"model": "no-such-model"}, None),
    ("full", "POST", "/model-config", {"model": "auto"}, None),
    ("full", "POST", "/model-config", {"model": SWITCHABLE}, None),
    ("full", "POST", "/apply", {}, None),
    ("full", "POST", "/apply", D, None),
    ("full", "POST", "/apply", C, None),
    ("full", "post", "/apply", D, None),
    # plugins
    ("full", "POST", "/plugins/searxng/enable", {}, None),
    ("full", "POST", "/plugins/searxng/enable", D, None),
    ("full", "POST", "/plugins/searxng/enable", C, None),
    ("full", "POST", "/plugins/llamacpp/enable", C, None),
    ("full", "POST", "/plugins/no-such-plugin/enable", C, None),
    ("full", "POST", "/plugins/searxng/disable", {}, None),
    ("full", "POST", "/plugins/searxng/disable", D, None),
    ("full", "POST", "/plugins/searxng/disable", C, None),
    ("full", "POST", "/plugins/llamacpp/disable", C, None),
    ("full", "POST", "/plugins/enable", C, None),
    ("full", "POST", "/plugins/a/b/enable", C, None),
    # the GPU lease
    ("full", "POST", "/jobs", {"id": "render", "vram_gb": 18, "kind": "media"}, None),
    ("full", "POST", "/jobs", {"id": "render"}, None),
    ("full", "POST", "/jobs", {"id": "huge", "vram_gb": 999}, None),
    ("full", "POST", "/jobs/complete", {}, None),
    ("full", "POST", "/jobs/complete", {"id": "ghost"}, None),
    ("full", "POST", "/jobs/heartbeat", {}, None),
    ("full", "POST", "/jobs/heartbeat", {"id": "ghost"}, None),
    ("leased", "POST", "/jobs/heartbeat", {"id": "gate-comfyui"}, None),
    ("leased", "POST", "/jobs/complete", {"id": "gate-comfyui"}, None),
    # service lifecycle
    ("full", "POST", "/services/llamacpp/start", {}, None),
    ("full", "POST", "/services/llamacpp/start", D, None),
    ("full", "POST", "/services/llamacpp/start", C, None),
    ("full", "POST", "/services/llamacpp/stop", {}, None),
    ("full", "POST", "/services/llamacpp/stop", D, None),
    ("full", "POST", "/services/llamacpp/stop", C, None),
    ("full", "POST", "/services/llamacpp/restart", {}, None),
    ("full", "POST", "/services/llamacpp/restart", D, None),
    ("full", "POST", "/services/llamacpp/restart", C, None),
    ("full", "POST", "/services/llamacpp/recreate", {}, None),
    ("full", "POST", "/services/llamacpp/recreate", D, None),
    ("full", "POST", "/services/llamacpp/recreate", C, None),
    ("full", "POST", "/services/caddy/restart", C, None),
    ("full", "POST", "/services/caddy/stop", C, None),
    ("full", "POST", "/services/caddy/start", C, None),
    ("full", "POST", "/services/caddy/recreate", C, None),
    ("full", "POST", "/services/no-such-service/restart", C, None),
    ("full", "POST", "/services/a/b/start", C, None),
    ("full", "POST", "/services/start", C, None),
    ("full", "POST", "/services//stop", C, None),
    ("full", "POST", "/services/llamacpp/logs", C, None),
    ("leased", "POST", "/services/llamacpp/start", C, None),
    ("leased", "POST", "/services/llamacpp/stop", C, None),
    ("leased", "POST", "/services/llamacpp/restart", C, None),
    ("leased", "POST", "/services/llamacpp/recreate", C, None),
    ("leased", "POST", "/services/open-webui/restart", C, None),
    # containers
    ("full", "POST", "/containers/ordo-llamacpp-1/restart", {}, None),
    ("full", "POST", "/containers/ordo-llamacpp-1/restart", C, None),
    ("full", "POST", "/containers/ordo-caddy-1/restart", C, None),
    ("leased", "POST", "/containers/ordo-llamacpp-1/restart", C, None),
    # compose verbs
    ("full", "POST", "/compose/up", {}, None),
    ("full", "POST", "/compose/up", C, None),
    ("full", "POST", "/compose/up", {"confirm": True, "service": ""}, None),
    ("full", "POST", "/compose/up", {"confirm": True, "service": "open-webui"}, None),
    ("full", "POST", "/compose/down", {}, None),
    ("full", "POST", "/compose/down", {"confirm": True, "service": 3}, None),
    ("full", "POST", "/compose/down", {"confirm": True, "service": "open-webui"}, None),
    ("full", "POST", "/compose/restart", {}, None),
    ("full", "POST", "/compose/restart", {"confirm": True, "service": "caddy"}, None),
    ("leased", "POST", "/compose/up", C, None),
    ("leased", "POST", "/compose/down", {"confirm": True, "service": "llamacpp"}, None),
    ("leased", "POST", "/compose/restart", {"confirm": True, "service": "open-webui"}, None),
    # downloads, ComfyUI nodes and the retired GPU pins
    ("full", "POST", "/models/download", {"url": "http://huggingface.co/x/w.safetensors"}, None),
    ("full", "POST", "/models/download", {"url": "https://evil.example.com/w.safetensors"}, None),
    ("full", "POST", "/models/download", {"url": "https://huggingface.co/x/w.safetensors",
                                          "filename": "../w"}, None),
    ("full", "POST", "/models/download", {"url": "https://huggingface.co/x/w.safetensors",
                                          "category": "nope"}, None),
    ("full", "POST", "/models/download", {"url": "https://huggingface.co/x/lora/w.safetensors"}, None),
    ("full", "POST", "/comfyui/install-node-requirements", {}, None),
    ("full", "POST", "/comfyui/install-node-requirements", {"confirm": True, "node_path": "../x"}, None),
    ("full", "POST", "/comfyui/install-node-requirements", {"confirm": True, "node_path": "Pack"}, None),
    ("full", "POST", "/comfyui/install-node-requirements", {"confirm": True, "node_path": "Ready"}, None),
    ("full", "POST", "/gpu/assign", {"service": "llamacpp"}, None),
    ("full", "POST", "/registry/models/local-chat/assign-gpu", {}, None),
    ("full", "POST", "/registry/models/a/b/assign-gpu", {}, None),
    # managed projects
    ("full", "POST", "/projects/side/containers/side-web-1/restart", {}, None),
    ("full", "POST", "/projects/side/containers/side-web-1/restart", C, None),
    ("full", "POST", "/projects/side/containers/nope/restart", C, None),
    ("full", "POST", "/projects/other/containers/x/restart", C, None),
    ("full", "POST", "/projects/side/containers", C, None),
    ("full", "POST", "/projects/side/containers/side-web-1/stop", C, None),
    # nothing serves these
    ("full", "GET", "/nope", None, None),
    ("full", "GET", "/services/", None, None),
    ("full", "GET", "/models/packs", None, None),
    ("full", "POST", "/status", {}, None),
    ("full", "DELETE", "/services/llamacpp/restart", None, None),
    ("full", "PUT", "/apply", C, None),
    ("full", "PATCH", "/model-config", {"model": "auto"}, None),
    ("full", "GET", "/apply", None, None),
    # no scheduler and no broker
    ("bare", "GET", "/status", None, None),
    ("bare", "GET", "/services", None, None),
    ("bare", "GET", "/services/llamacpp/logs", None, None),
    ("bare", "GET", "/containers", None, None),
    ("bare", "GET", "/containers/ordo-llamacpp-1/logs", None, None),
    ("bare", "GET", "/containers/ordo-llamacpp-1", None, None),
    ("bare", "GET", "/stats/services", None, None),
    ("bare", "GET", "/projects", None, None),
    ("bare", "GET", "/projects/side/containers", None, None),
    ("bare", "GET", "/jobs/history", None, None),
    ("bare", "GET", "/doctor", None, None),
    ("bare", "POST", "/apply", C, None),
    ("bare", "POST", "/apply", D, None),
    ("bare", "POST", "/model-config", {"model": SWITCHABLE}, None),
    ("bare", "POST", "/plugins/searxng/enable", C, None),
    ("bare", "POST", "/jobs", {"id": "render", "vram_gb": 18}, None),
    ("bare", "POST", "/jobs/complete", {"id": "render"}, None),
    ("bare", "POST", "/jobs/heartbeat", {"id": "render"}, None),
    ("bare", "POST", "/services/llamacpp/start", C, None),
    ("bare", "POST", "/services/llamacpp/stop", D, None),
    ("bare", "POST", "/containers/ordo-llamacpp-1/restart", C, None),
    ("bare", "POST", "/compose/up", C, None),
    ("bare", "POST", "/comfyui/install-node-requirements", {"confirm": True, "node_path": "Ready"}, None),
    ("bare", "POST", "/projects/side/containers/side-web-1/restart", C, None),
]


def _fake_subprocess_run(real_run):
    def run(cmd, *args, **kwargs):
        if list(cmd)[:1] == ["docker"]:
            if list(cmd)[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="ordo-llamacpp-1\n", stderr="")
            header = "PID STAT WCHAN COMMAND\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=header + "7 D p9_client_rpc python\n", stderr="")
        return real_run(cmd, *args, **kwargs)
    return run


@pytest.fixture
def world(monkeypatch):
    """Fakes for everything outside the process: DNS, docker's CLI and the live GPU reader."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run(subprocess.run))
    monkeypatch.setattr(gpu_live, "live_gpus", lambda: [dict(card) for card in CARDS])
    return monkeypatch


def _plane(tmp_path: Path, kind: str, monkeypatch, **extra: Any) -> ControlPlane:
    """A control plane over its own files under `tmp_path`: source, out/, audit log, custom nodes."""
    tmp_path.mkdir(parents=True)
    monkeypatch.setattr("ordo.control.api.AUDIT_LOG_PATH", tmp_path / "data" / "audit.jsonl")
    nodes = tmp_path / "custom_nodes"
    (nodes / "Ready").mkdir(parents=True)
    (nodes / "Ready" / "requirements.txt").write_text("requests\n", encoding="utf-8")
    monkeypatch.setattr("ordo.control.api.COMFYUI_CUSTOM_NODES_DIR", nodes)
    source = tmp_path / "ordo.yaml"
    source.write_text(yaml.safe_dump({
        "hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto",
        "plugins": ["searxng"], "managed_projects": ["side"],
    }), encoding="utf-8")
    AuditLog(tmp_path / "data" / "audit.jsonl").record(action="seed", target="t", result="ok", caller="c")
    if kind == "bare":
        cp = ControlPlane(source, CATALOG, REGISTRY, tmp_path / "out", **extra)
    else:
        scheduler = Scheduler(32)
        backend = MockBackend(yaml.safe_load(yaml.safe_dump(COMPOSE)))
        backend.foreign = {"side": {"side-web-1": (
            {"name": "side-web-1", "service": "web", "state": "running", "health": None,
             "status": "Up", "image": "nginx", "secret": "never shown"},
            {"Config": {"Env": []}, "HostConfig": {}},
        )}}
        broker = Broker(scheduler, backend)
        if kind == "leased":
            scheduler.cache_idle("llamacpp", 25)
            broker.request(Job("gate-comfyui", 20, "media"))
        cp = ControlPlane(source, CATALOG, REGISTRY, tmp_path / "out", scheduler=scheduler, broker=broker, **extra)
    cp._render().write(tmp_path / "out")
    cp._run_model_download = lambda *args: None  # no network: the download worker never runs
    return cp


def _normalize(value: Any, tmp_path: Path, cp: ControlPlane) -> str:
    text = str(value)
    for raw in (str(tmp_path), tmp_path.as_posix()):
        text = text.replace(raw, "<tmp>")
    return text.replace("\\", "/").replace(cp.substrate_digest, "<digest>")


def _last_audit(tmp_path: Path) -> tuple[str, str, str] | None:
    records = AuditLog(tmp_path / "data" / "audit.jsonl").tail(1)
    if not records or records[0].get("action") == "seed":
        return None
    record = records[0]
    return record["action"], record["target"], record["result"]


def _observe(tmp_path: Path, kind: str, monkeypatch, method: str, path: str, body, query) -> dict[str, Any]:
    cp = _plane(tmp_path, kind, monkeypatch)
    status, payload = cp.handle(method, path, body, query, "characterization")
    error = payload.get("error") if isinstance(payload, dict) else None
    return {
        "status": status,
        "keys": tuple(sorted(payload)) if isinstance(payload, dict) else type(payload).__name__,
        "error": _normalize(error, tmp_path, cp) if error is not None else None,
        "audit": _last_audit(tmp_path),
    }


def _under_held_lock(tmp_path: Path, kind: str, monkeypatch, method: str, path: str, body, query) -> str:
    """`refused` (409 busy), `blocks` (waits for the lock) or `runs`, while another thread holds
    the operation lock a stack-changing verb takes."""
    cp = _plane(tmp_path, kind, monkeypatch)
    held, release = threading.Event(), threading.Event()

    def holder():
        with cp._operation_lock:
            held.set()
            release.wait(10)

    holding = threading.Thread(target=holder, daemon=True)
    holding.start()
    held.wait(5)
    answer: list[tuple[int, dict]] = []
    call = threading.Thread(target=lambda: answer.append(cp.route(method, path, body, query)), daemon=True)
    call.start()
    call.join(LOCK_WAIT_SECONDS)
    blocked = call.is_alive()
    release.set()
    call.join(10)
    holding.join(10)
    if blocked:
        return "blocks"
    status, payload = answer[0]
    busy = status == 409 and str(payload.get("error", "")).startswith("another operation that changes the stack")
    return "refused" if busy else "runs"


def _row_id(call) -> str:
    plane, method, path, body, query = call
    return f"{plane} {method} {path} {body or ''} {query or ''}".strip()


@pytest.mark.parametrize("call", CALLS, ids=[_row_id(c) for c in CALLS])
def test_route_answers_as_recorded(world, tmp_path, call):
    plane, method, path, body, query = call
    answer = _observe(tmp_path / "answer", plane, world, method, path, body, query)
    under_lock = _under_held_lock(tmp_path / "locked", plane, world, method, path, body, query)
    observed = (answer["status"], answer["keys"], answer["error"], answer["audit"], under_lock)
    assert observed == EXPECTED[_row_id(call)]


@pytest.mark.parametrize("plane, method, token", [
    ("full", "GET", False), ("full", "GET", True), ("bare", "GET", False), ("full", "POST", True),
])
def test_metrics_answers_as_recorded(world, tmp_path, plane, method, token):
    """GET /metrics is served by the HTTP binding, not route(): unauthenticated, plain text, one
    family per metric. Pinned as (status, content type, the metric families, the collectors)."""
    from fastapi.testclient import TestClient

    from ordo.control import metrics

    world.setattr(metrics.DiskUsage, "of", classmethod(lambda cls, path: metrics.DiskUsage(100, 40, 30)))
    world.setattr(metrics, "read_cert_not_after", lambda path: 1_797_981_483.0)
    cp = _plane(tmp_path / "metrics", plane, world, disk_paths={"docker": "/", "host": "/config"},
                tls_cert_files={"edge": "/certs/edge.pem"})
    client = TestClient(cp.app(auth_token="characterization-token"), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer characterization-token"} if token else {}
    response = client.request(method, "/metrics", headers=headers)
    families = tuple(line.split()[2] for line in response.text.splitlines() if line.startswith("# TYPE "))
    collectors = tuple(line for line in response.text.splitlines() if line.startswith("ordo_metrics_collector_ok"))
    observed = (response.status_code, response.headers.get("content-type"), families, collectors)
    assert observed == METRICS_EXPECTED[(plane, method, token)]


def test_every_row_has_a_recording():
    assert sorted(EXPECTED) == sorted(_row_id(c) for c in CALLS)


# row id: (status, payload keys, error, audit (action, target, result), under the operation lock)
EXPECTED: dict[str, tuple] = {
    'full GET /status': (200, ('gpu', 'manifest'), None, None, 'runs'),
    'full GET /model-config': (200, ('active_file', 'active_mmproj', 'active_model', 'available', 'ctx_size', 'model_files', 'source_model', 'tier'), None, None, 'runs'),
    'full GET /plugins': (200, ('plugins',), None, None, 'runs'),
    'full GET /jobs/history': (200, ('history',), None, None, 'runs'),
    'full GET /doctor': (200, ('checks', 'ok'), None, None, 'runs'),
    'full POST /doctor': (404, ('error',), 'no route POST /doctor', ('unknown', '', 'refused'), 'runs'),
    'full GET /metrics': (404, ('error',), 'no route GET /metrics', None, 'runs'),
    'full GET /health': (200, ('ok', 'substrate_digest'), None, None, 'runs'),
    'full GET /healthz': (200, ('ok', 'substrate_digest'), None, None, 'runs'),
    'full GET /services': (200, ('services',), None, None, 'runs'),
    'full GET /services/llamacpp/logs': (200, ('logs', 'service'), None, None, 'runs'),
    'full GET /containers': (200, 'list', None, None, 'runs'),
    'full GET /containers/ordo-llamacpp-1/logs': (200, 'str', None, None, 'runs'),
    'full GET /containers/ordo-llamacpp-1': (200, ('health', 'image', 'image_id', 'mounts', 'name', 'networks', 'ports', 'project', 'restart_count', 'restart_policy', 'service', 'started_at', 'state'), None, None, 'runs'),
    'full GET /containers/no-such-container': (404, ('error',), "container 'no-such-container' is not in project 'ordo'", None, 'runs'),
    'full GET /containers/a/b': (404, ('error',), 'no route GET /containers/a/b', None, 'runs'),
    'full GET /stats/services': (200, ('gpu', 'services', 'vram_aggregate_unavailable'), None, None, 'runs'),
    'full GET /registry/models': (200, ('models',), None, None, 'runs'),
    'full GET /registry/gpus': (200, ('gpus',), None, None, 'runs'),
    'full GET /gpus': (200, ('gpus',), None, None, 'runs'),
    'full GET /models/download/status': (200, ('category', 'done', 'filename', 'output', 'progress', 'running', 'success'), None, None, 'runs'),
    'full GET /diagnostics/dstate': (200, ('errors', 'p9_wedged', 'scanned', 'wedged'), None, None, 'runs'),
    'full GET /audit': (200, ('entries',), None, None, 'runs'),
    "full GET /audit  {'limit': '2'}": (200, ('entries',), None, None, 'runs'),
    "full GET /audit  {'limit': 'x'}": (422, ('error',), 'limit must be an integer', None, 'runs'),
    "full GET /audit  {'limit': '0'}": (422, ('error',), 'limit must be between 1 and 1000', None, 'runs'),
    "full GET /audit  {'limit': '1001'}": (422, ('error',), 'limit must be between 1 and 1000', None, 'runs'),
    'full GET /projects': (200, ('projects',), None, None, 'runs'),
    'full GET /projects/side/containers': (200, ('containers', 'project'), None, None, 'runs'),
    'full GET /projects/other/containers': (404, ('error',), "'other' is not a managed project (ordo.yaml managed_projects: ['side'])", None, 'runs'),
    'full GET /projects/side/containers/side-web-1/logs': (200, ('container', 'logs', 'project', 'tail'), None, ('project.logs', 'side/side-web-1', 'ok'), 'runs'),
    "full GET /projects/side/containers/side-web-1/logs  {'tail': '5'}": (200, ('container', 'logs', 'project', 'tail'), None, ('project.logs', 'side/side-web-1', 'ok'), 'runs'),
    "full GET /projects/side/containers/side-web-1/logs  {'tail': 'x'}": (422, ('error',), 'tail must be an integer', ('project.logs', 'side/side-web-1', 'refused'), 'runs'),
    'full GET /projects/side/containers/nope/logs': (404, ('error',), "container 'nope' is not in project 'side'", ('project.logs', 'side/nope', 'refused'), 'runs'),
    'full GET /projects/side/containers/side-web-1/restart': (404, ('error',), 'no route GET /projects/side/containers/side-web-1/restart', None, 'runs'),
    'full get /status': (200, ('gpu', 'manifest'), None, None, 'runs'),
    'full POST /model-config': (400, ('error',), "body must include 'model' (a catalog id or 'auto')", ('model_config', '', 'refused'), 'refused'),
    "full POST /model-config {'model': 'no-such-model'}": (404, ('available', 'error'), "model 'no-such-model' not in catalog", ('model_config', 'no-such-model', 'refused'), 'refused'),
    "full POST /model-config {'model': 'auto'}": (200, ('active_model', 'apply', 'ctx_size', 'ok', 'warnings', 'wrote'), None, ('model_config', 'auto', 'ok'), 'refused'),
    "full POST /model-config {'model': 'qwen2.5-0.5b-instruct-q4'}": (200, ('active_model', 'apply', 'ctx_size', 'ok', 'warnings', 'wrote'), None, ('model_config', 'qwen2.5-0.5b-instruct-q4', 'ok'), 'refused'),
    'full POST /apply': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed, or {"dry_run": true} for the plan.', ('apply', 'stack', 'refused'), 'refused'),
    "full POST /apply {'dry_run': True}": (200, ('changes', 'dry_run', 'host_command', 'host_reasons', 'ok', 'orphans', 'recreated', 'removed_jobs', 'restart_required_on_host', 'running_jobs', 'stopped', 'warnings'), None, ('apply', 'stack', 'ok'), 'refused'),
    "full POST /apply {'confirm': True}": (200, ('changes', 'dry_run', 'host_command', 'host_reasons', 'ok', 'orphans', 'recreated', 'removed_jobs', 'restart_required_on_host', 'running_jobs', 'stopped', 'warnings'), None, ('apply', 'stack', 'ok'), 'refused'),
    "full post /apply {'dry_run': True}": (200, ('changes', 'dry_run', 'host_command', 'host_reasons', 'ok', 'orphans', 'recreated', 'removed_jobs', 'restart_required_on_host', 'running_jobs', 'stopped', 'warnings'), None, ('apply', 'stack', 'ok'), 'refused'),
    'full POST /plugins/searxng/enable': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('plugin.enable', 'searxng', 'refused'), 'refused'),
    "full POST /plugins/searxng/enable {'dry_run': True}": (200, ('plugin', 'would'), None, ('plugin.enable', 'searxng', 'ok'), 'refused'),
    "full POST /plugins/searxng/enable {'confirm': True}": (200, ('added', 'already_rendered', 'apply', 'compose_profile', 'missing_secrets', 'ok', 'plugin', 'services', 'wants_secrets', 'warnings', 'wrote'), None, ('plugin.enable', 'searxng', 'ok'), 'refused'),
    "full POST /plugins/llamacpp/enable {'confirm': True}": (403, ('error', 'installable'), "'llamacpp' is not an installable service (core, edge/front-door, and the agent are refused)", ('plugin.enable', 'llamacpp', 'refused'), 'refused'),
    "full POST /plugins/no-such-plugin/enable {'confirm': True}": (403, ('error', 'installable'), "'no-such-plugin' is not an installable service (core, edge/front-door, and the agent are refused)", ('plugin.enable', 'no-such-plugin', 'refused'), 'refused'),
    'full POST /plugins/searxng/disable': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('plugin.disable', 'searxng', 'refused'), 'refused'),
    "full POST /plugins/searxng/disable {'dry_run': True}": (200, ('plugin', 'would'), None, ('plugin.disable', 'searxng', 'ok'), 'refused'),
    "full POST /plugins/searxng/disable {'confirm': True}": (422, ('error',), 'cannot safely edit ordo.yaml plugins list: edited ordo.yaml `plugins` is not a list', ('plugin.disable', 'searxng', 'refused'), 'refused'),
    "full POST /plugins/llamacpp/disable {'confirm': True}": (403, ('error',), "'llamacpp' is not an installable service", ('plugin.disable', 'llamacpp', 'refused'), 'refused'),
    "full POST /plugins/enable {'confirm': True}": (403, ('error', 'installable'), "'' is not an installable service (core, edge/front-door, and the agent are refused)", ('unknown', '', 'refused'), 'refused'),
    "full POST /plugins/a/b/enable {'confirm': True}": (403, ('error', 'installable'), "'a/b' is not an installable service (core, edge/front-door, and the agent are refused)", ('plugin.enable', 'a/b', 'refused'), 'refused'),
    "full POST /jobs {'id': 'render', 'vram_gb': 18, 'kind': 'media'}": (200, ('eta_seconds', 'evicted_residents', 'free_vram_gb', 'idle_cached', 'leased', 'queued', 'rejected', 'running', 'state', 'total_vram_gb', 'waiting_on_vram'), None, ('lease.request', 'render', 'ok'), 'runs'),
    "full POST /jobs {'id': 'render'}": (400, ('error',), "job needs 'id' and numeric 'vram_gb'", ('lease.request', 'render', 'refused'), 'runs'),
    "full POST /jobs {'id': 'huge', 'vram_gb': 999}": (200, ('eta_seconds', 'evicted_residents', 'free_vram_gb', 'idle_cached', 'leased', 'queued', 'rejected', 'running', 'state', 'total_vram_gb', 'waiting_on_vram'), None, ('lease.request', 'huge', 'ok'), 'runs'),
    'full POST /jobs/complete': (400, ('error',), "body must include 'id'", ('lease.release', '', 'refused'), 'runs'),
    "full POST /jobs/complete {'id': 'ghost'}": (200, ('eta_seconds', 'evicted_residents', 'free_vram_gb', 'idle_cached', 'leased', 'queued', 'rejected', 'running', 'state', 'total_vram_gb', 'waiting_on_vram'), None, ('lease.release', 'ghost', 'ok'), 'runs'),
    'full POST /jobs/heartbeat': (400, ('error',), "body must include 'id'", ('lease.heartbeat', '', 'refused'), 'runs'),
    "full POST /jobs/heartbeat {'id': 'ghost'}": (404, ('error',), "no running job 'ghost'", ('lease.heartbeat', 'ghost', 'refused'), 'runs'),
    "leased POST /jobs/heartbeat {'id': 'gate-comfyui'}": (200, ('eta_seconds', 'evicted_residents', 'free_vram_gb', 'idle_cached', 'leased', 'queued', 'rejected', 'running', 'state', 'total_vram_gb', 'waiting_on_vram'), None, ('lease.heartbeat', 'gate-comfyui', 'ok'), 'runs'),
    "leased POST /jobs/complete {'id': 'gate-comfyui'}": (200, ('eta_seconds', 'evicted_residents', 'free_vram_gb', 'idle_cached', 'leased', 'queued', 'rejected', 'running', 'state', 'total_vram_gb', 'waiting_on_vram'), None, ('lease.release', 'gate-comfyui', 'ok'), 'runs'),
    'full POST /services/llamacpp/start': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('start', 'llamacpp', 'refused'), 'refused'),
    "full POST /services/llamacpp/start {'dry_run': True}": (200, ('service', 'would'), None, ('start', 'llamacpp', 'ok'), 'refused'),
    "full POST /services/llamacpp/start {'confirm': True}": (200, ('action', 'ok', 'service'), None, ('start', 'llamacpp', 'ok'), 'refused'),
    'full POST /services/llamacpp/stop': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('stop', 'llamacpp', 'refused'), 'refused'),
    "full POST /services/llamacpp/stop {'dry_run': True}": (200, ('service', 'would'), None, ('stop', 'llamacpp', 'ok'), 'refused'),
    "full POST /services/llamacpp/stop {'confirm': True}": (200, ('action', 'ok', 'service'), None, ('stop', 'llamacpp', 'ok'), 'refused'),
    'full POST /services/llamacpp/restart': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('restart', 'llamacpp', 'refused'), 'refused'),
    "full POST /services/llamacpp/restart {'dry_run': True}": (200, ('service', 'would'), None, ('restart', 'llamacpp', 'ok'), 'refused'),
    "full POST /services/llamacpp/restart {'confirm': True}": (200, ('action', 'ok', 'service'), None, ('restart', 'llamacpp', 'ok'), 'refused'),
    'full POST /services/llamacpp/recreate': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('recreate', 'llamacpp', 'refused'), 'refused'),
    "full POST /services/llamacpp/recreate {'dry_run': True}": (200, ('service', 'would'), None, ('recreate', 'llamacpp', 'ok'), 'refused'),
    "full POST /services/llamacpp/recreate {'confirm': True}": (200, ('action', 'ok', 'service'), None, ('recreate', 'llamacpp', 'ok'), 'refused'),
    "full POST /services/caddy/restart {'confirm': True}": (200, ('action', 'members', 'ok', 'service'), None, ('restart', 'caddy', 'ok'), 'refused'),
    "full POST /services/caddy/stop {'confirm': True}": (200, ('action', 'members', 'ok', 'service'), None, ('stop', 'caddy', 'ok'), 'refused'),
    "full POST /services/caddy/start {'confirm': True}": (200, ('action', 'members', 'ok', 'service'), None, ('start', 'caddy', 'ok'), 'refused'),
    "full POST /services/caddy/recreate {'confirm': True}": (200, ('action', 'members', 'ok', 'service'), None, ('recreate', 'caddy', 'ok'), 'refused'),
    "full POST /services/no-such-service/restart {'confirm': True}": (200, ('action', 'ok', 'service'), None, ('restart', 'no-such-service', 'ok'), 'refused'),
    "full POST /services/a/b/start {'confirm': True}": (500, ('error',), "not a valid compose service name for 'ordo': 'a/b'", ('start', 'a/b', 'error'), 'refused'),
    "full POST /services/start {'confirm': True}": (500, ('error',), "not a valid compose service name for 'ordo': ''", ('unknown', '', 'error'), 'refused'),
    "full POST /services//stop {'confirm': True}": (500, ('error',), "not a valid compose service name for 'ordo': ''", ('unknown', '', 'error'), 'refused'),
    "full POST /services/llamacpp/logs {'confirm': True}": (404, ('error',), 'no route POST /services/llamacpp/logs', ('unknown', '', 'refused'), 'runs'),
    "leased POST /services/llamacpp/start {'confirm': True}": (409, ('error', 'lease_holders'), "'llamacpp' is evicted for a GPU lease held by ['gate-comfyui']; starting it would put two tenants on one card. The scheduler restores it when the lease is released.", ('start', 'llamacpp', 'refused'), 'refused'),
    "leased POST /services/llamacpp/stop {'confirm': True}": (200, ('action', 'ok', 'service'), None, ('stop', 'llamacpp', 'ok'), 'refused'),
    "leased POST /services/llamacpp/restart {'confirm': True}": (409, ('error', 'lease_holders'), "'llamacpp' is evicted for a GPU lease held by ['gate-comfyui']; starting it would put two tenants on one card. The scheduler restores it when the lease is released.", ('restart', 'llamacpp', 'refused'), 'refused'),
    "leased POST /services/llamacpp/recreate {'confirm': True}": (409, ('error', 'lease_holders'), "'llamacpp' is evicted for a GPU lease held by ['gate-comfyui']; starting it would put two tenants on one card. The scheduler restores it when the lease is released.", ('recreate', 'llamacpp', 'refused'), 'refused'),
    "leased POST /services/open-webui/restart {'confirm': True}": (200, ('action', 'ok', 'service'), None, ('restart', 'open-webui', 'ok'), 'refused'),
    'full POST /containers/ordo-llamacpp-1/restart': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('container.restart', 'ordo-llamacpp-1', 'refused'), 'refused'),
    "full POST /containers/ordo-llamacpp-1/restart {'confirm': True}": (200, ('action', 'container', 'ok'), None, ('container.restart', 'ordo-llamacpp-1', 'ok'), 'refused'),
    "full POST /containers/ordo-caddy-1/restart {'confirm': True}": (200, ('action', 'container', 'members', 'ok'), None, ('container.restart', 'ordo-caddy-1', 'ok'), 'refused'),
    "leased POST /containers/ordo-llamacpp-1/restart {'confirm': True}": (409, ('error', 'lease_holders'), "'llamacpp' is evicted for a GPU lease held by ['gate-comfyui']; starting it would put two tenants on one card. The scheduler restores it when the lease is released.", ('container.restart', 'ordo-llamacpp-1', 'refused'), 'refused'),
    'full POST /compose/up': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('compose.up', '', 'refused'), 'refused'),
    "full POST /compose/up {'confirm': True}": (200, ('action', 'ok'), None, ('compose.up', '', 'ok'), 'refused'),
    "full POST /compose/up {'confirm': True, 'service': ''}": (400, ('error',), "service must be a non-empty compose service name, got ''; omit it to act on the whole stack", ('compose.up', '', 'refused'), 'refused'),
    "full POST /compose/up {'confirm': True, 'service': 'open-webui'}": (200, ('action', 'ok'), None, ('compose.up', 'open-webui', 'ok'), 'refused'),
    'full POST /compose/down': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('compose.down', '', 'refused'), 'refused'),
    "full POST /compose/down {'confirm': True, 'service': 3}": (400, ('error',), 'service must be a non-empty compose service name, got 3; omit it to act on the whole stack', ('compose.down', '3', 'refused'), 'refused'),
    "full POST /compose/down {'confirm': True, 'service': 'open-webui'}": (200, ('action', 'ok'), None, ('compose.down', 'open-webui', 'ok'), 'refused'),
    'full POST /compose/restart': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('compose.restart', '', 'refused'), 'refused'),
    "full POST /compose/restart {'confirm': True, 'service': 'caddy'}": (200, ('action', 'ok'), None, ('compose.restart', 'caddy', 'ok'), 'refused'),
    "leased POST /compose/up {'confirm': True}": (409, ('error', 'lease_holders'), "a GPU lease is active (held by ['gate-comfyui'], evicted ['llamacpp']); a whole-stack start would restart the evicted residents beside it. Name a service, or retry once the lease is released.", ('compose.up', '', 'refused'), 'refused'),
    "leased POST /compose/down {'confirm': True, 'service': 'llamacpp'}": (409, ('error', 'lease_holders'), "'llamacpp' is evicted for a GPU lease held by ['gate-comfyui']; starting it would put two tenants on one card. The scheduler restores it when the lease is released.", ('compose.down', 'llamacpp', 'refused'), 'refused'),
    "leased POST /compose/restart {'confirm': True, 'service': 'open-webui'}": (200, ('action', 'ok'), None, ('compose.restart', 'open-webui', 'ok'), 'refused'),
    "full POST /models/download {'url': 'http://huggingface.co/x/w.safetensors'}": (400, ('error',), 'URL must start with https://', ('models.download', 'w.safetensors', 'refused'), 'runs'),
    "full POST /models/download {'url': 'https://evil.example.com/w.safetensors'}": (400, ('error',), "Host 'evil.example.com' not in allowed list. Allowed: cdn-lfs-eu-1.huggingface.co, cdn-lfs-us-1.huggingface.co, cdn-lfs.huggingface.co, civitai.com, github.com, hf-mirror.com, huggingface.co, objects.githubusercontent.com", ('models.download', 'w.safetensors', 'refused'), 'runs'),
    "full POST /models/download {'url': 'https://huggingface.co/x/w.safetensors', 'filename': '../w'}": (400, ('error',), 'Invalid or undetectable filename', ('models.download', '../w', 'refused'), 'runs'),
    "full POST /models/download {'url': 'https://huggingface.co/x/w.safetensors', 'category': 'nope'}": (400, ('error',), "Invalid category. Must be one of: ('checkpoints', 'loras', 'text_encoders', 'latent_upscale_models', 'vae', 'unet', 'clip', 'clip_vision', 'controlnet', 'embeddings', 'upscale_models', 'diffusion_models', 'vae_approx')", ('models.download', 'w.safetensors', 'refused'), 'runs'),
    "full POST /models/download {'url': 'https://huggingface.co/x/lora/w.safetensors'}": (200, ('category', 'filename', 'status'), None, ('models.download', 'w.safetensors', 'ok'), 'runs'),
    'full POST /comfyui/install-node-requirements': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('comfyui_pip_install', '', 'refused'), 'refused'),
    "full POST /comfyui/install-node-requirements {'confirm': True, 'node_path': '../x'}": (400, ('error',), 'Invalid node_path', ('comfyui_pip_install', '../x', 'refused'), 'refused'),
    "full POST /comfyui/install-node-requirements {'confirm': True, 'node_path': 'Pack'}": (404, ('error',), 'No requirements.txt at custom_nodes/Pack/requirements.txt', ('comfyui_pip_install', 'Pack', 'refused'), 'refused'),
    "full POST /comfyui/install-node-requirements {'confirm': True, 'node_path': 'Ready'}": (503, ('error',), "Container 'ordo-comfyui-1' not found - start comfyui first", ('comfyui_pip_install', 'Ready', 'error'), 'refused'),
    "full POST /gpu/assign {'service': 'llamacpp'}": (410, ('error',), "GPU reassignment moved to the render pipeline: set the service's `gpu_pin:` in its manifest and re-render (`ordo render`), then recreate the service. Runtime reassignment was a silent no-op and has been retired.", ('gpu_assign', 'llamacpp', 'refused'), 'runs'),
    'full POST /registry/models/local-chat/assign-gpu': (410, ('error',), "GPU reassignment moved to the render pipeline: set the service's `gpu_pin:` in its manifest and re-render (`ordo render`), then recreate the service. Runtime reassignment was a silent no-op and has been retired.", ('gpu_assign', 'local-chat', 'refused'), 'runs'),
    'full POST /registry/models/a/b/assign-gpu': (410, ('error',), "GPU reassignment moved to the render pipeline: set the service's `gpu_pin:` in its manifest and re-render (`ordo render`), then recreate the service. Runtime reassignment was a silent no-op and has been retired.", ('gpu_assign', 'a/b', 'refused'), 'runs'),
    'full POST /projects/side/containers/side-web-1/restart': (400, ('error',), 'Destructive operation requires confirmation. Set {"confirm": true} in the request body to proceed.', ('project.restart', 'side/side-web-1', 'refused'), 'runs'),
    "full POST /projects/side/containers/side-web-1/restart {'confirm': True}": (200, ('action', 'container', 'ok', 'project'), None, ('project.restart', 'side/side-web-1', 'ok'), 'runs'),
    "full POST /projects/side/containers/nope/restart {'confirm': True}": (404, ('error',), "container 'nope' is not in project 'side'", ('project.restart', 'side/nope', 'refused'), 'runs'),
    "full POST /projects/other/containers/x/restart {'confirm': True}": (404, ('error',), "'other' is not a managed project (ordo.yaml managed_projects: ['side'])", ('project.restart', 'other/x', 'refused'), 'runs'),
    "full POST /projects/side/containers {'confirm': True}": (404, ('error',), 'no route POST /projects/side/containers', ('unknown', '', 'refused'), 'runs'),
    "full POST /projects/side/containers/side-web-1/stop {'confirm': True}": (404, ('error',), 'no route POST /projects/side/containers/side-web-1/stop', ('unknown', '', 'refused'), 'runs'),
    'full GET /nope': (404, ('error',), 'no route GET /nope', None, 'runs'),
    'full GET /services/': (404, ('error',), 'no route GET /services/', None, 'runs'),
    'full GET /models/packs': (404, ('error',), 'no route GET /models/packs', None, 'runs'),
    'full POST /status': (404, ('error',), 'no route POST /status', ('unknown', '', 'refused'), 'runs'),
    'full DELETE /services/llamacpp/restart': (404, ('error',), 'no route DELETE /services/llamacpp/restart', ('restart', 'llamacpp', 'refused'), 'runs'),
    "full PUT /apply {'confirm': True}": (404, ('error',), 'no route PUT /apply', ('apply', 'stack', 'refused'), 'runs'),
    "full PATCH /model-config {'model': 'auto'}": (404, ('error',), 'no route PATCH /model-config', ('model_config', 'auto', 'refused'), 'runs'),
    'full GET /apply': (404, ('error',), 'no route GET /apply', None, 'runs'),
    'bare GET /status': (200, ('gpu', 'manifest'), None, None, 'runs'),
    'bare GET /services': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /services/llamacpp/logs': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /containers': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /containers/ordo-llamacpp-1/logs': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /containers/ordo-llamacpp-1': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /stats/services': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /projects': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /projects/side/containers': (503, ('error',), 'no broker configured', None, 'runs'),
    'bare GET /jobs/history': (200, ('history',), None, None, 'runs'),
    'bare GET /doctor': (200, ('checks', 'ok'), None, None, 'runs'),
    "bare POST /apply {'confirm': True}": (503, ('error',), 'no container backend: this control plane cannot read or recreate containers', ('apply', 'stack', 'error'), 'refused'),
    "bare POST /apply {'dry_run': True}": (503, ('error',), 'no container backend: this control plane cannot read or recreate containers', ('apply', 'stack', 'error'), 'refused'),
    "bare POST /model-config {'model': 'qwen2.5-0.5b-instruct-q4'}": (200, ('active_model', 'apply', 'ctx_size', 'ok', 'warnings', 'wrote'), None, ('model_config', 'qwen2.5-0.5b-instruct-q4', 'ok'), 'refused'),
    "bare POST /plugins/searxng/enable {'confirm': True}": (200, ('added', 'already_rendered', 'apply', 'compose_profile', 'missing_secrets', 'ok', 'plugin', 'services', 'wants_secrets', 'warnings', 'wrote'), None, ('plugin.enable', 'searxng', 'ok'), 'refused'),
    "bare POST /jobs {'id': 'render', 'vram_gb': 18}": (503, ('error',), 'no broker configured', ('lease.request', 'render', 'error'), 'runs'),
    "bare POST /jobs/complete {'id': 'render'}": (503, ('error',), 'no broker configured', ('lease.release', 'render', 'error'), 'runs'),
    "bare POST /jobs/heartbeat {'id': 'render'}": (503, ('error',), 'no broker configured', ('lease.heartbeat', 'render', 'error'), 'runs'),
    "bare POST /services/llamacpp/start {'confirm': True}": (503, ('error',), 'no broker configured', ('start', 'llamacpp', 'error'), 'refused'),
    "bare POST /services/llamacpp/stop {'dry_run': True}": (503, ('error',), 'no broker configured', ('stop', 'llamacpp', 'error'), 'refused'),
    "bare POST /containers/ordo-llamacpp-1/restart {'confirm': True}": (503, ('error',), 'no broker configured', ('container.restart', 'ordo-llamacpp-1', 'error'), 'refused'),
    "bare POST /compose/up {'confirm': True}": (503, ('error',), 'no broker configured', ('compose.up', '', 'error'), 'refused'),
    "bare POST /comfyui/install-node-requirements {'confirm': True, 'node_path': 'Ready'}": (503, ('error',), 'no container backend', ('comfyui_pip_install', 'Ready', 'error'), 'refused'),
    "bare POST /projects/side/containers/side-web-1/restart {'confirm': True}": (503, ('error',), 'no broker configured', ('project.restart', 'side/side-web-1', 'error'), 'runs'),
}

METRICS_EXPECTED: dict[tuple, tuple] = {
    ('full', 'GET', False): (200, 'text/plain; version=0.0.4; charset=utf-8', ('ordo_gpu_leases_running', 'ordo_gpu_leases_queued', 'ordo_gpu_lease_held_seconds', 'ordo_gpu_lease_ttl_remaining_seconds', 'ordo_gpu_resident_evicted', 'ordo_container_running', 'ordo_container_unhealthy', 'ordo_container_restarts_total', 'ordo_filesystem_size_bytes', 'ordo_filesystem_free_bytes', 'ordo_filesystem_avail_bytes', 'ordo_tls_cert_not_after_timestamp_seconds', 'ordo_netns_repairs_total', 'ordo_netns_orphans', 'ordo_metrics_collector_ok'), ('ordo_metrics_collector_ok{collector="containers"} 1', 'ordo_metrics_collector_ok{collector="disk_docker"} 1', 'ordo_metrics_collector_ok{collector="disk_host"} 1', 'ordo_metrics_collector_ok{collector="restarts"} 1', 'ordo_metrics_collector_ok{collector="tls_cert_edge"} 1')),
    ('full', 'GET', True): (200, 'text/plain; version=0.0.4; charset=utf-8', ('ordo_gpu_leases_running', 'ordo_gpu_leases_queued', 'ordo_gpu_lease_held_seconds', 'ordo_gpu_lease_ttl_remaining_seconds', 'ordo_gpu_resident_evicted', 'ordo_container_running', 'ordo_container_unhealthy', 'ordo_container_restarts_total', 'ordo_filesystem_size_bytes', 'ordo_filesystem_free_bytes', 'ordo_filesystem_avail_bytes', 'ordo_tls_cert_not_after_timestamp_seconds', 'ordo_netns_repairs_total', 'ordo_netns_orphans', 'ordo_metrics_collector_ok'), ('ordo_metrics_collector_ok{collector="containers"} 1', 'ordo_metrics_collector_ok{collector="disk_docker"} 1', 'ordo_metrics_collector_ok{collector="disk_host"} 1', 'ordo_metrics_collector_ok{collector="restarts"} 1', 'ordo_metrics_collector_ok{collector="tls_cert_edge"} 1')),
    ('bare', 'GET', False): (200, 'text/plain; version=0.0.4; charset=utf-8', ('ordo_filesystem_size_bytes', 'ordo_filesystem_free_bytes', 'ordo_filesystem_avail_bytes', 'ordo_tls_cert_not_after_timestamp_seconds', 'ordo_metrics_collector_ok'), ('ordo_metrics_collector_ok{collector="containers"} 0', 'ordo_metrics_collector_ok{collector="disk_docker"} 1', 'ordo_metrics_collector_ok{collector="disk_host"} 1', 'ordo_metrics_collector_ok{collector="restarts"} 0', 'ordo_metrics_collector_ok{collector="tls_cert_edge"} 1')),
    ('full', 'POST', True): (404, 'application/json', (), ()),
}
