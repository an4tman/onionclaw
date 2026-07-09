<#
.SYNOPSIS
  Phase 3 — capacity-gated SELF-IMPROVEMENT SPAWNER (opportunistic, on-demand).

.DESCRIPTION
  Reads the prioritized backlog (soc-agent/backlog.md), checks spare Claude-subscription
  capacity via `npx ccusage@latest blocks --json`, and IF capacity allows, spawns a
  `claude -p` worker (driven by selfimprove-worker.prompt.md) to work the top unblocked
  backlog item. The worker produces a REVIEWABLE ARTIFACT on a NEW git branch
  (selfimprove/<slug>) and/or a proposal under soc-agent/proposals/ — NEVER a live change,
  NEVER a merge, NEVER a push.

  HARD SAFETY (see the worker prompt + the README "Self-improvement spawner" section):
    - Tiered autonomy is TOOL-ENFORCED via --allowedTools: the worker can Read/Grep/Glob,
      Write/Edit repo files, run scoped test/build Bash, and commit to its branch. It has NO
      tool to push, deploy, restart containers, write to Security Onion, touch pfSense, or
      change live OpenClaw/LiteLLM config. We deliberately do NOT include any live-infra MCP
      or deploy tooling in the allowlist.
    - The worker's branch stays UNMERGED + UNPUSHED. The operator reviews and decides.
    - Opportunistic only: NO Task Scheduler registration, NO Discord post (stubbed).

.NOTES
  Runner: the operator workstation, Claude Code on the operator's subscription.
  ccusage has NO plan-cap/reset field for a subscription, so the gate uses the ACTIVE block's
  costUSD / totalTokens (must be BELOW a max) AND projection.remainingMinutes (must be ABOVE
  a floor). Thresholds are the clearly-labeled config constants below.
#>

[CmdletBinding()]
param(
  # Don't spawn a worker; just report the gate decision + the item that WOULD be worked.
  [switch]$DryRun,
  # Override the capacity-gate cost ceiling (USD) for THIS run only (demo/testing).
  [Nullable[double]]$MaxCostUSD,
  # Override the active-block token ceiling for THIS run only (demo/testing).
  [Nullable[long]]$MaxTotalTokens,
  # Override the remaining-minutes floor for THIS run only (demo/testing).
  [Nullable[int]]$MinRemainingMinutes,
  # Work a specific item index (1-based, from the backlog table) instead of the top one.
  [int]$ItemIndex = 0,
  # Override the model (default: opus alias).
  [string]$Model = 'opus',
  # Max minutes to allow the headless worker run.
  [int]$TimeoutMinutes = 30
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ======================================================================================
# CONFIG — capacity-gate thresholds. Gate on the ACTIVE ccusage block. Proceed ONLY if
# (cost < MAX) AND (tokens < MAX) AND (projected remaining minutes > FLOOR). ccusage exposes
# usage/projection, NOT a subscription hard cap, so these are conservative spare-capacity
# guards, NOT the real plan limit. Tune to taste; -MaxCostUSD / -MaxTotalTokens /
# -MinRemainingMinutes override these for a single run (demo/testing).
# ======================================================================================
$CFG_MaxActiveCostUSD       = 20.0        # skip if active-block spend already >= this
$CFG_MaxActiveTotalTokens   = 30000000    # skip if active-block tokens already >= this (30M)
$CFG_MinRemainingMinutes    = 60          # skip if < this many minutes left in the active block
# --------------------------------------------------------------------------------------

if ($null -ne $MaxCostUSD)          { $CFG_MaxActiveCostUSD     = [double]$MaxCostUSD }
if ($null -ne $MaxTotalTokens)      { $CFG_MaxActiveTotalTokens = [long]$MaxTotalTokens }
if ($null -ne $MinRemainingMinutes) { $CFG_MinRemainingMinutes  = [int]$MinRemainingMinutes }

# --- Paths (resolve repo root from this script; portable across clones) ---
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path        # .../soc-agent
$RepoRoot    = Split-Path -Parent $ScriptDir                          # ...the parent repo root
$PromptFile  = Join-Path $ScriptDir 'selfimprove-worker.prompt.md'
$BacklogFile = Join-Path $ScriptDir 'backlog.md'
$ProposalDir = Join-Path $ScriptDir 'proposals'

if (-not (Test-Path $PromptFile))  { throw "Worker prompt missing: $PromptFile" }
if (-not (Test-Path $BacklogFile)) { throw "Backlog missing: $BacklogFile" }
New-Item -ItemType Directory -Force $ProposalDir | Out-Null

Write-Host "=== Self-improvement spawner — $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" -ForegroundColor Cyan
Write-Host "Repo: $RepoRoot"

# --------------------------------------------------------------------------------------
# Send-Discord — STUB / no-op (Phase 4). Opportunistic spawner does NOT notify yet.
# --------------------------------------------------------------------------------------
function Send-Discord {
  param([string]$Markdown, [string]$Title = 'Self-improvement spawner')
  Write-Host "[Send-Discord STUB] Would post '$Title' ($($Markdown.Length) chars). No-op." -ForegroundColor DarkYellow
}

# ======================================================================================
# 1) CAPACITY GATE — read the ACTIVE ccusage block and decide.
# ======================================================================================
Write-Host "`n[1/4] Capacity gate (ccusage active block)..." -ForegroundColor Cyan
$ccRaw = & npx ccusage@latest blocks --json 2>$null
if (-not $ccRaw) { throw 'ccusage returned no output (is npx/ccusage reachable?).' }
$cc = ($ccRaw -join "`n") | ConvertFrom-Json
$active = $cc.blocks | Where-Object { $_.isActive -eq $true } | Select-Object -First 1
if (-not $active) {
  Write-Host 'No ACTIVE ccusage block (idle). Treating as ample capacity.' -ForegroundColor Green
  $cost = 0.0; $tokens = 0; $remaining = 99999
} else {
  $cost      = [double]$active.costUSD
  $tokens    = [long]$active.totalTokens
  $remaining = if ($active.projection -and $null -ne $active.projection.remainingMinutes) {
                 [int]$active.projection.remainingMinutes } else { 0 }
}
Write-Host ("  active spend : `${0:N2}  (max `${1:N2})" -f $cost, $CFG_MaxActiveCostUSD)
Write-Host ("  active tokens: {0:N0}  (max {1:N0})" -f $tokens, $CFG_MaxActiveTotalTokens)
Write-Host ("  remaining min: {0}  (floor {1})" -f $remaining, $CFG_MinRemainingMinutes)

$gateReasons = @()
if ($cost      -ge $CFG_MaxActiveCostUSD)     { $gateReasons += "active cost `${0:N2} >= max `${1:N2}" -f $cost, $CFG_MaxActiveCostUSD }
if ($tokens    -ge $CFG_MaxActiveTotalTokens) { $gateReasons += "active tokens $tokens >= max $CFG_MaxActiveTotalTokens" }
if ($remaining -lt $CFG_MinRemainingMinutes)  { $gateReasons += "remaining $remaining < floor $CFG_MinRemainingMinutes" }

if ($gateReasons.Count -gt 0) {
  Write-Host "`nGATE: NO SPARE CAPACITY — not spawning. Reasons:" -ForegroundColor Yellow
  $gateReasons | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
  Write-Host "Spawner is opportunistic; it yields to the operator/SOC cycle. Exiting cleanly." -ForegroundColor Yellow
  return
}
Write-Host 'GATE: spare capacity available -> proceed.' -ForegroundColor Green

# ======================================================================================
# 2) PICK the top unblocked backlog item (skip rows tagged DONE/BLOCKED/WIP in the Item text).
# ======================================================================================
Write-Host "`n[2/4] Selecting backlog item..." -ForegroundColor Cyan
$backlogLines = Get-Content -LiteralPath $BacklogFile
# Data rows = markdown table rows that aren't the header / separator.
$rows = @()
foreach ($ln in $backlogLines) {
  if ($ln -match '^\s*\|' -and $ln -notmatch '^\s*\|\s*-{2,}' -and $ln -notmatch '^\s*\|\s*Date\s*\|') {
    $cells = ($ln -split '\|') | ForEach-Object { $_.Trim() }
    # leading/trailing empty cells from the outer pipes
    $cells = $cells | Where-Object { $_ -ne '' -or $false }
    if ($cells.Count -ge 4) {
      $rows += [pscustomobject]@{
        Date   = $cells[0]
        Score  = $cells[1]
        Item   = $cells[2]
        Source = $cells[3]
        Raw    = $ln
      }
    }
  }
}
if ($rows.Count -eq 0) { throw 'No data rows found in backlog.md.' }

# Unblocked = Item text does not start with a [DONE]/[BLOCKED]/[WIP] tag.
$unblocked = $rows | Where-Object { $_.Item -notmatch '^\s*\[(DONE|BLOCKED|WIP)\]' }
if ($unblocked.Count -eq 0) { Write-Host 'No unblocked backlog items. Nothing to do.' -ForegroundColor Yellow; return }

$pick = if ($ItemIndex -ge 1 -and $ItemIndex -le $unblocked.Count) { $unblocked[$ItemIndex-1] } else { $unblocked[0] }
$itemText = ($pick.Item -replace '\s+', ' ').Trim()
# Source report path: strip backticks.
$sourceRef = ($pick.Source -replace '`','').Trim()

# Build a short slug from the first words of the item.
$slugBase = ($itemText -replace '[^a-zA-Z0-9 ]','' -replace '\s+','-').ToLower()
$slug = ($slugBase -split '-' | Where-Object { $_ } | Select-Object -First 6) -join '-'
if (-not $slug) { $slug = 'item' }
$slug = $slug.Substring(0, [Math]::Min(48, $slug.Length)).Trim('-')
$branch = "selfimprove/$slug"

Write-Host "  Item   : $itemText"
Write-Host "  Source : $sourceRef"
Write-Host "  Slug   : $slug"
Write-Host "  Branch : $branch"

if ($DryRun) {
  Write-Host "`n-DryRun: gate PASSED; would spawn worker on branch '$branch' for the above item. Not spawning." -ForegroundColor DarkYellow
  return
}

# ======================================================================================
# 3) CREATE the artifact branch off the current main (clean tree required).
# ======================================================================================
Write-Host "`n[3/4] Preparing artifact branch..." -ForegroundColor Cyan
Push-Location $RepoRoot
try {
  $dirty = git status --porcelain
  if ($dirty) { throw "Working tree not clean; refusing to branch. Commit/stash first.`n$dirty" }
  $startRef = (git rev-parse --abbrev-ref HEAD).Trim()
  if ([string]::IsNullOrWhiteSpace($startRef) -or $startRef -eq 'HEAD') { $startRef = 'main' }

  # If the branch already exists, append a timestamp so we never clobber a prior artifact.
  $exists = (git branch --list $branch)
  if ($exists) { $branch = "$branch-$(Get-Date -Format 'yyyyMMdd-HHmm')" }
  git checkout -b $branch 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Failed to create branch $branch." }
  Write-Host "  Created + checked out: $branch" -ForegroundColor Green
}
finally { Pop-Location }

# ======================================================================================
# 4) SPAWN the worker (claude -p, headless) with a TIGHT allowlist (tool-enforced tiers).
# ======================================================================================
Write-Host "`n[4/4] Spawning worker (claude -p)..." -ForegroundColor Cyan

# TIERED-AUTONOMY ALLOWLIST. Deliberately EXCLUDES: any push/deploy/restart, any so_gateway /
# elasticsearch / live-infra MCP, any SSH/remote tooling. Git is scoped to the safe verbs
# (add/commit/branch/status/diff/log/checkout) -- NO push/merge. Bash test/build verbs only.
$AllowedTools = @(
  'Read','Grep','Glob','Write','Edit','Skill',
  'Bash(git add:*)','Bash(git commit:*)','Bash(git status:*)','Bash(git diff:*)',
  'Bash(git log:*)','Bash(git branch:*)','Bash(git checkout:*)','Bash(git rev-parse:*)',
  'Bash(pytest:*)','Bash(npm test:*)','Bash(npm run test:*)','Bash(go test:*)',
  'Bash(dotnet test:*)','Bash(python -m pytest:*)',
  'Bash(pwsh -NoProfile -Command Invoke-Pester*)'
) -join ','

# NOTE: NOT in the allowlist (and therefore tool-blocked): Bash(git push*), Bash(git merge*),
# Bash(docker*), Bash(ssh*), Bash(scp*), WebFetch, mcp__so_gateway__*, mcp__elasticsearch__*,
# and everything else. The worker physically cannot run them.

$PromptBody = Get-Content -Raw -LiteralPath $PromptFile
$RunHeader = @"
WORK ITEM: $itemText
ITEM SLUG: $slug
BRANCH: $branch
SOURCE REPORT: $sourceRef
RUN CONTEXT: Spawned on the operator workstation at $(Get-Date -Format 'yyyy-MM-dd HH:mm') local. You are
already on branch '$branch' (created off '$startRef'). Produce a reviewable artifact per the
rules below. Do NOT push, merge, or apply anything live.

"@
$FullPrompt = $RunHeader + $PromptBody

$claudeExe = (Get-Command claude -ErrorAction Stop).Source
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $claudeExe
foreach ($a in @(
    '-p', '--model', $Model,
    '--allowedTools', $AllowedTools,
    '--permission-mode', 'acceptEdits',
    '--add-dir', $RepoRoot
)) { $psi.ArgumentList.Add($a) }
$psi.RedirectStandardInput  = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true
$psi.UseShellExecute  = $false
$psi.WorkingDirectory = $RepoRoot

$TimeoutMs = $TimeoutMinutes * 60 * 1000
$proc = [System.Diagnostics.Process]::Start($psi)
$proc.StandardInput.Write($FullPrompt)
$proc.StandardInput.Close()
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()
if (-not $proc.WaitForExit($TimeoutMs)) {
  try { $proc.Kill($true) } catch {}
  throw "Worker exceeded ${TimeoutMinutes}m timeout."
}
$WorkerOut = $stdoutTask.Result
$ErrText   = $stderrTask.Result
$ExitCode  = $proc.ExitCode

Write-Host "`n--- worker stdout ---" -ForegroundColor DarkCyan
Write-Host $WorkerOut
if ($ErrText) { Write-Host "--- worker stderr ---`n$ErrText" -ForegroundColor DarkGray }

# Rate-limit handling: record + stop (do NOT loop).
if ($WorkerOut -match '(?i)RATE-LIMITED|rate.?limit|usage limit|\b429\b' -or
    $ErrText   -match '(?i)rate.?limit|usage limit|\b429\b') {
  Write-Host "`nWorker hit a rate limit / exhausted capacity. Recorded; stopping (no retry)." -ForegroundColor Yellow
}
if ($ExitCode -ne 0) {
  Write-Host "Worker exited $ExitCode (artifact branch '$branch' left as-is for inspection)." -ForegroundColor Yellow
}

# ======================================================================================
# Report the artifact. The branch stays UNMERGED + UNPUSHED. Operator reviews.
# ======================================================================================
Push-Location $RepoRoot
try {
  $commits = git log --oneline "$startRef..HEAD" 2>$null
  $changed = git diff --name-only "$startRef..HEAD" 2>$null
  Write-Host "`n=== Artifact summary ===" -ForegroundColor Cyan
  Write-Host "Branch (UNMERGED, UNPUSHED): $branch  (off $startRef)"
  Write-Host "Commits on branch:";  if ($commits) { $commits | ForEach-Object { Write-Host "  $_" } } else { Write-Host '  (none — worker committed nothing)' -ForegroundColor Yellow }
  Write-Host "Files changed:";      if ($changed) { $changed | ForEach-Object { Write-Host "  $_" } } else { Write-Host '  (none)' -ForegroundColor Yellow }
  Write-Host "`nReview:   git -C `"$RepoRoot`" diff $startRef..$branch"
  Write-Host "Discard:  git -C `"$RepoRoot`" branch -D $branch"
}
finally { Pop-Location }

Send-Discord -Markdown $WorkerOut -Title "Self-improvement artifact: $branch"
Write-Host "`n=== Spawner complete. Artifact left on branch '$branch' for human review. NOTHING applied live. ===" -ForegroundColor Cyan
