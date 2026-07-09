"""Tests for word-pair identifiers (so_gateway.wordtoken) and their wiring.

Tokens/handles are workflow bindings, not security boundaries (any client that
reaches the gateway can propose for itself), so they are optimized for humans:
readable word pairs, case/separator-tolerant matching, collision-checked
against everything still meaningful.
"""

import re

import pytest

from so_gateway import wordtoken
from so_gateway.tuning_service import TuningService
from so_gateway.tuning_store import TuningStore

from test_tuning_service import FakeClient

PAIR = re.compile(r"^[a-z]+-[a-z]+(-\d{2})?$")


def _svc(tmp_path):
    return TuningService(FakeClient(), TuningStore(str(tmp_path / "t.sqlite")))


def _propose(svc):
    return svc.propose_tuning(
        public_id="2009205",
        override_type="suppress",
        scope={"track": "by_src", "ip": "10.0.0.19/32"},
        rationale="benign",
    )


# -- the generator ----------------------------------------------------------


def test_new_token_shape_and_uniqueness():
    seen = set()
    for _ in range(50):
        t = wordtoken.new_token(taken=seen)
        assert PAIR.match(t), t
        assert t not in seen
        seen.add(t)


def test_new_token_respects_taken_even_when_space_is_exhausted():
    everything = {
        f"{a}-{n}" for a in wordtoken.ADJECTIVES for n in wordtoken.NOUNS
    }
    t = wordtoken.new_token(taken=everything)
    assert t not in everything
    assert PAIR.match(t)  # falls back to the numeric-suffix form


def test_normalize_is_case_and_separator_tolerant():
    assert wordtoken.normalize("Amber Fox") == "amber-fox"
    assert wordtoken.normalize("  AMBER_FOX ") == "amber-fox"
    assert wordtoken.normalize("amber - fox") == "amber-fox"
    # non-word-pair input (e.g. a legacy hex token) passes through unmangled
    assert wordtoken.normalize("A1B2C3") == "a1b2c3"


# -- wiring: tokens ----------------------------------------------------------


def test_propose_issues_word_pair_token(tmp_path):
    out = _propose(_svc(tmp_path))
    assert PAIR.match(out["token"]), out["token"]


def test_apply_accepts_sloppy_operator_token(tmp_path):
    svc = _svc(tmp_path)
    token = _propose(svc)["token"]
    sloppy = token.replace("-", " ").upper()  # "AMBER FOX"
    applied = svc.apply_tuning(sloppy)
    assert applied["status"] == "applied"


def test_tokens_unique_across_pending_and_consumed(tmp_path):
    svc = _svc(tmp_path)
    seen = set()
    for _ in range(8):
        token = _propose(svc)["token"]
        assert token not in seen
        seen.add(token)
        svc.apply_tuning(token)  # move it to consumed; must stay reserved


# -- wiring: handles ---------------------------------------------------------


def test_handles_are_word_pairs_and_revert_is_tolerant(tmp_path):
    svc = _svc(tmp_path)
    handle = svc.apply_tuning(_propose(svc)["token"])["handle"]
    assert PAIR.match(handle), handle
    out = svc.revert_tuning(handle.replace("-", " ").title())  # "Amber Fox"
    assert out["status"] == "reverted"


# -- list_pending ------------------------------------------------------------


def test_list_pending_shows_awaiting_proposals_then_empties(tmp_path):
    svc = _svc(tmp_path)
    assert svc.list_pending() == []
    token = _propose(svc)["token"]
    pending = svc.list_pending()
    assert [p["token"] for p in pending] == [token]
    assert pending[0]["public_id"] == "2009205"
    assert pending[0]["rationale"] == "benign"
    svc.apply_tuning(token)
    assert svc.list_pending() == []
