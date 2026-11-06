"""Schema migration / bootstrap — single source of truth for the events DDL.

v0.1.1 (P0): pre-S1 databases have an ``events`` table WITHOUT the
``tool_name`` column. Bootstrapping those with the current DDL used to fail
on ``CREATE INDEX idx_events_tool ON events (tool_name, ts)`` and silently
drop every worker batch. :func:`migrate` upgrades such databases in place —
preserving existing rows — inside a single transaction.

Versioning: ``meta(key TEXT PRIMARY KEY, value TEXT)`` with
``schema_version`` = ``3`` for current-shape databases (fresh or migrated).
A database already at the expected version is a fast-path no-op.

Schema v3 (F2 drift durability): adds the ``portrait_history`` table —
one row per engine portrait-history entry (``ts`` + one REAL column per
portrait dimension, column names exactly the six
``perception.anchors.DIMENSIONS``, matching the dashboard ``/drift``
fallback probe in ``dashboard/plugin_api._drift_shape``). Retention is
bounded at ``PORTRAIT_HISTORY_MAX`` rows (mirrors the engine's
``DRIFT_MAX`` in-memory deque bound), pruned on write to the most
recent rows. v2 databases gain the table empty (existing events rows
preserved); ``CREATE TABLE IF NOT EXISTS`` + missing-column ``ALTER``
keep the upgrade idempotent.

Import rules: this module is importable from both the worker (``metrics``)
and the dashboard mount without importing either of them, and it performs
no I/O at import time.
"""
from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 3
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION  # alias

_CREATE_EVENTS_TABLE = """CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY,
    ts                REAL NOT NULL,
    type              TEXT NOT NULL,
    session_id        TEXT,
    turn_id           TEXT,
    api_request_id    TEXT,
    model             TEXT,
    provider          TEXT,
    api_duration      REAL,
    finish_reason     TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    status_code       INTEGER,
    retry_count       INTEGER,
    retryable         INTEGER,
    error_type        TEXT,
    error_message     TEXT,
    user_chars        INTEGER,
    user_message      TEXT,
    sketch            BLOB,
    tool_name         TEXT,
    task_id           TEXT,
    tool_call_id      TEXT,
    duration_ms       REAL,
    result_type       TEXT,
    result_chars      INTEGER,
    status            TEXT,
    finished          INTEGER,
    reasoning_chars   INTEGER,
    reasoning_deltas  INTEGER,
    final_chars       INTEGER
)"""

_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events (type)",
    "CREATE INDEX IF NOT EXISTS idx_events_tool ON events (tool_name, ts)",
)

# Canonical events DDL (table + indexes). metrics.SCHEMA and
# dashboard.plugin_api._DDL are aliases of this string — do not copy it.
EVENTS_DDL = (
    _CREATE_EVENTS_TABLE + ";\n"
    + ";\n".join(_INDEX_STATEMENTS) + ";\n"
)
SCHEMA = EVENTS_DDL

# Pinned portrait dimension column names (F2). Must stay in sync with
# perception.anchors.DIMENSIONS and dashboard.plugin_api._dimensions();
# a local copy is deliberate: this module must not import perception.
PORTRAIT_HISTORY_DIMS = (
    "directness",
    "context_richness",
    "exploration",
    "analytical_depth",
    "iteration",
    "scope",
)

# Retention bound for portrait_history, mirroring the engine's
# DRIFT_MAX in-memory deque bound; enforced by a delete-oldest prune
# on every engine write (see engine._persist_history_entry).
PORTRAIT_HISTORY_MAX = 512

# One row per engine portrait-history entry: ts + one REAL per pinned
# dimension (NULL = dimension had no value for that entry). Column names
# and the ``ts`` ordering column are exactly what the /drift fallback
# probe (dashboard.plugin_api._drift_shape) reads: present = dims ∩
# columns, ordered by ts, values collected per dimension.
_CREATE_PORTRAIT_HISTORY_TABLE = """CREATE TABLE IF NOT EXISTS portrait_history (
    id                INTEGER PRIMARY KEY,
    ts                REAL NOT NULL,
    directness        REAL,
    context_richness  REAL,
    exploration       REAL,
    analytical_depth  REAL,
    iteration         REAL,
    scope             REAL
)"""

_CREATE_PORTRAIT_HISTORY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_portrait_history_ts "
    "ON portrait_history (ts)"
)

PORTRAIT_HISTORY_DDL = (
    _CREATE_PORTRAIT_HISTORY_TABLE + ";\n"
    + _CREATE_PORTRAIT_HISTORY_INDEX + ";\n"
)

_META_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
)

# Column name -> SQLite type, in canonical order, for ALTER TABLE upgrades
# of legacy tables (e.g. pre-S1 rows missing ``tool_name``). ``id`` is
# excluded: it cannot be added to an existing table and a legacy table
# without it is outside the known upgrade shapes.
_COLUMN_TYPES = {
    "ts": "REAL",
    "type": "TEXT",
    "session_id": "TEXT",
    "turn_id": "TEXT",
    "api_request_id": "TEXT",
    "model": "TEXT",
    "provider": "TEXT",
    "api_duration": "REAL",
    "finish_reason": "TEXT",
    "prompt_tokens": "INTEGER",
    "completion_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "status_code": "INTEGER",
    "retry_count": "INTEGER",
    "retryable": "INTEGER",
    "error_type": "TEXT",
    "error_message": "TEXT",
    "user_chars": "INTEGER",
    "user_message": "TEXT",
    "sketch": "BLOB",
    "tool_name": "TEXT",
    "task_id": "TEXT",
    "tool_call_id": "TEXT",
    "duration_ms": "REAL",
    "result_type": "TEXT",
    "result_chars": "INTEGER",
    "status": "TEXT",
    "finished": "INTEGER",
    "reasoning_chars": "INTEGER",
    "reasoning_deltas": "INTEGER",
    "final_chars": "INTEGER",
}


def _table_columns(conn: sqlite3.Connection, table: str) -> list:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _read_version(conn: sqlite3.Connection):
    """Recorded schema_version, or None (no meta table / no row)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        return None
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def get_schema_version(conn: sqlite3.Connection):
    """Public read of the recorded schema_version (None if unrecorded)."""
    return _read_version(conn)


def _add_missing_columns(conn: sqlite3.Connection, existing: list) -> None:
    for name, col_type in _COLUMN_TYPES.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE events ADD COLUMN {name} {col_type}")


def _create_indexes(conn: sqlite3.Connection) -> None:
    for stmt in _INDEX_STATEMENTS:
        conn.execute(stmt)


def _create_portrait_history(conn: sqlite3.Connection) -> None:
    """Create ``portrait_history`` if absent; add any missing dim columns.

    ``CREATE TABLE IF NOT EXISTS`` covers the fresh/v2-upgrade cases; a
    pre-existing table missing dim columns (partial hand-made table)
    gets them via ``ALTER TABLE`` (existing rows preserved, new NULL).
    """
    conn.execute(_CREATE_PORTRAIT_HISTORY_TABLE)
    existing = _table_columns(conn, "portrait_history")
    for dim in PORTRAIT_HISTORY_DIMS:
        if dim not in existing:
            conn.execute(
                f"ALTER TABLE portrait_history ADD COLUMN {dim} REAL")
    conn.execute(_CREATE_PORTRAIT_HISTORY_INDEX)


def migrate(conn: sqlite3.Connection,
            expected_version: int = CURRENT_SCHEMA_VERSION) -> bool:
    """Upgrade ``conn``'s database to ``expected_version`` in place.

    Idempotent: a database already at (or above) the expected version is a
    no-op (returns False). Fresh databases get the canonical DDL; legacy
    databases (e.g. missing ``tool_name``) get the missing columns added via
    ``ALTER TABLE`` (existing rows preserved, new columns NULL) *before* the
    indexes that reference them are created. v2 -> v3 likewise gains the
    ``portrait_history`` table (empty; existing events rows preserved).
    All steps run in a single transaction when the caller has not already
    opened one; any failure rolls the whole migration back (no partial
    column/index/version state). Returns True if work was done.
    """
    version = _read_version(conn)
    if version is not None and version >= expected_version:
        return False

    owns_txn = not conn.in_transaction
    prev_isolation = conn.isolation_level
    if owns_txn:
        # Autocommit mode so BEGIN/COMMIT/ROLLBACK below are explicit and
        # executescript-style implicit commits cannot split the migration.
        conn.isolation_level = None
    try:
        if owns_txn:
            conn.execute("BEGIN")
        existing = _table_columns(conn, "events")
        if not existing:
            conn.execute(_CREATE_EVENTS_TABLE)
        else:
            _add_missing_columns(conn, existing)
        _create_indexes(conn)
        _create_portrait_history(conn)
        conn.execute(_META_TABLE_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(expected_version)))
        if owns_txn:
            conn.execute("COMMIT")
    except BaseException:
        if owns_txn and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if owns_txn:
            conn.isolation_level = prev_isolation
    return True


def schema_bootstrap(conn: sqlite3.Connection, ddl=None) -> bool:
    """Shared bootstrap entry point for metrics.py and dashboard/plugin_api.py.

    ``ddl`` is accepted for call-site symmetry with the old per-module
    ``executescript(SCHEMA)`` pattern; when it is the canonical DDL (the
    normal case) the full idempotent :func:`migrate` runs instead, so a
    legacy database is upgraded rather than failing on the tool index.
    A custom ``ddl`` is executed first, then migrate() still runs to add
    any missing columns/indexes and record the version.
    """
    if ddl is not None and ddl != EVENTS_DDL:
        conn.executescript(ddl)
    return migrate(conn)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "EVENTS_DDL",
    "SCHEMA",
    "PORTRAIT_HISTORY_DIMS",
    "PORTRAIT_HISTORY_MAX",
    "PORTRAIT_HISTORY_DDL",
    "get_schema_version",
    "migrate",
    "schema_bootstrap",
]
