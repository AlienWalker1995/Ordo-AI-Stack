# One-command compose: auto-detects hardware, then runs docker compose.
# Usage: .\compose.ps1 <command> [args]
#
# Examples:
#   .\compose.ps1 up -d                                             # start all services
#   .\compose.ps1 up -d ollama dashboard open-webui                # start core only
#   .\compose.ps1 down                                             # stop all
#   .\compose.ps1 logs -f ollama                                   # tail logs
#   .\compose.ps1 run --rm model-puller                            # pull Ollama models
#
# Compose overrides (in overrides/):
#   .\compose.ps1 -f docker-compose.yml -f overrides/ollama-expose.yml up -d
#   .\compose.ps1 -f docker-compose.yml -f overrides/openclaw-secure.yml up -d
#   .\compose.ps1 -f docker-compose.yml -f overrides/vllm.yml --profile vllm up -d

param([Parameter(ValueFromRemainingArguments)][string[]]$PassThrough)

if ($PassThrough.Count -eq 0 -or $PassThrough[0] -in '--help', '-h') {
    Get-Content $MyInvocation.MyCommand.Path |
        Where-Object { $_ -match '^#' } |
        ForEach-Object { $_ -replace '^# ?', '' } |
        Select-Object -Skip 1
    exit 0
}

$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Get-Location).Path }
$env:BASE_PATH = $base
$detect = Join-Path $base "scripts\detect_hardware.py"
if (Test-Path $detect) {
    python $detect 2>$null | Out-Null
}
docker compose @PassThrough
