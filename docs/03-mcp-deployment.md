# 03 — MCP server deployment

The SOC agent reaches Security Onion through two MCP servers, both running as
containers on `SOC_DOCKER_HOST` (typically the same box as OpenClaw). This doc
deploys them. All site-specific values come from `config/soc-suite.env` — source
it first:

```bash
. config/soc-suite.env
```

## 1. Overview

| Container | Port (`host:container`) | Endpoint | Role |
|-----------|-------------------------|----------|------|
| `mcp-elasticsearch` | `SOC_ES_MCP_PORT:???` (9220) | `http://$SOC_DOCKER_HOST:9220/mcp` | Read-only bridge to SO's Elasticsearch |
| `mcp-so-gateway` | `SOC_SO_GATEWAY_PORT:8080` (9221) | `http://$SOC_DOCKER_HOST:9221/mcp` | SO Core API: read tools, gated tuning writes, TI enrichment |

Both speak the streamable-HTTP MCP transport and expose their endpoint at
`http://host:port/mcp`.

The **gateway is the only component that holds the SO write credential and the
only one that writes to SO.** The Elasticsearch bridge is strictly read-only.

> Under the trusted-LAN model these endpoints are unauthenticated and
> LAN-reachable (each server holds its own upstream credential; clients connect
> with no token). That is acceptable only on a trusted LAN — tighten it if that
> assumption changes.

## 2. `mcp-elasticsearch` (read-only bridge)

This bridges agents to Security Onion's Elasticsearch, **read-only**: it exposes
search / ES|QL / `get_mappings` / `list_indices` (and `get_shards`) tools, all
marked `readOnlyHint: true`. It is the agent's path for surveying alerts and
pivoting through telemetry.

It is a **separate, standard read-only ES MCP container**, not part of this
suite's tree. Elastic's official image works and is what the source deployment
runs (`bin/install.sh gateways` deploys it):

```bash
# es.env (0600):  ES_URL=$SOC_SO_ES_URL  ES_API_KEY=<key>  ES_SSL_SKIP_VERIFY=true
docker run -d --name mcp-elasticsearch --restart unless-stopped \
  --env-file ./es.env \
  -p "$SOC_ES_MCP_PORT:8080" docker.elastic.co/mcp/elasticsearch:latest
```

- Point it at `SOC_SO_ES_URL` with an SO Elasticsearch **API key** (read-only
  role: cluster monitor + index read/view_index_metadata/monitor).
- The server holds the ES key; MCP clients connect with no token.

Health check:

```bash
curl http://$SOC_DOCKER_HOST:$SOC_ES_MCP_PORT/ping   # -> Ready
```

A direct-curl fallback (when MCP tools aren't loaded in a session) hits
`SOC_SO_ES_URL` with the ES API key directly. The exact image, env-file
(`es.env` holding the ES URL + key), and run command are the reader's per the
standard read-only ES MCP pattern — keep the key in an env file, not on the
command line.

## 3. `mcp-so-gateway` (this suite)

The gateway lives at `onionclaw/mcp-so-gateway/`. It is a Python 3.12 +
FastMCP server that bridges agents to the **Security Onion Core API**: read
tools, human-gated reversible tuning writes, and threat-intel enrichment.

### 3.1 Build the image

From the gateway directory:

```bash
cd onionclaw/mcp-so-gateway
docker build -t mcp-so-gateway:latest .
```

### 3.2 Create the two env files (chmod 0600)

Credentials live in **two `0600` env files outside this tree**, each passed to
the container with `--env-file`. Keep both out of version control (see §5).

`so.env` — the SO service-account credential:

```ini
SO_URL=https://<SOC_SO_HOSTNAME>          # your SOC_SO_URL
SO_EMAIL=<so-service-account-email>
SO_PASSWORD=<so-service-account-password>
SO_SSL_SKIP_VERIFY=true                   # SO uses a self-signed cert
```

`ti.env` — the keyed threat-intel keys (see §4 for shape):

```ini
TI_OTX_API_KEY=<otx-key>
TI_ABUSEIPDB_API_KEY=<abuseipdb-key>
TI_VT_API_KEY=<virustotal-key>
TI_ENABLE_FEEDS=true
```

Lock them down:

```bash
chmod 600 so.env ti.env
```

The SO service account should be a least-privilege `analyst`-role account — the
gateway's RBAC is enforced upstream by SO.

### 3.3 Environment variables (gateway)

The gateway reads these (`SO_*` from `so.env`, `TI_*` from `ti.env`; ports/host
and audit DB from defaults or the run command):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SO_URL` | yes | — | Base URL of the Security Onion instance (your `SOC_SO_URL`) |
| `SO_EMAIL` | yes | — | Service-account login email |
| `SO_PASSWORD` | yes | — | Service-account login password |
| `SO_SSL_SKIP_VERIFY` | no | `false` | Set `true` to disable TLS verification (self-signed SO cert) |
| `SO_AUDIT_DB` | no | `/data/tuning-audit.sqlite` | Path to the tuning audit/undo SQLite DB |
| `MCP_HOST` | no | `0.0.0.0` | Bind host |
| `MCP_PORT` | no | `8080` | Bind port (published as `SOC_SO_GATEWAY_PORT`) |
| `TI_OTX_API_KEY` | no | — | AlienVault OTX key; provider disabled if unset |
| `TI_ABUSEIPDB_API_KEY` | no | — | AbuseIPDB key; provider disabled if unset |
| `TI_VT_API_KEY` | no | — | VirusTotal v3 key; provider disabled if unset |
| `TI_ENABLE_FEEDS` | no | `true` | Toggle the keyless feeds (Tor/Feodo/Spamhaus/DShield/blocklist.de) |
| `TI_CACHE_DB` | no | `/data/ti-cache.sqlite` | TI lookup cache (on the mounted volume) |
| `TI_TTL_SECONDS` | no | `21600` | Cache TTL for keyed-provider records (6h) |
| `TI_FEED_TTL_SECONDS` | no | `21600` | Cache TTL for keyless feed snapshots (6h) |
| `TI_HTTP_TIMEOUT` | no | `20` | Per-request HTTP timeout (seconds) |

### 3.4 Run the container

```bash
# from onionclaw/mcp-so-gateway/, with config sourced:
. ../config/soc-suite.env

docker run -d --name mcp-so-gateway --restart unless-stopped \
  --add-host "$SOC_SO_HOSTNAME:$SOC_SO_IP" \
  --env-file ./so.env \
  --env-file ./ti.env \
  -v "$PWD/data:/data" \
  -p "$SOC_SO_GATEWAY_PORT:8080" mcp-so-gateway:latest
```

The MCP endpoint is then at `http://$SOC_DOCKER_HOST:$SOC_SO_GATEWAY_PORT/mcp`.

**Why `--add-host`:** SO's nginx checks the `Host` header against its
`server_name`. Requests must arrive under the hostname SO expects
(`SOC_SO_HOSTNAME`), not the bare IP, or nginx rejects them (a 307/redirect).
`--add-host "$SOC_SO_HOSTNAME:$SOC_SO_IP"` makes that name resolve inside the
container without touching the host's `/etc/hosts`.

**Why the `-v …/data:/data` volume:** the tuning **audit/undo DB**
(`SO_AUDIT_DB`, default `/data/tuning-audit.sqlite`) and the **TI cache**
(`TI_CACHE_DB`, default `/data/ti-cache.sqlite`) persist there, surviving
container recreates. Every applied write is recorded in the audit DB with the
exact prior state, which is what makes `revert_tuning` possible.

## 4. Threat-intel providers

The gateway enriches external IOCs in-process via `enrich_iocs` /
`extract_iocs` / `ti_provider_status`. Providers split two ways:

- **Keyed** (key required, provider self-disables if its key is unset):
  - **OTX** (AlienVault) — IP / domain
  - **AbuseIPDB** — IP
  - **VirusTotal v3** — IP / domain / hash
- **Keyless** (public lists / no-key APIs, toggled as a group via
  `TI_ENABLE_FEEDS`):
  - Tor exit-node list, Feodo Tracker C2 blocklist, Spamhaus DROP, DShield/ISC,
    blocklist.de

**The enabled-provider set is the privacy/cost throttle.** Each lookup ships one
IOC to a third party, so you enable only what you accept paying out:

- Only the indicator **value** is sent to a provider's lookup API — the tools
  never fetch an attacker URL, never follow redirects, never execute anything
  from a response.
- **RFC1918 / internal IPs are dropped** by the extractor and never leave the
  network.
- Every lookup is **cache-checked** (`/data/ti-cache.sqlite`, 6h TTL) then
  **rate-limited** before any external call (VirusTotal paced to its 4 req/min
  free tier). Results are merged by a **deterministic aggregator** (not an LLM)
  into one consensus verdict per IOC.

`ti.env` shape (placeholder keys; **an unset key = that provider disabled**):

```ini
# Keyed providers — omit a line to disable that provider
TI_OTX_API_KEY=<otx-key>
TI_ABUSEIPDB_API_KEY=<abuseipdb-key>
TI_VT_API_KEY=<virustotal-key>

# Keyless feeds (Tor / Feodo / Spamhaus / DShield / blocklist.de) — group toggle
TI_ENABLE_FEEDS=true

# Polite User-Agent for the keyless feeds — DShield asks for contact info.
TI_USER_AGENT=soc-ti-enrichment/1.0 (you@example.com)
```

`ti_provider_status` reports which providers are enabled without exposing any
secret.

> **MISP is intentionally not included.** It's a self-hosted TI platform
> (standing up an instance + feed curation + upkeep), out of scope for this
> in-process pipeline — parked as a possible future addition.

## 5. Securing the env files

- Keep `so.env` and `ti.env` **out of version control.**
- If you must version them, **encrypt with SOPS** (commit only the encrypted
  form, decrypt to a `0600` plaintext at deploy time). This doc just notes the
  pattern; configure SOPS per your key-management setup.
- Both files should be `chmod 600`. The SO credential and TI keys live **only**
  in the container env — never in agent context or prompts.

## 6. Verify

Gateway endpoint reachable:

```bash
curl http://$SOC_DOCKER_HOST:$SOC_SO_GATEWAY_PORT/mcp   # MCP endpoint responds
```

Smoke-test the gateway code (from `onionclaw/mcp-so-gateway/`):

```bash
uv run pytest      # installs dev deps + runs the test suite
```

Live tool check: once a client is connected, the `ping` tool returns `"Ready"`.
A successful `get_detection` against a real `rule.uuid` confirms the SO Core API
path (auth + `--add-host`) end-to-end.

If tools start failing with a JSON-parse error after a long uptime, the SO
session likely expired; the gateway self-heals a stale session by
re-authenticating and retrying once, and `docker restart mcp-so-gateway` is the
manual fallback.

## Next steps

Wire these MCP servers into the autonomous agent: **[04-openclaw-setup.md](04-openclaw-setup.md)**.
