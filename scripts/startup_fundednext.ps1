<#
.SYNOPSIS
    Starts the FundedNext committee_reporter loop at boot: fundednext_reporter.py,
    the FundedNext-account sibling of startup.ps1/committee_reporter.py.

.DESCRIPTION
    Meant to run on its OWN machine/VPS, signed into the FundedNext MT5
    terminal (mt5fn-live-trade profile) -- NOT the same box as the Exness
    live account's startup.ps1/committee_reporter.py. See
    scripts/fundednext_reporter.py's own docstring and
    C:\Users\Hp\.claude\plans\calm-wondering-snail.md for the full design.

    Before running this for the first time on a fresh VPS:
      1. Install Python 3.11 x64 and MetaTrader 5 (the desktop terminal),
         sign into the FundedNext account, enable "Allow algorithmic trading"
         (Tools > Options > Expert Advisors).
      2. Create the venv and install deps (see the plan doc / this repo's
         README for the exact pip commands) -- this script assumes
         <repo>\.venv\Scripts\python.exe already exists.
      3. Copy agent\.env.example to agent\.env and fill in DEEPSEEK_API_KEY
         (or your LLM provider), SMTP_*/EMAIL_* for reports, and
         FINNHUB_API_KEY for the news-blackout guard.
      4. Fill in ACCOUNT_REF in scripts\commit_fundednext_mandate.py with the
         real FundedNext MT5 login, then run it once (human-run, on purpose
         -- see that script's docstring) to commit the mandate.
      5. Verify scripts\fundednext_reporter.py's TARGETS symbol names
         (EURUSDm/AUDUSDm) actually match what FundedNext's own MT5 server
         calls them -- the "m" suffix is an Exness convention, not universal.
      6. Dry-run once by hand first: .venv\Scripts\python.exe
         scripts\fundednext_reporter.py --once -- confirm no exceptions and
         the emailed report arrives, before registering this for autostart.

    Does NOT touch MT5 or any broker credential itself -- start MT5 and log
    into the FundedNext account yourself (Tools > Options > "Start
    automatically" + remember login in the terminal).

    The process is detached (Start-Process) and logs to scripts\..\logs\, so
    a boot-time failure is visible instead of silent.
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $RepoRoot ".venv\Scripts"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-StartupLog($Message) {
    Add-Content -Path (Join-Path $LogDir "fundednext_startup.log") -Value "$(Get-Date -Format o) $Message"
}

# Same truncate-on-relaunch problem startup.ps1 solves for the Exness
# reporter -- see that script's comment for the full rationale. Own history
# file (fundednext_reporter.history.log), never shares committee_reporter's.
$HistoryLog = Join-Path $LogDir "fundednext_reporter.history.log"

function Archive-PreviousLog($SourcePath, $Label) {
    if (Test-Path $SourcePath) {
        $content = Get-Content -Path $SourcePath -Raw -ErrorAction SilentlyContinue
        if ($content) {
            Add-Content -Path $HistoryLog -Value "===== $Label — boot $(Get-Date -Format o) ====="
            Add-Content -Path $HistoryLog -Value $content
        }
    }
}
Archive-PreviousLog (Join-Path $LogDir "fundednext_reporter.log") "stdout"
Archive-PreviousLog (Join-Path $LogDir "fundednext_reporter.err.log") "stderr"

$MaxHistoryBytes = 10MB
if ((Test-Path $HistoryLog) -and (Get-Item $HistoryLog).Length -gt $MaxHistoryBytes) {
    Move-Item -Path $HistoryLog -Destination (Join-Path $LogDir "fundednext_reporter.history.log.old") -Force
    Write-StartupLog "rotated fundednext_reporter.history.log (exceeded ${MaxHistoryBytes} bytes)"
}

# Same 2-hour cadence as the Exness reporter -- see startup.ps1's comment on
# why a short interval is a bad idea (overlapping runs, LLM spend). Tighten
# only after confirming actual trade rate/cost at this interval.
$ReporterIntervalSeconds = 2 * 60 * 60   # 2 hours

Start-Process -FilePath (Join-Path $Venv "python.exe") `
    -ArgumentList @(
        (Join-Path $RepoRoot "scripts\fundednext_reporter.py"),
        "--loop",
        "--interval", "$ReporterIntervalSeconds"
    ) `
    -WorkingDirectory $RepoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "fundednext_reporter.log") `
    -RedirectStandardError (Join-Path $LogDir "fundednext_reporter.err.log")
Write-StartupLog "launched fundednext_reporter.py --loop (every ${ReporterIntervalSeconds}s)"

Write-StartupLog "startup_fundednext.ps1 finished"
