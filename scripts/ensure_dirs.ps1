# Creates data directories for bind mounts. Run before first docker compose up.
$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Get-Location).Path }
$data = if ($env:DATA_PATH) { $env:DATA_PATH -replace '\\', '/' } else { Join-Path $base "data" }
$dirs = @(
    (Join-Path $data "ollama"),
    (Join-Path $data "mcp"),
    (Join-Path $data "open-webui"),
    (Join-Path $data "comfyui-storage"),
    (Join-Path $data "comfyui-output"),
    (Join-Path $data "n8n-data"),
    (Join-Path $data "n8n-files"),
    (Join-Path $data "openclaw"),
    (Join-Path $data "openclaw\workspace"),
    (Join-Path $base "models\comfyui\checkpoints"),
    (Join-Path $base "models\comfyui\loras"),
    (Join-Path $base "models\comfyui\latent_upscale_models"),
    (Join-Path $base "models\comfyui\text_encoders")
)
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    Write-Host "OK $d"
}

# Ensure openclaw/.env exists with a valid token (required for OpenClaw service)
$openclawEnv = Join-Path $base "openclaw\.env"
$openclawExample = Join-Path $base "openclaw\.env.example"
$needsCreate = -not (Test-Path $openclawEnv)
$needsToken = $false
if ((Test-Path $openclawEnv)) {
    $existing = Get-Content $openclawEnv -Raw -ErrorAction SilentlyContinue
    $needsToken = $existing -and $existing -match 'change-me-to-a-long-random-token'
}
if ($needsCreate -or $needsToken) {
    $token = -join ((1..32 | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) }))
    $content = if (Test-Path $openclawExample) { Get-Content $openclawExample -Raw } else { $null }
    if ($content) {
        $content = $content -replace 'OPENCLAW_GATEWAY_TOKEN=change-me-to-a-long-random-token', "OPENCLAW_GATEWAY_TOKEN=$token"
        $content = $content -replace 'BASE_PATH=[^\r\n]*', "BASE_PATH=$($base -replace '\\', '/')"
        Set-Content -Path $openclawEnv -Value $content -NoNewline
    } else {
        Set-Content -Path $openclawEnv -Value @"
BASE_PATH=$($base -replace '\\', '/')
OPENCLAW_GATEWAY_TOKEN=$token
"@
    }
    Write-Host "OK openclaw/.env ($(if ($needsCreate) { 'created' } else { 'token fixed' }))"
}

# Auto-detect GPU and generate docker-compose.compute.yml
$detectScript = Join-Path $base "scripts\detect_hardware.py"
if (Test-Path $detectScript) {
    $env:BASE_PATH = $base -replace '\\', '/'
    python $detectScript 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "OK Hardware detected (docker-compose.compute.yml)" }
}

Write-Host "Directories ready."
