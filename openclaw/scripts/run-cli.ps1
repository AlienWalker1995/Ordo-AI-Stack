# Run OpenClaw CLI via Docker with gateway URL and token so it reaches openclaw-gateway.
# Usage: from repo root, .\openclaw\scripts\run-cli.ps1 devices list
#        .\openclaw\scripts\run-cli.ps1 devices approve DEVICE_ID
# With gateway.mode=local the CLI uses local discovery (wrong in Docker); --url forces the target.
param([Parameter(ValueFromRemainingArguments = $true)] $CliArgs)
$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$envPath = Join-Path $base ".env"
if (-not (Test-Path $envPath)) {
    Write-Error ".env not found at $envPath. Copy .env.example to .env and set OPENCLAW_GATEWAY_TOKEN."
}
$line = Get-Content $envPath -Raw | Select-String -Pattern 'OPENCLAW_GATEWAY_TOKEN=(.+)' -AllMatches
if (-not $line -or -not $line.Matches.Groups[1].Value) {
    Write-Error "OPENCLAW_GATEWAY_TOKEN not found in .env"
}
$token = $line.Matches.Groups[1].Value.Trim()
$portLine = Get-Content $envPath -Raw | Select-String -Pattern 'OPENCLAW_GATEWAY_INTERNAL_PORT=(\d+)' -AllMatches
$gwPort = if ($portLine -and $portLine.Matches.Groups[1].Value) { $portLine.Matches.Groups[1].Value.Trim() } else { "6680" }
# Must match gateway listen port inside the container (default compose: 6680; set OPENCLAW_GATEWAY_INTERNAL_PORT in .env if you use openclaw-secure.yml).
$gatewayUrl = "ws://openclaw-gateway:$gwPort"
$dockerArgs = @("compose", "--profile", "openclaw-cli", "run", "--rm", "openclaw-cli") + @($CliArgs) + @("--url", $gatewayUrl, "--token", $token)
Push-Location $base
try {
    & docker @dockerArgs
} finally {
    Pop-Location
}
