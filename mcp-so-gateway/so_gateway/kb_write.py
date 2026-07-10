"""Gated kb writes: the tuning/grounding gate, generalized to the whole wiki.

Any agent that reads the kb can propose a change to it; nothing lands without
the operator's approval. Two verbs:

    propose_kb_append -> add ONE entry under an existing heading of an existing
                         page. Same shape as grounding appends.
    propose_kb_edit   -> replace ONE exact occurrence of old_text with new_text.
                         DOUBLE-GATED (the agent workflow demands a second
                         confirm) because an edit destroys text where an append
                         only adds.

Both are read-only at propose time (validate + preview + single-use word-pair
token); apply_kb re-validates against the CURRENT file before writing, so a
page that changed between propose and apply fails loudly instead of clobbering
(re-propose). revert_kb is a targeted undo (remove the appended block / put the
old text back), so later hand-edits survive. Every applied change records the
full prior file content in the same audit DB as tunings and groundings.

The writable root comes from ``KB_WRITE_ROOT``; paths are confined to it,
must be existing ``.md`` files, and proposed content may not introduce
markdown headings (structure stays human-owned).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from so_gateway import wordtoken
from so_gateway.grounding import (
    GroundingError,
    ProposalNotFoundError,
    TokenAlreadyUsedError,
    _atomic_write,
    _read,
    remove_entry,
)

_MAX_CHARS = 6000


class KbWriteError(GroundingError):
    """Invalid kb-write input (bad path, missing heading, ambiguous match)."""


def _validate_text(label: str, text: str, allow_multiline: bool = True) -> str:
    text = text.strip("\n").rstrip()
    if not text.strip():
        raise KbWriteError(f"{label} is empty")
    if len(text) > _MAX_CHARS:
        raise KbWriteError(f"{label} too long (> {_MAX_CHARS} chars)")
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            raise KbWriteError(
                f"{label} may not contain markdown headings (structure stays human-owned)"
            )
    if not allow_multiline and "\n" in text:
        raise KbWriteError(f"{label} must be a single block")
    return text


def append_under_heading(content: str, heading: str, entry: str) -> tuple[str, int]:
    """Append *entry* at the end of the section whose heading contains *heading*.

    Matches any ``#``-level heading whose text contains *heading*
    (case-insensitive). Ambiguity (0 or >1 matches) raises KbWriteError.
    """
    needle = heading.strip().lstrip("#").strip().lower()
    if not needle:
        raise KbWriteError("heading is empty")
    lines = content.splitlines()
    matches = [
        i
        for i, ln in enumerate(lines)
        if ln.startswith("#") and needle in ln.lstrip("#").strip().lower()
    ]
    if not matches:
        found = ", ".join(ln for ln in lines if ln.startswith("#")) or "(none)"
        raise KbWriteError(
            f"no heading containing {heading!r}; headings present: {found}"
        )
    if len(matches) > 1:
        raise KbWriteError(
            f"heading {heading!r} is ambiguous ({len(matches)} matches); be more specific"
        )
    start = matches[0]
    level = len(lines[start]) - len(lines[start].lstrip("#"))

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("#"):
            jlevel = len(lines[j]) - len(lines[j].lstrip("#"))
            if jlevel <= level:
                end = j
                break

    last = start
    for j in range(start + 1, end):
        if lines[j].strip():
            last = j

    entry_lines = entry.splitlines()
    if last == start:
        insert_at = start + 1
        entry_lines = [""] + entry_lines
        entry_starts = insert_at + 1
    else:
        insert_at = last + 1
        entry_starts = insert_at
    new_lines = lines[:insert_at] + entry_lines + lines[insert_at:]
    text = "\n".join(new_lines)
    if content.endswith("\n"):
        text += "\n"
    return text, entry_starts + 1


def replace_once(content: str, old_text: str, new_text: str) -> str:
    """Replace exactly one occurrence of *old_text*; 0 or >1 raises."""
    n = content.count(old_text)
    if n == 0:
        raise KbWriteError(
            "old_text not found in the page (it may have changed; re-read and re-propose)"
        )
    if n > 1:
        raise KbWriteError(
            f"old_text matches {n} places; include more surrounding context so the match is unique"
        )
    return content.replace(old_text, new_text, 1)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_changes (
    handle      TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- 'append' | 'edit'
    payload     TEXT NOT NULL,   -- JSON {heading, entry} | {old_text, new_text}
    prior       TEXT NOT NULL,   -- full prior file content
    rationale   TEXT NOT NULL,
    status      TEXT NOT NULL,   -- 'applied' | 'reverted'
    applied_at  TEXT NOT NULL,
    reverted_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KbWriteStore:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record_apply(
        self, *, path: str, kind: str, payload: dict, prior: str, rationale: str
    ) -> str:
        existing = {
            row["handle"]
            for row in self._conn.execute("SELECT handle FROM kb_changes")
        }
        handle = wordtoken.new_token(taken=existing)
        self._conn.execute(
            "INSERT INTO kb_changes (handle, path, kind, payload, prior, rationale, "
            "status, applied_at, reverted_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (handle, path, kind, json.dumps(payload), prior, rationale, "applied", _now(), None),
        )
        self._conn.commit()
        return handle

    def get(self, handle: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM kb_changes WHERE handle = ?", (handle,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def mark_reverted(self, handle: str) -> None:
        self._conn.execute(
            "UPDATE kb_changes SET status = 'reverted', reverted_at = ? WHERE handle = ?",
            (_now(), handle),
        )
        self._conn.commit()

    def list_applied(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM kb_changes WHERE status = 'applied' ORDER BY applied_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out


class KbWriteService:
    """propose -> (operator approves; edits confirm twice) -> apply -> revert."""

    def __init__(self, root: str | None, store: KbWriteStore) -> None:
        self._root = os.path.realpath(root) if root else None
        self._store = store
        self._pending: dict[str, dict] = {}
        self._in_flight: set[str] = set()
        self._consumed: set[str] = set()

    # -- path handling -------------------------------------------------------

    def _resolve(self, path: str) -> tuple[str, str]:
        """Return (abs_path, kb-relative path) for an existing .md page."""
        if not self._root:
            raise KbWriteError(
                "kb writes are not configured on this gateway (KB_WRITE_ROOT is empty)"
            )
        rel = path.strip().lstrip("/")
        if rel.startswith("kb/"):
            rel = rel[len("kb/"):]
        abs_path = os.path.realpath(os.path.join(self._root, rel))
        if not (abs_path == self._root or abs_path.startswith(self._root + os.sep)):
            raise KbWriteError(f"path escapes the kb root: {path!r}")
        if not abs_path.endswith(".md"):
            raise KbWriteError("only .md pages are writable")
        if not os.path.isfile(abs_path):
            raise KbWriteError(
                f"no such kb page: {path!r} (kb writes target existing pages only)"
            )
        return abs_path, rel

    # -- propose -------------------------------------------------------------

    def _issue(self, pending: dict) -> str:
        token = wordtoken.new_token(
            taken=self._pending.keys() | self._in_flight | self._consumed
        )
        self._pending[token] = pending
        return token

    def propose_kb_append(
        self, *, path: str, heading: str, entry: str, rationale: str
    ) -> dict:
        if not rationale or not rationale.strip():
            raise KbWriteError("a rationale is required")
        abs_path, rel = self._resolve(path)
        entry = _validate_text("entry", entry)
        _, line_no = append_under_heading(_read(abs_path), heading, entry)
        token = self._issue(
            {
                "kind": "append",
                "path": rel,
                "abs_path": abs_path,
                "heading": heading,
                "entry": entry,
                "rationale": rationale.strip(),
            }
        )
        return {
            "token": token,
            "kind": "append",
            "path": f"kb/{rel}",
            "heading": heading,
            "entry": entry,
            "insert_at_line": line_no,
            "double_gated": False,
            "rationale": rationale.strip(),
        }

    def propose_kb_edit(
        self, *, path: str, old_text: str, new_text: str, rationale: str
    ) -> dict:
        if not rationale or not rationale.strip():
            raise KbWriteError("a rationale is required")
        abs_path, rel = self._resolve(path)
        old_text = old_text.strip("\n").rstrip()
        if not old_text.strip():
            raise KbWriteError("old_text is empty")
        new_text = _validate_text("new_text", new_text)
        replace_once(_read(abs_path), old_text, new_text)  # validates uniqueness
        token = self._issue(
            {
                "kind": "edit",
                "path": rel,
                "abs_path": abs_path,
                "old_text": old_text,
                "new_text": new_text,
                "rationale": rationale.strip(),
            }
        )
        return {
            "token": token,
            "kind": "edit",
            "path": f"kb/{rel}",
            "old_text": old_text,
            "new_text": new_text,
            "double_gated": True,
            "rationale": rationale.strip(),
        }

    # -- apply ----------------------------------------------------------------

    def apply_kb(self, token: str) -> dict:
        token = wordtoken.normalize(token)
        if token in self._consumed:
            raise TokenAlreadyUsedError(
                "this kb token was already applied (tokens are single-use)"
            )
        if token in self._in_flight:
            raise TokenAlreadyUsedError("this kb token is already being applied")
        if token not in self._pending:
            raise ProposalNotFoundError(
                "no pending kb proposal for this token (unknown or expired -- re-propose)"
            )
        p = self._pending[token]

        self._in_flight.add(token)
        try:
            prior = _read(p["abs_path"])
            if p["kind"] == "append":
                new_text, _ = append_under_heading(prior, p["heading"], p["entry"])
                payload = {"heading": p["heading"], "entry": p["entry"]}
            else:
                new_text = replace_once(prior, p["old_text"], p["new_text"])
                payload = {"old_text": p["old_text"], "new_text": p["new_text"]}
            _atomic_write(p["abs_path"], new_text)
        except Exception:
            self._in_flight.discard(token)
            raise

        self._in_flight.discard(token)
        self._pending.pop(token, None)
        self._consumed.add(token)

        handle = self._store.record_apply(
            path=p["path"],
            kind=p["kind"],
            payload=payload,
            prior=prior,
            rationale=p["rationale"],
        )
        return {
            "handle": handle,
            "status": "applied",
            "kind": p["kind"],
            "path": f"kb/{p['path']}",
        }

    # -- revert ----------------------------------------------------------------

    def revert_kb(self, handle: str) -> dict:
        handle = wordtoken.normalize(handle)
        rec = self._store.get(handle)
        if rec is None:
            raise ProposalNotFoundError(f"no kb change record for handle {handle!r}")
        if rec["status"] == "reverted":
            raise ValueError(f"kb change {handle!r} is already reverted")

        abs_path, _ = self._resolve(rec["path"])
        content = _read(abs_path)
        if rec["kind"] == "append":
            removed, new_text = remove_entry(content, rec["payload"]["entry"])
            if not removed:
                raise ValueError(
                    "the appended entry was not found (edited since apply?) -- "
                    "remove it by hand; the record stays applied"
                )
        else:
            new = rec["payload"]["new_text"]
            if content.count(new) != 1:
                raise ValueError(
                    "the edited text was not found exactly once (changed since "
                    "apply?) -- restore it by hand; the record stays applied"
                )
            new_text = content.replace(new, rec["payload"]["old_text"], 1)
        _atomic_write(abs_path, new_text)
        self._store.mark_reverted(handle)
        return {"handle": handle, "status": "reverted", "path": f"kb/{rec['path']}"}

    # -- list ------------------------------------------------------------------

    def list_kb_changes(self) -> list[dict]:
        return [
            {
                "handle": r["handle"],
                "kind": r["kind"],
                "path": f"kb/{r['path']}",
                "payload": r["payload"],
                "rationale": r["rationale"],
                "applied_at": r["applied_at"],
            }
            for r in self._store.list_applied()
        ]

    def list_pending(self) -> list[dict]:
        out = []
        for token, p in self._pending.items():
            row = {
                "token": token,
                "kind": "kb",
                "change": p["kind"],
                "path": f"kb/{p['path']}",
                "rationale": p["rationale"],
            }
            if p["kind"] == "append":
                row["entry"] = p["entry"]
            else:
                row["old_text"] = p["old_text"]
                row["new_text"] = p["new_text"]
            out.append(row)
        return out
