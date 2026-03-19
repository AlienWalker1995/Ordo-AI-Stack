# Copies workspace templates (SOUL.md, AGENTS.md, TOOLS.md) to data/openclaw/workspace
# if they don't already exist. Uses *.example as source (gitignored originals stay local).
# Run after ensure_dirs.ps1.
$ErrorActionPreference = "Stop"
$base = if ($env:BASE_PATH) { $env:BASE_PATH -replace '\\', '/' } else { (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$data = if ($env:DATA_PATH) { $env:DATA_PATH -replace '\\', '/' } else { Join-Path $base "data" }
$workspace = Join-Path $data "openclaw\workspace"
$templates = Join-Path $base "openclaw\workspace"

$files = @("SOUL.md", "AGENTS.md", "TOOLS.md")
foreach ($f in $files) {
    $dst = Join-Path $workspace $f
    if (-not (Test-Path $dst)) {
        $src = Join-Path $templates $f
        $srcExample = Join-Path $templates "$f.example"
        if (Test-Path $src) {
            Copy-Item $src $dst -Force
            Write-Host "Copied $f to workspace"
        } elseif (Test-Path $srcExample) {
            Copy-Item $srcExample $dst -Force
            Write-Host "Copied $f.example to workspace as $f"
        }
    }
}
Write-Host "OpenClaw workspace ready."
