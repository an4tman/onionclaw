#!/bin/sh
# soc-suite-config.sh — resolve and load the suite configuration.
#
# Sourced by every orchestration shell script (soc-cycle.sh, ir-investigate.sh).
# It locates soc-suite.env and exports its values, then applies safe defaults so
# the scripts never carry hardcoded environment details.
#
# Resolution order for the config file:
#   1. $SOC_SUITE_ENV if set (explicit override)
#   2. <suite-root>/config/soc-suite.env   (relative to this lib, the normal case)
#   3. $SOC_AGENT_HOME/soc-suite.env       (when installed beside the cycle in-container)
#
# Usage (from a script in orchestration/<x>/):
#   SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
#   . "$SELF_DIR/../lib/soc-suite-config.sh"

# Locate this lib regardless of caller.
_SOC_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo .)

_load_env() {
  for _cand in \
    "${SOC_SUITE_ENV:-}" \
    "$_SOC_LIB_DIR/../../config/soc-suite.env" \
    "${SOC_AGENT_HOME:-/root/.openclaw/soc-agent}/soc-suite.env"
  do
    [ -n "$_cand" ] || continue
    if [ -f "$_cand" ]; then
      # shellcheck disable=SC1090
      . "$_cand"
      SOC_SUITE_ENV_LOADED="$_cand"
      return 0
    fi
  done
  return 1
}

if ! _load_env; then
  echo "soc-suite-config: no soc-suite.env found. Copy config/soc-suite.env.example to config/soc-suite.env (or set \$SOC_SUITE_ENV)." >&2
  exit 1
fi

# ── Defaults (only applied when the env file leaves a value unset) ───────────
: "${SOC_OPENCLAW_CONTAINER:=OpenClaw}"
: "${SOC_AGENT_HOME:=/root/.openclaw/soc-agent}"
: "${SOC_CLAUDE_BIN:=/root/.openclaw/npm-global/bin/claude}"
: "${SOC_CLAUDE_ENV:=/root/.openclaw/claude.env}"
: "${SOC_CLAUDE_CONFIG_DIR:=/root/.openclaw/claude}"
: "${SOC_CLOUD_MODEL:=anthropic/claude-sonnet-5}"
: "${SOC_TZ:=UTC}"

# ── Required values — fail loudly if missing ────────────────────────────────
for _req in SOC_DISCORD_CHANNEL; do
  eval "_val=\${$_req:-}"
  if [ -z "$_val" ]; then
    echo "soc-suite-config: required value $_req is unset in $SOC_SUITE_ENV_LOADED" >&2
    exit 1
  fi
done

export SOC_LAN_CIDR SOC_SO_IP SOC_SO_HOSTNAME SOC_SO_URL SOC_SO_ES_URL \
       SOC_DOCKER_HOST SOC_ES_MCP_PORT SOC_SO_GATEWAY_PORT \
       SOC_OPENCLAW_CONTAINER SOC_AGENT_HOME SOC_CLAUDE_BIN SOC_CLAUDE_ENV \
       SOC_CLAUDE_CONFIG_DIR SOC_CLOUD_MODEL SOC_DISCORD_CHANNEL \
       SOC_CYCLE_CRON SOC_TZ SOC_SYSLOG_PORT SOC_PIHOLE_LOG SOC_OPENCLAW_LOG_DIR
