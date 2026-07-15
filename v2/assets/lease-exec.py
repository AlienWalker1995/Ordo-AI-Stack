#!/usr/bin/env python3
"""Ordo GPU lease wrapper — run any command under an ops-controller VRAM lease.

Generic and stdlib-only: any GPU plugin bind-mounts this file (or prefixes its command with it)
to route GPU-heavy work through the scheduler. Nothing here is specific to any one service.
First consumer: the ai-toolkit plugin mounts it at /app/ai-toolkit/venv/bin/python — the path the
AI-toolkit UI prefers when spawning trainers — so every training run leases the GPU first.

Contract (env):
  OPS_CONTROLLER_URL            scheduler base URL (required), e.g. http://ops-controller:9000
  OPS_CONTROLLER_TOKEN          optional bearer token (sent when set)
  ORDO_LEASE_VRAM_GB            VRAM to lease (required); ~full card => exclusive lease
  ORDO_LEASE_KIND               job kind label (default "generic")
  ORDO_LEASE_JOB_ID             explicit job id (default: lease-$AITK_JOB_ID, else lease-<random>)
  ORDO_LEASE_ACQUIRE_TIMEOUT_S  max wait for admission (default 3600)
  ORDO_LEASE_POLL_S             admission poll interval (default 5)
  ORDO_LEASE_HEARTBEAT_S        heartbeat interval (default 60)

Behavior: POST /jobs → poll GET /status until admitted (fail on rejected/timeout) → run
`sys.executable argv[1:]` → heartbeat while it runs (re-acquiring if the controller lost the
lease, e.g. across a restart) → forward SIGINT/SIGTERM to the child → POST /jobs/complete on
exit, propagating the child's exit code. If the controller is unreachable up front, exit non-zero
WITHOUT running the command: GPU work must never run unleased (arbitration is not optional).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


def _log(msg: str) -> None:
    print(f"[ordo-lease] {msg}", file=sys.stderr, flush=True)


def _req(method: str, path: str, body: dict | None = None) -> dict:
    base = os.environ["OPS_CONTROLLER_URL"].rstrip("/")
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    token = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def _gpu(payload: dict) -> dict:
    # GET /status nests the scheduler block under "gpu"; POST /jobs* returns it bare.
    return payload.get("gpu", payload)


def _acquire(job_id: str, vram_gb: float, kind: str) -> None:
    poll_s = float(os.environ.get("ORDO_LEASE_POLL_S", "5"))
    timeout_s = float(os.environ.get("ORDO_LEASE_ACQUIRE_TIMEOUT_S", "3600"))
    st = _gpu(_req("POST", "/jobs", {"id": job_id, "vram_gb": vram_gb, "kind": kind}))
    deadline = time.monotonic() + timeout_s
    while True:
        if job_id in [j.get("id") for j in st.get("running", [])]:
            _log(f"lease '{job_id}' admitted ({vram_gb} GB, kind={kind})")
            return
        if job_id in st.get("rejected", []):
            raise SystemExit(
                f"[ordo-lease] job '{job_id}' REJECTED by scheduler ({vram_gb} GB does not fit)"
            )
        if time.monotonic() >= deadline:
            raise SystemExit(f"[ordo-lease] timed out after {timeout_s}s waiting for lease '{job_id}'")
        time.sleep(poll_s)
        st = _gpu(_req("GET", "/status"))


def _complete(job_id: str) -> None:
    for attempt in range(5):
        try:
            _req("POST", "/jobs/complete", {"id": job_id})
            _log(f"lease '{job_id}' released")
            return
        except (urllib.error.URLError, OSError) as e:
            _log(f"release attempt {attempt + 1}/5 failed: {e}")
            time.sleep(2)
    _log(f"WARNING: could not release lease '{job_id}' — scheduler sweep will reclaim it")


def _heartbeat_loop(job_id: str, vram_gb: float, kind: str, child: subprocess.Popen) -> None:
    interval = float(os.environ.get("ORDO_LEASE_HEARTBEAT_S", "60"))
    while child.poll() is None:
        time.sleep(interval)
        if child.poll() is not None:
            return
        try:
            _req("POST", "/jobs/heartbeat", {"id": job_id})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Controller lost the lease (e.g. restart wiped in-memory state). Re-acquire so
                # the resident is re-evicted rather than restored into our VRAM.
                _log(f"lease '{job_id}' lost (404) — re-acquiring")
                try:
                    _req("POST", "/jobs", {"id": job_id, "vram_gb": vram_gb, "kind": kind})
                except (urllib.error.URLError, OSError) as e2:
                    _log(f"re-acquire failed: {e2}")
            else:
                _log(f"heartbeat failed: HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            _log(f"heartbeat failed: {e}")


def main() -> int:
    if len(sys.argv) < 2:
        _log("usage: lease-exec.py <program-args...>")
        return 2
    vram_env = os.environ.get("ORDO_LEASE_VRAM_GB", "").strip()
    if not os.environ.get("OPS_CONTROLLER_URL", "").strip() or not vram_env:
        _log("OPS_CONTROLLER_URL and ORDO_LEASE_VRAM_GB are required — refusing to run unleased")
        return 2
    vram_gb = float(vram_env)
    kind = os.environ.get("ORDO_LEASE_KIND", "generic")
    job_id = os.environ.get("ORDO_LEASE_JOB_ID", "").strip() or (
        f"lease-{os.environ['AITK_JOB_ID']}"
        if os.environ.get("AITK_JOB_ID")
        else f"lease-{uuid.uuid4().hex[:8]}"
    )

    try:
        _acquire(job_id, vram_gb, kind)
    except (urllib.error.URLError, OSError) as e:
        _log(f"cannot reach ops-controller: {e} — refusing to run unleased")
        return 3

    child = subprocess.Popen([sys.executable] + sys.argv[1:])

    def _forward(signum: int, _frame) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, _forward)       # AI-toolkit's Stop button sends SIGINT
    signal.signal(signal.SIGTERM, _forward)

    threading.Thread(target=_heartbeat_loop, args=(job_id, vram_gb, kind, child), daemon=True).start()
    try:
        rc = child.wait()
    finally:
        _complete(job_id)
    return rc


if __name__ == "__main__":
    sys.exit(main())
