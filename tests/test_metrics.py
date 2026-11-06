"""OWNER tests: pipeline as slots land.

Green at hand-off: assert that the owner surface exists and raises
NotImplementedError, so the baseline proves the structure without
asserting on un-written behavior.
"""
import importlib
import math
import struct


def test_enqueue_accepts_events():
    import metrics
    importlib.reload(metrics)
    before = metrics._queue.qsize()
    metrics.enqueue({"type": "turn.start", "ts": 0.0})
    assert metrics._queue.qsize() == before + 1
    metrics._queue.get_nowait()


def test_sketch_shape_and_packing():
    import embedder
    importlib.reload(embedder)
    blob = embedder.sketch("hello world")
    assert len(blob) == embedder.DIM * 4, "packed DIM float32s expected"
    vals = struct.unpack(f"<{embedder.DIM}f", blob)
    assert all(math.isfinite(v) for v in vals)


def test_sketch_deterministic_and_casefolded():
    import embedder
    importlib.reload(embedder)
    assert embedder.sketch("same prompt") == embedder.sketch("same prompt")
    assert embedder.sketch("ABC") == embedder.sketch("abc")
    assert embedder.sketch("a bug in the parser") != embedder.sketch("a feature in the parser")


def test_sketch_l2_normalized():
    import embedder
    importlib.reload(embedder)
    import struct as _s
    vals = _s.unpack(f"<{embedder.DIM}f", embedder.sketch("normalize me"))
    norm = math.sqrt(sum(v * v for v in vals))
    assert abs(norm - 1.0) < 1e-4


def test_sketch_empty_is_zero_vector():
    import embedder
    importlib.reload(embedder)
    import struct as _s
    for text in ("", None):
        vals = _s.unpack(f"<{embedder.DIM}f", embedder.sketch(text))
        assert all(v == 0.0 for v in vals)


def test_start_worker_is_idempotent(tmp_path):
    import threading
    import metrics
    importlib.reload(metrics)
    metrics.set_db_path(tmp_path / "data.db")
    metrics.start_worker()
    metrics.start_worker()  # second call must be a no-op
    assert len([t for t in threading.enumerate()
                if t.name == "its-in-your-head-metrics" and t.is_alive()]) == 1
    metrics.stop_worker()


def test_worker_persists_events(tmp_path):
    import sqlite3
    import time
    import embedder
    importlib.reload(embedder)
    import metrics
    importlib.reload(metrics)
    metrics.set_db_path(tmp_path / "data.db")
    metrics.start_worker()
    ts = time.time()
    metrics.enqueue({
        "type": "turn", "ts": ts, "session_id": "s1", "turn_id": "t1",
        "model": "agnes-2.5-flash", "platform": "desktop", "is_first_turn": True,
        "user_message": "hello world", "user_chars": 11,
    })
    metrics.enqueue({
        "type": "api", "ts": ts + 1, "session_id": "s1", "turn_id": "t1",
        "api_request_id": "a1", "model": "agnes-2.5-flash", "provider": "custom",
        "api_duration": 0.84, "finish_reason": "stop",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })
    metrics.enqueue({
        "type": "error", "ts": ts + 2, "session_id": "s1", "turn_id": "t1",
        "api_request_id": "a2", "status_code": 503, "retry_count": 1,
        "retryable": True, "reason": "upstream",
        "error": {"type": "http", "message": "boom"},
    })
    db = tmp_path / "data.db"
    n = 0
    deadline = time.time() + 10
    with sqlite3.connect(db) as c:
        while time.time() < deadline:
            try:
                n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            except sqlite3.OperationalError:  # table not created yet
                n = 0
            if n == 3:
                break
            time.sleep(0.1)
    assert n == 3, "worker should have persisted all three event types"
    metrics.stop_worker()
    with sqlite3.connect(db) as c:
        turn = c.execute(
            "SELECT sketch, user_chars, user_message FROM events WHERE type='turn'"
        ).fetchone()
        assert len(turn[0]) == embedder.DIM * 4, "turn rows carry the sketch blob"
        assert turn[1] == 11 and turn[2] == "hello world"
        api = c.execute(
            "SELECT total_tokens, finish_reason, api_duration FROM events WHERE type='api'"
        ).fetchone()
        assert api == (15, "stop", 0.84)
        err = c.execute(
            "SELECT status_code, retryable, error_type, error_message "
            "FROM events WHERE type='error'"
        ).fetchone()
        assert err == (503, 1, "http", "boom")


def test_lazy_db_never_opened_at_import(tmp_path):
    """The lazy-DB rule, made testable: importing/reloading metrics and
    calling start_worker() must not create a DB file — only a persisted
    event may."""
    import metrics
    importlib.reload(metrics)
    metrics.set_db_path(tmp_path / "never.db")
    assert not (tmp_path / "never.db").exists()
    metrics.start_worker()
    metrics.stop_worker()
    assert not (tmp_path / "never.db").exists()
