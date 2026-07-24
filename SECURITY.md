# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| main    | :white_check_mark: |

This project follows a rolling release model. The `main` branch is supported.

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not** open a public issue for security vulnerabilities.
2. Email the maintainers or use GitHub Security Advisories if available.
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will acknowledge receipt and aim to respond within a reasonable timeframe.

## Security Considerations

> **Secrets (production, since the 2026-07-09 cutover):** operator secret **values** live in a gitignored **`out/secrets.env`**, rendered from the keys-only `out/secrets.env.example` and kept **separate from derived config** (`.env` stays config-only). Services that need secrets read `secrets.env` as a second `env_file` (`required: false`). Verify with `ordo preflight --secrets out/secrets.env`. **Never commit `out/secrets.env`** (nor the operator-real `ordo.yaml`, which carries host paths + tailnet identity). The SOPS + age at-rest model under `secrets/` still backs the encrypted material. The `.env` / `runtime/.env` notes below describe the legacy V1 secret flow.

### Authentication

- **Open WebUI:** The default `WEBUI_AUTH=False` disables login. This is intended for **local/single-user** use only. If you expose the stack to a network (e.g., via port forwarding or LAN access), **enable authentication** by setting `WEBUI_AUTH=True` in the environment.
- **Dashboard:** No per-service auth — it publishes no host port and is reached only through the Caddy edge (oauth2-proxy + Google SSO), which is the sole gate; internal callers reach it over the stack network. **ops-controller:** Set `OPS_CONTROLLER_TOKEN` (generate with `openssl rand -hex 32`) — it stays token-gated and its port is never exposed.
- **n8n:** No built-in auth by default. If port **5678** is reachable from others (LAN, port-forward, Tailscale), enable n8n authentication (Basic Auth or full user management in n8n settings) or restrict access with a firewall / reverse proxy. Prefer not exposing n8n to the public internet without TLS and auth.

### Network Binding

Services bind to `0.0.0.0` to allow access from other machines on your network. Use a firewall to restrict access if needed.

### Secrets

- **Never commit** `out/.env` or `out/secrets.env`. They are gitignored, along with the operator-real `ordo.yaml`.
- Use `ordo.example.yaml` and `out/secrets.env.example` as templates; copy to `ordo.yaml` / `out/secrets.env`, fill in values locally, then run `ordo render` to produce `out/.env` and `out/docker-compose.yml`.
- API keys (OpenAI, Anthropic, etc.) and tokens should only live in `out/secrets.env`, never in the repository.
- **Never commit** `data/` — it is gitignored and contains user-specific runtime state (Hermes session data, Discord guild/user IDs, MCP config, etc.). All secrets and setup-specific values belong in `data/` or `out/secrets.env`, not in shared code.

### Data

All runtime data is stored under `BASE_PATH/data/` via bind mounts. Ensure appropriate filesystem permissions and backups. The `data/` directory is gitignored and must remain untracked.

## Pre-deployment checklist

- [ ] `OPS_CONTROLLER_TOKEN` set (generate: `openssl rand -hex 32`)
- [ ] `out/.env` / `out/secrets.env` not committed (in `.gitignore`)
- [ ] Ops controller port (9000) not exposed to host/network
- [ ] Dashboard bound to localhost or Tailscale-only when multi-user

## Threat mitigations

| Threat | Check |
|--------|-------|
| docker.sock exposure | Only ops-controller and mcp-gateway mount it; dashboard does not |
| Controller compromise | Token in env; no default; never expose port |
| MCP SSRF (browser worker) | Egress blocks for 100.64/10, RFC1918, 169.254.169.254 — `./scripts/ssrf-egress-block.sh --target all` |
| Secret exfiltration (general) | Controller-only API keys; dashboard `/api/services` strips tokens from returned URLs |
| Unauthenticated admin | Dashboard reached only via the Caddy edge (oauth2-proxy + Google SSO); no host port |

## Audit

- [ ] Audit log path writable: `data/ops-controller/`

## Break-glass

1. **Reset OPS_CONTROLLER_TOKEN:** Generate new token, update `out/secrets.env`, then re-run `docker compose -p ordo … up` from `out/` to restart dashboard + ops-controller
2. **Restore data:** Restore `data/` from a local backup
3. **Disable MCP tools:** Clear `data/mcp/servers.txt` or set to a single safe server
4. **Safe mode:** Stop `mcp-gateway` and `hermes-gateway`; use `llamacpp` + `open-webui` only
