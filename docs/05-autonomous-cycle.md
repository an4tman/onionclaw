# 05: The autonomous SOC cycle

The thing you actually came for. Once a day, a headless run works ~24h of Security Onion
telemetry with the `soc-analyst` methodology and drops one briefing in your Discord, full
report attached, tuning proposals included. You approve the good ones, ignore the rest,
and get on with your life.

Prereqs: [02-security-onion-setup](02-security-onion-setup.md),
[03-mcp-deployment](03-mcp-deployment.md), [04-openclaw-setup](04-openclaw-setup.md), and
the `soc-analyst` skill installed in OpenClaw ([08-skill-install](08-skill-install.md)).

## Files

| File | Role |
|---|---|
| `orchestration/soc-cycle/soc-cycle.sh` | The wrapper. Runs the headless cycle, extracts a clean briefing, posts to Discord with the report attached. |
| `orchestration/soc-cycle/soc-cycle.prompt.md` | The fixed cycle contract (principles, tuning mechanics, the 5-section report shape). Pulls environment facts from the skill's `environment.md`. |
| `orchestration/lib/soc-suite-config.sh` | Resolves and loads `soc-suite.env` (sourced by the wrapper). |

## Why the cycle can't write

The wrapper runs `claude -p` with an explicit `--allowedTools` allowlist: the read-only
`elasticsearch` MCP, the read `so_gateway` tools, `propose_tuning` (itself read-only: it
validates, previews blast radius, and returns a single-use token without writing), plus
local `Read/Grep/Glob/Skill`. The `so_gateway` write tools (`apply_tuning`,
`revert_tuning`, `disposition_alerts`) are deliberately not in the list, so the cycle has
no way to apply a tuning. Applying is a separate step that waits for your approval (see
[09-operator-runbook](09-operator-runbook.md)).

> The wrapper enumerates the read-only `so_gateway` tools by name rather than a
> `mcp__so_gateway__*` wildcard, precisely so a write tool can't be reached even by name.
> If you add a read-only tool to the gateway, add it to `SO_RO_TOOLS` in `soc-cycle.sh`.

## Install (into the OpenClaw container)

1. **Configure.** Copy `config/soc-suite.env.example` to `config/soc-suite.env` and fill
   in your values (paths, `SOC_DISCORD_CHANNEL`, schedule). See [config/](../config/).
2. **Stage the orchestration tree** into the container at `SOC_AGENT_HOME`, preserving
   structure so the scripts find `../lib/` and their prompt files:

   ```bash
   . config/soc-suite.env
   # copy the orchestration tree + your filled soc-suite.env into the container
   # (or just: bin/install.sh orchestration, which scripts the same steps)
   docker cp orchestration/.        "$SOC_OPENCLAW_CONTAINER:$SOC_AGENT_HOME/"
   docker cp config/soc-suite.env   "$SOC_OPENCLAW_CONTAINER:$SOC_AGENT_HOME/soc-suite.env"
   docker exec "$SOC_OPENCLAW_CONTAINER" chmod +x \
     "$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh" \
     "$SOC_AGENT_HOME/ir-team/ir-investigate.sh"
   ```

   The config loader finds `soc-suite.env` next to the install (via
   `$SOC_AGENT_HOME/soc-suite.env`) or relative to the lib; either layout works.
3. **Verify auth + MCP reachability** with a dry smoke run:

   ```bash
   docker exec "$SOC_OPENCLAW_CONTAINER" sh -c \
     '. '"$SOC_CLAUDE_ENV"'; export CLAUDE_CONFIG_DIR='"$SOC_CLAUDE_CONFIG_DIR"'; \
      '"$SOC_CLAUDE_BIN"' -p "call mcp__so_gateway__ping and reply with its result" \
        --allowedTools mcp__so_gateway__ping'
   # expect: Ready
   ```
4. **Run a manual cycle** (writes a report + posts to Discord):

   ```bash
   docker exec "$SOC_OPENCLAW_CONTAINER" "$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh"
   ```

   Reports land in `$SOC_AGENT_HOME/reports/soc-<timestamp>.md` (with a `.err` sibling on
   failure).

## Schedule it

The recommended default is a plain host cron on the Docker host, with no LLM in the
scheduling path:

```cron
0 12 * * * docker exec "$SOC_OPENCLAW_CONTAINER" "$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh"
```

Wrap it in a small script that posts a Discord failure notice on non-zero exit (via
`docker exec <container> openclaw message send …`) so failures aren't silent. On Unraid
specifically, put the cron snippet in `/boot/config/plugins/dynamix/<name>.cron` and run
`update_cron`; that survives reboots.

OpenClaw's managed cron also works ([04-openclaw-setup §5](04-openclaw-setup.md)), but the
managed job is an agent turn: a model turn that execs the wrapper. Two verified failure
modes make it fragile:

1. Local models can loop the exec call. On the source deployment a local 12B repeated the
   identical exec 20 times until OpenClaw's loop detector blocked the session, silently
   killing the cycle for three days (`consecutiveErrors` climbing, nothing posted).
2. On OpenClaw 2026.6.5 the `command` cron payload is accepted by the CLI but silently
   ignored by the state-db loader (only `agentTurn`/`systemEvent` rows load), so you can't
   just drop the LLM out of the managed job.

If you use host cron, disable (don't delete) any OpenClaw cron job for the cycle. If you
use the managed cron, watch it across OpenClaw updates: a version update has been known to
silently drop a user-registered cron job during a migration. After an update, confirm the
job is still present and scheduled.

## The briefing shape

`soc-cycle.sh` extracts a tight, Discord-friendly briefing from the report (best-effort;
it degrades to "see attached" if the report shape drifts): a severity-tagged headline
(🔴 ESCALATE / 🟠 ATTENTION / 🟢 NOMINAL), the bottom line with its coverage bounds, an
"Interesting" insight line, a verdict tally, one line per report section, the escalation
arrows, and a count of tuning proposals. Each proposal is then posted as its own follow-up
message (verbatim block + an approve hint) so it is skimmable, and so a reaction
unambiguously targets one proposal on builds that deliver reaction events (see
[09-operator-runbook](09-operator-runbook.md)). The full report `.md` is always attached
to the briefing.

## Tuning the cadence & scope

- **Cadence**: change `SOC_CYCLE_CRON` (default once daily at noon). More frequent runs
  cost more API budget; the cycle is intentionally daily.
- **Report contract**: edit `soc-cycle.prompt.md` (the 5-section shape, the principles).
  Keep the literal markers the briefing extractor depends on: `**Bottom line:**`,
  `**Interesting:**`, `### N.` section headers, `verdict:`, `→` escalation lines, and the
  `PROPOSAL —` / `approve <token>` block.
- **Environment facts**: never hardcode them in the prompt; put them in the skill's
  `references/environment.md` ([08-skill-install](08-skill-install.md)).

Next: [06-ir-team](06-ir-team.md).
