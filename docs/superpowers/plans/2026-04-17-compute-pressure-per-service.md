# Compute Pressure: Per-Service Attribution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard's GPU-only `COMPUTE PRESSURE` panel with a per-service roster showing CPU%, RAM%, and VRAM% for every toolkit service, sorted by current pressure so the biggest hog is always on top.

**Architecture:** New `GET /stats/services` in `ops-controller` merges `docker stats` (CPU/RAM per container) with NVML per-PID VRAM (joined via `docker top <container>`). A new no-auth dashboard proxy `GET /api/hardware/service-pressure` annotates the payload with display names + `has_gpu` flags from `services_catalog.py` and returns it to the frontend, which re-renders the panel as a sorted per-service grid. Preserves the existing rule that the dashboard container never touches `docker.sock` directly.

**Tech Stack:** FastAPI, Python `docker` SDK (already installed in ops-controller), `pynvml`, psutil, vanilla JS in `dashboard/static/index.html`.

**Spec:** `docs/superpowers/specs/2026-04-17-compute-pressure-per-service-design.md`

---

## File structure

**Create**
- `tests/test_ops_controller_stats.py` — unit tests for stats helpers + `/stats/services` endpoint.
- `tests/test_dashboard_service_pressure.py` — unit tests for dashboard proxy endpoint.

**Modify**
- `ops-controller/main.py` — add four helpers (`_cpu_pct_from_stats`, `_mem_from_stats`, `_container_host_pids`, `_nvml_vram_by_pid`) and one route (`GET /stats/services`).
- `dashboard/services_catalog.py` — add `has_gpu: bool` to every SERVICES entry.
- `dashboard/app.py` — add `GET /api/hardware/service-pressure`; delete `_gpu_processes`, `_pid_to_service_label`, and `GET /api/hardware/gpu-processes`.
- `dashboard/static/index.html` — replace `#compute-pressure` markup + styles + JS refresh function.
- `CHANGELOG.md` — entry under current dev section.

**Delete**
- `tests/test_dashboard_gpu_processes.py` — tests for the removed endpoint.

---

## Task 1: Add `has_gpu` flag to SERVICES catalog

**Files:**
- Modify: `dashboard/services_catalog.py:31-54`

- [ ] **Step 1: Write the failing test**

Create a quick assertion at the bottom of an existing dashboard test, or run inline:

```bash
python -c "from dashboard.services_catalog import SERVICES; assert all('has_gpu' in s for s in SERVICES), [s['id'] for s in SERVICES if 'has_gpu' not in s]; assert {s['id']: s['has_gpu'] for s in SERVICES} == {'llamacpp': True, 'model-gateway': False, 'webui': False, 'mcp': False, 'comfyui': True, 'n8n': False, 'openclaw': False, 'qdrant': False}"
```

Expected: AssertionError (the flag is missing).

- [ ] **Step 2: Add the flag**

Edit `dashboard/services_catalog.py`. Add `"has_gpu": True` to the `llamacpp` and `comfyui` entries, and `"has_gpu": False` to every other entry in `SERVICES`. Example edit for the first entry:

```python
{"id": "llamacpp", "name": "llama.cpp", "port": 8080, "url": "http://localhost:8080", "check": "http://llamacpp:8080/health",
 "hint": "Backend-only; use model-gateway :11435 from host. Run: docker compose up -d llamacpp",
 "has_gpu": True},
```

Apply the same pattern to all 8 SERVICES entries. Only `llamacpp` and `comfyui` get `True`.

- [ ] **Step 3: Verify the check passes**

```bash
python -c "from dashboard.services_catalog import SERVICES; assert all('has_gpu' in s for s in SERVICES); print('ok')"
```

Expected output: `ok`

- [ ] **Step 4: Commit**

```bash
git add dashboard/services_catalog.py
git commit -m "feat(dashboard): add has_gpu flag to services catalog"
```

---

## Task 2: Add `_cpu_pct_from_stats` helper to ops-controller

`docker stats --no-stream` returns cumulative CPU counters in `cpu_stats` + `precpu_stats`. We compute percent the same way the `docker stats` CLI does.

**Files:**
- Modify: `ops-controller/main.py` (add helper near the other private helpers, around line 180)
- Create: `tests/test_ops_controller_stats.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ops_controller_stats.py`:

```python
"""Unit tests for ops-controller stats helpers and /stats/services endpoint."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("docker", MagicMock())

_path = Path(__file__).resolve().parent.parent / "ops-controller" / "main.py"
_spec = importlib.util.spec_from_file_location("ops_controller_main", _path)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)


def test_cpu_pct_from_stats_basic():
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000}, "system_cpu_usage": 10_000_000_000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 750_000_000}, "system_cpu_usage": 9_000_000_000},
    }
    # cpu_delta=250M, system_delta=1B, cpus=4 → (.25)*4*100 = 100.0
    assert oc._cpu_pct_from_stats(stats) == 100.0


def test_cpu_pct_from_stats_zero_system_delta():
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 1}, "system_cpu_usage": 100, "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 1}, "system_cpu_usage": 100},
    }
    assert oc._cpu_pct_from_stats(stats) == 0.0


def test_cpu_pct_from_stats_missing_fields():
    assert oc._cpu_pct_from_stats({}) == 0.0
    assert oc._cpu_pct_from_stats({"cpu_stats": {}}) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_ops_controller_stats.py -v
```

Expected: FAILED — AttributeError: module has no attribute '_cpu_pct_from_stats'.

- [ ] **Step 3: Add the helper**

Edit `ops-controller/main.py`. Add right after `_containers_for_service` (around line 180):

```python
def _cpu_pct_from_stats(stats: dict) -> float:
    """Compute CPU% from one docker stats sample using precpu_stats delta. Matches `docker stats` CLI math."""
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = int(cpu["cpu_usage"]["total_usage"]) - int(pre["cpu_usage"]["total_usage"])
        system_delta = int(cpu["system_cpu_usage"]) - int(pre.get("system_cpu_usage") or 0)
        online_cpus = int(cpu.get("online_cpus") or len((cpu["cpu_usage"].get("percpu_usage") or [])) or 1)
        if system_delta <= 0 or cpu_delta < 0:
            return 0.0
        return round((cpu_delta / system_delta) * online_cpus * 100.0, 1)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_ops_controller_stats.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ops-controller/main.py tests/test_ops_controller_stats.py
git commit -m "feat(ops-controller): add _cpu_pct_from_stats helper"
```

---

## Task 3: Add `_mem_from_stats` helper to ops-controller

- [ ] **Step 1: Add failing tests**

Append to `tests/test_ops_controller_stats.py`:

```python
def test_mem_from_stats_cgroup_v2():
    # docker stats subtracts inactive_file (v2) from usage
    stats = {"memory_stats": {
        "usage": 2_000_000_000,
        "stats": {"inactive_file": 500_000_000},
        "limit": 10_000_000_000,
    }}
    gb, pct = oc._mem_from_stats(stats)
    assert gb == 1.5
    assert pct == 15.0


def test_mem_from_stats_cgroup_v1_fallback_to_cache():
    stats = {"memory_stats": {
        "usage": 1_500_000_000,
        "stats": {"cache": 500_000_000},
        "limit": 4_000_000_000,
    }}
    gb, pct = oc._mem_from_stats(stats)
    assert gb == 1.0
    assert pct == 25.0


def test_mem_from_stats_empty():
    assert oc._mem_from_stats({}) == (0.0, 0.0)
    assert oc._mem_from_stats({"memory_stats": {}}) == (0.0, 0.0)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_ops_controller_stats.py -v -k mem
```

Expected: 3 failures, AttributeError.

- [ ] **Step 3: Add the helper**

Immediately after `_cpu_pct_from_stats` in `ops-controller/main.py`:

```python
def _mem_from_stats(stats: dict) -> tuple[float, float]:
    """Return (mem_gb, mem_pct). Subtracts inactive_file (cgroup v2) or cache (v1) like `docker stats`."""
    try:
        ms = stats["memory_stats"]
        usage = int(ms.get("usage") or 0)
        inner = ms.get("stats") or {}
        sub = int(inner.get("inactive_file") or inner.get("cache") or 0)
        used = max(0, usage - sub)
        limit = int(ms.get("limit") or 0)
        if limit <= 0:
            return (round(used / 1e9, 2), 0.0)
        return (round(used / 1e9, 2), round(used / limit * 100.0, 1))
    except (KeyError, TypeError, ValueError):
        return (0.0, 0.0)
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_ops_controller_stats.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add ops-controller/main.py tests/test_ops_controller_stats.py
git commit -m "feat(ops-controller): add _mem_from_stats helper"
```

---

## Task 4: Add `_container_host_pids` helper to ops-controller

- [ ] **Step 1: Add failing tests**

Append to `tests/test_ops_controller_stats.py`:

```python
def test_container_host_pids_parses_docker_top():
    c = MagicMock()
    c.top.return_value = {
        "Titles": ["PID", "COMMAND"],
        "Processes": [["1234", "python3"], ["5678", "llama-server"]],
    }
    assert oc._container_host_pids(c) == [1234, 5678]


def test_container_host_pids_handles_empty_or_missing():
    c = MagicMock()
    c.top.return_value = {"Titles": ["PID", "COMMAND"], "Processes": None}
    assert oc._container_host_pids(c) == []

    c2 = MagicMock()
    c2.top.return_value = {}
    assert oc._container_host_pids(c2) == []


def test_container_host_pids_swallows_exceptions():
    c = MagicMock()
    c.top.side_effect = RuntimeError("container not running")
    assert oc._container_host_pids(c) == []


def test_container_host_pids_skips_non_numeric_rows():
    c = MagicMock()
    c.top.return_value = {
        "Titles": ["PID", "COMMAND"],
        "Processes": [["1234", "python3"], ["bad", "x"], [], ["9999", "comfyui"]],
    }
    assert oc._container_host_pids(c) == [1234, 9999]
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_ops_controller_stats.py -v -k host_pids
```

Expected: 4 failures.

- [ ] **Step 3: Add the helper**

Append after `_mem_from_stats` in `ops-controller/main.py`:

```python
def _container_host_pids(container) -> list[int]:
    """Host-visible PIDs for a running container via `docker top`. Returns [] on any failure."""
    try:
        info = container.top(ps_args="-eo pid,comm")
    except Exception:
        return []
    procs = (info or {}).get("Processes") or []
    pids: list[int] = []
    for row in procs:
        if not row:
            continue
        raw = str(row[0]).strip()
        if raw.isdigit():
            pids.append(int(raw))
    return pids
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_ops_controller_stats.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add ops-controller/main.py tests/test_ops_controller_stats.py
git commit -m "feat(ops-controller): add _container_host_pids helper"
```

---

## Task 5: Add `_nvml_vram_by_pid` helper to ops-controller

Collects per-PID VRAM in bytes plus aggregate GPU info in one NVML session, with a `per_pid_available` flag for the Windows/WSL2 fallback.

- [ ] **Step 1: Add failing tests**

Append to `tests/test_ops_controller_stats.py`:

```python
class _MI:
    total = int(24e9)
    used = int(8e9)


class _UT:
    gpu = 42


class _P:
    def __init__(self, pid, mem):
        self.pid = pid
        self.usedGpuMemory = mem


def _patch_nvml(monkeypatch, compute_procs, graphics_procs=None):
    import pynvml
    monkeypatch.setattr(pynvml, "nvmlInit", lambda: None)
    monkeypatch.setattr(pynvml, "nvmlShutdown", lambda: None)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetHandleByIndex", lambda i: object())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetMemoryInfo", lambda h: _MI())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetUtilizationRates", lambda h: _UT())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetComputeRunningProcesses", lambda h: compute_procs)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetGraphicsRunningProcesses", lambda h: graphics_procs or [])


def test_nvml_vram_by_pid_happy(monkeypatch):
    _patch_nvml(monkeypatch, [_P(1234, int(6e9)), _P(5678, int(1e9))])
    pid_map, gpu = oc._nvml_vram_by_pid()
    assert pid_map == {1234: int(6e9), 5678: int(1e9)}
    assert gpu["total_gb"] == 24.0
    assert gpu["used_gb"] == 8.0
    assert gpu["utilization_pct"] == 42
    assert gpu["per_pid_available"] is True


def test_nvml_vram_by_pid_windows_fallback(monkeypatch):
    # On WSL2/WDDM, usedGpuMemory is None — flag goes to False
    _patch_nvml(monkeypatch, [_P(1234, None)])
    pid_map, gpu = oc._nvml_vram_by_pid()
    assert pid_map == {}
    assert gpu["per_pid_available"] is False
    assert gpu["total_gb"] == 24.0  # aggregate still works


def test_nvml_vram_by_pid_init_fails(monkeypatch):
    import pynvml
    monkeypatch.setattr(pynvml, "nvmlInit", lambda: (_ for _ in ()).throw(Exception("no gpu")))
    pid_map, gpu = oc._nvml_vram_by_pid()
    assert pid_map == {}
    assert gpu == {"total_gb": 0.0, "used_gb": 0.0, "utilization_pct": 0, "per_pid_available": False}
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_ops_controller_stats.py -v -k vram_by_pid
```

Expected: 3 failures.

- [ ] **Step 3: Add the helper**

Append after `_container_host_pids` in `ops-controller/main.py`:

```python
def _nvml_vram_by_pid() -> tuple[dict[int, int], dict]:
    """Return ({pid: vram_bytes}, gpu_summary). pid_map empty when per-PID VRAM is unavailable (e.g. WSL2/WDDM)."""
    default_gpu = {"total_gb": 0.0, "used_gb": 0.0, "utilization_pct": 0, "per_pid_available": False}
    try:
        import pynvml
        pynvml.nvmlInit()
    except Exception as e:
        logger.debug("NVML init failed: %s", e)
        return {}, default_gpu
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        mi = pynvml.nvmlDeviceGetMemoryInfo(h)
        ut = pynvml.nvmlDeviceGetUtilizationRates(h)
        total_b = int(mi.total)
        used_b = int(mi.used)
        pids: dict[int, int] = {}
        has_per_pid = False
        for getter in (pynvml.nvmlDeviceGetComputeRunningProcesses,
                       pynvml.nvmlDeviceGetGraphicsRunningProcesses):
            try:
                for p in getter(h):
                    mem = getattr(p, "usedGpuMemory", None) or getattr(p, "used_gpu_memory", None)
                    if mem is None:
                        continue
                    mem_b = int(mem)
                    if mem_b <= 0:
                        continue
                    has_per_pid = True
                    pids[int(p.pid)] = pids.get(int(p.pid), 0) + mem_b
            except pynvml.NVMLError:
                pass
        return pids, {
            "total_gb": round(total_b / 1e9, 1),
            "used_gb": round(used_b / 1e9, 1),
            "utilization_pct": int(ut.gpu),
            "per_pid_available": has_per_pid,
        }
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_ops_controller_stats.py -v
```

Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add ops-controller/main.py tests/test_ops_controller_stats.py
git commit -m "feat(ops-controller): add _nvml_vram_by_pid helper"
```

---

## Task 6: Add `GET /stats/services` endpoint to ops-controller

Merges container stats + per-PID VRAM into a keyed-by-compose-service-id payload. Auth-required (same pattern as other ops-controller routes).

- [ ] **Step 1: Add failing tests**

Append to `tests/test_ops_controller_stats.py`:

```python
from fastapi.testclient import TestClient

VALID_TOKEN = "test-token"


def _mk_container(name, service, status="running", stats_sample=None, pids=None):
    c = MagicMock()
    c.name = name
    c.status = status
    c.labels = {"com.docker.compose.service": service, "com.docker.compose.project": "ordo-ai-stack"}
    c.stats.return_value = stats_sample or {
        "cpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000}, "system_cpu_usage": 10_000_000_000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 750_000_000}, "system_cpu_usage": 9_000_000_000},
        "memory_stats": {"usage": 1_500_000_000, "stats": {"inactive_file": 500_000_000}, "limit": 10_000_000_000},
    }
    c.top.return_value = {
        "Titles": ["PID", "COMMAND"],
        "Processes": [[str(p), "x"] for p in (pids or [])],
    }
    return c


@pytest.fixture()
def stats_client(monkeypatch):
    monkeypatch.setattr(oc, "OPS_CONTROLLER_TOKEN", VALID_TOKEN)
    return TestClient(oc.app, raise_server_exceptions=False)


def test_stats_services_requires_auth(stats_client):
    r = stats_client.get("/stats/services")
    assert r.status_code == 401


def test_stats_services_basic_merge(stats_client, monkeypatch):
    containers = [
        _mk_container("ordo-comfyui-1", "comfyui", pids=[1234]),
        _mk_container("ordo-webui-1", "open-webui", pids=[2222]),
    ]
    monkeypatch.setattr(oc, "_get_containers", lambda: containers)
    _patch_nvml(monkeypatch, [_P(1234, int(6e9))])
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    d = r.json()
    assert d["vram_aggregate_unavailable"] is False
    assert d["gpu"]["total_gb"] == 24.0
    assert d["services"]["comfyui"]["running"] is True
    assert d["services"]["comfyui"]["vram_gb"] == 6.0
    assert d["services"]["comfyui"]["vram_pct"] == 25.0
    assert d["services"]["open-webui"]["vram_gb"] == 0.0
    assert d["services"]["open-webui"]["cpu_pct"] == 100.0


def test_stats_services_stopped_container(stats_client, monkeypatch):
    containers = [_mk_container("ordo-n8n-1", "n8n", status="exited")]
    monkeypatch.setattr(oc, "_get_containers", lambda: containers)
    _patch_nvml(monkeypatch, [])
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    d = r.json()
    assert d["services"]["n8n"]["running"] is False
    assert d["services"]["n8n"]["cpu_pct"] == 0.0


def test_stats_services_vram_unavailable(stats_client, monkeypatch):
    containers = [_mk_container("ordo-comfyui-1", "comfyui", pids=[1234])]
    monkeypatch.setattr(oc, "_get_containers", lambda: containers)
    _patch_nvml(monkeypatch, [_P(1234, None)])  # WSL2 fallback
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    d = r.json()
    assert d["vram_aggregate_unavailable"] is True
    assert d["services"]["comfyui"]["vram_gb"] == 0.0


def test_stats_services_docker_list_fails(stats_client, monkeypatch):
    def boom():
        raise RuntimeError("docker daemon down")
    monkeypatch.setattr(oc, "_get_containers", boom)
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    d = r.json()
    assert d["services"] == {}
    assert d["vram_aggregate_unavailable"] is True


def test_stats_services_stats_fetch_error_per_container(stats_client, monkeypatch):
    c = _mk_container("ordo-comfyui-1", "comfyui", pids=[1234])
    c.stats.side_effect = RuntimeError("stats failed")
    monkeypatch.setattr(oc, "_get_containers", lambda: [c])
    _patch_nvml(monkeypatch, [])
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    d = r.json()
    # Container is running but stats failed — row still present, zeros
    assert d["services"]["comfyui"]["running"] is True
    assert d["services"]["comfyui"]["cpu_pct"] == 0.0
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_ops_controller_stats.py -v -k stats_services
```

Expected: 6 failures (404 for the route).

- [ ] **Step 3: Add the endpoint**

Add this just before the `# --- Model downloads (ComfyUI files) ---` section in `ops-controller/main.py` (around line 580):

```python
@app.get("/stats/services")
async def stats_services(_: None = Depends(verify_token)):
    """Per-compose-service CPU/RAM/VRAM. Read-only, auth required (same as other ops routes)."""
    try:
        containers = _get_containers()
    except Exception as e:
        logger.warning("stats/services: docker list failed: %s", e)
        return {"gpu": None, "services": {}, "vram_aggregate_unavailable": True}

    vram_by_pid, gpu = await asyncio.to_thread(_nvml_vram_by_pid)
    vram_aggregate_unavailable = not gpu["per_pid_available"]

    services: dict[str, dict] = {}
    for c in containers:
        svc = (c.labels or {}).get("com.docker.compose.service")
        if not svc:
            continue
        row = services.setdefault(svc, {
            "cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0,
            "vram_gb": 0.0, "vram_pct": 0.0, "running": False,
        })
        status = getattr(c, "status", "") or ""
        if status != "running":
            continue
        row["running"] = True
        try:
            sample = c.stats(stream=False)
        except Exception as e:
            logger.debug("stats sample failed for %s: %s", svc, e)
            continue
        row["cpu_pct"] = _cpu_pct_from_stats(sample)
        row["mem_gb"], row["mem_pct"] = _mem_from_stats(sample)
        if vram_by_pid:
            pids = _container_host_pids(c)
            total_b = sum(vram_by_pid.get(pid, 0) for pid in pids)
            if total_b > 0 and gpu["total_gb"] > 0:
                row["vram_gb"] = round(total_b / 1e9, 2)
                row["vram_pct"] = round(total_b / (gpu["total_gb"] * 1e9) * 100.0, 1)

    gpu_out = None if gpu["total_gb"] == 0 else {k: v for k, v in gpu.items() if k != "per_pid_available"}
    return {
        "gpu": gpu_out,
        "services": services,
        "vram_aggregate_unavailable": vram_aggregate_unavailable,
    }
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_ops_controller_stats.py -v
```

Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add ops-controller/main.py tests/test_ops_controller_stats.py
git commit -m "feat(ops-controller): add /stats/services endpoint for per-container CPU/RAM/VRAM"
```

---

## Task 7: Add dashboard proxy `GET /api/hardware/service-pressure`

Reads the ops-controller payload, joins with `SERVICES` catalog to add `id/name/has_gpu`, ensures every catalog service appears (zero-filled if missing), sorts by max(cpu%, mem%, vram%) descending.

**Files:**
- Modify: `dashboard/app.py`
- Create: `tests/test_dashboard_service_pressure.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard_service_pressure.py`:

```python
"""Tests for /api/hardware/service-pressure proxy."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def _mk_httpx_response(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def _mk_async_client(response):
    ac = MagicMock()
    ac.__aenter__ = AsyncMock(return_value=ac)
    ac.__aexit__ = AsyncMock(return_value=None)
    ac.get = AsyncMock(return_value=response)
    return ac


def test_service_pressure_no_auth_required(monkeypatch):
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({
        "gpu": {"total_gb": 24.0, "used_gb": 6.0, "utilization_pct": 30},
        "services": {"comfyui": {"cpu_pct": 10.0, "mem_gb": 1.0, "mem_pct": 2.0, "vram_gb": 6.0, "vram_pct": 25.0, "running": True}},
        "vram_aggregate_unavailable": False,
    }))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        r = TestClient(app.app).get("/api/hardware/service-pressure")
    assert r.status_code == 200
    d = r.json()
    # Every catalog service appears
    ids = {s["id"] for s in d["services"]}
    assert {"llamacpp", "comfyui", "webui", "openclaw", "qdrant", "n8n", "mcp", "model-gateway"} <= ids
    # ComfyUI (catalog display id) sorted first because it has the highest max pct
    assert d["services"][0]["id"] == "comfyui"
    assert d["services"][0]["has_gpu"] is True
    assert d["services"][0]["vram_gb"] == 6.0


def test_service_pressure_sorts_by_max_percent(monkeypatch):
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({
        "gpu": None,
        "services": {
            "open-webui": {"cpu_pct": 80.0, "mem_gb": 0.5, "mem_pct": 1.0, "vram_gb": 0.0, "vram_pct": 0.0, "running": True},
            "comfyui": {"cpu_pct": 5.0, "mem_gb": 1.0, "mem_pct": 2.0, "vram_gb": 0.5, "vram_pct": 2.0, "running": True},
        },
        "vram_aggregate_unavailable": True,
    }))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    assert d["services"][0]["id"] == "webui"  # 80% CPU wins
    assert d["services"][0]["cpu_pct"] == 80.0


def test_service_pressure_maps_compose_name_to_display_id():
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({
        "gpu": None,
        "services": {"open-webui": {"cpu_pct": 1.0, "mem_gb": 0.1, "mem_pct": 0.2, "vram_gb": 0.0, "vram_pct": 0.0, "running": True}},
        "vram_aggregate_unavailable": False,
    }))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    webui_row = next(s for s in d["services"] if s["id"] == "webui")
    assert webui_row["name"] == "Open WebUI"
    assert webui_row["running"] is True


def test_service_pressure_missing_services_filled_as_zero():
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({"gpu": None, "services": {}, "vram_aggregate_unavailable": True}))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    assert len(d["services"]) >= 8
    assert all(not s["running"] for s in d["services"])


def test_service_pressure_ops_controller_unreachable(monkeypatch):
    import dashboard.app as app
    ac = MagicMock()
    ac.__aenter__ = AsyncMock(return_value=ac)
    ac.__aexit__ = AsyncMock(return_value=None)
    ac.get = AsyncMock(side_effect=app._httpx.ConnectError("unreachable"))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        r = TestClient(app.app).get("/api/hardware/service-pressure")
    assert r.status_code == 200
    d = r.json()
    assert d["vram_aggregate_unavailable"] is True
    assert all(not s["running"] for s in d["services"])


def test_service_pressure_ops_controller_error_status():
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({}, status=500))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    assert d["vram_aggregate_unavailable"] is True
    assert all(not s["running"] for s in d["services"])
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_dashboard_service_pressure.py -v
```

Expected: 6 failures (404).

- [ ] **Step 3: Add the endpoint**

In `dashboard/app.py`, add the endpoint near the other `/api/hardware` routes (after `/api/hardware` around line 2186). The file already imports `httpx as _httpx` at the top — use that alias (don't add another import).

```python
@app.get("/api/hardware/service-pressure")
async def service_pressure():
    """Per-service compute pressure (CPU/RAM/VRAM). No auth — read-only, like /api/hardware."""
    from dashboard.services_catalog import SERVICES, OPS_SERVICE_MAP

    ops_url = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
    token = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    host_info = {
        "cpu_cores": psutil.cpu_count() or 0,
        "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
    }

    def _empty_payload():
        services_out = [{
            "id": s["id"], "name": s["name"],
            "cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0,
            "vram_gb": 0.0, "vram_pct": 0.0,
            "has_gpu": bool(s.get("has_gpu", False)),
            "running": False,
        } for s in SERVICES]
        return {"gpu": None, "host": host_info, "services": services_out, "vram_aggregate_unavailable": True}

    try:
        async with _httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{ops_url}/stats/services", headers=headers)
            if r.status_code != 200:
                return _empty_payload()
            raw = r.json()
    except (_httpx.RequestError, OSError) as e:
        logger.debug("service-pressure: ops-controller unreachable: %s", e)
        return _empty_payload()

    raw_services: dict = raw.get("services") or {}
    catalog = {s["id"]: s for s in SERVICES}
    compose_to_display = {v: k for k, v in OPS_SERVICE_MAP.items()}

    services_out: list[dict] = []
    for compose_id, row in raw_services.items():
        display_id = compose_to_display.get(compose_id, compose_id)
        cat = catalog.get(display_id)
        services_out.append({
            "id": display_id,
            "name": (cat or {}).get("name") or compose_id,
            "cpu_pct": float(row.get("cpu_pct") or 0.0),
            "mem_gb": float(row.get("mem_gb") or 0.0),
            "mem_pct": float(row.get("mem_pct") or 0.0),
            "vram_gb": float(row.get("vram_gb") or 0.0),
            "vram_pct": float(row.get("vram_pct") or 0.0),
            "has_gpu": bool((cat or {}).get("has_gpu", False)),
            "running": bool(row.get("running", False)),
        })
    seen = {s["id"] for s in services_out}
    for cid, cat in catalog.items():
        if cid not in seen:
            services_out.append({
                "id": cid, "name": cat["name"],
                "cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0,
                "vram_gb": 0.0, "vram_pct": 0.0,
                "has_gpu": bool(cat.get("has_gpu", False)),
                "running": False,
            })
    services_out.sort(
        key=lambda s: max(s["cpu_pct"], s["mem_pct"], s["vram_pct"]),
        reverse=True,
    )
    return {
        "gpu": raw.get("gpu"),
        "host": host_info,
        "services": services_out,
        "vram_aggregate_unavailable": bool(raw.get("vram_aggregate_unavailable", False)),
    }
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_dashboard_service_pressure.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add dashboard/app.py tests/test_dashboard_service_pressure.py
git commit -m "feat(dashboard): add /api/hardware/service-pressure proxy endpoint"
```

---

## Task 8: Remove obsolete GPU-processes code

Deletes the old endpoint and its helpers, along with its test file. The new endpoint has fully replaced it.

**Files:**
- Modify: `dashboard/app.py` (delete lines ~2005-2118)
- Delete: `tests/test_dashboard_gpu_processes.py`

- [ ] **Step 1: Delete the helpers and endpoint**

In `dashboard/app.py`, delete:

- `_pid_to_service_label(pid)` function (around lines 2005-2035).
- `_gpu_processes()` function (around lines 2038-2104).
- `@app.get("/api/hardware/gpu-processes")` route and its `gpu_processes()` handler (around lines 2107-2118).

Leave `_nvml_vram_to_gpu_dict`, `/api/hardware`, and other helpers intact.

- [ ] **Step 2: Delete the test file**

```bash
git rm tests/test_dashboard_gpu_processes.py
```

- [ ] **Step 3: Run the full dashboard test suite to confirm no regressions**

```bash
pytest tests/test_dashboard_performance.py tests/test_dashboard_dependencies.py tests/test_dashboard_health.py tests/test_dashboard_auth_middleware.py tests/test_dashboard_service_pressure.py -v
```

Expected: all pass. No references to the removed endpoint anywhere.

- [ ] **Step 4: Commit**

```bash
git add dashboard/app.py
git commit -m "refactor(dashboard): remove obsolete /api/hardware/gpu-processes (replaced by /service-pressure)"
```

---

## Task 9: Redesign `#compute-pressure` frontend section

Replace the stacked-bar + PID-legend UI with a per-service grid: label | CPU bar | RAM bar | VRAM bar (or hidden slot) | numeric values. Sorted by pressure. Greyed when `running:false`.

**Files:**
- Modify: `dashboard/static/index.html`

- [ ] **Step 1: Replace the CSS block**

In `dashboard/static/index.html`, find the block starting at `#compute-pressure` CSS (around line 985) up to `.cp-score.st-starved` (around line 1055). Replace that entire CSS range with:

```css
    #compute-pressure { margin-bottom: var(--space-5); }
    .compute-header {
      display: flex; align-items: center; gap: var(--space-3);
      margin-bottom: var(--space-4);
    }
    .cp-util-badge {
      font-family: 'JetBrains Mono', monospace; font-size: 11px;
      color: var(--fg-muted); letter-spacing: .05em; margin-left: auto;
    }
    .cp-aggregate-row {
      display: grid; grid-template-columns: 90px 1fr 140px;
      gap: var(--space-3); align-items: center; margin-bottom: var(--space-3);
      padding: var(--space-2) 0; border-bottom: 1px solid var(--border-subtle);
    }
    .cp-rows {
      display: flex; flex-direction: column; gap: var(--space-2);
      margin-bottom: var(--space-4);
    }
    .cp-row {
      display: grid;
      grid-template-columns: 100px 1fr 1fr 1fr 120px;
      align-items: center; gap: var(--space-3);
      padding: 4px 0;
    }
    .cp-row.idle { opacity: 0.45; }
    .cp-row-label {
      font-size: 12px; font-weight: 700; color: var(--fg);
      font-family: 'Barlow Condensed', sans-serif;
      letter-spacing: .06em; text-transform: uppercase;
    }
    .cp-bar-group { display: flex; flex-direction: column; gap: 3px; }
    .cp-bar-label {
      font-size: 9px; color: var(--fg-muted); letter-spacing: .08em;
      font-family: 'JetBrains Mono', monospace; text-transform: uppercase;
    }
    .cp-bar-track {
      height: 6px; border-radius: 3px; background: var(--border); overflow: hidden;
    }
    .cp-bar-fill {
      height: 100%; border-radius: 3px; transition: width .4s ease;
    }
    .cp-bar-fill.cpu  { background: var(--cp-llm, #4ea8de); }
    .cp-bar-fill.ram  { background: var(--cp-comfy, #f4a261); }
    .cp-bar-fill.vram { background: var(--cp-embed, #a78bfa); }
    .cp-bar-slot-empty { visibility: hidden; }
    .cp-row-values {
      font-family: 'JetBrains Mono', monospace; font-size: 10px;
      color: var(--fg-muted); text-align: right; line-height: 1.3;
    }
    .cp-score {
      font-size: 12px; font-family: 'JetBrains Mono', monospace;
      padding: var(--space-3) var(--space-4);
      background: var(--bg-elevated); border-radius: var(--radius-sm);
      border-left: 3px solid var(--border);
    }
    .cp-score.st-nominal  { border-left-color: var(--success); color: var(--success); }
    .cp-score.st-degraded { border-left-color: var(--warning); color: var(--warning); }
    .cp-score.st-starved  { border-left-color: var(--danger);  color: var(--danger);  }
```

- [ ] **Step 2: Replace the markup block**

Find the `<!-- ── Compute Pressure ──` section (around line 1127) and replace through `</section>` closing (around line 1141) with:

```html
    <!-- ── Compute Pressure ──────────────────────────────── -->
    <section id="compute-pressure">
      <div class="compute-header">
        <span class="section-label">COMPUTE PRESSURE</span>
        <span id="cp-util-badge" class="cp-util-badge">— GPU</span>
        <span class="live-dot" id="cp-live-dot" style="margin-left:var(--space-2)"></span>
      </div>
      <div id="cp-aggregate" class="cp-aggregate-row" style="display:none">
        <span class="cp-row-label">GPU (AGG)</span>
        <div class="cp-bar-group">
          <span class="cp-bar-label">VRAM</span>
          <div class="cp-bar-track"><div id="cp-agg-fill" class="cp-bar-fill vram" style="width:0%"></div></div>
        </div>
        <span class="cp-row-values" id="cp-agg-values">— / — GB</span>
      </div>
      <div id="cp-rows" class="cp-rows"></div>
      <div id="cp-score" class="cp-score" style="display:none"></div>
    </section>
```

- [ ] **Step 3: Replace the JS refresh function**

Find `refreshComputePressure` (around line 2995) and replace the entire function + its timer setup (down through the `// ── End Compute Pressure` comment, around line 3086). Paste:

```javascript
    function _cpBarFill(pct) {
      const v = Math.max(0, Math.min(100, Number(pct) || 0));
      return v.toFixed(0) + '%';
    }

    async function refreshComputePressure() {
      try {
        const r = await api('/api/hardware/service-pressure');
        if (!r.ok) return;
        const d = await r.json();

        // GPU util badge
        if (d.gpu && typeof d.gpu.utilization_pct === 'number') {
          document.getElementById('cp-util-badge').textContent = d.gpu.utilization_pct + '% GPU';
        } else {
          document.getElementById('cp-util-badge').textContent = '— GPU';
        }

        // Aggregate-VRAM row shown only when per-PID unavailable
        const agg = document.getElementById('cp-aggregate');
        if (d.vram_aggregate_unavailable && d.gpu && d.gpu.total_gb > 0) {
          agg.style.display = '';
          const pct = d.gpu.total_gb > 0 ? (d.gpu.used_gb / d.gpu.total_gb * 100) : 0;
          document.getElementById('cp-agg-fill').style.width = _cpBarFill(pct);
          document.getElementById('cp-agg-values').textContent =
            d.gpu.used_gb.toFixed(1) + ' / ' + d.gpu.total_gb.toFixed(1) + ' GB';
        } else {
          agg.style.display = 'none';
        }

        // Per-service rows
        const rowsEl = document.getElementById('cp-rows');
        rowsEl.innerHTML = '';
        (d.services || []).forEach(s => {
          const row = document.createElement('div');
          row.className = 'cp-row' + (s.running ? '' : ' idle');
          const vramBar = s.has_gpu && !d.vram_aggregate_unavailable
            ? `<div class="cp-bar-group">
                 <span class="cp-bar-label">VRAM</span>
                 <div class="cp-bar-track"><div class="cp-bar-fill vram" style="width:${_cpBarFill(s.vram_pct)}"></div></div>
               </div>`
            : `<div class="cp-bar-group cp-bar-slot-empty">
                 <span class="cp-bar-label">VRAM</span>
                 <div class="cp-bar-track"></div>
               </div>`;
          const vramValue = s.has_gpu && !d.vram_aggregate_unavailable
            ? `${s.vram_gb.toFixed(1)}&nbsp;GB VRAM`
            : '';
          row.innerHTML = `
            <span class="cp-row-label">${escapeHtml(s.name)}</span>
            <div class="cp-bar-group">
              <span class="cp-bar-label">CPU</span>
              <div class="cp-bar-track"><div class="cp-bar-fill cpu" style="width:${_cpBarFill(s.cpu_pct)}"></div></div>
            </div>
            <div class="cp-bar-group">
              <span class="cp-bar-label">RAM</span>
              <div class="cp-bar-track"><div class="cp-bar-fill ram" style="width:${_cpBarFill(s.mem_pct)}"></div></div>
            </div>
            ${vramBar}
            <span class="cp-row-values">
              ${s.cpu_pct.toFixed(0)}%&nbsp;CPU<br>
              ${s.mem_gb.toFixed(1)}&nbsp;GB RAM<br>
              ${vramValue || (s.running ? '' : 'stopped')}
            </span>`;
          rowsEl.appendChild(row);
        });

        // LLM degradation score strip (unchanged — uses separate data source)
        const deg = _cpDegradation();
        const scoreEl = document.getElementById('cp-score');
        if (deg) {
          const cls = _cpScoreClass(deg.pct);
          const label = deg.pct >= 85 ? 'NOMINAL' : deg.pct >= 60 ? 'DEGRADED' : 'STARVED';
          scoreEl.className = 'cp-score ' + cls;
          scoreEl.textContent = `⚡ LLM at ${deg.pct}% of peak (${deg.latest} / ${deg.peak} tok/s) — ${label}`;
          scoreEl.style.display = '';
        } else {
          scoreEl.style.display = 'none';
        }
      } catch (e) {
        console.warn('compute pressure refresh failed', e);
      }
    }

    refreshComputePressure();
    let cpTimer = setInterval(refreshComputePressure, 3000);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearInterval(cpTimer); }
      else { refreshComputePressure(); cpTimer = setInterval(refreshComputePressure, 3000); }
    });
    // ── End Compute Pressure ──────────────────────────────────
```

Note: `_cpDegradation`, `_cpScoreClass`, `api`, and `escapeHtml` are existing helpers — leave them alone.

- [ ] **Step 4: Manual smoke test**

```bash
docker compose up -d ops-controller dashboard
curl -s http://localhost:8080/api/hardware/service-pressure | python -m json.tool
```

Expected: payload with 8 services, sorted, every one has `has_gpu` / `running` / numeric fields.

Open the dashboard in a browser, verify:
- The panel shows 8 rows with CPU+RAM bars (VRAM bar visible only on ComfyUI + llama.cpp).
- If any service is busy (e.g., kick a ComfyUI render), it rises to the top.
- Stopped services render greyed at the bottom with "stopped" in the value column.
- On Windows/WSL2: aggregate GPU row appears above the per-service list; VRAM bars inside rows are hidden.

- [ ] **Step 5: Commit**

```bash
git add dashboard/static/index.html
git commit -m "feat(dashboard): redesign compute pressure panel as per-service CPU/RAM/VRAM grid"
```

---

## Task 10: CHANGELOG entry + final verification

- [ ] **Step 1: Add CHANGELOG entry**

At the top of the current "Unreleased" / most recent dev section of `CHANGELOG.md`, add:

```markdown
- **Compute Pressure overhaul:** `COMPUTE PRESSURE` panel now shows CPU%, RAM%, and (where applicable) VRAM% per toolkit service, sorted by current pressure so the hog is always on top. New ops-controller endpoint `/stats/services` merges `docker stats` with NVML per-PID VRAM. Dashboard proxies via `/api/hardware/service-pressure` (no auth, same pattern as `/api/hardware`). On Windows/WSL2 where per-PID VRAM is unavailable, panel falls back to a single aggregate GPU row. Replaces `/api/hardware/gpu-processes` and the PID-labeling heuristic.
```

- [ ] **Step 2: Run the full relevant test suite**

```bash
pytest tests/test_ops_controller_stats.py tests/test_ops_controller_auth.py tests/test_dashboard_service_pressure.py tests/test_dashboard_performance.py tests/test_dashboard_health.py -v
```

Expected: all pass.

- [ ] **Step 3: Integration smoke (if Docker is available locally)**

```bash
docker compose up -d
curl -sS -H "Authorization: Bearer $OPS_CONTROLLER_TOKEN" http://localhost:9000/stats/services | python -m json.tool
curl -sS http://localhost:8080/api/hardware/service-pressure | python -m json.tool
```

Expected: both endpoints return sensible payloads; dashboard panel renders in browser.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): record compute pressure per-service overhaul"
```

---

## Notes for implementer

- The `api()` JS helper already attaches the dashboard auth header when auth is enabled; don't re-implement it.
- `psutil`, `httpx`, and `pynvml` are already present in the dashboard image; no new dependencies.
- `docker stats(stream=False)` pre-populates `precpu_stats`, so a single call gives enough data for CPU%. No need for two samples.
- If `ops-controller` isn't running (rare — it's in the base compose), the dashboard endpoint returns a fully zero-filled payload within 3s; the UI handles that gracefully.
- Keep the existing LLM degradation score strip at the bottom. It reads its own data and is independent of this feature.
