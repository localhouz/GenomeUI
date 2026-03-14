param(
    [string]$SessionId = "mysharedsurface",
    [int]$BackendPort = 8787,
    [int]$FrontendPort = 5173,
    [int]$NousPort = 7700,
    [string]$NousModel = "phi4-mini",
    [switch]$Bootstrap,
    [switch]$SmokeTest,
    [switch]$TestOnly,
    [switch]$WatchBackend
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

# -- Helpers --------------------------------------------------------------------

function Require-Command([string]$name, [string]$message) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { throw $message }
    return $cmd.Source
}

function Find-SystemPython {
    $paths = @(
        "C:\Users\steve\AppData\Local\Programs\Python\Python312\python.exe",
        "C:\Users\steve\AppData\Local\Programs\Python\Python311\python.exe"
    )
    foreach ($p in $paths) { if (Test-Path $p) { return $p } }
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }
    throw "System Python not found. Install Python 3.11+ first."
}

function Ensure-Venv {
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) { return $venvPython }
    $systemPython = Find-SystemPython
    Write-Host "Creating .venv using $systemPython" -ForegroundColor Yellow
    & $systemPython -m venv ".venv"
    if (-not (Test-Path $venvPython)) { throw "Failed to create .venv" }
    return $venvPython
}

function Get-LanIPv4 {
    $all = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
        Where-Object {
            $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
            -not $_.IPAddressToString.StartsWith("127.")
        } | Select-Object -ExpandProperty IPAddressToString
    foreach ($ip in $all) {
        if ($ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)') { return $ip }
    }
    return ($all | Select-Object -First 1)
}

function Clear-Port([int]$port) {
    $pids = netstat -ano 2>$null |
        Select-String ":$port\s" |
        ForEach-Object { ($_ -split '\s+')[-1] } |
        Where-Object { $_ -match '^\d+$' } |
        Select-Object -Unique
    foreach ($procId in $pids) {
        try { Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue } catch {}
    }
}

function Wait-Http([string]$url, [int]$timeoutSeconds = 20) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 2
            return $true
        } catch {}
        Start-Sleep -Milliseconds 350
    }
    return $false
}

function Run-Smoke([string]$sessionId, [int]$port) {
    Write-Host "Running smoke test..." -ForegroundColor Yellow
    $turn = Invoke-RestMethod -Uri "http://localhost:$port/api/turn" -Method Post `
        -ContentType "application/json" `
        -Body (@{ sessionId = $sessionId; intent = "add task launcher smoke test" } | ConvertTo-Json)
    if (-not $turn.revision -or -not $turn.planner) { throw "Smoke test failed: invalid turn response" }
    Write-Host "Smoke ok | revision=$($turn.revision) planner=$($turn.planner)" -ForegroundColor Green
}

# -- Setup ----------------------------------------------------------------------

$npmCmd = Require-Command "npm.cmd" "npm.cmd not found on PATH."
$venvPython = Ensure-Venv

if ($Bootstrap -or -not (Test-Path (Join-Path $root "node_modules"))) {
    Write-Host "Bootstrapping dependencies..." -ForegroundColor Yellow
    & $venvPython -m pip install -r "requirements.txt"
    & $npmCmd install
}

$lanIp = Get-LanIPv4
$desktopUrl = "http://localhost:$FrontendPort/?session=$SessionId"
$phoneUrl = if ($lanIp) { "http://$lanIp`:$FrontendPort/?session=$SessionId" } else { "(LAN IP unavailable)" }
$nousExe = Join-Path $root "..\Nous\rust\target\debug\nous-server.exe"
$nousAvailable = Test-Path $nousExe

Write-Host ""
Write-Host "GenomeUI OS Launcher" -ForegroundColor Cyan
Write-Host "venv:     $venvPython"
if ($nousAvailable) {
    Write-Host "Nous:     http://localhost:$NousPort ($NousModel)"
} else {
    Write-Host "Nous:     embedded classifier (gateway not found)" -ForegroundColor DarkYellow
}
Write-Host "Backend:  http://localhost:$BackendPort"
Write-Host "Desktop:  $desktopUrl"
Write-Host "Phone:    $phoneUrl"
Write-Host ""

# -- Boot -----------------------------------------------------------------------

Write-Host "Clearing ports..." -ForegroundColor DarkGray
if ($nousAvailable) { Clear-Port $NousPort }
Clear-Port $BackendPort
Clear-Port $FrontendPort
Start-Sleep -Milliseconds 500   # let OS release sockets

$nous = $null
$backend = $null
$frontend = $null

try {
    # 1. Nous gateway — optional; backend embedded classifier is the fallback
    if ($nousAvailable) {
        $nous = Start-Process -FilePath $nousExe `
            -ArgumentList "--port", "$NousPort", "--model", $NousModel, "--genomeui", "http://localhost:$BackendPort" `
            -WorkingDirectory $root `
            -PassThru
        Write-Host "Nous       pid=$($nous.Id)" -ForegroundColor DarkCyan

        if (-not (Wait-Http -url "http://127.0.0.1:$NousPort/health" -timeoutSeconds 20)) {
            throw "Nous gateway failed to start on port $NousPort"
        }
        Write-Host "Nous       ready" -ForegroundColor Green
    }

    # 2. Backend
    #    not a service the backend calls. Classification arrives via body.nousIntent.
    $env:NOUS_URL = ""

    # ── Load .env (OAuth credentials and other secrets live there, not here) ──
    $dotEnv = Join-Path $root ".env"
    if (Test-Path $dotEnv) {
        Get-Content $dotEnv | Where-Object { $_ -match '^\s*[^#]\S+=\S' } | ForEach-Object {
            $parts = $_ -split '=', 2
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
        }
    } else {
        Write-Host "Warning: .env not found - OAuth connectors will run in scaffold mode." -ForegroundColor Yellow
    }
    $backendArgs = @("-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "$BackendPort")
    if ($WatchBackend) { $backendArgs += "--reload" }

    $backendOut = Join-Path $root "backend.out.log"
    $backendErr = Join-Path $root "backend.err.log"
    $backend = Start-Process -FilePath $venvPython `
        -ArgumentList $backendArgs `
        -WorkingDirectory $root `
        -RedirectStandardOutput $backendOut `
        -RedirectStandardError  $backendErr `
        -NoNewWindow `
        -PassThru
    Write-Host "Backend    pid=$($backend.Id)  err=$backendErr" -ForegroundColor DarkCyan

    if (-not (Wait-Http -url "http://127.0.0.1:$BackendPort/api/health" -timeoutSeconds 25)) {
        Write-Host ""
        Write-Host "--- backend stderr (last 40 lines) ---" -ForegroundColor Yellow
        if (Test-Path $backendErr) { Get-Content $backendErr -Tail 40 | ForEach-Object { Write-Host $_ } }
        Write-Host "--- backend stdout (last 20 lines) ---" -ForegroundColor Yellow
        if (Test-Path $backendOut) { Get-Content $backendOut -Tail 20 | ForEach-Object { Write-Host $_ } }
        throw "Backend failed to start - see log above"
    }
    Write-Host "Backend    ready" -ForegroundColor Green

    if ($SmokeTest -or $TestOnly) { Run-Smoke -sessionId $SessionId -port $NousPort }
    if ($TestOnly) { Write-Host "Test-only mode complete." -ForegroundColor Green; return }

    # 3. Frontend
    $frontend = Start-Process -FilePath $npmCmd `
        -ArgumentList "run", "dev:client" `
        -WorkingDirectory $root `
        -PassThru
    Write-Host "Frontend   pid=$($frontend.Id)" -ForegroundColor DarkCyan
    Write-Host ""
    Write-Host "OS running. Open: $desktopUrl" -ForegroundColor Cyan
    Write-Host "Press Ctrl+C to shut down." -ForegroundColor DarkGray
    Write-Host ""

    $procs = @($nous, $backend, $frontend) | Where-Object { $_ -ne $null }
    Wait-Process -Id ($procs | Select-Object -ExpandProperty Id)
}
finally {
    Write-Host "Shutting down..." -ForegroundColor DarkGray
    foreach ($proc in @($frontend, $backend, $nous)) {
        if ($null -ne $proc) {
            try { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force } } catch {}
        }
    }
}
