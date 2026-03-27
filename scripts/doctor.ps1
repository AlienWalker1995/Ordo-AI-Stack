# Doctor: quick health probes + optional openclaw.json validation (M7).
# Usage: .\scripts\doctor.ps1
# Env: MODEL_GATEWAY_URL, MCP_GATEWAY_URL, DASHBOARD_URL, OPENCLAW_CONFIG_PATH, ORDO_AI_STACK_ROOT
#      DOCTOR_DEPS_TIMEOUT_SEC - max seconds for GET /api/dependencies (default 120; many sequential probes)
#      DOCTOR_STRICT=1 - treat optional Ollama/MCP host probes as FAIL if unreachable (default: WARN only)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

$mg = if ($env:MODEL_GATEWAY_URL) { $env:MODEL_GATEWAY_URL } else { "http://localhost:11435" }
$mcp = if ($env:MCP_GATEWAY_URL) { $env:MCP_GATEWAY_URL } else { "http://localhost:8811" }
$dash = if ($env:DASHBOARD_URL) { $env:DASHBOARD_URL } else { "http://localhost:8080" }
$ollama = if ($env:OLLAMA_URL) { $env:OLLAMA_URL } else { "http://localhost:11434" }

$fail = 0

function Get-DashboardAuthHeaders {
    $token = $env:DASHBOARD_AUTH_TOKEN
    if (-not $token) {
        $ef = Join-Path $RepoRoot ".env"
        if (Test-Path $ef) {
            $line = Select-String -Path $ef -Pattern '^\s*DASHBOARD_AUTH_TOKEN\s*=\s*(.+)\s*$' | Select-Object -First 1
            if ($line) {
                $token = $line.Matches.Groups[1].Value.Trim().Trim('"').Trim("'")
            }
        }
    }
    if ($token) {
        return @{ Authorization = "Bearer $token" }
    }
    return @{}
}

function Test-Probe {
    param([string]$Name, [string]$Url)
    $headers = @{}
    if ($Url.StartsWith($dash.TrimEnd('/'))) {
        $headers = Get-DashboardAuthHeaders
    }
    try {
        $params = @{ Uri = $Url; UseBasicParsing = $true; TimeoutSec = 5 }
        if ($headers.Count -gt 0) { $params['Headers'] = $headers }
        Invoke-WebRequest @params | Out-Null
        Write-Host "  OK   $Name"
    } catch {
        Write-Host "  FAIL $Name ($Url)" -ForegroundColor Red
        $script:fail = 1
    }
}

function Test-ProbeDependencies {
    param([string]$Name, [string]$Url)
    $sec = 120
    if ($env:DOCTOR_DEPS_TIMEOUT_SEC) {
        $p = 0
        if ([int]::TryParse($env:DOCTOR_DEPS_TIMEOUT_SEC, [ref]$p) -and $p -ge 10) {
            $sec = $p
        }
    }
    try {
        $params = @{ Uri = $Url; UseBasicParsing = $true; TimeoutSec = $sec }
        $h = Get-DashboardAuthHeaders
        if ($Url.StartsWith($dash.TrimEnd('/')) -and $h.Count -gt 0) { $params['Headers'] = $h }
        Invoke-WebRequest @params | Out-Null
        Write-Host "  OK   $Name"
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 404) {
            Write-Host "  WARN $Name - not found (HTTP 404); rebuild: docker compose build dashboard" -ForegroundColor Yellow
        } else {
            Write-Host "  FAIL $Name ($Url)" -ForegroundColor Red
            $script:fail = 1
        }
    }
}

function Test-ProbeReady {
    param([string]$Name, [string]$Url)
    # GET /ready: 503 = not ready for inference; 404 = old image without /ready (rebuild model-gateway).
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -SkipHttpErrorCheck
        if ($r.StatusCode -eq 200) {
            Write-Host "  OK   $Name"
        } elseif ($r.StatusCode -eq 503) {
            Write-Host "  WARN $Name - not ready (HTTP 503); pull a model or fix backends" -ForegroundColor Yellow
        } elseif ($r.StatusCode -eq 404) {
            Write-Host "  WARN $Name - GET /ready not found (HTTP 404); rebuild: docker compose build model-gateway" -ForegroundColor Yellow
        } else {
            Write-Host "  FAIL $Name (HTTP $($r.StatusCode))" -ForegroundColor Red
            $script:fail = 1
        }
    } else {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 | Out-Null
            Write-Host "  OK   $Name"
        } catch {
            $resp = $_.Exception.Response
            if ($null -ne $resp) {
                $code = [int]$resp.StatusCode
                if ($code -eq 503) {
                    Write-Host "  WARN $Name - not ready (HTTP 503); pull a model or fix backends" -ForegroundColor Yellow
                } elseif ($code -eq 404) {
                    Write-Host "  WARN $Name - GET /ready not found (HTTP 404); rebuild: docker compose build model-gateway" -ForegroundColor Yellow
                } else {
                    Write-Host "  FAIL $Name ($Url) (HTTP $code)" -ForegroundColor Red
                    $script:fail = 1
                }
            } else {
                Write-Host "  FAIL $Name ($Url)" -ForegroundColor Red
                $script:fail = 1
            }
        }
    }
}

function Test-ProbeOptionalBackendHost {
    param([string]$Name, [string]$Url, [string]$ExposeHint)
    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 | Out-Null
        Write-Host "  OK   $Name"
    } catch {
        $msg = "  WARN $Name - not reachable on host ($Url). Default compose keeps this backend internal. $ExposeHint"
        if ($env:DOCTOR_STRICT -eq '1') {
            Write-Host "  FAIL $Name ($Url)" -ForegroundColor Red
            $script:fail = 1
        } else {
            Write-Host $msg -ForegroundColor Yellow
        }
    }
}

function Test-ProbeMcpGatewayHost {
    param([string]$Name, [string]$Url, [string]$ExposeHint)
    # GET /mcp often returns 400 (needs Streamable HTTP POST); any TCP+HTTP response means the gateway is up.
    try {
        if ($PSVersionTable.PSVersion.Major -ge 6) {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -SkipHttpErrorCheck
            Write-Host "  OK   $Name (HTTP $($r.StatusCode))"
        } else {
            try {
                Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 | Out-Null
                Write-Host "  OK   $Name"
            } catch {
                $resp = $_.Exception.Response
                if ($null -ne $resp) {
                    Write-Host "  OK   $Name (HTTP $([int]$resp.StatusCode))"
                } else {
                    throw
                }
            }
        }
    } catch {
        $msg = "  WARN $Name - not reachable on host ($Url). Default compose keeps this backend internal. $ExposeHint"
        if ($env:DOCTOR_STRICT -eq '1') {
            Write-Host "  FAIL $Name ($Url)" -ForegroundColor Red
            $script:fail = 1
        } else {
            Write-Host $msg -ForegroundColor Yellow
        }
    }
}

Write-Host "==> Ordo AI Stack doctor (M7)"
Write-Host "==> Probes (published host ports)"
Test-Probe "dashboard /api/health"      "$dash/api/health"
Test-ProbeDependencies "dashboard /api/dependencies" "$dash/api/dependencies"
Test-Probe "model-gateway /health"      "$mg/health"
Test-ProbeReady "model-gateway /ready"       "$mg/ready"
Write-Host "==> Probes (optional: Ollama/MCP on localhost only if you use expose overrides)"
Test-ProbeOptionalBackendHost "ollama /api/version" "$ollama/api/version" "See overrides/ollama-expose.yml"
Test-ProbeMcpGatewayHost "mcp-gateway /mcp" "$mcp/mcp" "See overrides/mcp-expose.yml"

Write-Host "==> OpenClaw config validation"
& python (Join-Path $ScriptDir "validate_openclaw_config.py")
if ($LASTEXITCODE -ne 0) { $fail = 1 }

if ($fail -ne 0) {
    Write-Host "==> doctor FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "==> doctor PASSED"
exit 0
