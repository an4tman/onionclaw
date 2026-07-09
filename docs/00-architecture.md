# Architecture

`soc-agent-suite` is an **autonomous SOC analyst** layered over a [Security Onion](https://securityonion.net)
deployment. It triages alerts read-only, reports honestly to chat, proposes narrow tuning changes
behind an operator approval gate, enriches indicators with threat intel, and can escalate to a
read-only incident-response (IR) agent-team — all driven by a Claude subscription from inside an
[OpenClaw](https://docs.openclaw.ai) personal-assistant gateway.

## The pieces

```
                         ┌──────────────────────────────────────────────┐
                         │  Security Onion (manager/sensor, your box)    │
                         │   • Elasticsearch (telemetry)                 │
                         │   • Core API (detections, playbooks, tuning)  │
                         └───────────────▲──────────────▲───────────────┘
                  read-only ES queries   │              │  Core API (read + gated write)
                         ┌───────────────┴──────┐   ┌───┴───────────────────────┐
                         │  mcp-elasticsearch    │   │  mcp-so-gateway (THIS repo)│
                         │  :9220  (read-only)   │   │  :9221                     │
                         └───────────────▲──────┘   │  read tools · gated tuning │
                                         │          │  writes · TI enrichment    │
                                         │          └───▲────────────────────────┘
                                         │ MCP (HTTP)    │ MCP (HTTP)
                         ┌───────────────┴───────────────┴────────────────────────┐
                         │  OpenClaw container                                     │
                         │   • `soc` agent (cloud Claude)  ← Discord channel bind  │
                         │   • managed cron ─► soc-cycle.sh (headless `claude -p`) │
                         │   • coding-agent ─► ir-investigate.sh / self-improve    │
                         │   • `soc-analyst` skill (methodology + environment.md)  │
                         └───────────────────────────▲────────────────────────────┘
                                                     │ briefings / approvals
                                              ┌──────┴───────┐
                                              │   Discord    │  operator
                                              └──────────────┘
```

| Component | What it is | Where it lives |
|---|---|---|
| **`soc-analyst` skill** | The portable *methodology* — triage workflow, verdict taxonomy, query discipline, safety. Reads your per-deployment `environment.md` for grounding. | `skill/soc-analyst/` → installed into Claude Code + OpenClaw |
| **`mcp-elasticsearch`** | Read-only bridge to SO's Elasticsearch (search/esql/mappings). *Not bundled* — a standard read-only ES MCP. | external container, `:9220` |
| **`mcp-so-gateway`** | SO Core API gateway: read tools, **operator-gated** tuning writes, threat-intel enrichment. Holds the only SO write credential. | `mcp-so-gateway/` → container `:9221` |
| **Autonomous cycle** | Daily headless triage → honest Discord briefing + proposals. | `orchestration/soc-cycle/` |
| **IR agent-team** | Operator-approved, read-only deep investigation (5 facets → one incident record). | `orchestration/ir-team/` |
| **Self-improvement worker** | Capacity-gated, artifact-only worker that drafts proposals from the backlog. | `orchestration/self-improve/` |
| **SO-host helpers** | Optional SO-box add-ons: clock-resync override + signature-update health monitor. | `security-onion/` |
| **Site config** | One file (`soc-suite.env`) holding every environment-specific value. | `config/` |

## The runtime model (load-bearing decisions)

- **Cloud Claude drives the tools, not a local model.** The cycle and IR team run as headless
  `claude -p` on the operator's Claude **subscription**, inside the OpenClaw container. Local/heavy
  models loop on tool calls and never reliably reach the write step — so the `soc` agent and the
  headless runs are pinned to a cloud model (`SOC_CLOUD_MODEL`).
- **Read-only by construction; writes are gated.** The cycle's tool allowlist excludes every write
  tool, so it *physically cannot* tune. `propose_tuning` is read-only and issues a single-use token;
  the operator types `approve <token>` in Discord and only then does the `soc` agent call
  `apply_tuning`. Every applied write is audited and reversible (`revert_tuning`).
- **Two human gates for deep investigation.** An escalation candidate is *proposed*; the operator
  types `investigate <id>` to launch the read-only IR team (GATE 1); the team converges to one
  record and stops at a recommended action the operator applies (GATE 2). The team never writes.
- **Telemetry is untrusted.** Alert/log content can carry prompt-injection; every prompt treats it
  as data to analyze, never instructions. Enrichment ships only external indicator *values* to the
  enabled TI providers (RFC1918 dropped).

See [10-security-model.md](10-security-model.md) for the full safety model and the binding
monitoring tenets.

## Data flow — a daily cycle

1. OpenClaw's managed cron fires `soc-cycle.sh` (`SOC_CYCLE_CRON`, `SOC_TZ`).
2. It runs headless `claude -p` with a read-only tool scope, the cycle prompt, and the `soc-analyst`
   skill (which loads your `environment.md`).
3. The agent surveys ~24h of alerts via the Elastic MCP, pulls detections/playbooks and enriches
   external IOCs via the gateway, and writes a bounded-assurance report.
4. For clear false positives it calls `propose_tuning` (read-only) → tokens.
5. `soc-cycle.sh` posts one clean Discord briefing + the full report attached.
6. The operator replies `approve <token>` (apply a tuning) / `investigate <id>` (launch IR) /
   `revert <handle>` / `dismiss <id>` — handled by the `soc` agent in the channel.

## Install order

[01-prerequisites](01-prerequisites.md) → [02-security-onion-setup](02-security-onion-setup.md) →
[03-mcp-deployment](03-mcp-deployment.md) → [04-openclaw-setup](04-openclaw-setup.md) →
[05-autonomous-cycle](05-autonomous-cycle.md) → [06-ir-team](06-ir-team.md) →
[07-self-improvement](07-self-improvement.md) → [08-skill-install](08-skill-install.md) →
[09-operator-runbook](09-operator-runbook.md) → [10-security-model](10-security-model.md).
