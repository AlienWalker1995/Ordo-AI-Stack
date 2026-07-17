"""lease-exec wrapper behavior against a stub control plane (real subprocess, real HTTP)."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

LEASE_EXEC = str(Path(__file__).resolve().parent.parent / "assets" / "lease-exec.py")


class _StubOps:
    """Minimal ops-controller: admits after `admit_after_polls` GET /status calls."""

    def __init__(self, admit_after_polls=0, reject=False):
        self.admit_after_polls = admit_after_polls
        self.reject = reject
        self.jobs, self.heartbeats, self.completes = [], [], []
        self.polls = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _status(self):
        jid = self.jobs[-1] if self.jobs else None
        if self.reject:
            return {"running": [], "rejected": [jid]}
        admitted = self.polls >= self.admit_after_polls
        return {"running": [{"id": jid}] if (jid and admitted) else [], "rejected": []}

    def _make_handler(stub):  # noqa: N805 — closure over the stub instance
        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.polls += 1
                self._send(200, {"gpu": stub._status()})    # GET /status nests under "gpu"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/jobs":
                    stub.jobs.append(body["id"])
                    self._send(200, stub._status())          # POST returns bare status
                elif self.path == "/jobs/heartbeat":
                    stub.heartbeats.append(body["id"])
                    self._send(200, stub._status())
                elif self.path == "/jobs/complete":
                    stub.completes.append(body["id"])
                    self._send(200, stub._status())
                else:
                    self._send(404, {"error": "no route"})

            def log_message(self, *_):
                pass

        return Handler


def _env(stub_url, extra=None):
    env = {
        "OPS_CONTROLLER_URL": stub_url,
        "ORDO_LEASE_VRAM_GB": "30",
        "ORDO_LEASE_KIND": "training",
        "ORDO_LEASE_JOB_ID": "train-test",
        "ORDO_LEASE_POLL_S": "0.05",
        "ORDO_LEASE_HEARTBEAT_S": "0.1",
        "ORDO_LEASE_ACQUIRE_TIMEOUT_S": "5",
        "PATH": os.environ["PATH"],
    }
    if os.environ.get("SYSTEMROOT"):  # Windows: sockets need it
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    env.update(extra or {})
    return env


def _run(stub_url, child_code, marker, timeout=30):
    code = child_code.replace("MARKER", str(marker).replace("\\", "/"))
    return subprocess.run(
        [sys.executable, LEASE_EXEC, "-c", code],
        env=_env(stub_url), capture_output=True, text=True, timeout=timeout,
    )


def test_runs_child_under_lease_and_completes(tmp_path):
    stub = _StubOps()
    marker = tmp_path / "ran.txt"
    r = _run(stub.url, "open('MARKER','w').write('ok')", marker)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "ok"
    assert stub.jobs == ["train-test"]
    assert stub.completes == ["train-test"]


def test_child_exit_code_passes_through_and_lease_still_released(tmp_path):
    stub = _StubOps()
    r = _run(stub.url, "import sys; sys.exit(7)", tmp_path / "x")
    assert r.returncode == 7
    assert stub.completes == ["train-test"]


def test_waits_for_admission_before_running_child(tmp_path):
    stub = _StubOps(admit_after_polls=3)
    marker = tmp_path / "ran.txt"
    r = _run(stub.url, "open('MARKER','w').write('ok')", marker)
    assert r.returncode == 0, r.stderr
    assert stub.polls >= 3                      # it actually waited through the queue
    assert marker.exists()


def test_rejected_job_fails_loudly_without_running_child(tmp_path):
    stub = _StubOps(reject=True)
    marker = tmp_path / "ran.txt"
    r = _run(stub.url, "open('MARKER','w').write('ok')", marker)
    assert r.returncode != 0
    assert not marker.exists()                  # GPU work must never run unleased
    assert stub.completes == []


def test_unreachable_controller_fails_loudly_without_running_child(tmp_path):
    marker = tmp_path / "ran.txt"
    r = _run("http://127.0.0.1:1", "open('MARKER','w').write('ok')", marker)
    assert r.returncode != 0
    assert not marker.exists()


def test_heartbeats_while_child_runs(tmp_path):
    stub = _StubOps()
    r = _run(stub.url, "import time; time.sleep(0.6)", tmp_path / "x")
    assert r.returncode == 0, r.stderr
    assert len(stub.heartbeats) >= 2            # 0.6s child at 0.1s beat interval


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal forwarding")
def test_sigint_is_forwarded_and_lease_released(tmp_path):
    stub = _StubOps()
    marker = tmp_path / "sig.txt"
    child = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: (open('MARKER','w').write('int'), sys.exit(0)))\n"
        "time.sleep(30)\n"
    )
    p = subprocess.Popen(
        [sys.executable, LEASE_EXEC, "-c", child.replace("MARKER", str(marker))],
        env=_env(stub.url), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.time() + 10
    while not stub.jobs and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)                             # let the child reach its sleep
    p.send_signal(signal.SIGINT)                # what the AI-toolkit Stop button sends
    p.wait(timeout=10)
    assert marker.read_text() == "int"          # child received the forwarded SIGINT
    assert stub.completes == ["train-test"]     # and the lease was released


@pytest.mark.skipif(sys.platform == "win32", reason="/proc-based stall detection is Linux-only")
def test_stalled_child_is_killed_and_lease_released(tmp_path):
    # A child with frozen CPU+IO (pure sleep) past ORDO_LEASE_STALL_S must be terminated by
    # the wrapper — the zombie-lease case: heartbeats alone would renew forever.
    stub = _StubOps()
    marker = tmp_path / "ran.txt"
    env = _env(stub.url, {"ORDO_LEASE_STALL_S": "2", "ORDO_LEASE_HEARTBEAT_S": "0.5"})
    r = subprocess.run(
        [sys.executable, LEASE_EXEC, "-c", "import time; time.sleep(60); open('MARKER','w').write('no')".replace("MARKER", str(marker))],
        env=env, capture_output=True, text=True, timeout=40,
    )
    assert r.returncode != 0
    assert not marker.exists()                  # child never reached the end
    assert "stall" in r.stderr.lower()
    assert stub.completes == ["train-test"]     # lease released after the kill


@pytest.mark.skipif(sys.platform == "win32", reason="/proc-based stall detection is Linux-only")
def test_active_child_is_not_stall_killed(tmp_path):
    # A child doing real work (CPU busy) must survive a tight stall window.
    stub = _StubOps()
    marker = tmp_path / "ran.txt"
    code = ("import time\n"
            "t = time.time()\n"
            "x = 0\n"
            "while time.time() - t < 4: x += 1\n"
            f"open(r'{tmp_path / 'ran.txt'}', 'w').write('done')\n")
    env = _env(stub.url, {"ORDO_LEASE_STALL_S": "2", "ORDO_LEASE_HEARTBEAT_S": "0.5"})
    r = subprocess.run([sys.executable, LEASE_EXEC, "-c", code],
                       env=env, capture_output=True, text=True, timeout=40)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "done"
