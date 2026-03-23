# Install Python requirements for a custom_nodes subfolder into the running comfyui container.
# Usage (from repo root): .\scripts\comfyui\install_node_requirements.ps1 -NodePath "MyNodePack"
# Requires: docker compose, comfyui service up; BASE_PATH optional (defaults to current directory).

param(
    [Parameter(Mandatory = $true)]
    [string]$NodePath
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = if ($env:BASE_PATH) { $env:BASE_PATH } else { (Resolve-Path (Join-Path $ScriptDir "..\..")).Path }

$Posix = $NodePath -replace '\\', '/'
$Sub = $Posix.TrimStart('/') -replace '/', '\'
$Req = Join-Path $Base "data\comfyui-storage\ComfyUI\custom_nodes\$Sub\requirements.txt"

if (-not (Test-Path -LiteralPath $Req)) {
    Write-Error "Missing requirements file: $Req"
}

Push-Location $Base
try {
    docker compose exec comfyui pip install -r "/root/ComfyUI/custom_nodes/$Posix/requirements.txt"
} finally {
    Pop-Location
}
