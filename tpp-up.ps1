<#
.SYNOPSIS
    Single-command startup for the entire TPP engine.

.DESCRIPTION
    1. Starts Docker Desktop if not running
    2. Waits for tpp-redis + tpp-postgres to be healthy
    3. Activates the Python venv
    4. Initialises the database schema (tpp-init-db)
    5. Authenticates with Fyers (tpp-auth --manual)
    6. Launches all services in separate windows:
         ingest (auto-fetches expiries from Fyers) -> candles ->
         greeks -> strategies -> orders -> ui
    7. Ctrl+C in this window shuts everything down

.EXAMPLE
    .\tpp-up.ps1
    .\tpp-up.ps1 -SkipAuth             # token still valid from earlier
    .\tpp-up.ps1 -SkipOrders           # no paper trading
    .\tpp-up.ps1 -SkipUI               # headless
    .\tpp-up.ps1 -AutoAuth             # TOTP auto-login instead of browser
#>

param(
    [switch]$SkipAuth,
    [switch]$SkipOrders,
    [switch]$SkipUI,
    [switch]$AutoAuth,
    [switch]$SkipDocker
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ── Colours ────────────────────────────────────────────────────
function Write-Step  ($n, $msg) { Write-Host "[$n] $msg" -ForegroundColor Cyan }
function Write-Ok    ($msg)     { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Wait  ($msg)     { Write-Host "    ... $msg" -ForegroundColor Yellow }
function Write-Fail  ($msg)     { Write-Host "    FAIL: $msg" -ForegroundColor Red; exit 1 }

# ── Track child processes for cleanup ──────────────────────────
$script:Children = @()

function Stop-AllChildren {
    Write-Host ""
    Write-Host "Shutting down TPP services..." -ForegroundColor Yellow
    foreach ($child in $script:Children) {
        $name = $child.Name
        $proc = $child.Process
        if (-not $proc.HasExited) {
            Write-Host "  Stopping $name (PID $($proc.Id))..." -ForegroundColor Yellow
            try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
    Write-Host "All services stopped." -ForegroundColor Green
}

# Register cleanup on Ctrl+C
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action { Stop-AllChildren }
trap { Stop-AllChildren; break }

# ══════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "========================================" -ForegroundColor White
Write-Host "  Trading Plug&Play — Engine Startup"     -ForegroundColor White
Write-Host "========================================" -ForegroundColor White
Write-Host ""

# ── 1. Docker Desktop ─────────────────────────────────────────
Write-Step 1 "Docker Desktop"

if ($SkipDocker) {
    Write-Ok "Skipped (--SkipDocker)"
} else {
    # Check if Docker daemon is responsive
    $dockerOk = $false
    try {
        $null = docker info 2>$null
        if ($LASTEXITCODE -eq 0) { $dockerOk = $true }
    } catch {}

    if (-not $dockerOk) {
        Write-Wait "Docker not responding — starting Docker Desktop..."
        $dockerPath = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dockerPath) {
            Start-Process $dockerPath
        } else {
            # Try via Start Menu shortcut
            Start-Process "Docker Desktop" -ErrorAction SilentlyContinue
        }

        # Wait for daemon to respond
        $attempt = 0
        $maxAttempts = 60   # 2 minutes
        while ($attempt -lt $maxAttempts) {
            Start-Sleep -Seconds 2
            $attempt++
            try {
                $null = docker info 2>$null
                if ($LASTEXITCODE -eq 0) { $dockerOk = $true; break }
            } catch {}
            if ($attempt % 5 -eq 0) {
                Write-Wait "Waiting for Docker daemon... ($($attempt * 2)s)"
            }
        }
        if (-not $dockerOk) { Write-Fail "Docker Desktop did not start in 2 minutes" }
    }
    Write-Ok "Docker daemon is responsive"

    # ── 2. Containers ──────────────────────────────────────────
    Write-Step 2 "Docker containers (Redis + Postgres)"
    docker compose up -d 2>&1 | Out-Null

    # Wait for healthy
    $attempt = 0
    $maxAttempts = 30
    while ($attempt -lt $maxAttempts) {
        $ps = docker compose ps --format "{{.Name}} {{.Status}}" 2>$null
        $redisOk    = ($ps | Where-Object { $_ -match "tpp-redis.*healthy" }).Count -gt 0
        $postgresOk = ($ps | Where-Object { $_ -match "tpp-postgres.*healthy" }).Count -gt 0
        if ($redisOk -and $postgresOk) { break }
        Start-Sleep -Seconds 2
        $attempt++
    }
    if (-not ($redisOk -and $postgresOk)) {
        Write-Fail "Containers did not become healthy in 60s"
    }
    Write-Ok "tpp-redis (healthy), tpp-postgres (healthy)"
}

# ── 3. Python venv ─────────────────────────────────────────────
Write-Step 3 "Python virtual environment"
if (-not (Test-Path ".\.venv\Scripts\Activate.ps1")) {
    Write-Fail "No .venv found. Run: python -m venv .venv && pip install -e ."
}
& .\.venv\Scripts\Activate.ps1
Write-Ok "Activated .venv"

# ── 4. Init DB ─────────────────────────────────────────────────
Write-Step 4 "Database schema (tpp-init-db)"
tpp-init-db 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Fail "tpp-init-db failed" }
Write-Ok "Schema ensured"

# ── 5. Auth ────────────────────────────────────────────────────
Write-Step 5 "Fyers authentication"
if ($SkipAuth) {
    Write-Ok "Skipped (--SkipAuth) — using cached token"
} elseif ($AutoAuth) {
    Write-Wait "TOTP auto-login..."
    tpp-auth
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    Auto-login failed. Falling back to browser flow..." -ForegroundColor Yellow
        tpp-auth --manual
    }
    if ($LASTEXITCODE -ne 0) { Write-Fail "Authentication failed" }
    Write-Ok "Token cached in Redis"
} else {
    Write-Wait "Browser login — complete the login in your browser..."
    tpp-auth --manual
    if ($LASTEXITCODE -ne 0) { Write-Fail "Authentication failed" }
    Write-Ok "Token cached in Redis"
}

# ── 6. Launch services ─────────────────────────────────────────
# Expiries are auto-fetched from Fyers symbol master by tpp-ingest
Write-Host ""
Write-Step 6 "Starting services (expiries auto-fetched from Fyers)"

$venvPython = "$PSScriptRoot\.venv\Scripts\python.exe"

function Start-Service ($name, $module, [string[]]$svcArgs) {
    Write-Wait "Starting $name..."
    # Run as: python -m trading.scripts.<module> <args>
    # Using python directly avoids Start-Process issues with .exe shims
    $allArgs = @("-m", $module) + $svcArgs
    $argString = ($allArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "
    $proc = Start-Process -FilePath $venvPython -ArgumentList $argString `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized -PassThru
    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        Write-Fail "$name exited immediately (code $($proc.ExitCode))"
    }
    $script:Children += @{ Name = $name; Process = $proc }
    Write-Ok "$name started (PID $($proc.Id))"
}

# 6a. Ingest (auto-fetches expiries from Fyers)
Start-Service "ingest" "trading.scripts.run_ingest" @()

# 6b. Candles
Start-Sleep -Seconds 1
Start-Service "candles" "trading.scripts.run_candles" @()

# 6c. Greeks
Start-Sleep -Seconds 1
Start-Service "greeks" "trading.scripts.run_greeks" @()

# 6d. Strategies
Start-Sleep -Seconds 1
# Uses STRATEGIES_ENABLED from .env by default
Start-Service "strategies" "trading.scripts.run_strategies" @()

# 6e. Orders
if (-not $SkipOrders) {
    Start-Sleep -Seconds 1
    Start-Service "orders" "trading.scripts.run_orders" @()
}

# 6f. UI
if (-not $SkipUI) {
    Start-Sleep -Seconds 1
    Start-Service "ui" "trading.scripts.run_ui" @()
}

# ── 7. Summary ─────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  TPP engine is UP" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
$svcNames = ($script:Children | ForEach-Object { $_.Name }) -join ", "
Write-Host "  Services : $svcNames" -ForegroundColor White
if (-not $SkipUI) {
    Write-Host "  Dashboard: http://127.0.0.1:8088" -ForegroundColor White
}
Write-Host "  Expiries : auto-fetched from Fyers" -ForegroundColor White
Write-Host ""
Write-Host "  Press Ctrl+C to shut down all services" -ForegroundColor Yellow
Write-Host ""

# ── 8. Monitor — watch for crashes, Ctrl+C to exit ────────────
try {
    while ($true) {
        foreach ($child in $script:Children) {
            if ($child.Process.HasExited) {
                $name = $child.Name
                $code = $child.Process.ExitCode
                Write-Host ""
                Write-Host "  $name exited unexpectedly (code $code)" -ForegroundColor Red
                Stop-AllChildren
                exit 1
            }
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Stop-AllChildren
}
