# Elastic quick-reference (Security Onion)

> Offline essentials for querying a standard Security Onion Elasticsearch through the read-only
> `elasticsearch` MCP. Your deployment-specific host table and `observer.name` are in
> [environment.md](environment.md). Index names below are SO defaults — confirm with
> `mcp__elasticsearch__list_indices` / `get_mappings` on your cluster.

## The data-stream naming gotcha (the #1 trap)
SO uses **data streams**. Query the **bare stream name** (`logs-suricata.alerts-so`) or the
backing-index wildcard (`.ds-logs-suricata.alerts-so-*`) or `logs-*`. A *middle*-wildcard like
`logs-suricata.alerts-so-*` returns **0 hits** (can't match the dotted `.ds-…` indices) — a silent
false-negative. Always time-box (`@timestamp >= now-24h`) and aggregate before enumerating.

## Core indices
| Data | Index (data-stream name) |
|---|---|
| Suricata NIDS alerts | `logs-suricata.alerts-so` |
| Detection-engine alerts (Sigma/elastalert) | `logs-detections.alerts-so` |
| Zeek network metadata (pivot) | `logs-zeek-so` |
| Endpoint events | `logs-endpoint.events.process` / `.network` / `.file` / `.registry` |
| Windows | `logs-windows.sysmon_operational` / `.powershell` · `logs-system.security` |
| Firewall | `logs-pfsense.log` |
| SO detections + playbooks | `so-detection`, `so-detectionhistory` |

Key fields: `rule.name`, `rule.uuid` (= `so_detection.publicId`), `event.dataset`,
`source.ip`/`destination.ip`, `host.name`, `observer.name` (your sensor name). `event.severity` on
Suricata is inverted (**1 = highest**).

## Three canonical queries (`mcp__elasticsearch__search`)
Survey the queue (top rules, 24h) — index `logs-suricata.alerts-so`:
```json
{"size":0,"query":{"range":{"@timestamp":{"gte":"now-24h"}}},
 "aggs":{"top_rules":{"terms":{"field":"rule.name","size":20}}}}
```
Pivot an IP in Zeek — index `logs-zeek-so`:
```json
{"size":50,"sort":[{"@timestamp":"desc"}],"query":{"bool":{"filter":[{"range":{"@timestamp":{"gte":"now-6h"}}}],
 "should":[{"term":{"source.ip":"<ip>"}},{"term":{"destination.ip":"<ip>"}}],"minimum_should_match":1}},
 "_source":["@timestamp","event.dataset","source.ip","destination.ip","destination.port","dns.question.name"]}
```
Rule's playbook guidance — index `so-detection` (`content` carries `falsepositives`/ATT&CK tags):
```json
{"size":1,"query":{"term":{"so_detection.publicId":"<id>"}},
 "_source":["so_detection.title","so_detection.severity","so_detection.isEnabled","so_detection.content"]}
```
Extend these with frequency, top-talker, DNS, and process-pivot aggregations as needed; always
`size:0` + a `terms` agg first, then drill one group.
