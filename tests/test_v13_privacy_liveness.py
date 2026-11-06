"""v0.1.3 P0 + F4 regression: raw prompt text never reaches SQLite; the
worker's idle wait releases the flush-barrier condition (stop/flush
responsiveness). No wall-clock sleeps in assertions — bounded joins only.
"""
import importlib
import sqlite3
import time


def _fresh(tmp_path):
    import metrics
    importlib.reload(metrics)
    db = tmp_path / "data.db"
    metrics.set_db_path(db)
    return metrics, db


def _all_text_values(db):
    """Every value in every column of every table — for the privacy scan."""
    with sqlite3.connect(str(db)) as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        vals = []
        for t in tables:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
            for row in c.execute(f"SELECT * FROM {t}"):
                for col, v in zip(cols, row):
                    if v is not None:
                        vals.append((t, col, v))
    return vals


def test_privacy_raw_prompt_never_persisted(tmp_path):
    """Flush with a sentinel raw prompt; the DB must not contain it in ANY
    table/column, while the derived features (user_chars, sketch) ARE there."""
    metrics, db = _fresh(tmp_path)
    sentinel = "SENTINEL-RAW-PROMPT-7f3a"
    metrics.start_worker()
    try:
        metrics.enqueue({"type": "turn", "ts": 1.0, "session_id": "s",
                        "turn_id": "t1", "model": "m",
                        "user_message": sentinel, "user_chars": len(sentinel)})
        r = metrics.flush(timeout=20)
        assert r.drained, f"flush not drained: {r}"
        assert _count_rows(db) >= 1
        # The sentinel must be absent from every value in every table.
        for table, col, v in _all_text_values(db):
            hay = v if isinstance(v, str) else (
                v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v))
            assert sentinel not in hay, (
                f"raw prompt text persisted in {table}.{col}")
        # Derived features survived the purge.
        with sqlite3.connect(str(db)) as c:
            row = c.execute(
                "SELECT user_chars, user_message, sketch FROM events "
                "WHERE turn_id='t1'").fetchone()
        assert row is not None
        user_chars, user_message, sketch = row
        assert user_chars == len(sentinel)
        assert user_message is None  # column kept, written NULL
        assert sketch is not None    # derived sketch preserved
    finally:
        metrics.stop_worker()


def _count_rows(db):
    if not db.exists():
        return 0
    try:
        with sqlite3.connect(str(db)) as c:
            try:
                return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            except sqlite3.OperationalError:
                return 0
    except sqlite3.OperationalError:
        return 0


def test_worker_stop_releases_barrier_promptly(tmp_path):
    """Liveness regression (the 3a024bb CI failure class): an IDLE worker
    must not hold the flush-barrier condition while waiting for work.
    stop_worker() notifies the condition; the worker's cv.wait wakes and
    exits promptly — a bounded join of 0.25s must succeed (a worker stuck
    in a blocking 0.5s queue get would still be alive at that point)."""
    metrics, db = _fresh(tmp_path)
    metrics.start_worker()
    try:
        thread = metrics._worker_thread
        assert thread is not None
        metrics.stop_worker(timeout=0.25)
        thread.join(timeout=0.25)
        assert not thread.is_alive(), (
            "idle worker still holding the barrier after stop "
            "(blocking wait under the condition)")
    finally:
        metrics.stop_worker()
        metrics._items.clear()


def test_flush_prompt_while_worker_idle(tmp_path):
    """While the worker sits in its idle cv.wait, flush() acquires the
    condition freely and returns drained (no 0.5s stall per poll slice)."""
    metrics, db = _fresh(tmp_path)
    metrics.start_worker()
    try:
        t0 = time.monotonic()
        r = metrics.flush(timeout=2.0)
        assert r.drained and r.pending == 0, f"idle flush not drained: {r}"
        assert time.monotonic() - t0 < 1.0  # returns promptly, not on a stall
    finally:
        metrics.stop_worker()
        metrics._items.clear()
