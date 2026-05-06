---
name: stack-audit
description: Audit and manage package updates across the Ordo-AI-Stack Docker services with severity classification, approval workflow, automated updates, and git PR creation.
---

# Stack Audit & Update Skill

Automated monitoring and update management for all Docker services in the Ordo-AI-Stack.

## What It Does

1. **Monitors** all services in `docker-compose.yml` against their latest GitHub/Docker releases
2. **Classifies** severity: CRITICAL (CVE/security), HIGH (major version jump), MEDIUM (minor), LOW (patch), SAFE (up-to-date)
3. **Reports** to user with highlights and release notes
4. **Waits for approval** — no changes applied without explicit written consent
5. **Applies updates** to `docker-compose.yml` + `github_monitor.py` on approval
6. **Restarts** affected services (atomic rebuild)
7. **Creates a git branch**, commits, pushes, and opens a PR

## Triggering

Run the monitor:
```bash
python3 /c/dev/ordo-ai-stack/scripts/stack_monitor.py --json
```

The cron job (5cb290c34008) runs this daily at 12:00 UTC and delivers to Discord.

## Severity Classification

| Level | Emoji | Criteria |
|-------|-------|----------|
| CRITICAL | 🔴 | CVE mentioned, actual vulnerability/exploit references |
| HIGH | 🟠 | Major version jump (e.g., 2.x → 3.x) |
| MEDIUM | 🟡 | Minor update (e.g., 1.5.x → 1.6.x) |
| LOW | 🟢 | Patch update (e.g., 1.5.1 → 1.5.2) |
| SAFE | ✅ | Already up to date |

## Update Workflow

### Step 1: Audit
```bash
cd /c/dev/ordo-ai-stack
python3 scripts/stack_monitor.py --json > /tmp/stack_audit.json
```

### Step 2: Review Output
The JSON contains:
- `services`: Per-service severity, pinned vs latest, highlights
- `all_updates`: Dict of services needing updates
- `has_updates`: boolean

### Step 3: Wait for User Approval
Send the report to the user. DO NOT apply updates without explicit written approval.

### Step 4: Approve and Apply
User replies with something like: "approve all updates" or "approve n8n, Caddy".

Create an approval file:
```bash
echo '{"n8n": "2.21.0", "Caddy": "2.11.3"}' > /tmp/stack_approve.json
```

### Step 5: Apply Updates
```bash
cd /c/dev/ordo-ai-stack
python3 scripts/stack_monitor.py --apply --approve-file /tmp/stack_approve.json --json
```

This will:
- Update `docker-compose.yml` pinned versions
- Restart affected services (atomic rebuild)
- Create a git branch, commit, push, and open a PR

## Atomic Rebuild (Safe Restart)

When restarting services, always use the atomic form to prevent the "orchestrator dies mid-rebuild" scenario:

```bash
docker compose up -d --build --force-recreate --no-deps <service>
```

This rebuilds and recreates in one step. If the orchestrator dies mid-flight, the existing container keeps running.

## Safety Net: Hermes Self-Heal Watchdog

The ops-controller has an **opt-in self-heal watchdog** that automatically restarts exited `hermes-gateway` and `hermes-dashboard` containers after a configurable grace window. This prevents the "operator stopped for rebuild and died" scenario.

### Configuration (env vars for ops-controller service)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPS_HERMES_WATCHDOG_ENABLED` | `0` | Enable the watchdog (`1` to activate) |
| `OPS_HERMES_WATCHDOG_INTERVAL_SECONDS` | `30` | How often to poll |
| `OPS_HERMES_WATCHDOG_GRACE_SECONDS` | `60` | Minimum age before acting |
| `OPS_HERMES_WATCHDOG_PAUSE_FILE` | `/data/watchdog.paused` | Touch to pause, remove to resume |

### Enabling

```bash
# In .env:
OPS_HERMES_WATCHDOG_ENABLED=1

# Or inline:
OPS_HERMES_WATCHDOG_ENABLED=1 docker compose up -d ops-controller
```

### Pausing

```bash
touch /c/dev/ordo-ai-stack/data/ops-controller/watchdog.paused
```

### Verifying

1. Start with watchdog enabled: `OPS_HERMES_WATCHDOG_ENABLED=1 docker compose up -d ops-controller`
2. Stop Hermes: `docker stop ordo-ai-stack-hermes-gateway-1`
3. Within `interval + grace + ~10s` it should be back
4. Check audit log for `watchdog.acted` entry

## Services Monitored

| Service | Repo | Type |
|---------|------|------|
| n8n | n8n-io/n8n | GitHub + Docker Hub |
| Open WebUI | open-webui/open-webui | GitHub + Docker Hub |
| Qdrant | qdrant/qdrant | Docker Hub |
| Caddy | caddyserver/caddy | Docker Hub |
| llama.cpp | ggml-org/llama.cpp | GitHub + Docker |
| LiteLLM | BerriAI/litellm | Docker-only |
| ComfyUI | Comfy-Org/ComfyUI | GitHub |
| oauth2-proxy | oauth2-proxy/oauth2-proxy | GitHub + Docker Hub |

## Custom Nodes (Atom Feed)

ComfyUI-Manager, ComfyUI-KJNodes, ComfyUI-VideoHelperSuite use Atom feed parsing (GitHub API rate limits apply).

## Notes

- **n8n** uses `n8n@X.Y.Z` tag format — auto-stripped for comparison
- **ComfyUI** is managed via `yanwk/comfyui-boot:cpu` wrapper — not directly versioned in docker-compose
- **LiteLLM** uses `:latest` tag — always flagged as needing update check
- **llama.cpp** pinned to `server-cuda` — commit-style tags (e.g., `b9037`) classified as MEDIUM
- Always review HIGH and CRITICAL updates before applying

## Emergency Rollback

If an update causes issues:
```bash
cd /c/dev/ordo-ai-stack
git checkout HEAD~1 -- docker-compose.yml scripts/stack_monitor.py
docker compose up -d --build --force-recreate --no-deps <service>
```
