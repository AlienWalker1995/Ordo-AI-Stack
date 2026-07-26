<#
  Ordo installer (Windows / PowerShell).

  One command from a fresh machine to the interactive setup wizard:

      irm https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.ps1 | iex

  Mirrors install.sh (macOS/Linux) step-for-step: checks prerequisites, clones the repo,
  installs the `ordo` CLI into a .venv, and launches `ordo init`. Because `irm | iex` leaves
  the console attached, the wizard runs fully interactively in this same terminal.

  Overrides (set before running):  $env:ORDO_DIR = 'D:\ordo'   $env:ORDO_REPO_URL = '...'
#>

#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  x $m" -ForegroundColor Red; exit 1 }
function Have($c) { $null -ne (Get-Command $c -ErrorAction SilentlyContinue) }

$RepoUrl    = if ($env:ORDO_REPO_URL) { $env:ORDO_REPO_URL } else { 'https://github.com/AlienWalker1995/Ordo-AI-Stack.git' }
$DefaultDir = if ($env:ORDO_DIR)      { $env:ORDO_DIR }      else { Join-Path $HOME 'ordo' }
# irm | iex does NOT consume the console, so stdin stays interactive unless genuinely redirected.
$Interactive = -not [Console]::IsInputRedirected

Write-Host ''
Write-Host 'Ordo installer' -ForegroundColor Green
Write-Host '--------------------------------------------------'

# -- 1. Prerequisites ---------------------------------------------------------
Info 'Checking prerequisites'
if (-not (Have git))    { Die 'git not found - install Git for Windows (https://git-scm.com) and re-run.' }
if (-not (Have docker)) { Die 'Docker not found - install Docker Desktop (https://docs.docker.com/get-docker/) and re-run.' }
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Die "'docker compose' (v2) not found - enable it in Docker Desktop and re-run." }

$probe  = 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
$Python = $null
foreach ($cand in @('python', 'py', 'python3')) {
    if (-not (Have $cand)) { continue }
    if ($cand -eq 'py') { & $cand -3 -c $probe *> $null } else { & $cand -c $probe *> $null }
    if ($LASTEXITCODE -eq 0) { $Python = $cand; break }
}
if (-not $Python) { Die 'Python 3.11+ not found - install from https://python.org (tick "Add python.exe to PATH") and re-run.' }

if (Have nvidia-smi) {
    $gpu = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
    Info "NVIDIA GPU detected: $gpu"
} else {
    Warn 'nvidia-smi not found - the stack will run CPU-only (no image/video/voice). Continuing.'
}

# -- 2. Locate or clone the repo ---------------------------------------------
# When downloaded and run as a file we may already be inside a clone; when piped
# through iex there is no script file, so $PSCommandPath is null and we clone.
$ScriptDir = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { $null }
if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir 'pyproject.toml')) -and (Test-Path (Join-Path $ScriptDir 'ordo'))) {
    $Target = $ScriptDir
    Info "Using existing clone at $Target"
} else {
    $Target = $DefaultDir
    if ($Interactive) {
        $ans = Read-Host "Install directory [$DefaultDir]"
        if ($ans) { $Target = $ans }
    }
    if ((Test-Path (Join-Path $Target '.git')) -and (Test-Path (Join-Path $Target 'pyproject.toml'))) {
        Info "Repo already present at $Target - pulling latest"
        git -C $Target pull --ff-only
        if ($LASTEXITCODE -ne 0) { Warn 'git pull failed (local changes?) - using the existing checkout.' }
    } elseif ((Test-Path $Target) -and (Get-ChildItem -Force $Target -ErrorAction SilentlyContinue)) {
        Die "$Target exists and is not an Ordo clone - set `$env:ORDO_DIR to another path and re-run."
    } else {
        Info "Cloning $RepoUrl -> $Target"
        git clone --depth 1 $RepoUrl $Target
        if ($LASTEXITCODE -ne 0) { Die 'git clone failed.' }
    }
}
Set-Location $Target

# -- 3. Install the ordo CLI into a virtualenv -------------------------------
Info 'Installing the ordo CLI (virtualenv .venv)'
if (-not (Test-Path '.venv')) {
    if ($Python -eq 'py') { & py -3 -m venv .venv } else { & $Python -m venv .venv }
}
$VenvPy = Join-Path $Target '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) { Die 'failed to create virtualenv - is the Python venv module installed?' }
& $VenvPy -m pip install --quiet --upgrade pip *> $null
& $VenvPy -m pip install --quiet .
if ($LASTEXITCODE -ne 0) { Die 'pip install failed - check the output above.' }

# -- 4. Run the wizard --------------------------------------------------------
Info 'Launching the setup wizard'
if ($Interactive) {
    # Console is attached (irm | iex keeps it), so the wizard prompts run right here.
    & $VenvPy -m ordo init --out out
} else {
    Warn 'No interactive console (redirected input) - writing config non-interactively (no bring-up).'
    & $VenvPy -m ordo init --yes --out out
    Write-Host ''
    Write-Host "Config written to $Target\out\. To finish interactively:"
    Write-Host "  cd `"$Target`"; .\.venv\Scripts\python.exe -m ordo init --out out --force"
}

Info "Done. Repo: $Target"
