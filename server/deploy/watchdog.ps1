# Lumi watchdog: restarts the server when it is running but not answering (a hung start, a stalled loop). A server
# that exits is restarted by the `lumitv` scheduled task already (its RestartCount). Run every 2 minutes by the
# `lumitv-watchdog` task (deploy/windows.md). Writes only to logs\watchdog.log and logs\watchdog.state; touches no
# process outside this install.
param(
    [string]$Url = 'http://127.0.0.1:8765/health',
    [int]$FailuresBeforeRestart = 3,   # consecutive failed checks, 2 minutes apart
    [int]$StartGraceMinutes = 8        # a start can take ~6 minutes on a busy PC: never restart a younger server
)
$ErrorActionPreference = 'Stop'
$server = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $server 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$state = Join-Path $logs 'watchdog.state'
$log = Join-Path $logs 'watchdog.log'
function Note([string]$msg) { Add-Content -Path $log -Value ("{0:s} {1}" -f (Get-Date), $msg) -Encoding ascii }  # no BOM (Windows PowerShell's utf8 writes one)

$ok = $false
try { $ok = (Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 10).Content -eq 'ok' } catch { }
if ($ok) { Set-Content -Path $state -Value 0 -Encoding ascii; exit 0 }

$failures = 0
if (Test-Path $state) { [int]::TryParse((Get-Content $state -Raw).Trim(), [ref]$failures) | Out-Null }
$failures++
Set-Content -Path $state -Value $failures -Encoding ascii

# this install only (its folder is in the command line; a second install on the same PC is left alone): the wrapper
# and the `lumitv serve` processes (the venv's launcher and Python). uv's own line lacks the folder, but it is the
# wrapper's child and exits with its Python child.
$inServer = [regex]::Escape($server)
$mine = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match $inServer -and
        ($_.CommandLine -match 'start-lumitv' -or ($_.CommandLine -match 'lumitv' -and $_.CommandLine -match '\bserve\b'))
})
$youngest = ($mine | ForEach-Object { $_.CreationDate } | Sort-Object -Descending | Select-Object -First 1)
if ($youngest -and ((Get-Date) - $youngest).TotalMinutes -lt $StartGraceMinutes) {
    Note "health failed ($failures), server started $([int]((Get-Date) - $youngest).TotalMinutes) min ago: waiting"
    exit 0
}
if ($failures -lt $FailuresBeforeRestart) { Note "health failed ($failures of $FailuresBeforeRestart)"; exit 0 }

Note "health failed $failures times: restarting ($($mine.Count) processes)"
foreach ($p in $mine) { taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null }
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName lumitv
Set-Content -Path $state -Value 0 -Encoding ascii
Note 'restarted'
