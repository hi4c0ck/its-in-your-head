"""F4: flush() in-flight barrier — determinism tests (no wall-clock sleeps
in assertions; barriers/events + bounded waits with generous timeouts only).

Background: flush() used to treat an EMPTY QUEUE as fully drained, but the
worker may have already dequeued a batch and still be persisting it.
The barrier now waits for queue-empty AND in_flight == 0, and reports
honest pending/failed accounting via metrics.FlushResult.
"""
import importlib
import sqlite3
import threading

JOIN = 20  # generous bound; real completions are ~instant


def _fresh(tmp_path):
    import metrics
    importlib.reload(metrics)
    db = tmp_path / "data.db"
    metrics.set_db_path(db)
    return metrics, db


def _count(db):
    if not db.exists():
        return 0
    try:
        with sqlite3.connect(str(db)) as c:
            try:
                return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            except sqlite3.OperationalError:  # table not created yet
                return 0
    except sqlite3.OperationalError:
        return 0


def _ev(i=0):
    return {"type": "turn", "ts": 1.0, "session_id": "s",
            "turn_id": f"t{i}", "user_message": "hi", "user_chars": 2}


class _Gate:
    """Patch _persist_batch to pause mid-persist until released."""

    def __init__(self, metrics):
        self.entered = threading.Event()
        self.release = threading.Event()
        orig = metrics._persist_batch

        def gated(batch, _orig=orig, _e=self.entered, _r=self.release):
            _e.set()
            _r.wait(timeout=30)
            return _orig(batch)

        metrics._persist_batch = gated


def _start_flush(metrics, timeout=JOIN):
    box, done = {}, threading.Event()

    def target():
        box["r"] = metrics.flush(timeout=timeout)
        done.set()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box, done


def test_flush_enqueue_during_flush_none_lost(tmp_path):
    """(a) N events enqueued while flush is running; none lost."""
    metrics, db = _fresh(tmp_path)
    gate = _Gate(metrics)
    metrics.start_worker()
    try:
        metrics.enqueue(_ev(0))  # seed batch that blocks in persist
        assert gate.entered.wait(10), "worker never entered persist"
        t, box, done = _start_flush(metrics)
        n = 25
        for i in range(1, n + 1):  # enqueued while flush is running
            metrics.enqueue(_ev(i))
        gate.release.set()
        t.join(JOIN)
        assert not t.is_alive() and done.is_set(), "flush hung"
        r = box["r"]
        total = n + 1
        committed = _count(db)
        assert committed + r.pending == total  # none lost
        assert r.drained and r.pending == 0
        assert committed == total
    finally:
        gate.release.set()
        metrics.stop_worker()


def test_flush_waits_for_in_flight_batch(tmp_path):
    """(b) flush returns only after the dequeued-but-uncommitted batch
    commits (it must not return early while persist is gated)."""
    metrics, db = _fresh(tmp_path)
    gate = _Gate(metrics)
    metrics.start_worker()
    try:
        metrics.enqueue(_ev(0))
        assert gate.entered.wait(10), "worker never entered persist"
        with metrics._state_cv:
            assert metrics._in_flight_events == 1  # visible mid-persist
        t, box, done = _start_flush(metrics)
        # Gate closed => commit impossible => flush cannot have returned
        # (in_flight > 0) and the thread must still be alive-or-pending.
        assert not done.is_set(), "flush returned early with in-flight work"
        assert t.is_alive(), "flush thread died unexpectedly"
        gate.release.set()
        t.join(JOIN)
        assert not t.is_alive() and done.is_set(), "flush hung"
        r = box["r"]
        assert r.drained and r.pending == 0 and bool(r) is True
        assert _count(db) == 1
    finally:
        gate.release.set()
        metrics.stop_worker()


def test_flush_failed_persist_honest_accounting(tmp_path, caplog):
    """(c) failed persist: reported via the error path, NOT retried, and
    flush accounting is honest (drained False, failed counted)."""
    import logging
    metrics, db = _fresh(tmp_path)

    def boom(batch):
        raise RuntimeError("boom")

    metrics._persist_batch = boom
    n = 3
    for i in range(n):
        metrics.enqueue(_ev(i))  # before start: single batch, all fail
    metrics.start_worker()
    try:
        with caplog.at_level(logging.ERROR, logger="metrics"):
            r = metrics.flush(timeout=10)
        assert r.pending == 0
        assert r.failed == n
        assert r.drained is False and not r
        committed = _count(db)
        assert committed == 0
        assert committed + r.pending + r.failed == n
        assert any("batch persist failed" in rec.getMessage()
                   for rec in caplog.records), "failure not reported via error path"
    finally:
        metrics.stop_worker()


def test_stop_worker_releases_flush_barrier(tmp_path):
    """(d) worker stop releases a waiting flush (no hang); the blocked
    in-flight batch is reported pending, not claimed drained."""
    metrics, db = _fresh(tmp_path)
    gate = _Gate(metrics)
    metrics.start_worker()
    try:
        metrics.enqueue(_ev(0))
        assert gate.entered.wait(10), "worker never entered persist"
        t, box, done = _start_flush(metrics, timeout=30)
        metrics.stop_worker(timeout=1)  # join times out; worker still gated
        t.join(JOIN)
        assert not t.is_alive() and done.is_set(), "flush hung after stop"
        r = box["r"]
        assert r.drained is False
        assert r.in_flight == 1 and r.pending == 1
        assert _count(db) == 0  # gated batch never committed
        # After the gate opens the (stopped) worker commits its last batch
        # and exits. Wait for that commit (bounded poll of the DB, like
        # test_worker_persists_events), then a later flush sees a
        # genuinely drained queue.
        gate.release.set()
        metrics.stop_worker(timeout=3)
        deadline = 10
        while _count(db) != 1 and deadline > 0:
            import time as _time
            _time.sleep(0.05)
            deadline -= 0.05
        assert _count(db) == 1, "worker never committed its in-flight batch"
        r2 = metrics.flush(timeout=5)
        assert r2.drained and r2.pending == 0
        assert _count(db) == 1
    finally:
        gate.release.set()
        metrics.stop_worker()


def test_flush_timeout_reports_pending(tmp_path):
    """Timeout path: flush returns drained=False with real pending counts
    instead of claiming drained while work is uncommitted."""
    metrics, db = _fresh(tmp_path)
    gate = _Gate(metrics)
    metrics.start_worker()
    try:
        metrics.enqueue(_ev(0))
        metrics.enqueue(_ev(1))
        assert gate.entered.wait(10), "worker never entered persist"
        r = metrics.flush(timeout=1.0)  # batch gated: must time out honestly
        assert r.drained is False
        assert r.pending == 2 and r.in_flight == 2 and r.queued == 0
        assert r.failed == 0
        gate.release.set()
        r2 = metrics.flush(timeout=10)
        assert r2.drained and r2.pending == 0 and r2.failed == 0
        assert _count(db) == 2
    finally:
        gate.release.set()
        metrics.stop_worker()
