"""SQLite-backed TI lookup cache, keyed (ioc, provider), TTL'd.

Mirrors the so-agent SQLite-cache idea (kb/security/threat-intel-enrichment).
Lives on the mounted /data volume so it survives a container recreate. Checked
before any external provider call; short-circuits repeat lookups across runs and
is the primary defense against hammering rate-limited providers (esp. VirusTotal
free tier @ 4 req/min).

The store does NOT make network calls. It only persists normalized provider
records (the {verdict, score, categories, evidence} dicts the providers return).
"""

import json
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ti_cache (
    ioc        TEXT NOT NULL,
    provider   TEXT NOT NULL,
    record     TEXT NOT NULL,   -- JSON normalized provider record
    fetched_at REAL NOT NULL,   -- epoch seconds
    expires_at REAL NOT NULL,   -- epoch seconds
    PRIMARY KEY (ioc, provider)
);
"""


class TiCache:
    """Persistent (ioc, provider) -> normalized-record cache with TTL."""

    def __init__(self, path: str) -> None:
        # check_same_thread=False: FastMCP may dispatch tools on a worker thread.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, ioc: str, provider: str) -> dict | None:
        """Return the cached record for (ioc, provider) if present and unexpired."""
        row = self._conn.execute(
            "SELECT record, fetched_at, expires_at FROM ti_cache WHERE ioc = ? AND provider = ?",
            (ioc, provider),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < time.time():
            return None
        rec = json.loads(row["record"])
        rec["cached"] = True
        rec["cache_age_s"] = round(time.time() - row["fetched_at"], 1)
        return rec

    def put(self, ioc: str, provider: str, record: dict, ttl_s: int) -> None:
        """Cache a normalized record for (ioc, provider) for *ttl_s* seconds."""
        now = time.time()
        # Don't persist the cache-bookkeeping keys.
        clean = {k: v for k, v in record.items() if k not in ("cached", "cache_age_s")}
        self._conn.execute(
            "INSERT OR REPLACE INTO ti_cache (ioc, provider, record, fetched_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (ioc, provider, json.dumps(clean), now, now + ttl_s),
        )
        self._conn.commit()

    def stats(self) -> dict:
        """Return simple cache stats (total rows, live rows)."""
        total = self._conn.execute("SELECT COUNT(*) FROM ti_cache").fetchone()[0]
        live = self._conn.execute(
            "SELECT COUNT(*) FROM ti_cache WHERE expires_at >= ?", (time.time(),)
        ).fetchone()[0]
        return {"total_rows": total, "live_rows": live}
