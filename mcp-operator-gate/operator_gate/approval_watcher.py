"""Async tuning-approval watcher: turn an operator ✅ on a cycle PROPOSAL into
the real ``apply_tuning`` write.

The noon cycle is headless and exits, so no agent is around to wait on
``ask_operator`` for its tuning proposals. This background loop closes that gap:
it seeds ✅/❌ on each proposal message the cycle posts, and when the operator
taps ✅, it performs the single gated write directly and **posts a new reply**
announcing the outcome (a fresh message pings the operator; an in-place edit went
unnoticed). ❌ dismisses. It never mutates the original proposal; a small
persistent store records which messages it has handled so a restart never
double-applies or re-announces.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from .discord_rest import DiscordREST, snowflake_ms
from .so_gateway_client import SoGatewayClient, SoGatewayError
from .store import HandledStore

log = logging.getLogger("operator-gate.watcher")

APPROVE_EMOJI = "✅"
DISMISS_EMOJI = "❌"

_TOKEN_RE = re.compile(r"Token:\s*`?([A-Za-z][A-Za-z0-9-]{2,})`?")
_PROPOSAL_RE = re.compile(r"^PROPOSAL\s+[—-]", re.MULTILINE)
_PERMANENT_ERRORS = (
    "no pending proposal", "already applied", "already used",
    "unknown or expired", "single-use",
)


class ApprovalWatcher(threading.Thread):
    def __init__(
        self,
        rest: DiscordREST,
        so_gateway_url: str,
        channel_id: str,
        bot_user_id: str,
        operator_id: str,
        store: HandledStore,
        *,
        poll_seconds: float = 10.0,
        lookback_hours: float = 26.0,
    ) -> None:
        super().__init__(daemon=True, name="approval-watcher")
        self._rest = rest
        self._so = SoGatewayClient(so_gateway_url)
        self._channel = channel_id
        self._bot = str(bot_user_id)
        self._operator = str(operator_id)
        self._store = store
        self._poll = poll_seconds
        # Only ever act on proposals newer than this. Bounds work to the recent
        # cycle(s) instead of every historical PROPOSAL in the channel (which
        # would seed + poll hundreds of dead messages and hit the rate limit).
        self._cutoff_ms = int(time.time() * 1000) - int(lookback_hours * 3600_000)
        self._seeded: set[str] = set()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("approval watcher started (channel=%s)", self._channel)
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:  # never let a transient error kill the loop
                log.exception("approval watcher scan failed")
            self._stop.wait(self._poll)

    # -- one pass over recent messages ---------------------------------------
    def _scan_once(self) -> None:
        for msg in self._rest.list_messages(self._channel, limit=25):
            mid = msg["id"]
            if snowflake_ms(mid) < self._cutoff_ms:
                continue  # too old — ignore historical proposals entirely
            if str(msg.get("author", {}).get("id")) != self._bot:
                continue
            content = msg.get("content") or ""
            if not _PROPOSAL_RE.search(content):
                continue
            if self._store.is_handled(mid):
                continue
            m = _TOKEN_RE.search(content)
            if not m:
                continue
            token = m.group(1)
            self._ensure_seeded(mid)

            # Cheap gate: only pay for a per-user reactions lookup when the
            # message's reaction SUMMARY shows a non-bot reaction present.
            counts = {r.get("emoji", {}).get("name"): r.get("count", 0)
                      for r in msg.get("reactions", [])}
            if counts.get(DISMISS_EMOJI, 0) >= 2 and self._operator_reacted(mid, DISMISS_EMOJI):
                self._store.mark(mid, token, "dismissed")
                self._reply(mid, f"❌ Dismissed **{token}** — nothing applied.")
                log.info("proposal %s dismissed by operator", token)
                continue
            if counts.get(APPROVE_EMOJI, 0) >= 2 and self._operator_reacted(mid, APPROVE_EMOJI):
                self._apply(mid, token)

    def _ensure_seeded(self, message_id: str) -> None:
        if message_id in self._seeded:
            return
        for e in (APPROVE_EMOJI, DISMISS_EMOJI):
            try:
                self._rest.add_reaction(self._channel, message_id, e)
            except Exception:
                log.debug("seed reaction failed on %s", message_id)
        self._seeded.add(message_id)

    def _operator_reacted(self, message_id: str, emoji: str) -> bool:
        try:
            users = self._rest.reaction_users(self._channel, message_id, emoji)
        except Exception:
            return False
        return any(str(u.get("id")) == self._operator for u in users)

    def _apply(self, message_id: str, token: str) -> None:
        try:
            result = self._so.apply_tuning(token)
        except SoGatewayError as exc:
            msg = str(exc).lower()
            if any(s in msg for s in _PERMANENT_ERRORS):
                # Token gone for good (gateway restarted, or already applied) --
                # record it handled so we stop retrying, and say so once.
                self._store.mark(message_id, token, "stale")
                self._reply(
                    message_id,
                    f"⚠️ **{token}** is no longer valid (gateway restarted or already "
                    f"applied). Re-run a cycle to re-propose.",
                )
                log.info("proposal %s permanently stale; marked handled", token)
            else:
                # Transient (gateway down, network) -- leave retryable, no reply.
                log.warning("apply_tuning(%s) failed (transient, will retry): %s", token, exc)
            return
        handle = result.get("handle", "?")
        self._store.mark(message_id, token, f"applied:{handle}")
        self._reply(
            message_id,
            f"✅ **Applied {token}.** The tuning is live on Security Onion. "
            f"Undo anytime: `revert {handle}`",
        )
        log.info("applied tuning %s -> handle %s", token, handle)

    def _reply(self, proposal_message_id: str, text: str) -> None:
        try:
            self._rest.post_message(self._channel, text, reply_to=proposal_message_id)
        except Exception:
            log.debug("reply post failed for %s", proposal_message_id)
