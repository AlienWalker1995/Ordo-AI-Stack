# Repository Guidelines

## Project Structure & Module Organization
Core Python services live in `dashboard/`, `model-gateway/`, `ops-controller/`, `orchestration-mcp/`, and `comfyui-mcp/`. Docker and environment entry points are at the repo root: `docker-compose.yml`, `compose.ps1`, `compose`, `ordo-ai-stack.ps1`, and `ordo-ai-stack`. Tests are centralized in `tests/`, with fixtures under `tests/fixtures/`. Operational scripts live in `scripts/`, documentation in `docs/`, generated runtime data in `data/`, and local model assets in `models/`. Treat `overrides/compute.yml` as machine-specific generated output — do not edit it for persistent changes; use a separate override file instead.

## Build, Test, and Development Commands
Install Python test dependencies with `pip install -r tests/requirements.txt`.

- `python -m pytest tests/ -v`: run the full Python test suite.
- `python -m pytest tests/ -q`: quiet run used for CI checks.
- `python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts comfyui-mcp orchestration-mcp`: run lint checks used in CI.
- `make test`, `make lint`, `make smoke-test`: Linux/macOS shortcuts for the core workflows.
- `.\compose.ps1 up -d` or `./compose up -d`: bring up the stack with hardware detection.
- `.\ordo-ai-stack.ps1 initialize` or `./ordo-ai-stack initialize`: full bootstrap, including directory setup and container rebuilds.
- `docker compose build <service> && docker compose up -d <service>`: rebuild and hot-swap a single service.

## Coding Style & Naming Conventions
Target Python 3.12+. Ruff is the enforced linter; `pyproject.toml` sets a 120-character line length and enables `E`, `F`, `I`, and `UP` rules. Follow existing module patterns: `snake_case` for files, functions, and variables, `PascalCase` for classes, and `test_*.py` for tests. Keep service-specific logic inside its owning directory instead of adding cross-service utility modules at the repo root. Always use `from __future__ import annotations` at the top of Python files.

## Dashboard Service Patterns (`dashboard/`)
The dashboard backend is a FastAPI app in `dashboard/app.py` (~1950 lines). When adding endpoints:
- Use `asyncio.to_thread(blocking_fn)` for any blocking I/O (pynvml, psutil, subprocess) — never block the event loop.
- Shared in-process state (throughput samples, benchmarks) is protected by `_state_lock` (a `threading.Lock`). Always acquire it with `with _state_lock:`.
- Hardware/health endpoints are public (no auth). All `/api/*` endpoints that modify state require auth when `DASHBOARD_AUTH_TOKEN` is set — check `_verify_auth(request)`.
- New endpoints go immediately before the `# --- Static ---` comment at the bottom of `app.py`.
- Error handling: catch exceptions from optional dependencies (pynvml, httpx) and return a degraded-but-valid response rather than a 500. Log at `DEBUG` level with `logger.debug(...)`.

## Frontend Conventions (`dashboard/static/index.html`)
The dashboard frontend is a single vanilla JS/HTML file — no build step, no framework. When modifying it:
- All colors are CSS custom properties in `:root`. Never hardcode hex values in component styles; add a new variable to `:root` if needed.
- Fonts: `Barlow Condensed` for section labels and row labels (uppercase, `letter-spacing: .05em`), `DM Sans` for body text, `JetBrains Mono` for all numeric values and status codes.
- New sections follow a `<section id="...">` wrapper with the generic `section` CSS selector providing card styling. Insert sections by their logical position in the page, not at the bottom.
- JavaScript uses `fetch` + `async/await`. Polling intervals use `setInterval` at the bottom of the script block. New polls go alongside existing ones.
- No new npm dependencies. No build step. No bundler.

## Testing Guidelines
Add or update `pytest` coverage for every behavior change. Prefer focused unit tests near related coverage — e.g., `tests/test_dashboard_gpu_processes.py` for GPU process endpoint changes. Use `fastapi.testclient.TestClient` for endpoint tests. Mock external dependencies (pynvml, httpx, docker) with `unittest.mock.patch` or pytest `monkeypatch`. Use fixtures from `tests/fixtures/` when possible.

### Contract Test Pattern
When a config value or behavior must be enforced across redeployments, add it to two places:
1. A normalizer function in `openclaw/scripts/add_mcp_plugin_config.py` (see `normalize_internal_hooks`, `normalize_llm_idle_timeout` for examples).
2. An assertion in `tests/test_openclaw_runtime_contract.py` for both the live config file and the normalizer function.

This ensures the value is re-enforced any time `openclaw-config-sync` runs.

## Commit & Pull Request Guidelines
Recent history uses Conventional Commit prefixes such as `feat:`. Continue with `feat:`, `fix:`, `docs:`, `refactor:`, or `test:` followed by a short imperative summary. Use `feat(service):` scope when the change is isolated to one service (e.g., `feat(dashboard):`, `fix(bridge):`). Pull requests should describe the user-visible change, list validation performed, link related issues, and include screenshots only when UI behavior in `dashboard/` changes.

## Security & Configuration Tips
Never commit `.env`, `mcp/.env`, `data/`, `models/`, or `overrides/compute.yml`. Start from `.env.example`, keep tokens in environment variables, and review `SECURITY.md` before exposing services beyond localhost. When adding monitoring containers that need host process visibility, use `pid: host` in `overrides/compute.yml` (not in `docker-compose.yml`), and document why in an inline comment.
