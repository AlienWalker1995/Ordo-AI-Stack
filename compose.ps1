# One-command compose: auto-detects hardware, then runs docker compose.
# Usage: .\compose.ps1 up -d  |  .\compose.ps1 down  |  .\compose.ps1 logs -f ollama
$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Get-Location).Path }
$env:BASE_PATH = $base
$detect = Join-Path $base "scripts\detect_hardware.py"
if (Test-Path $detect) {
    python $detect 2>$null | Out-Null
}
docker compose @args
