<#
.SYNOPSIS
    Starts the Vibe-Trading committee_reporter.py loop at boot: it decides
    (and, for gold, conditionally trades) on a schedule and emails you the
    result every pass.

.DESCRIPTION
    Meant to be launched by Windows Task Scheduler at user log-on - see
    register-startup-task.ps1 in this same folder. Can also be run by hand
    for testing.

    Only one process runs here on purpose: committee_reporter.py is both the
    decision-maker and the notifier, so there's exactly one place checking
    positions and placing orders (see the docstring in committee_reporter.py
    for why running the same instrument through the server's
    /scheduled-runs executor too would be a race-condition risk).

    Does NOT touch MT5 or any broker credential. Start MT5 and log into your
    demo account yourself (Tools > Options > "Start automatically" + remember
    login in the terminal). The reporter fails closed and logs an error if
    MT5/the LLM provider/etc. aren't ready - it doesn't crash the loop.

    The process is detached (Start-Process) and logs to scripts\..\logs\, so
    a boot-time failure is visible instead of silent.
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $RepoRoot ".venv\Scripts"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-StartupLog($Message) {
    Add-Content -Path (Join-Path $LogDir "startup.log") -Value "$(Get-Date -Format o) $Message"
}

# Start-Process -RedirectStandardOutput/-Error TRUNCATES the target file on
# every launch, so a plain relaunch on each reboot silently threw away the
# previous session's reporter.log/reporter.err.log. Archive whatever's there
# into an accumulating history file first, so nothing is lost across restarts
# - reporter.log/err.log stay as "this session only" (committee_reporter.py's
# own --status tail-parsing depends on that), while reporter.history.log
# keeps the full record.
$HistoryLog = Join-Path $LogDir "reporter.history.log"

function Archive-PreviousLog($SourcePath, $Label) {
    if (Test-Path $SourcePath) {
        $content = Get-Content -Path $SourcePath -Raw -ErrorAction SilentlyContinue
        if ($content) {
            Add-Content -Path $HistoryLog -Value "===== $Label — boot $(Get-Date -Format o) ====="
            Add-Content -Path $HistoryLog -Value $content
        }
    }
}
Archive-PreviousLog (Join-Path $LogDir "reporter.log") "stdout"
Archive-PreviousLog (Join-Path $LogDir "reporter.err.log") "stderr"

# Cap the history file so it doesn't grow forever - keep at most one rotated
# backup (reporter.history.log.old) once the live file passes 10MB.
$MaxHistoryBytes = 10MB
if ((Test-Path $HistoryLog) -and (Get-Item $HistoryLog).Length -gt $MaxHistoryBytes) {
    Move-Item -Path $HistoryLog -Destination (Join-Path $LogDir "reporter.history.log.old") -Force
    Write-StartupLog "rotated reporter.history.log (exceeded ${MaxHistoryBytes} bytes)"
}

# A single committee run can take a long time (multi-agent debate + real data
# fetches); a short interval risks overlapping runs and burns LLM API spend
# for no benefit. Adjust freely, but keep it generous.
#
# 2 hours (was 3): shortened to raise the live gold target's opportunity count
# toward ~10 trades/week now that a live mandate is committed, without going
# all the way to the 1-hour cadence that would ~3x the DeepSeek spend the
# 3-hour interval was specifically chosen to control. Tighten further only
# after confirming the actual trade rate and API cost at this interval.
$ReporterIntervalSeconds = 2 * 60 * 60   # 2 hours

Start-Process -FilePath (Join-Path $Venv "python.exe") `
    -ArgumentList @(
        (Join-Path $RepoRoot "scripts\committee_reporter.py"),
        "--loop",
        "--interval", "$ReporterIntervalSeconds"
    ) `
    -WorkingDirectory $RepoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "reporter.log") `
    -RedirectStandardError (Join-Path $LogDir "reporter.err.log")
Write-StartupLog "launched committee_reporter.py --loop (every ${ReporterIntervalSeconds}s)"

Write-StartupLog "startup.ps1 finished"
