# Creates data directories for bind mounts. Run before first docker compose up.
$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Get-Location).Path }
$data = if ($env:DATA_PATH) { $env:DATA_PATH -replace '\\', '/' } else { Join-Path $base "data" }
$dirs = @(
    (Join-Path $base "models\gguf"),
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

# Ensure root .env exists and has basic setup (e.g., .env.example copy)
$rootEnv = Join-Path $base ".env"
$rootExample = Join-Path $base ".env.example"
$needsCreate = -not (Test-Path $rootEnv)
if ($needsCreate -and (Test-Path $rootExample)) {
    Copy-Item $rootExample $rootEnv -Force
    Write-Host "OK .env (created)"
}

# Auto-detect GPU and generate overrides/compute.yml
$detectScript = Join-Path $base "scripts\detect_hardware.py"
if (Test-Path $detectScript) {
    $env:BASE_PATH = $base -replace '\\', '/'
    python $detectScript 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "OK Hardware detected (overrides/compute.yml)" }
}

Write-Host "Directories ready."
