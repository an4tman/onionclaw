#!/bin/sh
# OnionClaw installer — automates the scriptable parts of docs/01-05.
#
#   bin/install.sh preflight       # check every prerequisite, change nothing
#   bin/install.sh gateways        # scaffold env files, build + run the two MCP containers
#   bin/install.sh orchestration   # install the cycle/IR/self-improve tree into OpenClaw
#   bin/install.sh all             # the three in order
#
# What stays manual (by design — see the docs): filling in credentials
# (so.env / ti.env / es.env), the SO-side service account + firewall hostgroups
# (docs/02), the OpenClaw agent/bind/systemPrompt (docs/04), registering the
# cron (docs/04 §5), and the soc-analyst skill install (docs/08).
#
# Idempotent: re-running rebuilds images, replaces the two MCP containers, and
# re-copies the orchestration tree. Your env files and audit DB are never touched.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$ROOT/config/soc-suite.env"
GATEWAY_DIR="$ROOT/mcp-so-gateway"
ES_MCP_IMAGE="${ES_MCP_IMAGE:-docker.elastic.co/mcp/elasticsearch:latest}"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=1; }

[ -f "$ENV_FILE" ] || {
  say "No $ENV_FILE — copy config/soc-suite.env.example there and fill it in first."
  exit 1
}
# shellcheck disable=SC1090
. "$ENV_FILE"

require_vars() {
  for v in SOC_SO_IP SOC_SO_HOSTNAME SOC_SO_URL SOC_SO_ES_URL SOC_DOCKER_HOST \
           SOC_ES_MCP_PORT SOC_SO_GATEWAY_PORT SOC_OPENCLAW_CONTAINER \
           SOC_AGENT_HOME SOC_CLAUDE_BIN SOC_CLAUDE_ENV SOC_DISCORD_CHANNEL; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || fail "soc-suite.env: $v is unset"
  done
  case "${SOC_DISCORD_CHANNEL:-}" in *'<'*) fail "soc-suite.env: SOC_DISCORD_CHANNEL still has a placeholder";; esac
}

# ---------------------------------------------------------------------------
preflight() {
  FAILED=0
  say "== preflight (read-only checks) =="

  require_vars

  command -v docker >/dev/null 2>&1 && ok "docker CLI present" || fail "docker not found"
  command -v curl   >/dev/null 2>&1 && ok "curl present"       || fail "curl not found"

  # SO Core API: must answer under its expected Host header (docs/02 §4).
  code=$(curl -sk -m 8 -o /dev/null -w '%{http_code}' \
           --resolve "$SOC_SO_HOSTNAME:443:$SOC_SO_IP" "https://$SOC_SO_HOSTNAME/" || true)
  case "$code" in
    2*|3*) ok "SO web/API reachable at $SOC_SO_HOSTNAME ($SOC_SO_IP) [$code]" ;;
    000)   fail "SO web/API unreachable — is $SOC_SO_IP right, and is this host in SO's 'analyst' hostgroup? (docs/02 §3)" ;;
    *)     warn "SO web/API answered $code — check SOC_SO_HOSTNAME matches SO's server_name (docs/02 §4)" ;;
  esac

  # Elasticsearch: 401 = reachable + auth required (good). Timeout = firewalled.
  code=$(curl -sk -m 8 -o /dev/null -w '%{http_code}' "$SOC_SO_ES_URL" || true)
  case "$code" in
    401|200) ok "SO Elasticsearch reachable at $SOC_SO_ES_URL [$code]" ;;
    000)     fail "Elasticsearch unreachable — add this host to SO's 'elasticsearch_rest' hostgroup (docs/02 §3)" ;;
    *)       warn "Elasticsearch answered $code — unexpected; check SOC_SO_ES_URL" ;;
  esac

  # OpenClaw container + the headless-Claude prerequisites (docs/04 §4).
  if docker inspect "$SOC_OPENCLAW_CONTAINER" >/dev/null 2>&1; then
    ok "OpenClaw container '$SOC_OPENCLAW_CONTAINER' exists"
    docker exec "$SOC_OPENCLAW_CONTAINER" sh -c "[ -x '$SOC_CLAUDE_BIN' ]" 2>/dev/null \
      && ok "claude binary at $SOC_CLAUDE_BIN" \
      || fail "claude binary missing/not executable at $SOC_CLAUDE_BIN (docs/04 §4)"
    docker exec "$SOC_OPENCLAW_CONTAINER" sh -c "[ -f '$SOC_CLAUDE_ENV' ]" 2>/dev/null \
      && ok "claude.env at $SOC_CLAUDE_ENV" \
      || fail "claude.env missing at $SOC_CLAUDE_ENV (docs/04 §4)"
    docker exec "$SOC_OPENCLAW_CONTAINER" sh -c "command -v openclaw >/dev/null" 2>/dev/null \
      && ok "openclaw CLI on the container PATH" \
      || fail "openclaw CLI not found in the container"
  else
    fail "OpenClaw container '$SOC_OPENCLAW_CONTAINER' not found"
  fi

  # Credential files (created by 'gateways' as templates; you fill them).
  for f in so.env ti.env es.env; do
    if [ -f "$GATEWAY_DIR/$f" ]; then
      grep -q '<' "$GATEWAY_DIR/$f" && warn "$f still contains <placeholders> — fill it in" || ok "$f present"
    else
      warn "$f not created yet (run: bin/install.sh gateways)"
    fi
  done

  [ "${FAILED:-0}" -eq 0 ] && say "preflight: all required checks passed." \
                           || { say "preflight: required checks FAILED (see above)."; return 1; }
}

# ---------------------------------------------------------------------------
scaffold_env() { # $1 = filename, stdin = template
  f="$GATEWAY_DIR/$1"
  if [ -f "$f" ]; then
    say "  $1 exists — leaving it alone."
  else
    cat > "$f"
    chmod 600 "$f"
    say "  created $1 (chmod 600) — FILL IN the placeholders."
    SCAFFOLDED=1
  fi
}

gateways() {
  FAILED=0; SCAFFOLDED=0
  require_vars
  [ "${FAILED:-0}" -eq 0 ] || exit 1
  say "== gateways: env scaffolds + build + run =="

  scaffold_env so.env <<EOF
SO_URL=$SOC_SO_URL
SO_EMAIL=<so-service-account-email>
SO_PASSWORD=<so-service-account-password>
SO_SSL_SKIP_VERIFY=true
EOF
  scaffold_env ti.env <<EOF
# Omit a line to disable that provider. TI_USER_AGENT: include YOUR contact.
TI_OTX_API_KEY=<otx-key>
TI_ABUSEIPDB_API_KEY=<abuseipdb-key>
TI_VT_API_KEY=<virustotal-key>
TI_ENABLE_FEEDS=true
TI_USER_AGENT=soc-ti-enrichment/1.0 (<you@example.com>)
EOF
  scaffold_env es.env <<EOF
ES_URL=$SOC_SO_ES_URL
ES_API_KEY=<so-elasticsearch-api-key-base64>
ES_SSL_SKIP_VERIFY=true
EOF

  if [ "$SCAFFOLDED" -eq 1 ]; then
    say "gateways: credential file(s) scaffolded in mcp-so-gateway/ — fill them in, then re-run."
    return 1
  fi
  if grep -q '<' "$GATEWAY_DIR/so.env" "$GATEWAY_DIR/es.env"; then
    say "gateways: so.env / es.env still contain <placeholders> — fill them in, then re-run."
    return 1
  fi

  say "  building mcp-so-gateway image..."
  docker build -q -t mcp-so-gateway:latest "$GATEWAY_DIR"

  say "  (re)creating mcp-so-gateway on :$SOC_SO_GATEWAY_PORT ..."
  docker rm -f mcp-so-gateway >/dev/null 2>&1 || true
  set -- docker run -d --name mcp-so-gateway --restart unless-stopped \
    --add-host "$SOC_SO_HOSTNAME:$SOC_SO_IP" \
    --env-file "$GATEWAY_DIR/so.env" \
    --env-file "$GATEWAY_DIR/ti.env" \
    -v "$GATEWAY_DIR/data:/data"
  # Optional grounding write path (docs/08): mount the directory holding the
  # canonical environment.md so the gated grounding tools can append to it.
  if [ -n "${SOC_GROUNDING_DIR:-}" ]; then
    set -- "$@" -v "$SOC_GROUNDING_DIR:/grounding" \
      -e "GROUNDING_PATHS=/grounding/environment.md"
  fi
  # Optional kb write path (docs/08): mount a wiki/notes dir read-write so the
  # gated propose/apply/revert_kb tools can change it with operator approval.
  if [ -n "${SOC_KB_WRITE_DIR:-}" ]; then
    set -- "$@" -v "$SOC_KB_WRITE_DIR:/kb-rw" -e "KB_WRITE_ROOT=/kb-rw"
  fi
  "$@" -p "$SOC_SO_GATEWAY_PORT:8080" mcp-so-gateway:latest >/dev/null

  say "  (re)creating mcp-elasticsearch on :$SOC_ES_MCP_PORT (image: $ES_MCP_IMAGE) ..."
  docker rm -f mcp-elasticsearch >/dev/null 2>&1 || true
  docker run -d --name mcp-elasticsearch --restart unless-stopped \
    --env-file "$GATEWAY_DIR/es.env" \
    -p "$SOC_ES_MCP_PORT:8080" "$ES_MCP_IMAGE" >/dev/null

  sleep 3
  for pair in "mcp-so-gateway:$SOC_SO_GATEWAY_PORT" "mcp-elasticsearch:$SOC_ES_MCP_PORT"; do
    name=${pair%%:*}; port=${pair##*:}
    if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
      code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://localhost:$port/mcp" || true)
      [ "$code" != "000" ] && ok "$name up, answering on :$port [$code]" \
                           || warn "$name running but :$port not answering yet"
    else
      fail "$name is not running — check: docker logs $name"
    fi
  done
  [ "${FAILED:-0}" -eq 0 ] || return 1
  say "gateways: done. MCP endpoints: http://$SOC_DOCKER_HOST:$SOC_ES_MCP_PORT/mcp and http://$SOC_DOCKER_HOST:$SOC_SO_GATEWAY_PORT/mcp"
  say "Next: wire both into openclaw.json mcp.servers AND user-scope Claude Code (docs/04 §2)."
}

# ---------------------------------------------------------------------------
orchestration() {
  FAILED=0
  require_vars
  [ "${FAILED:-0}" -eq 0 ] || exit 1
  say "== orchestration: install the cycle/IR tree into $SOC_OPENCLAW_CONTAINER =="

  docker exec "$SOC_OPENCLAW_CONTAINER" mkdir -p "$SOC_AGENT_HOME"
  docker cp "$ROOT/orchestration/." "$SOC_OPENCLAW_CONTAINER:$SOC_AGENT_HOME/"
  docker cp "$ENV_FILE"             "$SOC_OPENCLAW_CONTAINER:$SOC_AGENT_HOME/soc-suite.env"
  docker exec "$SOC_OPENCLAW_CONTAINER" chmod +x \
    "$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh" \
    "$SOC_AGENT_HOME/ir-team/ir-investigate.sh"
  docker exec "$SOC_OPENCLAW_CONTAINER" sh -n "$SOC_AGENT_HOME/soc-cycle/soc-cycle.sh" \
    && ok "cycle script installed + syntax-checked at $SOC_AGENT_HOME"

  say "orchestration: done. Still manual (docs/04):"
  say "  - create/bind the 'soc' agent + channel systemPrompt (docs/04 §3)"
  say "  - register the cron: schedule '$SOC_CYCLE_CRON' tz '$SOC_TZ' agent 'soc'"
  say "    command: $SOC_AGENT_HOME/soc-cycle/soc-cycle.sh   (docs/04 §5)"
  say "  - smoke-test one cycle: docker exec $SOC_OPENCLAW_CONTAINER $SOC_AGENT_HOME/soc-cycle/soc-cycle.sh"
}

# ---------------------------------------------------------------------------
case "${1:-}" in
  preflight)     preflight ;;
  gateways)      gateways ;;
  orchestration) orchestration ;;
  all)           preflight && gateways && orchestration ;;
  *) say "usage: bin/install.sh {preflight|gateways|orchestration|all}"; exit 2 ;;
esac
