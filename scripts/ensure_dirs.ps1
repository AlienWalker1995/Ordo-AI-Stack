# Creates data directories for bind mounts. Run before first docker compose up.
$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Get-Location).Path }
$data = if ($env:DATA_PATH) { $env:DATA_PATH -replace '\\', '/' } else { Join-Path $base "data" }
$dirs = @(
    (Join-Path $base "models\ollama"),
    (Join-Path $data "mcp"),
    (Join-Path $data "ops-controller"),
    (Join-Path $data "open-webui"),
    (Join-Path $data "comfyui-storage"),
    (Join-Path $data "comfyui-storage\ComfyUI\custom_nodes"),
    (Join-Path $data "comfyui-storage\ComfyUI\user\__manager"),
    (Join-Path $data "comfyui-output"),
    (Join-Path $data "comfyui-storage\ComfyUI\user\default\workflows"),
    (Join-Path $data "comfyui-storage\ComfyUI\user\default\workflows\mcp-api"),
    (Join-Path $data "n8n-data"),
    (Join-Path $data "n8n-files"),
    (Join-Path $data "dashboard"),
    (Join-Path $data "qdrant"),
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

# ComfyUI-Manager: seed security_level=weak so git/pip installs work when ComfyUI uses --listen (Docker)
$managerSeed = Join-Path $base "config\comfyui-manager-seed.ini"
$managerCfg = Join-Path $data "comfyui-storage\ComfyUI\user\__manager\config.ini"
if ((Test-Path $managerSeed) -and -not (Test-Path $managerCfg)) {
    Copy-Item $managerSeed $managerCfg -Force
    Write-Host ('OK ' + $managerCfg + ' (ComfyUI-Manager security_level=weak)')
}

# Seed ComfyUI user workflows (data/ is gitignored). API graphs live under mcp-api/ (compose default COMFY_MCP_DEFAULT_WORKFLOW_ID=mcp-api/generate_image).
$wfTemplateDir = Join-Path $base "workflow-templates\comfyui-workflows"
$wfMcpApi = Join-Path $data "comfyui-storage\ComfyUI\user\default\workflows\mcp-api"
$legacyWf = Join-Path $data "comfyui-workflows"
if (Test-Path $legacyWf) {
    Get-ChildItem $legacyWf -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object {
        $dest = Join-Path $wfMcpApi $_.Name
        if (-not (Test-Path $dest)) {
            Copy-Item $_.FullName $dest -Force
            Write-Host "OK migrate legacy comfyui-workflows/$($_.Name) -> .../workflows/mcp-api/"
        }
    }
}
if (Test-Path $wfTemplateDir) {
    Get-ChildItem $wfTemplateDir -Filter *.json | ForEach-Object {
        $dest = Join-Path $wfMcpApi $_.Name
        if (-not (Test-Path $dest)) {
            Copy-Item $_.FullName $dest -Force
            Write-Host "OK bootstrap workflows/mcp-api/$($_.Name)"
        }
    }
}

# Bootstrap MCP servers.txt with default tools (gateway hot-reloads)
$mcpServers = Join-Path $data "mcp\servers.txt"
$mcpRegistry = Join-Path $data "mcp\registry-custom.yaml"
if (-not (Test-Path $mcpServers)) {
    Set-Content -Path $mcpServers -Value "duckduckgo,n8n,tavily,comfyui" -NoNewline
    Write-Host "OK $mcpServers (duckduckgo,n8n,tavily,comfyui)"
}
# Bootstrap catalog fragment for ComfyUI (gateway uses --additional-catalog)
$registryTemplate = Join-Path $base "mcp\gateway\registry-custom.yaml"
if (-not (Test-Path $mcpRegistry) -and (Test-Path $registryTemplate)) {
    Copy-Item $registryTemplate $mcpRegistry -Force
    Write-Host "OK $mcpRegistry"
}

# Bootstrap openclaw.json with Ollama provider if config doesn't exist
$openclawConfig = Join-Path $data "openclaw\openclaw.json"
$openclawConfigExample = Join-Path $base "openclaw\openclaw.json.example"
if (-not (Test-Path $openclawConfig) -and (Test-Path $openclawConfigExample)) {
    Copy-Item $openclawConfigExample $openclawConfig -Force
    Write-Host "OK openclaw config (Ollama provider)"
}

# Ensure root .env has OPENCLAW_GATEWAY_TOKEN (required for OpenClaw service)
$rootEnv = Join-Path $base ".env"
$rootExample = Join-Path $base ".env.example"
$needsCreate = -not (Test-Path $rootEnv)
$needsToken = $false
if ((Test-Path $rootEnv)) {
    $existing = Get-Content $rootEnv -Raw -ErrorAction SilentlyContinue
    $needsToken = $existing -and ($existing -match 'OPENCLAW_GATEWAY_TOKEN=change-me|OPENCLAW_GATEWAY_TOKEN=\s*$' -or -not ($existing -match 'OPENCLAW_GATEWAY_TOKEN=.+'))
}
if ($needsCreate -or $needsToken) {
    $token = -join ((1..32 | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) }))
    if ($needsCreate -and (Test-Path $rootExample)) {
        Copy-Item $rootExample $rootEnv -Force
    }
    if (Test-Path $rootEnv) {
        $content = Get-Content $rootEnv -Raw
        if ($content -match 'OPENCLAW_GATEWAY_TOKEN=') {
            $content = $content -replace 'OPENCLAW_GATEWAY_TOKEN=[^\r\n]*', "OPENCLAW_GATEWAY_TOKEN=$token"
        } else {
            $content = $content.TrimEnd() + "`n# OpenClaw gateway auth (pinned; do not change unless re-pairing all devices)`nOPENCLAW_GATEWAY_TOKEN=$token`n"
        }
        Set-Content -Path $rootEnv -Value $content -NoNewline
    } else {
        Set-Content -Path $rootEnv -Value @"
BASE_PATH=$($base -replace '\\', '/')
OPENCLAW_GATEWAY_TOKEN=$token
"@
    }
    Write-Host "OK .env ($(if ($needsCreate) { 'created' } else { 'OPENCLAW_GATEWAY_TOKEN set' }))"
}

# Auto-detect GPU and generate overrides/compute.yml
$detectScript = Join-Path $base "scripts\detect_hardware.py"
if (Test-Path $detectScript) {
    $env:BASE_PATH = $base -replace '\\', '/'
    python $detectScript 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "OK Hardware detected (overrides/compute.yml)" }
}

# Configure Claude Code to route through the local model-gateway (optional; dashboard toggle: data/dashboard/claude_code_env_overwrite.json)
$claudeEnvJson = Join-Path $data "dashboard\claude_code_env_overwrite.json"
$claudeEnvOverwrite = $true
if (Test-Path $claudeEnvJson) {
    try {
        $j = Get-Content $claudeEnvJson -Raw | ConvertFrom-Json
        if ($null -ne $j.PSObject.Properties["enabled"]) {
            $claudeEnvOverwrite = [bool]$j.enabled
        }
    } catch { }
}
if (Get-Command claude -ErrorAction SilentlyContinue) {
    $port = if ($env:MODEL_GATEWAY_PORT) { $env:MODEL_GATEWAY_PORT } else { "11435" }
    if ($claudeEnvOverwrite) {
        [System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "local", "User")
        [System.Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "http://localhost:$port", "User")
        Write-Host "OK Claude Code configured -> http://localhost:$port (restart terminal to apply)"
        Write-Host "   Usage: claude --model <ollama-model-name>"
    } else {
        $curKey = [System.Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
        $curUrl = [System.Environment]::GetEnvironmentVariable("ANTHROPIC_BASE_URL", "User")
        if ($curKey -eq 'local' -and $curUrl -match '^http://localhost:\d+$') {
            [System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $null, "User")
            [System.Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $null, "User")
            Write-Host "OK Claude Code ANTHROPIC_* user overrides cleared (local routing off in dashboard). Restart terminal."
        } else {
            Write-Host 'Claude Code: local Model Gateway routing disabled in dashboard - skipped setting ANTHROPIC_*.'
        }
    }
} else {
    Write-Host "Note: Claude Code not installed. To install:"
    Write-Host "        npm install -g @anthropic-ai/claude-code"
    Write-Host "      Then re-run this script to configure it automatically."
}

Write-Host "Directories ready."
