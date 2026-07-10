# 09: Operator runbook (day 2)

The system's built; here's how you drive it. Everything happens in your Discord SOC
channel (`SOC_DISCORD_CHANNEL`), usually from a phone, usually in the thirty seconds
between reading the briefing and going back to whatever you were doing. That's the
design goal, anyway. The `soc` agent handles the commands.

## Discord command syntax

| You do | What happens |
|---|---|
| React ✅ (or 👍) on a proposal message | Approve that proposal. Requires an OpenClaw build that delivers Discord reaction events (2026.6.5 does not: the reaction listener exists in the bundle but is never registered; verify on yours before relying on it). The `soc` agent applies the one token in that message; ❌/🚫 dismisses. |
| `approve <token>` | Apply a tuning the cycle proposed. The `soc` agent calls `apply_tuning(token)`, the single SO write. Audited and reversible. |
| `approve` (bare) | With exactly one proposal pending, approves it; otherwise the agent lists what's pending and asks which. |
| `revert <handle>` | Undo a previously applied tuning. The agent calls `revert_tuning(handle)` and restores the captured prior state. |
| `list tunings` | List currently applied tunings and their undo handles. |
| `investigate <id>` | Launch the read-only IR team on an escalation candidate (gate 1). Posts a converged incident record. |
| `dismiss <id>` | Decline an escalation candidate. No investigation. |

Notes:
- Tokens and handles are short word pairs (e.g. `amber-fox`): easy to retype on a phone,
  and matching is case- and separator-tolerant (`Approve Amber Fox` works). They are
  workflow bindings, not secrets (see [10-security-model](10-security-model.md)).
- Tokens are single-use and in-memory. A proposed-tuning token lapses if the gateway
  restarts; re-run a cycle (or re-propose) for a fresh one. `list_pending_proposals` (ask
  the agent: "list pending") shows what's still open.
- `disable`/`modify` proposals get a second confirmation step before applying, because
  they're broader than a `suppress`.
- Reaction approval depends on your OpenClaw build delivering Discord reaction events
  (`reactionNotifications`, default `"own"`). Test it: react to any bot message and ask
  the agent what it saw. On builds without it (2026.6.5), typed approval is the path;
  proposals are still one message each, so the reaction flow lights up after an upgrade
  with no other change.

## Reading a briefing

Each cycle posts one briefing message: a severity dot (🔴 ESCALATE / 🟠 ATTENTION /
🟢 NOMINAL), the bottom line (what was detected vs. what the telemetry could see), an
"Interesting" insight, a verdict tally, one line per report section, the escalation
arrows (`→`), and a count of tuning proposals. Each proposal follows as its own message
with its approve line. The full report `.md` is attached; open it for the evidence,
queries, and per-group reasoning.

> Read the bottom line as bounded assurance, never "all clear." A 🟢 NOMINAL means nothing
> cleared the escalation bar in available telemetry, within the named blind spots. It does
> not mean the network is clean. See [10-security-model](10-security-model.md).

## Routine tasks

**Run a cycle on demand**
```bash
docker exec "$SOC_OPENCLAW_CONTAINER" "$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh"
```

**Check the last report / errors**
```bash
docker exec "$SOC_OPENCLAW_CONTAINER" sh -c 'ls -t '"$SOC_AGENT_HOME"'/reports/*.md | head -1'
# a same-named .err sibling exists only if a run failed
```

**Gateway health**
```bash
curl "http://$SOC_DOCKER_HOST:$SOC_SO_GATEWAY_PORT/mcp"   # reachable?
# or, via the agent: ask it to call mcp__so_gateway__ping  -> "Ready"
```

**Which TI providers are enabled**: ask the agent to call `ti_provider_status` (no
secrets), or check which keys are set in the gateway's `ti.env`.

**Rotate SO / TI credentials**: edit the gateway's `so.env` / `ti.env` and recreate the
`mcp-so-gateway` container (see [03-mcp-deployment](03-mcp-deployment.md)). Tuning tokens
reset on restart.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "Approved a tuning, nothing applied" | The SOC channel is probably bound to a model with weak tool calling (it loops and never calls `apply_tuning`). Confirm the Discord bind and the `soc` agent's model ([04-openclaw-setup](04-openclaw-setup.md)). |
| Every `so_gateway` tool 500s after a while | A stale SO session. The gateway self-heals (re-auth + retry); if it persists, `docker restart mcp-so-gateway`. Check the SO service account isn't locked/expired. |
| Cycle missed its run | If you use OpenClaw's managed cron, a version update can drop the job; confirm it exists, is scheduled on `SOC_CYCLE_CRON`, and is pinned to `agent_id = soc`. If you use host cron, check the host crontab and the wrapper's failure notice. |
| `get_detection` empty for a Sigma UUID | Stale gateway image; rebuild it. `get_detection` falls back from ES `_id` to `publicId`, so a UUID should resolve. Don't conclude "the gateway can't handle Sigma." |
| Queries silently return zero | The data-stream wildcard trap: query the bare stream name (`logs-suricata.alerts-so`), not `logs-suricata.alerts-so-*`. See the skill's `elastic-queries.md`. |
| Reports back-timestamped / cycle "misses today" | If SO is a VM whose clock drifted after a hypervisor pause, install the chrony override ([02-security-onion-setup](02-security-onion-setup.md)). |

## Keeping it honest

- Suppress behavior, never a host. Every `approve` you grant should be a narrow,
  behavior-specific proposal. If a proposal looks host-wholesale, reject it and ask for a
  narrower scope. The agent's supposed to know better, but the gate is only as good as
  the human at it.
- Feed the grounding. When a briefing teaches you something about your own network (a
  new device, a newly explained noisy pattern, a service that moved), put it in
  `environment.md`. That file is the analyst's entire mental model of your LAN, it can't
  update it itself, and stale grounding is the main source of misclassification. Five
  minutes of editing after a surprising briefing pays for itself for months.

Next: [10-security-model](10-security-model.md).
