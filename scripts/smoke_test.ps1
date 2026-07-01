# Smoke test: bring up services and verify health.
# Usage: .\scripts\smoke_test.ps1 [-NoUp]  (default: runs docker compose up -d first)
param([switch]$NoUp)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "==> Smoke test (repo: $RepoRoot)"

if (-not $NoUp) {
    Write-Host "==> Starting services..."
    docker compose up -d
    Write-Host "==> Waiting 60s for healthchecks..."
    Start-Sleep -Seconds 60
}

$Fail = 0

function Check-Health {
    param([string]$Name, [string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($r.StatusCode -lt 500) {
            Write-Host "  OK $Name"
        } else {
            Write-Host "  FAIL $Name ($Url) - HTTP $($r.StatusCode)"
            $script:Fail = 1
        }
    } catch {
        Write-Host "  FAIL $Name ($Url) - $($_.Exception.Message)"
        $script:Fail = 1
    }
}

Write-Host "==> Checking health endpoints..."
Check-Health "dashboard" "http://localhost:8080/api/health"
Check-Health "model-gateway" "http://localhost:11435/health"
Check-Health "mcp-gateway" "http://localhost:8811/mcp"

Write-Host "==> Service status"
docker compose ps

if ($Fail -eq 1) {
    Write-Host "==> Smoke test FAILED"
    exit 1
}

Write-Host "==> Smoke test PASSED"
exit 0
