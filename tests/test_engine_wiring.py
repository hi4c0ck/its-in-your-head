"""Engine wiring tests (parent, PG2 merge): pulse + portrait + drift live
data through process-level engine facades, plus hot-path invariants.

Pattern follows tests/test_s1_core.py: tmp DB via metrics.set_db_path,
fresh Engine instances (the module singleton is exercised only where
noted), no network, no I/O at import.
"""
import time

import pytest

import metrics


class _StubBackend:
    """encode() must never be reachable from the hot path."""

    name = "stub"
    model_version = "stub-0"

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        raise AssertionError("encode() called on a path that must not embed")

    @property
    def dimension(self):
        return 4


@pytest.fixture(autouse=True)
def _fresh_engine(tmp_path, monkeypatch):
    """Point the lazy DB at tmp, isolate the engine module singleton."""
    db = tmp_path / "engine.db"
    metrics.set_db_path(db)
    import engine
    monkeypatch.setattr(engine, "_engine", None)
    yield db
    try:
        metrics.stop_worker()
    except Exception:
        pass


def _eng(tmp_path=None):
    import engine
    return engine.Engine()


def _user_turn(text="Fix the login page", ts=None):
    return {"type": "turn", "ts": ts or time.time(),
            "session_id": "s1", "turn_id": "t1",
            "user_message": text, "user_chars": len(text)}


def _tool_end(tool="read_file", status="ok", ts=None, **kw):
    ev = {"type": "tool_end", "ts": ts or time.time(),
          "tool_name": tool, "status": status, "duration_ms": 12,
          "session_id": "s1", "turn_id": "t1"}
    ev.update(kw)
    return ev


def test_pulse_view_rows_after_tool_events(_fresh_engine):
    eng = _eng()
    eng.on_events([_user_turn(), _tool_end(tool="read_file"),
                   _tool_end(tool="terminal", status="error")])
    view = eng.pulse_view()
    tools = {r["tool_name"] for r in view["rows"]}
    assert tools == {"read_file", "terminal"}
    assert view["available"] is True
    assert view["columns"] == ["goal", "good", "friction", "unlabeled"]
    for row in view["rows"]:
        for cell in ("goal", "good", "friction"):
            assert "value" in row[cell] and "level" in row[cell]


def test_pulse_view_empty_before_events(_fresh_engine):
    eng = _eng()
    view = eng.pulse_view()
    assert view["rows"] == [] and view["available"] is False


def test_portrait_view_populated_after_user_turn(_fresh_engine):
    eng = _eng()
    eng.on_events([_user_turn("Rewrite the auth module to be async")])
    view = eng.portrait_view()
    assert view["available"] is True
    assert len(view["dimensions"]) == 6
    assert all(v is not None for v in view["dimensions"].values())
    assert len(view["confidence"]) == 6
    assert set(view["user"].keys()) == set(view["dimensions"].keys())
    assert set(view["agent"].keys()) == set(view["dimensions"].keys())


def test_drift_series_grow_with_turns(_fresh_engine):
    eng = _eng()
    for i in range(3):
        eng.on_events([_user_turn(f"prompt {i}")])
    view = eng.drift_view()
    assert view["available"] is True
    for dim, series in view["series"].items():
        assert len(series) == 3, dim
        assert all(0.0 <= v <= 1.0 for v in series)


def test_reset_clears_process_view(_fresh_engine):
    eng = _eng()
    eng.on_events([_user_turn(), _tool_end()])
    eng.reset()
    assert eng.drift_view()["available"] is False
    assert eng.goal_text is None
    p = eng.portrait_view()
    # empty-shape contract after reset: no evidence, None values
    assert p["available"] is False
    assert all(v is None for v in p["dimensions"].values())


def test_on_tool_start_never_encodes(_fresh_engine):
    eng = _eng()
    stub = _StubBackend()
    eng._backend = stub
    eng._backend_name = "stub"
    row = eng.on_tool_start(_tool_end(tool="search_files").copy())
    assert row["tool_name"] == "search_files"
    assert stub.calls == 0  # hot path stayed counter-only (C5)


def test_engine_error_isolation_in_on_events(_fresh_engine):
    eng = _eng()

    class _BoomBackend:
        name = "boom"
        model_version = "boom-0"

        def encode(self, texts):
            raise RuntimeError("backend down")

    eng._backend = _BoomBackend()
    # Must not raise out of on_events (worker must survive engine faults)
    eng.on_events([_user_turn(), _tool_end()])


def test_worker_feeds_engine_module(_fresh_engine):
    """The metrics worker's engine hook must point at the engine module."""
    assert metrics._engine is not None
    import engine
    assert metrics._engine is engine


def test_route_shapes_via_facades(tmp_path, monkeypatch):
    """Dashboard routes serve the engine facades when the engine module
    is importable (fresh singleton + data), with S5 shapes as fallback."""
    import importlib
    import dashboard.plugin_api as api
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # fresh engine, pointed at the tmp DB, wired as the module singleton
    import engine
    eng = engine.Engine()
    monkeypatch.setattr(engine, "_engine", eng)
    eng.on_events([_user_turn(), _tool_end(tool="read_file")])

    api._ephemeral_settings.clear()
    api.set_db_path(tmp_path / "data.db")
    api.set_ctx(None)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    r = client.get("/pulse")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert {row["tool_name"] for row in body["rows"]} == {"read_file"}

    p = client.get("/portrait").json()
    assert p["available"] is True and len(p["dimensions"]) == 6

    d = client.get("/drift").json()
    assert d["available"] is True and len(d["series"]) == 6
