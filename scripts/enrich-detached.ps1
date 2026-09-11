# Launch the chunked enrichment fully outside Claude Code.
#
# ponytail: Start-Process, not a service or a scheduled task. The harness stops
# background tasks whenever the SYSTEM runs low on memory - not when this
# process misbehaves - so on a laptop with little headroom an in-harness run
# gets reaped every few chunks. Detaching removes the reaper from the loop; the
# script's own per-chunk recovery still handles a real OS kill.
#
# Progress: logs/enrich-<date>.log   Stop: scripts/enrich-stop.ps1
param([int]$Chunk = 4, [int]$Chunks = 400)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

$running = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*enrich-chunked*' }
if ($running) {
    Write-Host "Already running (PIDs: $($running.ProcessId -join ', ')). Stop it first."
    exit 1
}

$env:CHUNK = $Chunk
$env:CHUNKS = $Chunks

# Git\bin\bash.exe, NOT Git\usr\bin\bash.exe. The usr\bin one starts with no
# PATH to the git-bash utilities, so the script dies on its first `date` with
# "command not found" - and behind -WindowStyle Hidden that failure is totally
# silent. The wrapper in Git\bin sets PATH up first.
$bash = 'C:\Program Files\Git\bin\bash.exe'

# Never fail silently again: capture the launcher's own stderr and confirm the
# process is still alive before claiming it started.
$boot = 'logs/enrich-launcher.log'
$p = Start-Process -FilePath $bash -ArgumentList 'scripts/enrich-chunked.sh' `
        -WindowStyle Hidden -PassThru -RedirectStandardError $boot
Start-Sleep -Seconds 5
if ($p.HasExited) {
    Write-Host "FAILED to start (exit $($p.ExitCode)). Launcher stderr:"
    if (Test-Path $boot) { Get-Content $boot -Tail 20 }
    exit 1
}

Write-Host "Detached enrichment started. PID $($p.Id), CHUNK=$Chunk CHUNKS=$Chunks"
Write-Host "Watch:  Get-Content logs/enrich-$(Get-Date -f yyyyMMdd).log -Tail 20 -Wait"
