"""operator-gate MCP server: the general human-in-the-loop primitive.

Exposes ``ask_operator`` — any agent (an interactive OpenClaw turn, the IR team,
a subjective judgment call) posts a question with tappable options and blocks
until the operator taps one. Also runs the async tuning-approval watcher so the
headless noon cycle's proposals get a ✅-to-approve path even though no agent is
around to wait on them.

Why reactions and not native Discord buttons: buttons render fine, but a button
*click* is delivered over the bot's gateway connection — which OpenClaw holds —
and this build doesn't surface it. Reaction polling over REST needs no gateway,
so it works today on the shared bot token. See discord_rest.py.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .approval_watcher import ApprovalWatcher
from .config import Config, load_config
from .discord_rest import DiscordREST
from .gate import MAX_OPTIONS, OperatorGate
from .store import HandledStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("operator-gate")

_cfg: Config = load_config()
_rest = DiscordREST(_cfg.discord_token)
_gate = OperatorGate(_rest, _cfg.channel_id, _cfg.operator_id)

mcp = FastMCP("operator-gate", host=_cfg.mcp_host, port=_cfg.mcp_port)


@mcp.tool()
def ask_operator(question: str, options: list[str], timeout_seconds: int | None = None) -> dict:
    """Ask the operator a question in Discord and wait for their tapped answer.

    Use this for ANY human gate: a subjective decision, a go/no-go, "which of
    these?", or confirming an action before you take it. Post one clear question
    and 1-{max} short option labels; the operator taps one emoji and you get the
    choice back. Prefer this over guessing when the operator's intent is genuinely
    needed.

    *question*: the decision to put to the operator (one line).
    *options*: 1-{max} short labels (e.g. ["Approve","Reject"], ["A","B","C"]).
    *timeout_seconds*: how long to wait (default {default}); on timeout the
    result is {{"answered": false, "timed_out": true}} — treat as no decision.

    Returns {{"answered": bool, "option": str|None, "index": int|None,
    "cancelled": bool, "timed_out": bool}}. Only the configured operator's
    reaction counts.
    """
    if not options:
        return {"answered": False, "error": "provide at least one option"}
    if len(options) > MAX_OPTIONS:
        return {"answered": False, "error": f"at most {MAX_OPTIONS} options"}
    t = timeout_seconds if timeout_seconds is not None else _cfg.default_timeout_seconds
    ans = _gate.ask(question, options, timeout_seconds=max(30, min(int(t), 3600)))
    return {
        "answered": ans.answered,
        "option": ans.option,
        "index": ans.index,
        "cancelled": ans.cancelled,
        "timed_out": ans.timed_out,
    }


ask_operator.__doc__ = (ask_operator.__doc__ or "").format(
    max=MAX_OPTIONS, default=_cfg.default_timeout_seconds
)


def _start_watcher() -> None:
    if not _cfg.watch_approvals:
        log.info("approval watcher disabled (WATCH_APPROVALS=false)")
        return
    bot_id = _rest.me()["id"]
    store = HandledStore(_cfg.handled_db)
    watcher = ApprovalWatcher(
        _rest, _cfg.so_gateway_url, _cfg.channel_id, bot_id, _cfg.operator_id, store,
        lookback_hours=_cfg.lookback_hours,
    )
    watcher.start()


if __name__ == "__main__":
    _start_watcher()
    mcp.run(transport="streamable-http")
