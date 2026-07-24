# Smoke test: bring up services and verify health.
# Usage: .\scripts\smoke_test.ps1 [-NoUp]  (default: runs docker compose up -d first)
#
# Targets the rendered v2 compose (out/docker-compose.yml, project "ordo"). Only Caddy
# publishes a host port (:443) -- every other service is ordo-net-internal, so health is
# probed with `docker compose exec` against the same in-container commands each service's
# own healthcheck already uses, not host-port requests.
param([switch]$NoUp)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$ComposeArgs = @("--project-directory", "out", "-f", "out/docker-compose.yml", "-p", "ordo")

Write-Host "==> Smoke test (repo: $RepoRoot, compose: out/docker-compose.yml, project: ordo)"

if (-not $NoUp) {
    Write-Host "==> Starting services..."
    docker compose @ComposeArgs up -d
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
Check-Exec "mcp-gateway" "mcp-gateway" @("sh", "/mcp-scripts/healthcheck.sh")

Write-Host "==> Service status"
docker compose @ComposeArgs ps

if ($Fail -eq 1) {
    Write-Host "==> Smoke test FAILED"
    exit 1
}

Write-Host "==> Smoke test PASSED"
exit 0
