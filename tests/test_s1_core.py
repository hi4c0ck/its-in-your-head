"""S1 core-telemetry tests: simulated turns into the pipeline.

Covers: register() wires the C5 matrix without opening the DB,
simulated hook calls produce the right rows/state in a tmp DB,
LIVE counter increments synchronously on pre_tool_call,
enqueue p99 < 1 ms on hot hooks, reasoning capture fires once per turn.
"""
import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

NINE_HOOKS = {
    "pre_llm_call", "post_api_request", "api_request_error",
    "pre_tool_call", "post_tool_call",
    "on_stream_delta", "on_stream_end",
    "on_session_start", "on_session_end",
}


def _load_plugin():
    p = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location("its_in_your_head_plugin_p", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.hooks = {}
        self._config = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def get_config(self, key, default=None):
        return self._config.get(key, default)


@pytest.fixture(autouse=True)
def _fresh_pipeline(tmp_path):
    """Point the pipeline at a tmp DB, reset in-memory state, and drain."""
    import metrics
    from core import collector
    collector.STATE.reset()
    collector.set_ctx(FakeCtx())
    metrics.set_db_path(tmp_path / "s1.db")
    metrics.start_worker()
    yield tmp_path / "s1.db"
    metrics.stop_worker()
    while not metrics._queue.empty():
        try:
            metrics._queue.get_nowait()
        except Exception:
            break


def _wait_rows(db, n, timeout=10.0):
    deadline = time.time() + timeout
    rows = []
    while time.time() < deadline:
        if db.exists():
            try:
                with sqlite3.connect(db) as c:
                    rows = c.execute("SELECT type FROM events ORDER BY id").fetchall()
            except sqlite3.OperationalError:
                rows = []
            if len(rows) >= n:
                break
        time.sleep(0.05)
    return [r[0] for r in rows]


def _query(db, sql, params=()):
    with sqlite3.connect(db) as c:
        return c.execute(sql, params).fetchall()


def test_register_wires_matrix_without_opening_db(tmp_path):
    """register() must wire all 9 C5 hooks; the DB file must NOT be
    created at register() time (lazy-DB locked rule)."""
    import metrics
    mod = _load_plugin()
    metrics.set_db_path(tmp_path / "never.db")
    ctx = FakeCtx()
    mod.register(ctx)
    assert set(ctx.hooks) == NINE_HOOKS
    # worker is running, but no event was ever enqueued -> no DB file
    time.sleep(0.6)
    assert not (tmp_path / "never.db").exists()


def test_simulated_turn_produces_rows(_fresh_pipeline):
    """One simulated turn: session start, prompt, reasoning deltas,
    a tool call, stream end, api metrics -> the expected rows with
    metadata only (no raw reasoning / tool output persisted)."""
    from core import collector as col
    db = _fresh_pipeline
    col._on_session_start(session_id="s1")
    col._on_pre_llm_call(session_id="s1", turn_id="t1", model="m",
                         user_message="hello", is_first_turn=True)
    for chunk in ("think ", "more ", "done"):
        col._on_stream_delta(delta=chunk, kind="reasoning",
                             session_id="s1", turn_id="t1")
    col._on_stream_delta(delta="answer", kind="text", session_id="s1", turn_id="t1")
    col._on_pre_tool_call(tool_name="terminal", args={"cmd": "ls"},
                          task_id="task1", session_id="s1", turn_id="t1")
    col._on_post_tool_call(tool_name="terminal", args={"cmd": "ls"},
                           result={"ok": True, "n": 3}, task_id="task1",
                           duration_ms=12.5, session_id="s1", turn_id="t1")
    col._on_stream_end(final_text="final answer", finished=True,
                       session_id="s1", turn_id="t1")
    col._on_post_api_request(session_id="s1", turn_id="t1", model="m",
                             provider="p", api_duration=0.5, finish_reason="stop",
                             usage={"prompt_tokens": 8, "completion_tokens": 4,
                                    "total_tokens": 12})
    col._on_session_end(session_id="s1")

    types = _wait_rows(db, 9)
    for expected in ("session_start", "turn", "reasoning_seg", "tool_start",
                     "tool_end", "stream_end", "api", "session_end"):
        assert expected in types, f"missing row {expected}: {types}"

    tool_end = _query(db, "SELECT tool_name, status, duration_ms, result_type "
                          "FROM events WHERE type='tool_end'")[0]
    assert tool_end == ("terminal", "ok", 12.5, "dict:n,ok")

    seg = _query(db, "SELECT reasoning_chars, reasoning_deltas, sketch "
                     "FROM events WHERE type='reasoning_seg'")[0]
    assert seg[0] == len("think more done") and seg[1] == 3
    assert seg[2] is not None  # worker-side sketch of the segment

    # C6: raw reasoning text and raw tool output never persisted.
    persisted = str(_query(db, "SELECT * FROM events"))
    assert "think more" not in persisted
    assert "final answer" not in persisted


def test_live_counter_increments_synchronously(_fresh_pipeline):
    """pre_tool_call bumps the LIVE counter immediately (before the
    worker persists anything) — the synchronous half of the C5 rule."""
    from core import collector as col
    assert col.STATE.counter("tool_calls_started") == 0
    col._on_pre_tool_call(tool_name="terminal", args={}, task_id="t",
                          session_id="s", turn_id="u")
    assert col.STATE.counter("tool_calls_started") == 1
    col._on_post_tool_call(tool_name="terminal", result="done",
                           duration_ms=1.0, session_id="s", turn_id="u")
    assert col.STATE.counter("tool_calls_completed") == 1


def test_reasoning_capture_fires_once_per_turn(_fresh_pipeline):
    """Many reasoning deltas -> exactly one reasoning_seg per turn;
    a second turn may capture its own."""
    from core import collector as col
    for i in range(10):
        col._on_stream_delta(delta=f"chunk{i}", kind="reasoning",
                             session_id="s", turn_id="t1")
    col._on_stream_end(final_text="", finished=True, session_id="s", turn_id="t1")
    for i in range(5):
        col._on_stream_delta(delta=f"x{i}", kind="reasoning",
                             session_id="s", turn_id="t2")
    col._on_stream_end(final_text="", finished=True, session_id="s", turn_id="t2")

    _wait_rows(_fresh_pipeline, 2)
    segs = _query(_fresh_pipeline,
                  "SELECT turn_id, reasoning_deltas FROM events "
                  "WHERE type='reasoning_seg' ORDER BY id")
    assert [t for t, _ in segs] == ["t1", "t2"]
    assert segs[0][1] == 10 and segs[1][1] == 5


def test_hot_hook_enqueue_p99_under_1ms():
    """pre_tool_call / on_stream_delta enqueue path: p99 < 1 ms."""
    from core import collector as col
    lat = []
    for i in range(300):
        t0 = time.perf_counter()
        col._on_pre_tool_call(tool_name="terminal", args={},
                              session_id="s", turn_id=f"t{i % 3}")
        lat.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        col._on_stream_delta(delta="r", kind="reasoning",
                             session_id="s", turn_id=f"t{i % 3}")
        lat.append(time.perf_counter() - t0)
    lat.sort()
    p99 = lat[int(len(lat) * 0.99) - 1]
    assert p99 < 1e-3, f"p99 enqueue latency {p99 * 1e3:.3f} ms >= 1 ms"


def test_events_validation_and_settings_defaults():
    from core import events, settings
    assert events.validate({"type": "tool_start", "tool_name": "x"})
    assert not events.validate({"type": "tool_start"})          # missing key
    assert not events.validate({"type": "bogus"})
    assert not events.validate("not a dict")
    snap = settings.load(FakeCtx())  # ctx without config -> defaults
    assert snap["adaptation.enabled"] is False  # C4 default
    assert snap["observation.reasoning"] is True


def test_tool_error_status_counts(_fresh_pipeline):
    from core import collector as col
    col._on_pre_tool_call(tool_name="browser", args={}, session_id="s", turn_id="t")
    col._on_post_tool_call(tool_name="browser", result={"error": "boom"},
                           duration_ms=2.0, session_id="s", turn_id="t")
    assert col.STATE.counter("tool_errors") == 1
    rows = _wait_rows(_fresh_pipeline, 2)
    assert "tool_end" in rows
    assert _query(_fresh_pipeline,
                  "SELECT status FROM events WHERE type='tool_end'") == [("error",)]
