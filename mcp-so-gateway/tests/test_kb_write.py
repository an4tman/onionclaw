"""Tests for the gated kb write path (append + edit over wiki pages)."""

import pytest

from so_gateway.kb_write import (
    KbWriteError,
    KbWriteService,
    KbWriteStore,
    append_under_heading,
    replace_once,
)
from so_gateway.grounding import ProposalNotFoundError, TokenAlreadyUsedError

PAGE = """\
---
title: Test page
---

# Test page

Intro text about the thing.

## Access

- ssh me@box, key auth.

## Gotchas

- The port is 9200 and it is grid-internal.

### Sub-gotcha

- nested detail.

## Status

Everything nominal.
"""


def _setup(tmp_path):
    kb = tmp_path / "kb"
    (kb / "security").mkdir(parents=True)
    page = kb / "security" / "test-page.md"
    page.write_text(PAGE, encoding="utf-8")
    svc = KbWriteService(str(kb), KbWriteStore(str(tmp_path / "audit.sqlite")))
    return svc, page


# -- pure helpers -------------------------------------------------------------


def test_append_lands_at_section_end_respecting_sublevels():
    text, line_no = append_under_heading(PAGE, "Gotchas", "- new gotcha")
    lines = text.splitlines()
    assert lines[line_no - 1] == "- new gotcha"
    # lands after the ### Sub-gotcha content, blank line preserved, before ## Status
    assert lines.index("## Status") == line_no + 1


def test_append_ambiguous_and_missing_headings():
    with pytest.raises(KbWriteError, match="ambiguous"):
        append_under_heading(PAGE, "gotcha", "- x")  # matches Gotchas + Sub-gotcha
    with pytest.raises(KbWriteError, match="headings present"):
        append_under_heading(PAGE, "Nonexistent", "- x")


def test_replace_once_requires_unique_match():
    out = replace_once(PAGE, "Everything nominal.", "Everything on fire.")
    assert "on fire" in out
    with pytest.raises(KbWriteError, match="not found"):
        replace_once(PAGE, "never present", "x")
    with pytest.raises(KbWriteError, match="matches 2"):
        replace_once(PAGE + "\nEverything nominal.\n", "Everything nominal.", "x")


# -- service ------------------------------------------------------------------


def test_append_roundtrip(tmp_path):
    svc, page = _setup(tmp_path)
    before = page.read_text()
    prop = svc.propose_kb_append(
        path="kb/security/test-page.md",
        heading="Access",
        entry="- web UI on :8443 too.",
        rationale="operator said so",
    )
    assert prop["double_gated"] is False
    assert before == page.read_text()  # propose is read-only

    applied = svc.apply_kb(prop["token"])
    assert applied["kind"] == "append"
    assert "- web UI on :8443 too." in page.read_text()

    reverted = svc.revert_kb(applied["handle"])
    assert reverted["status"] == "reverted"
    assert page.read_text() == before


def test_edit_roundtrip_double_gated(tmp_path):
    svc, page = _setup(tmp_path)
    before = page.read_text()
    prop = svc.propose_kb_edit(
        path="security/test-page.md",  # leading kb/ optional
        old_text="The port is 9200 and it is grid-internal.",
        new_text="The port is 9200; open to the docker host since 2026-07.",
        rationale="observed live",
    )
    assert prop["double_gated"] is True

    applied = svc.apply_kb(prop["token"])
    assert "open to the docker host" in page.read_text()

    svc.revert_kb(applied["handle"])
    assert page.read_text() == before


def test_apply_revalidates_current_content(tmp_path):
    svc, page = _setup(tmp_path)
    prop = svc.propose_kb_edit(
        path="security/test-page.md",
        old_text="Everything nominal.",
        new_text="Everything fine.",
        rationale="r",
    )
    # page changes between propose and apply
    page.write_text(page.read_text().replace("Everything nominal.", "All good."))
    with pytest.raises(KbWriteError, match="not found"):
        svc.apply_kb(prop["token"])
    # token survives the failed apply for a retry after re-checking... but the
    # content changed, so the right move is a re-propose; the old token stays
    # pending and would fail the same way.


def test_path_confinement_and_existence(tmp_path):
    svc, _ = _setup(tmp_path)
    with pytest.raises(KbWriteError, match="escapes"):
        svc.propose_kb_append(
            path="../outside.md", heading="x", entry="- y", rationale="r"
        )
    with pytest.raises(KbWriteError, match="no such kb page"):
        svc.propose_kb_append(
            path="security/new-page.md", heading="x", entry="- y", rationale="r"
        )
    with pytest.raises(KbWriteError, match=".md"):
        svc.propose_kb_append(
            path="security/test-page", heading="x", entry="- y", rationale="r"
        )


def test_heading_injection_rejected(tmp_path):
    svc, _ = _setup(tmp_path)
    with pytest.raises(KbWriteError, match="headings"):
        svc.propose_kb_append(
            path="security/test-page.md",
            heading="Access",
            entry="ok\n## Hijack",
            rationale="r",
        )
    with pytest.raises(KbWriteError, match="headings"):
        svc.propose_kb_edit(
            path="security/test-page.md",
            old_text="Everything nominal.",
            new_text="# Big Header",
            rationale="r",
        )


def test_token_single_use_and_unknown(tmp_path):
    svc, _ = _setup(tmp_path)
    prop = svc.propose_kb_append(
        path="security/test-page.md", heading="Status", entry="- fine", rationale="r"
    )
    svc.apply_kb(prop["token"])
    with pytest.raises(TokenAlreadyUsedError):
        svc.apply_kb(prop["token"])
    with pytest.raises(ProposalNotFoundError):
        svc.apply_kb("never-issued")
    with pytest.raises(ProposalNotFoundError):
        svc.revert_kb("never-issued")


def test_revert_after_hand_edit_errors(tmp_path):
    svc, page = _setup(tmp_path)
    prop = svc.propose_kb_edit(
        path="security/test-page.md",
        old_text="Everything nominal.",
        new_text="Everything fine.",
        rationale="r",
    )
    applied = svc.apply_kb(prop["token"])
    page.write_text(page.read_text().replace("Everything fine.", "Rewritten by hand."))
    with pytest.raises(ValueError, match="by hand"):
        svc.revert_kb(applied["handle"])
    assert svc.list_kb_changes()[0]["handle"] == applied["handle"]


def test_unconfigured_and_listing(tmp_path):
    svc_off = KbWriteService(None, KbWriteStore(str(tmp_path / "a.sqlite")))
    with pytest.raises(KbWriteError, match="not configured"):
        svc_off.propose_kb_append(path="x.md", heading="h", entry="- e", rationale="r")

    svc, _ = _setup(tmp_path)
    prop = svc.propose_kb_append(
        path="security/test-page.md", heading="Status", entry="- pending", rationale="r"
    )
    pending = svc.list_pending()
    assert pending[0]["kind"] == "kb" and pending[0]["change"] == "append"
    applied = svc.apply_kb(prop["token"])
    assert svc.list_pending() == []
    listed = svc.list_kb_changes()
    assert listed[0]["handle"] == applied["handle"]
    assert listed[0]["path"] == "kb/security/test-page.md"


def test_store_survives_reopen(tmp_path):
    svc, page = _setup(tmp_path)
    before = page.read_text()
    prop = svc.propose_kb_append(
        path="security/test-page.md", heading="Status", entry="- durable", rationale="r"
    )
    applied = svc.apply_kb(prop["token"])

    kb_root = str(page.parent.parent)
    svc2 = KbWriteService(kb_root, KbWriteStore(str(tmp_path / "audit.sqlite")))
    assert svc2.list_kb_changes()[0]["handle"] == applied["handle"]
    svc2.revert_kb(applied["handle"])
    assert page.read_text() == before
