"""L12 engine wiring: margin-aware confidence (L11 calibrated formula)
reaches BOTH live call sites — the persist path (paired turn ->
pattern_affinity_history row) and the perception view (portrait_view).
margin=None must still reproduce the legacy formula exactly.

Byte-sized fakes only (deterministic 384-dim backend, tmp sqlite).
"""
import hashlib
import math
import time

import pytest

import metrics
from perception import anchors, patterns as patterns_mod
from perception.patterns import confidence_from_evidence


@pytest.fixture(autouse=True)
def _fresh_engine(tmp_path, monkeypatch):
    db = tmp_path / "engine.db"
    metrics.set_db_path(db)
    import engine
    monkeypatch.setattr(engine, "_engine", None)
    monkeypatch.setattr(
        anchors, "get_anchor_vectors",
        lambda backend, **kw: anchors.build_anchor_vectors(backend))
    yield db
    try:
        metrics.stop_worker()
    except Exception:
        pass
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


class FakeBackend:
    """Deterministic 384-dim backend (Tier-1 shape, no network)."""

    model_version = "fake-384-v1"
    dim = 384

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        out = []
        for text in texts:
            text = text or ""
            self.calls.append(text)
            vec = [0.0] * self.dim
            t = text.casefold()
            grams = t if len(t) >= 3 else (" " + t + " ")
            for i in range(len(grams) - 2):
                h = hashlib.blake2b(grams[i:i + 3].encode(),
                                    digest_size=8).digest()
                vec[int.from_bytes(h, "big") % self.dim] += 1.0
            n = math.sqrt(sum(v * v for v in vec))
            out.append([v / n for v in vec] if n else vec)
        return out


def _paired_turn(eng):
    eng.on_events([{"type": "user_turn", "ts": time.time(),
                    "session_id": "s1", "turn_id": "t1",
                    "user_message": "Brainstorm options and think sideways",
                    "user_chars": 34}])
    eng.on_events([{"type": "reasoning_seg", "ts": time.time(),
                    "session_id": "s1", "turn_id": "t1",
                    "reasoning_text": "I am brainstorming alternatives "
                    "before deciding",
                    "reasoning_chars": 50}])


def _latest_history_row(eng_db_path):
    import sqlite3
    conn = sqlite3.connect(eng_db_path)
    try:
        return conn.execute(
            "SELECT ts, top_pattern, top_prompt_affinity, "
            "top_agent_affinity, top_margin, top_amplification, "
            "evidence, confidence, repeated "
            "FROM pattern_affinity_history ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def test_persist_row_confidence_is_margin_calibrated(tmp_path):
    """The persisted confidence equals the L11 calibrated formula
    (margin-gated, via the engine's own counters), NOT the legacy
    formula, on a paired turn."""
    eng = _eng()
    eng._backend = FakeBackend()
    _paired_turn(eng)
    row = _latest_history_row(tmp_path / "engine.db")
    assert row is not None, "paired turn must persist a pattern row"
    (_ts, name, _p_aff, _a_aff, margin, _amp, evidence, conf, _rep) = row
    assert margin is not None and margin > 0
    # Recompute through the engine's real counters (the prompt-only
    # pass of the user_turn event also counts a scored turn, so the
    # agreement is whatever the engine actually accumulated — never
    # assumed): age is exactly 0 because the row's ts is the same
    # instant _pattern_last_evidence was stamped.
    want = eng._pattern_confidence(name, evidence, _ts, margin=margin)
    assert conf == want
    # The margin-gated calibrated value must differ from the legacy
    # formula at the same (n, agreement, age): guards against the
    # wiring silently running legacy.
    agr = (eng._pattern_top_counts.get(name, 0) / eng._pattern_turns
           if eng._pattern_turns else 0.0)
    legacy = confidence_from_evidence(evidence, agr, 0.0)
    assert conf != legacy


def test_both_call_sites_pass_margin(tmp_path, monkeypatch):
    """Spy on _pattern_confidence: the persist path and the perception
    view must both forward a margin kwarg (never the legacy 3-arg form)."""
    eng = _eng()
    eng._backend = FakeBackend()
    calls = []
    real = eng._pattern_confidence

    def spy(name, evidence_n, now, *a, **kw):
        calls.append((name, kw))
        return real(name, evidence_n, now, *a, **kw)

    monkeypatch.setattr(eng, "_pattern_confidence", spy)
    _paired_turn(eng)                      # persist path
    view = eng.portrait_view()              # perception view path
    assert len(calls) >= 2
    assert all("margin" in kw for _n, kw in calls), (
        "every call site must forward the margin kwarg")
    # View entries carry their own margins; the persist call carries
    # the top entry's margin (float or None for prompt-only turns).
    for _n, kw in calls:
        assert isinstance(kw["margin"], (int, float)) or kw["margin"] is None
    # The persisted row must agree with the persist call's margin
    # (the prompt-only pass calls with margin=None first; the
    # paired persist call carries the top entry's float margin).
    row = _latest_history_row(tmp_path / "engine.db")
    persist_margin = next(
        kw["margin"] for _n, kw in calls if kw["margin"] is not None)
    assert row[4] == persist_margin


def test_none_margin_reproduces_legacy_exact():
    """margin=None (default) is byte-exact legacy — the backward-compat
    contract L11 pinned and the engine relies on for prompt-only turns."""
    eng = _eng()
    now = 1_000_000.0
    eng._pattern_turns = 1
    eng._pattern_top_counts = {"exploration": 1}
    eng._pattern_last_evidence = {"exploration": now}
    got = eng._pattern_confidence("exploration", 2, now)
    want = confidence_from_evidence(2, 1.0, 0.0)  # legacy (no margin)
    assert got == want


def test_margin_zero_collapses_calibrated():
    """The gate at margin 0 is 1 - exp(0) = 0: a zero-margin top pattern
    (a tie) must display zero calibrated confidence."""
    eng = _eng()
    now = 1_000_000.0
    eng._pattern_turns = 1
    eng._pattern_top_counts = {"exploration": 1}
    eng._pattern_last_evidence = {"exploration": now}
    assert eng._pattern_confidence("exploration", 2, now,
                                   margin=0.0) == 0.0
    # Same inputs with a generous margin gate near-pass-through.
    assert eng._pattern_confidence("exploration", 2, now,
                                   margin=0.5) > 0.0
