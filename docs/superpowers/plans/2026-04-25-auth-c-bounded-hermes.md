# Auth C — Bounded Hermes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Hermes' raw Docker socket access so a prompt-injected Hermes cannot `docker inspect` other containers, `docker exec` into them, or run arbitrary `docker compose` commands. Preserve every user-visible Hermes capability — restart, logs, container list, compose lifecycle — by routing them through ops-controller's HTTP API with a structured audit log. Ops-controller becomes the single holder of `/var/run/docker.sock`.

**Architecture:** Ops-controller grows a small set of named privileged verbs (`/containers/list`, `/containers/{name}/logs`, `/containers/{name}/restart`, `/compose/{up,down,restart}`). No `docker exec` endpoint — that's the prompt-injection escape hatch and stays amputated. Each privileged call emits one structured audit log line to `data/ops-controller/audit.jsonl`. A new `hermes/ops_client.py` wraps the HTTP calls with `OPS_CONTROLLER_TOKEN`. Hermes' MCP tools that previously shelled out to `docker` now call `ops_client`. Hermes' compose service blocks lose `volumes: /var/run/docker.sock:/var/run/docker.sock` and `group_add: ["0"]`.

**Tech stack:** FastAPI (ops-controller), Python `docker` SDK, JSON Lines audit, Hermes MCP tool layer.

**Lifecycle:** This plan lives only on `feat/auth-redesign` and is dropped before the implementation work merges to `main` (see `docs/superpowers/specs/2026-04-25-auth-redesign-design.md` § Lifecycle).

**Independence:** Plan C does not depend on Plans A or B.

---

## File structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ops-controller/main.py` | modify | Add new endpoints + audit-log middleware |
| `ops-controller/audit.py` | create | Append-only JSONL writer with 50MB rotation |
| `ops-controller/test_audit.py` | create | Audit log shape + rotation tests |
| `ops-controller/test_endpoints.py` | create | Per-endpoint round-trip + auth-deny tests |
| `hermes/ops_client.py` | create | Thin HTTP client wrapping ops-controller verbs |
| `hermes/test_ops_client.py` | create | Retry, error-surfacing, header tests |
| `hermes/tools/docker_tool.py` | modify | (Or whichever file holds Hermes' Docker-shelling tools) Migrate to `ops_client` |
| `docker-compose.yml` | modify | Remove docker.sock + group_add from hermes-{gateway,dashboard}; trim env vars Hermes doesn't need |
| `tests/test_hermes_socket_absent.py` | create | Verify Hermes container has no docker.sock |
| `docs/runbooks/bounded-hermes.md` | create | Operator runbook |

Branch base: `feat/auth-redesign`.

---

## Task 1: Audit log writer (TDD)

**Files:**
- Create: `ops-controller/audit.py`
- Create: `ops-controller/test_audit.py`

- [ ] **Step 1: Write the failing test**

Create `ops-controller/test_audit.py`:

```python
import json
import os
from pathlib import Path
import pytest

from ops_controller.audit import AuditLog


def test_writes_one_line_per_call(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(action="container.restart", target="foo", result="ok", caller="test")
    log.record(action="compose.up", target="all", result="ok", caller="test")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["action"] == "container.restart"
    assert parsed[1]["action"] == "compose.up"
    assert "ts" in parsed[0]


def test_rotates_at_size_cap(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=200)
    for i in range(20):
        log.record(action="container.restart", target=f"c{i}", result="ok", caller="t")
    # Primary file should be small (post-rotation), `audit.1.jsonl` should hold the rolled-over data.
    assert (tmp_path / "audit.1.jsonl").exists()
    assert (tmp_path / "audit.jsonl").stat().st_size < 1024


def test_record_returns_the_logged_dict(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    rec = log.record(action="container.logs", target="foo", result="ok", caller="t")
    assert rec["action"] == "container.logs"
    assert rec["caller"] == "t"


def test_concurrent_writes_dont_interleave(tmp_path: Path):
    """JSONL must always be parseable; no half-written lines."""
    import threading

    log = AuditLog(tmp_path / "audit.jsonl")

    def worker(i):
        for j in range(50):
            log.record(action="x", target=f"{i}-{j}", result="ok", caller="t")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 8 * 50
    for l in lines:
        json.loads(l)  # never raises
```

- [ ] **Step 2: Run, verify fail**

`cd /c/dev/AI-toolkit && pytest ops-controller/test_audit.py -v`
Expected: ImportError on `ops_controller.audit`.

- [ ] **Step 3: Implement audit.py**

Create `ops-controller/audit.py`:

```python
import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """Append-only JSONL audit log with simple size-based rotation.

    One privileged call → one record → one fsync'd JSONL line.
    Thread-safe; rotation is opportunistic (checked on each write).
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 50 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self, *, action: str, target: str, result: str, caller: str, **extra: Any
    ) -> dict[str, Any]:
        rec = {
            "ts": time.time(),
            "caller": caller,
            "action": action,
            "target": target,
            "result": result,
        }
        rec.update(extra)
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self._lock:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self._rotate()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return rec

    def _rotate(self) -> None:
        rolled = self.path.with_suffix(self.path.suffix + ".1")
        # Replace any prior rolled file (single rotation tier — keep it simple).
        if rolled.exists():
            rolled.unlink()
        self.path.rename(rolled)
```

- [ ] **Step 4: Run, verify pass**

`pytest ops-controller/test_audit.py -v`
Expected: 4/4 pass.

- [ ] **Step 5: Commit**

```bash
git add ops-controller/audit.py ops-controller/test_audit.py
git commit -m "feat(ops-controller): structured JSONL audit log with size rotation"
```

---

## Task 2: `/containers/list` endpoint (TDD)

**Files:**
- Modify: `ops-controller/main.py`
- Create: `ops-controller/test_endpoints.py`

- [ ] **Step 1: Write the failing test**

Create `ops-controller/test_endpoints.py`:

```python
import os
import pytest
from fastapi.testclient import TestClient

# The app is constructed with TOKEN already set; the test fixture pins it.
@pytest.fixture(autouse=True)
def set_token(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token-for-test")
    # Re-import so the module sees the env var.
    import importlib, ops_controller.main as m
    importlib.reload(m)
    return m


def test_containers_list_requires_bearer(set_token):
    client = TestClient(set_token.app)
    r = client.get("/containers")
    assert r.status_code == 401


def test_containers_list_returns_minimal_metadata(set_token):
    client = TestClient(set_token.app)
    r = client.get(
        "/containers", headers={"Authorization": "Bearer test-token-for-test"}
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:
        for entry in body:
            assert set(entry.keys()) >= {"name", "status", "image"}


def test_containers_list_emits_audit_line(set_token, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    import importlib, ops_controller.main as m
    importlib.reload(m)
    client = TestClient(m.app)
    client.get("/containers", headers={"Authorization": "Bearer test-token-for-test"})
    audit = (tmp_path / "audit.jsonl").read_text().splitlines()
    import json
    parsed = [json.loads(l) for l in audit]
    assert any(p["action"] == "containers.list" for p in parsed)
```

- [ ] **Step 2: Run, verify fail**

`pytest ops-controller/test_endpoints.py -v`
Expected: 404 on `GET /containers` (endpoint does not exist).

- [ ] **Step 3: Add the endpoint to ops-controller/main.py**

Near the existing endpoint registrations:

```python
import docker
from ops_controller.audit import AuditLog

_audit = AuditLog(os.environ.get("AUDIT_LOG_PATH", "/data/audit.jsonl"))
_dc = docker.from_env()


@app.get("/containers")
def list_containers(authorization: str = Header(None)):
    _verify_token(authorization)
    out = [
        {"name": c.name, "status": c.status, "image": c.image.tags[0] if c.image.tags else c.image.id}
        for c in _dc.containers.list(all=True)
    ]
    _audit.record(action="containers.list", target="*", result="ok", caller="hermes")
    return out
```

(Adjust to match the actual auth helper pattern in `ops-controller/main.py:130-138`.)

- [ ] **Step 4: Run, verify pass**

`pytest ops-controller/test_endpoints.py -v`
Expected: 3/3 pass.

- [ ] **Step 5: Commit**

```bash
git add ops-controller/main.py ops-controller/test_endpoints.py
git commit -m "feat(ops-controller): GET /containers endpoint + audit"
```

---

## Task 3: `/containers/{name}/logs` endpoint (TDD)

**Files:**
- Modify: `ops-controller/main.py`
- Modify: `ops-controller/test_endpoints.py`

- [ ] **Step 1: Append failing test**

In `ops-controller/test_endpoints.py`, add:

```python
def test_logs_endpoint_returns_text(set_token):
    client = TestClient(set_token.app)
    r = client.get(
        "/containers/ordo-ai-stack-llamacpp-1/logs?tail=10",
        headers={"Authorization": "Bearer test-token-for-test"},
    )
    assert r.status_code in (200, 404)  # 404 acceptable if container missing in test env
    if r.status_code == 200:
        assert isinstance(r.text, str)


def test_logs_unknown_container_returns_404(set_token):
    client = TestClient(set_token.app)
    r = client.get(
        "/containers/nonexistent-xyz/logs",
        headers={"Authorization": "Bearer test-token-for-test"},
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Add endpoint**

```python
from fastapi import HTTPException
from fastapi.responses import PlainTextResponse


@app.get("/containers/{name}/logs", response_class=PlainTextResponse)
def container_logs(
    name: str,
    tail: int = 100,
    since: str | None = None,
    authorization: str = Header(None),
):
    _verify_token(authorization)
    try:
        c = _dc.containers.get(name)
    except docker.errors.NotFound:
        _audit.record(action="container.logs", target=name, result="not_found", caller="hermes")
        raise HTTPException(404, f"container {name} not found")
    kwargs = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    logs = c.logs(**kwargs).decode("utf-8", errors="replace")
    _audit.record(action="container.logs", target=name, result="ok", caller="hermes", tail=tail)
    return logs
```

- [ ] **Step 3: Run, verify pass; commit**

```bash
pytest ops-controller/test_endpoints.py -v
git add ops-controller/main.py ops-controller/test_endpoints.py
git commit -m "feat(ops-controller): GET /containers/{name}/logs endpoint + audit"
```

---

## Task 4: `/containers/{name}/restart` endpoint (TDD)

**Files:**
- Modify: `ops-controller/main.py`
- Modify: `ops-controller/test_endpoints.py`

- [ ] **Step 1: Append test**

```python
def test_restart_unknown_container_returns_404(set_token):
    client = TestClient(set_token.app)
    r = client.post(
        "/containers/nonexistent-xyz/restart",
        headers={"Authorization": "Bearer test-token-for-test"},
    )
    assert r.status_code == 404
```

(A test that actually restarts a real container is reserved for the smoke-test phase.)

- [ ] **Step 2: Add endpoint**

```python
@app.post("/containers/{name}/restart")
def container_restart(name: str, authorization: str = Header(None)):
    _verify_token(authorization)
    try:
        c = _dc.containers.get(name)
    except docker.errors.NotFound:
        _audit.record(action="container.restart", target=name, result="not_found", caller="hermes")
        raise HTTPException(404, f"container {name} not found")
    c.restart()
    _audit.record(action="container.restart", target=name, result="ok", caller="hermes")
    return {"name": name, "restarted": True}
```

- [ ] **Step 3: Run, verify pass; commit**

```bash
pytest ops-controller/test_endpoints.py -v
git add ops-controller/main.py ops-controller/test_endpoints.py
git commit -m "feat(ops-controller): POST /containers/{name}/restart endpoint + audit"
```

---

## Task 5: `/compose/{up,down,restart}` endpoints (TDD)

**Files:**
- Modify: `ops-controller/main.py`
- Modify: `ops-controller/test_endpoints.py`

- [ ] **Step 1: Append tests**

```python
def test_compose_restart_invalid_service_400(set_token):
    client = TestClient(set_token.app)
    r = client.post(
        "/compose/restart",
        json={"service": "../etc/passwd"},  # path-traversal attempt
        headers={"Authorization": "Bearer test-token-for-test"},
    )
    assert r.status_code == 400


def test_compose_restart_no_service_targets_all(set_token):
    """Posting with no service body restarts the whole stack — must require explicit confirmation."""
    client = TestClient(set_token.app)
    r = client.post(
        "/compose/restart",
        json={},
        headers={"Authorization": "Bearer test-token-for-test"},
    )
    # Whole-stack ops require explicit `confirm: true` to prevent accidents.
    assert r.status_code == 400
    assert "confirm" in r.json().get("detail", "").lower()
```

- [ ] **Step 2: Add the endpoints**

```python
import subprocess
from pydantic import BaseModel, Field, field_validator


class ComposeOpRequest(BaseModel):
    service: str | None = Field(default=None, max_length=64)
    confirm: bool = False

    @field_validator("service")
    @classmethod
    def _safe_service(cls, v: str | None) -> str | None:
        if v is None:
            return None
        # Service names are alphanum + dash + underscore only.
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("service name contains illegal characters")
        return v


def _run_compose(verb: str, service: str | None) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", verb]
    if service:
        cmd.append(service)
    elif verb == "up":
        cmd += ["-d"]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=os.environ.get("COMPOSE_PROJECT_DIR", "/workspace"))


def _compose_endpoint(verb: str, body: ComposeOpRequest):
    if body.service is None and not body.confirm:
        raise HTTPException(400, "whole-stack compose op requires confirm=true")
    target = body.service or "all"
    proc = _run_compose(verb, body.service)
    result = "ok" if proc.returncode == 0 else "fail"
    _audit.record(
        action=f"compose.{verb}", target=target, result=result, caller="hermes",
        rc=proc.returncode, stderr=proc.stderr[-500:] if proc.stderr else "",
    )
    if proc.returncode != 0:
        raise HTTPException(500, f"compose {verb} failed: {proc.stderr[-200:]}")
    return {"verb": verb, "target": target, "stdout": proc.stdout[-2000:]}


@app.post("/compose/up")
def compose_up(body: ComposeOpRequest, authorization: str = Header(None)):
    _verify_token(authorization)
    return _compose_endpoint("up", body)


@app.post("/compose/down")
def compose_down(body: ComposeOpRequest, authorization: str = Header(None)):
    _verify_token(authorization)
    return _compose_endpoint("down", body)


@app.post("/compose/restart")
def compose_restart(body: ComposeOpRequest, authorization: str = Header(None)):
    _verify_token(authorization)
    return _compose_endpoint("restart", body)
```

- [ ] **Step 3: Run, verify pass; commit**

```bash
pytest ops-controller/test_endpoints.py -v
git add ops-controller/main.py ops-controller/test_endpoints.py
git commit -m "feat(ops-controller): /compose/{up,down,restart} with whole-stack confirm guard"
```

---

## Task 6: Hermes ops_client wrapper (TDD)

**Files:**
- Create: `hermes/ops_client.py`
- Create: `hermes/test_ops_client.py`

- [ ] **Step 1: Write failing test**

Create `hermes/test_ops_client.py`:

```python
import os
import pytest
import respx
from httpx import Response

from hermes.ops_client import OpsClient, OpsClientError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    monkeypatch.setenv("OPS_CONTROLLER_URL", "http://ops-controller:9000")
    return OpsClient()


def test_list_containers_includes_bearer(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.get("/containers").mock(return_value=Response(200, json=[{"name": "a", "status": "running", "image": "x"}]))
        out = client.list_containers()
        request = mock.calls.last.request
        assert request.headers["Authorization"] == "Bearer test-token"
    assert out[0]["name"] == "a"


def test_restart_unknown_raises_ops_client_error(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.post("/containers/missing/restart").mock(return_value=Response(404, json={"detail": "not found"}))
        with pytest.raises(OpsClientError) as ei:
            client.restart_container("missing")
        assert "not found" in str(ei.value).lower()


def test_compose_restart_whole_stack_requires_confirm(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.post("/compose/restart").mock(
            return_value=Response(200, json={"verb": "restart", "target": "all"})
        )
        client.compose_restart(service=None, confirm=True)
        body = mock.calls.last.request.read()
        assert b'"confirm":true' in body or b'"confirm": true' in body


def test_logs_returns_string(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.get("/containers/foo/logs").mock(return_value=Response(200, text="line1\nline2"))
        assert client.container_logs("foo") == "line1\nline2"
```

- [ ] **Step 2: Run, verify fail**

`pytest hermes/test_ops_client.py -v`
Expected: ImportError on `hermes.ops_client`.

- [ ] **Step 3: Implement ops_client.py**

Create `hermes/ops_client.py`:

```python
"""HTTP client for ops-controller's privileged verbs.

Hermes uses this in place of raw `docker` / `docker compose` shelling.
The class is intentionally narrow — every method maps to one named
ops-controller endpoint. There is no `exec` or arbitrary-shell verb.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class OpsClientError(RuntimeError):
    """Raised when ops-controller returns a non-2xx response."""


class OpsClient:
    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 60.0,
    ):
        self.url = url or os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000")
        token = token or os.environ.get("OPS_CONTROLLER_TOKEN", "")
        if not token:
            raise OpsClientError("OPS_CONTROLLER_TOKEN env var is empty")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.Client(base_url=self.url, headers=self._headers, timeout=timeout)

    def _check(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise OpsClientError(f"{r.status_code} {detail}")

    def list_containers(self) -> list[dict[str, Any]]:
        r = self._client.get("/containers")
        self._check(r)
        return r.json()

    def container_logs(self, name: str, *, tail: int = 100, since: str | None = None) -> str:
        params = {"tail": tail}
        if since:
            params["since"] = since
        r = self._client.get(f"/containers/{name}/logs", params=params)
        self._check(r)
        return r.text

    def restart_container(self, name: str) -> dict[str, Any]:
        r = self._client.post(f"/containers/{name}/restart")
        self._check(r)
        return r.json()

    def compose_up(self, *, service: str | None = None, confirm: bool = False) -> dict[str, Any]:
        return self._compose("up", service, confirm)

    def compose_down(self, *, service: str | None = None, confirm: bool = False) -> dict[str, Any]:
        return self._compose("down", service, confirm)

    def compose_restart(self, *, service: str | None = None, confirm: bool = False) -> dict[str, Any]:
        return self._compose("restart", service, confirm)

    def _compose(self, verb: str, service: str | None, confirm: bool) -> dict[str, Any]:
        body = {"service": service, "confirm": confirm}
        r = self._client.post(f"/compose/{verb}", json=body)
        self._check(r)
        return r.json()

    def close(self) -> None:
        self._client.close()
```

- [ ] **Step 4: Run, verify pass**

`pytest hermes/test_ops_client.py -v`
Expected: 4/4 pass.

- [ ] **Step 5: Commit**

```bash
git add hermes/ops_client.py hermes/test_ops_client.py
git commit -m "feat(hermes): ops_client HTTP wrapper for ops-controller verbs"
```

---

## Task 7: Migrate Hermes' Docker tools to use ops_client

**Files:**
- Modify: whichever Hermes module currently shells out to `docker` / `docker compose` (likely under `hermes/tools/` or via MCP)

This task is search-and-replace, but each replacement should keep the user-facing behavior identical.

- [ ] **Step 1: Locate every `subprocess.run(["docker", ...])` / `os.system("docker ...")` in Hermes**

```bash
cd /c/dev/AI-toolkit
grep -rn 'subprocess.*docker\|os.system.*docker\|run.*docker compose' hermes/
```

For each match, identify the verb being shelled (list, logs, restart, compose action) and migrate to the matching `OpsClient` method.

- [ ] **Step 2: Pattern: replace docker-list shelling**

Before:

```python
proc = subprocess.run(["docker", "ps", "--format", "json"], capture_output=True, text=True)
containers = [json.loads(l) for l in proc.stdout.splitlines()]
```

After:

```python
from hermes.ops_client import OpsClient
ops = OpsClient()
containers = ops.list_containers()
```

- [ ] **Step 3: Pattern: replace logs**

Before:

```python
proc = subprocess.run(["docker", "logs", "--tail", str(n), name], capture_output=True, text=True)
return proc.stdout
```

After:

```python
return ops.container_logs(name, tail=n)
```

- [ ] **Step 4: Pattern: replace restart / compose**

Same shape: `ops.restart_container(name)`, `ops.compose_restart(service=svc)`.

- [ ] **Step 5: Add a shared OpsClient instance**

In Hermes' tool-init module (likely `hermes/tools/__init__.py`):

```python
from hermes.ops_client import OpsClient
_ops_singleton = OpsClient()

def ops() -> OpsClient:
    return _ops_singleton
```

- [ ] **Step 6: Run Hermes' existing test suite**

```bash
pytest hermes/ -v
```
Expected: existing tests still pass (no behavior regressions).

- [ ] **Step 7: Commit**

```bash
git add hermes/
git commit -m "refactor(hermes): route Docker verbs through ops-controller HTTP API"
```

---

## Task 8: Remove Docker socket from Hermes compose services

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Edit hermes-gateway**

In `hermes-gateway`'s service block, REMOVE these two:

```yaml
    group_add: ["0"]
```

```yaml
      - /var/run/docker.sock:/var/run/docker.sock
```

Also REMOVE any `DOCKER_HOST=unix:///var/run/docker.sock` env var if present.

ADD env wiring for ops-controller (if not already present):

```yaml
      - OPS_CONTROLLER_URL=http://ops-controller:9000
      - OPS_CONTROLLER_TOKEN=${OPS_CONTROLLER_TOKEN:?required}
```

- [ ] **Step 2: Edit hermes-dashboard**

Same removals; same env wiring.

- [ ] **Step 3: Recreate Hermes services**

```bash
docker compose up -d --force-recreate hermes-gateway hermes-dashboard
```

- [ ] **Step 4: Verify the socket is gone**

```bash
docker exec ordo-ai-stack-hermes-gateway-1 ls /var/run/docker.sock
```
Expected: `ls: cannot access '/var/run/docker.sock': No such file or directory`. Exit 2.

- [ ] **Step 5: Verify ops-client reach works from inside Hermes**

```bash
docker exec ordo-ai-stack-hermes-gateway-1 \
  curl -s -H "Authorization: Bearer $OPS_CONTROLLER_TOKEN" \
  http://ops-controller:9000/containers | head -c 200
```
Expected: JSON list of containers.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(hermes): remove raw Docker socket; route through ops-controller"
```

---

## Task 9: Trim env vars Hermes doesn't actually use

**Files:**
- Modify: `docker-compose.yml`

The spec says: "Move secrets [Hermes] does not actually use (Tavily, GitHub PAT, HF, Civitai) out of its env block."

- [ ] **Step 1: Audit which tokens Hermes actually needs**

Currently passed in `hermes-gateway` env: confirm via `git grep` in Hermes source. The Discord bot token and `LITELLM_MASTER_KEY` are needed. Tavily/GitHub PAT/HF/Civitai are typically only needed by `mcp-gateway`, `comfyui`, `gguf-puller`.

- [ ] **Step 2: Remove unused tokens from hermes-gateway env block**

Remove (if present, and if Hermes doesn't use them):

```yaml
      - TAVILY_API_KEY=...
      - GITHUB_PERSONAL_ACCESS_TOKEN=...
      - HF_TOKEN=...
      - CIVITAI_TOKEN=...
```

(Keep `DISCORD_BOT_TOKEN_FILE` if Plan B has shipped, else `DISCORD_BOT_TOKEN`.)

- [ ] **Step 3: Same audit + trim for hermes-dashboard**

- [ ] **Step 4: Recreate**

```bash
docker compose up -d --force-recreate hermes-gateway hermes-dashboard
```

- [ ] **Step 5: Verify Hermes still functions**

Smoke test via Discord (if connected) or hermes-dashboard UI: ask Hermes to list containers, restart a service, fetch logs.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(hermes): trim env vars Hermes does not directly use"
```

---

## Task 10: Acceptance test — Hermes' Docker socket is gone

**Files:**
- Create: `tests/test_hermes_socket_absent.py`

- [ ] **Step 1: Write the test**

```python
import json
import subprocess


def test_hermes_gateway_has_no_docker_sock():
    r = subprocess.run(
        ["docker", "exec", "ordo-ai-stack-hermes-gateway-1",
         "test", "-S", "/var/run/docker.sock"],
        capture_output=True,
    )
    assert r.returncode != 0, "FAIL: /var/run/docker.sock present in hermes-gateway"


def test_hermes_dashboard_has_no_docker_sock():
    r = subprocess.run(
        ["docker", "exec", "ordo-ai-stack-hermes-dashboard-1",
         "test", "-S", "/var/run/docker.sock"],
        capture_output=True,
    )
    assert r.returncode != 0


def test_hermes_gateway_not_in_root_group():
    r = subprocess.run(
        ["docker", "inspect", "ordo-ai-stack-hermes-gateway-1"],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(r.stdout)
    group_add = parsed[0]["HostConfig"].get("GroupAdd", []) or []
    assert "0" not in group_add, "FAIL: group_add ['0'] still present (root-group access)"


def test_hermes_can_reach_ops_controller():
    """Hermes should still be able to call ops-controller — that's the whole point."""
    r = subprocess.run(
        ["docker", "exec", "ordo-ai-stack-hermes-gateway-1",
         "wget", "-qO-", "http://ops-controller:9000/health"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
```

- [ ] **Step 2: Run, verify pass**

```bash
pytest tests/test_hermes_socket_absent.py -v
```
Expected: 4/4 pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hermes_socket_absent.py
git commit -m "test(hermes): assert Docker socket and root-group access are gone"
```

---

## Task 11: Operator runbook

**Files:**
- Create: `docs/runbooks/bounded-hermes.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Bounded Hermes — Operator Runbook

## Mental model

Hermes used to hold `/var/run/docker.sock` directly, giving it (and any
prompt-injection of it) full Docker daemon access. Now it holds nothing
privileged: when it needs to restart a service, fetch logs, or manage the
compose stack, it makes an HTTP call to ops-controller, which is the
single holder of the socket.

Ops-controller emits one structured audit line per privileged call to
`data/ops-controller/audit.jsonl`. `tail -f` it to see what Hermes is
doing in real time.

## What Hermes can still do

- `list_containers()` → `GET /containers`
- `container_logs(name, tail=N)` → `GET /containers/{name}/logs`
- `restart_container(name)` → `POST /containers/{name}/restart`
- `compose_up | compose_down | compose_restart(service=...)` →
  `POST /compose/{verb}`

Whole-stack compose ops require an explicit `confirm=true` to prevent
accident-prompts from taking the stack down.

## What Hermes can no longer do

- `docker exec` into other containers — by design. Specific named verbs
  only. If you find yourself wanting `exec`, add a named verb to
  ops-controller instead.
- `docker inspect` other containers — high-value tokens that live in
  Docker secrets are now invisible to Hermes even with prompt injection.
- Mount new volumes, create containers from arbitrary images, or
  invoke any Docker SDK call ops-controller doesn't explicitly expose.

## Audit log

```bash
tail -f data/ops-controller/audit.jsonl | jq
```

Each line:

```json
{"ts": 1745611200.123, "caller": "hermes", "action": "container.restart",
 "target": "open-webui", "result": "ok"}
```

Rotation: when `audit.jsonl` exceeds 50MB, it's renamed to `audit.1.jsonl`
and a fresh `audit.jsonl` starts. One historical generation; older data
is dropped. Increase `AUDIT_LOG_MAX_BYTES` in compose to retain more.

## Adding a new privileged verb

1. Write a failing test in `ops-controller/test_endpoints.py`.
2. Implement the endpoint in `ops-controller/main.py`. Pattern:
   `_verify_token(authorization)` → do work → `_audit.record(...)` → return.
3. Add a method on `OpsClient` in `hermes/ops_client.py`.
4. Migrate the Hermes call site to use it.
5. Test, commit, restart `ops-controller` and `hermes-gateway`.

## Recovery — ops-controller down

When ops-controller is down, Hermes can't perform any privileged action.
The stack itself stays up; only Hermes-driven ops are blocked. From the
host directly: `docker compose restart ops-controller`. The host shell
retains full Docker access (this is intentional — the host operator is
still trusted).

## Recovery — Hermes ops_client misconfigured

Symptom: every Hermes operation fails with `OPS_CONTROLLER_TOKEN env var
is empty` or 401 from ops-controller.

Fix: confirm `OPS_CONTROLLER_TOKEN` in `~/.ai-toolkit/runtime/.env`
matches the value ops-controller uses. Both read from the same source
(see Plan B § Task 5). After fix: `docker compose restart hermes-gateway`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/bounded-hermes.md
git commit -m "docs(hermes): operator runbook for bounded Hermes / ops-controller verbs"
```

---

## Self-review checklist

- [ ] Every task has concrete file paths and complete code/scripts (no "TBD" or "see above").
- [ ] Spec coverage: § Components > ops-controller new endpoints (Tasks 2-5), no `docker exec` (omitted by design, called out in Task 11), audit log (Tasks 1, 11), § Components > Hermes socket removal (Task 8), trim env vars (Task 9), § Failure modes ops-controller compromised (Task 11), Hermes prompt-injected (Task 10), § Testing — Hermes ops-controller round-trip (Tasks 6-7), audit-log structure (Tasks 1-5).
- [ ] Spec items deferred to Plan A: SSO, oauth2-proxy. (No dependency.)
- [ ] Spec items deferred to Plan B: Docker secrets for high-value tokens. Plan C's Task 9 references `DISCORD_BOT_TOKEN_FILE` *if Plan B has shipped*.
- [ ] Type/method consistency: `OpsClient.list_containers()` / `container_logs()` / `restart_container()` / `compose_{up,down,restart}` used identically across `hermes/ops_client.py`, `hermes/test_ops_client.py`, Task 7's migration patterns, and the runbook in Task 11.
- [ ] Public-repo safety: no real tokens, real container names beyond the public `ordo-ai-stack-*` naming, or personal data in committed code.
