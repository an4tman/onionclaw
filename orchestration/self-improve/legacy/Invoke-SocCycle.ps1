<#
.SYNOPSIS
  Phase 1 autonomous SOC triage cycle — ON-DEMAND, read-only.

.DESCRIPTION
  Runs `claude -p` headless on the workstation (the operator's Claude subscription auth) with the
  soc-analyst skill + the two READ-ONLY MCPs (elasticsearch, so_gateway), driven by the
  fixed prompt soc-cycle.prompt.md. Writes a timestamped report, appends a dated KB
  cycle-note + one prioritized backlog line, then path-scoped commits.

  STRICTLY READ-ONLY against Security Onion and all live systems. No Task Scheduler.
  No Discord post (Send-Discord is a stub). No live-infra / tuning changes.

.NOTES
  MCP mechanism: `elasticsearch` and `so_gateway` are USER-SCOPED MCP servers
  (claude mcp list / `claude mcp get <name>` -> "Scope: User config"). Headless `claude -p`
  INHERITS user-scoped MCPs automatically — no --mcp-config / .mcp.json needed. We do NOT
  pass --strict-mcp-config (that would strip the inherited servers). Tool access is scoped
  with --allowedTools to the two read-only MCP namespaces plus read-only local tools.
#>

[CmdletBinding()]
param(
  # Skip the git commit (dry run of the analysis + file writes only).
  [switch]$NoCommit,
  # Mark this run's notes/backlog as an initial TEST cycle.
  [switch]$TestCycle,
  # Override the model (default: opus alias).
  [string]$Model = 'opus',
  # Max minutes to allow the headless run.
  [int]$TimeoutMinutes = 25
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- Paths (resolve repo root from this script; portable across clones) ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path        # .../soc-agent
$RepoRoot  = Split-Path -Parent $ScriptDir                          # ...the parent repo root
$PromptFile   = Join-Path $ScriptDir 'soc-cycle.prompt.md'
$ReportsDir   = Join-Path $ScriptDir 'reports'
$BacklogFile  = Join-Path $ScriptDir 'backlog.md'
$CycleNoteDir = Join-Path $RepoRoot 'kb\security\cycle-notes'

if (-not (Test-Path $PromptFile)) { throw "Prompt file missing: $PromptFile" }
New-Item -ItemType Directory -Force $ReportsDir   | Out-Null
New-Item -ItemType Directory -Force $CycleNoteDir | Out-Null

$Stamp      = Get-Date -Format 'yyyyMMdd-HHmm'
$DateOnly   = Get-Date -Format 'yyyy-MM-dd'
$ReportFile = Join-Path $ReportsDir "soc-$Stamp.md"
$RunLabel   = if ($TestCycle) { 'TEST / initial cycle' } else { 'cycle' }

# Read-only MCP tool namespaces + read-only local tools. claude.exe accepts wildcards here.
$AllowedTools = @(
  'mcp__elasticsearch__list_indices','mcp__elasticsearch__get_mappings',
  'mcp__elasticsearch__get_shards','mcp__elasticsearch__search','mcp__elasticsearch__esql',
  'mcp__so_gateway__ping','mcp__so_gateway__get_detection','mcp__so_gateway__get_playbook',
  'mcp__so_gateway__run_guided_analysis',
  'Read','Grep','Glob','Skill'
) -join ','

# --------------------------------------------------------------------------------------
# Send-Discord — STUB / no-op. TODO(operator): wire the Discord webhook URL (Phase 2).
# Do NOT post anything until the operator provides the webhook + approves posting.
# --------------------------------------------------------------------------------------
function Send-Discord {
  param([string]$Markdown, [string]$Title = 'SOC cycle')
  # TODO(Phase 2): replace stub with a real webhook POST once $DiscordWebhookUrl is set.
  #   Invoke-RestMethod -Uri $env:SOC_DISCORD_WEBHOOK -Method Post -ContentType 'application/json' `
  #     -Body (@{ content = "**$Title**`n$Markdown" } | ConvertTo-Json -Depth 4)
  Write-Host "[Send-Discord STUB] Would post '$Title' ($($Markdown.Length) chars). No-op until webhook configured." -ForegroundColor DarkYellow
}

Write-Host "=== SOC triage cycle ($RunLabel) — $Stamp ===" -ForegroundColor Cyan
Write-Host "Repo:   $RepoRoot"
Write-Host "Report: $ReportFile"
Write-Host "Model:  $Model   (read-only; on-demand; no Discord post)"

# --- Build the run prompt: fixed prompt file + a short run header ---
$PromptBody = Get-Content -Raw -LiteralPath $PromptFile
$RunHeader  = @"
RUN CONTEXT: This is a $RunLabel, executed on-demand on the operator workstation at $Stamp local time.
Window: last ~24h. Read-only. Produce the 4-section bounded report exactly as specified below.

"@
$FullPrompt = $RunHeader + $PromptBody

Write-Host "`nInvoking claude -p (headless)..." -ForegroundColor Cyan
$TimeoutMs = $TimeoutMinutes * 60 * 1000

# claude -p reads the prompt as an argument. Redirect stdin from $null to skip the 3s
# "no stdin" wait. Inherit user-scoped MCPs (no --strict-mcp-config). acceptEdits perm mode
# so the allowed read-only tools run without interactive prompts.
$claudeExe = (Get-Command claude -ErrorAction Stop).Source
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName  = $claudeExe
foreach ($a in @(
    '-p', '--model', $Model,
    '--allowedTools', $AllowedTools,
    '--permission-mode', 'acceptEdits',
    '--add-dir', $RepoRoot
)) { $psi.ArgumentList.Add($a) }
$psi.RedirectStandardInput  = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true
$psi.UseShellExecute = $false
$psi.WorkingDirectory = $RepoRoot

$proc = [System.Diagnostics.Process]::Start($psi)
# Send the prompt on stdin (more robust than a giant argv on Windows) then close it.
$proc.StandardInput.Write($FullPrompt)
$proc.StandardInput.Close()
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()
if (-not $proc.WaitForExit($TimeoutMs)) {
  try { $proc.Kill($true) } catch {}
  throw "claude -p exceeded ${TimeoutMinutes}m timeout."
}
$Report = $stdoutTask.Result
$ErrText = $stderrTask.Result
$ExitCode = $proc.ExitCode

if ($ExitCode -ne 0) {
  Write-Host "claude -p exited $ExitCode" -ForegroundColor Red
  if ($ErrText) { Write-Host "STDERR:`n$ErrText" -ForegroundColor Red }
  throw "Headless run failed (exit $ExitCode)."
}
if ([string]::IsNullOrWhiteSpace($Report)) { throw 'Headless run produced empty report.' }

# Strip any pre-report chatter the model emits before the first horizontal rule / Bottom line.
# Keep everything from the first '**Bottom line' or the first '---' line, whichever comes first.
$reportLines = $Report -split "`r?`n"
$startIdx = ($reportLines | Select-String -SimpleMatch '**Bottom line' | Select-Object -First 1).LineNumber
if (-not $startIdx) {
  for ($i=0; $i -lt $reportLines.Count; $i++) { if ($reportLines[$i].Trim() -eq '---') { $startIdx = $i + 1; break } }
}
if ($startIdx -and $startIdx -gt 1) {
  $Report = ($reportLines[($startIdx-1)..($reportLines.Count-1)] -join "`n").TrimStart()
}

# Guardrail: the prompt forbids the word "clean" as a posture claim. Warn (do not block) if present.
if ($Report -match '(?i)\bclean\b') {
  Write-Host "WARNING: report contains the word 'clean' — verify it is not a posture claim." -ForegroundColor Yellow
}

# --- Write the timestamped report (prepend a small provenance header) ---
$ReportHeader = @"
<!-- SOC cycle ($RunLabel) — generated $Stamp local on the operator workstation via headless claude -p.
     Read-only triage over ~24h. Source prompt: soc-agent/soc-cycle.prompt.md. -->

"@
Set-Content -LiteralPath $ReportFile -Value ($ReportHeader + $Report) -Encoding UTF8
Write-Host "Report written: $ReportFile" -ForegroundColor Green

# --- Append-only KB cycle-note (NEVER overwrite existing analysis) ---
$CycleNoteFile = Join-Path $CycleNoteDir 'index.md'
if (-not (Test-Path $CycleNoteFile)) {
  $seed = @"
---
title: SOC Cycle Notes (append-only log)
tags: [security, soc, cycle-notes, agent, append-only]
status: log
---

# SOC Cycle Notes

Append-only dated log of autonomous SOC triage cycles (Phase 1, on-demand on the workstation).
Each entry: durable learnings only, newest at the bottom. Do **not** overwrite prior entries.
Standing tenets: [[kb/security/monitoring-principles]]. Roadmap: [[kb/projects/soc-agent-roadmap]].
"@
  Set-Content -LiteralPath $CycleNoteFile -Value $seed -Encoding UTF8
}
# Pull the bottom-line: the text after a '**Bottom line' marker, else first substantive line.
$BottomLine = ''
$bm = ($Report | Select-String -Pattern '(?im)\*\*Bottom line:?\*\*\s*(.+)$' | Select-Object -First 1)
if ($bm) { $BottomLine = $bm.Matches[0].Groups[1].Value }
if (-not $BottomLine) {
  $BottomLine = ($Report -split "`r?`n" |
    Where-Object { $_.Trim() -and $_ -notmatch '^\s*(#|<!--|---|\*\*\d|\|)' } |
    Select-Object -First 1)
}
$BottomLine = ($BottomLine -replace '\*\*','').Trim()
if (-not $BottomLine) { $BottomLine = '(see report)' }
$noteEntry = @"

## $DateOnly $Stamp — $RunLabel
- Bottom line: $($BottomLine.Trim())
- Report: ``soc-agent/reports/soc-$Stamp.md``
- Method: headless ``claude -p`` + soc-analyst skill, read-only elasticsearch + so_gateway MCPs, ~24h window.
"@
Add-Content -LiteralPath $CycleNoteFile -Value $noteEntry -Encoding UTF8
Write-Host "KB cycle-note appended: $CycleNoteFile" -ForegroundColor Green

# --- Append one scored, dated line to the prioritized backlog ---
if (-not (Test-Path $BacklogFile)) {
  $bseed = @"
# SOC Agent — Prioritized Backlog

One scored, dated line per cycle's top recommendation. Score = rough priority 1 (low) - 5 (urgent).
Append-only; the operator re-prioritizes. Capability recs come from each cycle's Section 3.

| Date | Score | Item | Source report |
|---|---|---|---|
"@
  Set-Content -LiteralPath $BacklogFile -Value $bseed -Encoding UTF8
}
# Extract the Section-3 capability recommendation: the first substantive line AFTER the
# "## 3." heading (skip the heading itself), else first 'recommend' line, else fallback.
$lines = $Report -split "`r?`n"
$capLine = ''
$sec3 = $null
for ($i=0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -match '^\s*#+\s*3[\.\):]') { $sec3 = $i; break }
}
if ($null -ne $sec3) {
  for ($j=$sec3+1; $j -lt $lines.Count; $j++) {
    $t = ($lines[$j] -replace '^\s*[\*\-#>\s]+','').Trim()
    if ($t -and $t -notmatch '^#') { $capLine = $t; break }
  }
}
if (-not $capLine) {
  $capLine = ($lines | Where-Object { $_ -match '(?i)recommend' -and $_ -notmatch '^\s*#' } | Select-Object -First 1)
}
if (-not $capLine) { $capLine = 'See report Section 3 (capability recommendation).' }
$capLine = ($capLine -replace '\|','/' -replace '\*\*','' -replace '^\s*[\*\-#>\s]+','').Trim()
if ($capLine.Length -gt 160) { $capLine = $capLine.Substring(0,157) + '...' }
$score = 3   # default mid priority; operator re-scores
$backlogRow = "| $DateOnly | $score | $capLine | ``soc-agent/reports/soc-$Stamp.md`` |"
Add-Content -LiteralPath $BacklogFile -Value $backlogRow -Encoding UTF8
Write-Host "Backlog line appended: $BacklogFile" -ForegroundColor Green

# --- Discord (STUB — no-op) ---
Send-Discord -Markdown $Report -Title "SOC cycle $Stamp ($RunLabel)"

# --- Path-scoped commit (rebase on reject) ---
if ($NoCommit) {
  Write-Host "`n-NoCommit set; skipping git commit." -ForegroundColor DarkYellow
  return
}
Push-Location $RepoRoot
try {
  $relReport = "soc-agent/reports/soc-$Stamp.md"
  git add -- 'soc-agent/' 'kb/security/cycle-notes/' 2>&1 | Out-Null
  $msg = "soc-agent: $RunLabel $Stamp (report + cycle-note + backlog)"
  git commit -m $msg 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Host 'Nothing to commit (or commit failed).' -ForegroundColor Yellow }
  else { Write-Host "Committed: $msg" -ForegroundColor Green }

  # Push with rebase-on-reject (don't force).
  git fetch origin main 2>&1 | Out-Null
  git push origin HEAD:main 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host 'Push rejected; rebasing on origin/main and retrying once...' -ForegroundColor Yellow
    git pull --rebase origin main 2>&1 | Out-Null
    git push origin HEAD:main 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host 'Push still failing — resolve manually.' -ForegroundColor Red }
    else { Write-Host 'Pushed after rebase.' -ForegroundColor Green }
  } else { Write-Host 'Pushed to origin/main.' -ForegroundColor Green }
}
finally { Pop-Location }

Write-Host "`n=== Cycle complete ($RunLabel) ===" -ForegroundColor Cyan
