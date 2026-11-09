"""Lane 1 (v0.1.1 P0/P2): legacy-DB migration + DB-override unification.

LEGACY_DDL below is the exact pre-S1 shape simulated here: the current
events table WITHOUT the ``tool_name`` column (and therefore without the
idx_events_tool index, which cannot be created on that shape), with the
ts/type indexes, no meta table, and a handful of legacy rows.
"""
import importlib
import sqlite3

import pytest

from storage import database, migration

LEGACY_DDL = """
CREATE TABLE events (
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
);
CREATE INDEX idx_events_ts ON events (ts);
CREATE INDEX idx_events_type ON events (type);
"""


@pytest.fixture(autouse=True)
def _reset_override():
    yield
    database.set_override(None)


def _make_legacy(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_DDL)
    assert "tool_name" not in [
        r[1] for r in conn.execute("PRAGMA table_info(events)")]
    conn.execute(
        "INSERT INTO events (ts, type, session_id, user_message) "
        "VALUES (1000.0, 'turn', 's-legacy', 'legacy hello')")
    conn.execute(
        "INSERT INTO events (ts, type, session_id, total_tokens) "
        "VALUES (1001.0, 'api', 's-legacy', 42)")
    conn.execute(
        "INSERT INTO events (ts, type, session_id, status_code) "
        "VALUES (1002.0, 'error', 's-legacy', 500)")
    conn.commit()
    conn.close()
    return path


def _columns(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(events)")]


def _indexes(conn):
    return {r[1] for r in conn.execute("PRAGMA index_list(events)")}


def test_legacy_migrates_via_worker_persist_path(tmp_path):
    """Worker connect+migrate+batch-persist on a legacy DB: no drop."""
    import metrics
    importlib.reload(metrics)
    db_path = _make_legacy(tmp_path / "legacy.db")
    metrics.set_db_path(db_path)
    # Pre-migration the current INSERT (with tool_name) would fail; the
    # worker persist path must migrate first and succeed.
    metrics._persist_batch([{
        "type": "tool_end", "ts": 1003.0, "session_id": "s-legacy",
        "tool_name": "read_file", "status": "ok",
    }])
    conn = sqlite3.connect(str(db_path))
    try:
        assert "tool_name" in _columns(conn)
        assert "idx_events_tool" in _indexes(conn)
        assert migration.get_schema_version(conn) == migration.CURRENT_SCHEMA_VERSION
        rows = conn.execute(
            "SELECT type, tool_name, user_message FROM events ORDER BY id"
        ).fetchall()
        # Legacy rows preserved, tool_name NULL; new row persisted.
        assert [r[0] for r in rows] == ["turn", "api", "error", "tool_end"]
        assert rows[0][1] is None and rows[0][2] == "legacy hello"
        assert rows[3][1] == "read_file"
    finally:
        conn.close()


def test_legacy_dashboard_route_migrates(tmp_path):
    """Dashboard first-connect (route time) also migrates a legacy DB."""
    import dashboard.plugin_api as api
    importlib.reload(api)
    db_path = _make_legacy(tmp_path / "legacy-api.db")
    api.set_db_path(db_path)
    rows = api._query("SELECT COUNT(*) FROM events")
    assert rows == [(3,)]
    conn = sqlite3.connect(str(db_path))
    try:
        assert "tool_name" in _columns(conn)
        assert "idx_events_tool" in _indexes(conn)
        assert migration.get_schema_version(conn) == migration.CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_fresh_bootstrap_and_second_call_is_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    database.set_override(db_path)
    conn = database.connect()
    try:
        assert migration.schema_bootstrap(conn) is True
        assert migration.get_schema_version(conn) == migration.CURRENT_SCHEMA_VERSION
        assert "tool_name" in _columns(conn)
        assert {"idx_events_ts", "idx_events_type", "idx_events_tool"} \
            <= _indexes(conn)
    finally:
        conn.close()

    calls = []
    orig = migration._create_indexes

    def _spy(c):
        calls.append(1)
        return orig(c)

    monkeypatch.setattr(migration, "_create_indexes", _spy)
    conn = database.connect()
    try:
        assert migration.schema_bootstrap(conn) is False  # fast-path no-op
        assert migration.migrate(conn) is False
    finally:
        conn.close()
    assert calls == [], "DDL must not re-run on an already-migrated DB"


def test_override_unified_api_and_worker_same_file(tmp_path):
    import dashboard.plugin_api as api
    import metrics
    importlib.reload(api)
    importlib.reload(metrics)
    db_path = tmp_path / "shared.db"
    api.set_db_path(db_path)
    # storage.database is the single resolver for both entry points.
    assert database.resolve_path() == db_path
    assert database._db_path_override == db_path
    assert not hasattr(api, "_db_path_override")
    conn = api._connect()
    try:
        listed = conn.execute("PRAGMA database_list").fetchall()
        assert any(str(db_path) == str(r[2]) or str(db_path).endswith(r[2])
                   for r in listed)
        migration.schema_bootstrap(conn)
    finally:
        conn.close()
    # Worker connect() targets the same file and sees the migrated schema.
    metrics.set_db_path(db_path)
    assert database.resolve_path() == db_path
    conn = database.connect()
    try:
        assert migration.get_schema_version(conn) == migration.CURRENT_SCHEMA_VERSION
        assert "tool_name" in _columns(conn)
    finally:
        conn.close()


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    """Failure after the ALTER must leave no partial state."""
    db_path = _make_legacy(tmp_path / "legacy-fail.db")
    database.set_override(db_path)
    conn = database.connect()

    def _boom(c):
        raise RuntimeError("simulated mid-migration failure")

    monkeypatch.setattr(migration, "_create_indexes", _boom)
    with pytest.raises(RuntimeError):
        migration.migrate(conn)
    conn.close()

    conn = sqlite3.connect(str(db_path))
    try:
        # No partial state: no tool_name column, no tool index, no version.
        assert "tool_name" not in _columns(conn)
        assert "idx_events_tool" not in _indexes(conn)
        assert migration.get_schema_version(conn) is None
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert n == 3  # legacy rows untouched
    finally:
        conn.close()

    # And a clean retry migrates successfully.
    monkeypatch.undo()
    database.set_override(db_path)
    conn = database.connect()
    try:
        assert migration.migrate(conn) is True
        assert "tool_name" in _columns(conn)
        assert migration.get_schema_version(conn) == migration.CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_schema_aliases_are_canonical():
    import dashboard.plugin_api as api
    import metrics
    assert metrics.SCHEMA == migration.EVENTS_DDL
    assert api._DDL == migration.EVENTS_DDL


def test_schema_v5_pattern_affinity_history_ddl(tmp_path):
    database.set_override(tmp_path / "v5.db")
    conn = database.connect()
    try:
        assert migration.schema_bootstrap(conn) is True
        assert migration.get_schema_version(conn) == migration.CURRENT_SCHEMA_VERSION
        assert migration.CURRENT_SCHEMA_VERSION == 8
        assert migration.PATTERN_AFFINITY_HISTORY_MAX == 512
        assert migration.PATTERN_AFFINITY_HISTORY_MAX == \
            migration.PORTRAIT_HISTORY_MAX
        # E3/v7: the top-3 persistence columns exist on a fresh DB.
        pah_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(pattern_affinity_history)")}
        assert {"rank", "turn_key"} <= pah_cols
        # DDL presence
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='pattern_affinity_history'").fetchone()
        assert row is not None
        cols = conn.execute(
            "PRAGMA table_info(pattern_affinity_history)").fetchall()
        names = [r[1] for r in cols]
        types = {r[1]: r[2].upper() for r in cols}
        assert names == ["id", "ts", "top_pattern", "top_prompt_affinity",
                         "top_agent_affinity", "top_margin",
                         "top_amplification", "evidence", "confidence",
                         "repeated", "rank", "turn_key"], names
        assert types["ts"] == "REAL"
        assert types["top_pattern"] == "TEXT"
        assert types["top_prompt_affinity"] == "REAL"
        assert types["top_agent_affinity"] == "REAL"
        assert types["top_margin"] == "REAL"
        assert types["top_amplification"] == "REAL"
        assert types["evidence"] == "INTEGER"
        assert types["confidence"] == "REAL"
        assert types["repeated"] == "INTEGER"
        assert types["rank"] == "INTEGER"
        assert types["turn_key"] == "TEXT"
        # No free-text user/reasoning columns: only top_pattern (a preset
        # bank pattern name, never user content) and turn_key (a
        # deterministic turn-ts string, never user content) may be TEXT.
        text_cols = [n for n, t in types.items() if t == "TEXT"]
        assert text_cols == ["top_pattern", "turn_key"], text_cols
        # defaults
        defaults = {r[1]: r[4] for r in cols}
        assert defaults["evidence"] == "0"
        assert defaults["repeated"] == "0"
        assert defaults["rank"] == "1"
    finally:
        conn.close()


def test_pattern_affinity_prune_to_cap(tmp_path):
    database.set_override(tmp_path / "prune.db")
    conn = database.connect()
    try:
        migration.schema_bootstrap(conn)
        n = migration.PATTERN_AFFINITY_HISTORY_MAX + 20
        for i in range(n):
            conn.execute(
                "INSERT INTO pattern_affinity_history "
                "(ts, top_pattern, evidence) VALUES (?, ?, ?)",
                (float(i), "focused", i))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM pattern_affinity_history"
        ).fetchone()[0] == n
        migration.prune_pattern_affinity_history(conn)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM pattern_affinity_history"
        ).fetchone()[0] == migration.PATTERN_AFFINITY_HISTORY_MAX
        # keeps the most recent rows
        first = conn.execute(
            "SELECT MIN(ts) FROM pattern_affinity_history").fetchone()[0]
        assert first == 20.0
        # log-and-never-raise: pruning a DB without the table is a no-op
        conn2 = sqlite3.connect(":memory:")
        try:
            migration.prune_pattern_affinity_history(conn2)  # no raise
        finally:
            conn2.close()
    finally:
        conn.close()
