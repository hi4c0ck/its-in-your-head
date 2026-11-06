"""OWNER tests: backend routes against a seeded tmp DB.

Uses plugin_api.set_db_path (test-only override) so the routes never touch
the real plugin DB. The infra baseline (tests/test_structure.py) stays
green unmodified: its shape assertions pass on an empty, freshly bootstrapped
database.
"""
import importlib
import sqlite3
import time

import dashboard.plugin_api as api
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path, seed=False):
    importlib.reload(api)
    api.set_db_path(tmp_path / "data.db")
    db = sqlite3.connect(tmp_path / "data.db")
    db.executescript(api._DDL)
    if seed:
        now = time.time()

        def ins(ts_offset, etype, total_tokens=None, status_code=None,
                finish_reason=None, model=None):
            db.execute(
                "INSERT INTO events (ts, type, total_tokens, status_code, "
                "finish_reason, model) VALUES (?,?,?,?,?,?)",
                (now + ts_offset, etype, total_tokens, status_code,
                 finish_reason, model))
        ins(-3600, "turn", model="agnes-2.5-flash")       # hour bucket 1
        ins(-120, "api", total_tokens=100, finish_reason="stop")
        ins(-60, "error", status_code=503)
        ins(-10, "api", total_tokens=200, finish_reason="tool_calls")
        db.commit()
    db.close()
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_metrics_empty_db_shape(tmp_path):
    c = _client(tmp_path, seed=False)
    r = c.get("/metrics")
    assert r.status_code == 200
    assert r.json() == {"events": 0, "total_tokens": 0, "last_event": None}
    assert c.get("/recent").json() == []
    heat = c.get("/heatmap?window=7d").json()
    assert heat["hours"] == 168 and heat["cells"] == {}


def test_metrics_aggregates(tmp_path):
    c = _client(tmp_path, seed=True)
    m = c.get("/metrics").json()
    assert m["events"] == 4
    assert m["total_tokens"] == 300
    assert m["last_event"] is not None


def test_heatmap_hour_buckets(tmp_path):
    c = _client(tmp_path, seed=True)
    heat = c.get("/heatmap?window=24h").json()
    assert heat["hours"] == 24
    cells = heat["cells"]
    assert cells["0"] == {"turns": 0, "calls": 2, "errors": 1}  # current hour
    assert cells["1"] == {"turns": 1, "calls": 0, "errors": 0}  # hour -1
    assert len(cells) == 2


def test_recent_newest_first_and_shape(tmp_path):
    c = _client(tmp_path, seed=True)
    rows = c.get("/recent?limit=3").json()
    assert len(rows) == 3
    assert rows[0]["type"] == "api" and rows[0]["total_tokens"] == 200
    assert rows[1]["type"] == "error" and rows[1]["status_code"] == 503
    assert rows[2]["type"] == "api" and rows[2]["total_tokens"] == 100
    for row in rows:
        assert set(row) == {"type", "ts", "model", "total_tokens",
                            "finish_reason", "status_code"}


def test_unknown_window_falls_back_to_24h(tmp_path):
    c = _client(tmp_path, seed=False)
    assert c.get("/heatmap?window=bogus").json()["hours"] == 24
