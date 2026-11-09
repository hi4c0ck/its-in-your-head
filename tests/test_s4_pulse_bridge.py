"""PULSE cross-process bridge (schema v4).

The agent half (metrics worker + engine) and the isolated dashboard half
(the plugin-host child) run in separate processes against the same
profile DB. These tests pin the two bridges:

1. agent side: ``engine.on_events`` -> ``pulse_tools`` /
   ``pulse_prototypes`` / ``pulse_goal`` / ``pulse_capsules`` (the
   worker persists; the hot path never does I/O);
2. dashboard side: ``/pulse`` falls back to the profile DB when the
   in-process engine view is unavailable, keeping the four-column
   contract; empty banks stay neutral and never hide tool rows;
3. separate processes: the agent fixture writes, the dashboard fixture
   (fresh process, engine module absent) reads back the same rows,
   derived vectors and objective-vs-user friction split.

Privacy invariant: the pulse domain stores DERIVED vectors only — no
raw prompt text anywhere (v0.1.3 ruling).
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from storage import database  # noqa: E402
import engine  # noqa: E402
from pulse import feedback  # noqa: E402

_PROMPT = "please fix the flaky pulse bridge test"


@pytest.fixture
def agent_env(tmp_path):
    """Engine + DB override (v4 pulse tables) in the test process."""
    db = tmp_path / "agent.db"
    database.set_override(db)
    engine._engine = None
    eng = engine.get_engine()
    yield eng, db
    engine._engine = None
    database.set_override(None)


def _feed_engine(eng, now: float) -> None:
    eng.on_events([
        {"type": "tool_start", "tool_name": "terminal", "ts": now},
        {"type": "tool_start", "tool_name": "read_file", "ts": now},
    ])
    eng.on_events([
        # The error end is objective friction (never user friction).
        {"type": "tool_end", "tool_name": "terminal", "status": "error",
         "duration_ms": 40, "ts": now + 0.1},
        {"type": "tool_end", "tool_name": "read_file", "status": "ok",
         "duration_ms": 10, "ts": now + 0.2},
    ])
    eng.on_events([
        {"type": "user_turn", "ts": now + 0.3, "user_message": _PROMPT},
    ])
    # Hot-path feedback (the collector's API): labels the most recent
    # capsule, marks the bank dirty — I/O happens on the next worker
    # pass only.
    feedback.apply_feedback(eng.protos, eng.capsule_state(), "great job")
    eng.on_events([])


def test_agent_side_persists_pulse_state(agent_env):
    eng, db = agent_env
    _feed_engine(eng, time.time())
    conn = sqlite3.connect(str(db))
    try:
        tools = [r[0] for r in conn.execute(
            "SELECT tool_name FROM pulse_tools ORDER BY tool_name")]
        assert tools == ["read_file", "terminal"]
        assert conn.execute(
            "SELECT errors FROM pulse_tools WHERE tool_name = 'terminal'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT calls_completed FROM pulse_tools"
            " WHERE tool_name = 'read_file'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM pulse_prototypes WHERE bank = 'good'"
        ).fetchone()[0] == 1  # 'great job' labelled the read_file capsule
        assert conn.execute(
            "SELECT COUNT(*) FROM pulse_prototypes"
            " WHERE bank = 'objective_friction'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM pulse_goal WHERE id = 1").fetchone()[0] == 1
        goal_blob = conn.execute(
            "SELECT vector FROM pulse_goal WHERE id = 1").fetchone()[0]
        assert goal_blob and len(goal_blob) % 4 == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pulse_capsules").fetchone()[0] == 2
        # Privacy: no raw prompt in the pulse domain (any table, any cell)
        for table in ("pulse_tools", "pulse_prototypes", "pulse_goal",
                      "pulse_capsules"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert _PROMPT not in repr(rows), table
    finally:
        conn.close()


def _api(db):
    import importlib
    import dashboard.plugin_api as api
    importlib.reload(api)
    api.set_db_path(db)
    return api


def test_pulse_fallback_serves_db_rows_when_engine_unavailable(agent_env,
                                                                monkeypatch):
    eng, db = agent_env
    _feed_engine(eng, time.time())
    api = _api(db)
    # Simulate the isolated child: the live-view probe finds no in-process
    # engine -> the route must serve the DB fallback, not an empty shape.
    monkeypatch.setattr(api, "_pulse_live_view", lambda: None)

    shape = api._pulse_shape()
    assert shape["available"] is True
    assert shape["columns"] == ["goal", "good", "friction", "unlabeled"]
    rows = {r["tool_name"]: r for r in shape["rows"]}
    assert set(rows) == {"terminal", "read_file"}
    # LIVE rows are never hidden: both tools surface with counters.
    assert rows["terminal"]["live"]["errors"] == 1
    assert rows["read_file"]["live"]["calls_completed"] == 1
    # GOOD: the feedback-labelled capsule scores against the goal vector.
    assert rows["read_file"]["good"]["value"] is not None
    # FRICTION split: the error end is objective-only; an error end must
    # never render as USER friction.
    assert rows["terminal"]["friction"]["objective"] is not None
    assert rows["terminal"]["friction"]["user"] is None
    # Empty-bank readiness: no labelled friction on read_file -> the
    # friction cell is learning (goal context present, no evidence),
    # value None, level neutral.
    assert rows["read_file"]["friction"]["value"] is None
    assert rows["read_file"]["friction"]["state"] == "learning"
    assert rows["read_file"]["friction"]["user_state"] == "learning"
    assert rows["read_file"]["friction"]["objective_state"] == "learning"
    # GOOD per-cell: one labelled sample -> provisional, level visible.
    assert rows["read_file"]["good"]["support"]["count"] == 1
    assert rows["read_file"]["good"]["state"] == "provisional"
    assert rows["read_file"]["good"]["level"] in ("weak", "strong")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api.router)
    body = TestClient(app).get("/pulse").json()
    assert body["available"] is True
    assert body["rows"] == shape["rows"]


def test_pulse_live_view_wins_over_fallback(agent_env, monkeypatch):
    eng, db = agent_env
    _feed_engine(eng, time.time())
    api = _api(db)
    # The gateway half holds live state -> the live view is served and the
    # fallback must NOT be consulted (rows already exist in-memory).
    assert api._pulse_live_view() is not None
    called: dict = {}
    monkeypatch.setattr(
        api, "_pulse_shape", lambda: called.setdefault("n", 1) or {})
    import asyncio
    body = asyncio.run(api.pulse())
    assert body["rows"], "live view must carry rows"
    assert "n" not in called, "fallback must not be consulted when live"


def test_pulse_fallback_empty_db_returns_documented_shape(tmp_path):
    api = _api(tmp_path / "empty.db")
    monkeypatch_free = api._pulse_shape()
    assert monkeypatch_free == {"rows": [],
                                "columns": ["goal", "good", "friction",
                                            "unlabeled"],
                                "available": False}


def _run_fixture(script: str, db, timeout: int = 120) -> str:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "fixtures" / script),
         str(db)],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
        timeout=timeout)
    assert result.returncode == 0, (
        f"{script} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[-400:]}\nstderr: {result.stderr[-800:]}")
    return result.stdout.strip()


def test_separate_processes_pulse_bridge(tmp_path):
    """The exact production topology: agent process writes, dashboard
    process (fresh, no engine module) reads the same profile DB."""
    db = tmp_path / "bridge.db"
    _run_fixture("pulse_agent_process.py", db)
    out = _run_fixture("pulse_dashboard_process.py", db)
    body = json.loads(out.splitlines()[-1])
    assert body["available"] is True
    assert body["columns"] == ["goal", "good", "friction", "unlabeled"]
    rows = {r["tool_name"]: r for r in body["rows"]}
    assert set(rows) == {"terminal", "read_file"}
    for row in body["rows"]:
        assert {"tool_name", "tool_id", "state", "labeled", "live",
                "goal", "good", "friction", "momentum"} == set(row)
        # Momentum (Tier-0 positive feedback) crossed the process bridge as
        # a well-formed cell: a known state + a value floored into [0, 1].
        m = row["momentum"]
        assert m["state"] in ("surging", "steady", "cooling")
        assert 0.0 <= m["value"] <= 1.0
    # LIVE counters crossed the process boundary.
    assert rows["terminal"]["live"]["errors"] == 1
    assert rows["read_file"]["live"]["calls_completed"] == 1
    # Derived goal vector scored the objective bank across processes.
    assert rows["terminal"]["goal"]["value"] is not None
    # Feedback bridge: 'great job' -> read_file good bank.
    assert rows["read_file"]["good"]["value"] is not None
    # Friction split held through the bridge.
    assert rows["terminal"]["friction"]["objective"] is not None
    assert rows["terminal"]["friction"]["user"] is None


def test_separate_process_goal_vector_is_deterministic(tmp_path):
    """Same prompt + tier-0 sketch -> identical goal vector in a fresh
    process (the bridge carries numbers, not model state)."""
    db = tmp_path / "det.db"
    _run_fixture("pulse_agent_process.py", db)
    conn = sqlite3.connect(str(db))
    try:
        v1 = conn.execute(
            "SELECT vector FROM pulse_goal WHERE id = 1").fetchone()[0]
    finally:
        conn.close()
    _run_fixture("pulse_agent_process.py", db)
    conn = sqlite3.connect(str(db))
    try:
        v2 = conn.execute(
            "SELECT vector FROM pulse_goal WHERE id = 1").fetchone()[0]
    finally:
        conn.close()
    assert v1 == v2 and len(v1) > 0
