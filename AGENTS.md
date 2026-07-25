# Repository Guidelines

> ⚠️ **The stack is Ordo, defined and operated entirely from the repo root.** Config is rendered from `ordo.yaml` (tracked template: `ordo.example.yaml`) into `out/` (gitignored) via `ordo render`, GPU work is scheduled by `ordo serve` (no reactive guardian), and agents are manifests under `services/<id>/agent.yaml` (Hermes default). The old top-level V1 layout (root `docker-compose.yml`, `./compose` / `.\compose.ps1`, `Makefile`, `overrides/`, `scripts/detect_hardware.py`, root `.env.example`, and the root `ops-controller/`, `model-gateway/`, `mcp/` source dirs) was **removed 2026-07-24** (commit `62540bf`) — the repo was the `v2/` stack from that removal until it was **flattened 2026-07-24** (commit `2d4bd9c`): the `v2/` directory no longer exists, its contents live at the repo root; there is no v2, there is only Ordo ([`docs/LEGACY-CLEANUP.md`](docs/LEGACY-CLEANUP.md) is the historical planning record for the V1 removal). Work on the stack at the repo root and follow `docs/operator-guide.md` (+ `docs/history/CUTOVER.md`). Every service is now co-located under `services/<id>/` — its render manifest (`plugin.yaml` / `agent.yaml` / `dashboard.yaml`) sits next to its build context (`Dockerfile` + sources). The source dirs that survived the V1 removal (the dashboard, `comfyui-mcp`, `orchestration`, etc., which still build live Ordo service images) moved under `services/<id>/` in the 2026-07-25 reorg; the "legacy top-level tree" guidance further below is retained for historical context only and no longer describes on-disk paths.

## Working on Ordo (the render substrate)
- **Source of truth:** `ordo.yaml` (declarative). Never hand-edit rendered outputs in `out/` — they don't survive a re-render; use the source's `overrides:` block.
- **Run the substrate tests (no host Python needed):**
  ```bash
  docker run --rm -v "$PWD:/w" -w /w python:3.11-slim \
    sh -c "pip install -q pyyaml pytest && python -m pytest -q tests/substrate"
  ```
  Or, with host Python: `pip install . && python -m pytest tests/substrate -q` from the repo root (`PYTHONPATH=.`) (runtime dep is just PyYAML). CI runs a path-gated `substrate` job (ruff + the mocked-profile suite + a fresh-install render smoke), path-gated on `ordo/**`, `catalog/**`, `services/**` — see `.github/workflows/ci.yml`.
- **Service images** build from `services/<id>/` (each has a README/Dockerfile with the exact context; a few — ops-controller, worker — build from the repo root against the `.dockerignore` allowlist). The dashboard control plane is `ops-api` (manifest under `services/ops-api/`), **not** the old root `ops-controller/`.
- **Agents** are data manifests at `services/<id>/agent.yaml`; Hermes is `default: true`. See `docs/agents.md`.

## Project Structure & Module Organization (service source dirs)
Each service is a self-contained directory under `services/<id>/` holding both its render manifest (`plugin.yaml` / `agent.yaml` / `dashboard.yaml`) and its build context (Python sources + `Dockerfile` + a README with the exact context) — e.g. the dashboard sources at `services/v1-parity/dashboard/`, `services/orchestration/`, `services/comfyui-mcp/`, `services/worker/`. Most images build from their own `services/<id>/` context; the two that need the render substrate — `ops-controller` (`services/ops-controller/Dockerfile`, `docker build -f services/ops-controller/Dockerfile -t ordo/ops-controller:latest .`) and `worker` (`services/worker/Dockerfile`) — build from the repo root, where a root `.dockerignore` allowlist (`ordo/`, `catalog/`, `services/`) keeps the context tiny. The old root `model-gateway/` and `ops-controller/` source dirs, and the `docker-compose.yml` / `compose.ps1` / `compose` entry points that built them, were removed 2026-07-24 (see `docs/operator-guide.md`). Tests are centralized in `tests/`, with fixtures under `tests/fixtures/`. Operational scripts live in `scripts/`, documentation in `docs/`, generated runtime data in `data/`, and local model assets in `models/`. The `overrides/` dir (`compute.yml`, `gpu-assignments.yml`) is gone — hardware/GPU detection now happens at `ordo render` time (`hardware: auto` / `ordo detect`) and is written into `out/`. **Note:** each service's manifest + build context are co-located under `services/<id>/` — edits to the stack belong there.

## Build, Test, and Development Commands (root service sources)
Install Python test dependencies with `pip install -r tests/requirements.txt`.

- `python -m pytest tests/ -v`: run the full root Python test suite.
- `python -m pytest tests/ -q`: quiet run used for CI checks.
- `python -m ruff check dashboard tests rag-ingestion scripts comfyui-mcp orchestration-mcp worker`: run lint checks used in CI.
- `docker compose build <service> && docker compose up -d <service>`: rebuild and hot-swap a single service, run from `out/` against the rendered compose (see `docs/operator-guide.md`). There is no root `make up`/Makefile or `./compose` / `.\compose.ps1` wrapper anymore — bring-up is always `ordo render --out out` followed by `docker compose -p ordo ... up` from `out/`.

## Coding Style & Naming Conventions
Target Python 3.12+. Ruff is the enforced linter; `pyproject.toml` sets a 120-character line length and enables `E`, `F`, `I`, and `UP` rules. Follow existing module patterns: `snake_case` for files, functions, and variables, `PascalCase` for classes, and `test_*.py` for tests. Keep service-specific logic inside its owning directory instead of adding cross-service utility modules at the repo root. Always use `from __future__ import annotations` at the top of Python files.

## Dashboard Service Patterns (`dashboard/`)
The dashboard backend is a FastAPI app in `dashboard/app.py` (~1950 lines). When adding endpoints:
- Use `asyncio.to_thread(blocking_fn)` for any blocking I/O (pynvml, psutil, subprocess) — never block the event loop.
- Shared in-process state (throughput samples, benchmarks) is protected by `_state_lock` (a `threading.Lock`). Always acquire it with `with _state_lock:`.
- Hardware/health endpoints are public (no auth). The `_verify_auth(request)` / `DASHBOARD_AUTH_TOKEN` Bearer path still exists in code but is **unset in the Ordo deployment** (`AUTH_REQUIRED=False`) — the Caddy edge SSO is the sole gate. Don't reintroduce a per-service token requirement.
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
Never commit `data/`, `models/`, or the rendered `out/` (includes `out/secrets.env`). Start from `out/secrets.env.example`, keep tokens in environment variables, and review `SECURITY.md` before exposing services beyond localhost. (The root `.env` / SOPS `secrets/.env.sops` and the root `mcp/.env` / `overrides/compute.yml` were the V1 path; `mcp/` and `overrides/` no longer exist.) When adding monitoring containers that need host process visibility, use `pid: host` via `ordo.yaml`'s `overrides:` block (not by hand-editing rendered `out/docker-compose.yml`), and document why in an inline comment.
