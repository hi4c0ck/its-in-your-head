"""D1-D6 user-upset friction accumulator tests.

Pure-math coverage of pulse/upset.py (fake encoder, in-memory sqlite
for the meta-KV persistence), plus the engine wiring (the upset feed on
reasoning_seg / user_turn / session_end and the pulse_view upset block).
No network, no model-scale I/O.
"""
import json
import sqlite3
import time

import pytest

from pulse import upset as _upset
from pulse.upset import (
    MOOD_BETA,
    MOOD_NEUTRAL,
    UPSET_DECAY,
    UPSET_GLITCH_TIER,
    UPSET_LANE_TIER,
    UPSET_PHRASES,
    UpsetAccumulator,
)

_NORM = sum(UPSET_DECAY ** i for i in range(6))


# -- the fake encoder: a 4-dim space with a dedicated UPSET axis -------- #
class _FakeEncoder:
    """Deterministic 4-dim encoder. The anchor family sits on axis 0;
    'upset' texts land near axis 0, 'neutral' texts land on axis 2 (zero
    cosine against the anchors)."""

    name = "fake"
    model_version = "fake-0"

    def __init__(self, broken=False):
        self.broken = broken
        self.anchors = [[1.0, 0.0, 0.0, 0.0]] * 8
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        if self.broken:
            raise RuntimeError("encoder down")
        out = []
        for t in texts:
            if not isinstance(t, str):
                out.append([])
                continue
            tl = t.strip()
            # The anchor family (UPSET_PHRASES: the D1 narration lines
            # AND the user-side declaratives) + reasoning/user texts
            # that MENTION the user's upset state land on axis 0 (high
            # cosine against the anchors); anything unrelated lands on
            # axis 3 — a direction no anchor ever uses (zero cosine).
            if (tl in UPSET_PHRASES or any(
                    p in tl for p in ("upset", "annoyed", "frustrated",
                                       "irritated", "angry", "fed up",
                                       "losing patience", "harsh"))):
                out.append([0.95, 0.2, 0.0, 0.0])
            else:
                out.append([0.0, 0.0, 0.0, 1.0])
        return out


def _enc():
    return _FakeEncoder()


def _acc(backend=None):
    return UpsetAccumulator()


# -- D2: the decay-window math ------------------------------------------ #
def test_f_prime_full_window_reads_one():
    acc = _acc()
    import collections
    win = collections.deque(maxlen=6)
    for _ in range(6):
        win.append(1.0)
    assert acc.f_prime(win) == pytest.approx(1.0)


def test_f_prime_single_turn_upset():
    acc = _acc()
    import collections
    win = collections.deque(maxlen=6)
    win.append(1.0)
    # owner immediacy rule (2026-11-12): f_prime normalizes by the
    # window ACTUALLY PRESENT — one turn of s=1.0 reads at FULL
    # strength (tier 2 territory), no 6-turn dilution.
    assert acc.f_prime(win) == pytest.approx(1.0, abs=1e-9)


def test_f_prime_decays_with_recency():
    acc = _acc()
    import collections
    # a distant upset outranks a fresh one in the window sum
    win = collections.deque(maxlen=6)
    win.append(1.0)   # 5 turns old
    win.append(0.0)
    win.append(0.0)
    win.append(0.0)
    win.append(0.0)
    win.append(0.0)
    assert acc.f_prime(win) == pytest.approx(UPSET_DECAY ** 5 / _NORM)


# -- D1: the per-turn score ---------------------------------------------- #
def test_score_reasoning_upset_high():
    acc = _acc()
    s = acc.score_reasoning(
        "The user is clearly frustrated and losing patience.", _enc())
    assert s == pytest.approx(0.95 / (0.95 ** 2 + 0.2 ** 2) ** 0.5,
                             abs=1e-6) or s > 0.9


def test_score_reasoning_unrelated_low():
    acc = _acc()
    s = acc.score_reasoning("Now fix the build pipeline.", _enc())
    assert s < 0.1   # the MiniLM-floor analog: no anchor alignment


def test_score_reasoning_empty_is_no_evidence():
    acc = _acc()
    assert acc.score_reasoning("", _enc()) == 0.0
    assert acc.score_reasoning(None, _enc()) == 0.0  # type: ignore[arg-type]


def test_score_reasoning_broken_backend_degrades():
    acc = _acc()
    assert acc.score_reasoning("user upset", _FakeEncoder(broken=True)) == 0.0


# -- the turn-boundary tick (D1: one score per LLM response) ------------- #
def _reasoning(turn, text, ts=1000.0):
    return {"type": "reasoning_seg", "ts": ts, "session_id": "s1",
            "turn_id": turn, "reasoning_text": text}


def test_turn_boundary_steps_previous_turn():
    acc = _acc()
    be = _enc()
    # t1: unrelated reasoning (scores s ~ 0 when t2 arrives)
    acc.on_reasoning_seg(_reasoning("t1", "Now fix the build."), be)
    # t2: the upset narration (t1 is FINALIZED here -> a zero s)
    acc.on_reasoning_seg(
        _reasoning("t2", "The user is clearly frustrated."), be)
    p = acc.payload("s1")
    assert p["value"] == 0.0
    assert p["tier"] == 0
    # t3: another upset turn (t2 is FINALIZED -> one high s in the
    # window, normalized by the window PRESENT: F' = 1.0/1.73 ~ 0.58)
    acc.on_reasoning_seg(
        _reasoning("t3", "The user is fed up with the delays."), be)
    p = acc.payload("s1")
    assert p["value"] == pytest.approx(1.0 / (1.0 + UPSET_DECAY), abs=1e-4)
    assert p["tier"] == 1
    # t4: a closer (t3, s=1.0, is FINALIZED -> two high s in the
    # window: F' = (1 + 0.73)/2.2629 ~ 0.76 — past the glitch tier now,
    # because the normalization follows the window present)
    acc.on_reasoning_seg(_reasoning("t4", "ok"), be)
    p = acc.payload("s1")
    assert p["value"] == pytest.approx(
        (1.0 + UPSET_DECAY) / (1.0 + UPSET_DECAY + UPSET_DECAY ** 2),
        abs=1e-4)
    assert p["tier"] == 2
    assert p["upset_turns"] == 2


def test_session_switch_finalizes_previous():
    acc = _acc()
    be = _enc()
    # establish a scoring cycle first (the close-path re-score needs a
    # recorded backend — the first finalized turn records it)
    acc.on_reasoning_seg(_reasoning("t0", "Now fix the build."), be)
    acc.on_reasoning_seg(
        _reasoning("t1", "The user is upset about the delay."), be)
    m_before = acc.M   # t0 (zero s) has been stepped; t1 is buffered
    # a NEW session arrives: the old session's buffered last turn is
    # scored into M on the lazy close; its F state is dropped (D3).
    acc.on_reasoning_seg(
        {"type": "reasoning_seg", "ts": 1000.0, "session_id": "s2",
         "turn_id": "t1", "reasoning_text": "ok"}, be)
    assert "s1" not in acc._sessions
    assert acc.M < m_before   # the finalized upset s pulled M down


# -- D4: the tiers --------------------------------------------------------- #
def _force_fprime(acc, value):
    """Seed the active session's window directly with a constant s that
    yields the target F' (all six slots equal -> F' = s)."""
    import collections
    st = acc._sessions.get("local")
    if st is None:
        acc.on_user_turn({"type": "user_turn", "session_id": None,
                          "user_message": "hi"}, now=1.0)
        st = acc._sessions["local"]
    st["window"] = collections.deque(
        [value] * 6, maxlen=_upset.UPSET_WINDOW)
    if value >= 0.3:
        st["upset_turns"] = 6


def test_tier_boundaries():
    for value, tier in ((0.0, 0), (0.2999, 0), (UPSET_LANE_TIER, 1),
                        (0.5, 1), (UPSET_GLITCH_TIER, 2), (1.0, 2)):
        acc = _acc()
        _force_fprime(acc, value)
        p = acc.payload()
        assert p["tier"] == tier, (value, p)


# -- D5: the good-reset (scoped to F' only) ------------------------------ #
def test_good_reset_wipes_fprime_not_mood():
    acc = _acc()
    be = _enc()
    # four FINALIZED upset turns -> F' in the glitch tier
    # (F' = (1 + 0.73 + 0.73^2 + 0.73^3)/3.1432 ~ 0.84; the buffered
    # fifth is not yet stepped, by the turn-boundary rule)
    for i in range(5):
        acc.on_reasoning_seg(
            _reasoning(f"t{i}", "The user is angry and fed up."), be)
    p = acc.payload("s1")
    assert p["tier"] == 2
    m_before = p["mood"]
    # a short GOOD reaction hard-resets F' to 0 (same session)
    acc.on_user_turn(
        {"type": "user_turn", "session_id": "s1", "user_message": "great",
         "ts": 1001.0}, good_reset=True)
    p = acc.payload("s1")
    assert p["value"] == 0.0
    assert p["tier"] == 0
    assert p["mood"] == m_before   # D5: M is untouched


# -- D6: the slow mood ------------------------------------------------------ #
def test_mood_update_rule():
    acc = _acc()
    assert acc.M == MOOD_NEUTRAL
    # s = 1.0 -> M = (1-beta)*M + beta*(1-1) = 0.45
    acc.on_user_turn({"type": "user_turn", "user_message": "x"})
    import collections
    st = acc._sessions["local"]
    acc._step(st, 1.0)
    assert acc.M == pytest.approx(MOOD_NEUTRAL * (1 - MOOD_BETA))
    # s = 0.0 -> M = 0.45*0.9 + 0.1*1 = 0.505
    acc._step(st, 0.0)
    assert acc.M == pytest.approx(0.45 * (1 - MOOD_BETA) + MOOD_BETA)


def test_mood_releases_at_same_rate():
    """Strain builds and releases at the SAME slow rate (D6): from a
    strained M, good turns pull M toward 1.0 at beta per step — never
    faster, never reset."""
    acc = _acc()
    acc.M = 0.1
    import collections
    st = {"window": collections.deque(maxlen=6), "upset_turns": 0}
    acc._step(st, 0.0)
    assert acc.M == pytest.approx(0.1 * 0.9 + 0.1)


# -- D6: persistence + the read-time prognosis ---------------------------- #
def _meta_conn():
    conn = sqlite3.connect(":memory:")
    from storage import migration as _mig
    _mig.schema_bootstrap(conn)
    return conn


def test_close_session_persists_mood_and_history():
    acc = _acc()
    conn = _meta_conn()
    acc.M = 0.4
    acc.close_session("s1", conn)
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        ("upset_mood",)).fetchone()
    assert row is not None
    assert json.loads(row[0])["value"] == pytest.approx(0.4)
    hist = json.loads(conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        ("upset_mood_history",)).fetchone()[0])
    assert len(hist) == 1
    assert hist[0]["session_id"] == "s1"
    # no history yet -> the prognosis is an honest absence
    assert acc.prognosis() is None


def test_hydrate_restores_mood_and_prognosis_appears():
    conn = _meta_conn()
    import collections
    # process 1: fresh (never stepped) -> the hand-off row is 0.3
    acc1 = _acc()
    acc1.M = 0.3
    acc1.close_session("s1", conn)
    # process 2: integrates M down via real steps (0.5 -> 0.45 -> 0.405)
    # then closes: the stepped M is the process's own truth (no clobber)
    acc2 = _acc()
    st2 = {"window": collections.deque(maxlen=6), "upset_turns": 0}
    acc2._step(st2, 1.0)
    acc2._step(st2, 1.0)
    assert acc2.M == pytest.approx(0.405)
    acc2.close_session("s2", conn)
    # a fresh process hydrates both the value and the history
    acc3 = _acc()
    acc3.hydrate_mood(conn)
    assert acc3.M == pytest.approx(0.405)
    p = acc3.prognosis()
    assert p is not None
    assert p["values"] == pytest.approx([0.3, 0.405])
    assert p["slope"] == pytest.approx(0.105)   # improving (toward good)


def test_mood_history_is_bounded():
    conn = _meta_conn()
    acc = _acc()
    for i in range(70):
        acc.M = 0.5 - i * 0.0001
        acc.close_session(f"s{i}", conn)
    hist = json.loads(conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        ("upset_mood_history",)).fetchone()[0])
    assert len(hist) <= _upset.MOOD_HISTORY_MAX


def test_close_session_broken_conn_degrades_to_memory():
    class _BrokenConn:
        def execute(self, *a, **kw):
            raise RuntimeError("db down")

        def commit(self):
            raise RuntimeError("db down")

    acc = _acc()
    acc.M = 0.3
    acc.close_session("s1", _BrokenConn())   # must not raise
    assert acc.M == pytest.approx(0.3)        # the in-memory value holds


def test_close_session_without_conn_is_in_memory_only():
    acc = _acc()
    acc.close_session("s1", None)             # the D3 restart path
    assert "s1" not in acc._sessions


# -- D3 cross-process handoff (the F' mirror) ------------------------------ #
def test_fstate_roundtrip_reconstructs_block():
    import collections
    conn = _meta_conn()
    acc = _acc()
    acc._session("s1", 100.0)
    st = acc._sessions["s1"]          # step the REAL session (persist_state reads it)
    acc._step(st, 1.0)
    acc._step(st, 0.0)
    st["upset_turns"] = 1
    conn2 = _meta_conn()
    acc.persist_state(conn2, ts=200.0, session_id="s1")
    block = UpsetAccumulator.read_state(conn2, now=201.0)
    assert block is not None
    assert block["tier"] in (0, 1, 2)
    # the window [1.0, 0.0] (newest last) reads F' via the same decay math
    expect = UpsetAccumulator.f_prime(st["window"])
    assert block["value"] == pytest.approx(expect, abs=1e-3)
    assert block["upset_turns"] == 1
    assert block["mood"] == pytest.approx(acc.M, abs=1e-3)


def test_fstate_ttl_settles_but_mood_survives():
    import collections
    conn = _meta_conn()
    acc = _acc()
    acc._session("s1", 100.0)
    st = acc._sessions["s1"]
    acc._step(st, 1.0)
    # write the mirror at ts=100: a FRESH read sees the live window
    # (F'=1.0), a STALE read (past the TTL) settles to neutral
    acc.persist_state(conn, ts=100.0, session_id="s1")
    fresh = UpsetAccumulator.read_state(conn, now=100.0)
    assert fresh is not None
    assert fresh["value"] == pytest.approx(1.0)
    stale_now = 100.0 + _upset.SESSION_TTL_S + 1.0
    block = UpsetAccumulator.read_state(conn, now=stale_now)
    assert block is not None
    assert block["value"] == 0.0          # the stale mirror settles
    assert block["tier"] == 0
    assert block["upset_turns"] == 0
    # the slow mood M (written with the mirror) still surfaces
    assert block["mood"] == pytest.approx(acc.M, abs=1e-3)


def test_fstate_close_writes_cleared_mirror():
    import collections
    conn = _meta_conn()
    acc = _acc()
    acc._session("s1", 100.0)
    st = acc._sessions["s1"]
    acc._step(st, 1.0)
    m_before = acc.M
    # a LIVE mirror (friction is real now): F' = 1.0 on the single step
    acc.persist_state(conn, session_id="s1")
    live = UpsetAccumulator.read_state(conn)
    assert live is not None
    assert live["value"] == pytest.approx(1.0)
    acc.close_session("s1", conn)               # writes the CLEARED mirror
    block = UpsetAccumulator.read_state(conn)
    assert block is not None
    assert block["value"] == 0.0          # F' cleared on close (D3)
    assert block["tier"] == 0
    assert block["mood"] == pytest.approx(m_before, abs=1e-2)


def test_read_state_empty_db_is_mood_only_neutral():
    conn = _meta_conn()
    block = UpsetAccumulator.read_state(conn)
    assert block is not None
    assert block["value"] == 0.0
    assert block["tier"] == 0
    assert block["mood"] == pytest.approx(MOOD_NEUTRAL)
    assert block["prognosis"] is None     # honest absence


def test_read_state_corrupt_mirror_degrades_to_mood_only():
    conn = _meta_conn()
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("upset_fstate", "{not json"))
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("upset_mood", json.dumps({"value": 0.3})))
    conn.commit()
    block = UpsetAccumulator.read_state(conn)
    assert block is not None
    assert block["value"] == 0.0          # corrupt mirror -> settled
    assert block["mood"] == pytest.approx(0.3)


def test_read_state_no_conn_is_none():
    assert UpsetAccumulator.read_state(None) is None


# -- engine wiring ---------------------------------------------------------- #
def test_engine_pulse_view_carries_upset_block(tmp_path, monkeypatch):
    import metrics
    metrics.set_db_path(tmp_path / "engine.db")
    import engine
    eng = engine.Engine()
    be = _enc()
    monkeypatch.setattr(eng, "_get_backend", lambda: be)
    ev = _reasoning("t1", "The user is angry and fed up.", ts=time.time())
    ev2 = _reasoning("t2", "ok", ts=time.time() + 1.0)
    eng.on_events([ev, ev2])
    view = eng.pulse_view()
    assert "upset" in view
    assert view["upset"]["tier"] in (0, 1, 2)
    assert set(view["upset"].keys()) == {
        "value", "tier", "upset_turns", "mood", "prognosis"}


def test_engine_session_end_persists_mood(tmp_path, monkeypatch):
    import metrics
    db = tmp_path / "engine.db"
    metrics.set_db_path(db)
    import engine
    eng = engine.Engine()
    be = _enc()
    monkeypatch.setattr(eng, "_get_backend", lambda: be)
    eng.on_events([
        _reasoning("t1", "The user is frustrated.", ts=time.time())])
    eng.on_events([{
        "type": "session_end", "ts": time.time(),
        "session_id": "s1", "reasoning": "done"}])
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("upset_mood",)).fetchone()
        assert row is not None   # the hand-off row was written
    finally:
        conn.close()


def test_engine_reset_wipes_accumulator(tmp_path, monkeypatch):
    import metrics
    metrics.set_db_path(tmp_path / "engine.db")
    import engine
    eng = engine.Engine()
    be = _enc()
    monkeypatch.setattr(eng, "_get_backend", lambda: be)
    eng.on_events([
        _reasoning("t1", "The user is angry.", ts=time.time())])
    acc_before = eng.upset
    eng.reset()
    assert eng.upset is not acc_before
    assert acc_before is not eng.upset
    assert eng.upset._sessions == {}


# -- immediacy feed (owner rule 2026-11-12: react NOW) ---------------------- #
def _user(turn, text, ts=1500.0):
    return {"type": "user_turn", "ts": ts, "session_id": "s1",
            "turn_id": turn, "user_message": text}


def test_user_message_scores_instantly():
    acc = _acc()
    be = _enc()
    # No reasoning, no next turn: ONE harsh user message alone clears
    # both tiers (a single s=1.0 step reads at full strength).
    ev = _user("u1", "The user is clearly frustrated.")
    acc.on_user_turn(ev)
    acc.on_user_message(ev, ev["user_message"], be)
    p = acc.payload("s1")
    assert p["value"] == pytest.approx(1.0, abs=1e-4)
    assert p["tier"] == 2
    # a neutral user message decays it (an s=0 step rides in)
    ev2 = _user("u2", "Now fix the build.")
    acc.on_user_turn(ev2)
    acc.on_user_message(ev2, ev2["user_message"], be)
    p = acc.payload("s1")
    assert p["value"] == pytest.approx(
        UPSET_DECAY / (1.0 + UPSET_DECAY), abs=1e-4)
    assert p["tier"] == 1


def test_engine_user_turn_instant_upset(tmp_path, monkeypatch):
    import metrics
    metrics.set_db_path(tmp_path / "engine.db")
    import engine
    eng = engine.Engine()
    be = _enc()
    monkeypatch.setattr(eng, "_get_backend", lambda: be)
    ev = _user("u1", "The user is angry and fed up.")
    eng.on_events([dict(ev, ts=time.time())])
    p = eng.upset.payload("s1")
    assert p["tier"] == 2   # instant — no reasoning narration involved


def test_engine_good_message_brings_fprime_down(tmp_path, monkeypatch):
    import metrics
    metrics.set_db_path(tmp_path / "engine.db")
    import engine
    eng = engine.Engine()
    be = _enc()
    monkeypatch.setattr(eng, "_get_backend", lambda: be)
    eng.on_events([dict(_user("u1", "The user is angry and fed up."),
                         ts=time.time())])
    high = eng.upset.payload("s1")["value"]
    eng.on_events([dict(_user("u2", "great, thanks for the fix"),
                         ts=time.time() + 1.0)])
    low = eng.upset.payload("s1")["value"]
    assert low < high   # D5 reset (or an s~0 decay step) — either way down
