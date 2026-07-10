# 01: Prerequisites

Inventory check before you touch anything. OnionClaw isn't self-contained; it's the
missing analyst for one specific stack: headless Claude Code inside an OpenClaw
container, talking to Security Onion through two MCP bridges, reporting to Discord. If
that sentence describes infrastructure you already run (or want to), keep reading. This
page is everything that has to exist before the rest of the docs make sense.

Configurable values are referenced by their `SOC_*` names throughout. Those live in
[`config/soc-suite.env.example`](../config/soc-suite.env.example); you copy it to
`soc-suite.env` and fill in your site's values during setup. No secrets go in that file.

---

## Checklist

| # | You need | Required? | Key config |
|---|----------|-----------|------------|
| 1 | Security Onion 2.4+ (reachable, ES + Core API) | Required | `SOC_SO_*` |
| 2 | A Docker host for the two MCP containers | Required | `SOC_DOCKER_HOST`, `SOC_ES_MCP_PORT`, `SOC_SO_GATEWAY_PORT` |
| 3 | OpenClaw (self-hosted assistant gateway) | Required | `SOC_OPENCLAW_CONTAINER`, `SOC_AGENT_HOME` |
| 4 | Claude Code + a model backend (Anthropic API key, or a local Anthropic-compatible endpoint) | Required | `SOC_CLAUDE_*`, `SOC_CLOUD_MODEL` |
| 5 | A Discord server + OpenClaw's bot + a SOC channel | Required | `SOC_DISCORD_CHANNEL` |
| 6 | Threat-intel API keys (OTX / AbuseIPDB / VirusTotal) | Optional | (in the gateway's `ti.env`) |
| 7 | The `soc-analyst` skill installed | Required | |

---

## 1. A working Security Onion deployment

The suite reads from an existing Security Onion 2.4+ install, and writes tunings to it when
you approve them. It does not stand one up for you.

- [ ] Security Onion 2.4 or newer, deployed and healthy. It can be a VM, a standalone box,
      or a manager+sensor grid, as long as it is reachable over the network from your
      Docker host (item 2). Install it per the official docs:
      <https://docs.securityonion.net>.
- [ ] Elasticsearch reachable (typically port `9200`). A stock SO install keeps ES internal
      to the grid; you must grant the Docker host access via SO's firewall hostgroups
      (`so-firewall includehost elasticsearch_rest …`; the full recipe is
      [02-security-onion-setup §3](./02-security-onion-setup.md)). Set `SOC_SO_ES_URL` to
      this endpoint. Self-signed TLS is expected (the bridge skips verification).
- [ ] The SO Core API reachable over HTTPS (`SOC_SO_URL`). SO's nginx checks the `Host`
      header, so the gateway must address SO by the name SO expects. Set `SOC_SO_HOSTNAME`
      and `SOC_SO_IP` so the gateway container can map the name to the IP.
- [ ] Admin access to SO, so you can:
  - create a dedicated service account (an analyst-role account) for the agent. Do not use
    your personal SO login;
  - mint a read-only Elasticsearch API key for the ES bridge (cluster `monitor`; indices
    `read` / `view_index_metadata` / `monitor`).
- [ ] Know your internal LAN CIDR(s) (`SOC_LAN_CIDR`). The analyst uses this to reason
      about traffic direction and to keep RFC1918 addresses out of threat-intel lookups.

> Why a service account: the agent's reads and writes should be attributable and revocable
> independently of any human login.

## 2. A Docker host for the MCP containers

Two MCP servers bridge the agent to Security Onion. Both run as containers, typically on
the same host that runs OpenClaw.

- [ ] A Docker host (`SOC_DOCKER_HOST`, by hostname or IP) that can reach Security Onion
      (item 1).
- [ ] Two free, LAN-reachable ports published from that host:

| Container | Purpose | Port |
|-----------|---------|------|
| `mcp-elasticsearch` | Read-only bridge to SO's Elasticsearch (`search`, `esql`, `list_indices`, `get_mappings`, `get_shards`) | `SOC_ES_MCP_PORT` (default 9220) |
| `mcp-so-gateway` | This suite's bridge to the SO Core API: detections/playbooks, the tuning write path, and threat-intel enrichment | `SOC_SO_GATEWAY_PORT` (default 9221) |

- [ ] The SO credentials these containers hold (the ES API key and the service account)
      live in the gateway's own env files (`so.env` / `ti.env`), not in `soc-suite.env`.
      Standing those up is covered in `docs/03-mcp-deployment.md`.

> Trust model: the MCP endpoints are unauthenticated but LAN-reachable. The server holds
> the credentials; clients connect with no token. This is fine under a trusted-LAN
> assumption; tighten it if that assumption does not hold.

## 3. OpenClaw

[OpenClaw](https://docs.openclaw.ai) is a self-hosted personal-assistant gateway. It is
the runtime that ties the suite together: it runs the agents, the MCP client wiring, the
Discord bot, and the `coding-agent` capability that spawns headless Claude Code.

- [ ] A working OpenClaw instance, typically a Docker container on the same host as the
      MCP containers (item 2). `SOC_OPENCLAW_CONTAINER` names it.
- [ ] OpenClaw configured to reach both MCP servers as MCP clients (the ES bridge and the
      SO gateway).
- [ ] A way to schedule the daily cycle (`SOC_CYCLE_CRON`, default daily at noon;
      `SOC_TZ`). OpenClaw has a managed cron, but a plain host cron is the more robust
      path; see [05-autonomous-cycle](05-autonomous-cycle.md).
- [ ] The `coding-agent` capability enabled, i.e. the `claude` binary on the container's
      PATH, so OpenClaw can spawn headless Claude Code for the autonomous cycle and the IR
      team. The in-container install paths are `SOC_AGENT_HOME`, `SOC_CLAUDE_BIN`,
      `SOC_CLAUDE_ENV`, and `SOC_CLAUDE_CONFIG_DIR`.

## 4. Claude Code + a model backend

The autonomous cycle and the IR team run as headless Claude Code (`claude -p`) inside the
OpenClaw container. Claude Code needs a backend, and there are two ways to give it one:

**Mode A: an Anthropic API key (recommended).** Cloud Claude drives the analysis.

- [ ] Claude Code installed inside the OpenClaw container (reachable at `SOC_CLAUDE_BIN`).
- [ ] An Anthropic API key in a `claude.env` file (`SOC_CLAUDE_ENV`), sourced by the
      container at startup, with an isolated config dir (`SOC_CLAUDE_CONFIG_DIR`). Keep
      this file tightly permissioned (`0600`, encrypted at rest if you can).
- [ ] `SOC_CLOUD_MODEL` set to a cloud Claude model (e.g. `anthropic/claude-sonnet-5`).
- [ ] If the OpenClaw container runs as root, set `IS_SANDBOX=1` so Claude Code's
      `--permission-mode bypassPermissions` is permitted.

**Mode B: a local Anthropic-compatible endpoint (experimental).** Since ollama 0.14
(January 2026), ollama natively speaks the Anthropic Messages API, so Claude Code can run
against a local model with no Anthropic account at all. The `claude.env` becomes:

```bash
export ANTHROPIC_BASE_URL="http://<ollama-host>:11434"
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_DEFAULT_SONNET_MODEL="<your-local-model>"   # the tier the cycle asks for
export CLAUDE_CONFIG_DIR="..."   # unchanged
export IS_SANDBOX=1              # unchanged
```

Everything else in the suite works identically: same allowlists, same user-scoped MCPs,
same prompts, because it's still Claude Code. (Interactively, `ollama launch` on 0.14.5+
automates the same wiring.)

Now the caveats, and take them seriously. The reason this suite defaults to cloud Claude
was never licensing; it was capability. The cycle is a dozens-of-tool-calls analyst run
inside Claude Code's heavy harness. On the source deployment a 30B coder model looped its
tool calls and never finished a cycle, and a local 12B looped a single exec call badly
enough to kill three days of runs. If you go local: use the strongest tool-calling model
you can serve, give it at least a 32K context window (16K overflows the assembled prompt;
more is better), expect briefings that need more skepticism, and verify the whole
propose-token flow end to end before trusting a verdict. Mode B removes the Anthropic
dependency; it does not repeal the capability requirement.

> The interactive `soc` agent is a separate, easier decision; see
> [04-openclaw-setup](04-openclaw-setup.md) §3.

## 5. A Discord server + bot

Delivery and operator interaction happen in Discord, through OpenClaw's existing two-way
bot (a real bot, not a webhook).

- [ ] A Discord server with OpenClaw's bot installed and connected.
- [ ] A dedicated channel for SOC briefings and approvals. Set its ID in
      `SOC_DISCORD_CHANNEL` (format `channel:<id>`).
- [ ] The SOC channel bound to the `soc` agent in OpenClaw, so approvals are handled by a
      model with reliable tool calling.

The cycle posts a single briefing (full report attached) to this channel; you drive the
agent from here with `approve <token>`, `revert <handle>`, `list tunings`,
`investigate <id>`, and `dismiss <id>`.

## 6. Optional: threat-intel API keys

The enrichment tier in `mcp-so-gateway` works without any keys. Keyless feeds (Tor-exit,
Feodo Tracker, Spamhaus-DROP, DShield, blocklist.de) cover the baseline. Adding keys
enables the richer keyed providers.

- [ ] (Optional) API keys for OTX, AbuseIPDB, and/or VirusTotal. These live in the
      gateway's `ti.env`, not in `soc-suite.env`.

> Keys are the privacy and cost throttle: only external IPs are ever sent for enrichment
> (RFC1918 is dropped), and you enable exactly the providers you are comfortable querying.
> Leave them out to stay fully on keyless feeds.

## 7. Skills tooling

The suite's behavior layer is the `soc-analyst` skill: environment grounding, triage
methodology, and the query cookbook.

- [ ] Install the `soc-analyst` skill into both Claude Code and OpenClaw, so the headless
      cycle and the interactive `soc` agent share the same behavior.

---

## What this is NOT

- Not a turnkey appliance. There is no single installer that produces a running SOC agent
  from nothing.
- It assumes you run this stack (OpenClaw + Claude Code + Discord + Docker) in front of an
  existing Security Onion. If you do not run this combination, this suite is not a drop-in
  fit.
- It does not install or manage Security Onion, OpenClaw, Docker, Claude Code, or your
  model backend. Those are your prerequisites.
- You must work through the dependency steps (the rest of these docs) in order; later docs
  assume each item above is already satisfied.

---

## Next steps

Once the items above are in place, continue with
[`02-security-onion-setup.md`](./02-security-onion-setup.md): creating the service
account, the read-only ES API key, and confirming SO is reachable from your Docker host.
