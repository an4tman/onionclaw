"""Tests for the audit/undo store (so_gateway.tuning_store).

The store is the HARD-SAFETY backbone: every applied write records what changed,
when, and the exact prior state -- so revert is a faithful replay and the gateway
keeps a tamper-evident audit trail. SQLite, file-backed; tests use :memory: or a
tmp file.
"""

import pytest

from so_gateway.tuning_store import TuningStore


@pytest.fixture
def store(tmp_path):
    return TuningStore(str(tmp_path / "audit.sqlite"))


def test_record_apply_returns_handle(store):
    handle = store.record_apply(
        public_id="2009205",
        detection_id="Pj_lqJcBPiDhvlxwuZTf",
        override_type="suppress",
        applied_override={"type": "suppress", "ip": "1.2.3.4/32"},
        prior_state={"isEnabled": True, "overrides": []},
        rationale="benign test",
        review_horizon_days=90,
    )
    assert isinstance(handle, str)
    assert len(handle) >= 8


def test_get_returns_recorded_fields(store):
    handle = store.record_apply(
        public_id="2009205",
        detection_id="ESID1",
        override_type="threshold",
        applied_override={"type": "threshold", "count": 1},
        prior_state={"isEnabled": True, "overrides": [{"type": "modify"}]},
        rationale="why",
        review_horizon_days=30,
    )
    rec = store.get(handle)
    assert rec["public_id"] == "2009205"
    assert rec["detection_id"] == "ESID1"
    assert rec["override_type"] == "threshold"
    assert rec["applied_override"] == {"type": "threshold", "count": 1}
    assert rec["prior_state"] == {"isEnabled": True, "overrides": [{"type": "modify"}]}
    assert rec["rationale"] == "why"
    assert rec["review_horizon_days"] == 30
    assert rec["status"] == "applied"
    assert rec["applied_at"]  # timestamp present


def test_get_unknown_handle_returns_none(store):
    assert store.get("nope") is None


def test_mark_reverted_updates_status(store):
    handle = store.record_apply(
        public_id="p", detection_id="d", override_type="suppress",
        applied_override={}, prior_state={"isEnabled": True, "overrides": []},
        rationale="r", review_horizon_days=None,
    )
    store.mark_reverted(handle)
    rec = store.get(handle)
    assert rec["status"] == "reverted"
    assert rec["reverted_at"]


def test_list_applied_excludes_reverted(store):
    h1 = store.record_apply(
        public_id="p1", detection_id="d1", override_type="suppress",
        applied_override={}, prior_state={"isEnabled": True, "overrides": []},
        rationale="r", review_horizon_days=None,
    )
    h2 = store.record_apply(
        public_id="p2", detection_id="d2", override_type="threshold",
        applied_override={}, prior_state={"isEnabled": True, "overrides": []},
        rationale="r", review_horizon_days=None,
    )
    store.mark_reverted(h2)
    applied = store.list_applied()
    handles = {r["handle"] for r in applied}
    assert h1 in handles
    assert h2 not in handles


def test_list_all_includes_reverted(store):
    h = store.record_apply(
        public_id="p", detection_id="d", override_type="suppress",
        applied_override={}, prior_state={"isEnabled": True, "overrides": []},
        rationale="r", review_horizon_days=None,
    )
    store.mark_reverted(h)
    all_recs = store.list_all()
    assert any(r["handle"] == h and r["status"] == "reverted" for r in all_recs)


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "audit.sqlite")
    s1 = TuningStore(path)
    h = s1.record_apply(
        public_id="p", detection_id="d", override_type="suppress",
        applied_override={"x": 1}, prior_state={"isEnabled": True, "overrides": []},
        rationale="r", review_horizon_days=None,
    )
    # New instance, same file -> record survives.
    s2 = TuningStore(path)
    rec = s2.get(h)
    assert rec is not None
    assert rec["applied_override"] == {"x": 1}


def test_handles_are_unique(store):
    handles = {
        store.record_apply(
            public_id="p", detection_id="d", override_type="suppress",
            applied_override={}, prior_state={"isEnabled": True, "overrides": []},
            rationale="r", review_horizon_days=None,
        )
        for _ in range(20)
    }
    assert len(handles) == 20
