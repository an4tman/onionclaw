"""Tests for the ask_operator gate + tuning-approval watcher, against a fake
Discord REST (records calls; scriptable reactions). No network."""

import re

import pytest

from operator_gate.approval_watcher import ApprovalWatcher
from operator_gate.gate import CANCEL_EMOJI, NUMBER_EMOJI, OperatorGate

OPERATOR = "200000000000000001"
BOT = "999000111"


class FakeRest:
    def __init__(self):
        self.messages = {}          # id -> content
        self.reactions = {}         # (mid, emoji) -> set(user_ids)
        self.replies = []           # (reply_to, content) posted as replies
        self._seq = 1000
        self.applied = []           # tokens the fake so-gateway "applied"

    def post_message(self, ch, content, reply_to=None):
        self._seq += 1
        mid = str(self._seq)
        self.messages[mid] = content
        if reply_to is not None:
            self.replies.append((reply_to, content))
        return mid

    def edit_message(self, ch, mid, content):
        self.messages[mid] = content

    def get_message(self, ch, mid):
        return {"id": mid, "content": self.messages[mid], "author": {"id": BOT}}

    def list_messages(self, ch, limit=50, after=None):
        return [{"id": mid, "content": c, "author": {"id": BOT}}
                for mid, c in self.messages.items()]

    def add_reaction(self, ch, mid, emoji):
        self.reactions.setdefault((mid, emoji), set()).add(BOT)  # bot seeds

    def reaction_users(self, ch, mid, emoji):
        return [{"id": u} for u in self.reactions.get((mid, emoji), set())]

    def me(self):
        return {"id": BOT}

    # test helper: simulate the operator tapping an emoji
    def operator_taps(self, mid, emoji):
        self.reactions.setdefault((mid, emoji), set()).add(OPERATOR)


class FakeSo:
    def __init__(self, rest, raise_error=None):
        self._rest = rest
        self.calls = []
        self._raise = raise_error  # an exception instance to raise, or None

    def apply_tuning(self, token):
        self.calls.append(token)
        if self._raise is not None:
            raise self._raise
        return {"handle": f"undo-{token}", "status": "applied",
                "public_id": "x", "override_type": "customFilter"}


# -- ask_operator ------------------------------------------------------------


def test_ask_seeds_options_and_returns_operator_choice():
    rest = FakeRest()
    gate = OperatorGate(rest, "chan", OPERATOR)

    # operator will tap option 2 before we poll
    def ask():
        return gate.ask("Proceed?", ["Yes", "No"], timeout_seconds=5, poll_seconds=0.01)

    # pre-arrange the tap: monkeypatch by tapping right after post via a subclass
    # simplest: tap inside reaction_users on first call
    orig = rest.reaction_users
    state = {"tapped": False}

    def tap_then(ch, mid, emoji):
        if not state["tapped"] and emoji == NUMBER_EMOJI[1]:
            rest.operator_taps(mid, NUMBER_EMOJI[1])
            state["tapped"] = True
        return orig(ch, mid, emoji)

    rest.reaction_users = tap_then
    ans = ask()
    assert ans.answered and ans.index == 1 and ans.option == "No"
    # both option emoji + cancel were seeded as reactions on the question message
    seeded = {emoji for (_mid, emoji) in rest.reactions}
    assert NUMBER_EMOJI[0] in seeded and NUMBER_EMOJI[1] in seeded and CANCEL_EMOJI in seeded


def test_ask_cancel_returns_cancelled():
    rest = FakeRest()
    gate = OperatorGate(rest, "chan", OPERATOR)
    orig = rest.reaction_users

    def tap_cancel(ch, mid, emoji):
        if emoji == CANCEL_EMOJI:
            rest.operator_taps(mid, CANCEL_EMOJI)
        return orig(ch, mid, emoji)

    rest.reaction_users = tap_cancel
    ans = gate.ask("Do X?", ["Go"], timeout_seconds=5, poll_seconds=0.01)
    assert not ans.answered and ans.cancelled


def test_ask_times_out_when_no_reaction():
    rest = FakeRest()
    gate = OperatorGate(rest, "chan", OPERATOR)
    ans = gate.ask("Q?", ["A", "B"], timeout_seconds=0, poll_seconds=0.01)
    assert not ans.answered and ans.timed_out


def test_ask_rejects_too_many_options():
    gate = OperatorGate(FakeRest(), "chan", OPERATOR)
    with pytest.raises(ValueError):
        gate.ask("Q?", [str(i) for i in range(len(NUMBER_EMOJI) + 1)])


# -- approval watcher --------------------------------------------------------

PROPOSAL = (
    "PROPOSAL — Linux Webshell Indicators (818f7b24-...)\n"
    "- Suppression: customFilter host.name hal\n"
    "- Token: `amber-fox`\n"
    "- To APPROVE: reply approve amber-fox"
)


def _watcher(rest, tmp_path):
    from operator_gate.store import HandledStore
    store = HandledStore(str(tmp_path / "handled.sqlite"))
    w = ApprovalWatcher(rest, "http://so/mcp", "chan", BOT, OPERATOR, store)
    w._so = FakeSo(rest)  # swap in the fake so-gateway
    return w


def test_watcher_seeds_but_does_not_apply_without_operator(tmp_path):
    rest = FakeRest()
    mid = rest.post_message("chan", PROPOSAL)
    w = _watcher(rest, tmp_path)
    w._scan_once()
    assert (mid, "✅") in rest.reactions  # seeded
    assert w._so.calls == []              # nothing applied


def test_watcher_applies_on_operator_approve_and_replies(tmp_path):
    rest = FakeRest()
    mid = rest.post_message("chan", PROPOSAL)
    w = _watcher(rest, tmp_path)
    w._scan_once()                        # seed
    rest.operator_taps(mid, "✅")
    w._scan_once()                        # detect + apply
    assert w._so.calls == ["amber-fox"]
    assert any(rt == mid and "Applied amber-fox" in c and "revert undo-amber-fox" in c
               for rt, c in rest.replies)


def test_watcher_dedup_no_double_apply(tmp_path):
    rest = FakeRest()
    mid = rest.post_message("chan", PROPOSAL)
    w = _watcher(rest, tmp_path)
    w._scan_once()
    rest.operator_taps(mid, "✅")
    w._scan_once()
    w._scan_once()                        # second pass must skip the handled msg
    assert w._so.calls == ["amber-fox"]   # applied exactly once


def test_watcher_dismiss_replies_and_skips_apply(tmp_path):
    rest = FakeRest()
    mid = rest.post_message("chan", PROPOSAL)
    w = _watcher(rest, tmp_path)
    w._scan_once()
    rest.operator_taps(mid, "❌")
    w._scan_once()
    assert w._so.calls == []
    assert any(rt == mid and "Dismissed" in c for rt, c in rest.replies)


def test_watcher_ignores_non_proposal_and_tokenless(tmp_path):
    rest = FakeRest()
    rest.post_message("chan", "just a briefing, no proposal here")
    rest.post_message("chan", "PROPOSAL — but no token line")
    w = _watcher(rest, tmp_path)
    w._scan_once()
    assert w._so.calls == []


def test_watcher_permanent_error_marks_handled_stops_retrying(tmp_path):
    from operator_gate.so_gateway_client import SoGatewayError
    rest = FakeRest()
    mid = rest.post_message("chan", PROPOSAL)
    w = _watcher(rest, tmp_path)
    w._so = FakeSo(rest, raise_error=SoGatewayError("no pending proposal for this token"))
    w._scan_once()
    rest.operator_taps(mid, "✅")
    w._scan_once()                        # attempt #1 -> permanent error -> terminal mark
    w._scan_once()                        # must NOT retry
    assert len(w._so.calls) == 1
    assert any(rt == mid and "no longer valid" in c for rt, c in rest.replies)


def test_watcher_transient_error_leaves_retryable(tmp_path):
    from operator_gate.so_gateway_client import SoGatewayError
    rest = FakeRest()
    mid = rest.post_message("chan", PROPOSAL)
    w = _watcher(rest, tmp_path)
    w._so = FakeSo(rest, raise_error=SoGatewayError("connection refused"))
    w._scan_once()
    rest.operator_taps(mid, "✅")
    w._scan_once()                        # attempt #1 (transient)
    w._scan_once()                        # attempt #2 -> still retries
    assert len(w._so.calls) == 2
    assert rest.replies == []  # transient failure announces nothing
