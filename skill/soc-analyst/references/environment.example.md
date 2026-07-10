# Environment grounding: WORKED EXAMPLE

> This is a filled-in example modeled on the home-lab deployment this suite was built on
> (identifiers fictionalized). It shows the shape and depth of grounding the analyst
> needs. Replace it with your own by copying `environment.md` and editing every value. Do
> not ship someone else's host table as your grounding; it is wrong for your network and
> will misclassify your alerts.

## Network

- LAN: `192.168.1.0/24`, a trusted home LAN. `observer.name` on SO data is
  `securityonion`.
- Anything outside the LAN CIDR is external; RFC1918 is internal.

## Host table

| Host | Role and context for triage |
|---|---|
| `192.168.1.15` | **nas**: Unraid server running an assistant/LLM stack (OpenClaw, LiteLLM, ollama), ~25 containers, and the SO VM itself. Heavy, varied egress is expected (Docker pulls, model fetches, media stack, indexers via VPN). |
| `192.168.1.19` | **workstation**: operator's main workstation + secondary inference host. Highest-privilege host (below). Generates app traffic (e.g. Spotify P2P). |
| `192.168.1.50` | **securityonion**: the SO VM (sensor + Elastic). Self-traffic. |
| `192.168.1.53` | **pihole**: LAN DNS/adblock (macvlan, own IP). Most internal DNS funnels through it. |
| `192.168.1.135` | **gamepc**: family Windows gaming PC. |
| `192.168.1.255` | broadcast. |

## Highest-privilege host (deepest scrutiny)

workstation / `.19` (user `operator`): holds SSH keys into the NAS and the SO box, runs
broadly permissioned agentic tooling (Claude Code with shell/file/network access), and
browses the web. That makes it the largest attack surface and the prime target on the
LAN. It gets the deepest look and an explicit deviation check on every cycle.

## Known-noisy-but-benign (don't escalate the expected)

- ET INFO Suricata sigs (Spotify P2P, normal app telemetry).
- The media stack's indexer/torrent traffic egresses a gluetun VPN, so download traffic
  shows a VPN exit IP. Expected.
- Docker registry pulls; STUN/WebRTC for voice and gaming; enterprise-proxy CONNECT from
  managed work laptops.

Conversely, this is a home net: a workstation beaconing to a rare external IP on an odd
port, lateral SMB/RDP between client hosts, or PowerShell/LOLBin execution from a user
folder is *not* normal and deserves real scrutiny.

## Documented false-positive baselines (contextualize each NARROWLY)

**nas / `.15`: platform-mismatch Sigma misfires (dominant volume).** Windows-product
Sigma `process_creation` rules fire on the NAS's Linux Unraid processes: the mover,
container supervision, and array/loopback scanning (`emhttpd`, `s6-supervise`, `runc`,
`btrfs`, `losetup`, `in_use`, `move`, `pihole-FTL`). These are cross-platform rule
misfires with no detection value on a Linux host. For a new one in this family, identify
the Linux process, confirm the Windows-rule mismatch, and recommend a host/product scope
(propose against the `rule.uuid`).

**workstation / `.19`: the operator's agentic-dev workflow.** Benign signature: parent
`claude.exe` spawning `bash.exe` / `pwsh.exe` in the `C:/Users/operator/projects`
workdir. A second agentic IDE (a language-server binary) runs on this host with the same
shape. Firing rules and their benign explanation:

| Sigma rule | What it actually is |
|---|---|
| Script Interpreter From Suspicious Folder | git-bash / interpreters launched from the agentic workdir |
| Curl (Download+Execute) | signed Git curl → localhost MCP health probe or TI API |
| Emoji / non-ASCII in CommandLine | git commit messages containing em-dashes |
| Base64 PowerShell | `pwsh` running the `gh` CLI under `claude.exe` |
| Schtasks persistence | agentic task scheduling under the dev workflow |

For each workstation alert that fits this pattern, state the narrow behavior-specific
context (exact parent / workdir / command shape) AND run an explicit deviation check:
right parent process, right workdir, plausible command shape, plausible timing. A match
is "explained." A mismatch (unexpected parent, novel external destination, new command
shape) is a deviation worth escalating, because this is the highest-value host.

## Telemetry coverage (state current coverage each cycle)

Network visibility for this deployment (after a boundary-mirror + firewall-syslog +
pihole-DNS remediation):

- Zeek L7 live: `zeek.ssl`, `zeek.http`, `zeek.file`, `zeek.x509`, `zeek.quic`, plus
  `conn`/`dns`/`notice`/`weird`. TLS SNI/cert, HTTP host/URI, and on-wire file metadata
  are available. JA3/JA3S/JA4 hashing is live under `hash.ja3` / `hash.ja3s` /
  `hash.ja4` (not `ssl.ja3`).
- Recursive DNS via pihole (.53) log ingest plus `zeek.dns`.
- Firewall logs flow to `logs-pfsense.log` (inbound and outbound).
- Endpoint network events (`logs-endpoint.events.network`) on the Windows hosts. The
  NAS's per-process network events are unavailable (Unraid's custom kernel lacks the eBPF
  hooks Defend needs); NAS egress is covered on-wire by Zeek, container east-west is
  dark.

Residual blind spots to name when one bounds a conclusion: VPN/tunneled egress (gluetun,
encrypted at the boundary), container-to-container traffic on the NAS, DoH/DoT clients
that bypass pihole, same-segment east-west unicast.

## Signature-update health

Suricata ET rules auto-update daily at 07:01 via `so-rule-update`. A health monitor (see
`security-onion/rule-update-health/`) writes the result to the `so-rule-update-health` ES
index (single doc, `_id:latest`): read it each cycle and treat `status:"ok"` with
`age_hours <= 26` as current. Flag a failed or stale update as a posture gap.
