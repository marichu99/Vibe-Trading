<#
.SYNOPSIS
    Registers (or removes) the Windows Task Scheduler entry that runs the
    daily automated repair pass (run_daily_repair_agent.ps1 ->
    daily_repair_agent.py) once a day.

.DESCRIPTION
    Unlike register-startup-task.ps1/register-fundednext-startup-task.ps1
    (AtLogOn triggers for the two always-on trading loops), this is a
    -Daily trigger: it runs once at a fixed time regardless of log-on/
    reboot activity. 3:17 AM local by default -- overnight, off an exact
    hour on purpose (avoids being the one task woken at the same instant
    as every other :00-scheduled job on this machine), and well clear of
    either bot's own pass cadence so the daily_repair_agent.py worktree
    checkout never overlaps a live committee pass in time (not that it
    could collide -- see daily_repair_agent.py's module docstring for why
    the isolated worktree makes that a non-issue regardless).

.EXAMPLE
    .\register-daily-repair-task.ps1
    Installs the task at the default time (3:17 AM).

.EXAMPLE
    .\register-daily-repair-task.ps1 -At "04:30"
    Installs it at a custom time instead.

.EXAMPLE
    .\register-daily-repair-task.ps1 -Unregister
    Removes it.
#>

param(
    [string]$At = "03:17",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "DailyRepairAgent"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$ScriptPath = Join-Path $PSScriptRoot "run_daily_repair_agent.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

$Trigger = New-ScheduledTaskTrigger -Daily -At $At

# StartWhenAvailable: if the machine is asleep/off at 3:17 AM, run as soon
# as it's next available instead of silently skipping the day entirely.
# No RestartCount here (unlike the two loop tasks) -- a failed repair pass
# should surface in the log for a human to look at, not silently retry
# against what might be the same underlying problem.
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description "Runs one daily automated repair pass over the repo (propose-only, opens a PR) -- see scripts/daily_repair_agent.py." `
        -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host "FAILED to register scheduled task '$TaskName': $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "This usually means the current PowerShell session isn't elevated. Re-run this script from:"
    Write-Host "  an Administrator PowerShell (right-click PowerShell -> Run as administrator), then:"
    Write-Host "  cd '$PSScriptRoot'; .\register-daily-repair-task.ps1"
    exit 1
}

$Verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Verify) {
    Write-Host "Register-ScheduledTask returned no error, but the task is not present on re-check. Something is wrong  -  check Task Scheduler manually." -ForegroundColor Red
    exit 1
}

Write-Host "Registered scheduled task '$TaskName'  -  it will run daily at $At from now on."
Write-Host "To test it immediately: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it: .\register-daily-repair-task.ps1 -Unregister"
Write-Host "Log: logs\daily_repair_agent.log"
