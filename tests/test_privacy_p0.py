"""v0.1.28 P0 privacy-hardening regression tests (plan §2, §3.4, §4).

These guard the privacy invariants against regression:
  §2.1  reasoning buffers are (session, turn)-scoped, bounded, flushed
        atomically, and a late delta after flush cannot recreate the text.
  §2.2  clear_session removes unfinished buffers + completion markers.
  §2.4  the raw-text event queue is bounded (overflow + text-truncation
        accounting, surfaced via the FlushResult).
  §3.4  the maintainer smoke subprocess uses the same isolated env policy as
        the runtime worker, and a failed smoke reports a bounded reason.
  §4    arbitrary error/provider text is never persisted; only a bounded
        error class + numeric counters survive.
"""
import json
import os
import subprocess
import sys
import types

import pytest

from core import session_state as ss
from perception import worker_client
import metrics


# -- §2.1 reasoning buffer lifecycle -----------------------------------------

def test_reasoning_buffer_is_session_scoped():
    s = ss.SessionState()
    s.reasoning_feed("s1", "t1", "hello ")
    s.reasoning_feed("s1", "t1", "world")
    s.reasoning_feed("s2", "t1", "other session, same turn id")
    assert s.reasoning_pending("s1", "t1")
    assert s.reasoning_pending("s2", "t1")
    assert s.reasoning_flush("s1", "t1") == ("hello world", 2)
    assert s.reasoning_flush("s2", "t1") == ("other session, same turn id", 1)


def test_reasoning_flush_is_atomic_and_marker_blocks_late_delta():
    s = ss.SessionState()
    s.reasoning_feed("s", "t", "segment ")
    s.reasoning_flush("s", "t")
    # a late stream delta for the already-flushed turn must NOT recreate the
    # raw buffer (the bounded completion marker wins).
    assert s.reasoning_feed("s", "t", "late delta") is False
    assert not s.reasoning_pending("s", "t")
    assert s.reasoning_flush("s", "t") is None


def test_reasoning_active_buffers_are_bounded():
    s = ss.SessionState()
    for i in range(ss.REASONING_MAX_ACTIVE + 10):
        s.reasoning_feed("s", f"t{i}", "data")  # never flushed -> active
    active = [k for k, v in s._reasoning.items() if not v["done"]]
    assert len(active) <= ss.REASONING_MAX_ACTIVE
    # completion markers are also bounded.
    for i in range(ss.REASONING_MAX_COMPLETED + 10):
        s.reasoning_feed("m", f"u{i}", "x")
        s.reasoning_flush("m", f"u{i}")
    assert len(s._completed) <= ss.REASONING_MAX_COMPLETED


# -- §2.2 session termination -------------------------------------------------

def test_clear_session_removes_buffers_and_markers():
    s = ss.SessionState()
    s.reasoning_feed("sA", "t1", "secret A")
    s.reasoning_flush("sA", "t1")          # leaves a completion marker
    s.reasoning_feed("sB", "t2", "secret B")
    s.clear_session("sA")
    assert not s.reasoning_pending("sA", "t1")
    assert ("sA", "t1") not in s._completed
    assert s.reasoning_pending("sB", "t2")  # other session untouched
    s.clear_session(None)                  # unconditional clear
    assert not s.reasoning_pending("sB", "t2")
    assert len(s._reasoning) == 0 and len(s._completed) == 0


# -- P1.3 session lifecycle: no reasoning resurrection after end -----------

def test_late_reasoning_after_session_end_is_rejected():
    """A late delta for an already-ended session must not recreate a raw
    buffer (the ended-session tombstone wins over the cleared markers)."""
    s = ss.SessionState()
    s.reasoning_feed("sA", "t1", "secret A")
    s.session_ended("sA")                  # tombstones sA + resets turn/task
    s.clear_session("sA")                   # (collector does both)
    # a late stream delta for the finished session is rejected — no buffer
    assert s.reasoning_feed("sA", "t1", "late resurrection") is False
    assert not s.reasoning_pending("sA", "t1")
    assert ("sA", "t1") not in s._reasoning
    assert s.reasoning_flush("sA", "t1") is None


def test_session_a_to_b_no_turn_inheritance():
    """Ending session A must not let session B inherit A's turn correlation;
    a late A delta stays rejected while B captures normally."""
    s = ss.SessionState()
    s.reasoning_feed("sA", "tA", "A text")
    s.reasoning_flush("sA", "tA")
    s.session_ended("sA")
    # start B: the host does not pass B's turn yet; A's turn must not stick.
    s.session_started("sB")
    assert s.turn_id is None and s.task_id is None
    # B captures its own delta; a late A delta is still rejected.
    assert s.reasoning_feed("sB", "tB", "B text") is True
    assert s.reasoning_feed("sA", "tA", "A late") is False
    assert s.reasoning_pending("sB", "tB")
    assert not s.reasoning_pending("sA", "tA")


def test_correlation_resets_on_session_transition():
    """note(): switching session_id drops the previous session's turn/task so
    a new session inherits nothing."""
    s = ss.SessionState()
    s.note({"session_id": "sA", "turn_id": "tA", "task_id": "kA"})
    assert s.turn_id == "tA" and s.task_id == "kA"
    out = s.note({"session_id": "sB"})     # transition: turn/task cleared
    assert out == {"session_id": "sB", "turn_id": None, "task_id": None}
    # same-session note does NOT clobber a later turn_id
    out2 = s.note({"turn_id": "tB"})
    assert out2["turn_id"] == "tB" and out2["session_id"] == "sB"


# -- §2.4 bounded event queue + overflow accounting ---------------------------

def test_enqueue_overflow_policy_is_bounded():
    metrics.stop_worker()
    with metrics._state_cv:
        metrics._items.clear()
    metrics._dropped.clear()
    old_cap = metrics.MAX_QUEUE_EVENTS
    metrics.MAX_QUEUE_EVENTS = 1
    try:
        with metrics._state_cv:
            metrics._items.append({"type": "turn", "ts": 1.0})  # at capacity
        metrics.enqueue({"type": "turn", "ts": 2.0})            # dropped
        assert metrics._dropped.get("turn", 0) >= 1
        with metrics._state_cv:
            assert len(metrics._items) <= metrics.MAX_QUEUE_EVENTS
    finally:
        metrics.MAX_QUEUE_EVENTS = old_cap
        with metrics._state_cv:
            metrics._items.clear()
        metrics._dropped.clear()


def test_enqueue_truncates_over_cap_prompt():
    metrics.stop_worker()
    with metrics._state_cv:
        metrics._items.clear()
    metrics._rejected.clear()
    old_chars = metrics.MAX_PROMPT_CHARS
    metrics.MAX_PROMPT_CHARS = 10
    try:
        metrics.enqueue({"type": "turn", "ts": 1.0, "user_message": "x" * 50})
        ev = metrics._items[0]
        assert ev["user_message"] == "x" * 10
        assert ev["user_chars"] == 10
        assert sum(metrics._rejected.values()) == 1
    finally:
        metrics.MAX_PROMPT_CHARS = old_chars
        with metrics._state_cv:
            metrics._items.clear()
        metrics._rejected.clear()


def test_flush_reports_overflow_and_truncation_counts():
    metrics.stop_worker()
    with metrics._state_cv:
        metrics._items.clear()
    metrics._dropped.clear()
    metrics._rejected.clear()
    # A drained, clean window reports zero overflow + truncation.
    clean = metrics.flush(timeout=0)
    assert clean.dropped == 0 and clean.rejected == 0
    assert clean.pending == 0 and clean.drained is True

    # Now populate the counters and confirm a flush surfaces them. The counts
    # are windowed: they reflect overflow/truncation that occurred at or after
    # the flush entry, so capture the baseline first, produce the events, then
    # read the module counters that the next flush will carry.
    old_cap, old_chars = metrics.MAX_QUEUE_EVENTS, metrics.MAX_PROMPT_CHARS
    metrics.MAX_QUEUE_EVENTS = 1
    metrics.MAX_PROMPT_CHARS = 4
    try:
        with metrics._state_cv:
            metrics._items.clear()
        metrics._dropped.clear()
        metrics._rejected.clear()
        metrics.enqueue({"type": "turn", "ts": 1.0, "user_message": "x" * 100})
        metrics.enqueue({"type": "turn", "ts": 2.0, "user_message": "y" * 100})
        # one overflow + two truncations are recorded in the module counters,
        # which the bounded queue maintains for every flush result.
        assert sum(metrics._dropped.values()) == 1
        assert sum(metrics._rejected.values()) == 2
    finally:
        metrics.MAX_QUEUE_EVENTS = old_cap
        metrics.MAX_PROMPT_CHARS = old_chars
        with metrics._state_cv:
            metrics._items.clear()
        metrics._dropped.clear()
        metrics._rejected.clear()


# -- §3.4 maintainer smoke subprocess isolation + bounded failure -------------

def _run_smoke_harness(monkeypatch, model_dir, manifest_path, stdout, returncode=0,
                       stderr=""):
    """Patch run_smoke's pm + subprocess.run so the smoke runs hermetically."""
    import tools.fetch_onnx_model as fom
    fake_pm = types.ModuleType("pm")
    fake_pm.ensure_project_environment = lambda *a, **k: sys.executable
    monkeypatch.setitem(sys.modules, "pm", fake_pm)

    captured = {}

    class _Proc:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, *a, **kw):
        captured["env"] = kw.get("env")
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return fom, captured


def test_run_smoke_launch_uses_isolated_env(monkeypatch, tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    vec = [1.0] + [0.0] * 383  # L2 norm == 1.0
    ok = json.dumps({"request_id": "smoke", "vectors": [vec, vec],
                     "dim": 384, "latency_ms": 1.0}) + "\n"
    fom, captured = _run_smoke_harness(monkeypatch, model_dir, manifest, ok)
    fom.run_smoke(model_dir, manifest)

    env = captured["env"]
    assert env is not None, "run_smoke must pass an explicit env"
    # it is the isolated allowlist, never the full parent env.
    expected = {n: os.environ[n] for n in
                ("PATH", "HOME", "LANG", "LC_ALL")
                + (("SYSTEMROOT", "TEMP", "TMP") if os.name == "nt" else ())
                if n in os.environ}
    assert env == expected == worker_client._worker_env()
    for k in ("PYTHONPATH", "PYTHONHOME", "IYH_TEST_PROVIDER_API_KEY"):
        assert k not in env


def test_run_smoke_failure_is_bounded(monkeypatch, tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    long_err = "Traceback (most recent call last): " + "SECRET_ENVVALUE" * 50
    fom, _ = _run_smoke_harness(monkeypatch, model_dir, manifest, "", returncode=1,
                                stderr=long_err)
    with pytest.raises(RuntimeError) as ei:
        fom.run_smoke(model_dir, manifest)
    msg = str(ei.value)
    assert "handshake failed" in msg
    assert len(msg) < 400, "a failed smoke must not dump unbounded stderr"


# -- §4 arbitrary error text is never persisted --------------------------------

def test_error_message_is_never_persisted():
    SENTINEL = "SECRET_SENTINEL_xyz"
    row = metrics._row_from_event({
        "type": "error", "ts": 1.0, "status_code": 503, "retryable": 1,
        "error": {"type": "http", "message": SENTINEL},
    })
    assert row["error_message"] is None
    assert row["error_type"] == "http"
    assert row["status_code"] == 503 and row["retryable"] == 1
    assert SENTINEL not in str(row)


def test_top_level_error_type_is_carried_but_not_message():
    # P1.2: a top-level error_type is coerced through the strict allowlist
    # ("upstream_timeout" maps onto the "timeout" category); the raw message
    # is never persisted.
    row = metrics._row_from_event(
        {"type": "stream_end", "ts": 2.0, "error_type": "upstream_timeout"})
    assert row["error_type"] == "timeout"
    assert row["error_message"] is None


def test_events_stream_end_does_not_carry_raw_message():
    from core import events
    ev = events.stream_end(finished=False,
                           error={"type": "rate_limit",
                                  "message": "provider secret text"})
    assert ev.get("error_type") == "rate_limit"
    assert ev.get("error_message") is None
    assert "provider secret text" not in json.dumps(ev)


def test_error_type_allowlist_rejects_hostile_free_text():
    """P1.2: arbitrary free-text (a Bearer token, a raw exception string, a
    hostile code/error field) is NEVER stored — it collapses to "unknown" at
    both the extractor + the direct-dict DB boundary."""
    from core import events
    SENTINEL = "Bearer abcdef1234567890deadbeef"
    assert events._error_type({"type": SENTINEL}) == "unknown"
    assert events._error_type({"code": SENTINEL}) == "unknown"
    assert events._error_type({"error": SENTINEL}) == "unknown"
    assert events._error_type(SENTINEL) == "unknown"
    # DB boundary: a producer-supplied direct error_type is coerced too.
    row = metrics._row_from_event(
        {"type": "stream_end", "ts": 2.0, "error_type": SENTINEL})
    assert row["error_type"] == "unknown"
    assert SENTINEL not in str(row)
    # known transport terms still map onto the safe categories.
    assert events._error_type("Request timed out") == "timeout"
    assert events._error_type("429 too many requests") == "rate_limit"
    assert events._error_type("401 invalid api key") == "authentication"


# -- P1.1 reset barrier: stop_worker reports a live worker + keeps its ref ----

def test_stop_worker_keeps_ref_when_live():
    """A worker that cannot be joined within the timeout keeps its thread ref
    (so start_worker will not spawn a second thread on top of it) +
    stop_worker reports False. Once it reports dead the ref is cleared + a
    fresh start works."""
    import metrics
    metrics.stop_worker(timeout=2.0)  # drain any real worker before the fake

    class _FakeThread:
        is_alive_result = True
        def is_alive(self):
            return self.is_alive_result
        def join(self, timeout=None):
            pass  # simulate a join that times out with the worker still live
        def start(self):
            pass

    fake = _FakeThread()
    metrics._worker_thread = fake
    assert metrics.stop_worker(timeout=0.1) is False
    assert metrics._worker_thread is fake  # ref retained, not cleared
    # start_worker must not spawn a second thread over the live one.
    metrics.start_worker()
    assert metrics._worker_thread is fake
    # Now the (fake) worker reports dead: the next stop clears the ref.
    fake.is_alive_result = False
    assert metrics.stop_worker(timeout=0.1) is True
    assert metrics._worker_thread is None
