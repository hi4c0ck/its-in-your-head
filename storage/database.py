"""Lazy-DB facade over plugins.plugin_storage.plugin_db.

The connection opens on first use (first persisted event), NEVER at
register()/import time — doctor runs register() under a temp HERMES_HOME
and deletes it (WinError 145 on Windows). The C3 DDL source of truth
stays metrics.SCHEMA; this module owns only the connection lifecycle.
"""
import sqlite3

PLUGIN_ID = "its-in-your-head"

_db_path_override = None  # test-only, see set_override()


def set_override(path) -> None:
    """TEST ONLY: point storage at a local file instead of plugin_db()."""
    global _db_path_override
    _db_path_override = path


def connect() -> sqlite3.Connection:
    """Open a fresh WAL connection. Callers close it (check_same_thread
    False handles arrive from plugin_db; our own connects are closed by
    the caller too)."""
    if _db_path_override is not None:
        conn = sqlite3.connect(str(_db_path_override))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_ID)
