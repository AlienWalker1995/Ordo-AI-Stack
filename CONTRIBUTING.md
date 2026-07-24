# Contributing

Thanks for contributing to Ordo.

> **The stack is Ordo, defined and operated entirely from the repo root.** Config is rendered from `ordo.yaml` (tracked template: `ordo.example.yaml`); the old top-level V1 tree was removed 2026-07-24 (see [docs/LEGACY-CLEANUP.md](docs/LEGACY-CLEANUP.md) for history). Changes to the stack belong at the repo root.

## Building and testing the stack

- **Tests (no host Python needed)** — run in a throwaway container:
  ```bash
  docker run --rm -v "$PWD:/w" -w /w python:3.11-slim \
    sh -c "pip install -q pyyaml pytest && python -m pytest -q tests/substrate"
  ```
  (or `pip install -e .` then `python -m pytest tests/substrate` from the repo root, with `PYTHONPATH=.`). CI runs a path-gated `substrate` job — see `.github/workflows/ci.yml`.
- **Render + deploy** — edit the declarative source `ordo.yaml`, then `ordo render` and bring up the rendered compose from `out/` (`docker compose -p ordo …`). Never hand-edit `out/*` — it's regenerated. See [`docs/operator-guide.md`](docs/operator-guide.md) and [`docs/history/CUTOVER.md`](docs/history/CUTOVER.md).
- **Service images** build from `docker/<name>/` (each has a README with the exact context).

## What not to commit

This repo is public. **Never commit**:

- **`out/secrets.env`** — operator secret values (rendered from `secrets.env.example`). Gitignored.
- **`ordo.yaml`** — operator-real source (host paths, tailnet hostname/IP). Only `ordo.example.yaml` is tracked. Gitignored.
- **`data/`** — user-specific runtime state (Hermes session data, Discord guild/user IDs, MCP config). Gitignored.
- **`models/`** — model files. Gitignored.

Shared code should use placeholders (e.g. `YOUR_GUILD_ID`) or read from environment variables. See [SECURITY.md](SECURITY.md) for details.
