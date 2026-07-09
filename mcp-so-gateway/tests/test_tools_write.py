"""Tests for the WRITE MCP tool functions in so_gateway.server.

Tool functions delegate to an injected TuningService (server._tuning_service).
We inject a fake service and assert the tools forward args + return its output.
"""

import pytest

import so_gateway.server as server
from so_gateway.server import (
    apply_tuning,
    disposition_alerts,
    list_tunings,
    propose_tuning,
    revert_tuning,
)


class _FakeService:
    def __init__(self):
        self.calls = []

    def propose_tuning(self, **kw):
        self.calls.append(("propose", kw))
        return {"token": "T", **kw}

    def apply_tuning(self, token):
        self.calls.append(("apply", token))
        return {"handle": "H", "status": "applied"}

    def revert_tuning(self, handle):
        self.calls.append(("revert", handle))
        return {"handle": handle, "status": "reverted"}

    def list_tunings(self):
        self.calls.append(("list", None))
        return [{"handle": "H"}]

    def disposition_alerts(self, **kw):
        self.calls.append(("disposition", kw))
        return {"handle": "H", "status": "dispositioned"}


@pytest.fixture(autouse=True)
def fake_service():
    fake = _FakeService()
    server._tuning_service = fake
    yield fake
    server._tuning_service = None


def test_propose_tuning_forwards_args(fake_service):
    out = propose_tuning(
        public_id="2009205",
        override_type="suppress",
        scope={"ip": "1.2.3.4/32"},
        rationale="benign",
    )
    assert out["token"] == "T"
    assert fake_service.calls[0][0] == "propose"
    assert fake_service.calls[0][1]["public_id"] == "2009205"
    assert fake_service.calls[0][1]["override_type"] == "suppress"


def test_apply_tuning_forwards_token(fake_service):
    out = apply_tuning("T")
    assert out["status"] == "applied"
    assert fake_service.calls[0] == ("apply", "T")


def test_revert_tuning_forwards_handle(fake_service):
    out = revert_tuning("H")
    assert out["status"] == "reverted"
    assert fake_service.calls[0] == ("revert", "H")


def test_list_tunings_delegates(fake_service):
    out = list_tunings()
    assert out == [{"handle": "H"}]
    assert fake_service.calls[0] == ("list", None)


def test_disposition_alerts_forwards_args(fake_service):
    out = disposition_alerts(
        rule_uuid="2009208",
        date_range="2026/01/03 03:02:00 AM - 2026/01/04 03:02:00 PM",
        acknowledge=True,
        escalate=False,
    )
    assert out["status"] == "dispositioned"
    assert fake_service.calls[0][0] == "disposition"
    assert fake_service.calls[0][1]["rule_uuid"] == "2009208"
