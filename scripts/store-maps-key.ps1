param(
    [Parameter(Mandatory=$true)]
    [string]$ApiKey
)

$root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$dotEnv  = Join-Path $root ".env"

# Read existing .env (if any), strip any old GOOGLE_MAPS_API_KEY line
$lines = @()
if (Test-Path $dotEnv) {
    $lines = Get-Content $dotEnv | Where-Object { $_ -notmatch '^GOOGLE_MAPS_API_KEY=' }
}
$lines += "GOOGLE_MAPS_API_KEY=$ApiKey"
$lines | Set-Content $dotEnv -Encoding UTF8

Write-Host "Stored GOOGLE_MAPS_API_KEY in $dotEnv" -ForegroundColor Green
Write-Host "Restart the dev session for the key to take effect." -ForegroundColor DarkGray
