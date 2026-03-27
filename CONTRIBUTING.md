# Contributing

Thanks for contributing to Ordo AI Stack.

## What not to commit

This repo is public. **Never commit**:

- **`.env`** — contains API keys, tokens, paths. Use `.env.example` as a template.
- **`data/`** — contains user-specific config (OpenClaw gateway token, Discord guild/user IDs, session data, MCP config). Gitignored.
- **`models/`** — model files. Gitignored.
- **`overrides/compute.yml`** — hardware-specific. Gitignored.
- **`mcp/.env`** — MCP API keys. Gitignored.

Shared code should use placeholders (e.g. `YOUR_GUILD_ID`, `BASE_PATH=.`) or read from environment variables. See [SECURITY.md](SECURITY.md) for details.
