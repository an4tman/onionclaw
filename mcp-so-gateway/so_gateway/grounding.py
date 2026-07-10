"""Grounding write path: the tuning gate, pointed at environment.md.

The analyst's entire model of the network lives in the skill's
``references/environment.md`` (host table, expected behavior, FP baselines,
blind spots). The agent can't be allowed to edit its own grounding silently --
a poisoned host table blinds the SOC -- so grounding updates ride the exact
same two-call approval seam as tunings:

    propose_grounding -> validate the entry + preview where it lands in each
                         grounding file + issue a SINGLE-USE token. NO WRITE.
    apply_grounding   -> consume the token, capture prior file contents,
                         append the entry under the right section heading in
                         every configured copy, record the undo. Atomic per
                         file; a partial multi-file failure rolls back.
    revert_grounding  -> remove the exact inserted block (targeted, so edits
                         made after the apply survive), mark reverted.
    list_groundings   -> applied grounding changes + their undo handles.

Same word-pair tokens, same single-use discipline, same audit DB as tuning.
The service only APPENDS entries under known section headings; it can't
rewrite or delete existing grounding, so the worst a bad approval can do is
add one visible, attributed, revertible line.

Which files get written comes from the ``GROUNDING_PATHS`` environment
variable (colon-separated in-container paths). Deployments that install the
skill into more than one runtime (Claude Code + OpenClaw) list every copy so
they can't drift.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from so_gateway import wordtoken


class GroundingError(ValueError):
    """Invalid grounding input (unknown section, malformed entry, bad file)."""


class ProposalNotFoundError(KeyError):
    """apply/revert referenced a token/handle the gateway does not know."""


class TokenAlreadyUsedError(RuntimeError):
    """A single-use grounding token was presented a second time."""


# Section key -> the lowercase prefix its "## " heading must start with.
# Prefix-matched so the parenthetical tails in the template headings
# ("## Telemetry coverage (state current coverage each cycle)") don't matter.
SECTIONS: dict[str, str] = {
    "host_table": "## host table",
    "known_noisy": "## known-noisy",
    "fp_baselines": "## documented false-positive baselines",
    "coverage": "## telemetry coverage",
}

_MAX_ENTRY_CHARS = 4000


def normalize_entry(section: str, entry: str) -> str:
    """Validate + normalize an entry for *section*. Raises GroundingError."""
    if section not in SECTIONS:
        raise GroundingError(
            f"unknown section {section!r}; one of {sorted(SECTIONS)}"
        )
    entry = entry.strip("\n").rstrip()
    if not entry.strip():
        raise GroundingError("entry is empty")
    if len(entry) > _MAX_ENTRY_CHARS:
        raise GroundingError(f"entry too long (> {_MAX_ENTRY_CHARS} chars)")
    for line in entry.splitlines():
        if line.lstrip().startswith("#"):
            # An injected heading could hijack section structure for every
            # future insert -- grounding entries are content, never structure.
            raise GroundingError("entry may not contain markdown headings")
    if section == "host_table":
        if "\n" in entry:
            raise GroundingError("a host_table entry is a single markdown table row")
        if not entry.startswith("|") or entry.count("|") < 3:
            raise GroundingError(
                'a host_table entry must be a table row like "| `<ip>` | <role> |"'
            )
    elif section in ("known_noisy", "coverage"):
        first = entry.splitlines()[0]
        if not first.lstrip().startswith(("-", "*")):
            entry = "- " + entry
    return entry


def insert_entry(content: str, section: str, entry: str) -> tuple[str, int]:
    """Append *entry* at the end of *section* in *content*.

    Returns ``(new_content, line_number)`` where line_number is the 1-based
    line the entry starts on in the new content. Raises GroundingError when
    the section heading isn't in the file.
    """
    prefix = SECTIONS[section]
    lines = content.splitlines()

    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and line.lower().startswith(prefix):
            start = i
            break
    if start is None:
        found = ", ".join(ln for ln in lines if ln.startswith("## ")) or "(none)"
        raise GroundingError(
            f"no '## ' heading starting {prefix!r} in the grounding file; "
            f"headings present: {found}"
        )

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    last = start
    for j in range(start + 1, end):
        if lines[j].strip():
            last = j

    entry_lines = entry.splitlines()
    if last == start:
        # Empty section: heading, blank line, then the entry.
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
    return text, entry_starts + 1  # 1-based


def remove_entry(content: str, entry: str) -> tuple[bool, str]:
    """Remove the first exact occurrence of *entry*'s lines from *content*.

    Targeted undo: only the inserted block is removed, so grounding edits made
    after the apply survive a revert. Returns ``(removed, new_content)``.
    """
    lines = content.splitlines()
    entry_lines = entry.splitlines()
    n = len(entry_lines)
    for i in range(len(lines) - n + 1):
        if lines[i : i + n] == entry_lines:
            del lines[i : i + n]
            # Collapse the doubled blank line a removal can leave behind.
            if 0 < i < len(lines) and not lines[i].strip() and not lines[i - 1].strip():
                del lines[i]
            text = "\n".join(lines)
            if content.endswith("\n"):
                text += "\n"
            return True, text
    return False, content


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise GroundingError(f"cannot read grounding file {path!r}: {exc}") from exc


def _atomic_write(path: str, text: str) -> None:
    """Write via tmp + rename in the same directory (safe under bind mounts)."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".grounding-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_SCHEMA = """
CREATE TABLE IF NOT EXISTS groundings (
    handle      TEXT PRIMARY KEY,
    section     TEXT NOT NULL,
    entry       TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    files       TEXT NOT NULL,   -- JSON {path: prior full content}
    status      TEXT NOT NULL,   -- 'applied' | 'reverted'
    applied_at  TEXT NOT NULL,
    reverted_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GroundingStore:
    """Audit/undo log for grounding writes. Same DB file as the tuning audit."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record_apply(
        self, *, section: str, entry: str, rationale: str, files: dict[str, str]
    ) -> str:
        existing = {
            row["handle"]
            for row in self._conn.execute("SELECT handle FROM groundings")
        }
        handle = wordtoken.new_token(taken=existing)
        self._conn.execute(
            "INSERT INTO groundings (handle, section, entry, rationale, files, "
            "status, applied_at, reverted_at) VALUES (?,?,?,?,?,?,?,?)",
            (handle, section, entry, rationale, json.dumps(files), "applied", _now(), None),
        )
        self._conn.commit()
        return handle

    def get(self, handle: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM groundings WHERE handle = ?", (handle,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def mark_reverted(self, handle: str) -> None:
        self._conn.execute(
            "UPDATE groundings SET status = 'reverted', reverted_at = ? WHERE handle = ?",
            (_now(), handle),
        )
        self._conn.commit()

    def list_applied(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM groundings WHERE status = 'applied' ORDER BY applied_at DESC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["files"] = json.loads(d["files"])
        return d


class GroundingService:
    """propose -> (human approves) -> apply -> revert, over grounding files."""

    def __init__(self, paths: list[str], store: GroundingStore) -> None:
        self._paths = list(paths)
        self._store = store
        self._pending: dict[str, dict] = {}
        self._in_flight: set[str] = set()
        self._consumed: set[str] = set()

    # -- propose -----------------------------------------------------------

    def propose_grounding(self, *, section: str, entry: str, rationale: str) -> dict:
        if not self._paths:
            raise GroundingError(
                "grounding is not configured on this gateway (GROUNDING_PATHS is empty)"
            )
        if not rationale or not rationale.strip():
            raise GroundingError("a rationale is required (what taught us this?)")
        entry = normalize_entry(section, entry)

        previews = []
        for path in self._paths:
            _, line_no = insert_entry(_read(path), section, entry)
            previews.append({"path": path, "insert_at_line": line_no})

        token = wordtoken.new_token(
            taken=self._pending.keys() | self._in_flight | self._consumed
        )
        self._pending[token] = {
            "section": section,
            "entry": entry,
            "rationale": rationale.strip(),
        }
        return {
            "token": token,
            "section": section,
            "entry": entry,
            "rationale": rationale.strip(),
            "files": previews,
            "double_gated": False,
        }

    # -- apply -------------------------------------------------------------

    def apply_grounding(self, token: str) -> dict:
        token = wordtoken.normalize(token)
        if token in self._consumed:
            raise TokenAlreadyUsedError(
                "this grounding token was already applied (tokens are single-use)"
            )
        if token in self._in_flight:
            raise TokenAlreadyUsedError(
                "this grounding token is already being applied (apply in flight)"
            )
        if token not in self._pending:
            raise ProposalNotFoundError(
                "no pending grounding proposal for this token (unknown or expired -- re-propose)"
            )
        proposal = self._pending[token]

        self._in_flight.add(token)
        priors: dict[str, str] = {}
        written: list[str] = []
        try:
            for path in self._paths:
                priors[path] = _read(path)
            for path in self._paths:
                new_text, _ = insert_entry(
                    priors[path], proposal["section"], proposal["entry"]
                )
                _atomic_write(path, new_text)
                written.append(path)
        except Exception:
            # Partial multi-file failure: put back what we already wrote so the
            # copies can't drift, and leave the token re-appliable.
            for path in written:
                try:
                    _atomic_write(path, priors[path])
                except OSError:
                    pass
            self._in_flight.discard(token)
            raise

        self._in_flight.discard(token)
        self._pending.pop(token, None)
        self._consumed.add(token)

        handle = self._store.record_apply(
            section=proposal["section"],
            entry=proposal["entry"],
            rationale=proposal["rationale"],
            files=priors,
        )
        return {
            "handle": handle,
            "status": "applied",
            "section": proposal["section"],
            "entry": proposal["entry"],
            "files": written,
        }

    # -- revert ------------------------------------------------------------

    def revert_grounding(self, handle: str) -> dict:
        handle = wordtoken.normalize(handle)
        rec = self._store.get(handle)
        if rec is None:
            raise ProposalNotFoundError(f"no grounding record for handle {handle!r}")
        if rec["status"] == "reverted":
            raise ValueError(f"grounding {handle!r} is already reverted")

        results = []
        any_removed = False
        for path in rec["files"]:
            removed, new_text = remove_entry(_read(path), rec["entry"])
            if removed:
                _atomic_write(path, new_text)
                any_removed = True
            results.append({"path": path, "removed": removed})
        if not any_removed:
            raise ValueError(
                "the inserted entry was not found in any grounding file (edited "
                "since apply?) -- remove it by hand; the record stays applied"
            )
        self._store.mark_reverted(handle)
        return {"handle": handle, "status": "reverted", "files": results}

    # -- list --------------------------------------------------------------

    def list_groundings(self) -> list[dict]:
        """Applied grounding entries + undo handles (excludes reverted)."""
        out = []
        for rec in self._store.list_applied():
            out.append(
                {
                    "handle": rec["handle"],
                    "section": rec["section"],
                    "entry": rec["entry"],
                    "rationale": rec["rationale"],
                    "applied_at": rec["applied_at"],
                    "files": list(rec["files"]),
                }
            )
        return out

    def list_pending(self) -> list[dict]:
        return [
            {
                "token": token,
                "kind": "grounding",
                "section": p["section"],
                "entry": p["entry"],
                "rationale": p["rationale"],
            }
            for token, p in self._pending.items()
        ]
