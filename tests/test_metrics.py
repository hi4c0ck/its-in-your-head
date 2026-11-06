import os
import tempfile


def test_schema_and_store(tmp_path, monkeypatch):
    """Run the worker's storage path against a temp HERMES_HOME plugin-data root."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib
    import metrics
    importlib.reload(metrics)
    import plugins.plugin_storage as ps
    importlib.reload(ps) if hasattr(ps, "__spec__") else None
    from plugins.plugin_storage import plugin_db
    conn = plugin_db("its-in-your-head")
    metrics.ensure_schema(conn)
    from embedder import sketch
    ev = {"type": "turn.start", "session_id": "s1", "user_message": "hi", "ts": 1.0,
          "embedding": sketch("hi")}
    conn.execute(
        """INSERT INTO events (ts, type, session_id, user_message, embedding)
           VALUES (?,?,?,?,?)""", (1.0, "turn.start", "s1", "hi", ev["embedding"]),
    )
    conn.commit()
    n, = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    assert n == 1


def test_plugin_api_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import importlib
    import dashboard.plugin_api as api
    importlib.reload(api)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert set(r.json()) == {"events", "total_tokens", "last_event"}
    assert client.get("/heatmap").status_code == 200
    assert client.get("/recent").status_code == 200
