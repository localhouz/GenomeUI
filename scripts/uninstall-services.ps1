# GenomeUI — Remove Windows Scheduled Task services
#
# Stops and unregisters GenomeUI-Nous and GenomeUI-Backend.
# Run before switching machines or reinstalling.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "GenomeUI OS Service Uninstaller" -ForegroundColor Yellow
Write-Host ""

foreach ($name in @("GenomeUI-Nous", "GenomeUI-Backend")) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "  $name  not found (skipping)" -ForegroundColor DarkGray
        continue
    }

    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Write-Host "  $name  stopped" -ForegroundColor DarkGray
    }

    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "  $name  removed" -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Services will no longer start at logon." -ForegroundColor Cyan
Write-Host ""
