"""The comfyui-mcp entrypoint waits for ComfyUI instead of exiting.

Upstream server.py probes ComfyUI five times (about 30 s) and then exits 1. ComfyUI takes minutes
to boot, so every deploy that recreated both crash-looped mcp-comfyui through its restart policy
(7 restarts on 2026-09-29). depends_on cannot prevent it: `ordo apply` / `ordo recreate` use
--no-deps, and a Docker daemon restart ignores depends_on. services/comfyui-mcp/start.py waits
with no deadline and only then hands the process to server.py.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "comfyui-mcp"


def _load_start():
    spec = importlib.util.spec_from_file_location("comfyui_mcp_start", SERVICE / "start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


start = _load_start()


class _Answer:
    """What the fake ComfyUI (or its gate) answers on the probe path."""

    status = 200
    body = b"{}"


@pytest.fixture
def fake_comfyui():
    answer = _Answer()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's naming)
            if self.path != start.PROBE_PATH:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(answer.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(answer.body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", answer
    finally:
        server.shutdown()
        server.server_close()


def test_a_comfyui_that_answers_the_probe_is_available(fake_comfyui):
    url, answer = fake_comfyui
    answer.body = json.dumps({"CheckpointLoaderSimple": {"input": {}}}).encode()
    assert start.comfyui_available(url) is True


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (502, b'{"detail": "upstream comfyui is not reachable"}'),  # the gate while ComfyUI boots
        (503, b"starting"),
        (200, b"<html>not json</html>"),
        (200, b"[]"),
    ],
)
def test_a_comfyui_that_is_not_ready_is_not_available(fake_comfyui, status, body):
    url, answer = fake_comfyui
    answer.status, answer.body = status, body
    assert start.comfyui_available(url) is False


def test_nothing_listening_is_not_available():
    # Port 9 (discard) on loopback: nothing listens there, so the connection is refused.
    assert start.comfyui_available("http://127.0.0.1:9", timeout=1) is False


def test_it_keeps_waiting_well_past_upstreams_five_tries_and_never_exits():
    answers = iter([False] * 40 + [True])
    delays: list[float] = []

    start.wait_for_comfyui("http://comfyui-gate:8188", check=lambda _url: next(answers), sleep=delays.append)

    assert len(delays) == 40
    assert delays[:4] == [2, 4, 8, start.MAX_DELAY_S]
    assert max(delays) == start.MAX_DELAY_S


def test_it_does_not_sleep_when_comfyui_is_already_up():
    delays: list[float] = []
    start.wait_for_comfyui("http://comfyui-gate:8188", check=lambda _url: True, sleep=delays.append)
    assert delays == []


def test_main_hands_the_process_to_the_upstream_server_once_comfyui_answers(monkeypatch):
    calls: list[str] = []
    monkeypatch.setenv("COMFYUI_URL", "http://comfyui-gate:8188")
    monkeypatch.setattr(start, "wait_for_comfyui", lambda url: calls.append(f"wait {url}"))
    monkeypatch.setattr(start.os, "execv", lambda exe, argv: calls.append(f"exec {argv[1:]}"))

    start.main([])

    assert calls == ["wait http://comfyui-gate:8188", "exec ['server.py']"]


def test_main_refuses_without_a_comfyui_url(monkeypatch):
    monkeypatch.delenv("COMFYUI_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        start.main([])
    assert "COMFYUI_URL" in str(exc.value)


def test_the_image_starts_through_the_waiting_entrypoint():
    dockerfile = (SERVICE / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY start\.py start\.py$", dockerfile, re.MULTILINE)
    cmd_lines = [line for line in dockerfile.splitlines() if line.startswith("CMD ")]
    assert cmd_lines == ['CMD ["python", "start.py"]']
