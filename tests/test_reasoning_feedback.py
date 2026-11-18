"""Reasoning -> interpreted portrait + explicit feedback -> prototypes.

Byte-small fakes only; no network, no multi-GB writes. Fixture pattern
follows tests/test_engine_wiring.py (tmp DB via metrics.set_db_path,
fresh Engine instances / module singleton isolation).
"""
import time

import pytest

import embedder
import metrics
from perception import anchors
from pulse.feedback import MAX_WORDS, detect_feedback


@pytest.fixture(autouse=True)
def _fresh_engine(tmp_path, monkeypatch):
    db = tmp_path / "engine.db"
    metrics.set_db_path(db)
    import engine
    monkeypatch.setattr(engine, "_engine", None)
    # Keep anchor vectors in-memory: no cache file I/O in these tests.
    monkeypatch.setattr(
        anchors, "get_anchor_vectors",
        lambda backend, **kw: anchors.build_anchor_vectors(backend))
    yield db
    try:
        metrics.stop_worker()
    except Exception:
        pass
    # Hygiene: turn events enqueued via the collector seam (notably by
    # _user_turn_via_collector) must not outlive the test. Left queued,
    # they are drained by a LATER test's worker under that test's DB path
    # — and the worker-side engine feed now does F2 portrait_history
    # DB I/O per turn, so a stale turn can create a DB file inside an
    # unrelated test (deterministic order-dependent failure of
    # test_register_wires_matrix_without_opening_db). Drain + reset.
    while metrics._items:
        metrics._items.popleft()
    try:
        import engine as _engine_mod
        _engine_mod._engine = None
    except Exception:
        pass


def _eng():
    import engine
    return engine.Engine()


def _reasoning(text="Brainstorm every option, even wild ones, "
                    "explore alternatives and think sideways", ts=None):
    return {"type": "reasoning_seg", "ts": ts or time.time(),
            "session_id": "s1", "turn_id": "t1",
            "reasoning_text": text,
            "reasoning_chars": len(text), "reasoning_deltas": 3}


def _tool_end(tool="read_file", status="ok", ts=None, **kw):
    ev = {"type": "tool_end", "ts": ts or time.time(),
          "tool_name": tool, "status": status, "duration_ms": 12,
          "session_id": "s1", "turn_id": "t1"}
    ev.update(kw)
    return ev


def _user_turn_via_collector(text, eng, turn_id="t9"):
    """Drive the collector seam with ``eng`` as the module singleton."""
    import engine
    import core.collector as collector
    engine._engine = eng
    return collector._on_pre_llm_call(
        user_message=text, turn_id=turn_id, session_id="s1")


# --- reasoning -> interpreted portrait ------------------------------------

def test_reasoning_seg_moves_interpreted_off_neutral():
    eng = _eng()
    before = eng.portrait.snapshot()["interpreted"]["values"]
    assert all(v == 0.5 for v in before.values())
    assert eng.portrait.snapshot()["interpreted"]["evidence_count"] == 0
    eng.on_events([_reasoning()])
    snap = eng.portrait.snapshot()["interpreted"]
    assert snap["evidence_count"] == 1
    assert any(abs(v - 0.5) > 1e-9 for v in snap["values"].values())
    assert eng._interpreted_evidence_count == 1
    assert eng._interpreted_last_ts is not None
    # Observed track untouched by reasoning.
    assert eng.portrait.snapshot()["observed"]["evidence_count"] == 0


def test_portrait_view_available_gating_reflects_reasoning_evidence():
    eng = _eng()
    empty = eng.portrait_view()
    assert empty["available"] is False
    assert all(v is None for v in empty["agent"].values())
    eng.on_events([_reasoning()])
    view = eng.portrait_view()
    assert view["available"] is True
    assert all(v is not None for v in view["agent"].values())
    assert any(abs(v - 0.5) > 1e-9 for v in view["agent"].values())
    # User track still gated: no observed evidence yet.
    assert all(v is None for v in view["user"].values())


def test_portrait_view_agent_none_without_reasoning_evidence():
    eng = _eng()
    eng.on_events([{"type": "turn", "ts": time.time(), "session_id": "s1",
                    "turn_id": "t1", "user_message": "Fix the login page",
                    "user_chars": 16}])
    view = eng.portrait_view()
    assert view["available"] is True
    assert all(v is not None for v in view["user"].values())
    # Agent track reports values ONLY when reasoning evidence exists.
    assert all(v is None for v in view["agent"].values())


def test_empty_batch_leaves_interpreted_neutral():
    eng = _eng()
    eng.on_events([])
    snap = eng.portrait.snapshot()["interpreted"]
    assert snap["evidence_count"] == 0
    assert all(v == 0.5 for v in snap["values"].values())
    assert eng.portrait_view()["available"] is False


def test_reasoning_without_vector_is_noop_not_crash():
    eng = _eng()
    eng.on_events([{"type": "reasoning_seg", "ts": time.time(),
                    "turn_id": "t1"}])  # no sketch/vector, no raw text
    snap = eng.portrait.snapshot()["interpreted"]
    assert snap["evidence_count"] == 0
    assert all(v == 0.5 for v in snap["values"].values())
    assert eng.portrait_view()["available"] is False


def test_reasoning_vector_dimension_mismatch_is_noop():
    eng = _eng()
    eng.on_events([{"type": "reasoning_seg", "ts": time.time(),
                    "turn_id": "t1", "vector": [1.0, 0.0]}])
    assert eng.portrait.snapshot()["interpreted"]["evidence_count"] == 0


def test_reset_clears_interpreted_evidence():
    eng = _eng()
    eng.on_events([_reasoning()])
    assert eng.portrait_view()["available"] is True
    eng.reset()
    assert eng.portrait_view()["available"] is False
    assert eng.portrait.snapshot()["interpreted"]["evidence_count"] == 0


# --- detector unit behaviour ----------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("thanks, that worked", "good"),
    ("thank you so much", "good"),
    ("you fixed it", "good"),
    ("that did it", "good"),
    ("good job", "good"),
    ("still broken", "friction"),
    ("it did not work", "friction"),
    ("no it failed", "friction"),
    ("that is wrong", "friction"),
    ("now run the tests", None),
    ("what about the dashboard?", None),
    ("hmm, interesting", None),
    ("", None),
    (None, None),
])
def test_detect_feedback_table(text, expected):
    assert detect_feedback(text) == expected


def test_detect_feedback_long_utterance_never_labeled():
    # Gratitude BURIED mid-clause in a long task description stays unlabeled;
    # only an isolated clause-start acknowledgement is promoted (below).
    text = "please " + " ".join(["please"] * MAX_WORDS) + " thanks again"
    assert len(text.split()) > MAX_WORDS
    assert detect_feedback(text) is None


def test_detect_feedback_clause_start_thanks_in_long_message():
    text = ("dat huge! Thanks. Keep investigate, we should follow "
            "is everything going straight forward as our expectations.")
    assert len(text.split()) > MAX_WORDS
    assert detect_feedback(text) == "good"


def test_detect_feedback_negative_outranks_positive():
    assert detect_feedback("good job but still broken") == "friction"


# --- feedback -> prototypes (collector seam) -------------------------------

def test_positive_utterance_credits_all_tools_in_preceding_turn():
    eng = _eng()
    eng.on_events([_tool_end(tool="read_file", ts=1000.0),
                   _tool_end(tool="terminal", ts=1001.0)])
    caps = eng.capsule_state()
    read_vec = caps["read_file"][-1]["vector"]
    term_vec = caps["terminal"][-1]["vector"]
    _user_turn_via_collector("thanks, that worked", eng)
    # One reaction to an assistant turn credits the distinct tools that
    # turn used; it is not a single last-capsule lottery.
    assert eng.protos.bank("terminal")["good"] == [term_vec]
    assert eng.protos.bank("read_file")["good"] == [read_vec]
    assert eng.protos.labeled_count("terminal") == 1
    assert eng.protos.labeled_count("read_file") == 1


def test_negative_utterance_records_friction():
    eng = _eng()
    eng.on_events([_tool_end(tool="read_file")])
    vec = eng.capsule_state()["read_file"][-1]["vector"]
    _user_turn_via_collector("still broken", eng)
    assert eng.protos.bank("read_file")["friction"] == [vec]
    # The successful tool_end itself is behavioural GOOD evidence; explicit
    # friction is a separate label, never a rewrite of that success.
    assert eng.protos.bank("read_file")["good"] == [vec]


def test_followup_records_no_explicit_label():
    eng = _eng()
    eng.on_events([_tool_end(tool="read_file")])
    _user_turn_via_collector("now run the tests", eng)
    # A successful call still lands in GOOD (behavioural evidence); the
    # neutral follow-up adds no USER label of its own.
    assert eng.protos.labeled_count("read_file") == 1
    assert len(eng.protos.bank("read_file")["good"]) == 1
    assert eng.protos.bank("read_file")["friction"] == []


def test_feedback_without_capsule_records_nothing():
    eng = _eng()
    _user_turn_via_collector("thanks, that worked", eng)
    assert eng.protos.tools() == []


def test_detector_fault_degrades_to_observer_only(monkeypatch):
    import core.collector as collector
    eng = _eng()
    eng.on_events([_tool_end(tool="read_file")])

    def _boom(text):
        raise RuntimeError("detector down")
    monkeypatch.setattr(collector, "detect_feedback", _boom)
    out = _user_turn_via_collector("thanks, that worked", eng)
    assert out is None  # observer-only, no crash out of the hook
    # The successful call still lands GOOD; the faulted detector adds
    # nothing further (no duplicate, no crash).
    assert eng.protos.labeled_count("read_file") == 1
    assert len(eng.protos.bank("read_file")["good"]) == 1


def test_feedback_credits_all_distinct_turn_tools():
    eng = _eng()
    eng.on_events([_tool_end(tool="read_file", ts=1000.0),
                   _tool_end(tool="terminal", ts=1001.0),
                   _tool_end(tool="search_files", ts=1002.0)])
    caps = eng.capsule_state()
    _user_turn_via_collector("you fixed it", eng)
    for tool in ("read_file", "terminal", "search_files"):
        vec = caps[tool][-1]["vector"]
        assert eng.protos.bank(tool)["good"] == [vec]
        assert eng.protos.labeled_count(tool) == 1


def test_feedback_ignores_tools_from_earlier_turns():
    eng = _eng()
    eng.on_events([
        _tool_end(tool="read_file", ts=1000.0, turn_id="t0"),
        _tool_end(tool="terminal", ts=1001.0, turn_id="t1"),
        _tool_end(tool="search_files", ts=1002.0, turn_id="t1"),
    ])
    caps = eng.capsule_state()
    before = {t: len(eng.protos.bank(t)["good"])
              for t in ("read_file", "terminal", "search_files")}
    assert all(n == 1 for n in before.values())  # successful calls only
    _user_turn_via_collector("you fixed it", eng)
    after = {t: len(eng.protos.bank(t)["good"])
             for t in before}
    # Earlier-turn tool got no extra credit; the t1 tools were already
    # GOOD from their own successful calls and are deduplicated.
    assert after == before
    for tool in ("read_file", "terminal", "search_files"):
        vec = caps[tool][-1]["vector"]
        assert eng.protos.bank(tool)["good"] == [vec]
