"""The ComfyUI idle-RAM reclaim cron (scripts/hermes/comfyui_idle_reclaim.sh) against a stub
ops-controller and a stub ComfyUI gate: a real bash subprocess and real HTTP.

SEC-1 step 5: the script read ComfyUI's memory with `docker stats` over the raw socket Hermes is
losing. It now reads `GET /stats/services` from ops-controller, a route the scoped `hermes`
principal may call, and never runs `docker`. A fake `docker` on PATH records any call.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes" / "comfyui_idle_reclaim.sh"
TOKEN = "idle-reclaim-token"
BASH = shutil.which("bash")
CURL = shutil.which("curl")

pytestmark = pytest.mark.skipif(not (BASH and CURL), reason="needs bash and curl")


class Stub:
    """ops-controller (/stats/services, /status, POST /services/comfyui/restart) and the ComfyUI
    gate (/queue) on one port."""

    def __init__(self, *, mem_gb=13.0, stats_status=200, gpu=None, queue=None):
        self.mem_gb = mem_gb
        self.stats_status = stats_status
        self.gpu = gpu if gpu is not None else {"running": [], "queued": [], "evicted_residents": {}}
        self.queue = queue if queue is not None else {"queue_running": [], "queue_pending": []}
        self.restarts: list[dict] = []
        self.paths: list[str] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(stub):  # noqa: N805 - closure over the stub
        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.paths.append(self.path)
                if self.path == "/queue":
                    return self._send(200, stub.queue)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"error": "missing or invalid bearer token"})
                if self.path == "/stats/services":
                    if stub.stats_status != 200:
                        return self._send(stub.stats_status, {"error": "docker stats failed"})
                    return self._send(200, {"gpu": {}, "services": {
                        "comfyui": {"cpu_pct": 0.1, "mem_gb": stub.mem_gb, "mem_pct": 10.0,
                                    "vram_gb": 0.0, "vram_pct": 0.0, "running": True},
                        "comfyui-gate": {"cpu_pct": 0.0, "mem_gb": 99.0, "mem_pct": 0.0,
                                         "vram_gb": 0.0, "vram_pct": 0.0, "running": True}}})
                if self.path == "/status":
                    return self._send(200, {"gpu": stub.gpu})
                return self._send(404, {"error": "no route"})

            def do_POST(self):
                stub.paths.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"error": "missing or invalid bearer token"})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/services/comfyui/restart":
                    stub.restarts.append(body)
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "no route"})

            def log_message(self, *_):
                pass

        return Handler

    def close(self):
        self.server.shutdown()


def _bin_dir(tmp_path: Path) -> Path:
    """A PATH entry with a `docker` that records every call, and a `python3` when the host has
    none (Windows has `python`; CI has python3)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "docker-called"
    (bin_dir / "docker").write_text(f'#!/bin/bash\necho "$@" >> "{marker.as_posix()}"\nexit 1\n', encoding="utf-8")
    if not shutil.which("python3") or sys.platform == "win32":
        (bin_dir / "python3").write_text(f'#!/bin/bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
                                         encoding="utf-8")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    return bin_dir


def _bash_path(path: Path) -> str:
    """A path as bash sees it. On Windows (Git Bash) `C:/x` must become `/c/x`: the drive colon
    would otherwise split a PATH entry in two."""
    posix = path.resolve().as_posix()
    if len(posix) > 1 and posix[1] == ":":
        return f"/{posix[0].lower()}{posix[2:]}"
    return posix


def _run(stub: Stub, tmp_path: Path) -> subprocess.CompletedProcess:
    bin_dir = _bin_dir(tmp_path)
    env = {**os.environ, "OPS_CONTROLLER_URL": stub.url, "COMFYUI_URL": stub.url,
           "OPS_CONTROLLER_TOKEN": TOKEN}
    script = _bash_path(SCRIPT)
    return subprocess.run([BASH, "-c", f'export PATH="{_bash_path(bin_dir)}:$PATH"; bash "{script}"'],
                          env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def stub():
    servers = []

    def make(**kwargs):
        server = Stub(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _docker_called(tmp_path: Path) -> bool:
    return (tmp_path / "docker-called").exists()


def test_an_idle_bloated_comfyui_is_restarted_through_ops_controller(stub, tmp_path):
    ops = stub(mem_gb=13.2)
    r = _run(ops, tmp_path)
    assert r.returncode == 0, r.stderr
    assert ops.restarts == [{"confirm": True}]
    assert "Reclaimed ComfyUI RAM" in r.stdout and "13.2" in r.stdout
    assert "/stats/services" in ops.paths
    assert not _docker_called(tmp_path)


def test_a_small_comfyui_is_left_alone_silently(stub, tmp_path):
    ops = stub(mem_gb=5.0)
    r = _run(ops, tmp_path)
    assert (r.returncode, r.stdout, ops.restarts) == (0, "", [])
    assert not _docker_called(tmp_path)


@pytest.mark.parametrize("mem_gb, restarted", [(11.99, False), (12.0, True)])
def test_the_threshold_is_twelve_gigabytes(stub, tmp_path, mem_gb, restarted):
    ops = stub(mem_gb=mem_gb)
    _run(ops, tmp_path)
    assert bool(ops.restarts) is restarted


def test_unreadable_stats_count_as_busy(stub, tmp_path):
    ops = stub(stats_status=500)
    r = _run(ops, tmp_path)
    assert (r.stdout, ops.restarts) == ("", [])
    assert not _docker_called(tmp_path)


def test_a_held_lease_blocks_the_restart(stub, tmp_path):
    ops = stub(gpu={"running": [{"id": "render-1"}], "queued": [], "evicted_residents": {"llamacpp": 20.0}})
    r = _run(ops, tmp_path)
    assert (r.stdout, ops.restarts) == ("", [])


def test_a_busy_queue_blocks_the_restart(stub, tmp_path):
    ops = stub(queue={"queue_running": [["x"]], "queue_pending": []})
    r = _run(ops, tmp_path)
    assert (r.stdout, ops.restarts) == ("", [])


def test_the_script_never_calls_docker():
    text = SCRIPT.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("docker " in line for line in code)
