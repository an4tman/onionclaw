#!/bin/sh
# Build + (re)deploy the operator-gate container on the Docker host.
#
# Requires an env file (default ./operator-gate.env, chmod 600) with:
#   DISCORD_TOKEN=<the same bot token OpenClaw uses>
#   OPERATOR_CHANNEL_ID=<the SOC/operator channel id>
#   OPERATOR_USER_ID=<your Discord user id — only your reactions count>
#   SO_GATEWAY_URL=http://mcp-so-gateway:8080/mcp   (default; shared docker network)
#
# The container joins the same docker network as mcp-so-gateway so it can reach
# it by name, and publishes its own MCP endpoint on OPERATOR_GATE_PORT (9225).
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="${OPERATOR_GATE_ENV:-$DIR/operator-gate.env}"
PORT="${OPERATOR_GATE_PORT:-9225}"
NETWORK="${OPERATOR_GATE_NETWORK:-bridge}"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (see deploy.sh header)"; exit 1; }

echo "building mcp-operator-gate:latest ..."
docker build -q -t mcp-operator-gate:latest "$DIR"

echo "(re)creating mcp-operator-gate on :$PORT (network $NETWORK) ..."
docker rm -f mcp-operator-gate >/dev/null 2>&1 || true
DATA_DIR="${OPERATOR_GATE_DATA:-$(dirname -- "$ENV_FILE")/data}"
mkdir -p "$DATA_DIR"
docker run -d --name mcp-operator-gate --restart unless-stopped \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -e MCP_PORT=8080 \
  -v "$DATA_DIR:/data" \
  -p "$PORT:8080" mcp-operator-gate:latest >/dev/null

sleep 3
if [ "$(docker inspect -f '{{.State.Running}}' mcp-operator-gate 2>/dev/null)" = "true" ]; then
  echo "mcp-operator-gate up; MCP endpoint http://<host>:$PORT/mcp"
  docker logs --tail 5 mcp-operator-gate 2>&1 || true
else
  echo "mcp-operator-gate failed to start:"; docker logs mcp-operator-gate 2>&1 | tail -15; exit 1
fi
