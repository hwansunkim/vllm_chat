"""Per-thread SQLite connection management.

Each thread gets its own connection (sqlite3 connections are not safe to share
across threads by default). Stored on `self._local`, a threading.local owned by
the SimDB instance, so multiple SimDB instances stay isolated.
"""

import sqlite3
import threading


class ConnMixin:
    """Provides `self._conn()` returning a thread-local sqlite3.Connection."""

    _db_path: str
    _local: threading.local

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn
