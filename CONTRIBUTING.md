# Contributing

Thanks for contributing to Ordo.

> **The stack is Ordo, defined and operated entirely from the repo root.** Config is rendered from `ordo.yaml` (tracked template: `ordo.example.yaml`); the old top-level V1 tree was removed 2026-07-24. Changes to the stack belong at the repo root.

## Building and testing the stack

- **Tests (no host Python needed)** — run in a throwaway container:
  ```bash
  docker run --rm -v "$PWD:/w" -w /w python:3.11-slim \
    sh -c "pip install -q -r requirements-dev.txt && PYTHONPATH=. python -m pytest -q tests/substrate"
  ```
  (or `pip install -r requirements-dev.txt` then `PYTHONPATH=. python -m pytest tests/substrate` from the repo root). The main suite is `pip install -r tests/requirements.txt`, then `python -m pytest tests/ -q --ignore=tests/substrate`. CI runs both, the substrate job path-gated; see `.github/workflows/ci.yml`.
- **Render + deploy:** edit the declarative source (`out/ordo.yaml`), then `python -m ordo --source out/ordo.yaml render --out out` and `ordo up --all` from the repo root (never a hand-assembled compose bring-up). Never hand-edit `out/*`: it's regenerated. See [`docs/operator-guide.md`](docs/operator-guide.md).
- **Service images** (`ordo/<name>`) are built by `ordo build <svc>` (or `ordo build --all`), which tags each with the commit that last changed its build context; never by hand.

## Edge serving contract

Every UI serves at its origin **root** behind a plain SSO reverse_proxy (`import sso_service <upstream>`); **no** edge- or sidecar-side path rewriting (`handle_path`/`strip_prefix`, `X-Forwarded-Prefix` injection, `sub_filter`) in a UI block — enforced by `tests/test_caddyfile_invariants.py`. Two permanent exceptions: Grafana's native same-origin `/grafana/` embed, and n8n's external `:443/n8n` webhook/OAuth-callback URLs (registered outside the stack).

## What not to commit

This repo is public. **Never commit**:

- **`out/secrets.env`**: operator secret values (materialized from the secret store by `ordo secrets materialize`). Gitignored.
- **`ordo.yaml`** — operator-real source (host paths, tailnet hostname/IP). Only `ordo.example.yaml` is tracked. Gitignored.
- **`data/`** — user-specific runtime state (Hermes session data, Discord guild/user IDs, MCP config). Gitignored.
- **`models/`** — model files. Gitignored.

Shared code should use placeholders (e.g. `YOUR_GUILD_ID`) or read from environment variables. See [SECURITY.md](SECURITY.md) for details.
