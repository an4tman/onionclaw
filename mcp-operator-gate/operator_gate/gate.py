"""The operator-gate primitive: ask the human a question, get a tapped answer.

This is the general capability behind every human gate in the suite — tuning
approval, escalation, and any subjective judgment call. An agent calls
``ask_operator(question, options)``; the operator taps one emoji; the agent
gets the chosen option back. Reaction-based so it works on the shared bot token
with no Discord gateway connection (see discord_rest.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .discord_rest import DiscordREST

# Keycap-digit emoji 1-9 (U+003N U+FE0F U+20E3), then 🔟. Enough option slots.
NUMBER_EMOJI = [f"{d}️⃣" for d in "123456789"] + ["\U0001f51f"]
CANCEL_EMOJI = "❌"  # ❌
MAX_OPTIONS = len(NUMBER_EMOJI)


@dataclass
class Answer:
    answered: bool
    index: int | None = None
    option: str | None = None
    cancelled: bool = False
    timed_out: bool = False


class OperatorGate:
    def __init__(self, rest: DiscordREST, channel_id: str, operator_id: str) -> None:
        self._rest = rest
        self._channel = channel_id
        self._operator = str(operator_id)

    def ask(
        self,
        question: str,
        options: list[str],
        *,
        timeout_seconds: int = 600,
        poll_seconds: float = 3.0,
        allow_cancel: bool = True,
    ) -> Answer:
        """Post *question* with tappable *options*; block until the operator taps.

        Returns an :class:`Answer`. Only reactions from the configured operator
        user id count; anyone else's are ignored.
        """
        if not options:
            raise ValueError("ask_operator needs at least one option")
        if len(options) > MAX_OPTIONS:
            raise ValueError(f"at most {MAX_OPTIONS} options (got {len(options)})")

        emojis = NUMBER_EMOJI[: len(options)]
        lines = [f"**{question}**", ""]
        lines += [f"{emojis[i]}  {opt}" for i, opt in enumerate(options)]
        lines.append("")
        tail = "React to choose."
        if allow_cancel:
            tail += f"  ({CANCEL_EMOJI} to cancel.)"
        lines.append(f"_{tail}_")
        message_id = self._rest.post_message(self._channel, "\n".join(lines))

        # Seed the reactions so the operator just taps (no typing an emoji).
        for e in emojis:
            self._rest.add_reaction(self._channel, message_id, e)
        if allow_cancel:
            self._rest.add_reaction(self._channel, message_id, CANCEL_EMOJI)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            if allow_cancel and self._reacted(message_id, CANCEL_EMOJI):
                self._confirm(message_id, "❌ Cancelled — no decision recorded.")
                return Answer(answered=False, cancelled=True)
            for i, e in enumerate(emojis):
                if self._reacted(message_id, e):
                    self._confirm(message_id, f"✅ You chose: **{options[i]}**")
                    return Answer(answered=True, index=i, option=options[i])
        self._confirm(message_id, "⏱️ Timed out — no response, treating as no decision.")
        return Answer(answered=False, timed_out=True)

    def _reacted(self, message_id: str, emoji: str) -> bool:
        try:
            users = self._rest.reaction_users(self._channel, message_id, emoji)
        except Exception:
            return False
        return any(str(u.get("id")) == self._operator for u in users)

    def _confirm(self, question_message_id: str, text: str) -> None:
        # Post a NEW reply (not an edit) so the operator gets a notification that
        # their tap registered; an in-place edit goes unnoticed.
        try:
            self._rest.post_message(self._channel, text, reply_to=question_message_id)
        except Exception:
            pass  # cosmetic; the answer is already determined
