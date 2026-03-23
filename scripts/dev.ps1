param(
    [string]$SessionId    = "mysharedsurface",
    [int]$BackendPort     = 8787,
    [int]$FrontendPort    = 5173,
    [int]$NousPort        = 7700,
    # Recommended:
    #   qwen2.5:0.5b  -> fastest local intent classification path
    #   phi4-mini     -> slower, but useful for deeper experimentation
    [string]$NousModel    = "qwen2.5:0.5b",
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

function Resolve-NousBinary([string]$RepoRoot) {
    $candidates = @(
        (Join-Path $RepoRoot "nous\rust\target\debug\nous-server.exe"),
        (Join-Path $RepoRoot "Nous\rust\target\debug\nous-server.exe"),
        (Join-Path (Split-Path -Parent $RepoRoot) "nous\rust\target\debug\nous-server.exe"),
        (Join-Path (Split-Path -Parent $RepoRoot) "Nous\rust\target\debug\nous-server.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    return $null
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

function Load-DotEnvIfUnset([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | Where-Object { $_ -match '^\s*[^#]\S+=\S' } | ForEach-Object {
        $parts = $_ -split '=', 2
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        $existing = if ([string]::IsNullOrWhiteSpace($key)) { "" } else { [System.Environment]::GetEnvironmentVariable($key, "Process") }
        if (-not [string]::IsNullOrWhiteSpace($key) -and [string]::IsNullOrWhiteSpace($existing)) {
            [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

function Run-Smoke([string]$sessionId, [int]$port) {
    Write-Host "Running smoke test..." -ForegroundColor Yellow
    $turn = Invoke-RestMethod -Uri "http://localhost:$port/api/turn" -Method Post `
        -ContentType "application/json" `
        -Body (@{ sessionId = $sessionId; intent = "add task launcher smoke test" } | ConvertTo-Json)
    if (-not $turn.revision -or -not $turn.planner) { throw "Smoke test failed: invalid turn response" }
    Write-Host "Smoke ok | revision=$($turn.revision) planner=$($turn.planner)" -ForegroundColor Green
}

function Ensure-DevAuthBypassForAutomation {
    if (-not $env:GENOME_AUTH_ENABLED) {
        $env:GENOME_AUTH_ENABLED = "false"
        Write-Host "Auth bypass enabled for scripted smoke/test mode (set GENOME_AUTH_ENABLED=true to override)." -ForegroundColor Yellow
    }
}

function Services-Installed {
    $nous    = Get-ScheduledTask -TaskName "GenomeUI-Nous"    -ErrorAction SilentlyContinue
    $backend = Get-ScheduledTask -TaskName "GenomeUI-Backend" -ErrorAction SilentlyContinue
    return ($null -ne $nous -and $null -ne $backend)
}

function Ensure-ServiceRunning([string]$name, [string]$healthUrl, [int]$timeoutSeconds) {
    $task = Get-ScheduledTask -TaskName $name
    if ($task.State -ne "Running") {
        Start-ScheduledTask -TaskName $name
        Write-Host "$name  starting..." -ForegroundColor DarkCyan
    } else {
        Write-Host "$name  already running" -ForegroundColor DarkCyan
    }
    if (-not (Wait-Http $healthUrl $timeoutSeconds)) {
        throw "$name failed to become healthy at $healthUrl"
    }
    Write-Host "$name  ready" -ForegroundColor Green
}

# -- Setup ----------------------------------------------------------------------

$npmCmd    = Require-Command "npm.cmd" "npm.cmd not found on PATH."
$venvPython = Ensure-Venv

if ($Bootstrap -or -not (Test-Path (Join-Path $root "node_modules"))) {
    Write-Host "Bootstrapping dependencies..." -ForegroundColor Yellow
    & $venvPython -m pip install -r "requirements.txt"
    & $npmCmd install
}

if ($SmokeTest -or $TestOnly) {
    Ensure-DevAuthBypassForAutomation
}

$lanIp      = Get-LanIPv4
$desktopUrl = "http://localhost:$FrontendPort/?session=$SessionId"
$phoneUrl   = if ($lanIp) { "http://$lanIp`:$FrontendPort/?session=$SessionId" } else { "(LAN IP unavailable)" }

Write-Host ""
Write-Host "GenomeUI OS Launcher" -ForegroundColor Cyan
Write-Host "venv:     $venvPython"
Write-Host "Nous:     http://localhost:$NousPort ($NousModel)"
Write-Host "Backend:  http://localhost:$BackendPort"
Write-Host "Desktop:  $desktopUrl"
Write-Host "Phone:    $phoneUrl"
Write-Host ""

# -- Boot -----------------------------------------------------------------------

$serviceMode = Services-Installed
$nous        = $null
$backend     = $null
$frontend    = $null

try {
    if ($serviceMode) {
        # -----------------------------------------------------------------------
        # Service mode: Nous and Backend are OS services (Scheduled Tasks).
        # dev.ps1 just ensures they are running and waits for health.
        # Services self-supervise on crash; dev.ps1 does NOT kill them on exit.
        # -----------------------------------------------------------------------
        Write-Host "Service mode: using Windows Scheduled Tasks" -ForegroundColor DarkCyan

        Ensure-ServiceRunning "GenomeUI-Nous"    "http://127.0.0.1:$NousPort/health"      20
        Ensure-ServiceRunning "GenomeUI-Backend" "http://127.0.0.1:$BackendPort/api/health" 25

    } else {
        # -----------------------------------------------------------------------
        # Direct mode: services not installed - start processes directly.
        # Run scripts/install-services.ps1 for proper OS service registration.
        # -----------------------------------------------------------------------
        Write-Host "Direct mode: services not installed, starting processes directly" -ForegroundColor Yellow
        Write-Host "Tip: run scripts/install-services.ps1 to register as OS services." -ForegroundColor DarkGray
        Write-Host ""

        $nousExe = Resolve-NousBinary $root
        if (-not (Test-Path $nousExe)) {
            throw "Nous AI gateway not found in repo-local or sibling nous/Nous rust targets - build it first."
        }

        Write-Host "Clearing ports..." -ForegroundColor DarkGray
        Clear-Port $NousPort
        Clear-Port $BackendPort
        Clear-Port $FrontendPort
        Start-Sleep -Milliseconds 500

        # 1. Nous AI gateway - required
        $nous = Start-Process -FilePath $nousExe `
            -ArgumentList "--port", "$NousPort", "--model", $NousModel, "--genomeui", "http://localhost:$BackendPort" `
            -WorkingDirectory $root `
            -PassThru
        Write-Host "Nous       pid=$($nous.Id)" -ForegroundColor DarkCyan

        if (-not (Wait-Http -url "http://127.0.0.1:$NousPort/health" -timeoutSeconds 20)) {
            throw "Nous AI gateway failed to start on port $NousPort"
        }
        Write-Host "Nous       ready" -ForegroundColor Green

        # 2. Backend
        $env:NOUS_URL = ""

        $dotEnv = Join-Path $root ".env"
        if (Test-Path $dotEnv) {
            Load-DotEnvIfUnset $dotEnv
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
    }

    if ($SmokeTest -or $TestOnly) { Run-Smoke -sessionId $SessionId -port $NousPort }
    if ($TestOnly) { Write-Host "Test-only mode complete." -ForegroundColor Green; return }

    # 3. Frontend (Vite) - always started directly regardless of service mode
    $env:NOUS_URL = ""
    $dotEnv = Join-Path $root ".env"
    if ((-not $serviceMode) -and (Test-Path $dotEnv)) {
        # Already loaded above in direct mode; load here for service mode
    } elseif ($serviceMode -and (Test-Path $dotEnv)) {
        Load-DotEnvIfUnset $dotEnv
    }

    $frontend = Start-Process -FilePath $npmCmd `
        -ArgumentList "run", "dev:client" `
        -WorkingDirectory $root `
        -PassThru
    Write-Host "Frontend   pid=$($frontend.Id)" -ForegroundColor DarkCyan

    # 4. Electron shell
    $env:ELECTRON_RUN_AS_NODE = ""
    Get-Process "electron" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    $electronExe = Join-Path $root "node_modules\electron\dist\electron.exe"
    $electron    = $null
    if (Test-Path $electronExe) {
        if (-not (Wait-Http -url "http://localhost:$FrontendPort" -timeoutSeconds 15)) {
            Write-Host "Warning: frontend not ready - launching Electron anyway" -ForegroundColor Yellow
        }
        $electron = Start-Process -FilePath $electronExe `
            -ArgumentList "." `
            -WorkingDirectory $root `
            -PassThru
        Write-Host "Electron   pid=$($electron.Id)" -ForegroundColor DarkCyan
    } else {
        Write-Host "Electron   not found - open browser at $desktopUrl" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "OS running. Open: $desktopUrl" -ForegroundColor Cyan
    if ($serviceMode) {
        Write-Host "Services (Nous/Backend) are self-supervising. Close Electron to exit dev session." -ForegroundColor DarkGray
    } else {
        Write-Host "Press Ctrl+C to shut down all processes." -ForegroundColor DarkGray
    }
    Write-Host ""

    if ($serviceMode) {
        # In service mode, just wait for Electron to close.
        # Services keep running — they are OS services, not dev processes.
        if ($electron) {
            $electron.WaitForExit()
            Write-Host "Electron closed." -ForegroundColor DarkGray
        } else {
            Write-Host "No Electron window - press Enter to exit dev session." -ForegroundColor DarkGray
            Read-Host
        }
    } else {
        # In direct mode, supervise Nous and Backend and restart if they crash.
        Write-Host "Supervisor running. Close the Electron window to shut down." -ForegroundColor DarkGray

        while ($true) {
            Start-Sleep -Seconds 3

            if ($electron -and $electron.HasExited) {
                Write-Host "Electron closed - shutting down." -ForegroundColor DarkGray
                break
            }

            if ($nous -and $nous.HasExited) {
                Write-Host "Nous died (exit $($nous.ExitCode)) - restarting..." -ForegroundColor Yellow
                $nous = Start-Process -FilePath $nousExe `
                    -ArgumentList "--port", "$NousPort", "--model", $NousModel, "--genomeui", "http://localhost:$BackendPort" `
                    -WorkingDirectory $root `
                    -PassThru
                Write-Host "Nous       restarted pid=$($nous.Id)" -ForegroundColor DarkCyan
            }

            if ($backend -and $backend.HasExited) {
                Write-Host "Backend died (exit $($backend.ExitCode)) - restarting..." -ForegroundColor Yellow
                $backend = Start-Process -FilePath $venvPython `
                    -ArgumentList $backendArgs `
                    -WorkingDirectory $root `
                    -RedirectStandardOutput $backendOut `
                    -RedirectStandardError  $backendErr `
                    -NoNewWindow `
                    -PassThru
                Write-Host "Backend    restarted pid=$($backend.Id)" -ForegroundColor DarkCyan
            }
        }
    }
}
finally {
    Write-Host "Shutting down dev session..." -ForegroundColor DarkGray
    # In service mode, never kill Nous or Backend - they are OS services.
    # Only kill frontend and Electron which are dev-session processes.
    $devProcs = if ($serviceMode) {
        @($electron, $frontend)
    } else {
        @($electron, $frontend, $backend, $nous)
    }
    foreach ($proc in $devProcs) {
        if ($null -ne $proc) {
            try { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force } } catch {}
        }
    }
    if ($serviceMode) {
        Write-Host "Note: GenomeUI-Nous and GenomeUI-Backend are still running (OS services)." -ForegroundColor DarkGray
        Write-Host "      Stop-ScheduledTask -TaskName 'GenomeUI-Nous' to halt manually." -ForegroundColor DarkGray
    }
}
