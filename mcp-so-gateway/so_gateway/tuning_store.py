"""SQLite-backed audit + undo store for applied SO tunings.

HARD SAFETY (spec §6): every applied write is logged here with (a) what changed,
(b) when, (c) the exact prior detection state -- so ``revert_tuning`` is a
faithful replay and there is a tamper-evident trail of every change the gateway
made to SO. The DB lives on a mounted volume in the container so it survives a
recreate; ``*.sqlite`` is gitignored, so the live DB is never committed (a
diffable export would be an explicit, separate action per the spec).

The store does NOT talk to SO. It only persists records; the server orchestrates
the SO write + the store record together.
"""

import json
import sqlite3
from datetime import datetime, timezone

from so_gateway import wordtoken

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tunings (
    handle             TEXT PRIMARY KEY,
    public_id          TEXT NOT NULL,
    detection_id       TEXT NOT NULL,
    override_type      TEXT NOT NULL,
    applied_override   TEXT NOT NULL,   -- JSON
    prior_state        TEXT NOT NULL,   -- JSON {isEnabled, overrides}
    rationale          TEXT NOT NULL,
    review_horizon_days INTEGER,
    status             TEXT NOT NULL,   -- 'applied' | 'reverted'
    applied_at         TEXT NOT NULL,
    reverted_at        TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TuningStore:
    """Persistent audit/undo log. One row per applied tuning."""

    def __init__(self, path: str) -> None:
        # check_same_thread=False: FastMCP may dispatch tools on a worker thread.
        # Writes are tiny and serialized by SQLite's own lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record_apply(
        self,
        *,
        public_id: str,
        detection_id: str,
        override_type: str,
        applied_override: dict,
        prior_state: dict,
        rationale: str,
        review_horizon_days: int | None,
    ) -> str:
        """Persist an applied tuning and return its undo *handle*."""
        # Word-pair handle ('lucid-heron'): typed back by a human as
        # `revert <handle>`, so it gets the same friendliness as tokens.
        # Unique against every handle ever issued by this store.
        existing = {
            row["handle"]
            for row in self._conn.execute("SELECT handle FROM tunings")
        }
        handle = wordtoken.new_token(taken=existing)
        self._conn.execute(
            "INSERT INTO tunings (handle, public_id, detection_id, override_type, "
            "applied_override, prior_state, rationale, review_horizon_days, status, "
            "applied_at, reverted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                handle,
                public_id,
                detection_id,
                override_type,
                json.dumps(applied_override),
                json.dumps(prior_state),
                rationale,
                review_horizon_days,
                "applied",
                _now(),
                None,
            ),
        )
        self._conn.commit()
        return handle

    def get(self, handle: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tunings WHERE handle = ?", (handle,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def mark_reverted(self, handle: str) -> None:
        self._conn.execute(
            "UPDATE tunings SET status = 'reverted', reverted_at = ? WHERE handle = ?",
            (_now(), handle),
        )
        self._conn.commit()

    def list_applied(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tunings WHERE status = 'applied' ORDER BY applied_at DESC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tunings ORDER BY applied_at DESC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["applied_override"] = json.loads(d["applied_override"])
        d["prior_state"] = json.loads(d["prior_state"])
        return d
