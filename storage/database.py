"""Single DB path resolver + lazy connection facade.

The connection opens on first use (first persisted event / first route
request), NEVER at register()/import time — doctor runs register() under a
temp HERMES_HOME and deletes it (WinError 145 on Windows). The DDL source
of truth is storage.migration.EVENTS_DDL; this module owns only path
resolution and the connection lifecycle. metrics.py and
dashboard/plugin_api.py both resolve/connect through here (P2 unification:
no per-module override state).
"""
import sqlite3
from pathlib import Path

PLUGIN_ID = "its-in-your-head"

_db_path_override = None  # test-only, see set_override()


def set_override(path) -> None:
    """TEST ONLY: point storage at a local file instead of plugin_db()."""
    global _db_path_override
    _db_path_override = path


def resolve_path():
    """The file storage.connect() will open.

    The test override when set, else the plugin_db() location
    (<hermes home>/plugin-data/its-in-your-head/data.db). Resolving the
    non-override location goes through plugin_data_dir (which creates the
    plugin-data dir, exactly as plugin_db() itself does on connect), so
    only call this at connect/request time, never at register time.
    """
    if _db_path_override is not None:
        return Path(_db_path_override)
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_ID) / "data.db"


def connect() -> sqlite3.Connection:
    """Open a fresh WAL connection. Callers close it (check_same_thread
    False handles arrive from plugin_db; our own connects are closed by
    the caller too)."""
    if _db_path_override is not None:
        conn = sqlite3.connect(str(_db_path_override))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Bound WAL writer/checkpoint contention instead of failing
        # fast (the metrics worker writes the same file concurrently).
        conn.execute("PRAGMA busy_timeout=5000")
        # §8: physically overwrite deleted pages so an ordinary DELETE does
        # not leave the raw bytes recoverable from the free list.
        conn.execute("PRAGMA secure_delete=ON")
        return conn
    from plugins.plugin_storage import plugin_db
    conn = plugin_db(PLUGIN_ID)
    # §8: honor secure_delete on the host-managed connection too (best
    # effort — the host may open the file read-only).
    # busy_timeout bounds WAL writer/checkpoint contention (the
    # metrics worker persists into the same file concurrently).
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA secure_delete=ON")
    except Exception:
        pass
    return conn
