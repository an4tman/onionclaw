"""Tiny persistent 'handled' store for the approval watcher.

Since the watcher now leaves the original proposal message untouched (it posts a
NEW reply with the outcome instead of editing), it can no longer use an in-message
marker to know what it already acted on. This SQLite table is that memory: one row
per handled proposal message, so a restart never re-applies or re-announces.
"""

from __future__ import annotations

import sqlite3
import time


class HandledStore:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS handled ("
            "  message_id TEXT PRIMARY KEY,"
            "  token      TEXT,"
            "  outcome    TEXT,"
            "  handled_at REAL"
            ")"
        )
        self._conn.commit()

    def is_handled(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM handled WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark(self, message_id: str, token: str, outcome: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO handled (message_id, token, outcome, handled_at) "
            "VALUES (?,?,?,?)",
            (message_id, token, outcome, time.time()),
        )
        self._conn.commit()
