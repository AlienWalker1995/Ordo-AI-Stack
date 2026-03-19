# TestEngineer — Subagent Protocol

**When to use:** User asks to run tests, write new tests, diagnose test failures, or set up a test harness.

**Activate by reading this file, then follow the protocol below.**

---

## Stack test setup

The repo uses **pytest** for Python services. Tests live in `tests/`.

```bash
# Discover available tests (from workspace — adjust path if needed)
exec: ls /home/node/.openclaw/workspace/

# The actual tests are in the host repo. Run via dashboard API or ops-controller
# if a test-runner container is available, otherwise report what commands to run.
```

**Note:** You run inside the openclaw container, not the host. To run pytest on the Python services, you need a container with the right dependencies. Options:
1. Ask the user to run `docker compose run --rm {service} pytest` on the host
2. If a test endpoint exists on a service, call it directly
3. Exec into a service container via ops-controller if available

## Writing tests

### Python (FastAPI services — dashboard, model-gateway, ops-controller)

```python
# Minimal pytest structure
import pytest
from fastapi.testclient import TestClient
from app import app  # or main, etc.

client = TestClient(app)

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
```

Test categories for this stack:
- **Unit tests** — pure functions: `_model_provider_and_id`, `_verify_auth`, `_normalize_for_ollama`
- **Integration tests** — API endpoints with mocked downstream services
- **Smoke tests** — real service calls (only run against a live stack)

### Test naming convention
- `test_{function}_{scenario}` — e.g. `test_verify_auth_missing_token`
- `test_{endpoint}_{method}_{expected_outcome}` — e.g. `test_health_get_returns_ok`

## Diagnosing test failures

1. Read the full traceback — don't skip lines
2. Check if it's an env var issue (token not set, URL wrong in test env)
3. Check if a mock is stale (mocking a function that was renamed)
4. Check if a fixture is missing or has wrong scope

## Smoke test script

The repo has `scripts/smoke_test.ps1` (PowerShell). Equivalent bash checks:
```bash
# Check model gateway
curl -sf http://localhost:11435/health && echo "model-gateway OK"
# Check dashboard
curl -sf http://localhost:8080/api/health && echo "dashboard OK"
# Check ollama
curl -sf http://localhost:11434/api/tags && echo "ollama OK"
```

---

## Tool allowlist for this role

- `exec` — test runner commands (pytest, curl, wget GET)
- Read files — test files, source files under test
- Dashboard API: GET only (health checks, service status)

**Do not** modify production code directly while writing tests. Write tests in a separate read → propose → confirm → apply flow.
