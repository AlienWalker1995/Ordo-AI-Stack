# Add an MCP server to the gateway. Run from repo root.
# Usage: .\scripts\mcp_add.ps1 <server-name>
# Example: .\scripts\mcp_add.ps1 fetch
# Config is stored in out/mcp/servers.txt (mounted into ordo-mcp-gateway-1 at /mcp-config);
# gateway reloads in ~10s (no container restart). out/ is rendered by `ordo render` -- this
# script edits the rendered file directly and does not survive a future re-render.
param([Parameter(Mandatory=$true)][string]$Server)

$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Get-Location).Path }
$out = if ($env:OUT_PATH) { $env:OUT_PATH -replace '\\', '/' } else { Join-Path $base "out" }
$configFile = Join-Path $out "mcp\servers.txt"

$configDir = Split-Path $configFile
if (-not (Test-Path $configDir)) { New-Item -ItemType Directory -Force -Path $configDir | Out-Null }
if (-not (Test-Path $configFile)) { Set-Content -Path $configFile -Value "duckduckgo" -NoNewline }

$current = Get-Content $configFile -Raw -ErrorAction SilentlyContinue
$servers = $current -split '[,\r\n]' | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique

if ($servers -contains $Server) {
    Write-Host "Server '$Server' is already enabled."
    exit 0
}

$servers = @($servers) + $Server
$newValue = $servers -join ','
Set-Content -Path $configFile -Value $newValue -NoNewline

Write-Host "Added $Server. Gateway will reload in ~10s (no container restart)."
Write-Host "$Server is available at http://localhost:8811/mcp"
