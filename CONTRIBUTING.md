# Contributing

Thanks for contributing to Ordo.

> **The stack is Ordo, defined and operated from [`v2/`](v2/).** Config is rendered from `v2/ordo.yaml`; the old top-level layout is the pre-render stack, superseded and pending removal (see [docs/LEGACY-CLEANUP.md](docs/LEGACY-CLEANUP.md)). Changes to the stack belong in `v2/`.

## Building and testing the stack

- **Tests (no host Python needed)** — run in a throwaway container:
  ```bash
  docker run --rm -v "$PWD/v2:/w" -w /w python:3.11-slim \
    sh -c "pip install -q pyyaml pytest && python -m pytest -q"
  ```
  (or `pip install -e ./v2` then `cd v2 && python -m pytest -q`). CI runs a path-gated `v2-substrate` job — see `.github/workflows/ci.yml`.
- **Render + deploy** — edit the declarative source `v2/ordo.yaml`, then `python -m ordo.cli render --out out` and bring up the rendered compose (`docker compose -p ordo …`). Never hand-edit `v2/out/*` — it's regenerated. See [`v2/README.md`](v2/README.md) and [`v2/CUTOVER.md`](v2/CUTOVER.md).
- **Service images** build from `v2/docker/<name>/` (each has a README with the exact context).

## What not to commit

This repo is public. **Never commit**:

- **`v2/out/secrets.env`** — operator secret values (rendered from `secrets.env.example`). Gitignored.
- **`v2/ordo.yaml`** — operator-real source (host paths, tailnet hostname/IP). Only `v2/ordo.example.yaml` is tracked. Gitignored.
- **`data/`** — user-specific runtime state (Hermes session data, Discord guild/user IDs, MCP config). Gitignored.
- **`models/`** — model files. Gitignored.
- **`.env`** *(legacy V1)* — API keys, tokens, paths. Gitignored; use `.env.example` as a template.
- **`overrides/compute.yml`** *(legacy V1)* — hardware-specific. Gitignored.

Shared code should use placeholders (e.g. `YOUR_GUILD_ID`) or read from environment variables. See [SECURITY.md](SECURITY.md) for details.
