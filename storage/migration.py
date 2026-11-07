"""Schema migration / bootstrap — single source of truth for the events DDL.

v0.1.1 (P0): pre-S1 databases have an ``events`` table WITHOUT the
``tool_name`` column. Bootstrapping those with the current DDL used to fail
on ``CREATE INDEX idx_events_tool ON events (tool_name, ts)`` and silently
drop every worker batch. :func:`migrate` upgrades such databases in place —
preserving existing rows — inside a single transaction.

Versioning: ``meta(key TEXT PRIMARY KEY, value TEXT)`` with
``schema_version`` = ``6`` for current-shape databases (fresh or migrated).
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

Schema v4 (PULSE cross-process bridge): adds the pulse-domain tables —
``pulse_tools`` (LIVE rows; its DDL moves from pulse.tool_registry's
lazy script to this single source so the isolated dashboard half can
bootstrap it without importing the pulse package), ``pulse_prototypes``
(bounded GOOD / FRICTION / objective-friction prototype vectors),
``pulse_goal`` (the derived goal vector — the embedding of the last user
prompt; the raw prompt is NEVER written), and ``pulse_capsules`` (recent
per-tool capsule vectors). All vector columns are little-endian float32
BLOBs (the ``events.sketch`` convention); only derived vectors are
stored. v3 databases gain the tables empty (existing rows preserved).

Schema v5 (PORTRAIT perception, D4 Option B — derived persistence): adds
``pattern_affinity_history`` — one row per interaction carrying only the
derived strongest-pattern numbers (affinities, margin, amplification,
evidence, confidence, repeated flag). It holds NO raw user or reasoning
content: ``top_pattern`` is a PATTERN NAME from the preset bank, never
free text. v4 databases gain the table empty (existing rows preserved).

Schema v6 (PORTRAIT perception bank upscaling, D3 route A): adds
``pattern_bank`` — one row per reinforced pattern name carrying the
DERIVED prompt/agent anchor vectors (little-endian float32 BLOBs, the
``events.sketch`` convention), an evidence count, and ``updated_at``.
It holds NO raw content: ``pattern_name`` is a bank name (preset or
custom), never free text, and no raw-content column exists. v5
databases gain the table empty (existing rows preserved).

Import rules: this module is importable from both the worker (``metrics``)
and the dashboard mount without importing either of them, and it performs
no I/O at import time.
"""
from __future__ import annotations

import logging
import sqlite3

CURRENT_SCHEMA_VERSION = 6
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION  # alias

_logger = logging.getLogger(__name__)

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

# --- PULSE cross-process bridge (v4) -------------------------------------
# LIVE tool rows: moved here from pulse.tool_registry's lazy DDL so the
# isolated dashboard half (plugin-host child, plugin root off its
# sys.path) can bootstrap the same table via schema_bootstrap() without
# importing the pulse package. pulse.tool_registry.SCHEMA is an alias of
# PULSE_TOOLS_DDL — do not copy the DDL.
_CREATE_PULSE_TOOLS_TABLE = """CREATE TABLE IF NOT EXISTS pulse_tools (
    id               INTEGER PRIMARY KEY,
    tool_name        TEXT UNIQUE NOT NULL,
    first_seen       REAL NOT NULL,
    last_seen        REAL NOT NULL,
    calls_started    INTEGER NOT NULL DEFAULT 0,
    calls_completed  INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0
)"""

_CREATE_PULSE_TOOLS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_pulse_tools_name "
    "ON pulse_tools (tool_name)"
)

PULSE_TOOLS_DDL = (
    _CREATE_PULSE_TOOLS_TABLE + ";\n"
    + _CREATE_PULSE_TOOLS_INDEX + ";\n"
)

# Bounded prototype banks (FIFO-capped per tool+bank by the writer).
# `vector` is a derived embedding (float32 BLOB) — never raw text.
_CREATE_PULSE_PROTOTYPES_TABLE = """CREATE TABLE IF NOT EXISTS pulse_prototypes (
    id           INTEGER PRIMARY KEY,
    tool_name    TEXT NOT NULL,
    bank         TEXT NOT NULL,
    ts           REAL NOT NULL,
    vector       BLOB NOT NULL
)"""

_CREATE_PULSE_PROTOTYPES_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_pulse_prototypes_tool "
    "ON pulse_prototypes (tool_name, bank)"
)

PULSE_PROTOTYPES_DDL = (
    _CREATE_PULSE_PROTOTYPES_TABLE + ";\n"
    + _CREATE_PULSE_PROTOTYPES_INDEX + ";\n"
)

# Singleton derived goal vector (id = 1): the embedding of the last user
# prompt. The raw prompt is never written (v0.1.3 privacy ruling).
_CREATE_PULSE_GOAL_TABLE = """CREATE TABLE IF NOT EXISTS pulse_goal (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    ts       REAL NOT NULL,
    vector   BLOB NOT NULL,
    backend  TEXT
)"""

PULSE_GOAL_DDL = _CREATE_PULSE_GOAL_TABLE + ";\n"

# Recent per-tool activity-capsule vectors (the GOAL column's fallback
# reference when a tool's banks are still empty), pruned to the newest
# per-tool bound by the writer.
_CREATE_PULSE_CAPSULES_TABLE = """CREATE TABLE IF NOT EXISTS pulse_capsules (
    id           INTEGER PRIMARY KEY,
    tool_name    TEXT NOT NULL,
    ts           REAL NOT NULL,
    vector       BLOB NOT NULL
)"""

_CREATE_PULSE_CAPSULES_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_pulse_capsules_tool "
    "ON pulse_capsules (tool_name, ts)"
)

PULSE_CAPSULES_DDL = (
    _CREATE_PULSE_CAPSULES_TABLE + ";\n"
    + _CREATE_PULSE_CAPSULES_INDEX + ";\n"
)

# The three derived-vector tables, for the pulse writer's lazy
# executescript bootstrap (single source; do not copy the DDL).
PULSE_STATE_DDL = (
    PULSE_PROTOTYPES_DDL + PULSE_GOAL_DDL + PULSE_CAPSULES_DDL
)

# --- PORTRAIT perception derived persistence (v5, D4 Option B) -----------
# Retention bound mirroring PORTRAIT_HISTORY_MAX (engine DRIFT_MAX deque).
PATTERN_AFFINITY_HISTORY_MAX = 512

# One row per interaction: the DERIVED strongest-pattern numbers only.
# INVARIANT (privacy): this table must have NO column that could hold raw
# user or reasoning content. ``top_pattern`` is a PATTERN NAME drawn from
# the perception preset bank (e.g. "focused"), never free text, never a
# prompt fragment. All other columns are ints/floats/flags derived from
# cosine scoring; raw text is discarded after scoring, never persisted.
_CREATE_PATTERN_AFFINITY_HISTORY_TABLE = """CREATE TABLE IF NOT EXISTS pattern_affinity_history (
    id                    INTEGER PRIMARY KEY,
    ts                    REAL NOT NULL,
    top_pattern           TEXT,
    top_prompt_affinity   REAL,
    top_agent_affinity    REAL,
    top_margin            REAL,
    top_amplification     REAL,
    evidence              INTEGER NOT NULL DEFAULT 0,
    confidence            REAL,
    repeated              INTEGER NOT NULL DEFAULT 0
)"""

_CREATE_PATTERN_AFFINITY_HISTORY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_pattern_affinity_history_ts "
    "ON pattern_affinity_history (ts)"
)

PATTERN_AFFINITY_HISTORY_DDL = (
    _CREATE_PATTERN_AFFINITY_HISTORY_TABLE + ";\n"
    + _CREATE_PATTERN_AFFINITY_HISTORY_INDEX + ";\n"
)

# --- Reinforced pattern bank (v6, D3 route A) ------------------------------
# Derived vectors only (float32 struct-packed BLOBs); ``pattern_name`` is
# a bank name (preset or custom), never free text; no raw content column
# exists. Upserted per pattern by perception.patterns.persist_reinforced.
_CREATE_PATTERN_BANK_TABLE = """CREATE TABLE IF NOT EXISTS pattern_bank (
    pattern_name TEXT PRIMARY KEY,
    prompt_anchor BLOB NOT NULL,
    agent_anchor BLOB NOT NULL,
    evidence INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
)"""

PATTERN_BANK_DDL = _CREATE_PATTERN_BANK_TABLE + ";\n"


def prune_pattern_affinity_history(conn: sqlite3.Connection) -> None:
    """Prune ``pattern_affinity_history`` to ``PATTERN_AFFINITY_HISTORY_MAX``.

    Keeps the most recent rows by ``id`` — mirroring the engine's
    portrait_history prune. Log-and-never-raise (same discipline as pulse
    persistence): a prune failure must never break a write or a route.
    """
    try:
        conn.execute(
            "DELETE FROM pattern_affinity_history WHERE id NOT IN "
            "(SELECT id FROM pattern_affinity_history "
            "ORDER BY id DESC LIMIT ?)",
            (PATTERN_AFFINITY_HISTORY_MAX,))
    except Exception:
        _logger.exception(
            "its-in-your-head: pattern_affinity_history prune failed")

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


def _create_pattern_affinity_history(conn: sqlite3.Connection) -> None:
    """Create ``pattern_affinity_history`` if absent (v5; IF NOT EXISTS,
    so a pre-existing table is a no-op and existing rows are preserved).
    Individual ``execute`` calls — no ``executescript`` — so the calls
    stay inside the caller's transaction (migrate() owns BEGIN/COMMIT).
    """
    conn.execute(_CREATE_PATTERN_AFFINITY_HISTORY_TABLE)
    conn.execute(_CREATE_PATTERN_AFFINITY_HISTORY_INDEX)


def _create_pattern_bank(conn: sqlite3.Connection) -> None:
    """Create ``pattern_bank`` if absent (v6; IF NOT EXISTS, so a
    pre-existing table is a no-op and existing rows are preserved).
    Individual ``execute`` calls — no ``executescript`` — so the calls
    stay inside the caller's transaction (migrate() owns BEGIN/COMMIT).
    """
    conn.execute(_CREATE_PATTERN_BANK_TABLE)


def _create_pulse_tables(conn: sqlite3.Connection) -> None:
    """Create the v4 pulse-domain tables if absent (all IF NOT EXISTS,
    so a pre-existing set is a no-op and existing rows are preserved).

    Individual ``execute`` calls — no ``executescript`` — so the calls
    stay inside the caller's transaction (migrate() owns BEGIN/COMMIT).
    """
    for stmt in (
        _CREATE_PULSE_TOOLS_TABLE,
        _CREATE_PULSE_TOOLS_INDEX,
        _CREATE_PULSE_PROTOTYPES_TABLE,
        _CREATE_PULSE_PROTOTYPES_INDEX,
        _CREATE_PULSE_GOAL_TABLE,
        _CREATE_PULSE_CAPSULES_TABLE,
        _CREATE_PULSE_CAPSULES_INDEX,
    ):
        conn.execute(stmt)


def migrate(conn: sqlite3.Connection,
            expected_version: int = CURRENT_SCHEMA_VERSION) -> bool:
    """Upgrade ``conn``'s database to ``expected_version`` in place.

    Idempotent: a database already at (or above) the expected version is a
    no-op (returns False). Fresh databases get the canonical DDL; legacy
    databases (e.g. missing ``tool_name``) get the missing columns added via
    ``ALTER TABLE`` (existing rows preserved, new columns NULL) *before* the
    indexes that reference them are created. v2 -> v3 likewise gains the
    ``portrait_history`` table (empty; existing events rows preserved), and
    v3 -> v4 gains the pulse-domain tables (empty; existing rows preserved),
    and v4 -> v5 gains ``pattern_affinity_history`` (empty; existing rows
    preserved), and v5 -> v6 gains ``pattern_bank`` (empty; existing rows
    preserved).
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
        _create_pulse_tables(conn)
        _create_pattern_affinity_history(conn)
        _create_pattern_bank(conn)
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
    "PULSE_TOOLS_DDL",
    "PULSE_PROTOTYPES_DDL",
    "PULSE_GOAL_DDL",
    "PULSE_CAPSULES_DDL",
    "PULSE_STATE_DDL",
    "PATTERN_AFFINITY_HISTORY_MAX",
    "PATTERN_AFFINITY_HISTORY_DDL",
    "PATTERN_BANK_DDL",
    "prune_pattern_affinity_history",
    "get_schema_version",
    "migrate",
    "schema_bootstrap",
]
