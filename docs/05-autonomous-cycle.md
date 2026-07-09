# The autonomous SOC cycle

The daily cycle is the heart of the suite: a headless, **read-only** triage run that drives the
`soc-analyst` methodology over ~24h of Security Onion telemetry and posts one honest briefing (plus
the full report attached) to Discord, including any operator-gated tuning proposals.

Prereqs: [02-security-onion-setup](02-security-onion-setup.md), [03-mcp-deployment](03-mcp-deployment.md),
[04-openclaw-setup](04-openclaw-setup.md), and the `soc-analyst` skill installed in OpenClaw
([08-skill-install](08-skill-install.md)).

## Files

| File | Role |
|---|---|
| `orchestration/soc-cycle/soc-cycle.sh` | The wrapper. Runs the headless cycle, extracts a clean briefing, posts to Discord with the report attached. |
| `orchestration/soc-cycle/soc-cycle.prompt.md` | The fixed cycle contract (principles, tuning mechanics, the 5-section report shape). Pulls environment facts from the skill's `environment.md`. |
| `orchestration/lib/soc-suite-config.sh` | Resolves and loads `soc-suite.env` (sourced by the wrapper). |

## How it's read-only by construction

The wrapper runs `claude -p` with an explicit `--allowedTools` allowlist: the read-only
`elasticsearch` MCP, the **read** `so_gateway` tools, `propose_tuning` (itself read-only — it
validates, previews blast radius, and returns a single-use token without writing), plus local
`Read/Grep/Glob/Skill`. The `so_gateway` **write** tools (`apply_tuning`, `revert_tuning`,
`disposition_alerts`) are deliberately *not* in the list — so the cycle physically cannot apply a
tuning. Applying is a separate, operator-gated step (see [09-operator-runbook](09-operator-runbook.md)).

> The wrapper enumerates the read-only `so_gateway` tools by name rather than a
> `mcp__so_gateway__*` wildcard, precisely so a write tool can't be reached even by name. If you
> add a read-only tool to the gateway, add it to `SO_RO_TOOLS` in `soc-cycle.sh`.

## Install (into the OpenClaw container)

1. **Configure.** Copy `config/soc-suite.env.example` → `config/soc-suite.env` and fill in your
   values (paths, `SOC_DISCORD_CHANNEL`, schedule). See [config/](../config/).
2. **Stage the orchestration tree** into the container at `SOC_AGENT_HOME`, preserving structure so
   the scripts find `../lib/` and their prompt files:

   ```bash
   . config/soc-suite.env
   # copy the orchestration tree + your filled soc-suite.env into the container
   docker cp orchestration/.        "$SOC_OPENCLAW_CONTAINER:$SOC_AGENT_HOME/"
   docker cp config/soc-suite.env   "$SOC_OPENCLAW_CONTAINER:$SOC_AGENT_HOME/soc-suite.env"
   docker exec "$SOC_OPENCLAW_CONTAINER" chmod +x \
     "$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh" \
     "$SOC_AGENT_HOME/ir-team/ir-investigate.sh"
   ```

   The config loader finds `soc-suite.env` next to the install (via `$SOC_AGENT_HOME/soc-suite.env`)
   or relative to the lib — either layout works.
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

   Reports land in `$SOC_AGENT_HOME/reports/soc-<timestamp>.md` (with a `.err` sibling on failure).

## Schedule it (OpenClaw managed cron)

Register a cron job that runs the cycle on `SOC_CYCLE_CRON` in `SOC_TZ`, pinned to the `soc` agent.
The job command is the wrapper path inside the container:

```
$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh
```

OpenClaw's cron is SQLite-backed (`state/openclaw.sqlite`, table `cron_jobs`). Editing it headlessly
can be fiddly (the cron CLI may demand a device-pairing approval) — see the caveat in
[04-openclaw-setup](04-openclaw-setup.md#managed-cron). Pin the job to `agent_id = soc` so the cycle
runs on the reliable cloud model, never the default agent.

> **Watch cron across OpenClaw updates.** A version update has been known to silently drop a
> user-registered cron job during a migration. After an OpenClaw update, confirm the SOC cycle job
> is still present and scheduled.

## The briefing shape

`soc-cycle.sh` extracts a tight, Discord-friendly briefing from the report (best-effort, degrades to
"see attached" if the report shape drifts): a severity-tagged headline (🔴 ESCALATE / 🟠 ATTENTION /
🟢 NOMINAL), the bounded-assurance bottom line, an "Interesting" insight line, a verdict tally, one
line per report section, the escalation arrows, and any **tuning proposals verbatim** (the
load-bearing `approve <token>` lines are never trimmed). The full report `.md` is always attached.

## Tuning the cadence & scope

- **Cadence** — change `SOC_CYCLE_CRON` (default once daily at noon). More frequent runs cost more
  subscription budget; the cycle is intentionally daily.
- **Report contract** — edit `soc-cycle.prompt.md` (the 5-section shape, the principles). Keep the
  literal markers the briefing extractor depends on: `**Bottom line:**`, `**Interesting:**`,
  `### N.` section headers, `verdict:`, `→` escalation lines, and the `PROPOSAL —` / `approve <token>`
  block.
- **Environment facts** — never hardcode them in the prompt; put them in the skill's
  `references/environment.md` ([08-skill-install](08-skill-install.md)).

Next: [06-ir-team](06-ir-team.md).
