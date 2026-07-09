#!/bin/bash
# so-rule-update-healthcheck.sh
#
# Detect a SILENT failure of the daily signature update (so-rule-update /
# idstools-rulecat, root cron 07:01). Evaluates the run log read-only and writes
# ONE health doc to local Elasticsearch (fixed _id=latest, so the index always
# holds exactly one doc and cannot balloon like the elastalert indices did).
# The OpenClaw SOC noon cycle reads this doc via the elasticsearch MCP and
# reports signature-update freshness to Discord.
#
# No new secrets: uses SO's own ES curl.config. Installed 2026-06-13.
# Ships with soc-agent-suite: security-onion/rule-update-health/.
set -uo pipefail

LOG=/opt/so/log/idstools/download_cron.log
ES=https://localhost:9200
IDX=so-rule-update-health
CURLCFG=/opt/so/conf/elasticsearch/curl.config
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOST=$(hostname)

status=ok
note=""
last_run="unknown"
age_hours=-1
final_write=false
error_count=-1
total=0
enabled=0

if [[ ! -f "$LOG" ]]; then
  status=error
  note="download_cron.log missing"
else
  mtime_epoch=$(stat -c %Y "$LOG")
  now_epoch=$(date +%s)
  age_hours=$(( (now_epoch - mtime_epoch) / 3600 ))
  last_run=$(date -u -d "@$mtime_epoch" +%Y-%m-%dT%H:%M:%SZ)

  # The final write step ("...all.rules: total: N") proves rulecat ran end to end.
  fw=$(grep "Writing rules to /opt/so/rules/nids/suri/all.rules: total:" "$LOG" | tail -1)
  if [[ -n "$fw" ]]; then
    final_write=true
    total=$(echo "$fw" | grep -oE "total: [0-9]+" | grep -oE "[0-9]+")
    enabled=$(echo "$fw" | grep -oE "enabled: [0-9]+" | grep -oE "[0-9]+")
    total=${total:-0}
    enabled=${enabled:-0}
  fi

  # Real idstools failures use the "- <LEVEL> -" log format; a bare "error"
  # grep would false-match rule text, so key on the level field / tracebacks.
  error_count=$(grep -cE " - <(ERROR|CRITICAL)> -|Traceback" "$LOG")

  if (( age_hours > 26 )); then
    status=stale
    note="last run ${age_hours}h ago (>26h) -- cron may not be firing"
  fi
  if [[ "$final_write" != "true" ]]; then
    status=error
    note="no final-write marker -- rulecat did not complete"
  fi
  if (( error_count > 0 )); then
    status=error
    note="${error_count} idstools ERROR/CRITICAL/Traceback lines in log"
  fi
fi

DOC=$(cat <<JSON
{"@timestamp":"$NOW","check":"so-rule-update","status":"$status","last_run":"$last_run","age_hours":$age_hours,"final_write_present":$final_write,"error_count":$error_count,"rules_total":$total,"rules_enabled":$enabled,"note":"$note","host":"$HOST"}
JSON
)

curl -sk --config "$CURLCFG" -X PUT "$ES/$IDX/_doc/latest" \
  -H "Content-Type: application/json" -d "$DOC" >/dev/null

echo "$NOW so-rule-update-health: status=$status age=${age_hours}h errors=$error_count final_write=$final_write total=$total enabled=$enabled"
