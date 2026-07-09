"""Tests for the pure tuning logic in so_gateway.tuning.

These cover building the exact SO override dicts (verified against the live
PUT /api/detection HAR captures, so-tune-detection-*-request.har) and applying
them to a detection object, with NO HTTP. The override shapes are the verified
SO 2.4 contract:

  suppress  -> {type, isEnabled, note, track, ip}
  threshold -> {type, isEnabled, note, thresholdType, track, count, seconds}
  modify    -> {type, isEnabled, note, regex, value}

Verified 2026-06-02 against ~/so-agent/specs/research/so-tune-detection-*.har
and a live GET /api/detection/public/2009205 on the box.
"""

import pytest

from so_gateway.tuning import (
    InvalidTuningError,
    apply_override,
    build_override,
    revert_detection_state,
)

# A minimal detection object in the SO 2.4 PUT shape (the fields that matter
# for tuning: id, publicId, isEnabled, overrides). Extra fields are preserved.
DETECTION = {
    "id": "Pj_lqJcBPiDhvlxwuZTf",
    "publicId": "2009205",
    "title": "ET MALWARE KEYPLUG/Conficker test",
    "isEnabled": True,
    "engine": "suricata",
    "language": "suricata",
    "content": "alert udp ...; sid:2009205; rev:5;)",
    "overrides": [],
}


# ---------------------------------------------------------------------------
# build_override — suppress
# ---------------------------------------------------------------------------


def test_build_suppress_override():
    ov = build_override(
        "suppress",
        scope={"track": "by_src", "ip": "10.0.0.19/32"},
        note="benign Spotify P2P from edison",
    )
    assert ov["type"] == "suppress"
    assert ov["isEnabled"] is True
    assert ov["track"] == "by_src"
    assert ov["ip"] == "10.0.0.19/32"
    assert ov["note"] == "benign Spotify P2P from edison"
    # suppress must NOT carry threshold/modify-only keys
    assert "count" not in ov
    assert "regex" not in ov


def test_build_suppress_default_track_is_by_either():
    ov = build_override("suppress", scope={"ip": "1.2.3.4/32"}, note="n")
    assert ov["track"] == "by_either"


def test_build_suppress_requires_ip():
    with pytest.raises(InvalidTuningError, match="ip"):
        build_override("suppress", scope={"track": "by_src"}, note="n")


def test_build_suppress_rejects_bad_track():
    with pytest.raises(InvalidTuningError, match="track"):
        build_override(
            "suppress", scope={"track": "sideways", "ip": "1.2.3.4/32"}, note="n"
        )


def test_build_suppress_rejects_bad_ip():
    with pytest.raises(InvalidTuningError, match="ip"):
        build_override(
            "suppress", scope={"track": "by_src", "ip": "not-an-ip"}, note="n"
        )


def test_build_suppress_accepts_bare_ip_and_cidr():
    # bare host
    ov = build_override("suppress", scope={"ip": "10.0.0.5"}, note="n")
    assert ov["ip"] == "10.0.0.5"
    # cidr range
    ov2 = build_override("suppress", scope={"ip": "10.0.0.0/24"}, note="n")
    assert ov2["ip"] == "10.0.0.0/24"


# ---------------------------------------------------------------------------
# build_override — threshold
# ---------------------------------------------------------------------------


def test_build_threshold_override():
    ov = build_override(
        "threshold",
        scope={
            "thresholdType": "limit",
            "track": "by_dst",
            "count": 1,
            "seconds": 60,
        },
        note="limit to one alert per 60s per dst",
    )
    assert ov["type"] == "threshold"
    assert ov["isEnabled"] is True
    assert ov["thresholdType"] == "limit"
    assert ov["track"] == "by_dst"
    assert ov["count"] == 1
    assert ov["seconds"] == 60


def test_build_threshold_requires_count_and_seconds():
    with pytest.raises(InvalidTuningError):
        build_override(
            "threshold",
            scope={"thresholdType": "limit", "track": "by_dst", "count": 1},
            note="n",
        )


def test_build_threshold_rejects_bad_threshold_type():
    with pytest.raises(InvalidTuningError, match="thresholdType"):
        build_override(
            "threshold",
            scope={
                "thresholdType": "bogus",
                "track": "by_dst",
                "count": 1,
                "seconds": 60,
            },
            note="n",
        )


def test_build_threshold_rejects_non_positive_count():
    with pytest.raises(InvalidTuningError, match="count"):
        build_override(
            "threshold",
            scope={
                "thresholdType": "limit",
                "track": "by_dst",
                "count": 0,
                "seconds": 60,
            },
            note="n",
        )


# ---------------------------------------------------------------------------
# build_override — modify (double-gated type; shape still validated here)
# ---------------------------------------------------------------------------


def test_build_modify_override():
    ov = build_override(
        "modify",
        scope={"regex": "foo", "value": "bar"},
        note="rewrite",
    )
    assert ov["type"] == "modify"
    assert ov["isEnabled"] is True
    assert ov["regex"] == "foo"
    assert ov["value"] == "bar"


def test_build_modify_requires_regex_and_value():
    with pytest.raises(InvalidTuningError):
        build_override("modify", scope={"regex": "foo"}, note="n")


# ---------------------------------------------------------------------------
# build_override — unknown type
# ---------------------------------------------------------------------------


def test_build_override_unknown_type():
    with pytest.raises(InvalidTuningError, match="type"):
        build_override("frobnicate", scope={}, note="n")


def test_build_override_requires_note():
    with pytest.raises(InvalidTuningError, match="note"):
        build_override("suppress", scope={"ip": "1.2.3.4"}, note="")


# ---------------------------------------------------------------------------
# apply_override — produces the new detection to PUT (suppress/threshold/modify)
# ---------------------------------------------------------------------------


def test_apply_override_appends_to_overrides_and_keeps_enabled():
    ov = build_override("suppress", scope={"ip": "1.2.3.4/32"}, note="n")
    new = apply_override(DETECTION, ov)
    # appended (original had 0)
    assert len(new["overrides"]) == 1
    assert new["overrides"][-1] == ov
    # detection stays enabled for a suppress/threshold/modify tuning
    assert new["isEnabled"] is True
    # other fields preserved
    assert new["publicId"] == "2009205"
    assert new["content"] == DETECTION["content"]


def test_apply_override_does_not_mutate_input():
    ov = build_override("suppress", scope={"ip": "1.2.3.4/32"}, note="n")
    apply_override(DETECTION, ov)
    # input detection's overrides list is untouched
    assert DETECTION["overrides"] == []


def test_apply_override_preserves_existing_overrides():
    det = dict(DETECTION)
    existing = {"type": "modify", "isEnabled": False, "note": "old", "regex": "*", "value": "1"}
    det["overrides"] = [existing]
    ov = build_override("suppress", scope={"ip": "9.9.9.9/32"}, note="new")
    new = apply_override(det, ov)
    assert len(new["overrides"]) == 2
    assert new["overrides"][0] == existing
    assert new["overrides"][1] == ov


# ---------------------------------------------------------------------------
# disable — special: flips detection isEnabled, not an override
# ---------------------------------------------------------------------------


def test_apply_disable_flips_is_enabled_false():
    ov = build_override("disable", scope={}, note="too noisy, disabling")
    new = apply_override(DETECTION, ov)
    assert new["isEnabled"] is False
    # disable does not add an override entry
    assert new["overrides"] == DETECTION["overrides"]


# ---------------------------------------------------------------------------
# revert_detection_state — restores a captured prior state exactly
# ---------------------------------------------------------------------------


def test_revert_restores_prior_overrides_and_enabled():
    prior = {
        "isEnabled": True,
        "overrides": [{"type": "suppress", "isEnabled": False, "note": "x",
                       "track": "by_src", "ip": "5.5.5.5/32"}],
    }
    # current (post-apply) state has an extra override + maybe disabled
    current = dict(DETECTION)
    current["isEnabled"] = False
    current["overrides"] = [
        {"type": "suppress", "isEnabled": False, "note": "x", "track": "by_src", "ip": "5.5.5.5/32"},
        {"type": "threshold", "isEnabled": True, "note": "new", "thresholdType": "limit",
         "track": "by_dst", "count": 1, "seconds": 60},
    ]
    reverted = revert_detection_state(current, prior)
    assert reverted["isEnabled"] is True
    assert reverted["overrides"] == prior["overrides"]
    # untouched identity fields preserved from current
    assert reverted["id"] == current["id"]
    assert reverted["publicId"] == current["publicId"]
