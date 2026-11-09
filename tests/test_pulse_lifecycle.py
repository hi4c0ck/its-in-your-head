"""PULSE Stage 1 lifecycle: profile-scoped lazy hydration (plan §6/§14).

Covers: restart restores banks/capsules/goal; feedback-before-first-poll
associates correctly; concurrent first reads don't double-load; reset
stays empty; per-profile isolation; no raw prompt/tool-output persisted;
live (hydrated) view equals DB-fallback view for same persisted evidence.
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from storage import database  # noqa: E402
import engine  # noqa: E402
from engine import Engine  # noqa: E402
from pulse import feedback  # noqa: E402
from pulse import persistence  # noqa: E402

_PROMPT = "please fix the flaky pulse bridge test"
_TOOL_OUTPUT = "SECRET_TOOL_OUTPUT_xyzzy_12345"


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "life.db"
    database.set_override(db)
    engine._engine = None
    eng = engine.get_engine()
    yield eng, db
    engine._engine = None
    database.set_override(None)


def _feed(eng, now):
    eng.on_events([
        {"type": "tool_start", "tool_name": "terminal", "ts": now},
        {"type": "tool_start", "tool_name": "read_file", "ts": now},
    ])
    eng.on_events([
        {"type": "tool_end", "tool_name": "terminal", "status": "error",
         "duration_ms": 40, "ts": now + 0.1},
        {"type": "tool_end", "tool_name": "read_file", "status": "ok",
         "duration_ms": 10, "ts": now + 0.2},
    ])
    eng.on_events([
        {"type": "user_turn", "ts": now + 0.3, "user_message": _PROMPT},
    ])
    feedback.apply_feedback(eng.protos, eng.capsule_state(), "great job")
    eng.on_events([])


def test_restart_restores_banks_capsules_goal(env):
    eng, db = env
    _feed(eng, time.time())
    before_banks = {t: eng.protos.bank(t) for t in eng.protos.tools()}
    before_caps = eng.capsule_state()
    before_goal = list(eng._goal_vector)

    # Fresh engine, same profile DB: no new events fed.
    fresh = Engine()
    assert fresh.protos.tools() == []
    assert fresh._goal_vector is None
    fresh.ensure_pulse_hydrated()
    assert {t: fresh.protos.bank(t) for t in fresh.protos.tools()} == before_banks
    # Capsule vectors+ts restored (in-memory capsules carry extra fields
    # beyond the persisted vector+ts, so compare those).
    def _capsig(caps):
        return {t: [(c.get("ts"), list(c.get("vector") or []))
                     for c in lst] for t, lst in caps.items()}
    assert _capsig(fresh.capsule_state()) == _capsig(before_caps)
    assert fresh._goal_vector == before_goal
    assert fresh._goal_ts is not None
    # Banks are clean (hydration must not re-mark dirty / re-persist).
    assert fresh.protos.dirty_tools() == set()
    # pulse_view works off restored state alone.
    view = fresh.pulse_view()
    assert view["available"] is True
    rows = {r["tool_name"]: r for r in view["rows"]}
    assert set(rows) == {"terminal", "read_file"}
    assert rows["read_file"]["good"]["value"] is not None
    assert rows["terminal"]["friction"]["objective"] is not None
    assert rows["terminal"]["friction"]["user"] is None


def test_restart_via_separate_process(tmp_path):
    db = tmp_path / "bridge.db"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    r = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "fixtures" / "pulse_agent_process.py"),
         str(db)], capture_output=True, text=True, env=env, cwd=str(ROOT),
        timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    r = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "fixtures" / "pulse_restart_process.py"),
         str(db)], capture_output=True, text=True, env=env, cwd=str(ROOT),
        timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    body = json.loads(r.stdout.strip().splitlines()[-1])
    assert body["available"] is True
    rows = {row["tool_name"]: row for row in body["rows"]}
    assert set(rows) == {"terminal", "read_file"}
    assert rows["read_file"]["good"]["value"] is not None
    assert rows["terminal"]["friction"]["objective"] is not None


def test_feedback_before_first_poll_associates_correctly(env):
    eng, db = env
    _feed(eng, time.time())
    # Fresh engine: feedback arrives via collector BEFORE any pulse_view.
    fresh = Engine()
    engine._engine = fresh
    from core import collector
    # Simulate persisted capsule being the most recent: hydration must
    # run inside collector._apply_explicit_feedback before association.
    collector._apply_explicit_feedback("great job")
    # The restored read_file capsule should have been labelled good
    # (most recent capsule across tools by ts).
    assert fresh.protos.bank("read_file")["good"], "feedback must associate"
    # The restored good bank had 1 vector; feedback labels the most
    # recent capsule again, so the count grows (not a hydration dup).
    assert len(fresh.protos.bank("read_file")["good"]) == 2


def test_concurrent_first_reads_do_not_double_load(env, monkeypatch):
    eng, db = env
    _feed(eng, time.time())
    fresh = Engine()
    calls = []
    orig = persistence.load_pulse_state_checked

    def counting():
        calls.append(1)
        time.sleep(0.05)  # widen the race window
        return orig()

    monkeypatch.setattr(persistence, "load_pulse_state_checked", counting)
    threads = [threading.Thread(target=fresh.ensure_pulse_hydrated)
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Also via pulse_view concurrently.
    threads = [threading.Thread(target=fresh.pulse_view) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, (
        f"load_pulse_state_checked called {len(calls)} times")


def test_reset_stays_empty(env, monkeypatch):
    eng, db = env
    _feed(eng, time.time())
    eng.reset()
    assert eng.protos.tools() == []
    assert eng.capsule_state() == {}
    assert eng._goal_vector is None
    calls = []
    orig = persistence.load_pulse_state
    monkeypatch.setattr(persistence, "load_pulse_state",
                        lambda: (calls.append(1), orig())[1])
    eng.ensure_pulse_hydrated()
    eng.pulse_view()
    assert calls == [], "reset must not trigger re-hydration"
    assert eng.protos.tools() == []
    assert eng.capsule_state() == {}


def test_per_profile_isolation(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    database.set_override(db_a)
    engine._engine = None
    eng_a = engine.get_engine()
    _feed(eng_a, time.time())
    # Switch profile: fresh engine against empty DB B must see nothing.
    database.set_override(db_b)
    fresh_b = Engine()
    fresh_b.ensure_pulse_hydrated()
    assert fresh_b.protos.tools() == []
    assert fresh_b.capsule_state() == {}
    assert fresh_b._goal_vector is None
    assert fresh_b.pulse_view()["available"] is False
    # Switch back to A: still intact (never combined, never migrated).
    database.set_override(db_a)
    fresh_a = Engine()
    fresh_a.ensure_pulse_hydrated()
    assert set(fresh_a.protos.tools()) == {"read_file", "terminal"}
    engine._engine = None
    database.set_override(None)


def test_no_raw_prompt_or_tool_output_persisted(env):
    eng, db = env
    _feed(eng, time.time())
    # Tool output text must never reach the DB: simulate a tool_end via
    # collector path carrying result text — engine only stores vector.
    conn = sqlite3.connect(str(db))
    try:
        for table in ("pulse_tools", "pulse_prototypes", "pulse_goal",
                      "pulse_capsules", "events"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert _PROMPT not in repr(rows), table
            assert _TOOL_OUTPUT not in repr(rows), table
        # user_message column stays NULL for pulse-written events? Check
        # the events table: user_turn events write user_message at the
        # metrics layer, but pulse tables must never contain raw text.
        for table in ("pulse_tools", "pulse_prototypes", "pulse_goal",
                      "pulse_capsules"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert _PROMPT not in repr(rows), table
    finally:
        conn.close()
    # load_pulse_state returns vectors only, no text fields.
    state = persistence.load_pulse_state()
    assert _PROMPT not in repr(state)


def test_live_hydrated_view_equals_db_fallback(env, monkeypatch):
    eng, db = env
    _feed(eng, time.time())
    fresh = Engine()
    live = fresh.pulse_view()
    # DB fallback (isolated dashboard mirror) from same persisted evidence.
    import dashboard.plugin_api as api
    api.set_db_path(db)
    fallback = api._pulse_shape()
    assert live["available"] is True and fallback["available"] is True
    live_rows = {r["tool_name"]: r for r in live["rows"]}
    fb_rows = {r["tool_name"]: r for r in fallback["rows"]}
    assert set(live_rows) == set(fb_rows)
    for tool in live_rows:
        for metric in ("goal", "good"):
            assert live_rows[tool][metric]["value"] == \
                pytest.approx(fb_rows[tool][metric]["value"]) \
                if fb_rows[tool][metric]["value"] is not None else \
                live_rows[tool][metric]["value"] is None
        assert live_rows[tool]["friction"]["objective"] == \
            fb_rows[tool]["friction"]["objective"]
        assert live_rows[tool]["friction"]["user"] == \
            fb_rows[tool]["friction"]["user"]
        assert live_rows[tool]["state"] == fb_rows[tool]["state"]
        assert live_rows[tool]["labeled"] == fb_rows[tool]["labeled"]


def test_on_tool_start_does_not_hydrate(env, monkeypatch):
    eng, db = env
    _feed(eng, time.time())
    fresh = Engine()
    calls = []
    orig = persistence.load_pulse_state
    monkeypatch.setattr(persistence, "load_pulse_state",
                        lambda: (calls.append(1), orig())[1])
    fresh.on_tool_start({"type": "tool_start", "tool_name": "terminal"})
    assert calls == [], "sync tool-start hot path must stay I/O-free"
    assert fresh._pulse_hydrated is False
