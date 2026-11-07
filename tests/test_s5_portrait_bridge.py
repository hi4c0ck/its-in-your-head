"""PORTRAIT cross-process bridge (v3 portrait_history fallback).

/pulse gained a profile-DB fallback for the isolated dashboard half;
/portrait had the same gap: /drift reads portrait_history, but
/portrait only probed for an importable portrait function, so the
agent worker's persisted observations were invisible to the pane.
These tests pin:

1. agent side: user turns persist observed portrait dimensions to
   portrait_history (the F2 durable path);
2. dashboard side: /portrait falls back to the latest
   portrait_history row when the in-process engine view is
   unavailable — dimensions + user carry the stored observed values,
   confidence/agent stay None (the interpreted track is never
   invented);
3. separate processes: the agent fixture writes, the dashboard fixture
   (fresh process, agent modules absent) reads the observed track.
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

_PROMPT_A = "please tighten the portrait fallback tests"
_PROMPT_B = "now make the drift sparklines honest"


@pytest.fixture
def agent_env(tmp_path):
    """Engine + DB override in the test process (fresh engine)."""
    db = tmp_path / "agent.db"
    database.set_override(db)
    engine._engine = None
    eng = engine.get_engine()
    yield eng, db
    engine._engine = None
    database.set_override(None)


def _api(db):
    import importlib
    import dashboard.plugin_api as api
    importlib.reload(api)
    api.set_db_path(db)
    return api


def test_agent_side_persists_portrait_observations(agent_env):
    eng, db = agent_env
    now = time.time()
    eng.on_events([
        {"type": "turn", "ts": now, "user_message": _PROMPT_A},
        {"type": "turn", "ts": now + 0.5, "user_message": _PROMPT_B},
    ])
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT ts, directness, context_richness, exploration,"
            " analytical_depth, iteration, scope"
            " FROM portrait_history ORDER BY ts, id").fetchall()
        assert len(rows) == 2
        latest = rows[-1]
        assert all(v is not None for v in latest[1:]), latest
        # Privacy: no raw prompt text in the table (v0.1.3 ruling)
        blob = conn.execute("SELECT * FROM portrait_history").fetchall()
        assert _PROMPT_A not in repr(blob)
        assert _PROMPT_B not in repr(blob)
    finally:
        conn.close()


def test_portrait_fallback_serves_last_observation(agent_env, monkeypatch):
    eng, db = agent_env
    now = time.time()
    eng.on_events([
        {"type": "turn", "ts": now, "user_message": _PROMPT_A},
        {"type": "turn", "ts": now + 0.5, "user_message": _PROMPT_B},
    ])
    api = _api(db)
    # Simulate the isolated child deterministically: no in-process
    # engine view, and the perception probe fails (plugin root is off
    # the child's sys.path).
    monkeypatch.setattr(api, "_portrait_live_view", lambda: None)
    monkeypatch.setattr(api, "_engine_first", lambda *a, **k: (None, None))

    shape = api._portrait_shape()
    assert shape["available"] is True
    dims = shape["dimensions"]
    assert all(v is not None for v in dims.values()), dims
    # Observed track: dimensions and user carry the same stored values
    assert shape["user"] == dims
    # Neither track is persisted -> confidence/agent stay None
    assert all(v is None for v in shape["confidence"].values())
    assert all(v is None for v in shape["agent"].values())

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api.router)
    body = TestClient(app).get("/portrait").json()
    assert body["available"] is True
    assert body["dimensions"] == dims
    assert body["user"] == dims
    assert body["confidence"] == shape["confidence"]
    assert body["agent"] == shape["agent"]


def test_portrait_fallback_empty_db_returns_documented_shape(tmp_path):
    api = _api(tmp_path / "empty.db")
    # The probe is importable in the test process; force the fallback
    # branch by stubbing the probe like the isolated child sees it.
    orig = api._engine_first
    api._engine_first = lambda *a, **k: (None, None)
    try:
        shape = api._portrait_shape()
    finally:
        api._engine_first = orig
    assert shape == {"available": False,
                     "dimensions": {d: None for d in
                                    shape["dimensions"].keys()},
                     "confidence": {d: None for d in
                                    shape["confidence"].keys()},
                     "user": {d: None for d in shape["user"].keys()},
                     "agent": {d: None for d in shape["agent"].keys()}}


def test_portrait_live_view_wins_over_fallback(agent_env, monkeypatch):
    eng, db = agent_env
    now = time.time()
    eng.on_events([
        {"type": "turn", "ts": now, "user_message": _PROMPT_A},
    ])
    api = _api(db)
    # The gateway half holds observed state -> the live view is served
    # and the fallback must NOT be consulted.
    assert api._portrait_live_view() is not None
    called: dict = {}
    monkeypatch.setattr(
        api, "_portrait_shape", lambda: called.setdefault("n", 1) or {})
    import asyncio
    body = asyncio.run(api.portrait())
    assert body["available"] is True
    assert body["dimensions"], "live view must carry observed values"
    assert "n" not in called, "fallback must not be consulted when live"


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


def test_separate_processes_portrait_bridge(tmp_path):
    """The exact production topology: agent process writes, dashboard
    process (fresh, agent modules absent) reads the same profile DB."""
    db = tmp_path / "portrait.db"
    _run_fixture("portrait_agent_process.py", db)
    out = _run_fixture("portrait_dashboard_process.py", db)
    body = json.loads(out.splitlines()[-1])
    assert body["available"] is True
    dims = body["dimensions"]
    assert all(v is not None for v in dims.values()), dims
    # user track mirrors the observed dimensions (same stored values)
    assert body["user"] == dims
    # unpersisted tracks stay None, never invented
    assert all(v is None for v in body["confidence"].values())
    assert all(v is None for v in body["agent"].values())
