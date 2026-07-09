"""Pure tuning logic: build SO detection overrides and apply/revert them.

NO HTTP here -- this module only computes the exact change that the write path
(``SoClient.put_detection``) will PUT to ``/api/detection``. Keeping it pure
makes ``propose_tuning`` (validate + preview, no write) trivially testable and
side-effect free, which is the core safety property: an injected/adversarial
alert can shape a *proposal* but the proposal is just data until a human-gated
``apply`` PUTs it.

Override shapes are the VERIFIED SO 2.4 contract (from the live box + the
so-tune-detection-*-request.har captures, 2026-06-02):

    suppress  -> {"type","isEnabled","note","track","ip"}
    threshold -> {"type","isEnabled","note","thresholdType","track","count","seconds"}
    modify    -> {"type","isEnabled","note","regex","value"}

``disable`` is special: SO disables a detection by flipping the top-level
``isEnabled`` to false, NOT by adding an override.

A tuning is applied by PUTting the WHOLE detection object back with the new
``overrides``/``isEnabled`` -- so apply/revert here operate on the full
detection dict and never mutate their inputs (callers keep the prior state for
the audit/undo record).
"""

import copy
import ipaddress

# The override types the gateway can build. ``disable`` is handled specially
# (it flips isEnabled) but is listed so propose/validate accept it.
VALID_TYPES = frozenset({"suppress", "threshold", "modify", "disable"})

# Types that silence broadly -> the spec requires a louder/second confirm
# ("double-gated"). The gateway tags these so the workflow can enforce it.
DOUBLE_GATED_TYPES = frozenset({"disable", "modify"})

_VALID_TRACK = frozenset({"by_src", "by_dst", "by_either"})
_VALID_THRESHOLD_TYPE = frozenset({"threshold", "limit", "both"})


class InvalidTuningError(ValueError):
    """A proposed tuning is malformed (bad type, scope, or missing field).

    Raised by ``build_override`` so ``propose_tuning`` can reject injected /
    malformed input *before* any token is issued and long before any write.
    """


def _validate_ip(value: str) -> str:
    """Validate a host IP or CIDR; return it unchanged. Raise otherwise."""
    if not isinstance(value, str) or not value:
        raise InvalidTuningError("suppress requires a non-empty 'ip' (host or CIDR)")
    try:
        if "/" in value:
            ipaddress.ip_network(value, strict=False)
        else:
            ipaddress.ip_address(value)
    except ValueError as exc:
        raise InvalidTuningError(f"invalid 'ip' {value!r}: {exc}") from None
    return value


def build_override(override_type: str, scope: dict, note: str) -> dict:
    """Build the exact SO override dict for *override_type* from *scope*.

    *override_type*: one of suppress / threshold / modify / disable.
    *scope*: the type-specific parameters (see module docstring).
    *note*: human-readable rationale recorded on the override (required).

    For ``disable`` the returned dict is a sentinel ``{"type":"disable","note":...}``
    consumed by ``apply_override`` (which flips ``isEnabled``) -- it is NOT an
    SO override entry.

    Raises :class:`InvalidTuningError` on any malformed input.
    """
    if override_type not in VALID_TYPES:
        raise InvalidTuningError(
            f"unknown tuning type {override_type!r}; expected one of {sorted(VALID_TYPES)}"
        )
    if not note or not str(note).strip():
        raise InvalidTuningError("a non-empty 'note' (rationale) is required")
    scope = scope or {}

    if override_type == "disable":
        # Sentinel; apply_override turns this into isEnabled=false.
        return {"type": "disable", "isEnabled": True, "note": note}

    if override_type == "suppress":
        track = scope.get("track", "by_either")
        if track not in _VALID_TRACK:
            raise InvalidTuningError(
                f"invalid 'track' {track!r}; expected one of {sorted(_VALID_TRACK)}"
            )
        ip = _validate_ip(scope.get("ip", ""))
        return {
            "type": "suppress",
            "isEnabled": True,
            "note": note,
            "track": track,
            "ip": ip,
        }

    if override_type == "threshold":
        ttype = scope.get("thresholdType")
        if ttype not in _VALID_THRESHOLD_TYPE:
            raise InvalidTuningError(
                f"invalid 'thresholdType' {ttype!r}; expected one of "
                f"{sorted(_VALID_THRESHOLD_TYPE)}"
            )
        track = scope.get("track", "by_src")
        if track not in _VALID_TRACK:
            raise InvalidTuningError(
                f"invalid 'track' {track!r}; expected one of {sorted(_VALID_TRACK)}"
            )
        count = scope.get("count")
        seconds = scope.get("seconds")
        if not isinstance(count, int) or count <= 0:
            raise InvalidTuningError("threshold requires a positive integer 'count'")
        if not isinstance(seconds, int) or seconds <= 0:
            raise InvalidTuningError("threshold requires a positive integer 'seconds'")
        return {
            "type": "threshold",
            "isEnabled": True,
            "note": note,
            "thresholdType": ttype,
            "track": track,
            "count": count,
            "seconds": seconds,
        }

    # modify
    regex = scope.get("regex")
    value = scope.get("value")
    if not regex or value is None:
        raise InvalidTuningError("modify requires non-empty 'regex' and 'value'")
    return {
        "type": "modify",
        "isEnabled": True,
        "note": note,
        "regex": regex,
        "value": value,
    }


def apply_override(detection: dict, override: dict) -> dict:
    """Return a NEW detection dict with *override* applied (input untouched).

    For ``disable`` the result flips ``isEnabled`` to False and leaves
    ``overrides`` unchanged. For every other type the override is appended to
    the detection's ``overrides`` list. This new dict is what gets PUT back to
    ``/api/detection``.
    """
    new = copy.deepcopy(detection)
    if override.get("type") == "disable":
        new["isEnabled"] = False
        return new
    overrides = list(new.get("overrides") or [])
    overrides.append(override)
    new["overrides"] = overrides
    return new


def revert_detection_state(current: dict, prior: dict) -> dict:
    """Return a NEW detection dict restoring *prior* state onto *current*.

    *prior* is the captured pre-apply state ``{"isEnabled", "overrides"}``.
    We restore those two tuning-bearing fields onto a copy of the CURRENT
    detection (so any unrelated SO-side fields stay current). Identity fields
    (id, publicId, content, ...) come from *current*.
    """
    new = copy.deepcopy(current)
    new["isEnabled"] = prior["isEnabled"]
    new["overrides"] = copy.deepcopy(prior["overrides"])
    return new


def capture_prior_state(detection: dict) -> dict:
    """Snapshot the tuning-bearing fields of *detection* for the undo record."""
    return {
        "isEnabled": detection.get("isEnabled", True),
        "overrides": copy.deepcopy(detection.get("overrides") or []),
    }
