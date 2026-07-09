# Repository Guidelines

> ⚠️ **Production is the v2 substrate (cutover 2026-07-09; `main` @ `d115035`, PR #72).** The stack is defined and operated from **[`v2/`](v2/)** — config is rendered from `v2/ordo.yaml` into `v2/out/` (gitignored), GPU work is scheduled by `ordo serve` (no reactive guardian), and agents are manifests under `v2/agents/` (Hermes default). The top-level V1 layout below (`docker-compose.yml`, `./compose`, root `ops-controller/`, root `model-gateway/`, etc.) is **LEGACY** — superseded, pending a separate cleanup PR ([`docs/LEGACY-CLEANUP.md`](docs/LEGACY-CLEANUP.md)). When working on the **production stack, work in `v2/`** and follow `v2/README.md`. The guidance below applies to the legacy V1 tree and any V1 code that still needs maintenance before removal.

## Working on v2 (production)
- **Source of truth:** `v2/ordo.yaml` (declarative). Never hand-edit rendered outputs in `v2/out/` — they don't survive a re-render; use the source's `overrides:` block.
- **Run the v2 tests (no host Python needed):**
  ```bash
  docker run --rm -v "$PWD/v2:/w" -w /w python:3.11-slim \
    sh -c "pip install -q pyyaml pytest && python -m pytest -q"
  ```
  Or, with host Python: `pip install -e ./v2 && cd v2&& python -m pytest -q` (runtime dep is just PyYAML). CI runs a path-gated `v2-substrate` job (ruff + the mocked-profile suite + a fresh-install render smoke) — see `.github/workflows/ci.yml`.
- **v2 service images** build from `v2/docker/<name>/` (each has a README with the exact context). The V2 control plane is `ops-api` (built from `v2/docker/ops-api/`), **not** the root `ops-controller/`.
- **Agents** are data manifests at `v2/agents/<id>/agent.yaml`; Hermes is `default: true`. See `v2/agents/README.md`.

## Project Structure & Module Organization (LEGACY V1)
Legacy V1 Python services live in `dashboard/`, `model-gateway/`, `ops-controller/`, `orchestration-mcp/`, and `comfyui-mcp/`. Docker and environment entry points are at the repo root: `docker-compose.yml`, `compose.ps1`, `compose`. Tests are centralized in `tests/`, with fixtures under `tests/fixtures/`. Operational scripts live in `scripts/`, documentation in `docs/`, generated runtime data in `data/`, and local model assets in `models/`. Treat `overrides/compute.yml` as machine-specific generated output — do not edit it for persistent changes; use a separate override file instead. **Note:** the V2 stack has its own copies of the service build contexts under `v2/docker/` — edits to the production stack belong there, not in these root directories.

## Build, Test, and Development Commands (LEGACY V1)
Install Python test dependencies with `pip install -r tests/requirements.txt`.

- `python -m pytest tests/ -v`: run the full (legacy) Python test suite.
- `python -m pytest tests/ -q`: quiet run used for CI checks.
- `python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts comfyui-mcp orchestration-mcp worker`: run lint checks used in CI.
- `make test`, `make lint`, `make smoke-test`: Linux/macOS shortcuts for the core workflows.
- `.\compose.ps1 up -d` or `./compose up -d`: bring up the **legacy V1** stack with hardware detection.
- `.\compose.ps1 up -d --build --force-recreate` or `./compose up -d --build --force-recreate`: full V1 bring-up with hardware detection and rebuild.
- `docker compose build <service> && docker compose up -d <service>`: rebuild and hot-swap a single V1 service.

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

## Commit & Pull Request Guidelines
Recent history uses Conventional Commit prefixes such as `feat:`. Continue with `feat:`, `fix:`, `docs:`, `refactor:`, or `test:` followed by a short imperative summary. Use `feat(service):` scope when the change is isolated to one service (e.g., `feat(dashboard):`, `fix(bridge):`). Pull requests should describe the user-visible change, list validation performed, link related issues, and include screenshots only when UI behavior in `dashboard/` changes.

## Security & Configuration Tips
Never commit `.env`, `mcp/.env`, `data/`, `models/`, or `overrides/compute.yml`. Start from `.env.example`, keep tokens in environment variables, and review `SECURITY.md` before exposing services beyond localhost. When adding monitoring containers that need host process visibility, use `pid: host` in `overrides/compute.yml` (not in `docker-compose.yml`), and document why in an inline comment.
