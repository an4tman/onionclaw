"""Tests for the tuning orchestration service (so_gateway.tuning_service).

This is the gateway-enforced two-call approval gate (spec §4):

  propose_tuning(...)  -> validates + computes exact override + blast-radius +
                          issues a SINGLE-USE token. NO WRITE.
  apply_tuning(token)  -> consumes the token, PUTs the override, records undo.
  revert_tuning(handle)-> replays the prior state.
  list_tunings()       -> currently-applied overrides + undo handles.

The service is the GATING SEAM: apply_tuning requires a valid, unused token from
a prior propose_tuning. The agent workflow layers the human-approval gate ON TOP
(CC permission prompt / OpenClaw operator affirmation) -- the service guarantees
no apply without a reviewed proposal, and tokens are single-use.

Tested with a fake SoClient (records calls, no HTTP) + a real on-disk store.
"""

import pytest

from so_gateway.tuning_service import (
    ProposalNotFoundError,
    TokenAlreadyUsedError,
    TuningService,
)
from so_gateway.tuning_store import TuningStore

DETECTION = {
    "id": "ESID1",
    "publicId": "2009205",
    "title": "ET MALWARE KEYPLUG test",
    "isEnabled": True,
    "engine": "suricata",
    "overrides": [],
}


class FakeClient:
    """Records put/get/disposition calls; serves a canned detection."""

    def __init__(self, detection=None):
        self._detection = detection or dict(DETECTION)
        self.put_calls = []
        self.disposition_calls = []
        # how many alerts the blast-radius probe should report
        self.events_total = 7

    def get_detection_by_public_id(self, public_id):
        d = dict(self._detection)
        d["publicId"] = public_id
        return d

    def put_detection(self, detection):
        self.put_calls.append(detection)
        # SO echoes back the stored detection
        self._detection = detection
        return detection

    def disposition_alerts(self, **kwargs):
        self.disposition_calls.append(kwargs)
        return {"updated": self.events_total}

    def count_matching_alerts(self, public_id, scope, range=None):
        """Blast-radius probe: how many recent alerts the tuning would silence."""
        return self.events_total


@pytest.fixture
def svc(tmp_path):
    return TuningService(FakeClient(), TuningStore(str(tmp_path / "a.sqlite")))


# ---------------------------------------------------------------------------
# propose_tuning -- read-only: validate + preview + token. NO write.
# ---------------------------------------------------------------------------


def test_propose_returns_token_and_preview_no_write(svc):
    out = svc.propose_tuning(
        public_id="2009205",
        override_type="suppress",
        scope={"track": "by_src", "ip": "10.0.0.19/32"},
        rationale="benign Spotify P2P",
    )
    assert "token" in out and out["token"]
    # human-readable preview of the exact change
    assert out["override"]["type"] == "suppress"
    assert out["override"]["ip"] == "10.0.0.19/32"
    assert out["detection"]["publicId"] == "2009205"
    assert "blast_radius" in out  # estimate present
    assert out["double_gated"] is False
    # NO write happened during propose
    assert svc._client.put_calls == []


def test_propose_marks_disable_and_modify_double_gated(svc):
    out = svc.propose_tuning(
        public_id="2009205",
        override_type="disable",
        scope={},
        rationale="too noisy",
    )
    assert out["double_gated"] is True


def test_propose_rejects_invalid_scope_before_token(svc):
    from so_gateway.tuning import InvalidTuningError
    with pytest.raises(InvalidTuningError):
        svc.propose_tuning(
            public_id="2009205",
            override_type="suppress",
            scope={"ip": "not-an-ip"},
            rationale="x",
        )
    # no proposal stored, no token leaked
    assert svc._pending == {}


# ---------------------------------------------------------------------------
# apply_tuning -- consumes token, writes, records undo. Single-use.
# ---------------------------------------------------------------------------


def test_apply_with_valid_token_writes_and_records(svc):
    prop = svc.propose_tuning(
        public_id="2009205",
        override_type="suppress",
        scope={"track": "by_src", "ip": "10.0.0.19/32"},
        rationale="benign",
    )
    res = svc.apply_tuning(prop["token"])

    # exactly one PUT with the override appended
    assert len(svc._client.put_calls) == 1
    written = svc._client.put_calls[0]
    assert written["overrides"][-1]["ip"] == "10.0.0.19/32"
    # an undo handle is returned + recorded
    assert "handle" in res
    rec = svc._store.get(res["handle"])
    assert rec["status"] == "applied"
    assert rec["override_type"] == "suppress"
    assert rec["prior_state"]["overrides"] == []  # captured BEFORE the write


def test_apply_token_is_single_use(svc):
    prop = svc.propose_tuning(
        public_id="2009205", override_type="suppress",
        scope={"ip": "1.2.3.4/32"}, rationale="x",
    )
    svc.apply_tuning(prop["token"])
    with pytest.raises(TokenAlreadyUsedError):
        svc.apply_tuning(prop["token"])
    # still only one write
    assert len(svc._client.put_calls) == 1


def test_apply_unknown_token_raises(svc):
    with pytest.raises(ProposalNotFoundError):
        svc.apply_tuning("bogus-token")
    assert svc._client.put_calls == []


def test_apply_disable_flips_is_enabled(svc):
    prop = svc.propose_tuning(
        public_id="2009205", override_type="disable", scope={}, rationale="noisy",
    )
    svc.apply_tuning(prop["token"])
    written = svc._client.put_calls[0]
    assert written["isEnabled"] is False


# ---------------------------------------------------------------------------
# revert_tuning -- replays prior state, marks reverted
# ---------------------------------------------------------------------------


def test_revert_restores_prior_state(svc):
    prop = svc.propose_tuning(
        public_id="2009205", override_type="suppress",
        scope={"ip": "1.2.3.4/32"}, rationale="x",
    )
    applied = svc.apply_tuning(prop["token"])
    assert len(svc._client._detection["overrides"]) == 1  # applied

    rev = svc.revert_tuning(applied["handle"])
    # second PUT restores empty overrides
    assert len(svc._client.put_calls) == 2
    restored = svc._client.put_calls[1]
    assert restored["overrides"] == []
    assert restored["isEnabled"] is True
    # record marked reverted
    assert svc._store.get(applied["handle"])["status"] == "reverted"
    assert rev["status"] == "reverted"


def test_revert_unknown_handle_raises(svc):
    with pytest.raises(ProposalNotFoundError):
        svc.revert_tuning("nope")


def test_double_revert_is_rejected(svc):
    prop = svc.propose_tuning(
        public_id="2009205", override_type="suppress",
        scope={"ip": "1.2.3.4/32"}, rationale="x",
    )
    applied = svc.apply_tuning(prop["token"])
    svc.revert_tuning(applied["handle"])
    with pytest.raises(ValueError, match="already reverted"):
        svc.revert_tuning(applied["handle"])


# ---------------------------------------------------------------------------
# list_tunings
# ---------------------------------------------------------------------------


def test_list_tunings_shows_applied_only(svc):
    p1 = svc.propose_tuning(public_id="1111111", override_type="suppress",
                            scope={"ip": "1.1.1.1/32"}, rationale="a")
    a1 = svc.apply_tuning(p1["token"])
    p2 = svc.propose_tuning(public_id="2222222", override_type="suppress",
                            scope={"ip": "2.2.2.2/32"}, rationale="b")
    a2 = svc.apply_tuning(p2["token"])
    svc.revert_tuning(a2["handle"])

    listed = svc.list_tunings()
    handles = {r["handle"] for r in listed}
    assert a1["handle"] in handles
    assert a2["handle"] not in handles


# ---------------------------------------------------------------------------
# disposition -- recorded in the audit log too
# ---------------------------------------------------------------------------


def test_disposition_acknowledge_calls_client_and_audits(svc):
    out = svc.disposition_alerts(
        rule_uuid="2009208",
        date_range="2026/01/03 03:02:00 AM - 2026/01/04 03:02:00 PM",
        acknowledge=True,
    )
    assert len(svc._client.disposition_calls) == 1
    assert out["result"]["updated"] == svc._client.events_total
    # an audit record exists for the disposition
    assert any(r["override_type"] == "disposition" for r in svc._store.list_all())
