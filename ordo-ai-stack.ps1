# ordo-ai-stack -- project management CLI
# Usage: .\ordo-ai-stack.ps1 <command>
#
# Commands:
#   initialize   Bootstrap directories, config, hardware profile, then rebuild/recreate and start the full default stack
#   help         Show this help

param([string]$Command = "help")

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    if ($env:BASE_PATH) {
        return ($env:BASE_PATH -replace '\\', '/')
    }
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    return ($here -replace '\\', '/')
}

function ok($msg)      { Write-Host "  [ok] $msg" -ForegroundColor Green }
function info($msg)    { Write-Host "  [--] $msg" -ForegroundColor Cyan }
function warn($msg)    { Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function err($msg)     { Write-Host "  [xx] $msg" -ForegroundColor Red }
function section($n, $t) { Write-Host "`n=== $n $t" -ForegroundColor White }

function Invoke-Initialize {
    $base = Get-RepoRoot
    $composeFile = Join-Path $base "docker-compose.yml"
    if (-not (Test-Path $composeFile)) {
        err "No docker-compose.yml under: $base"
        err "Set BASE_PATH to your Ordo AI Stack repo root, or run this script from the repo (.\ordo-ai-stack.ps1 initialize)."
        exit 1
    }

    Write-Host ""
    $ordoBanner = @'
  ___          _       
 / _ \ _ __ __| | ___  
| | | | '__/ _` |/ _ \ 
| |_| | | | (_| | (_) |
 \___/|_|  \__,_|\___/
'@
    Write-Host $ordoBanner -ForegroundColor Yellow
    $dataPath = if ($env:DATA_PATH) { $env:DATA_PATH -replace '\\', '/' } else { "$base/data" }
    Write-Host "base=$base  data=$dataPath" -ForegroundColor DarkGray

    Push-Location -LiteralPath $base
    try {
        $env:BASE_PATH = $base

        section "1/3" "Directories, config, hardware (ensure_dirs)"
        $ensureDirs = Join-Path $base "scripts/ensure_dirs.ps1"
        if (-not (Test-Path $ensureDirs)) {
            err "Missing scripts/ensure_dirs.ps1"
            exit 1
        }
        & $ensureDirs

        section "2/3" "OpenClaw workspace templates (if missing)"
        $ensureWs = Join-Path $base "openclaw/scripts/ensure_openclaw_workspace.ps1"
        if (Test-Path $ensureWs) {
            & $ensureWs
        }
        else {
            warn "openclaw/scripts/ensure_openclaw_workspace.ps1 not found - skipped"
        }

        section "3/3" "Docker - build, recreate, start full stack"
        info "docker compose up -d --build --force-recreate"
        docker compose up -d --build --force-recreate
    }
    catch {
        throw
    }
    finally {
        Pop-Location
    }

    Write-Host "`nReady" -ForegroundColor Green
    Write-Host ""
    $urls = @(
        @("Dashboard",     "http://localhost:8080"),
        @("Open WebUI",    "http://localhost:3000"),
        @("ComfyUI",       "http://localhost:8188"),
        @("N8N",           "http://localhost:5678"),
        @("OpenClaw",      "http://localhost:6680"),
        @("Model Gateway", "http://localhost:11435")
    )
    foreach ($u in $urls) {
        Write-Host ("  {0,-22}" -f $u[0]) -ForegroundColor White -NoNewline
        Write-Host $u[1]
    }
    Write-Host ""
    Write-Host "  Pull Ollama models:   docker compose run --rm model-puller" -ForegroundColor DarkGray
    Write-Host "  Pull ComfyUI models:  docker compose --profile comfyui-models run --rm comfyui-model-puller" -ForegroundColor DarkGray
    Write-Host ""
}

# --- dispatch ---
switch ($Command) {
    { $_ -in "initialize", "init" } { Invoke-Initialize }
    { $_ -in "help", "--help", "-h" } {
        Get-Content $MyInvocation.MyCommand.Path |
            Where-Object { $_ -match '^#' } |
            ForEach-Object { $_ -replace '^# ?', '' } |
            Select-Object -Skip 1
    }
    default {
        err "Unknown command: $Command"
        Write-Host "  Run .\ordo-ai-stack.ps1 help for usage."
        exit 1
    }
}
