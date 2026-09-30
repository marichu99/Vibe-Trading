<#
.SYNOPSIS
    Restarts a crashed trading reporter and emails an alert. Run every 15
    minutes by the "VibeTradingWatchdog" scheduled task (see
    register-watchdog-task.ps1).

.DESCRIPTION
    Added 2026-09-30. The reporters were only ever (re)started by the
    log-on tasks, so a reporter that died mid-day stayed down, silently,
    until someone noticed. For each reporter this checks for a running
    python process whose command line names its script. A running reporter
    is never touched (even mid-committee-run); a missing one is relaunched
    through its normal startup script -- which also archives the crashed
    session's logs into the history log -- and an email alert is sent with
    the tail of its error log. The reporters' own lock files still prevent
    a duplicate if this races a manual restart.
#>

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot "logs"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-WatchdogLog($Message) {
    Add-Content -Path (Join-Path $LogDir "watchdog.log") -Value "$(Get-Date -Format o) $Message"
}

$Reporters = @(
    @{ Name = "Exness";     Script = "committee_reporter.py";  Startup = "startup.ps1";            ErrLog = "reporter.err.log" },
    @{ Name = "FundedNext"; Script = "fundednext_reporter.py"; Startup = "startup_fundednext.ps1"; ErrLog = "fundednext_reporter.err.log" }
)

$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    ForEach-Object { $_.CommandLine }

foreach ($r in $Reporters) {
    $alive = $running | Where-Object { $_ -and $_ -match [regex]::Escape($r.Script) }
    if ($alive) { continue }

    $errTail = ""
    $errPath = Join-Path $LogDir $r.ErrLog
    if (Test-Path $errPath) {
        $errTail = (Get-Content -Path $errPath -Tail 25 -ErrorAction SilentlyContinue) -join "`n"
    }
    Write-WatchdogLog "$($r.Name) reporter ($($r.Script)) not running -- restarting via $($r.Startup)"
    try {
        & (Join-Path $PSScriptRoot $r.Startup)
        Write-WatchdogLog "$($r.Name) restart launched"
        $outcome = "restarted"
    } catch {
        Write-WatchdogLog "$($r.Name) restart FAILED: $($_.Exception.Message)"
        $outcome = "restart FAILED: $($_.Exception.Message)"
    }

    $body = "The watchdog found the $($r.Name) reporter ($($r.Script)) not running at $(Get-Date -Format o) and $outcome.`n`n" +
            "Last lines of its error log before the restart:`n$errTail"
    $env:WATCHDOG_SUBJECT = "[Vibe-Trading] WATCHDOG: $($r.Name) reporter was down -- $outcome"
    $env:WATCHDOG_BODY = $body
    $py = @'
import os, sys
sys.path.insert(0, "scripts"); sys.path.insert(0, "agent")
from src.providers.llm import _ensure_dotenv
_ensure_dotenv()
from committee_reporter import send_email
send_email(os.environ["WATCHDOG_SUBJECT"], os.environ["WATCHDOG_BODY"])
'@
    Push-Location $RepoRoot
    try {
        $py | & $Python - 2>&1 | Out-Null
        Write-WatchdogLog "$($r.Name) alert email sent"
    } catch {
        Write-WatchdogLog "$($r.Name) alert email FAILED: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }
}
