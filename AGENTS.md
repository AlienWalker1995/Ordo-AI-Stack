# Repository Guidelines

Ordo is defined and operated from the repo root. Config is rendered from `ordo.yaml` (tracked template: `ordo.example.yaml`) into `out/` (gitignored) by `ordo render`; GPU work is scheduled by the control plane (`ops-controller`), and every service lives in `services/<id>/`. Operator workflow: `docs/operator-guide.md`.

## Working on the render substrate (`ordo/`)
- **Source of truth:** the operator's `ordo.yaml`, which lives at `out/ordo.yaml`. Never hand-edit rendered files in `out/`; they do not survive a re-render. Use the source's `overrides:` block.
- **Always pass the source:** `python -m ordo --source out/ordo.yaml render --out out` (`--source` goes before the subcommand). Without it the CLI renders the public `ordo.example.yaml` over `out/`, dropping the host paths the live stack needs.
- **Bring-up:** render as above, then `ordo up --all` (whole stack, every rendered profile), `ordo up <svc>...` or `ordo recreate <svc>...` from the repo root. Never hand-assemble `docker compose -p ordo ... up`: the command shares ops-controller's argv builder (both env files, every profile, `--no-deps` for named services, and caddy's netns members named alongside it) and refuses while the GPU lease would be violated. `--dry-run` prints the argv. The one exception is the evals one-shot, `docker compose -p ordo --profile evals run --rm evals` (`scripts/evals/run.sh`): a `run --rm`, not a bring-up. `ordo up` builds any first-party image the rendered compose names that Docker lacks (`--no-build` skips it).
- **First-party images (`ordo/<name>`) are built by `ordo build`, never by hand:** `out/docker-compose.yml` is image-only, so `docker compose build` there does nothing, and a hand `docker build -t ordo/<name>:latest` is invisible to the stack. `ordo build --all` (or `ordo build <svc>...`) tags each image with the 12-character sha of the last commit that changed its build context (`-dirty` for uncommitted changes), skips tags that already exist, moves `ordo/<name>:current`, and records the tag in `out/images.json`. Every render (host and ops-controller) pins the compose to that record, so a deploy is `ordo build --all`, render, `ordo up --all`. Manifests, `ordo/compose.py` and a catalog `backend_image` (the patched llama.cpp build, `ordo build llamacpp`) declare first-party images untagged; never add `:latest` (`tests/substrate/test_images.py` enforces it). Contexts come from `ordo/buildspec.py`; the tag model lives in `ordo/images.py`.
- **The control plane renders with its own copy of `ordo/`:** `ops-controller` (`services/ops-controller/Dockerfile`, built from the repo root against the `.dockerignore` allowlist) ships the `ordo/` package and re-renders `out/` when a model is switched. After changing render code, `ordo build ops-controller`, render, then `ordo recreate ops-controller`. Every render records a substrate digest (`ordo/substrate.py`, a hash of every render input) in `out/manifest.json`; ops-controller answers 409 instead of re-rendering when its own digest differs, and `ordo doctor` reports a running ops-controller that differs from the checkout.
- **Binds on services the control plane recreates** must use host paths (`${BASE_PATH}/...`), never `./...`: compose runs inside ops-controller with its project directory at `/config`. `tests/substrate/test_compose.py` enforces this.

## Project structure
- `ordo/`: the render substrate and control plane (`control.py` is the ops-controller API, `scheduler.py` the GPU lease arbiter).
- `services/<id>/`: one directory per service, holding its render manifest (`plugin.yaml` / `agent.yaml` / `dashboard.yaml`), an optional `catalog.json` dashboard card, and its build context (`Dockerfile` + sources). Agents are manifests (`services/<id>/agent.yaml`); Hermes is `default: true` (see `docs/agents.md`).
- `catalog/models.yaml`: the model catalog a model switch picks from.
- `tests/` (with `tests/substrate/` for the render engine), `scripts/` (operational scripts), `docs/`, `monitoring/` (Prometheus + Grafana provisioning). `data/`, `models/` and `out/` are runtime state, never committed.

## Build, test and lint
- `pip install -r tests/requirements.txt`, then `python -m pytest tests/ -q --ignore=tests/substrate` (the main CI job).
- `pip install -r requirements-dev.txt`, then `PYTHONPATH=. python -m pytest tests/substrate -q` (the path-gated substrate job, run on changes to `ordo/`, `catalog/` or `services/`).
- `python -m ruff check .`: the lint gate. It honors `.gitignore`, so one command covers everything.
- Dependencies are pinned to exact versions (for example `services/dashboard/dashboard/requirements.txt`, which `tests/requirements.txt` includes). Bump deliberately, rebuild, retest.

## Coding style
Python 3.11+ (the ops-controller image that ships `ordo/` runs 3.11, and ruff targets `py311`), `from __future__ import annotations` at the top of every file. Ruff enforces a 120-character line and the `E`, `F`, `I` and `UP` rules. `snake_case` for files, functions and variables, `PascalCase` for classes, `test_*.py` for tests. Keep service logic inside its own `services/<id>/` directory instead of adding cross-service utilities at the root.

## Dashboard (`services/dashboard/dashboard/`)
- **Backend:** FastAPI. `routes_console.py` serves the five pages (`/api/overview`, `/api/activity`, `/api/services/table`, `/api/models` + `/switch` + `/delete`, `/api/media` + `/view`, `/api/perf/*`); it fetches concurrently and hands plain dicts to `console.py`, which holds the pure logic (verdicts, attention items, model slots). Keep that split: logic in `console.py`, I/O in the routes.
- Blocking I/O (pynvml, psutil, subprocess) goes through `asyncio.to_thread`; shared in-process state is guarded by `_state_lock`.
- Degrade, don't 500: an unreachable dependency becomes "unavailable" data, logged at `DEBUG`.
- Auth (`dashboard/auth.py`): every state-changing `/api/*` route, and everything under `/api/ops/` and `/api/orchestration/` (except `/readiness`), needs a principal: the edge SSO identity (`X-Forwarded-Email`, trusted only when the TCP peer is `caddy`) or `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` for internal callers. With the edge off (the render decides, never a flag) the local operator signs in instead: `DASHBOARD_LOCAL_LOGIN_TOKEN` (rendered to the dashboard only) buys a signed session cookie (`routes_auth.py`). Health and read-only views stay open. Don't add per-service tokens.
- A model switch goes only through `/api/models/switch` (catalog id, then render, then recreate). Never write `.env` directly; the next render undoes it.
- **Frontend:** React 18 + Vite + Tailwind in `frontend/`. Pages in `src/pages/*Page.jsx` (Overview, Services, Models, Media, Performance), shared primitives in `src/components/ui.jsx`, the API client and polling hooks in `src/api.js`, formatters in `src/lib/format.js`. Design tokens (colors, type scale, radii) live in `tailwind.config.js`: style with those utilities, never hardcoded hex. Build with `npm ci && npm run build`; the backend serves `frontend/dist/`.
- Routes that were retired stay listed in `tests/test_dashboard_retired_routes.py` so they cannot quietly return.

## GPU work
Every GPU render goes through the gate (`$COMFYUI_URL`, `http://comfyui-gate:8188`), which takes the scheduler lease first. Never submit to ComfyUI directly.

## Testing
Add or update `pytest` coverage for every behavior change. Use `fastapi.testclient.TestClient` for endpoints, and mock external dependencies (pynvml, httpx, docker) with `unittest.mock.patch` or `monkeypatch`.

## Commits and pull requests
Conventional prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`), with a service scope when the change is isolated to one (`feat(dashboard):`). `main` is protected: work on a branch and open a PR. PRs describe the user-visible change and the validation performed; include screenshots when the dashboard UI changes.

## Security
This repository is public: never commit `data/`, `models/`, `out/` (it contains `out/secrets.env`), tokens, emails or hostnames. Start from `out/secrets.env.example`; encrypted at-rest secrets live in `secrets/` (SOPS). Review `SECURITY.md` before exposing services beyond localhost. Host-level container options such as `pid: host` go through `ordo.yaml`'s `overrides:` block with a comment saying why, never by hand-editing `out/`.
