<#
.SYNOPSIS
    Registers (or removes) the Windows Task Scheduler entry that runs
    startup.ps1 automatically at your log-on.

.DESCRIPTION
    This is the supported, safe mechanism for "run something at boot" on
    Windows  -  it does NOT touch the bootloader, BCD, or any boot-time system
    file. It just registers a normal scheduled task tied to your user
    session, exactly like any startup program.

.EXAMPLE
    .\register-startup-task.ps1
    Installs the task.

.EXAMPLE
    .\register-startup-task.ps1 -Unregister
    Removes it  -  also stop the running processes by hand afterward if you
    want an immediate stop (Get-Process vibe-trading,python | Stop-Process),
    since unregistering only prevents future log-ons from starting it.
#>

param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$TaskName = "VibeTradingAutostart"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'. Existing running processes are untouched  -  stop them with:"
    Write-Host "  Get-Process vibe-trading,python -ErrorAction SilentlyContinue | Stop-Process"
    return
}

$ScriptPath = Join-Path $PSScriptRoot "startup.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

# 2026-09-02: an AtLogOn trigger firing a hidden PowerShell console the
# instant the session starts is a known Windows race -- the session/
# console teardown that happens during early logon can send the fresh
# process a console-close signal, killing it with STATUS_CONTROL_C_EXIT
# (0xC000013A) before it executes a single line. Confirmed this exact
# symptom in the wild: LastTaskResult 0xC000013A with zero corresponding
# entry in startup.log, i.e. it died before even the first Write-StartupLog
# call. A short logon delay lets the session finish settling first.
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Trigger.Delay = "PT45S"

# Belt-and-suspenders: if it still dies (any reason), have Task Scheduler
# itself retry a few times rather than silently leaving the live-trading
# loop down for hours until someone notices.
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)

try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description "Starts the Vibe-Trading server+scheduler and committee_reporter.py email loop at log-on." `
        -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host "FAILED to register scheduled task '$TaskName': $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "This usually means the current PowerShell session isn't elevated. Re-run this script from:"
    Write-Host "  an Administrator PowerShell (right-click PowerShell -> Run as administrator), then:"
    Write-Host "  cd '$PSScriptRoot'; .\register-startup-task.ps1"
    exit 1
}

$Verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Verify) {
    Write-Host "Register-ScheduledTask returned no error, but the task is not present on re-check. Something is wrong  -  check Task Scheduler manually." -ForegroundColor Red
    exit 1
}

Write-Host "Registered scheduled task '$TaskName'  -  it will run at every log-on from now on."
Write-Host "To test it immediately without rebooting: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it: .\register-startup-task.ps1 -Unregister"
