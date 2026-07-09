"""Sigma (elastalert) tuning: customFilter overrides + the engine-aware gate.

Found live 2026-07-09: the cycle proposed a Suricata-style suppress on a Sigma
rule; SO rejected the PUT with an opaque 400 only at APPLY time, burning the
operator's approval. The gateway now (a) builds proper customFilter overrides
(SO model/detection.go contract) and (b) refuses engine-mismatched proposals
pre-token with a corrective message.
"""

import pytest

from so_gateway import tuning
from so_gateway.tuning import InvalidTuningError
from so_gateway.tuning_service import TuningService
from so_gateway.tuning_store import TuningStore

from test_tuning_service import DETECTION, FakeClient

SIGMA_DETECTION = dict(DETECTION, engine="elastalert", language="sigma", overrides=None)


def _svc(tmp_path, detection):
    return TuningService(FakeClient(detection), TuningStore(str(tmp_path / "s.sqlite")))


# -- build_override: customFilter --------------------------------------------


def test_custom_filter_yaml_single_field():
    o = tuning.build_override("customFilter", {"filter": {"host.name": "nas"}}, "benign")
    assert o["type"] == "customFilter"
    assert o["isEnabled"] is True
    assert o["customFilter"] == 'sofilter:\n  host.name: "nas"\n'


def test_custom_filter_yaml_multi_field_and_list():
    o = tuning.build_override(
        "customFilter",
        {"filter": {"host.name": "nas", "process.name": ["cat", "grep"]}},
        "benign",
    )
    y = o["customFilter"]
    assert 'host.name: "nas"' in y
    assert 'process.name:\n    - "cat"\n    - "grep"' in y


def test_custom_filter_rejects_bad_shapes():
    with pytest.raises(InvalidTuningError):
        tuning.build_override("customFilter", {}, "benign")  # no filter
    with pytest.raises(InvalidTuningError):
        tuning.build_override("customFilter", {"filter": {}}, "benign")  # empty
    with pytest.raises(InvalidTuningError):
        tuning.build_override(
            "customFilter", {"filter": {"bad field!": "x"}}, "benign"
        )  # invalid field name
    with pytest.raises(InvalidTuningError):
        tuning.build_override(
            "customFilter", {"filter": {"host.name": {"nested": 1}}}, "benign"
        )  # non-scalar value


def test_custom_filter_quotes_injection_safely():
    o = tuning.build_override(
        "customFilter",
        {"filter": {"process.command_line": 'x" OR 1\nsofilter_evil:\n  a: b'}},
        "benign",
    )
    # the whole hostile value stays inside ONE quoted YAML scalar on ONE line
    # (json.dumps escapes the newlines), so it cannot smuggle extra YAML keys
    lines = o["customFilter"].rstrip("\n").split("\n")
    assert lines[0] == "sofilter:"
    assert len(lines) == 2 and lines[1].startswith('  process.command_line: "')


# -- the engine gate ----------------------------------------------------------


def test_engine_gate_rejects_suppress_on_sigma(tmp_path):
    svc = _svc(tmp_path, SIGMA_DETECTION)
    with pytest.raises(InvalidTuningError) as e:
        svc.propose_tuning(
            public_id="818f7b24",
            override_type="suppress",
            scope={"track": "by_src", "ip": "10.0.0.15/32"},
            rationale="fp",
        )
    assert "customFilter" in str(e.value)  # the corrective hint
    assert svc.list_pending() == []  # refused pre-token


def test_engine_gate_rejects_custom_filter_on_suricata(tmp_path):
    svc = _svc(tmp_path, DETECTION)  # engine: suricata
    with pytest.raises(InvalidTuningError):
        svc.propose_tuning(
            public_id="2009205",
            override_type="customFilter",
            scope={"filter": {"host.name": "nas"}},
            rationale="fp",
        )


def test_disable_allowed_for_both_engines():
    tuning.check_engine("suricata", "disable")
    tuning.check_engine("elastalert", "disable")
    tuning.check_engine(None, "suppress")  # unknown engine: not blocked


# -- end-to-end on a Sigma detection ------------------------------------------


def test_sigma_propose_apply_revert_roundtrip(tmp_path):
    svc = _svc(tmp_path, SIGMA_DETECTION)
    out = svc.propose_tuning(
        public_id="818f7b24",
        override_type="customFilter",
        scope={"filter": {"host.name": "nas"}},
        rationale="Unraid API polling misfires this Windows rule",
    )
    assert out["override"]["customFilter"].startswith("sofilter:")
    applied = svc.apply_tuning(out["token"])
    assert applied["status"] == "applied"
    reverted = svc.revert_tuning(applied["handle"])
    assert reverted["status"] == "reverted"
