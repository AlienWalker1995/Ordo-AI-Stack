"""Regression guard for the ops-api `/stats/services` widget fix (concurrent per-container sampling).

The V1-parity dashboard's hw-stat service-pressure bar calls ops-api `/stats/services` with a 3s
timeout. That endpoint samples each running container via `c.stats(stream=False)` — ~1-2s per
container while the daemon computes the CPU delta. The original loop was SEQUENTIAL, so with this
stack's ~24 running services it took ~48s (past the 3s timeout) and the dashboard fell back to its
`_empty_payload` branch: every service rendered `running:false`. The fix fans the independent
samples out across a bounded thread pool so wall time collapses to ~one sample regardless of N.

These tests load the ops-api's `main.py` (build context `docker/ops-api/`) with a stubbed docker SDK
and assert BOTH the correctness (all services seeded, runners flipped+filled) and the concurrency
(wall time ~= one sample, not N). Skipped where fastapi/docker aren't installed (the bare substrate
dev suite) — they run in the ops-api runtime-deps environment.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="ops-api runtime deps (fastapi/docker) not present")

ROOT = Path(__file__).resolve().parent.parent
_OPS_API = ROOT / "docker" / "ops-api"


def _load_main(monkeypatch):
    """Import the ops-api main.py in isolation with a stubbed docker SDK + token set."""
    # Stub the docker SDK (main.py imports it at module load).
    docker_stub = MagicMock()
    errors_mod = types.ModuleType("docker.errors")

    class _NotFound(Exception):
        pass

    errors_mod.NotFound = _NotFound
    docker_stub.errors = errors_mod
    docker_stub.from_env.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "docker", docker_stub)
    monkeypatch.setitem(sys.modules, "docker.errors", errors_mod)
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    # main.py loads sibling modules (audit etc.) by name — make the dir importable.
    monkeypatch.syspath_prepend(str(_OPS_API))

    spec = importlib.util.spec_from_file_location("ops_api_main_under_test", _OPS_API / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeContainer:
    def __init__(self, svc, status="running", stats_delay=0.0):
        self.name = f"ordo-{svc}-1"
        self.status = status
        self.labels = {"com.docker.compose.service": svc}
        self._delay = stats_delay

    def stats(self, stream=False):
        import time
        if self._delay:
            time.sleep(self._delay)
        # Minimal shape _cpu_pct_from_stats / _mem_from_stats parse without raising.
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": 200, "percpu_usage": [1]},
                          "system_cpu_usage": 2000, "online_cpus": 1},
            "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
            "memory_stats": {"usage": 2_000_000_000, "limit": 8_000_000_000, "stats": {"inactive_file": 0}},
        }


def _no_gpu(m, monkeypatch, containers):
    monkeypatch.setattr(m, "_get_containers", lambda: containers)
    monkeypatch.setattr(m, "_nvml_vraam_by_pid", lambda: ({}, {
        "total_gb": 0.0, "used_gb": 0.0, "utilization_pct": 0, "per_pid_available": False}))


def _call(m):
    from fastapi.testclient import TestClient
    client = TestClient(m.app)
    return client.get("/stats/services", headers={"Authorization": "Bearer test-token"})


def test_stats_services_seeds_all_and_flips_runners(monkeypatch):
    m = _load_main(monkeypatch)
    conts = [_FakeContainer("llamacpp"), _FakeContainer("comfyui", status="exited"),
             _FakeContainer("dashboard")]
    _no_gpu(m, monkeypatch, conts)
    r = _call(m)
    assert r.status_code == 200
    svcs = r.json()["services"]
    # Every declared service is present; the exited one is seeded running:false with zeroed metrics.
    assert set(svcs) == {"llamacpp", "comfyui", "dashboard"}
    assert svcs["comfyui"]["running"] is False and svcs["comfyui"]["cpu_pct"] == 0.0
    # Runners flipped to running:true and filled with real (non-null) numbers.
    assert svcs["llamacpp"]["running"] is True
    assert svcs["llamacpp"]["cpu_pct"] > 0 and svcs["llamacpp"]["mem_gb"] > 0
    assert svcs["dashboard"]["running"] is True


def test_stats_services_samples_concurrently_not_sequentially(monkeypatch):
    m = _load_main(monkeypatch)
    # 8 running containers, each with a 0.5s sample delay. Sequential = ~4s; concurrent = ~0.5s.
    conts = [_FakeContainer(f"svc{i}", stats_delay=0.5) for i in range(8)]
    _no_gpu(m, monkeypatch, conts)
    import time
    t = time.time()
    r = _call(m)
    elapsed = time.time() - t
    assert r.status_code == 200
    assert all(v["running"] for v in r.json()["services"].values())
    # Concurrent fan-out: well under the 8×0.5s=4s a sequential loop would take. Generous bound so a
    # loaded CI box stays non-flaky while a regression to sequential still fails hard.
    assert elapsed < 2.0, f"stats/services took {elapsed:.2f}s — per-container sampling regressed to sequential"
