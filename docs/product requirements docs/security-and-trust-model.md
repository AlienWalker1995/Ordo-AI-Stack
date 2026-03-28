# Security & Trust Model

## Threat Model

| Asset | Threat | Current State | Mitigation |
|-------|--------|---------------|------------|
| `docker.sock` (ops-controller) | Container escape → host RCE | Mounted; allowlisted actions only | Token auth; no host port; allowlist in code; run ops-controller read-only workspace mount |
| `docker.sock` (mcp-gateway) | MCP server escapes → host pivot | Mounted; Docker MCP Gateway owns it | Accept: required for spawning server containers; isolate mcp-gateway to backend network |
| Ops controller token | Token theft → privileged ops | Token in `.env`; no default | Generate with `openssl rand -hex 32`; never expose controller port to host |
| MCP tools (filesystem) | Data exfiltration via tool | Enabled in servers.txt; broken without root-dir | Remove from default servers.txt; require explicit opt-in |
| MCP tools (browser/playwright) | SSRF → RFC1918/metadata | No egress blocks yet | Add `DOCKER-USER` iptables egress block; document in runbooks |
| Tool output → model | Prompt injection via tool output | No sandbox; tool output passed to model | Allowlists; structured tool calls (`<tool_result>` tags); validate tool schemas |
| Dashboard auth | Unauthenticated admin | Optional `DASHBOARD_AUTH_TOKEN` | Document: set for networked use; pre-deployment checklist item |
| `openclaw.json` plaintext keys | Key exposure if file shared/backed up | In gitignored `data/`; acceptable on local disk | Flag in docs: avoid `data/openclaw/` in cloud backups without encryption |
| WEBUI_AUTH=False | Open WebUI accessible without auth | Explicit in compose env | Change default to `WEBUI_AUTH=${WEBUI_AUTH:-True}`; opt-out, not opt-in |
| Model gateway | No auth on `/v1/` endpoints | None; local-first intentional | Acceptable for localhost; add API key support if exposed to LAN |

## AuthN / AuthZ Tiers

- **Tier 0:** No auth (health endpoints, read-only model list)
- **Tier 1:** Bearer token (ops controller — `OPS_CONTROLLER_TOKEN`; optional dashboard — `DASHBOARD_AUTH_TOKEN`)
- **Future Tier 3:** OAuth / OIDC (if multi-user or Tailscale integration needed)
- **RBAC:** Currently binary (authed = full access). Future: read-only role (view logs, health) vs admin role (start/stop).

## Correlation ID Flow

1. External client sends `X-Request-ID: req-abc` to model gateway
2. Model gateway logs it; includes in throughput record to dashboard
3. Dashboard passes `X-Request-ID` when calling ops controller
4. Ops controller includes in audit entry
5. Result: one request traceable across model → throughput → ops → audit

## Secret Handling

### End-to-End

- `.env` — gitignored, host-only, not committed
- `mcp/.env` — gitignored, host-only; mount as Docker secret via compose `secrets:` block
- `data/openclaw/openclaw.json` — gitignored; may list skill keys; gateway and Discord/Telegram tokens should be supplied via `.env` (`merge_gateway_config.py` injects gateway token and channel SecretRefs when env vars are set).
- Gateway tokens — in `.env`, set via compose `environment:`
- **Secret rotation:** Update `.env`, `docker compose up -d --force-recreate <service>`. Document in `BACKUP_RESTORE.md`.

### OpenClaw-Specific Secrets

| Secret | Location | Injected by | Notes |
|--------|----------|-------------|-------|
| `OPENCLAW_GATEWAY_TOKEN` | `.env` | Compose `environment:` | Orchestrator + CLI (bridge auth only) |
| `CLAUDE_AI_SESSION_KEY` | `.env` | Compose `environment:` (gateway only) | Never in CLI container |
| `CLAUDE_WEB_SESSION_KEY` | `.env` | Compose `environment:` (gateway only) | Never in CLI container |
| `CLAUDE_WEB_COOKIE` | `.env` | Compose `environment:` (gateway only) | Never in CLI container |
| Telegram bot token | `data/openclaw/openclaw.json` | OpenClaw config sync | Gitignored |
| Skill API keys | `data/openclaw/openclaw.json` | OpenClaw config sync | Gitignored |

**Rotation:** See `docs/runbooks/SECURITY_HARDENING.md`.

## SSRF Defenses (MCP)

```bash
# Block MCP containers from reaching RFC1918 + metadata endpoints
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 10.0.0.0/8 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 172.16.0.0/12 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 192.168.0.0/16 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 100.64.0.0/10 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 169.254.169.254/32 -j DROP
```

SSRF scripts live at `scripts/ssrf-egress-block.sh` (Linux/WSL2) and `scripts/ssrf-egress-block.ps1` (Windows guidance).

### Browser-Tier Egress Control

When browser/playwright is active, worker containers can make arbitrary outbound HTTP requests. Apply RFC1918 + metadata blocks:

```bash
# Block the openclaw network specifically:
./scripts/ssrf-egress-block.sh --target openclaw

# Block both MCP and openclaw in one pass:
./scripts/ssrf-egress-block.sh --target all
```

Blocked ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (RFC1918), `100.64.0.0/10` (Tailscale CGNAT), `169.254.169.254/32`, `169.254.170.2/32` (cloud metadata). DNS (port 53) is explicitly allowed.

## Prompt Injection Defense at Tool-Output Boundary

- Tool results returned in structured `<tool_result>` boundary by the MCP bridge plugin
- The orchestrator treats tool output as **data**, not **instructions**
- Validate tool output schemas where possible (MCP `registry.json` `outputSchema` field)
- Structured boundary ensures the model can distinguish injected text from genuine prompts

## Container Hardening (Both Tiers)

Both OpenClaw containers run with:
```yaml
cap_drop: [ALL]
security_opt: ["no-new-privileges:true"]
```

`openclaw-gateway` additionally has:
```yaml
deploy:
  resources:
    limits:
      memory: 2G
healthcheck:
  test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:6680"]
  start_period: 60s
  interval: 30s
  timeout: 10s
  retries: 3
```

`openclaw-cli` has `restart: "no"` because it is an interactive/on-demand process.

## Security + Reliability Intersection

Items that are both security and reliability problems:
- Open WebUI auth default
- MCP per-client enforcement gaps
- Plaintext tokens in `openclaw.json`

**Improvements:** auth on by default for remotely reachable UIs; env-based secret resolution where possible; explicit per-client policy at MCP gateway; tool registration workflow; immutable audit trail for config changes.
