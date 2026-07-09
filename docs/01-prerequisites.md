# 01 — Prerequisites

`soc-agent-suite` installs an **autonomous SOC analyst** on top of a Security
Onion deployment. The agent triages alerts, reports honestly, proposes
(operator-gated) Security Onion tunings, enriches with threat intel, can escalate
to a read-only IR agent-team, and delivers to Discord.

It is **not** self-contained. It assumes a specific stack — the
"OpenClaw-centric" deployment model — where headless Claude Code runs the
analysis cycle inside an OpenClaw container, on your Claude subscription, and
talks to Security Onion through two MCP containers. This page lists everything
you need in place **before** starting.

Configurable values are referenced by their `SOC_*` names throughout. Those live
in [`config/soc-suite.env.example`](../config/soc-suite.env.example) — you copy it
to `soc-suite.env` and fill in your site's values during setup. No secrets go in
that file.

---

## At-a-glance checklist

| # | You need | Required? | Key config |
|---|----------|-----------|------------|
| 1 | Security Onion 2.4+ (reachable, ES + Core API) | Required | `SOC_SO_*` |
| 2 | A Docker host for the two MCP containers | Required | `SOC_DOCKER_HOST`, `SOC_ES_MCP_PORT`, `SOC_SO_GATEWAY_PORT` |
| 3 | OpenClaw (self-hosted assistant gateway) | Required | `SOC_OPENCLAW_CONTAINER`, `SOC_AGENT_HOME` |
| 4 | A Claude subscription + Claude Code | Required | `SOC_CLAUDE_*`, `SOC_CLOUD_MODEL` |
| 5 | A Discord server + OpenClaw's bot + a SOC channel | Required | `SOC_DISCORD_CHANNEL` |
| 6 | Threat-intel API keys (OTX / AbuseIPDB / VirusTotal) | Optional | — (in the gateway's `ti.env`) |
| 7 | The `soc-analyst` skill installed | Required | — |

---

## 1. A working Security Onion deployment

The suite reads from and (operator-gated) writes to an existing **Security Onion
2.4+** install. It does **not** stand one up for you.

- [ ] Security Onion **2.4 or newer**, deployed and healthy. It can be a VM, a
      standalone box, or a manager+sensor grid — anything that is **reachable
      over the network** from your Docker host (item 2). Install it per the
      official docs: <https://docs.securityonion.net>.
- [ ] **Elasticsearch** reachable (typically port `9200`). A stock SO install
      keeps ES **internal to the grid** — you must grant the Docker host access
      via SO's firewall hostgroups (`so-firewall includehost elasticsearch_rest
      …`; the full recipe is [02-security-onion-setup §3](./02-security-onion-setup.md)).
      Set `SOC_SO_ES_URL` to this endpoint. Self-signed TLS is expected (the
      bridge skips verification).
- [ ] The **SO Core API** reachable over HTTPS (`SOC_SO_URL`). SO's nginx checks
      the `Host` header, so the gateway must address SO by the **name SO
      expects** — set `SOC_SO_HOSTNAME` and `SOC_SO_IP` so the gateway container
      can map the name to the IP.
- [ ] **Admin access** to SO, so you can:
  - create a **dedicated service account** (an analyst-role account) for the
    agent — **do not** use your personal SO login;
  - mint a **read-only Elasticsearch API key** for the ES bridge (cluster
    `monitor`; indices `read` / `view_index_metadata` / `monitor`).
- [ ] Know your **internal LAN CIDR(s)** (`SOC_LAN_CIDR`) — the analyst uses this
      to reason about traffic direction and to keep RFC1918 addresses out of
      threat-intel lookups.

> Why a service account: the agent's reads and its operator-gated writes should
> be attributable and revocable independently of any human login.

## 2. A Docker host for the MCP containers

Two MCP servers bridge the agent to Security Onion. Both run as containers,
typically on the **same host that runs OpenClaw**.

- [ ] A **Docker host** (`SOC_DOCKER_HOST`, by hostname or IP) that can reach
      Security Onion (item 1).
- [ ] Two free, LAN-reachable ports published from that host:

| Container | Purpose | Port |
|-----------|---------|------|
| `mcp-elasticsearch` | **Read-only** bridge to SO's Elasticsearch (`search`, `esql`, `list_indices`, `get_mappings`, `get_shards`) | `SOC_ES_MCP_PORT` (default **9220**) |
| `mcp-so-gateway` | This suite's bridge to the **SO Core API** — detections/playbooks, the operator-gated tuning write path, and threat-intel enrichment | `SOC_SO_GATEWAY_PORT` (default **9221**) |

- [ ] The SO credentials these containers hold (the ES API key and the service
      account) live in the **gateway's own env files** (`so.env` / `ti.env`),
      **not** in `soc-suite.env`. Standing those up is covered in
      `docs/03-mcp-deployment.md`.

> Trust model: the MCP endpoints are unauthenticated but LAN-reachable — the
> server holds the credentials, clients connect with no token. This is fine
> under a trusted-LAN assumption; tighten it if that assumption does not hold.

## 3. OpenClaw

[OpenClaw](https://docs.openclaw.ai) is a **self-hosted personal-assistant
gateway**. It is the runtime that ties the suite together — it runs the agents,
the MCP client wiring, the managed cron, and the Discord bot, and it provides the
`coding-agent` capability that spawns headless Claude Code.

- [ ] A working **OpenClaw** instance — typically a **Docker container on the same
      host** as the MCP containers (item 2). `SOC_OPENCLAW_CONTAINER` names it.
- [ ] OpenClaw configured to reach **both** MCP servers as MCP clients (the ES
      bridge and the SO gateway).
- [ ] OpenClaw's **built-in managed cron** available — the cycle is scheduled
      here (`SOC_CYCLE_CRON`, default daily at noon; `SOC_TZ`), pinned to the
      dedicated `soc` agent so it runs on a reliable cloud model.
- [ ] The **`coding-agent` capability enabled** — i.e. the `claude` binary on the
      container's PATH — so OpenClaw can spawn headless Claude Code for the
      autonomous cycle and the IR team. The in-container install paths are
      `SOC_AGENT_HOME`, `SOC_CLAUDE_BIN`, `SOC_CLAUDE_ENV`, and
      `SOC_CLAUDE_CONFIG_DIR`.

## 4. A Claude subscription + Claude Code

The autonomous cycle and the IR agent-team run as **headless Claude Code
(`claude -p`) inside the OpenClaw container**, authenticated with your **Claude
subscription** (an OAuth token), *not* an Anthropic API key.

- [ ] An active **Claude subscription** with **Claude Code** installed inside the
      OpenClaw container (reachable at `SOC_CLAUDE_BIN`).
- [ ] The subscription **OAuth token** available to the container via a
      `claude.env` file (`SOC_CLAUDE_ENV`), sourced by the container at startup,
      with an isolated config dir (`SOC_CLAUDE_CONFIG_DIR`). Keep this file
      tightly permissioned (e.g. `0600`, encrypted at rest).
- [ ] `SOC_CLOUD_MODEL` set to a **cloud Claude** model (e.g.
      `anthropic/claude-sonnet-5`).
- [ ] If the OpenClaw container runs **as root**, set **`IS_SANDBOX=1`** so
      Claude Code's `--permission-mode bypassPermissions` is permitted.

> **Why a subscription + cloud Claude, not a local model?** The cycle is
> tool-heavy — it has to drive ES queries, playbook lookups, and the gateway
> reliably. The roadmap found that **local/heavy models loop on tool calls**
> (the then-heavy `qwen3-coder:30b` looped and never completed the cycle); the
> cycle drives tools reliably **only on cloud Claude**. The SOC cycle stays on
> the subscription regardless of what the rest of OpenClaw uses.

## 5. A Discord server + bot

Delivery and operator interaction happen in Discord, through **OpenClaw's
existing two-way bot** (a real bot, not a webhook).

- [ ] A **Discord server** with **OpenClaw's bot** installed and connected.
- [ ] A **dedicated channel** for SOC briefings and approvals — set its ID in
      `SOC_DISCORD_CHANNEL` (format `channel:<id>`).
- [ ] The SOC channel **bound to the `soc` agent** in OpenClaw so it runs on the
      reliable cloud model.

The cycle posts a single briefing (full report attached) to this channel; the
operator drives the agent from here — e.g. `approve <token>`, `revert <handle>`,
`list tunings`, `investigate <id>`, `dismiss <id>`.

## 6. Optional — threat-intel API keys

The enrichment tier in `mcp-so-gateway` works **without any keys** — keyless
feeds (Tor-exit, Feodo Tracker, Spamhaus-DROP, DShield, blocklist.de) cover the
baseline. Adding keys enables the richer **keyed** providers.

- [ ] (Optional) API keys for **OTX**, **AbuseIPDB**, and/or **VirusTotal**.
      These live in the gateway's `ti.env`, **not** in `soc-suite.env`.

> Keys are the **privacy/cost throttle**: only **external** IPs are ever sent for
> enrichment (RFC1918 is dropped), and you enable exactly the providers you are
> comfortable querying. Leave them out to stay fully on keyless feeds.

## 7. Skills tooling

The suite's behavior layer is the **`soc-analyst` skill** — environment
grounding, triage methodology, and the query cookbook.

- [ ] Install the **`soc-analyst` skill into both Claude Code and OpenClaw**, so
      the headless cycle and the interactive `soc` agent share the same behavior.

---

## What this is NOT

- **Not a turnkey appliance.** There is no single installer that produces a
  running SOC agent from nothing.
- It **assumes you run this stack** — OpenClaw **+** Claude subscription **+**
  Discord **+** Docker, in front of an existing Security Onion. If you do not run
  this combination, this suite is not a drop-in fit.
- It does **not** install or manage Security Onion, OpenClaw, Docker, or your
  Claude subscription — those are **your** prerequisites.
- You must **work through the dependency steps** (the rest of these docs) in
  order; later docs assume each item above is already satisfied.

---

## Next steps

Once the items above are in place, continue with
[`02-security-onion-setup.md`](./02-security-onion-setup.md) — creating the
service account, the read-only ES API key, and confirming SO is reachable from
your Docker host.
