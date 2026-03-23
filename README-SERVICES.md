# GenomeUI Services

GenomeUI can run its backend and Nous gateway as Windows Scheduled Tasks so they survive terminal and Electron shutdowns.

## Install

From the repo root:

```powershell
powershell -NoLogo -ExecutionPolicy Bypass -File .\scripts\install-services.ps1
```

Optional flags:

```powershell
powershell -NoLogo -ExecutionPolicy Bypass -File .\scripts\install-services.ps1 -NousModel qwen2.5:0.5b -BackendPort 8787 -NousPort 7700
```

Before installing:

- build the Nous gateway so `nous-server.exe` exists under a repo-local or sibling `nous/` Rust target directory
- create the Python virtualenv with `powershell -NoLogo -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Bootstrap`

The installer registers two logon-triggered tasks:

- `GenomeUI-Nous`
- `GenomeUI-Backend`

Both tasks run as the current user, request elevated run level, and use a restart policy of 10 retries with a 1 second interval.

## Verify

Check task registration and HTTP health:

```powershell
powershell -NoLogo -ExecutionPolicy Bypass -File .\scripts\service-status.ps1
```

You can also inspect them directly:

```powershell
Get-ScheduledTask -TaskName 'GenomeUI-*' | Select-Object TaskName, State
```

Start them manually if needed:

```powershell
Start-ScheduledTask -TaskName 'GenomeUI-Nous'
Start-ScheduledTask -TaskName 'GenomeUI-Backend'
```

## Use In Dev

Once installed, launch the normal dev entrypoint:

```powershell
powershell -NoLogo -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

`dev.ps1` will detect the scheduled tasks, ensure both services are healthy, then start Vite and Electron without killing the backend or Nous when the dev session ends.

## Uninstall

Remove both tasks cleanly:

```powershell
powershell -NoLogo -ExecutionPolicy Bypass -File .\scripts\uninstall-services.ps1
```

If they are running, the uninstall script stops them first and then unregisters them.
