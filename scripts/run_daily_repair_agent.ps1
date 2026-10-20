<#
.SYNOPSIS
    Runs one daily automated repair pass (scripts/daily_repair_agent.py) --
    see that file's module docstring for the full safety design.

.DESCRIPTION
    Meant to be launched by Windows Task Scheduler once a day -- see
    register-daily-repair-task.ps1 in this same folder. Unlike startup.ps1/
    startup_fundednext.ps1, this is NOT a background loop: it runs once,
    to completion, and exits (Task Scheduler owns the daily cadence). Task
    Scheduler already runs this non-interactively and captures nothing by
    default, so this script's own log file (not console output) is the
    thing to check afterward.
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $RepoRoot ".venv\Scripts"

& (Join-Path $Venv "python.exe") (Join-Path $RepoRoot "scripts\daily_repair_agent.py")
exit $LASTEXITCODE
