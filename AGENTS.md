# Repository Guidelines

## Project Structure & Module Organization
Core Python services live in `dashboard/`, `model-gateway/`, `ops-controller/`, `orchestration-mcp/`, and `comfyui-mcp/`. Docker and environment entry points are at the repo root: `docker-compose.yml`, `compose.ps1`, `compose`, `ordo-ai-stack.ps1`, and `ordo-ai-stack`. Tests are centralized in `tests/`, with fixtures under `tests/fixtures/`. Operational scripts live in `scripts/`, documentation in `docs/`, generated runtime data in `data/`, and local model assets in `models/`. Treat `overrides/compute.yml` as machine-specific generated output.

## Build, Test, and Development Commands
Install Python test dependencies with `pip install -r tests/requirements.txt`.

- `python -m pytest tests/ -v`: run the full Python test suite.
- `python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts comfyui-mcp orchestration-mcp`: run lint checks used in CI.
- `make test`, `make lint`, `make smoke-test`: Linux/macOS shortcuts for the core workflows.
- `.\compose.ps1 up -d` or `./compose up -d`: bring up the stack with hardware detection.
- `.\ordo-ai-stack.ps1 initialize` or `./ordo-ai-stack initialize`: full bootstrap, including directory setup and container rebuilds.

## Coding Style & Naming Conventions
Target Python 3.12+. Ruff is the enforced linter; `pyproject.toml` sets a 120-character line length and enables `E`, `F`, `I`, and `UP` rules. Follow existing module patterns: `snake_case` for files, functions, and variables, `PascalCase` for classes, and `test_*.py` for tests. Keep service-specific logic inside its owning directory instead of adding cross-service utility modules at the repo root.

## Testing Guidelines
Add or update `pytest` coverage for every behavior change. Prefer focused unit tests near related coverage, for example `tests/test_model_gateway_contract.py` for gateway API behavior or `tests/test_ops_controller_audit.py` for ops-controller changes. Use fixtures from `tests/fixtures/` when possible. For stack-sensitive changes, run `python scripts/validate_openclaw_config.py tests/fixtures/openclaw_valid.json` and the compose smoke path when Docker behavior changes.

## Commit & Pull Request Guidelines
Recent history uses Conventional Commit prefixes such as `feat:`. Continue with `feat:`, `fix:`, `docs:`, `refactor:`, or `test:` followed by a short imperative summary. Pull requests should describe the user-visible change, list validation performed, link related issues, and include screenshots only when UI behavior in `dashboard/` changes.

## Security & Configuration Tips
Never commit `.env`, `mcp/.env`, `data/`, `models/`, or `overrides/compute.yml`. Start from `.env.example`, keep tokens in environment variables, and review `SECURITY.md` before exposing services beyond localhost.
