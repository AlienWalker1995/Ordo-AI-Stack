# Remove an MCP server from the gateway. Run from repo root.
# Usage: .\scripts\mcp_remove.ps1 <server-name>
# Config is stored in out/mcp/servers.txt (mounted into ordo-mcp-gateway-1 at /mcp-config);
# gateway reloads in ~10s (no container restart). out/ is rendered by `ordo render` -- this
# script edits the rendered file directly and does not survive a future re-render.
param([Parameter(Mandatory=$true)][string]$Server)

$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Get-Location).Path }
$out = if ($env:OUT_PATH) { $env:OUT_PATH -replace '\\', '/' } else { Join-Path $base "out" }
$configFile = Join-Path $out "mcp\servers.txt"

if (-not (Test-Path $configFile)) {
    Write-Host "No MCP config found at $configFile" -ForegroundColor Red
    exit 1
}

$current = Get-Content $configFile -Raw -ErrorAction SilentlyContinue
$servers = $current -split '[,\r\n]' | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne $Server } | Select-Object -Unique

$newValue = ($servers -join ',').Trim()
if (-not $newValue) { $newValue = "n8n" }

Set-Content -Path $configFile -Value $newValue -NoNewline

Write-Host "Removed $Server. Gateway will reload in ~10s (no container restart)."
