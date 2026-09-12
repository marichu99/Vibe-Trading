<#
.SYNOPSIS
    Registers (or removes) the Windows Task Scheduler entry that runs
    startup_fundednext.ps1 automatically at log-on, on the FundedNext VPS.

.DESCRIPTION
    FundedNext-account sibling of register-startup-task.ps1 -- a distinct
    task name (FundedNextAutostart, not VibeTradingAutostart) so the two
    never collide if this repo is ever cloned onto the same box as the
    Exness live account for any reason.

.EXAMPLE
    .\register-fundednext-startup-task.ps1
    Installs the task.

.EXAMPLE
    .\register-fundednext-startup-task.ps1 -Unregister
    Removes it -- also stop the running process by hand afterward if you
    want an immediate stop (Get-Process python -ErrorAction SilentlyContinue
    | Stop-Process), since unregistering only prevents future log-ons from
    starting it.
#>

param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$TaskName = "FundedNextAutostart"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'. Existing running processes are untouched  -  stop them with:"
    Write-Host "  Get-Process python -ErrorAction SilentlyContinue | Stop-Process"
    return
}

$ScriptPath = Join-Path $PSScriptRoot "startup_fundednext.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

# Same AtLogOn race + settle delay as register-startup-task.ps1 -- see that
# script's comment for the full 0xC000013A rationale.
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Trigger.Delay = "PT45S"

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)

try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description "Starts the FundedNext committee_reporter (fundednext_reporter.py) email loop at log-on." `
        -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host "FAILED to register scheduled task '$TaskName': $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "This usually means the current PowerShell session isn't elevated. Re-run this script from:"
    Write-Host "  an Administrator PowerShell (right-click PowerShell -> Run as administrator), then:"
    Write-Host "  cd '$PSScriptRoot'; .\register-fundednext-startup-task.ps1"
    exit 1
}

$Verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Verify) {
    Write-Host "Register-ScheduledTask returned no error, but the task is not present on re-check. Something is wrong  -  check Task Scheduler manually." -ForegroundColor Red
    exit 1
}

Write-Host "Registered scheduled task '$TaskName'  -  it will run at every log-on from now on."
Write-Host "To test it immediately without rebooting: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it: .\register-fundednext-startup-task.ps1 -Unregister"
