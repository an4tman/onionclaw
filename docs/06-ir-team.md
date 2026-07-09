# IR escalation team (approval-gated deep investigation)

When the daily cycle surfaces a genuine escalation candidate, the operator can launch a one-shot,
**read-only** incident investigation: a small agent-team that re-qualifies the candidate, builds a
timeline, maps it to ATT&CK, drafts response options, and converges to **one** incident record —
then stops. The team never writes to Security Onion and never applies anything.

## Files

| File | Role |
|---|---|
| `orchestration/ir-team/ir-investigate.sh` | The runner. Spawned by OpenClaw after GATE 1, captures the converged record, posts it to Discord. |
| `orchestration/ir-team/team.md` | The Incident-Commander brief: orchestrates the facets, enforces the rules, the §D scorecard, the two gates. |
| `orchestration/ir-team/facets/*.md` | The five read-only facet roles (below). |

## The five facets

1. **Triage** — re-qualifies the candidate, computes the §D trigger scorecard (the over-escalation
   audit), checks the documented benign baselines from your `environment.md`.
2. **Telemetry Investigator** ‖ 3. **Threat-Intel / ATT&CK** — the parallel leg (run via `Task`):
   build the timeline + blast radius; map ATT&CK + run indicator reputation via `enrich_iocs`. The
   Threat-Intel facet is the injection firewall for external indicators.
4. **Response Planner** — D3FEND-tagged options, each tagged *agentic-readonly* or *human-only*, plus
   one recommended action. **Proposals only.**
5. **Reporter** — the sole convergence point: assembles one incident record, rejects unsupported
   claims, ends at the recommended action + the two gate decisions.

## The two gates

- **GATE 1** — the operator types `investigate <id>` in Discord. OpenClaw (via `coding-agent`) spawns
  `ir-investigate.sh <id>`. No writes occurred to get here; `dismiss <id>` declines.
- **GATE 2** — the record ends with the *exact* recommended action (rule text / IP / token / narrow
  suppression). If it's a tuning, the operator applies it through the gated `approve <token>` path.
  The team did not and will not apply it.

## Read-only by construction

`ir-investigate.sh` runs `claude -p` with an allowlist of only the **read verbs** of the two SO MCP
namespaces plus local read/orchestration tools (`Read/Grep/Glob/Skill/Task/TodoWrite`). There is no
write/tune/disposition tool, no Bash, no Write/Edit — so even a successful prompt injection cannot
write.

## Install & invoke

The IR files install alongside the cycle (they're part of the `orchestration/` tree staged into
`$SOC_AGENT_HOME` in [05-autonomous-cycle](05-autonomous-cycle.md)). OpenClaw invokes the runner
from inside the container when the operator approves a candidate:

```bash
docker exec "$SOC_OPENCLAW_CONTAINER" \
  "$SOC_AGENT_HOME/ir-team/ir-investigate.sh" <candidate-id> [context-file]
```

OpenClaw passes the candidate id and (optionally) a file holding the candidate context (the cycle's
escalation block + known facts). The converged record is written to
`$SOC_AGENT_HOME/ir-team/reports/ir-<id>-<timestamp>.md` and posted to `SOC_DISCORD_CHANNEL` as one
short message + the full record attached.

> The channel `systemPrompt` (configured in [04-openclaw-setup](04-openclaw-setup.md)) tells the
> `soc` agent to run this exact path on `investigate <id>`. The agent runs *inside* the OpenClaw
> container and has no `docker` CLI, so the command it runs is the in-container path above — not a
> `docker exec` from elsewhere.

## Cost note

A full 5-facet fan-out spends real subscription budget (the parallel leg is the expensive part).
It's on-demand only — nothing runs until the operator approves a specific candidate.

Next: [07-self-improvement](07-self-improvement.md).
