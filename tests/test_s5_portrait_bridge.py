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
    # No pattern_affinity_history rows -> empty perception shape
    assert shape["perception"] == _empty_perception_expected(
        list(shape["dimensions"].keys()))

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


def test_portrait_probe_importable_but_empty_serves_db_fallback(
        agent_env, monkeypatch):
    """Gateway half after a restart: the perception probe IS importable
    but yields nothing (fresh in-memory engine) — the persisted
    observations must still surface from the profile DB, never be
    hidden by the empty state."""
    eng, db = agent_env
    now = time.time()
    eng.on_events([
        {"type": "turn", "ts": now, "user_message": _PROMPT_A},
    ])
    api = _api(db)
    monkeypatch.setattr(api, "_portrait_live_view", lambda: None)

    class _mod:  # a found module; its zero-arg candidate returns {}
        pass
    monkeypatch.setattr(
        api, "_engine_first", lambda *a, **k: (_mod(), lambda: {}))

    shape = api._portrait_shape()
    assert shape["available"] is True
    assert all(v is not None for v in shape["dimensions"].values()), shape
    assert shape["user"] == shape["dimensions"]
    assert all(v is None for v in shape["confidence"].values())
    assert all(v is not None for v in shape["dimensions"].values())


def _empty_perception_expected(dims):
    return {"patterns": [],
            "dim_signals": {d: {"prompt": None, "agent": None}
                            for d in dims},
            "perception_available": False}


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
    dims = list(shape["dimensions"].keys())
    assert shape == {"available": False,
                     "dimensions": {d: None for d in dims},
                     "confidence": {d: None for d in dims},
                     "user": {d: None for d in dims},
                     "agent": {d: None for d in dims},
                     "perception": _empty_perception_expected(dims),
                     # S16 additive keys: the transient live state is never
                     # reconstructed from history (neutral rest), and the
                     # morphology is neutral without persisted evidence.
                     "live_expression": dict(api._NEUTRAL_LIVE_EXPRESSION),
                     "morphology": dict(api._NEUTRAL_MORPHOLOGY)}


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
    # perception filled from the dashboard fixture's seeded latest row
    perc = body["perception"]
    assert perc["perception_available"] is True
    assert len(perc["patterns"]) == 1
    pat = perc["patterns"][0]
    assert pat == {"name": "focused", "prompt_affinity": 0.42,
                   "agent_affinity": 0.81, "margin": 0.27,
                   "amplification": 0.39, "evidence": 3,
                   "confidence": 0.75, "repeated": False}, pat
    # per-dim signals are not persisted -> all None, never invented
    assert set(perc["dim_signals"].keys()) == set(dims.keys())
    assert all(s == {"prompt": None, "agent": None}
               for s in perc["dim_signals"].values())


def _seed_affinity_rows(db, rows):
    """Insert derived pattern_affinity_history rows directly via SQL."""
    from storage import migration
    conn = sqlite3.connect(str(db))
    try:
        migration.schema_bootstrap(conn)
        for r in rows:
            conn.execute(
                "INSERT INTO pattern_affinity_history (ts, top_pattern, "
                "top_prompt_affinity, top_agent_affinity, top_margin, "
                "top_amplification, evidence, confidence, repeated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", r)
        conn.commit()
    finally:
        conn.close()


def _fallback_shape(agent_env, monkeypatch):
    _eng, db = agent_env
    api = _api(db)
    monkeypatch.setattr(api, "_portrait_live_view", lambda: None)
    monkeypatch.setattr(api, "_engine_first", lambda *a, **k: (None, None))
    return api, api._portrait_shape()


def test_portrait_fallback_perception_from_latest_row(agent_env, monkeypatch):
    eng, db = agent_env
    now = time.time()
    eng.on_events([{"type": "turn", "ts": now, "user_message": _PROMPT_A}])
    _seed_affinity_rows(db, [
        (now - 3, "exploratory", 0.10, 0.20, 0.05, 0.10, 1, 0.5, 0),
        (now - 2, "focused", 0.30, 0.70, 0.20, 0.40, 2, 0.6, 0),
        (now - 1, "focused", 0.42, 0.81, 0.27, 0.39, 3, 0.75, 0),
    ])
    _api_obj, shape = _fallback_shape(agent_env, monkeypatch)
    # agent track STILL None in fallback (extended, not broken)
    assert all(v is None for v in shape["agent"].values())
    perc = shape["perception"]
    assert perc["perception_available"] is True
    # E1: latest row PER top_pattern — focused surfaces from its own
    # latest row (repeated: it also won at now-2, within its 3-prior
    # window), exploratory from its own latest row (no prior window ->
    # not repeated). Order = per-name latest ts desc.
    assert perc["patterns"] == [
        {"name": "focused", "prompt_affinity": 0.42,
         "agent_affinity": 0.81, "margin": 0.27, "amplification": 0.39,
         "evidence": 3, "confidence": 0.75, "repeated": True},
        {"name": "exploratory", "prompt_affinity": 0.10,
         "agent_affinity": 0.20, "margin": 0.05, "amplification": 0.10,
         "evidence": 1, "confidence": 0.5, "repeated": False}]
    assert all(s == {"prompt": None, "agent": None}
               for s in perc["dim_signals"].values())


def test_portrait_fallback_perception_not_repeated(agent_env, monkeypatch):
    eng, db = agent_env
    now = time.time()
    eng.on_events([{"type": "turn", "ts": now, "user_message": _PROMPT_A}])
    _seed_affinity_rows(db, [
        (now - 2, "exploratory", 0.10, 0.20, 0.05, 0.10, 1, 0.5, 0),
        (now - 1, "focused", 0.42, 0.81, 0.27, 0.39, 3, 0.75, 0),
    ])
    _api_obj, shape = _fallback_shape(agent_env, monkeypatch)
    assert shape["perception"]["patterns"][0]["repeated"] is False
    assert shape["perception"]["perception_available"] is True


def test_portrait_fallback_perception_empty_table(agent_env, monkeypatch):
    eng, db = agent_env
    now = time.time()
    eng.on_events([{"type": "turn", "ts": now, "user_message": _PROMPT_A}])
    _api_obj, shape = _fallback_shape(agent_env, monkeypatch)
    assert shape["perception"] == _empty_perception_expected(
        list(shape["dimensions"].keys()))


def test_portrait_live_perception_passthrough(agent_env, monkeypatch):
    eng, db = agent_env
    now = time.time()
    eng.on_events([{"type": "turn", "ts": now, "user_message": _PROMPT_A}])
    api = _api(db)
    perc = {"patterns": [{"name": "focused", "prompt_affinity": 0.1,
                          "agent_affinity": 0.9, "margin": 0.3,
                          "amplification": 0.8, "evidence": 5,
                          "confidence": 0.9, "repeated": False}],
            "dim_signals": {d: {"prompt": 0.1, "agent": 0.2}
                            for d in api._dimensions()},
            "perception_available": True}
    monkeypatch.setattr(api, "_portrait_live_view", lambda: {
        "available": True,
        "dimensions": {d: 0.5 for d in api._dimensions()},
        "confidence": {d: None for d in api._dimensions()},
        "user": {d: 0.5 for d in api._dimensions()},
        "agent": {d: None for d in api._dimensions()},
        "perception": perc})
    import asyncio
    body = asyncio.run(api.portrait())
    assert body["perception"] == perc  # passed through unchanged


def test_portrait_live_missing_perception_gets_empty(agent_env, monkeypatch):
    eng, db = agent_env
    now = time.time()
    eng.on_events([{"type": "turn", "ts": now, "user_message": _PROMPT_A}])
    api = _api(db)
    monkeypatch.setattr(api, "_portrait_live_view", lambda: {
        "available": True,
        "dimensions": {d: 0.5 for d in api._dimensions()},
        "confidence": {d: None for d in api._dimensions()},
        "user": {d: 0.5 for d in api._dimensions()},
        "agent": {d: None for d in api._dimensions()}})
    import asyncio
    body = asyncio.run(api.portrait())
    assert body["perception"] == _empty_perception_expected(
        list(api._dimensions()))
