# soc-agent-suite

An **autonomous SOC analyst** for a [Security Onion](https://securityonion.net) home/lab
deployment. It triages your alerts read-only on a schedule, posts an honest, bounded-assurance
briefing to chat, proposes *narrow* tuning changes behind an operator approval gate, enriches
indicators with threat intel, and can escalate to a read-only incident-response agent-team — all
driven by a Claude subscription from inside an [OpenClaw](https://docs.openclaw.ai)
personal-assistant gateway.

> **Status:** consolidated from a working home-lab deployment into an installable package. It is
> **not** a turnkey appliance — it assumes you run (or are willing to stand up) the dependency stack
> below and work through the setup. Every environment-specific value is parameterized into one
> config file; nothing is hardcoded to the original network.

## What you get

- **`soc-analyst` skill** — the portable triage methodology (workflow, verdict taxonomy, query
  discipline, safety), grounded by a per-deployment `environment.md` you fill in.
- **`mcp-so-gateway`** — a tested MCP server for SO's Core API: read tools, **operator-gated**
  tuning writes (audited, reversible), and threat-intel enrichment (OTX/AbuseIPDB/VirusTotal +
  keyless feeds). Holds the only SO write credential.
- **Autonomous cycle** — a daily headless triage → one clean Discord briefing + the full report.
- **IR agent-team** — operator-approved, read-only deep investigation that converges to one incident
  record and stops.
- **Self-improvement worker** — a capacity-gated, artifact-only worker that drafts proposals from the
  backlog (off by default).
- **SO-host helpers** — optional clock-resync override + signature-update health monitor.
- **Full setup docs** — Security Onion, MCP deployment, OpenClaw wiring, the cycle/IR/self-improve
  install, the operator runbook, and the security model.

## Architecture at a glance

```
Security Onion ──▶ mcp-elasticsearch (:9220, read-only) ─┐
   (your box)  ──▶ mcp-so-gateway   (:9221, read+gated write) ─┤
                                                               ▼
                       OpenClaw  ──(headless claude -p)──▶ soc-cycle / ir-team
                          │  soc agent (cloud Claude) + managed cron + coding-agent
                          ▼
                       Discord  ◀── briefings · approvals (approve / investigate / revert)
```

Full diagram and the load-bearing runtime decisions: **[docs/00-architecture.md](docs/00-architecture.md)**.

## Repository layout

```
soc-agent-suite/
├── config/soc-suite.env.example     # the ONE place every site-specific value lives
├── docs/                            # 00-architecture … 10-security-model
├── mcp-so-gateway/                  # the SO Core API gateway (source + tests + Dockerfile)
├── skill/soc-analyst/               # the analyst skill + environment.md (you fill in)
├── orchestration/                   # the headless runtime (installed into the OpenClaw container)
│   ├── lib/soc-suite-config.sh      #   config loader (sourced by the scripts)
│   ├── soc-cycle/                   #   the daily cycle: wrapper + prompt
│   ├── ir-team/                     #   IR runner + team brief + 5 facets
│   ├── self-improve/                #   worker prompt (+ legacy spawner for reference)
│   └── soc-log-forwarder.py         #   optional: ship pihole/OpenClaw logs to SO
├── security-onion/                  # optional SO-host helpers (clock resync, rule-update health)
├── examples/                        # an example egress-baseline elastalert rule
└── LICENSE
```

## Install

Configure once, then follow the docs in order:

```bash
cp config/soc-suite.env.example config/soc-suite.env
$EDITOR config/soc-suite.env        # fill in your network, SO, OpenClaw, and Discord values
```

1. **[Prerequisites](docs/01-prerequisites.md)** — what you need (SO, Docker, OpenClaw, a Claude
   subscription, Discord, optional TI keys).
2. **[Security Onion setup](docs/02-security-onion-setup.md)** — service account, API/ES access, the
   optional SO-host helpers.
3. **[MCP deployment](docs/03-mcp-deployment.md)** — the read-only Elastic bridge + this gateway.
4. **[OpenClaw setup](docs/04-openclaw-setup.md)** — agent, Discord bind, MCP wiring, cron,
   coding-agent.
5. **[Autonomous cycle](docs/05-autonomous-cycle.md)** — install + schedule the daily triage.
6. **[IR team](docs/06-ir-team.md)** · **[Self-improvement](docs/07-self-improvement.md)** — the
   escalation paths.
7. **[Skill install](docs/08-skill-install.md)** — install `soc-analyst` and fill in `environment.md`.
8. **[Operator runbook](docs/09-operator-runbook.md)** · **[Security model](docs/10-security-model.md)**
   — day-2 ops and the safety/threat model.

## Safety in one paragraph

The cycle is **read-only by construction** (its tool allowlist has no write tool). Tuning is
**operator-gated**: `propose_tuning` previews + issues a single-use token; you type `approve <token>`
in Discord; only then does the agent apply the one write, which is **audited and reversible**. The IR
team is read-only behind two human gates; the self-improvement worker is artifact-only and off by
default. Telemetry is treated as **untrusted** (prompt-injection), and only external indicator values
ever leave your network. The agent obeys binding monitoring tenets — no host is "trusted," the
highest-privilege host gets the most scrutiny, and it never reports "clean," only detections bounded
by named blind spots. See **[docs/10-security-model.md](docs/10-security-model.md)**.

## Provenance

Consolidated from a running home-lab deployment. The original scattered components
(`mcp-so-gateway`, the `soc-agent` orchestration, the `soc-analyst` skill, and the SO-VM helpers)
were gathered here, de-hardcoded into `config/soc-suite.env`, and documented for outside install.
Licensed under [MIT](LICENSE).
