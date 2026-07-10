# OnionClaw

> **Read this first.** Hobby project. Alpha. Unsupported. It came out of one homelab and
> it has bugs I haven't met yet. If it breaks, you get to keep both pieces. It is also,
> and I say this with the confidence of someone who watches it run every day, extremely
> cool.

Security Onion is great. Being the only analyst on your own Security Onion box is not.
The queue fills up with the same platform-mismatch Sigma noise every single day, and the
one alert that matters is buried under two hundred that don't. I got tired of being the
night shift, so I made Claude do it.

Once a day a headless Claude Code session wakes up inside my OpenClaw gateway and works
the queue like an analyst who actually read the SOPs: it pulls the last ~24h of alerts
out of SO's Elasticsearch, groups them, reads SO's own playbook for each detection,
pivots through Zeek and endpoint data, runs the external indicators past threat intel,
and posts one briefing to my Discord. It is not allowed to say "all clear". Ever. It
reports what it detected, what its sensors could and couldn't see, and what it thinks
should happen next.

When it finds an obvious false positive it drafts the narrowest suppression that kills
the noise and hands me a token. Nothing touches Security Onion until I type
`approve amber-fox` in the channel. Every applied change is logged with its prior state;
`revert` puts it back. When something actually smells wrong I type `investigate <id>` and
a read-only IR team requalifies the candidate, builds the timeline, maps it to ATT&CK,
and hands back one incident record with exactly one recommended action. I apply it or I
don't. The agents never do.

The important part, and the reason I trust it at 3am: the daily cycle *cannot* write.
Its tool allowlist simply doesn't contain a write tool. This isn't a prompt promising to
behave. There is no tool to misbehave with.

## What's in the box

- `skill/soc-analyst/`: the triage methodology as a skill. Workflow, verdict taxonomy,
  query discipline, the safety rules. You feed it an `environment.md` describing your
  network, because an analyst who doesn't know what's normal on your LAN is just a
  random-verdict generator.
- `mcp-so-gateway/`: an MCP server for SO's Core API. Reads detections and playbooks,
  holds the only SO write credential, does the propose/approve/revert tuning dance, and
  enriches IOCs (OTX, AbuseIPDB, VirusTotal, plus the keyless feeds). Tested; run
  `uv run pytest` and see.
- `orchestration/soc-cycle/`: the daily triage run. One wrapper script, one prompt, one
  briefing in your Discord.
- `orchestration/ir-team/`: the deep-dive team. Five roles, read-only, converges to one
  record and stops.
- `orchestration/self-improve/`: a worker that turns the backlog into reviewable git
  branches. Off by default, and it should stay off until you've watched it work.
- `security-onion/`: two small quality-of-life things for the SO box itself (a clock
  resync fix for VMs that got paused, and a watchdog for the daily rule update, because
  a silently stale ruleset is worse than no ruleset).
- `docs/`: the whole setup, in order, including the security model. Read `docs/10` even
  if you read nothing else.

## The shape of it

```
Security Onion ──▶ mcp-elasticsearch (:9220, read-only) ─┐
   (your box)  ──▶ mcp-so-gateway   (:9221, read + approved writes) ─┤
                                                               ▼
                       OpenClaw  ──(headless claude -p)──▶ soc-cycle / ir-team
                          │  soc agent + cron + coding-agent
                          ▼
                       Discord  ◀── briefings · approvals (approve / investigate / revert)
```

Full picture and the reasoning behind it: [docs/00-architecture.md](docs/00-architecture.md).

## What you need

An existing Security Onion 2.4+ install, a Docker host that can reach it, an OpenClaw
gateway, Claude Code with an Anthropic API key, and a Discord server with OpenClaw's bot
in it. If you don't already run most of that stack, stop here; this is not a drop-in
anything. It's the missing analyst for a specific kind of homelab, documented well enough
that you can build the same one.

One deliberate cost note: the headless runs want a cloud Claude model. I tried local
models. A 30B coder model looped its tool calls until the loop detector shot it. The
interactive Discord agent runs fine on a local 12B (mine does), but the tool-heavy
analysis cycle earns its API bill.

## Install

```bash
cp config/soc-suite.env.example config/soc-suite.env
$EDITOR config/soc-suite.env        # your network, SO, OpenClaw, and Discord values

bin/install.sh preflight            # checks reachability, touches nothing
bin/install.sh all                  # credential scaffolding, MCP containers, orchestration
```

The installer covers the scriptable parts and is idempotent, so re-run it after you fix
whatever the preflight yelled about. Credentials, the SO-side account and firewall grants
(docs/02), the OpenClaw wiring (docs/04), and the skill install (docs/08) are manual, on
purpose: those are the steps where you should know what you just did.

Then work through the docs in order: [prerequisites](docs/01-prerequisites.md) →
[Security Onion setup](docs/02-security-onion-setup.md) →
[MCP deployment](docs/03-mcp-deployment.md) → [OpenClaw](docs/04-openclaw-setup.md) →
[the cycle](docs/05-autonomous-cycle.md) → [IR team](docs/06-ir-team.md) →
[self-improvement](docs/07-self-improvement.md) → [the skill](docs/08-skill-install.md) →
[runbook](docs/09-operator-runbook.md) → [security model](docs/10-security-model.md).

## The part you should actually read

You are pointing an LLM at your security telemetry and giving it a path (a narrow,
supervised path) to change your detection rules. Think about that for a second, then go
read [docs/10-security-model.md](docs/10-security-model.md). The short version: the
cycle's allowlist has no write tools; the only write path is a single-use token that you
personally approve in Discord; everything applied is audited and reversible; all telemetry
is treated as hostile input because alert fields are a prompt-injection delivery vehicle;
and the only bytes that ever leave your network are external indicator values going to
the TI providers you chose to turn on. Also: the agent will sometimes be wrong. It's a
first-pass analyst, not an oracle. Read the report, not just the headline.

## Provenance

This ran for weeks as a pile of scripts scattered across a homelab config repo before it
got a name. What's here is that pile, de-hardcoded into one env file and documented so
somebody who isn't me can stand it up. MIT licensed. If you do something fun with it, I'd
genuinely like to hear about it.
