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

- **Open WebUI:** The default `WEBUI_AUTH=False` disables login. This is intended for **local/single-user** use only. If you expose the stack to a network (e.g., via port forwarding or LAN access), **enable authentication** by setting `WEBUI_AUTH=True` in the environment.
- **OpenClaw:** Requires `OPENCLAW_GATEWAY_TOKEN`. Generate with `openssl rand -hex 32` and keep it secret.
- **n8n:** No built-in auth by default. Consider n8n's security options if exposed.

### Network Binding

Services bind to `0.0.0.0` to allow access from other machines on your network. Use a firewall to restrict access if needed.

### Secrets

- **Never commit** `.env` or `openclaw/.env`. They are gitignored.
- Use `.env.example` as a template; copy to `.env` and fill in values locally.
- API keys (OpenAI, Anthropic, etc.) and tokens should only live in `.env` files, never in the repository.

### Data

All runtime data is stored under `BASE_PATH/data/` via bind mounts. Ensure appropriate filesystem permissions and backups.
