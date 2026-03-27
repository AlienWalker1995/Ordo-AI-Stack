# Security Hardening Runbook

Operational guidance for hardening the Ordo AI Stack beyond the defaults.

**See also:** [SECURITY.md](../../SECURITY.md) · [Product Requirements Document](../Product%20Requirements%20Document.md)

---

## 1. SSRF Egress Blocks (MCP Gateway)

The MCP gateway spawns tool containers that make outbound HTTP calls. Without egress controls, a
malicious or misconfigured tool can reach internal services (Ollama, ops-controller, cloud metadata).

### Linux host (iptables / DOCKER-USER chain)

**Script (recommended):** From repo root on a Linux host or WSL2:

```bash
./scripts/ssrf-egress-block.sh --dry-run   # show commands
./scripts/ssrf-egress-block.sh             # apply (sudo)
./scripts/ssrf-egress-block.sh --remove    # remove rules
```

The script auto-detects the Docker network subnet (`ordo-ai-stack-frontend` or `ordo-ai-stack_default`), or you can pass it: `./scripts/ssrf-egress-block.sh 172.18.0.0/16`.

**Manual commands** (if you prefer):

```bash
# Find the subnet used by MCP containers.
docker network inspect ordo-ai-stack-frontend | jq '.[0].IPAM.Config[].Subnet'
# Example output: "172.18.0.0/16"
MCP_SUBNET="172.18.0.0/16"   # replace with your subnet

# Block RFC1918 (private) ranges
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -d 10.0.0.0/8     -j DROP
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -d 172.16.0.0/12  -j DROP
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -d 192.168.0.0/16 -j DROP

# Block Tailscale range
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -d 100.64.0.0/10  -j DROP

# Block cloud metadata endpoints
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -d 169.254.169.254/32 -j DROP
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -d 169.254.170.2/32   -j DROP  # ECS metadata

# Allow DNS (required for outbound tool calls)
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -p udp --dport 53 -j ACCEPT
sudo iptables -I DOCKER-USER -s "$MCP_SUBNET" -p tcp --dport 53 -j ACCEPT

# Verify
sudo iptables -L DOCKER-USER -n --line-numbers
```

**Make persistent:** On Debian/Ubuntu: `apt install iptables-persistent && netfilter-persistent save`.
On RHEL/Fedora: use `firewalld` rich rules or `iptables-services`.

### Windows host (WSL2 / Docker Desktop)

Docker Desktop on Windows uses a virtual network; DOCKER-USER is not directly accessible from PowerShell.
Run from repo root: `.\scripts\ssrf-egress-block.ps1` for guidance.
Alternatives:
- **WSL2:** Run the bash script from inside WSL (see script comments).
- Use Docker Desktop network policies (enterprise feature) or a network plugin.
- Run a proxy container between MCP and the internet with egress filtering.
- Accept reduced protection for local-only use (default posture).

---

## 2. Backend network (internal: true)

The `backend` network is set to `internal: true` in `docker-compose.yml`, so containers that are **only** on the backend (ollama, ops-controller) have no outbound internet access. Services that are on both frontend and backend (model-gateway, dashboard, openclaw-gateway) still have outbound access via the frontend network.

If you see DNS resolution failures or healthcheck timeouts for backend-only services:
- Temporarily set `internal: false` for the `backend` network in `docker-compose.yml`, or
- Move the affected service to the frontend network as well (only if acceptable for your threat model).

---

## 3. Ops Controller Port Isolation

The ops controller runs without a host port by default (internal Docker network only).
**Never expose port 9000 to the network** unless behind a VPN/firewall.

To verify no host port is exposed:
```bash
docker inspect ordo-ai-stack-ops-controller-1 --format '{{json .HostConfig.PortBindings}}'
# Expected: {} (empty)
```

If you need remote access to the controller for debugging, use SSH tunneling:
```bash
ssh -L 9000:localhost:9000 user@host
# Then access locally: curl http://localhost:9000/health
```

---

## 4. Dashboard Auth

By default, `DASHBOARD_AUTH_TOKEN` is empty and dashboard API auth is disabled.
For any multi-user or networked deployment, set a Bearer token in `.env`:

```bash
DASHBOARD_AUTH_TOKEN=$(openssl rand -hex 32)
```

Then restart the dashboard: `docker compose up -d dashboard`.

---

## 5. Token Rotation

All tokens live in `.env` (gitignored). To rotate:

```bash
# Generate new token
NEW_TOKEN=$(openssl rand -hex 32)

# Update .env
# Replace OPS_CONTROLLER_TOKEN or OPENCLAW_GATEWAY_TOKEN with the new value

# Restart affected services
docker compose up -d dashboard ops-controller   # for OPS_CONTROLLER_TOKEN
docker compose restart openclaw-gateway          # for OPENCLAW_GATEWAY_TOKEN
# Re-pair OpenClaw clients after gateway restart (see TROUBLESHOOTING.md)
```

---

## 6. Open WebUI Auth

Open WebUI ships with `WEBUI_AUTH=True` by default in this stack.
If you're on a single-user local machine and want to skip the login:

```bash
# .env
WEBUI_AUTH=False
```

For shared/LAN use, keep `WEBUI_AUTH=True` and create a strong admin account on first launch.

---

## 7. Non-root and Optional Hardening

- **n8n:** Runs as `user: "1000:1000"`. If you see permission errors on the n8n data volume, ensure the host directory is writable by UID 1000 or remove the `user` directive from the n8n service in `docker-compose.yml`.
- **ComfyUI:** Runs as root by default (image and mounts use `/root`). Running as non-root would require image support and volume ownership changes; not applied in this stack.

## 8. Container Hardening Verification

After `docker compose up -d`, verify hardening is applied to custom services:

```bash
# Check cap_drop on model-gateway
docker inspect $(docker compose ps -q model-gateway) \
  --format '{{.HostConfig.CapDrop}}' 
# Expected: [ALL]

# Check read-only rootfs on dashboard
docker inspect $(docker compose ps -q dashboard) \
  --format '{{.HostConfig.ReadonlyRootfs}}'
# Expected: true

# Check no-new-privileges
docker inspect $(docker compose ps -q model-gateway) \
  --format '{{.HostConfig.SecurityOpt}}'
# Expected: [no-new-privileges:true]
```

---

## 9. Supply Chain

Optional: scan images before deploy.

```bash
# Install Trivy (https://aquasecurity.github.io/trivy/)
# Scan Ollama image
trivy image ollama/ollama:0.17.4

# Scan custom builds
docker compose build model-gateway
trivy image ordo-ai-stack-model-gateway:latest
```

For pinned digests, see the comment in `docker-compose.yml` under `ollama`:
```
# Pin digest: OLLAMA_IMAGE=ollama/ollama:0.17.4@sha256:...
```
Run `docker inspect ollama/ollama:0.17.4 --format '{{index .RepoDigests 0}}'` to get the digest.

---

## 10. Filesystem MCP Server

The `filesystem` MCP server is disabled by default (`allow_clients: []` in `data/mcp/registry.json`).
To enable it safely:

1. Decide which directory to expose (e.g. a specific project folder, NOT `/` or home).
2. Add to `mcp/.env`:
   ```
   FILESYSTEM_ROOT=/path/to/allowed/directory
   ```
3. Wire the secret in `docker-compose.yml` under `mcp-gateway` (uncomment the `secrets:` block).
4. Edit `data/mcp/servers.txt` and add `filesystem`.
5. Restart: `docker compose up -d mcp-gateway`.
6. **Test:** Verify the server can only access the intended directory.

---

## 11. OpenClaw secrets (data/openclaw/openclaw.json)

OpenClaw stores config in `data/openclaw/openclaw.json`, which may contain sensitive values in plaintext:

- **Gateway auth:** `gateway.auth.token` — set `OPENCLAW_GATEWAY_TOKEN` in `.env`; `openclaw-config-sync` injects it into `openclaw.json` at startup so the token lives only in `.env` and is not committed. Rotate in `.env` and restart the stack (or openclaw-gateway).
- **Channels (Discord / Telegram):** When `DISCORD_TOKEN` (mapped to `DISCORD_BOT_TOKEN` in the gateway container) or `TELEGRAM_BOT_TOKEN` is set in `.env`, `openclaw-config-sync` runs `merge_gateway_config.py`, which rewrites `channels.discord.token` / `channels.telegram.botToken` to OpenClaw **SecretRef** form (`{"source":"env","provider":"default","id":"…"}`) so the bot token is not stored as plaintext in `openclaw.json`. Ensure the same variables are available to `openclaw-gateway` (see `docker-compose.yml`). **OpenClaw 2026.3.x requires `provider: "default"`** on that object; without it the gateway exits with `channels.discord.token: Invalid input` and Discord never connects. Fix: upgrade repo `merge_gateway_config.py`, run `docker compose run --rm openclaw-config-sync` (or `up` so sync runs), or hand-edit `openclaw.json` to add `"provider":"default"` next to `source`/`id`, then `docker compose restart openclaw-gateway`.
- **Skills:** e.g. `skills.entries.<name>.apiKey` — still typically stored in JSON unless the upstream skill supports env / SecretRef; rotate in place if exposed.

**Recommendations:**

- **Do not** include `data/openclaw/` in unencrypted cloud backups. If you back up `data/`, exclude `openclaw.json` or encrypt the backup.
- **Rotate** the gateway token via `.env` and then update `openclaw.json` (or re-run OpenClaw setup) so `gateway.auth.token` matches; restart the gateway.
- **Rotate** Discord/Telegram keys in the provider’s dashboard, update `.env`, and restart `openclaw-gateway`. For channels covered by SecretRef, you do not need to edit `openclaw.json` for the token value.
