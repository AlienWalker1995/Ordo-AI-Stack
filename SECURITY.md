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

### Authentication

- **SSO front door:** The default deployment puts the dashboard behind Caddy + oauth2-proxy + Google OIDC, allowlisted by `auth/oauth2-proxy/emails.txt` and bound to your tailnet IP via `${CADDY_BIND}`. The Caddyfile's `:?` failsafe refuses to start with an empty `CADDY_BIND` so the bind never silently degrades to `0.0.0.0:443`. See [docs/runbooks/auth.md](docs/runbooks/auth.md).
- **Dashboard:** Trusts `X-Forwarded-Email` only when the request originates from `DASHBOARD_TRUSTED_PROXY_NET` (default `172.16.0.0/12` — the Docker bridge range). For non-proxy callers it falls back to `Authorization: Bearer ${DASHBOARD_AUTH_TOKEN}` with `hmac.compare_digest` (timing-safe).
- **Ops controller:** Always requires `Authorization: Bearer ${OPS_CONTROLLER_TOKEN}`; no host port published. Failed attempts log `AUTH_FAIL` with path/method/source IP.
- **Open WebUI:** `WEBUI_AUTH=False` is the default and is intended for **local/single-user** use. Set `WEBUI_AUTH=True` if more than one person reaches the host.
- **n8n:** No built-in auth by default. Tailscale alone is the gate. If n8n is reachable beyond your tailnet, enable Basic Auth or n8n's user management; do not expose it to the public internet without TLS and auth.

### Network Binding

Services bind to `0.0.0.0` so anything on the host's network can reach them — the design assumes **Tailscale is the network gate**. Caddy specifically binds to `${CADDY_BIND}` (your tailnet IP) so even on shared LANs nothing else can hit the SSO front door.

### Secrets

- **Encrypted at rest.** Tokens live in `secrets/*.sops` (SOPS + age) — safe to commit. The matching age private key (`~/.config/sops/age/keys.txt`) is the **only** sensitive artifact and should be backed up to a password manager.
- **High-value tokens are Docker secrets.** Discord, GitHub PAT, HF, Tavily, Civitai are mounted into containers as files at `/run/secrets/<name>`, never as env vars — so they don't appear in `docker inspect`. The `_FILE` env-var bridge in each consumer's entrypoint reads the file into the env var the SDK expects.
- **Plaintext `.env`.** Holds non-sensitive config (paths, model filenames, port numbers, allowlist user IDs). It's gitignored but should still not be shared.
- **Never commit** `.env`, `mcp/.env`, `data/`, `auth/caddy/certs/`, `~/.ai-toolkit/runtime/`. All gitignored.
- Run `scripts/secrets/audit-git-history.sh` before any push if you suspect a token slipped in — it greps the full git log for known token-format prefixes.

### Data

All runtime data is stored under `BASE_PATH/data/` via bind mounts (plus the named volume `ordo-ai-stack_hermes-data` for Hermes state). Ensure appropriate filesystem permissions and backups. The `data/` directory is gitignored.

## Pre-deployment checklist

- [ ] `~/.config/sops/age/keys.txt` exists, `chmod 600`, and the private key is backed up
- [ ] `secrets/.env.sops` decrypts cleanly (`make decrypt-secrets`)
- [ ] `OAUTH2_PROXY_CLIENT_ID`/`SECRET`/`COOKIE_SECRET` set in `secrets/.env.sops` (cookie secret is exactly 16/24/32 raw bytes)
- [ ] `auth/oauth2-proxy/emails.txt` reflects your real allowlist (skip-worktree set so it isn't accidentally committed)
- [ ] `auth/caddy/certs/tailnet.{crt,key}` issued and current (Tailscale certs renew ~every 90 days)
- [ ] `CADDY_BIND` set to your tailnet IP (compose `:?` failsafe will refuse empty)
- [ ] Ops controller port (`9000`) not exposed to the host
- [ ] `WEBUI_AUTH=True` if more than one person can reach Open WebUI

## Threat mitigations

| Threat | Check |
|--------|-------|
| Public ingress to admin UIs | Caddy binds to `${CADDY_BIND}` (tailnet IP), Tailscale gates the network |
| docker.sock exposure | Mounted into ops-controller, mcp-gateway, and hermes-gateway. Consider using `OpsClient()` for audited paths from automation. |
| Controller compromise | Token in `secrets/.env.sops`; failed-auth logging; no default; never publish the port |
| MCP SSRF (browser worker) | Egress blocks for 100.64/10, RFC1918, 169.254.169.254 — `./scripts/ssrf-egress-block.sh --target all` |
| Secret exfiltration via `docker inspect` | High-value tokens are Docker secrets (file-form), not env vars |
| Secret exfiltration via repo history | `secrets/*.sops` is encrypted with the public age recipient; private key never in repo |
| Unauthenticated admin via SSO outage | Dashboard's bearer fallback (`DASHBOARD_AUTH_TOKEN`) still works for `localhost`/internal-network callers |
| Browser session hijack | Cookie secret rotation invalidates all sessions: `make rotate-internal-tokens` |
| Brute-force token guess | Failed-auth lines log path/method/source IP for both dashboard and ops-controller |
| Timing side-channel on tokens | `hmac.compare_digest` everywhere |
| Shell injection via env | `LLAMACPP_EXTRA_ARGS` validated against a strict character allowlist on `POST /env/set` |

## Audit

- [ ] Audit log path writable: `data/ops-controller/`
- [ ] Rotation working: `audit.1.jsonl` appears alongside `audit.jsonl` once `audit.jsonl` exceeds `AUDIT_LOG_MAX_BYTES` (default 50 MB)
- [ ] `tail -f data/ops-controller/audit.jsonl | jq` produces one line per privileged ops-controller call

## Break-glass

1. **Reset internal tokens:** `make rotate-internal-tokens` (regenerates `LITELLM_MASTER_KEY`, `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN`, `OAUTH2_PROXY_COOKIE_SECRET`), then restart all consumers and re-sign-in.
2. **Restore data:** Restore `data/`, `models/`, and the `ordo-ai-stack_hermes-data` volume from your last backup. See [docs/data.md](docs/data.md#backup-and-recovery).
3. **age key compromised:** Treat as catastrophic — generate a new keypair, re-encrypt every `secrets/*.sops`, force-push, **and rotate every actual token at its provider** (the old encrypted blobs remain decryptable to whoever has the leaked key). See [docs/runbooks/secrets.md](docs/runbooks/secrets.md#recovery--age-key-leaked).
4. **Disable MCP tools:** Clear `data/mcp/servers.txt` or set to a single safe server.
5. **Safe mode (no agent, no external tools):** `docker compose stop mcp-gateway hermes-gateway`; chat through Open WebUI talking to llama.cpp via the model gateway only.
