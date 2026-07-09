# Environment grounding — FILL THIS IN for your deployment

> The `soc-analyst` skill reads this file at the start of every triage. The shipped version is a
> **template with placeholders**. Replace every `<…>` with your environment's real values, then
> delete this banner. A complete worked example is in
> [environment.example.md](environment.example.md) — copy its shape, not its values.

## Network

- LAN: **`<your LAN CIDR, e.g. 10.0.0.0/24>`**. `observer.name` on SO data is `<your-sensor-name>`.
- Anything outside the LAN CIDR is external; RFC1918 is internal.

## Host table

| Host | Role — context for triage |
|---|---|
| `<ip>` | `<hostname>` — `<role; what egress/behavior is EXPECTED here>` |
| `<ip>` | `<hostname>` — `<…>` |
| `<ip>` | broadcast. |

## Highest-privilege host (deepest scrutiny)

**`<host / ip>`** (`<user>`): `<why it is the highest-value target — keys, agentic tooling, admin
access, browsing>`. It gets the deepest look and an explicit deviation check on every cycle.

## Known-noisy-but-benign (don't escalate the expected)

- `<signature / traffic pattern that is normal here, and why>`
- `<…>`

State what is genuinely abnormal for your network too (e.g. lateral SMB/RDP between clients,
LOLBin execution from a user folder, beaconing to a rare external IP).

## Documented false-positive baselines (contextualize each NARROWLY)

For each recurring benign pattern, record the **exact** signature so suppression stays
behavior-specific: which host, which rule(s), the precise parent process / workdir / command shape,
and the deviation check that distinguishes "explained" from "worth escalating".

- **`<host>` — `<baseline name>`:** `<exact benign signature + how to recognize a deviation>`

## Telemetry coverage (state current coverage each cycle)

- `<what network/endpoint telemetry you actually collect — Zeek tables, firewall logs, DNS,
  endpoint agents on which hosts>`
- **Residual blind spots to name when one bounds a conclusion:** `<VPN/tunneled egress,
  container east-west, DoH/DoT, east-west unicast, hosts without an endpoint agent, …>`

## Signature-update health (optional)

`<if you installed security-onion/rule-update-health/, note the so-rule-update-health ES doc the
cycle reads and the freshness threshold; otherwise remove this section>`
