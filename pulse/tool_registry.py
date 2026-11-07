"""Dynamic tool rows (S4).

First observed C6 tool event creates a row keyed by tool name; there is
NO hardcoded tool list — a new tool appears with no plugin update.
Rows and their ids are persisted in SQLite via the existing lazy-DB
pattern in ``storage.database`` (the connection helper is imported here,
never opened at import time; it opens on first observation).

pre_tool_call path: ``live_tick()`` / ``observe_start()`` — synchronous
counter/row bump only, zero inference, zero embedding.
"""
from __future__ import annotations

import threading
import time

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from ..storage import database as _database
    from ..storage import migration as _migration
except ImportError:  # pragma: no cover
    from storage import database as _database  # type: ignore[no-redef]
    from storage import migration as _migration  # type: ignore[no-redef]

# Single source: the pulse_tools DDL lives in storage.migration (v4) so the
# isolated dashboard half (plugin-host child, plugin root off its sys.path)
# can bootstrap the same table without importing this module. SCHEMA is an
# alias — do not copy the DDL.
SCHEMA = _migration.PULSE_TOOLS_DDL

ERROR_STATUSES = frozenset({"error", "timeout", "failed", "failure"})


class ToolRegistry:
    """Dynamic per-tool rows. ``connect_fn`` defaults to the lazy
    ``storage.database.connect`` helper (honours the test override set by
    metrics.set_db_path); pass an explicit callable in tests if desired.
    """

    def __init__(self, connect_fn=None):
        self._connect_fn = connect_fn or _database.connect
        self._lock = threading.Lock()
        self._schema_ready = False

    # -- persistence ---------------------------------------------------
    def _conn(self):
        conn = self._connect_fn()
        if not self._schema_ready:
            conn.executescript(SCHEMA)
            conn.commit()
            self._schema_ready = True
        return conn

    def _execute(self, fn):
        conn = self._conn()
        try:
            result = fn(conn)
            conn.commit()
            return result
        finally:
            conn.close()

    # -- row lifecycle ---------------------------------------------------
    def get_or_create(self, tool_name, ts=None):
        """Idempotent: first observation creates the row and its id."""
        name = str(tool_name or "unknown").strip() or "unknown"
        now = float(ts) if ts is not None else time.time()

        def _fn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO pulse_tools"
                " (tool_name, first_seen, last_seen) VALUES (?, ?, ?)",
                (name, now, now))
            return conn.execute(
                "SELECT id, tool_name, first_seen, last_seen,"
                " calls_started, calls_completed, errors"
                " FROM pulse_tools WHERE tool_name = ?", (name,)).fetchone()
        with self._lock:
            row = self._execute(_fn)
        return self._row_dict(row)

    @staticmethod
    def _row_dict(row) -> dict:
        return {"id": row[0], "tool_name": row[1], "first_seen": row[2],
                "last_seen": row[3], "calls_started": row[4],
                "calls_completed": row[5], "errors": row[6]}

    def observe_start(self, tool_name, ts=None) -> dict:
        """pre_tool_call half: create row if new, bump LIVE started count."""
        name = str(tool_name or "unknown").strip() or "unknown"
        now = float(ts) if ts is not None else time.time()
        self.get_or_create(name, ts=now)

        def _fn(conn):
            conn.execute(
                "UPDATE pulse_tools SET calls_started = calls_started + 1,"
                " last_seen = ? WHERE tool_name = ?", (now, name))
            return conn.execute(
                "SELECT id, tool_name, first_seen, last_seen,"
                " calls_started, calls_completed, errors"
                " FROM pulse_tools WHERE tool_name = ?", (name,)).fetchone()
        with self._lock:
            return self._row_dict(self._execute(_fn))

    def observe_end(self, event: dict) -> dict:
        """post_tool_call half: bump completed (+ errors on error/timeout)."""
        name = str(event.get("tool_name") or "unknown").strip() or "unknown"
        now = float(event.get("ts") or time.time())
        status = str(event.get("status") or "ok").lower()
        self.get_or_create(name, ts=now)

        def _fn(conn):
            if status in ERROR_STATUSES:
                conn.execute(
                    "UPDATE pulse_tools SET calls_completed = calls_completed + 1,"
                    " errors = errors + 1, last_seen = ? WHERE tool_name = ?",
                    (now, name))
            else:
                conn.execute(
                    "UPDATE pulse_tools SET calls_completed = calls_completed + 1,"
                    " last_seen = ? WHERE tool_name = ?", (now, name))
            return conn.execute(
                "SELECT id, tool_name, first_seen, last_seen,"
                " calls_started, calls_completed, errors"
                " FROM pulse_tools WHERE tool_name = ?", (name,)).fetchone()
        with self._lock:
            return self._row_dict(self._execute(_fn))

    def observe_event(self, event: dict) -> dict:
        etype = event.get("type")
        if etype == "tool_end":
            return self.observe_end(event)
        return self.observe_start(event.get("tool_name"), ts=event.get("ts"))

    # -- reads -----------------------------------------------------------
    def get(self, tool_name) -> dict | None:
        name = str(tool_name or "").strip()
        if not name:
            return None
        def _fn(conn):
            return conn.execute(
                "SELECT id, tool_name, first_seen, last_seen,"
                " calls_started, calls_completed, errors"
                " FROM pulse_tools WHERE tool_name = ?", (name,)).fetchone()
        with self._lock:
            row = self._execute(_fn)
        return self._row_dict(row) if row else None

    def list_tools(self) -> list[dict]:
        def _fn(conn):
            return conn.execute(
                "SELECT id, tool_name, first_seen, last_seen,"
                " calls_started, calls_completed, errors"
                " FROM pulse_tools ORDER BY first_seen, id").fetchall()
        with self._lock:
            return [self._row_dict(r) for r in self._execute(_fn)]

    def tool_names(self) -> list[str]:
        return [r["tool_name"] for r in self.list_tools()]


def live_tick(registry: ToolRegistry, event: dict) -> dict:
    """Synchronous pre_tool_call entry point: LIVE row create/increment.

    No EmbeddingBackend, no encode() — see activity_encoder.
    """
    if not isinstance(event, dict):
        raise TypeError("live_tick expects a C6 tool event dict")
    return registry.observe_event(event)
