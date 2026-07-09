#!/bin/sh
# SOC triage cycle — headless, READ-ONLY, container-side.
# Runs the bounded SOC prompt via Claude Code (operator subscription), captures the
# report, then delivers a clean analyst-style briefing to the operator's Discord channel.
#
# Read-only by construction: --allowedTools is scoped to the elasticsearch MCP plus the
# READ-ONLY so_gateway tools AND so_gateway propose_tuning (which is itself read-only: it
# validates+previews and issues a single-use token, performing NO SO write). The so_gateway
# WRITE tools (apply_tuning / revert_tuning / disposition_alerts) are deliberately NOT in
# the allowlist, so this headless cycle physically cannot apply a tuning. Applying a proposal
# is a separate, operator-gated step (operator replies `approve <token>` in Discord ->
# OpenClaw's agent calls apply_tuning). No write/edit/bash tools are granted here.
#
# NOTE: we enumerate so_gateway tools explicitly instead of the `mcp__so_gateway__*` wildcard
# precisely so the write tools cannot be reached even by name. Keep this list in sync if the
# gateway adds read-only tools.
#
# Delivery (redesigned 2026-06-02): ONE clean Discord message — no code-block chunking.
#   - A tight markdown briefing: severity-tagged headline (🔴/🟠/🟢 + ESCALATE/ATTENTION/
#     NOMINAL), the bounded-assurance bottom line, a verdict tally, one line per report
#     section, and the escalation arrows. Kept well under Discord's 2000-char message limit.
#   - The FULL report .md attached as a document via --media so nothing is lost.
#   Why this shape (verified 2026-06-02 on build 2026.5.27, re-verified 2026-07-09 on
#   2026.6.5): `openclaw message send --presentation` is accepted but the buttons do NOT
#   render on Discord (Discord API read-back shows components:[]). So the clean, reliable
#   path is markdown body + attached document, with proposals as separate messages the
#   operator can react to (reaction events DO reach the agent). Re-test --presentation
#   buttons after an OpenClaw upgrade — the current docs describe working components v2.
#   Severity is conveyed by the leading colored dot + label instead of an embed sidebar.
# Presentation only: the analytical prompt/content is unchanged.
#
# Runs INSIDE the OpenClaw container (paths/channel come from soc-suite.env):
#   docker exec "$SOC_OPENCLAW_CONTAINER" "$SOC_AGENT_HOME/soc-cycle.sh"
set -eu

# Load site config (resolves soc-suite.env; see orchestration/lib/soc-suite-config.sh).
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SELF_DIR/../lib/soc-suite-config.sh"

PROMPT="$SELF_DIR/soc-cycle.prompt.md"
REPORTS="$SOC_AGENT_HOME/reports"
CLAUDE="$SOC_CLAUDE_BIN"
DISCORD_CHANNEL="$SOC_DISCORD_CHANNEL"

mkdir -p "$REPORTS"

# 1. Auth + config for headless Claude Code (subscription token, isolated config dir).
. "$SOC_CLAUDE_ENV"
export CLAUDE_CONFIG_DIR="$SOC_CLAUDE_CONFIG_DIR"

TS=$(date +%Y%m%dT%H%M%S)
REPORT="$REPORTS/soc-$TS.md"

# 2. Run the bounded cycle. Read-only tool scope: elasticsearch (read-only) + the read-only
#    so_gateway tools + propose_tuning (read-only) + local reads. WRITE so_gateway tools
#    (apply_tuning/revert_tuning/disposition_alerts) are intentionally omitted.
SO_RO_TOOLS="mcp__so_gateway__ping mcp__so_gateway__get_detection mcp__so_gateway__get_playbook mcp__so_gateway__run_guided_analysis mcp__so_gateway__propose_tuning mcp__so_gateway__list_pending_proposals mcp__so_gateway__enrich_iocs mcp__so_gateway__extract_iocs mcp__so_gateway__ti_provider_status"
"$CLAUDE" -p "$(cat "$PROMPT")" \
  --allowedTools "$SO_RO_TOOLS mcp__elasticsearch__* Read Grep Glob Skill" \
  > "$REPORT" 2>"$REPORTS/soc-$TS.err" || {
    echo "claude cycle FAILED (exit $?). stderr:" >&2
    cat "$REPORTS/soc-$TS.err" >&2
    exit 1
  }

echo "Report written: $REPORT"

# 3. Build a clean analyst briefing from the report. Extraction is best-effort and
#    degrades gracefully ("see attached") if the report shape drifts.
WINDOW="last ~24h"

# Bottom line: the sentence after the "Bounded-assurance bottom line:" marker.
BOTTOM=$(awk '
  /[Bb]ottom.?line/ {
    line=$0
    sub(/.*[Bb]ottom.?line[^:]*:[[:space:]]*/, "", line)
    gsub(/\*\*/, "", line)
    sub(/^[[:space:]]+/, "", line)
    if (length(line) > 2) { print line; exit }
  }' "$REPORT")
[ -n "$BOTTOM" ] || BOTTOM=$(sed -n '1,12p' "$REPORT" | grep -m1 . | sed 's/[#*]//g')

# One interesting insight: the line the prompt emits as "**Interesting:** ...".
INSIGHT=$(awk '
  /Interesting:/ {
    line=$0
    sub(/.*Interesting:[[:space:]*]*/, "", line)
    gsub(/[*]/, "", line)
    if (length(line) > 2) { print line; exit }
  }' "$REPORT" | cut -c1-220)

# Verdict tally across the report.
count() { grep -oiE "verdict[^A-Za-z]*$1" "$REPORT" | wc -l | tr -d ' '; }
N_ESC=$(count 'escalate')
N_INV=$(count 'investigate')
N_TUNE=$(count 'tune')
N_SUP=$(count 'suppress')

# Escalation-candidate arrow lines from section 4 (the prompt emits "→ VERDICT: ...").
ESCLINES=$(grep -E '^\*\*→|^→' "$REPORT" | sed -E 's/^\*\*//; s/\*\*$//; s/\*\*//g' | head -4)
# Did the cycle explicitly say nothing cleared the bar?
NONE_CLEARED=0
grep -qiE 'no (other )?finding[s]? clear|none clear|nothing clears|does not clear the escalation bar' "$REPORT" && NONE_CLEARED=1

# Severity model:
#   🔴 ESCALATE  = a hard ESCALATE verdict exists.
#   🟠 ATTENTION = real escalation candidates / investigate / tune (needs attention).
#   🟢 NOMINAL   = only suppress/explained, nothing to chase.
HAS_CANDIDATE=0
{ printf '%s' "$ESCLINES" | grep -qiE 'investigate|escalate'; } && HAS_CANDIDATE=1
if [ "$N_ESC" -gt 0 ]; then
  DOT="🔴"; SEV="ESCALATE"
elif [ "$HAS_CANDIDATE" -eq 1 ] && [ "$NONE_CLEARED" -eq 0 ]; then
  DOT="🟠"; SEV="ATTENTION"
elif [ "$N_INV" -gt 0 ] || [ "$N_TUNE" -gt 0 ]; then
  DOT="🟠"; SEV="ATTENTION"
else
  DOT="🟢"; SEV="NOMINAL"
fi

# First strong lead-in line under a "### N." section heading.
headline() {
  awk -v n="$1" '
    $0 ~ ("^###[[:space:]]*" n "\.") { insec=1; next }
    insec && /^###/ { exit }
    insec && /[A-Za-z]/ {
      l=$0; gsub(/^[[:space:]>*-]+/, "", l); gsub(/\*\*/, "", l)
      if (length(l) > 3) { print l; exit }
    }' "$REPORT"
}
H_NET=$(headline 2 | cut -c1-160)

# The single capability recommendation: the bolded title under section 3.
CAP=$(awk '
  /^###[[:space:]]*3\./ { insec=1; next }
  insec && /^###/ { exit }
  insec && /^\*\*/ { l=$0; gsub(/\*\*/,"",l); gsub(/^[[:space:]]+|[[:space:]]+$/,"",l); print l; exit }
' "$REPORT" | cut -c1-180)
[ -n "$CAP" ] || CAP=$(headline 3 | cut -c1-160)

# Tuning proposals: surface them IN the message (token + approve line) so the operator can
# approve without opening the attachment. The prompt emits one "PROPOSAL — ..." block per
# proposal with a "Token:" and a "To APPROVE: reply ... approve <token>" line. Extract those
# blocks verbatim (they are short and the operator needs the exact token + approve syntax).
PROPOSALS=$(awk '
  /^PROPOSAL —/ || /^PROPOSAL -/ { inblk=1 }
  inblk {
    if ($0 ~ /^[[:space:]]*```/) next        # drop stray code-fence lines the model may add
    if ($0 ~ /^[[:space:]]*$/ && started) { inblk=0; print ""; next }
    print; started=1
  }
' "$REPORT")
N_PROP=$(printf '%s\n' "$PROPOSALS" | grep -cE '^PROPOSAL [—-]' || true)

# Proposals are delivered as SEPARATE follow-up messages (one per proposal, sent after the
# briefing below) so an operator reaction targets exactly one proposal: react ✅ to approve,
# ❌ to dismiss — or reply `approve <token>` as always. The briefing just counts them.
if [ "${N_PROP:-0}" -gt 0 ]; then
  PROP_SECTION=$(printf '\n**5 · Tuning proposals (%s) — operator-gated**\nPosted below, one message each: react ✅ to approve (❌ to dismiss) or reply `approve <token>`. Nothing is applied until you do.\n' \
    "$N_PROP")
else
  PROP_SECTION=$(printf '\n**5 · Tuning proposals** — none this cycle.\n')
fi

# Assemble the analyst-section part of the briefing (everything EXCEPT the proposals).
HEAD=$(printf '%s **SOC briefing — %s** · %s · window: %s\n\n%s\n**Interesting** - %s\n\n**1 · Alerts** — escalate %s · investigate %s · tune %s · suppress %s\n**2 · Network / posture** — %s\n**3 · Capability rec** — %s\n**4 · Escalation**\n%s\n' \
  "$DOT" "$SEV" "$TS" "$WINDOW" \
  "$BOTTOM" \
  "${INSIGHT:-see attached}" \
  "$N_ESC" "$N_INV" "$N_TUNE" "$N_SUP" \
  "${H_NET:-see attached}" \
  "${CAP:-see attached}" \
  "$ESCLINES")
FOOT=$(printf '\n*Full report attached (soc-%s.md).*' "$TS")

# Discord caps a message at 2000 chars. Proposals ride their own messages now, so only the
# short count line + footer need a budget; trim only the analyst-section HEAD to fit.
PROP_LEN=$(printf '%s%s' "$PROP_SECTION" "$FOOT" | wc -c | tr -d ' ')
HEAD_BUDGET=$((1900 - PROP_LEN))
[ "$HEAD_BUDGET" -lt 200 ] && HEAD_BUDGET=200
HEAD=$(printf '%s' "$HEAD" | cut -c1-"$HEAD_BUDGET")
BODY=$(printf '%s%s%s' "$HEAD" "$PROP_SECTION" "$FOOT")

# 4. Deliver: the briefing (full report attached as a doc), then ONE message PER proposal.
#    One-proposal-per-message is what makes reaction approval unambiguous: the channel
#    agent maps an operator's ✅ on a message to the single token that message contains.
openclaw message send \
  --channel discord --target "$DISCORD_CHANNEL" \
  -m "$BODY" \
  --media "$REPORT"

if [ "${N_PROP:-0}" -gt 0 ]; then
  PROPDIR="$REPORTS/proposals-$TS"
  mkdir -p "$PROPDIR"
  printf '%s\n' "$PROPOSALS" | awk -v dir="$PROPDIR" '
    /^PROPOSAL [—-]/ { n++; blank=0 }
    n {
      if ($0 ~ /^[[:space:]]*$/) { blank=1; next }   # a blank line ends a block
      if (blank) next
      print >> (dir "/prop-" n ".txt")
    }
  '
  for PF in "$PROPDIR"/prop-*.txt; do
    [ -f "$PF" ] || continue
    PROP_MSG=$(printf '%s\n\n✅ react to approve · ❌ react to dismiss · or reply `approve <token>`' "$(cat "$PF")")
    openclaw message send \
      --channel discord --target "$DISCORD_CHANNEL" \
      -m "$PROP_MSG"
  done
  rm -rf "$PROPDIR"
fi

echo "Posted clean briefing to Discord channel $DISCORD_CHANNEL (severity=$SEV, report attached, proposals=$N_PROP)."
