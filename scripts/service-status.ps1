param(
    [int]$BackendPort = 8787,
    [int]$NousPort = 7700
)

$ErrorActionPreference = "Stop"

function Get-TaskSummary([string]$TaskName, [string]$HealthUrl) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return [pscustomobject]@{
            TaskName = $TaskName
            Installed = $false
            State = "missing"
            Healthy = $false
            HealthUrl = $HealthUrl
        }
    }

    $healthy = $false
    try {
        $null = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 2
        $healthy = $true
    } catch {
        $healthy = $false
    }

    return [pscustomobject]@{
        TaskName = $TaskName
        Installed = $true
        State = $task.State
        Healthy = $healthy
        HealthUrl = $HealthUrl
    }
}

$rows = @(
    Get-TaskSummary "GenomeUI-Backend" "http://127.0.0.1:$BackendPort/api/health"
    Get-TaskSummary "GenomeUI-Nous" "http://127.0.0.1:$NousPort/health"
)

Write-Host ""
Write-Host "GenomeUI Service Status" -ForegroundColor Cyan
Write-Host ""
$rows | Format-Table TaskName, Installed, State, Healthy, HealthUrl -AutoSize
Write-Host ""
