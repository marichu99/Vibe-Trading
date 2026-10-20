<#
.SYNOPSIS
    Registers (or removes) the scheduled task that runs watchdog.ps1 every
    15 minutes in your user session.

.EXAMPLE
    .\register-watchdog-task.ps1
.EXAMPLE
    .\register-watchdog-task.ps1 -Unregister
#>

param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$TaskName = "VibeTradingWatchdog"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$ScriptPath = Join-Path $PSScriptRoot "watchdog.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

# Every 15 minutes, starting 5 minutes from registration, for ~10 years.
# Runs in the interactive user session (the default principal) because the
# reporters need the logged-in desktop session that the MT5 terminals live in.
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Restarts a crashed Vibe-Trading reporter (Exness/FundedNext) and emails an alert." `
    -Force | Out-Null

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Registration reported no error but the task is missing -- check Task Scheduler." -ForegroundColor Red
    exit 1
}
Write-Host "Registered '$TaskName' -- runs every 15 minutes. Log: logs\watchdog.log"
