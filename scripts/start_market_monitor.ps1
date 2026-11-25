param(
    [ValidateRange(0.001, 24)] [double] $Hours = 8,
    [ValidateRange(1, 60)] [double] $PollSeconds = 15,
    [ValidatePattern('^[a-f0-9]{32}$')] [string] $DemoOnceAuthorization,
    [ValidatePattern('^[a-f0-9]{32}$')] [string] $DemoSeriesAuthorization
)

$ErrorActionPreference = 'Stop'
if ($DemoOnceAuthorization -and $DemoSeriesAuthorization) {
    throw 'Choose either one-entry or sequential authorization, not both.'
}
$quantRoot = Split-Path -Parent $PSScriptRoot
$quantPython = Join-Path $quantRoot '.venv\Scripts\python.exe'
$quantScript = Join-Path $PSScriptRoot 'monitor_market.py'
$quantMonitorFolder = Join-Path $quantRoot 'logs\monitor'
if (-not (Test-Path -LiteralPath $quantPython -PathType Leaf)) {
    throw "Project Python is missing: $quantPython"
}
New-Item -ItemType Directory -Path $quantMonitorFolder -Force | Out-Null
$quantStamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$quantStdout = Join-Path $quantMonitorFolder "console-$quantStamp.log"
$quantStderr = Join-Path $quantMonitorFolder "errors-$quantStamp.log"
$quantCulture = [Globalization.CultureInfo]::InvariantCulture
$quantRunId = [Guid]::NewGuid().ToString('N')
$quantArguments = '-u -B "{0}" --hours {1} --poll-seconds {2} --run-id {3}' -f $quantScript, $Hours.ToString($quantCulture), $PollSeconds.ToString($quantCulture), $quantRunId
if ($DemoOnceAuthorization) {
    $quantArguments += " --demo-once $DemoOnceAuthorization"
}
if ($DemoSeriesAuthorization) {
    $quantArguments += " --demo-series $DemoSeriesAuthorization"
}
$quantProcess = Start-Process -FilePath $quantPython -ArgumentList $quantArguments `
    -WorkingDirectory $quantRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $quantStdout -RedirectStandardError $quantStderr
$quantStatusFile = Join-Path $quantMonitorFolder 'status.json'
$quantWaitUntil = [DateTime]::UtcNow.AddSeconds(45)
while ([DateTime]::UtcNow -lt $quantWaitUntil) {
    $quantProcess.Refresh()
    if ($quantProcess.HasExited) {
        Write-Output "Monitor exited with code $($quantProcess.ExitCode)."
        Get-Content -LiteralPath $quantStderr
        Get-Content -LiteralPath $quantStdout
        exit 1
    }
    if (Test-Path -LiteralPath $quantStatusFile) {
        $quantStatus = Get-Content -LiteralPath $quantStatusFile -Raw | ConvertFrom-Json
        # Windows venv Python may create a worker with a different PID.
        # Match this launch's random ID, never another run's old status file.
        if ($quantStatus.run_id -eq $quantRunId -and $quantStatus.status -eq 'running') {
            Write-Output "Mode $($quantStatus.mode). Worker PID $($quantStatus.pid); deadline UTC $($quantStatus.deadline_utc)."
            if ($quantStatus.mode -eq 'demo_once') {
                Write-Output 'One DEMO entry is authorized if the strategy and broker checks pass. No retries.'
            } elseif ($quantStatus.mode -eq 'demo_series') {
                Write-Output 'Sequential DEMO entries authorized: fresh setups, one open position, verified exits, no retries.'
            } else {
                Write-Output 'No orders will be submitted automatically.'
            }
            Write-Output "Status: $quantStatusFile"
            Write-Output "Console: $quantStdout"
            exit 0
        }
    }
    Start-Sleep -Milliseconds 200
}
Write-Output "Startup is still pending for PID $($quantProcess.Id). Inspect --status and $quantStderr before starting another process."
exit 2
