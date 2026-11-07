"""F2 (v0.1.2): portrait_history durability — drift survives restart.

Covers: (a) restart simulation (persist N, new Engine rehydrates,
/drift probe returns the persisted rows), (b) v2->v3 migration
idempotence (run twice; events preserved), (c) writer failure
isolation (persist error degrades to in-memory, no leak), (d) the
512-row retention bound (prune on write + deque cap on rehydrate).

Byte-small fixtures only; no network; DB via storage.database override.
"""
import sqlite3
import time

import pytest

import metrics
from storage import database, migration


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "f2.db"
    metrics.set_db_path(db)
    import engine
    monkeypatch.setattr(engine, "_engine", None)
    yield db
    database.set_override(None)
    try:
        metrics.stop_worker()
    except Exception:
        pass


def _eng():
    import engine
    return engine.Engine()


def _user_turn(text, ts):
    return {"type": "turn", "ts": ts, "session_id": "s1",
            "turn_id": "t1", "user_message": text, "user_chars": len(text)}


def _count_ph(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM portrait_history").fetchone()[0]
    finally:
        conn.close()


# (a) restart simulation ---------------------------------------------------- #
def test_restart_rehydrates_and_probe_returns_persisted(_tmp_db):
    eng1 = _eng()
    for i in range(4):
        eng1.on_events([_user_turn(f"prompt {i}", ts=1000.0 + i)])
    pre = eng1.drift_view()
    assert pre["available"] is True
    assert all(len(s) == 4 for s in pre["series"].values())
    assert _count_ph(_tmp_db) == 4

    # Simulate a plugin-process restart: a brand-new Engine on the same
    # DB file. No DB handle is open at construction (lazy rehydrate).
    eng2 = _eng()
    assert list(eng2._history) == []
    post = eng2.drift_view()
    assert post["available"] is True
    # In-memory rehydrated view == pre-restart view (exact float values).
    assert post["series"] == pre["series"]

    # The /drift fallback probe (dashboard _drift_shape) sees the same rows.
    import dashboard.plugin_api as api
    probe = api._drift_shape()
    assert probe["available"] is True
    assert probe["series"] == pre["series"]

    # Ordering preserved (ts, id) across the restart.
    assert len(probe["series"]["directness"]) == 4


# (b) migration idempotence -------------------------------------------------- #
def test_migration_v2_to_v3_idempotent_and_preserves_events(_tmp_db):
    # Build a v2-shape DB by hand: events DDL + meta=2, no portrait_history.
    conn = sqlite3.connect(str(_tmp_db))
    try:
        conn.executescript(migration.EVENTS_DDL)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '2')")
        conn.execute(
            "INSERT INTO events (ts, type, user_message) "
            "VALUES (1.0, 'turn', 'legacy hello')")
        conn.commit()
    finally:
        conn.close()

    database.set_override(_tmp_db)
    conn = database.connect()
    try:
        assert migration.migrate(conn) is True
        assert migration.get_schema_version(conn) == 6
        cols = [r[1] for r in conn.execute("PRAGMA table_info(portrait_history)")]
        assert cols == ["id", "ts", *migration.PORTRAIT_HISTORY_DIMS]
        assert conn.execute("SELECT COUNT(*) FROM portrait_history").fetchone()[0] == 0
        assert conn.execute(
            "SELECT user_message FROM events").fetchone()[0] == "legacy hello"
        # Purposely persist one row, then run migrate again: no-op, row kept.
        conn.execute(
            "INSERT INTO portrait_history (ts, directness) VALUES (2.0, 0.5)")
        conn.commit()
        assert migration.migrate(conn) is False  # second run: fast-path no-op
        assert migration.schema_bootstrap(conn) is False
        assert conn.execute("SELECT COUNT(*) FROM portrait_history").fetchone()[0] == 1
        assert migration.get_schema_version(conn) == 6
    finally:
        conn.close()

    # Fresh-DB migrate also creates the table and is a no-op the 2nd time.
    fresh = _tmp_db.with_name("fresh.db")
    database.set_override(fresh)
    conn = database.connect()
    try:
        assert migration.migrate(conn) is True
        assert migration.migrate(conn) is False
        assert migration.get_schema_version(conn) == 6
    finally:
        conn.close()


# (c) writer failure isolation ---------------------------------------------- #
def test_writer_failure_degrades_to_in_memory(monkeypatch, caplog):
    eng = _eng()

    def _boom():
        raise RuntimeError("simulated persist failure")

    monkeypatch.setattr(database, "connect", _boom)
    with caplog.at_level("ERROR"):
        # Must not raise: perception still updates, history is in-memory.
        eng.on_events([_user_turn("fix the login", ts=2000.0)])
    assert any("portrait_history persist failed" in r.message
               or "portrait_history persist failed" in r.getMessage()
               for r in caplog.records)
    view = eng.drift_view()
    assert view["available"] is True
    assert all(len(s) == 1 for s in view["series"].values())


# (d) retention bound --------------------------------------------------------- #
def test_retention_pruned_on_write_and_rehydrate_capped(_tmp_db):
    database.set_override(_tmp_db)
    conn = database.connect()
    try:
        migration.schema_bootstrap(conn)
        dims = migration.PORTRAIT_HISTORY_DIMS
        n = migration.PORTRAIT_HISTORY_MAX + 20  # 532 rows over the cap
        conn.executemany(
            f"INSERT INTO portrait_history (ts, {', '.join(dims)}) "
            f"VALUES ({', '.join(['?'] * (len(dims) + 1))})",
            [(1000.0 + i,) + tuple(0.01 * i for _ in dims)
             for i in range(n)])
        conn.commit()
    finally:
        conn.close()
    assert _count_ph(_tmp_db) == n

    # One engine write prunes back to the most recent 512 (by id).
    eng = _eng()
    eng._ensure_history()  # also capped by the deque bound (512)
    assert len(eng._history) == migration.PORTRAIT_HISTORY_MAX
    eng._persist_history_entry(
        {"ts": 9999.0, "values": {d: 0.5 for d in migration.PORTRAIT_HISTORY_DIMS}})
    assert _count_ph(_tmp_db) == migration.PORTRAIT_HISTORY_MAX
    conn = sqlite3.connect(str(_tmp_db))
    try:
        mn = conn.execute("SELECT MIN(ts) FROM portrait_history").fetchone()[0]
        mx = conn.execute("SELECT MAX(ts) FROM portrait_history").fetchone()[0]
        assert mx == 9999.0
        # Oldest 21 rows (20 overflow + the new write) evicted: min ts is
        # the row with index 21 (1000.0 + 21).
        assert mn == 1000.0 + 21
    finally:
        conn.close()

    # Rehydrate of an over-cap table still respects the deque bound and
    # yields the most recent entries in order.
    eng2 = _eng()
    view = eng2.drift_view()
    assert all(len(s) == migration.PORTRAIT_HISTORY_MAX
               for s in view["series"].values())
    assert view["series"]["directness"][-1] == 0.5  # the new write is last
