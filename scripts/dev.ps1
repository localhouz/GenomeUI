param(
    [string]$SessionId = "mysharedsurface",
    [int]$BackendPort = 8787,
    [int]$FrontendPort = 5173,
    [switch]$Bootstrap,
    [switch]$SmokeTest,
    [switch]$TestOnly,
    [switch]$WatchBackend
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

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
    foreach ($p in $paths) {
        if (Test-Path $p) { return $p }
    }

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

    if (-not (Test-Path $venvPython)) {
        throw "Failed to create .venv"
    }
    return $venvPython
}

function Get-LanIPv4 {
    $all = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
        Where-Object {
            $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
            -not $_.IPAddressToString.StartsWith("127.")
        } |
        Select-Object -ExpandProperty IPAddressToString

    foreach ($ip in $all) {
        if ($ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)') {
            return $ip
        }
    }
    return ($all | Select-Object -First 1)
}

function Wait-Http([string]$url, [int]$timeoutSeconds = 20) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 2
            if ($resp) { return $true }
        }
        catch {}
        Start-Sleep -Milliseconds 350
    }
    return $false
}

function Run-Smoke([string]$sessionId, [int]$port) {
    Write-Host "Running backend smoke test..." -ForegroundColor Yellow
    $init = Invoke-RestMethod -Uri "http://localhost:$port/api/session/init" -Method Post -ContentType "application/json" -Body (@{ sessionId = $sessionId } | ConvertTo-Json)
    $turn = Invoke-RestMethod -Uri "http://localhost:$port/api/turn" -Method Post -ContentType "application/json" -Body (@{ sessionId = $sessionId; intent = "add task launcher smoke test" } | ConvertTo-Json)

    if (-not $turn.revision -or -not $turn.planner) {
        throw "Smoke test failed: invalid turn response"
    }

    Write-Host "Smoke test ok | revision=$($turn.revision) planner=$($turn.planner)" -ForegroundColor Green
}

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

Write-Host ""
Write-Host "GenomeUI OS Launcher" -ForegroundColor Cyan
Write-Host "venv:     $venvPython"
Write-Host "Backend:  http://localhost:$BackendPort"
Write-Host "Desktop:  $desktopUrl"
Write-Host "Phone:    $phoneUrl"
Write-Host ""

$backend = $null
$frontend = $null

try {
    $backendArgs = @("-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "$BackendPort")
    if ($WatchBackend) { $backendArgs += "--reload" }

    $backend = Start-Process -FilePath $venvPython `
        -ArgumentList $backendArgs `
        -WorkingDirectory $root `
        -PassThru

    if (-not (Wait-Http -url "http://localhost:$BackendPort/api/health" -timeoutSeconds 25)) {
        throw "Backend failed to start on port $BackendPort"
    }

    if ($SmokeTest -or $TestOnly) {
        Run-Smoke -sessionId $SessionId -port $BackendPort
    }

    if ($TestOnly) {
        Write-Host "Test-only mode complete." -ForegroundColor Green
        return
    }

    $frontend = Start-Process -FilePath $npmCmd `
        -ArgumentList "run", "dev:client" `
        -WorkingDirectory $root `
        -PassThru

    Write-Host "Processes started. Press Ctrl+C to stop." -ForegroundColor Yellow
    Wait-Process -Id $backend.Id, $frontend.Id
}
finally {
    foreach ($proc in @($backend, $frontend)) {
        if ($null -ne $proc) {
            try {
                if (-not $proc.HasExited) {
                    Stop-Process -Id $proc.Id -Force
                }
            }
            catch {}
        }
    }
}

