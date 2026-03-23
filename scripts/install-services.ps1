# GenomeUI - Register OS Services as Windows Scheduled Tasks
#
# Registers GenomeUI-Nous and GenomeUI-Backend as logon-triggered tasks
# that auto-restart on crash. Run once per machine/user.
#
# Usage:
#   .\install-services.ps1
#   .\install-services.ps1 -NousModel qwen2.5:0.5b -BackendPort 8787 -NousPort 7700

param(
    [int]$BackendPort = 8787,
    [int]$NousPort = 7700,
    [string]$NousModel = "qwen2.5:0.5b"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

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

function Register-GenomeTask {
    param(
        [string]$TaskName,
        [object]$Action,
        [object]$Trigger,
        [object]$Settings,
        [object]$Principal,
        [string]$Description
    )

    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -Principal $Principal `
            -Description $Description `
            -Force | Out-Null
        Write-Host ("  {0,-18} registered" -f $TaskName) -ForegroundColor Green
    } catch {
        Write-Host ("  {0,-18} failed" -f $TaskName) -ForegroundColor Red
        throw
    }
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$nousExe = Resolve-NousBinary $root

if (-not $nousExe) {
    throw "Nous binary not found in repo-local or sibling nous/Nous rust targets.`nBuild the gateway first, then rerun this installer."
}
if (-not (Test-Path $venvPython)) {
    throw ".venv not found at $venvPython`nRun scripts/dev.ps1 -Bootstrap first."
}

Write-Host ""
Write-Host "GenomeUI OS Service Installer" -ForegroundColor Cyan
Write-Host "  Root:    $root"
Write-Host "  Nous:    $nousExe"
Write-Host "  Backend: $venvPython"
Write-Host "  Model:   $NousModel"
Write-Host ""

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Seconds 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

$trigger = New-ScheduledTaskTrigger -AtLogon -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

$nousAction = New-ScheduledTaskAction `
    -Execute $nousExe `
    -Argument "--port $NousPort --model $NousModel --genomeui http://localhost:$BackendPort" `
    -WorkingDirectory $root

Register-GenomeTask `
    -TaskName "GenomeUI-Nous" `
    -Action $nousAction `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "GenomeUI Nous AI gateway - starts at logon, auto-restarts on crash"

$backendAction = New-ScheduledTaskAction `
    -Execute $venvPython `
    -Argument "-m uvicorn backend.main:app --host 0.0.0.0 --port $BackendPort" `
    -WorkingDirectory $root

Register-GenomeTask `
    -TaskName "GenomeUI-Backend" `
    -Action $backendAction `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "GenomeUI backend API - starts at logon, auto-restarts on crash"

Write-Host ""
Write-Host "Services registered. Starting them now..." -ForegroundColor Cyan
Write-Host ""

foreach ($name in @("GenomeUI-Nous", "GenomeUI-Backend")) {
    $state = (Get-ScheduledTask -TaskName $name).State
    if ($state -ne "Running") {
        Start-ScheduledTask -TaskName $name
        Write-Host "  $name started" -ForegroundColor DarkCyan
    } else {
        Write-Host "  $name already running" -ForegroundColor DarkCyan
    }
}

Write-Host ""
Write-Host "Done. Services will auto-start at every logon." -ForegroundColor Green
Write-Host "To stop:      Stop-ScheduledTask -TaskName 'GenomeUI-Nous'"
Write-Host "To remove:    scripts\uninstall-services.ps1"
Write-Host "To check:     Get-ScheduledTask -TaskName 'GenomeUI-*' | Select TaskName,State"
Write-Host ""
