"""services/voice/start.sh: the TTS server is the main process and a stop reaches it at once.

The warm-up runs beside the server, so a SIGTERM during warm-up (docker stop, or the GPU arbiter
yielding the secondary card) is forwarded immediately; the script exits only after the server has
finished shutting down, with the server's status; and a server that dies at start ends the
container right away instead of after the warm-up's 600 s poll.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "services" / "voice" / "start.sh"

pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None
                                or shutil.which("python3") is None,
                                reason="needs a POSIX sh, python3 and POSIX signals")

# A stand-in for Chatterbox-TTS-Server: never serves, takes 0.5 s to shut down cleanly on SIGTERM
# and records that it did, or crashes at once when CRASH is set.
FAKE_SERVER = """
import os, signal, sys, time
if os.environ.get("CRASH"):
    sys.exit(3)
def stop(*_):
    time.sleep(0.5)
    open("stopped-cleanly", "w").close()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(0.1)
"""


def start(tmp_path: Path, **env: str) -> subprocess.Popen:
    (tmp_path / "server.py").write_text(FAKE_SERVER)
    script = tmp_path / "start.sh"
    script.write_bytes(START.read_bytes())
    return subprocess.Popen(["sh", str(script)], cwd=tmp_path,
                            env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_a_stop_during_warm_up_reaches_the_server_and_waits_for_it(tmp_path):
    proc = start(tmp_path)
    time.sleep(1.5)                      # the warm-up is polling a server that never answers
    t0 = time.time()
    proc.send_signal(signal.SIGTERM)
    status = proc.wait(timeout=10)
    assert time.time() - t0 < 5, "the stop waited for the warm-up instead of reaching the server"
    assert (tmp_path / "stopped-cleanly").exists(), "the script exited before the server shut down"
    assert status == 0, f"exited {status}, not the server's status"


def test_a_server_that_dies_at_start_ends_the_container_promptly(tmp_path):
    proc = start(tmp_path, CRASH="1")
    status = proc.wait(timeout=15)       # not the warm-up's 600 s poll
    assert status == 3, f"exited {status}, not the server's crash status"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
