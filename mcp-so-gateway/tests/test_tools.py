"""Tests for MCP tool functions in so_gateway.server.

Tool functions are tested in isolation by injecting a fake client via
``so_gateway.server._client``.  Each test resets _client to None on teardown
so there is no state leak between tests.

Verified 2026-06-01: get_playbook returns a list; run_guided_analysis accepts
public_id (rule.uuid), alert_fields, and optional range.
"""

import pytest

import so_gateway.server as server
from so_gateway.server import get_detection, get_playbook, ping, run_guided_analysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLAYBOOK_SENTINEL = [
    {
        "name": "Test Playbook",
        "id": "1600005",
        "questions": [{"question": "q1", "context": None, "range": None, "query": "..."}],
    }
]

GUIDED_ANALYSIS_SENTINEL = [
    {
        "question": "q1",
        "context": None,
        "query": "converted",
        "fields": ["host.name"],
        "events": [],
    }
]


class _FakeClient:
    """Minimal stand-in for SoClient — no real HTTP."""

    def get_detection(self, detection_id: str) -> dict:
        return {"_fake_detection": detection_id}

    def get_playbook(self, public_id: str) -> list:
        return PLAYBOOK_SENTINEL

    def run_guided_analysis(
        self, public_id: str, alert_fields: dict, range: str | None = None
    ) -> list:
        return GUIDED_ANALYSIS_SENTINEL


@pytest.fixture(autouse=True)
def reset_client():
    """Ensure _client is always None before and after each test."""
    server._client = None
    yield
    server._client = None


# ---------------------------------------------------------------------------
# ping (unchanged behaviour)
# ---------------------------------------------------------------------------


def test_ping_returns_ready():
    assert ping() == "Ready"


# ---------------------------------------------------------------------------
# get_detection tool
# ---------------------------------------------------------------------------


def test_get_detection_delegates_to_client():
    server._client = _FakeClient()
    result = get_detection("det-abc")
    assert result == {"_fake_detection": "det-abc"}


def test_get_detection_passes_id_through():
    server._client = _FakeClient()
    assert get_detection("xyz-999") == {"_fake_detection": "xyz-999"}


# ---------------------------------------------------------------------------
# get_playbook tool — returns a list (verified 2026-06-01)
# ---------------------------------------------------------------------------


def test_get_playbook_delegates_to_client():
    server._client = _FakeClient()
    result = get_playbook("02773bed-83bf-469f-b7ff-e676e7d78bab")
    assert result == PLAYBOOK_SENTINEL


def test_get_playbook_returns_list():
    server._client = _FakeClient()
    result = get_playbook("any-uuid")
    assert isinstance(result, list)


def test_get_playbook_passes_public_id_through():
    """The tool must forward the public_id (rule.uuid) unchanged."""
    calls = []

    class _RecordingClient(_FakeClient):
        def get_playbook(self, public_id: str) -> list:
            calls.append(public_id)
            return PLAYBOOK_SENTINEL

    server._client = _RecordingClient()
    get_playbook("02773bed-83bf-469f-b7ff-e676e7d78bab")
    assert calls == ["02773bed-83bf-469f-b7ff-e676e7d78bab"]


# ---------------------------------------------------------------------------
# run_guided_analysis tool
# ---------------------------------------------------------------------------


def test_run_guided_analysis_delegates_to_client():
    server._client = _FakeClient()
    result = run_guided_analysis("det-abc", {"soc_id": "Kl7yjpsBEEV8H2qfvz4w"})
    assert result == GUIDED_ANALYSIS_SENTINEL


def test_run_guided_analysis_passes_args_through():
    """The tool must forward public_id, alert_fields, and range unchanged."""
    calls = []

    class _RecordingClient(_FakeClient):
        def run_guided_analysis(
            self, public_id: str, alert_fields: dict, range: str | None = None
        ) -> list:
            calls.append((public_id, alert_fields, range))
            return []

    server._client = _RecordingClient()
    run_guided_analysis(
        "02773bed-83bf-469f-b7ff-e676e7d78bab",
        {"soc_id": "Kl7yjpsBEEV8H2qfvz4w"},
        range="2026/01/05 10:00:00 AM - 2026/01/05 12:00:00 PM",
    )
    assert calls == [
        (
            "02773bed-83bf-469f-b7ff-e676e7d78bab",
            {"soc_id": "Kl7yjpsBEEV8H2qfvz4w"},
            "2026/01/05 10:00:00 AM - 2026/01/05 12:00:00 PM",
        )
    ]


def test_run_guided_analysis_range_defaults_to_none():
    """When range is not provided, the tool passes None to the client."""
    calls = []

    class _RecordingClient(_FakeClient):
        def run_guided_analysis(
            self, public_id: str, alert_fields: dict, range: str | None = None
        ) -> list:
            calls.append(range)
            return []

    server._client = _RecordingClient()
    run_guided_analysis("uuid", {"soc_id": "abc"})
    assert calls == [None]
