<#
.SYNOPSIS
    Places a folder produced by export_machine_config.ps1 into the correct
    locations on this (second) machine.

.DESCRIPTION
    Run this AFTER: cloning the repo, creating the venv, and installing
    MetaTrader5 + logging into the same account. This script only restores
    files -- it does not install anything, does not touch Task Scheduler,
    and does not start the trading loop. That's deliberate: this machine
    is meant to come up as a STANDBY, not a second active trader.

.EXAMPLE
    .\import_machine_config.ps1 -Source E:\vibe-trading-transfer
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Source
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VibeHome = Join-Path $env:USERPROFILE ".vibe-trading"
$ClaudeMemDir = Join-Path $env:USERPROFILE ".claude\projects\C--Users-Spectre-Documents-Vibe-Trading\memory"

function Restore-IfExists($From, $To) {
    if (Test-Path $From) {
        $ToDir = Split-Path -Parent $To
        New-Item -ItemType Directory -Force -Path $ToDir | Out-Null
        Copy-Item -Path $From -Destination $To -Recurse -Force
        Write-Host "  restored: $To"
    } else {
        Write-Host "  skip (not in transfer folder): $From" -ForegroundColor DarkGray
    }
}

Write-Host "Importing from $Source ..."

Restore-IfExists "$Source\vibe-trading\.env"                       "$VibeHome\.env"
Restore-IfExists "$Source\vibe-trading\live\mt5\mandate.json"       "$VibeHome\live\mt5\mandate.json"
Restore-IfExists "$Source\vibe-trading\live\mt5\trade_counter.json" "$VibeHome\live\mt5\trade_counter.json"
Restore-IfExists "$Source\vibe-trading\live\mt5\consent"            "$VibeHome\live\mt5\consent"
Restore-IfExists "$Source\vibe-trading\trading-connections.json"    "$VibeHome\trading-connections.json"
Restore-IfExists "$Source\vibe-trading\memory"                      "$VibeHome\memory"
Restore-IfExists "$Source\repo-logs\trade_journal.json"             "$RepoRoot\logs\trade_journal.json"
Restore-IfExists "$Source\repo-logs\live_baseline.json"             "$RepoRoot\logs\live_baseline.json"
Restore-IfExists $Source\claude-code-memory                         $ClaudeMemDir

Write-Host ""
Write-Host "Done restoring files." -ForegroundColor Green
Write-Host ""
Write-Host "This machine is now a STANDBY: config is in place but nothing runs" -ForegroundColor Yellow
Write-Host "automatically. Do NOT run register-startup-task.ps1 (or start the loop" -ForegroundColor Yellow
Write-Host "by hand) while the primary machine's loop is still active -- that would" -ForegroundColor Yellow
Write-Host "put two independent decision-makers on the same MT5 account at once." -ForegroundColor Yellow
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Verify read-only:  .venv\Scripts\python.exe scripts\committee_reporter.py --status"
Write-Host "  2. Delete the transfer folder ($Source) and the USB copy -- it has live credentials."
Write-Host "  3. Only if/when you actually fail over: stop the primary machine's loop first,"
Write-Host "     then run register-startup-task.ps1 here (or start.ps1 by hand)."
