Param(
    [switch]$SkipUi,
    [switch]$SkipTauri
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

Step "Build web app"
npm run build | Out-Host

Step "Run Python unit tests"
npm run os:test:unit | Out-Host

Step "Run Electron module tests"
npm run os:test:electron | Out-Host

if (-not $SkipUi) {
    Step "Run Playwright smoke tests"
    npx playwright test tests/ui/webdeck-scene.spec.js tests/ui/welcome-boot.spec.js tests/ui/shopping-direct.spec.js | Out-Host
}

if (-not $SkipTauri) {
    Step "Run Tauri compile check"
    cargo check --manifest-path src-tauri/Cargo.toml | Out-Host
}

Write-Host ""
Write-Host "Local CI parity checks passed." -ForegroundColor Green
