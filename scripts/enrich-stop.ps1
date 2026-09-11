# Stop the detached enrichment. Safe at any point: the pass commits every 5
# schools, so the next run resumes rather than repeats.
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*enrich-chunked*' -or $_.CommandLine -like '*cli enrich*' } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; "stopped $($_.ProcessId)" }
        catch { "already gone $($_.ProcessId)" }
    }
