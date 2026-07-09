"""Pure tuning logic: build SO detection overrides and apply/revert them.

NO HTTP here -- this module only computes the exact change that the write path
(``SoClient.put_detection``) will PUT to ``/api/detection``. Keeping it pure
makes ``propose_tuning`` (validate + preview, no write) trivially testable and
side-effect free, which is the core safety property: an injected/adversarial
alert can shape a *proposal* but the proposal is just data until a human-gated
``apply`` PUTs it.

Override shapes are the VERIFIED SO 2.4 contract (from the live box + the
so-tune-detection-*-request.har captures, 2026-06-02; customFilter from SO's
own ``model/detection.go`` Override struct + ``PrepareForSigma``, 2026-07-09):

    suppress     -> {"type","isEnabled","note","track","ip"}            (suricata)
    threshold    -> {"type","isEnabled","note","thresholdType","track","count","seconds"} (suricata)
    modify       -> {"type","isEnabled","note","regex","value"}         (suricata)
    customFilter -> {"type","isEnabled","note","customFilter"}          (elastalert/Sigma)

``disable`` is special: SO disables a detection by flipping the top-level
``isEnabled`` to false, NOT by adding an override.

OVERRIDE TYPES ARE ENGINE-SPECIFIC (SO rejects a mismatch with an opaque
HTTP 400 at PUT time -- the trap this module's ``check_engine`` closes at
propose time): Suricata/NIDS rules take suppress/threshold/modify; Sigma
(engine ``elastalert``) rules take ONLY ``customFilter`` -- a YAML map whose
top-level ``sofilter*`` keys each hold a Sigma detection-style field map that
SO merges into the rule as an exclusion (``and not sofilter``), e.g.::

    sofilter:
      host.name: hal

The gateway builds that YAML from ``scope["filter"]`` (a flat dict of
field -> scalar-or-list), so callers never hand-write YAML.

A tuning is applied by PUTting the WHOLE detection object back with the new
``overrides``/``isEnabled`` -- so apply/revert here operate on the full
detection dict and never mutate their inputs (callers keep the prior state for
the audit/undo record).
"""

import copy
import ipaddress
import json
import re

# The override types the gateway can build. ``disable`` is handled specially
# (it flips isEnabled) but is listed so propose/validate accept it.
VALID_TYPES = frozenset({"suppress", "threshold", "modify", "disable", "customFilter"})

# Types that silence broadly -> the spec requires a louder/second confirm
# ("double-gated"). The gateway tags these so the workflow can enforce it.
DOUBLE_GATED_TYPES = frozenset({"disable", "modify"})

# Which override types each SO detection engine accepts (SO model/detection.go
# + the SOC UI's overrideTypes map). ``disable`` (top-level isEnabled flip)
# works for every engine.
ENGINE_TYPES = {
    "suricata": frozenset({"suppress", "threshold", "modify", "disable"}),
    "elastalert": frozenset({"customFilter", "disable"}),
    "strelka": frozenset({"disable"}),
}

_VALID_TRACK = frozenset({"by_src", "by_dst", "by_either"})
_VALID_THRESHOLD_TYPE = frozenset({"threshold", "limit", "both"})

# Sigma filter field names: dotted ECS-style paths (host.name, process.parent.executable).
_FILTER_FIELD = re.compile(r"^[A-Za-z0-9_@][A-Za-z0-9_@.\-]*$")


class InvalidTuningError(ValueError):
    """A proposed tuning is malformed (bad type, scope, or missing field).

    Raised by ``build_override`` so ``propose_tuning`` can reject injected /
    malformed input *before* any token is issued and long before any write.
    """


def check_engine(engine: str | None, override_type: str) -> None:
    """Reject an override type the detection's engine cannot take.

    SO only fails this at PUT time with an opaque ``400 The request could not
    be processed`` -- catching it here means propose_tuning refuses BEFORE a
    token is issued, with a message that says what to do instead.
    """
    allowed = ENGINE_TYPES.get((engine or "").lower())
    if allowed is None:
        return  # unknown engine: let SO be the judge rather than block
    if override_type not in allowed:
        hint = ""
        if engine == "elastalert":
            hint = (
                " Sigma/elastalert detections take a 'customFilter' override: "
                "re-propose with override_type='customFilter' and "
                "scope={'filter': {'<ecs.field>': '<value>', ...}}."
            )
        raise InvalidTuningError(
            f"override type {override_type!r} is not valid for engine {engine!r} "
            f"(allowed: {sorted(allowed)})." + hint
        )


def _yaml_scalar(value) -> str:
    """Render a scalar as safe YAML (JSON string quoting is valid YAML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _build_custom_filter_yaml(filter_map: dict) -> str:
    """Render ``scope['filter']`` as the sofilter YAML SO expects.

    One ``sofilter`` block; multiple fields AND together (Sigma map
    semantics), a list value means any-of for that field.
    """
    lines = ["sofilter:"]
    for field, value in filter_map.items():
        if not isinstance(field, str) or not _FILTER_FIELD.match(field):
            raise InvalidTuningError(f"invalid filter field name {field!r}")
        if isinstance(value, (list, tuple)):
            if not value:
                raise InvalidTuningError(f"filter field {field!r} has an empty list value")
            lines.append(f"  {field}:")
            for v in value:
                if isinstance(v, (dict, list, tuple)):
                    raise InvalidTuningError(f"filter field {field!r} values must be scalars")
                lines.append(f"    - {_yaml_scalar(v)}")
        elif isinstance(value, dict):
            raise InvalidTuningError(f"filter field {field!r} value must be a scalar or list")
        else:
            lines.append(f"  {field}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


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

    if override_type == "customFilter":
        filter_map = scope.get("filter")
        if not isinstance(filter_map, dict) or not filter_map:
            raise InvalidTuningError(
                "customFilter requires scope={'filter': {'<ecs.field>': <value>, ...}} "
                "(a non-empty field->value map; the gateway renders the sofilter YAML)"
            )
        return {
            "type": "customFilter",
            "isEnabled": True,
            "note": note,
            "customFilter": _build_custom_filter_yaml(filter_map),
        }

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
