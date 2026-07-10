# 06: The IR escalation team

For the day the briefing shows something that actually smells wrong. Type
`investigate <id>` and a small agent team goes digging: re-qualifies the candidate,
builds the timeline, maps it to ATT&CK, drafts response options, and converges to one
incident record. Then it stops. Not "mostly stops". It never writes to Security Onion
and never applies anything; the last line of every record is a recommendation waiting on
you.

## Files

| File | Role |
|---|---|
| `orchestration/ir-team/ir-investigate.sh` | The runner. Spawned by OpenClaw after gate 1, captures the converged record, posts it to Discord. |
| `orchestration/ir-team/team.md` | The Incident-Commander brief: orchestrates the facets, enforces the rules, the §D scorecard, the two gates. |
| `orchestration/ir-team/facets/*.md` | The five read-only facet roles (below). |

## The five facets

1. **Triage** re-qualifies the candidate, computes the §D trigger scorecard (the
   over-escalation audit), and checks the documented benign baselines from your
   `environment.md`.
2. **Telemetry Investigator** and 3. **Threat-Intel / ATT&CK** run in parallel (via
   `Task`): one builds the timeline and blast radius; the other maps ATT&CK and runs
   indicator reputation via `enrich_iocs`. The Threat-Intel facet is the injection
   firewall for external indicators.
4. **Response Planner** drafts D3FEND-tagged options, each tagged *agentic-readonly* or
   *human-only*, plus one recommended action. Proposals only.
5. **Reporter** is the sole convergence point: it assembles one incident record, rejects
   unsupported claims, and ends at the recommended action + the two gate decisions.

## The two gates

- **Gate 1**: you type `investigate <id>` in Discord. OpenClaw (via `coding-agent`) spawns
  `ir-investigate.sh <id>`. No writes occurred to get here; `dismiss <id>` declines.
- **Gate 2**: the record ends with the exact recommended action (rule text / IP / token /
  narrow suppression). If it's a tuning, you apply it through the normal
  `approve <token>` path. The team did not and will not apply it.

## Why the team can't write

`ir-investigate.sh` runs `claude -p` with an allowlist of only the read verbs of the two
SO MCP namespaces plus local read/orchestration tools
(`Read/Grep/Glob/Skill/Task/TodoWrite`). There is no write/tune/disposition tool, no Bash,
no Write/Edit. Even a successful prompt injection has nothing to write with.

## Install & invoke

The IR files install alongside the cycle (they're part of the `orchestration/` tree staged
into `$SOC_AGENT_HOME` in [05-autonomous-cycle](05-autonomous-cycle.md)). OpenClaw invokes
the runner from inside the container when you approve a candidate:

```bash
docker exec "$SOC_OPENCLAW_CONTAINER" \
  "$SOC_AGENT_HOME/ir-team/ir-investigate.sh" <candidate-id> [context-file]
```

OpenClaw passes the candidate id and (optionally) a file holding the candidate context
(the cycle's escalation block + known facts). The converged record is written to
`$SOC_AGENT_HOME/ir-team/reports/ir-<id>-<timestamp>.md` and posted to
`SOC_DISCORD_CHANNEL` as one short message + the full record attached.

> The channel `systemPrompt` (configured in [04-openclaw-setup](04-openclaw-setup.md))
> tells the `soc` agent to run this exact path on `investigate <id>`. The agent runs
> inside the OpenClaw container and has no `docker` CLI, so the command it runs is the
> in-container path above, not a `docker exec` from elsewhere.

## Cost note

A full 5-facet fan-out spends real API budget (the parallel leg is the expensive part).
It's on-demand only; nothing runs until you approve a specific candidate.

Next: [07-self-improvement](07-self-improvement.md).
