# Architecture

The whole machine on one page. OnionClaw bolts an analyst onto a
[Security Onion](https://securityonion.net) box: a headless Claude Code session inside an
[OpenClaw](https://docs.openclaw.ai) gateway does the daily triage, a Discord channel is
the console, and anything that could actually change your SO config waits for you to say
so. This page is the map; the numbered docs are the build order.

## The pieces

```
                         ┌──────────────────────────────────────────────┐
                         │  Security Onion (manager/sensor, your box)    │
                         │   • Elasticsearch (telemetry)                 │
                         │   • Core API (detections, playbooks, tuning)  │
                         └───────────────▲──────────────▲───────────────┘
                  read-only ES queries   │              │  Core API (read + approved writes)
                         ┌───────────────┴──────┐   ┌───┴───────────────────────┐
                         │  mcp-elasticsearch    │   │  mcp-so-gateway (THIS repo)│
                         │  :9220  (read-only)   │   │  :9221                     │
                         └───────────────▲──────┘   │  read tools · tuning       │
                                         │          │  writes · TI enrichment    │
                                         │          └───▲────────────────────────┘
                                         │ MCP (HTTP)    │ MCP (HTTP)
                         ┌───────────────┴───────────────┴────────────────────────┐
                         │  OpenClaw container                                     │
                         │   • `soc` agent  ← Discord channel bind                 │
                         │   • cron ─► soc-cycle.sh (headless `claude -p`)         │
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
| `soc-analyst` skill | The portable methodology: triage workflow, verdict taxonomy, query discipline, safety. Reads your per-deployment `environment.md` for grounding. | `skill/soc-analyst/` → installed into Claude Code + OpenClaw |
| `mcp-elasticsearch` | Read-only bridge to SO's Elasticsearch (search/esql/mappings). Not bundled; a standard read-only ES MCP. | external container, `:9220` |
| `mcp-so-gateway` | SO Core API gateway: read tools, tuning writes that wait for your approval, threat-intel enrichment. Holds the only SO write credential. | `mcp-so-gateway/` → container `:9221` |
| Autonomous cycle | Daily headless triage → Discord briefing + proposals. | `orchestration/soc-cycle/` |
| IR team | Read-only deep investigation you launch on a candidate (5 facets → one incident record). | `orchestration/ir-team/` |
| Self-improvement worker | Drafts proposals from the backlog. Artifact-only, off by default. | `orchestration/self-improve/` |
| SO-host helpers | Optional SO-box add-ons: clock-resync override + signature-update health monitor. | `security-onion/` |
| Site config | One file (`soc-suite.env`) holding every environment-specific value. | `config/` |

## The runtime model (load-bearing decisions)

- **Grounding is the whole ballgame.** The analyst reads your `environment.md` (host
  table, what each box is supposed to do, documented FP baselines, named blind spots) at
  the start of every run. Feed it a wrong or stale host table and you get confident
  nonsense with query citations. The methodology ships in the skill; the judgment comes
  from what you teach it about your network. See [08-skill-install](08-skill-install.md),
  and treat that file as a living document, not a form you fill in once.
- **The headless runs are Claude Code, not the OpenClaw agent.** The cycle and the IR team run
  as `claude -p` inside the OpenClaw container, authenticated with their own credentials
  (an Anthropic API key in `claude.env`), separate from whatever model OpenClaw's agents use.
  The cycle is tool-heavy and needs a model that drives many tool calls reliably; weak local
  models loop on tool calls and never finish. The interactive `soc` agent that handles Discord
  approvals is a normal OpenClaw agent and can run on a local model if it has solid tool
  calling (the source deployment runs a local 12B there, routed through LiteLLM), but verify
  the approve-to-apply path before trusting one.
- **Reads are open, writes wait for you.** The cycle's tool allowlist excludes every write
  tool, so it cannot tune anything. `propose_tuning` is itself read-only: it validates,
  previews blast radius, and returns a single-use token. You type `approve <token>` in
  Discord, and only then does the `soc` agent call `apply_tuning`. Every applied write is
  audited and reversible (`revert_tuning`).
- **Two human decisions gate a deep investigation.** The cycle proposes an escalation
  candidate; you type `investigate <id>` to launch the read-only IR team (gate 1); the team
  converges to one record and stops at a recommended action that you apply yourself (gate 2).
  The team never writes.
- **Telemetry is untrusted.** Alert and log content can carry prompt injection, so every
  prompt treats it as data to analyze, never as instructions. Enrichment sends only external
  indicator values to the TI providers you enabled (RFC1918 is dropped).

See [10-security-model.md](10-security-model.md) for the full safety model and the binding
monitoring tenets.

## Data flow of a daily cycle

1. Cron fires `soc-cycle.sh` (`SOC_CYCLE_CRON`, `SOC_TZ`). This can be OpenClaw's managed
   cron or a plain host cron; see [05-autonomous-cycle](05-autonomous-cycle.md) for the
   tradeoff.
2. The wrapper runs headless `claude -p` with a read-only tool scope, the cycle prompt, and
   the `soc-analyst` skill (which loads your `environment.md`).
3. The agent surveys ~24h of alerts via the Elastic MCP, pulls detections and playbooks,
   enriches external IOCs via the gateway, and writes a report that states its coverage and
   blind spots.
4. For clear false positives it calls `propose_tuning` (read-only) and collects tokens.
5. `soc-cycle.sh` posts one clean Discord briefing with the full report attached.
6. You reply `approve <token>` (apply a tuning), `investigate <id>` (launch IR),
   `revert <handle>`, or `dismiss <id>`. The `soc` agent in the channel handles these.

## Install order

[01-prerequisites](01-prerequisites.md) → [02-security-onion-setup](02-security-onion-setup.md) →
[03-mcp-deployment](03-mcp-deployment.md) → [04-openclaw-setup](04-openclaw-setup.md) →
[05-autonomous-cycle](05-autonomous-cycle.md) → [06-ir-team](06-ir-team.md) →
[07-self-improvement](07-self-improvement.md) → [08-skill-install](08-skill-install.md) →
[09-operator-runbook](09-operator-runbook.md) → [10-security-model](10-security-model.md).
