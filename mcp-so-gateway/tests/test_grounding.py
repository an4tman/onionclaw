"""Tests for the grounding write path (propose/apply/revert over environment.md)."""

import pytest

from so_gateway import grounding
from so_gateway.grounding import (
    GroundingError,
    GroundingService,
    GroundingStore,
    ProposalNotFoundError,
    TokenAlreadyUsedError,
    insert_entry,
    normalize_entry,
    remove_entry,
)

TEMPLATE = """\
# Environment grounding

## Network

- LAN: `192.168.1.0/24`.

## Host table

| Host | Role and context for triage |
|---|---|
| `192.168.1.15` | **nas**: the server. |
| `192.168.1.255` | broadcast. |

## Known-noisy-but-benign (don't escalate the expected)

- ET INFO Suricata sigs.

## Documented false-positive baselines (contextualize each NARROWLY)

**nas / `.15`: platform-mismatch Sigma misfires.** Linux processes trip Windows rules.

## Telemetry coverage (state current coverage each cycle)

- Zeek L7 live.
"""


def _files(tmp_path, n=1):
    paths = []
    for i in range(n):
        p = tmp_path / f"environment-{i}.md"
        p.write_text(TEMPLATE, encoding="utf-8")
        paths.append(str(p))
    return paths


def _service(tmp_path, n=1):
    paths = _files(tmp_path, n)
    store = GroundingStore(str(tmp_path / "audit.sqlite"))
    return GroundingService(paths, store), paths


# -- normalize_entry ---------------------------------------------------------


def test_unknown_section_rejected():
    with pytest.raises(GroundingError, match="unknown section"):
        normalize_entry("secrets", "| x | y |")


def test_empty_entry_rejected():
    with pytest.raises(GroundingError, match="empty"):
        normalize_entry("known_noisy", "   \n ")


def test_heading_injection_rejected():
    with pytest.raises(GroundingError, match="headings"):
        normalize_entry("fp_baselines", "fine text\n## Host table\nhijack")


def test_host_table_entry_must_be_row():
    with pytest.raises(GroundingError, match="table row"):
        normalize_entry("host_table", "192.168.1.87 is the thermostat")
    with pytest.raises(GroundingError, match="single"):
        normalize_entry("host_table", "| a | b |\n| c | d |")


def test_bullet_sections_get_bullet_prefix():
    assert normalize_entry("known_noisy", "STUN traffic from consoles").startswith("- ")
    assert normalize_entry("coverage", "- already a bullet") == "- already a bullet"


# -- insert_entry / remove_entry ---------------------------------------------


def test_insert_host_row_lands_after_last_row():
    row = "| `192.168.1.87` | **thermostat**: MQTT to the nas only. |"
    text, line_no = insert_entry(TEMPLATE, "host_table", row)
    lines = text.splitlines()
    assert lines[line_no - 1] == row
    assert lines[line_no - 2] == "| `192.168.1.255` | broadcast. |"
    # the next section heading is still intact below the inserted row
    assert any(ln.startswith("## Known-noisy") for ln in lines[line_no:])


def test_insert_into_empty_section():
    content = "## Telemetry coverage (x)\n\n## Next\n\n- other\n"
    text, line_no = insert_entry(content, "coverage", "- new fact")
    lines = text.splitlines()
    assert lines[line_no - 1] == "- new fact"
    assert lines.index("## Next") > line_no - 1


def test_insert_missing_heading_errors_with_inventory():
    with pytest.raises(GroundingError, match="headings present"):
        insert_entry("# t\n\n## Something else\n", "host_table", "| a | b |")


def test_remove_entry_roundtrip():
    row = "| `192.168.1.87` | **thermostat**: MQTT only. |"
    text, _ = insert_entry(TEMPLATE, "host_table", row)
    removed, back = remove_entry(text, row)
    assert removed
    assert back == TEMPLATE


def test_remove_entry_absent():
    removed, text = remove_entry(TEMPLATE, "- never inserted")
    assert not removed
    assert text == TEMPLATE


# -- service: propose/apply/revert -------------------------------------------


def test_propose_is_read_only(tmp_path):
    svc, paths = _service(tmp_path)
    before = open(paths[0]).read()
    out = svc.propose_grounding(
        section="known_noisy", entry="game console STUN", rationale="operator said so"
    )
    assert out["token"]
    assert out["entry"] == "- game console STUN"
    assert out["files"][0]["insert_at_line"] > 0
    assert open(paths[0]).read() == before


def test_apply_writes_all_copies_and_reverts(tmp_path):
    svc, paths = _service(tmp_path, n=2)
    out = svc.propose_grounding(
        section="host_table",
        entry="| `192.168.1.87` | **thermostat**: MQTT to the nas only. |",
        rationale="operator: learn .87",
    )
    applied = svc.apply_grounding(out["token"])
    assert applied["status"] == "applied"
    for p in paths:
        assert "thermostat" in open(p).read()

    reverted = svc.revert_grounding(applied["handle"])
    assert reverted["status"] == "reverted"
    for p in paths:
        assert open(p).read() == TEMPLATE


def test_token_is_single_use(tmp_path):
    svc, _ = _service(tmp_path)
    out = svc.propose_grounding(
        section="coverage", entry="- DoH visibility gap", rationale="cycle finding"
    )
    svc.apply_grounding(out["token"])
    with pytest.raises(TokenAlreadyUsedError):
        svc.apply_grounding(out["token"])


def test_token_normalization(tmp_path):
    svc, paths = _service(tmp_path)
    out = svc.propose_grounding(
        section="known_noisy", entry="- backup rsync at 03:00", rationale="op"
    )
    pretty = out["token"].replace("-", " ").title()  # 'Amber Fox'
    applied = svc.apply_grounding(pretty)
    assert applied["status"] == "applied"
    assert "backup rsync" in open(paths[0]).read()


def test_unknown_token_and_handle(tmp_path):
    svc, _ = _service(tmp_path)
    with pytest.raises(ProposalNotFoundError):
        svc.apply_grounding("never-issued")
    with pytest.raises(ProposalNotFoundError):
        svc.revert_grounding("never-issued")


def test_partial_multifile_failure_rolls_back(tmp_path, monkeypatch):
    svc, paths = _service(tmp_path, n=2)
    out = svc.propose_grounding(
        section="known_noisy", entry="- transient", rationale="op"
    )

    real_write = grounding._atomic_write
    calls = {"n": 0}

    def flaky(path, text):
        calls["n"] += 1
        if calls["n"] == 2:  # second file's write blows up
            raise OSError("disk went away")
        real_write(path, text)

    monkeypatch.setattr(grounding, "_atomic_write", flaky)
    with pytest.raises(OSError):
        svc.apply_grounding(out["token"])
    monkeypatch.setattr(grounding, "_atomic_write", real_write)

    # first file rolled back; token still valid for a retry
    assert open(paths[0]).read() == TEMPLATE
    applied = svc.apply_grounding(out["token"])
    assert applied["status"] == "applied"


def test_revert_after_hand_edit_errors_and_stays_applied(tmp_path):
    svc, paths = _service(tmp_path)
    out = svc.propose_grounding(
        section="coverage", entry="- to be hand-edited", rationale="op"
    )
    applied = svc.apply_grounding(out["token"])
    # operator rewrites the line by hand
    content = open(paths[0]).read().replace("- to be hand-edited", "- rewritten")
    open(paths[0], "w").write(content)

    with pytest.raises(ValueError, match="by hand"):
        svc.revert_grounding(applied["handle"])
    assert svc.list_groundings()[0]["handle"] == applied["handle"]


def test_list_pending_and_groundings(tmp_path):
    svc, _ = _service(tmp_path)
    out = svc.propose_grounding(
        section="known_noisy", entry="- pending thing", rationale="op"
    )
    pending = svc.list_pending()
    assert pending[0]["kind"] == "grounding"
    assert pending[0]["token"] == out["token"]
    applied = svc.apply_grounding(out["token"])
    assert svc.list_pending() == []
    listed = svc.list_groundings()
    assert listed[0]["handle"] == applied["handle"]
    assert listed[0]["section"] == "known_noisy"


def test_unconfigured_service_refuses(tmp_path):
    store = GroundingStore(str(tmp_path / "audit.sqlite"))
    svc = GroundingService([], store)
    with pytest.raises(GroundingError, match="not configured"):
        svc.propose_grounding(section="coverage", entry="- x", rationale="r")


def test_store_survives_reopen(tmp_path):
    svc, paths = _service(tmp_path)
    out = svc.propose_grounding(
        section="known_noisy", entry="- durable fact", rationale="op"
    )
    applied = svc.apply_grounding(out["token"])

    # New store/service over the same DB (a container restart): the record and
    # revert path survive; only pendings are in-memory.
    store2 = GroundingStore(str(tmp_path / "audit.sqlite"))
    svc2 = GroundingService(paths, store2)
    assert svc2.list_groundings()[0]["handle"] == applied["handle"]
    reverted = svc2.revert_grounding(applied["handle"])
    assert reverted["status"] == "reverted"
    assert open(paths[0]).read() == TEMPLATE
