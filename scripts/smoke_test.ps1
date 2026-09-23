# Smoke test: verify the running stack's health. Changes nothing unless asked.
# Usage: .\scripts\smoke_test.ps1 [-Up]  (-Up first brings the stack up the canonical way)
#
# Targets the rendered compose (out/docker-compose.yml, project "ordo"). Only Caddy
# publishes a host port (:443) -- every other service is ordo-net-internal, so health is
# probed with `docker compose exec` against the same in-container commands each service's
# own healthcheck already uses, not host-port requests.
param([switch]$Up, [switch]$NoUp)  # -NoUp is the default now; kept so old invocations work

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Both env files, always: compose interpolates ${...} from them, and a call without secrets.env
# renders blank secrets, so an `up` would recreate services with empty credentials.
$ComposeArgs = @("--project-directory", "out", "-f", "out/docker-compose.yml", "-p", "ordo",
                 "--env-file", "out/.env", "--env-file", "out/secrets.env")

Write-Host "==> Smoke test (repo: $RepoRoot, compose: out/docker-compose.yml, project: ordo)"

if ($Up) {
    Write-Host "==> Starting services..."
    # The sanctioned bring-up: every profile, both env files, refused while a GPU lease holds.
    python -m ordo up --all --out out
    if ($LASTEXITCODE -ne 0) { throw "ordo up --all failed (exit $LASTEXITCODE)" }
    Write-Host "==> Waiting 60s for healthchecks..."
    Start-Sleep -Seconds 60
}

$Fail = 0

# Probes in-network via `docker compose exec`, reusing each service's own healthcheck
# command (see out/docker-compose.yml) instead of requesting unpublished host ports.
function Check-Exec {
    param([string]$Name, [string]$Service, [string[]]$Cmd)
    docker compose @ComposeArgs exec -T $Service @Cmd *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK $Name"
    } else {
        Write-Host "  FAIL $Name (exec in $Service)"
        $script:Fail = 1
    }
}

Write-Host "==> Checking health endpoints (in-network)..."
Check-Exec "dashboard" "dashboard" @("python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')")
Check-Exec "model-gateway" "model-gateway" @("python3", "-c", "import os, urllib.request; req = urllib.request.Request('http://localhost:11435/v1/models', headers={'Authorization': 'Bearer ' + os.environ.get('LITELLM_MASTER_KEY', 'local')}); urllib.request.urlopen(req)")
# MCP is served by model-gateway (LiteLLM /mcp); its server list must not be empty.
Check-Exec "mcp (model-gateway)" "model-gateway" @("python3", "-c", "import json, os, urllib.request; req = urllib.request.Request('http://localhost:11435/v1/mcp/server', headers={'Authorization': 'Bearer ' + os.environ.get('LITELLM_MASTER_KEY', 'local')}); assert json.load(urllib.request.urlopen(req)), 'no MCP servers registered'")

Write-Host "==> Service status"
docker compose @ComposeArgs ps

if ($Fail -eq 1) {
    Write-Host "==> Smoke test FAILED"
    exit 1
}

Write-Host "==> Smoke test PASSED"
exit 0
