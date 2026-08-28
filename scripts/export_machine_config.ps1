<#
.SYNOPSIS
    Gathers everything this machine has beyond the git repo -- secrets,
    the committed live mandate, consent records, and learning history --
    into one folder so it can be moved to a second computer as a standby.

.DESCRIPTION
    None of this lives in git (see .gitignore: .env, CLAUDE.md, .claude/*).
    It's real machine state: an LLM API key + SMTP password, the live
    trading mandate that the enforcement gate requires to allow any order,
    and the trade journal / excursion history the committee reads back
    into its own prompts.

    This script only COPIES files to -Destination. It does not transmit
    anything anywhere. Move the resulting folder to the second machine
    yourself via a USB drive or another offline/physical channel -- not
    email, Slack, or a cloud sync folder -- since it contains live
    credentials and a file that authorizes real orders.

.EXAMPLE
    .\export_machine_config.ps1 -Destination E:\vibe-trading-transfer
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VibeHome = Join-Path $env:USERPROFILE ".vibe-trading"
$ClaudeMemDir = Join-Path $env:USERPROFILE ".claude\projects\C--Users-Spectre-Documents-Vibe-Trading\memory"

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

function Copy-IfExists($From, $To) {
    if (Test-Path $From) {
        $ToDir = Split-Path -Parent $To
        New-Item -ItemType Directory -Force -Path $ToDir | Out-Null
        Copy-Item -Path $From -Destination $To -Recurse -Force
        Write-Host "  copied: $From"
    } else {
        Write-Host "  skip (not found): $From" -ForegroundColor DarkGray
    }
}

Write-Host "Exporting to $Destination ..."

# --- Secrets: LLM key + SMTP creds ---
Copy-IfExists "$VibeHome\.env" "$Destination\vibe-trading\.env"

# --- Live trading state: mandate, consent records, per-day trade counter ---
Copy-IfExists "$VibeHome\live\mt5\mandate.json"       "$Destination\vibe-trading\live\mt5\mandate.json"
Copy-IfExists "$VibeHome\live\mt5\trade_counter.json" "$Destination\vibe-trading\live\mt5\trade_counter.json"
Copy-IfExists "$VibeHome\live\mt5\consent"            "$Destination\vibe-trading\live\mt5\consent"
Copy-IfExists "$VibeHome\trading-connections.json"    "$Destination\vibe-trading\trading-connections.json"

# --- Agent's own memory (separate from Claude Code's memory) ---
Copy-IfExists "$VibeHome\memory" "$Destination\vibe-trading\memory"

# --- Trade journal + circuit-breaker baseline (repo-local, gitignored-equivalent) ---
Copy-IfExists "$RepoRoot\logs\trade_journal.json" "$Destination\repo-logs\trade_journal.json"
Copy-IfExists "$RepoRoot\logs\live_baseline.json" "$Destination\repo-logs\live_baseline.json"

# --- Claude Code's own project memory for this working directory ---
Copy-IfExists $ClaudeMemDir "$Destination\claude-code-memory"

Write-Host ""
Write-Host "Done. Contents of ${Destination}:" -ForegroundColor Green
Get-ChildItem -Recurse -Path $Destination | Select-Object FullName

Write-Host ""
Write-Host "SECURITY: this folder contains a live LLM API key, SMTP password, and" -ForegroundColor Yellow
Write-Host "a mandate file that authorizes real MT5 orders. Move it to the other" -ForegroundColor Yellow
Write-Host "machine via USB or another offline channel, then delete it from here" -ForegroundColor Yellow
Write-Host "and from the USB drive once import_machine_config.ps1 succeeds there." -ForegroundColor Yellow
