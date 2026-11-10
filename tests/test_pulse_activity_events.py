"""v0.1.10 (spec §3.3/§7.1): the cursor-based PULSE activity batch.

Pins: no-cursor baseline (high-water mark, no history), single/new
event delivery, five-calls-same-tool = five events, multi-tool
ascending event-id order, repeated-cursor no-duplicates, bounded
pagination with no skips, non-tool types excluded, and the fault
posture (DB down -> empty page, never a 500). The /pulse route
stays schema version 2 with the field purely additive.
"""
import asyncio
import sqlite3

import pytest

from dashboard import plugin_api as api
from storage import database
from storage import migration


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "activity.db"
    database.set_override(db)
    api.set_db_path(db)
    import engine
    engine._engine = None  # isolate the module singleton per test
    # Bootstrap the events table and yield a connection for inserts.
    conn = sqlite3.connect(str(db))
    migration.schema_bootstrap(conn, migration.EVENTS_DDL)
    yield db, conn
    conn.close()
    engine._engine = None
    database.set_override(None)
    api.set_db_path(None)


def _tool_start(conn, tool, call_id=None, ts=1000.0):
    cur = conn.execute(
        "INSERT INTO events (ts, type, tool_name, tool_call_id)"
        " VALUES (?, 'tool_start', ?, ?)",
        (ts, tool, call_id))
    conn.commit()
    return cur.lastrowid


def _other(conn, etype, ts=1000.0):
    conn.execute(
        "INSERT INTO events (ts, type, user_message)"
        " VALUES (?, ?, ?)", (ts, etype, "x"))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# --- no-cursor baseline -----------------------------------------------------

def test_no_cursor_establishes_high_water_mark(env):
    db, conn = env
    i1 = _tool_start(conn, "terminal", ts=100.0)
    i2 = _tool_start(conn, "read_file", ts=200.0)
    r = api._pulse_activity_events(None)
    assert r["events"] == [], "no historical events on baseline"
    assert r["next_cursor"] == i2, "high-water mark = MAX(id)"
    assert r["has_more"] is False


def test_no_cursor_empty_database(env):
    db, conn = env
    r = api._pulse_activity_events(None)
    assert r["events"] == [] and r["next_cursor"] == 0


# --- single + repeated-calls delivery ----------------------------------------

def test_one_new_tool_start_returns_one_event(env):
    db, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    i = _tool_start(conn, "terminal", call_id="call_a")
    r = api._pulse_activity_events(base)
    assert [e["id"] for e in r["events"]] == [i]
    assert r["events"][0]["tool_name"] == "terminal"
    assert r["events"][0]["tool_call_id"] == "call_a"
    assert r["next_cursor"] == i and r["has_more"] is False


def test_five_calls_same_tool_are_five_events(env):
    db, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    ids = [_tool_start(conn, "terminal", ts=100.0 + i) for i in range(5)]
    r = api._pulse_activity_events(base)
    assert [e["id"] for e in r["events"]] == ids, \
        "five calls to ONE tool = five events (no collapsing)"
    assert all(e["tool_name"] == "terminal" for e in r["events"])


def test_multi_tools_keep_ascending_event_id_order(env):
    db, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    a = _tool_start(conn, "terminal")
    b = _tool_start(conn, "github")
    c = _tool_start(conn, "terminal")
    d = _tool_start(conn, "browser")
    r = api._pulse_activity_events(base)
    assert [e["id"] for e in r["events"]] == [a, b, c, d]
    assert [e["tool_name"] for e in r["events"]] == \
        ["terminal", "github", "terminal", "browser"]


def test_repeated_cursor_request_no_duplicates(env):
    db, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    a = _tool_start(conn, "terminal")
    r1 = api._pulse_activity_events(base)
    assert [e["id"] for e in r1["events"]] == [a]
    # Same cursor again: the page is identical, not duplicated.
    r2 = api._pulse_activity_events(base)
    assert [e["id"] for e in r2["events"]] == [a]
    # Advanced cursor: nothing new.
    r3 = api._pulse_activity_events(r1["next_cursor"])
    assert r3["events"] == [] and r3["next_cursor"] == a
    _tool_start(conn, "github")
    r4 = api._pulse_activity_events(a)
    assert len(r4["events"]) == 1 and r4["events"][0]["tool_name"] == "github"


# --- pagination ---------------------------------------------------------------

def test_pagination_no_events_skipped(env):
    db, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    ids = [_tool_start(conn, "terminal") for _ in range(5)]
    p1 = api._pulse_activity_events(base, limit=2)
    assert [e["id"] for e in p1["events"]] == ids[:2]
    assert p1["has_more"] is True
    p2 = api._pulse_activity_events(p1["next_cursor"], limit=2)
    assert [e["id"] for e in p2["events"]] == ids[2:4]
    assert p2["has_more"] is True
    p3 = api._pulse_activity_events(p2["next_cursor"], limit=2)
    assert [e["id"] for e in p3["events"]] == ids[4:]
    assert p3["has_more"] is False, "final page not truncated"


def test_cursor_past_high_water_returns_empty_page(env):
    db, conn = env
    i = _tool_start(conn, "terminal")
    r = api._pulse_activity_events(i + 100)
    assert r["events"] == [] and r["has_more"] is False
    assert r["next_cursor"] == i + 100, "cursor never moves backwards"


# --- type filtering ------------------------------------------------------------

def test_non_tool_events_never_returned(env):
    db, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    _other(conn, "user_turn")
    _other(conn, "tool_end")
    _tool_start(conn, "terminal")
    _other(conn, "turn")
    r = api._pulse_activity_events(base)
    assert len(r["events"]) == 1 and r["events"][0]["tool_name"] == "terminal"


# --- fault posture ---------------------------------------------------------------

def test_db_fault_maps_to_empty_page_at_cursor(tmp_path, monkeypatch):
    def boom():
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(api._database, "connect", boom)
    r = api._pulse_activity_events(42)
    assert r["events"] == [] and r["has_more"] is False
    assert r["next_cursor"] == 42, "the caller's cursor is preserved"


# --- route wiring (additive v2 envelope) ------------------------------------------

def test_pulse_route_carries_activity_events(env, monkeypatch):
    db, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    i = _tool_start(conn, "terminal")
    # The engine half is absent in this fixture: fallback path.
    out = asyncio.run(api.pulse(after_event_id=base))
    assert out["schema_version"] == 2
    assert out["lane_order"] == ["live", "goal", "good", "friction"]
    assert "lanes" in out and "rows" in out, "legacy envelope intact"
    assert [e["id"] for e in out["activity_events"]["events"]] == [i]
    # No cursor -> baseline (additive, same envelope).
    out2 = asyncio.run(api.pulse())
    assert out2["activity_events"]["events"] == []
    assert out2["activity_events"]["next_cursor"] == i
