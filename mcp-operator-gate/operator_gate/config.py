"""Environment config for the operator-gate service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    discord_token: str
    channel_id: str
    operator_id: str
    so_gateway_url: str
    mcp_host: str
    mcp_port: int
    watch_approvals: bool
    default_timeout_seconds: int
    handled_db: str
    lookback_hours: float


def load_config() -> Config:
    token = os.environ.get("DISCORD_TOKEN") or ""
    channel = os.environ.get("OPERATOR_CHANNEL_ID") or ""
    operator = os.environ.get("OPERATOR_USER_ID") or ""
    missing = [n for n, v in
               (("DISCORD_TOKEN", token), ("OPERATOR_CHANNEL_ID", channel),
                ("OPERATOR_USER_ID", operator)) if not v]
    if missing:
        raise RuntimeError(f"operator-gate missing required env: {', '.join(missing)}")
    return Config(
        discord_token=token,
        channel_id=channel,
        operator_id=operator,
        so_gateway_url=os.environ.get("SO_GATEWAY_URL", "http://mcp-so-gateway:8080/mcp"),
        mcp_host=os.environ.get("MCP_HOST", "0.0.0.0"),
        mcp_port=int(os.environ.get("MCP_PORT", "8080")),
        watch_approvals=os.environ.get("WATCH_APPROVALS", "true").lower() != "false",
        default_timeout_seconds=int(os.environ.get("ASK_TIMEOUT_SECONDS", "600")),
        handled_db=os.environ.get("HANDLED_DB", "/data/operator-gate.sqlite"),
        lookback_hours=float(os.environ.get("APPROVAL_LOOKBACK_HOURS", "26")),
    )
