# mcp-so-gateway

The gateway to Security Onion's Core API, and the one component in this whole system
that's allowed to touch it. It holds the only SO write credential; everything else reads.
Sibling to the read-only `mcp-elasticsearch` bridge. Tools are served over the
[streamable-HTTP MCP transport](https://modelcontextprotocol.io/docs/concepts/transports).

Deployment walkthrough: [../docs/03-mcp-deployment.md](../docs/03-mcp-deployment.md).

### Read tools

| Tool | Description |
|------|-------------|
| `ping` | Liveness check; returns `"Ready"` |
| `get_detection` | Fetch a detection by `publicId` (sid / Sigma UUID) or ES `_id` (auto-fallback) |
| `get_playbook` | Fetch the playbook for a detection (`rule.uuid`) |
| `run_guided_analysis` | Run SO's per-detection guided-analysis questions and return events |

### Write tools (approval-based, reversible)

| Tool | Writes? | Description |
|------|---------|-------------|
| `propose_tuning` | no | Validate + preview a tuning (suppress/threshold/modify/disable), estimate blast radius, issue a single-use token |
| `apply_tuning` | yes | Apply a proposed tuning (`PUT /api/detection`); records an undo record |
| `revert_tuning` | yes | Restore the captured prior state for an applied tuning |
| `list_tunings` | no | List currently applied tunings + undo handles |
| `disposition_alerts` | yes | Acknowledge (close) / escalate alerts for a rule (`POST /api/events/ack`); audited |

### Grounding tools (approval-based writes to environment.md; enabled by `GROUNDING_PATHS`)

| Tool | Writes? | Description |
|------|---------|-------------|
| `propose_grounding` | no | Validate + preview an environment.md entry (host_table / known_noisy / fp_baselines / coverage), issue a single-use token |
| `apply_grounding` | yes | Append the proposed entry under its section heading in every configured grounding file (atomic; multi-file rollback); records an undo |
| `revert_grounding` | yes | Remove exactly the inserted block (later hand-edits survive) |
| `list_groundings` | no | Applied grounding entries + undo handles |

These power the operator's `learn <entity>: <what it is>` flow: the analyst's model of
the network is a file the operator teaches, through the same token gate as tunings. The
service can only append under known headings; it can't rewrite or delete existing
grounding, and heading-bearing entries are rejected so an injected entry can't hijack the
file's structure. Off unless `GROUNDING_PATHS` is set.

### Threat-intel enrichment tools (read-only)

| Tool | Description |
|------|-------------|
| `enrich_iocs` | Enrich external IOCs (IPs/domains/hashes) into one reputation summary per IOC across the enabled providers. RFC1918/internal IPs are dropped by the extractor and never sent out. Cached + rate-limited. |
| `ti_provider_status` | Which providers are enabled (the privacy/cost throttle). No secrets. |
| `extract_iocs` | Deterministic type/dedupe of indicator strings (and what the RFC1918 filter dropped); no external call. |

The enabled-provider set is the privacy and cost throttle: a keyed provider with no key is
never called, and keyless feeds toggle as a group via `TI_ENABLE_FEEDS`. Each provider is
cache-checked (on-disk `/data/ti-cache.sqlite`, 6h TTL) then rate-limited before any
external call (VirusTotal paced to its 4 req/min free tier). Results are merged by a
deterministic aggregator (not an LLM) into a consensus verdict
(`malicious`/`suspicious`/`benign`/`unknown`), max score, contributing/conflicting
sources.

Enabled providers:
- Keyed: OTX (IP/domain), AbuseIPDB (IP), VirusTotal v3 (IP/domain/hash).
- Keyless: Tor exit list, Feodo Tracker C2 blocklist, Spamhaus DROP, DShield/ISC,
  blocklist.de.

Injection hygiene: IOCs come from untrusted alert data. The tools only send the indicator
VALUE to a provider's lookup API; they never fetch an attacker URL, never follow
redirects, and never execute anything from a response.

How the approval gate works: `propose_tuning` is read-only and injection-safe;
`apply_tuning` is the only tuning write and requires a single-use token from a prior
propose. The agent workflow layers the human gate on top. In Claude Code the native
permission prompt on `apply_tuning` is the structural gate; in OpenClaw the skill protocol
forbids calling `apply_tuning` until the operator affirms the surfaced token + blast
radius. `disable`/`modify` proposals return `double_gated: true` for a louder second
confirmation. Every applied write is logged in the SQLite audit DB (`SO_AUDIT_DB`, default
`/data/tuning-audit.sqlite`) with the exact prior state and is reversible via
`revert_tuning`.

The SO write mechanism: `PUT /api/detection` with the full detection object and a modified
`overrides[]` array (or `isEnabled` for disable). All writes ride the browser-flow
cookies + `X-Srv-Token` header (the API/Bearer flow only works for GETs). The client
self-heals a stale SO session: read and write paths re-authenticate and retry once on a
400/401/403 or a login redirect.

## Local development

```bash
uv run pytest                       # install dev deps + run the test suite
python -m so_gateway.server         # start the server locally (binds 0.0.0.0:8080 by default)
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SO_URL` | yes | | Base URL of the Security Onion instance (your `SOC_SO_URL`) |
| `SO_EMAIL` | yes | | Service-account login email |
| `SO_PASSWORD` | yes | | Service-account login password |
| `SO_SSL_SKIP_VERIFY` | no | `false` | Set `true` to disable TLS verification (self-signed SO cert) |
| `SO_AUDIT_DB` | no | `/data/tuning-audit.sqlite` | Path to the tuning audit/undo SQLite DB |
| `MCP_HOST` | no | `0.0.0.0` | Bind host |
| `MCP_PORT` | no | `8080` | Bind port (published as `SOC_SO_GATEWAY_PORT`) |
| `TI_OTX_API_KEY` | no | | AlienVault OTX key; provider disabled if unset |
| `TI_ABUSEIPDB_API_KEY` | no | | AbuseIPDB key; provider disabled if unset |
| `TI_VT_API_KEY` | no | | VirusTotal v3 key; provider disabled if unset |
| `TI_ENABLE_FEEDS` | no | `true` | Toggle the keyless feeds (Tor/Feodo/Spamhaus/DShield/blocklist.de) |
| `TI_CACHE_DB` | no | `/data/ti-cache.sqlite` | TI lookup cache (on the mounted volume) |
| `TI_TTL_SECONDS` | no | `21600` | Cache TTL for keyed-provider records (6h) |
| `TI_FEED_TTL_SECONDS` | no | `21600` | Cache TTL for keyless feed snapshots (6h) |
| `TI_HTTP_TIMEOUT` | no | `20` | Per-request HTTP timeout (seconds) |
| `GROUNDING_PATHS` | no | | Colon-separated in-container paths to environment.md copies; enables the grounding tools. Mount the containing directory, not the bare file (atomic rename) |

Credentials live in two 0600 env files outside this tree: `so.env` (the three `SO_*`
values) and `ti.env` (the keyed TI keys), both passed to the container as `--env-file`.
Keep them out of version control; if you version them, encrypt (e.g. SOPS). See
`../docs/03-mcp-deployment.md`.

## Build & run

```bash
docker build -t mcp-so-gateway:latest .

# Source your site config so the values below are filled in:
. ../config/soc-suite.env

docker run -d --name mcp-so-gateway --restart unless-stopped \
  --add-host "$SOC_SO_HOSTNAME:$SOC_SO_IP" \
  --env-file ./so.env \
  --env-file ./ti.env \
  -v "$PWD/data:/data" \
  -p "$SOC_SO_GATEWAY_PORT:8080" mcp-so-gateway:latest
```

The `--add-host` maps SO's nginx `server_name` to its IP; requests must use the hostname
SO expects (`SOC_SO_HOSTNAME`), not the bare IP, or nginx rejects them. The
`-v …/data:/data` volume persists the tuning audit DB and the TI cache across container
recreates. The MCP endpoint is then available at
`http://$SOC_DOCKER_HOST:$SOC_SO_GATEWAY_PORT/mcp`.
