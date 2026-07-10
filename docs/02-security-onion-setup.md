# 02: Security Onion setup

Everything on the SO side that has to happen before the MCP servers can talk to it: a
dedicated service account, punching two holes in SO's firewall (it firewalls everything
by default, which is correct behavior and also the number-one reason nothing works on a
fresh wiring), and a couple of optional quality-of-life helpers for the SO box itself.

All deployment-specific values are referenced by their `SOC_*` config variable (defined in
`config/soc-suite.env`, see `config/soc-suite.env.example`). Substitute your own values.
Anything below that names a path or version is standard SO 2.4 behavior; adjust for your
install where noted.

---

## 1. Overview

The suite reaches Security Onion two ways, and both need credentials:

| Path | Component | Direction | Auth |
|---|---|---|---|
| Elasticsearch | `mcp-elasticsearch` bridge (`SOC_ES_MCP_PORT`) | read-only | ES API key / user |
| Core API (SOC server) | `mcp-so-gateway` (`SOC_SO_GATEWAY_PORT`) | read and write (tuning) | SO web/Kratos login |

- The read-only Elasticsearch path bridges SO's Elastic store (`SOC_SO_ES_URL`, typically
  `:9200`) for searching alerts, events, and telemetry. It never writes.
- The Core API path drives SO's SOC server: it reads detections and playbooks, runs
  guided-analysis queries, and applies the tuning writes the suite uses
  (`PUT /api/detection` overrides, `POST /api/events/ack`).

Keep credentials out of `soc-suite.env`. SO and threat-intel secrets live in the gateway's
own `so.env` / `ti.env` (see `docs/03-mcp-deployment.md`).

---

## 2. Create a SOC service account

Stand up a dedicated SO user for the gateway rather than reusing your personal web/console
login. This keeps the agent's API activity auditable and lets you disable it
independently.

Give the account the `analyst` role. The analyst role can:

- read detections (`GET /api/detection/...`) and playbooks
  (`GET /api/playbook/detection/{publicId}`),
- run guided-analysis / events queries, and
- apply the tuning writes the gateway uses: `PUT /api/detection` (suppress / threshold /
  modify / disable via the `overrides[]` array) and `POST /api/events/ack` (disposition /
  escalate). This was verified live; an analyst account's `apply` PUT returned `200`.

SO identities are Ory Kratos accounts keyed by email, managed with the `so-user` CLI on
the manager node. The short form:

```bash
# On the SO manager node, as a sudoer:
sudo so-user add --email <soc-service-account>@<your-so-domain> --role analyst
# (the username MUST be a valid email; password can be piped via STDIN)
sudo so-user list                      # confirm it exists with the analyst role
```

> Notes
> - `so-user` has no delete. Accounts can only be disabled, so type the email carefully.
> - A notorious "salt is not running" error from `so-user` is usually just an invalid
>   (non-email) username.
> - Use a placeholder like `<soc-service-account>@<your-so-domain>` and a strong generated
>   password. Never commit a real email or password.
> - Keep your own web/console login separate from this service account; the gateway only
>   needs the service account.

Record the service account's credentials in the gateway's `so.env` (encrypted / out of
version control), not in `soc-suite.env`.

---

## 3. Open SO's internal-only ports (firewall hostgroups). REQUIRED.

A stock SO 2.4 install firewalls everything it doesn't itself need: the web UI / Core API
(`:443`) answers only IPs you granted at install time, and Elasticsearch `:9200` is
internal to the grid. Neither MCP container can connect until you explicitly grant the
Docker/OpenClaw host access. This is the single most common "nothing works" cause on a
fresh wiring.

SO manages its iptables through Salt: a grant is a hostgroup membership, recorded durably
in the local pillar (`/opt/so/saltstack/local/pillar/firewall/soc_firewall.sls`) so it
survives `soup`, highstate, and reboots. Never hand-edit iptables; Salt will overwrite it.
Grant with the `so-firewall` CLI on the SO manager (or in the SOC web UI:
Administration → Configuration → firewall):

```bash
# On the SO manager. Prefer a /32 for the one Docker host over a whole subnet.
sudo so-firewall includehost analyst <docker-host-ip>/32            # 443: web UI + Core API
sudo so-firewall includehost elasticsearch_rest <docker-host-ip>/32 # 9200: Elasticsearch REST
sudo so-firewall apply                                              # applies the salt state
```

What the suite needs, hostgroup by hostgroup:

| Hostgroup | Opens | Needed by |
|---|---|---|
| `analyst` | `443` (SOC web + Core API) | required: `mcp-so-gateway` (reads + tuning writes) |
| `elasticsearch_rest` | `9200` (Elasticsearch REST) | required: the `mcp-elasticsearch` read-only bridge |
| `syslog` | `514` tcp/udp | optional: the host running `orchestration/soc-log-forwarder.py` |
| `elastic_agent_endpoint` | agent enroll/data ports | optional: LAN hosts you enroll Elastic Agents on |

For a source that ships on a nonstandard port (e.g. a router that can only syslog to a
custom UDP port), create a custom pairing: `sudo so-firewall addhostgroup <name>` +
`includehost <name> <ip>`, then define the matching portgroup and chain assignment in the
SOC UI (Administration → Configuration → firewall). The result lands in the same local
pillar.

Verify both sides after `apply`:

```bash
# on SO: the grants materialized in iptables
sudo iptables -L INPUT -n | grep -E '9200|443'
# from the Docker host: 401 = reachable (auth required); timeout = still blocked
curl -sk -o /dev/null -w '%{http_code}\n' "$SOC_SO_ES_URL"
```

> The source deployment granted its whole LAN CIDR to `analyst` / `elasticsearch_rest` for
> convenience; a per-host `/32` for the Docker host is the tighter default. Grants are
> additive; `so-firewall removehost <ip>` removes an IP from all hostgroups.

---

## 4. API & Elasticsearch reachability

### Core API (the gateway)

The gateway authenticates the way SO 2.4's SOC server actually requires, and the two auth
paths are not interchangeable:

- The Bearer / API flow is GET-only. A `Bearer` token works for reads, but POST/PUT return
  `400 "The request could not be processed."`
- The browser (Kratos) flow + CSRF is required for any write. The gateway logs in via the
  SO web/Kratos browser flow, then carries the resulting `X-Srv-Token` (a JWT read from
  `GET /api/info`) on subsequent requests. This is the working write path for
  `PUT /api/detection` and `POST /api/events/ack`.

Because SO's nginx checks the Host header, requests must use SO's configured `server_name`
(`SOC_SO_HOSTNAME`), not a bare IP, or the login flow fails. This is why the gateway
container is launched with a Docker `--add-host` mapping `SOC_SO_HOSTNAME` → `SOC_SO_IP`
(wired up in `docs/03-mcp-deployment.md`). The gateway's base URL is `SOC_SO_URL`.

For the verified endpoint contract (ID gotchas, request shapes, the auth recipe), see the
`so-core-api` reference. Don't invent endpoints beyond what it documents.

### Elasticsearch (the read-only bridge)

The `mcp-elasticsearch` server bridges SO's Elastic store at `SOC_SO_ES_URL` (typically
`https://<SOC_SO_IP>:9200`). SO ships a self-signed certificate, so the bridge must skip
TLS verification:

```bash
SO_SSL_SKIP_VERIFY=true          # SO uses a self-signed cert on :9200
```

Use a read-only ES API key/role for this path (cluster monitor + index `read` /
`view_index_metadata` / `monitor`).

> If the bridge can't connect at all (timeout, not 401), the Docker host is missing from
> the `elasticsearch_rest` hostgroup; see §3 above.

---

## 5. Index / data-stream notes

SO stores telemetry in Elasticsearch data streams, not classic time-rolled indices. When
you query, target the bare data-stream name (e.g. the Suricata and detection alert
streams) and avoid middle-wildcard patterns, which match the backing `.ds-*` indices
unreliably. For the full naming map, the ECS fields, and worked query recipes, see the
`soc-analyst` skill's `elastic-queries` reference (`skill/`); this doc deliberately does
not reproduce the cookbook.

---

## 6. Optional SO-host helpers (`security-onion/`)

The suite ships two optional hardening/observability helpers that live on the Security
Onion box itself (a separate machine from the Docker/OpenClaw host), versioned here for
recoverability. Neither is required to run the gateway or skill. Install them on the SO
manager, e.g. `ssh <you>@$SOC_SO_IP` with sudo. Full detail is in
`security-onion/README.md`.

### (a) chrony `makestep` override: guest-clock resync after a hypervisor pause

If SO runs as a VM, a hypervisor pause (e.g. on a host-disk-full event) freezes the guest
clock. chrony's default `makestep 1.0 3` only steps the clock in the first 3 updates after
start, so a mid-run pause is never corrected. The guest drifts behind, which silently
back-timestamps all ingest, so every `now-X` query and the scheduled cycle miss "today".
The override sets `makestep 1.0 -1` (step at any poll when offset > 1s) as an SO-local
salt override that survives `soup`/highstate.

```bash
sudo cp chrony.conf /opt/so/saltstack/local/salt/ntp/chrony.conf
sudo salt-call state.apply ntp
timedatectl show -p NTPSynchronized --value   # -> yes
```

### (b) `so-rule-update-health`: a monitor for the daily signature update

`so-rule-update` (idstools-rulecat) refreshes Suricata ET rules daily (~07:01) and nothing
watches it by default, so a failed or stale run is silent. This monitor reads the run log
(read-only) and writes one ES doc (`so-rule-update-health/_doc/latest`, fixed id so the
index never grows) reporting `status` / `age_hours` / `final_write_present` /
`rules_total` / `error_count`. The autonomous cycle reads that doc via the Elasticsearch
MCP to report signature freshness. It reuses SO's own ES `curl.config` (no new secrets)
and installs to `/etc/cron.d` so it survives `soup` and reboots.

```bash
sudo install -m 0755 -o root -g root rule-update-health/so-rule-update-healthcheck.sh /usr/local/bin/
sudo install -m 0644 rule-update-health/so-rule-update-health.cron /etc/cron.d/so-rule-update-health
sudo /usr/local/bin/so-rule-update-healthcheck.sh   # seed the doc
```

> The healthcheck hardcodes standard SO 2.4 paths
> (`/opt/so/log/idstools/download_cron.log`, `/opt/so/conf/elasticsearch/curl.config`,
> `https://localhost:9200`). Adjust only if your SO layout differs.

---

## 7. Storage resilience note (optional)

SO's Elasticsearch indices, especially the elastalert error/status indices, can balloon
when rules misbehave (e.g. a broken Sigma rule writing constant validation exceptions on
every execution loop) and fill the host disk, which on a VM can pause the guest. Cap
unmanaged indices with an ILM policy so they roll over and delete on a bounded schedule
instead of growing without limit. This is general guidance; consult Security Onion's own
documentation for ILM specifics and recommended retention for your storage budget.

---

## Next steps

Proceed to `docs/03-mcp-deployment.md` to deploy the `mcp-elasticsearch` bridge and the
`mcp-so-gateway` containers (including the `--add-host` mapping and the `so.env` /
`ti.env` credential files referenced above).
