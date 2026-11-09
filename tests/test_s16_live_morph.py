"""S16: live expression + pattern-pair morphology (plan: Live
Expression & Pattern Mutations).

Coverage:
* pure helper math in perception.patterns (stage-1 / stage-2
  derivation, settle/hold/release display arithmetic, pair gating,
  morphology bounds, determinism);
* engine wiring (transient live state, portrait_view keys, reset);
* dashboard-fallback reconstruction (neutral live state, morphology
  rebuilt from the persisted top-three pattern history, s15-style).

Byte-sized fakes only; file sqlite only.
"""
import importlib
import math
import random
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
from perception import patterns as P  # noqa: E402
from storage import migration  # noqa: E402

SIX = ["exploration", "directness", "analytical_depth",
       "iteration", "scope", "context_richness"]
T0 = 1_000_000.0


def _entries(prompt=None, agent=None, amp=0.0, margin=0.2):
    prompt = prompt or {}
    agent = agent or {}
    out = []
    for n in SIX:
        out.append({
            "name": n,
            "prompt_affinity": prompt.get(n, 0.5),
            "agent_affinity": agent.get(n, 0.5) if agent else None,
            "amplification": amp,
            "margin": margin,
        })
    return out


# ------------------------------------------------------------------ #
# Stage-1 / stage-2 derivation (pure, deterministic)
# ------------------------------------------------------------------ #
def test_neutral_live_expression_shape():
    n = P.neutral_live_expression()
    assert n["stage"] == "none" and n["updated_at"] is None
    assert n["openness"] == 0.0 and n["focus"] == 0.0 and n["intensity"] == 0.0
    # Resting midpoint, never an invented direction:
    assert n["resonance"] == 0.5 and n["tension"] == 0.5
    assert n["confidence"] == 0.0


def test_stage1_prompt_only_derives_openness_not_resonance():
    e = P.derive_live_expression(
        _entries(prompt={"exploration": 0.9, "context_richness": 0.8}),
        paired=False, ts=T0)
    assert e["stage"] == "prompt"
    assert e["openness"] > 0.4          # exploratory breadth reads positive
    assert e["focus"] == 0.0           # directness/analysis at neutral
    assert e["intensity"] == e["openness"]
    # The agent side is unavailable: NEVER fabricated, lowered confidence
    # instead.
    assert e["resonance"] is None and e["tension"] is None
    assert e["confidence"] == P.EXPR_PROMPT_CONFIDENCE
    assert e["updated_at"] == T0


def test_stage2_paired_refines_and_bounds_resonance_tension():
    e = P.derive_live_expression(
        _entries(prompt={"exploration": 0.9, "context_richness": 0.8},
                 agent={"exploration": 0.85, "context_richness": 0.75},
                 amp=0.3),
        paired=True, ts=T0)
    assert e["stage"] == "paired"
    assert e["resonance"] == pytest.approx(0.8)
    assert e["tension"] == pytest.approx(0.2)
    # One divergence signal: the two channels sum to the neutral 1.0.
    assert e["resonance"] + e["tension"] == pytest.approx(1.0)
    assert 0.0 <= e["intensity"] <= 1.0
    # Confidence is the paired base plus a margin-gated span (<= 1).
    assert P.EXPR_PAIRED_CONFIDENCE_BASE <= e["confidence"] <= 1.0


def test_paired_without_signals_keeps_unavailable_channels_none():
    raw = _entries()
    for x in raw:
        x["amplification"] = None  # divergence signal unavailable
        x["margin"] = None
    e = P.derive_live_expression(raw, paired=True, ts=T0)
    assert e["resonance"] is None and e["tension"] is None
    # No margin -> exactly the paired base, no span added.
    assert e["confidence"] == P.EXPR_PAIRED_CONFIDENCE_BASE
    # Empty / invalid input -> the neutral resting state, with the
    # requested timestamp (never a stale value).
    n = P.derive_live_expression([], ts=T0 + 5)
    assert n["stage"] == "none" and n["updated_at"] == T0 + 5


# ------------------------------------------------------------------ #
# Settle / hold / release display arithmetic (timestamps only)
# ------------------------------------------------------------------ #
def _target(openness):
    return {"openness": openness, "focus": 0.0, "resonance": None,
            "tension": None, "intensity": openness, "confidence": 0.3,
            "stage": "prompt", "updated_at": T0}


def test_display_settles_holds_and_releases():
    tgt = _target(0.8)
    base = P.neutral_live_expression()
    settle_end = T0 + P.EXPR_SETTLE_S
    hold_end = settle_end + P.EXPR_HOLD_S
    # Mid-settle: strictly between base and target (smoothstep).
    mid = P.live_expression_display(base, tgt, T0, T0 + P.EXPR_SETTLE_S / 2)
    assert 0.0 < mid["openness"] < 0.8
    # Settle complete: at target.
    at_settle = P.live_expression_display(base, tgt, T0, settle_end)
    assert at_settle["openness"] == pytest.approx(0.8)
    # Hold: still at target (well under one poll interval of settling,
    # hold itself is several seconds).
    at_hold = P.live_expression_display(base, tgt, T0, hold_end)
    assert at_hold["openness"] == pytest.approx(0.8)
    # One release tau after the hold: exponential decay.
    one_tau = P.live_expression_display(
        base, tgt, T0, hold_end + P.EXPR_RELEASE_TAU_S)
    assert one_tau["openness"] == pytest.approx(
        0.8 * math.exp(-1.0), abs=1e-4)
    # Three release taus: clamped to the neutral rest.
    far = P.live_expression_display(
        base, tgt, T0, hold_end + 3 * P.EXPR_RELEASE_TAU_S)
    assert far["openness"] == pytest.approx(0.8 * 0.0498, abs=0.005)
    rest = P.live_expression_display(
        base, tgt, T0, hold_end + 5 * P.EXPR_RELEASE_TAU_S)
    assert rest["openness"] == 0.0
    # No timers: identical inputs -> identical output.
    a = P.live_expression_display(base, tgt, T0, hold_end + 0.7)
    b = P.live_expression_display(base, tgt, T0, hold_end + 0.7)
    assert a == b
    # A never-set target is just the neutral resting state.
    assert P.live_expression_display(base, None, T0, T0 + 10) == \
        P.neutral_live_expression()


def test_display_continuity_sets_from_previous_snapshot():
    """A new target settles from the CURRENT display value, never jumps."""
    first = _target(0.8)
    display_at_4s = P.live_expression_display(
        P.neutral_live_expression(), first, T0, T0 + 4.0)  # at target
    second = _target(0.0)   # e.g. a focus-only expression next turn
    settled = P.live_expression_display(
        display_at_4s, second, T0 + 4.0, T0 + 4.0 + P.EXPR_SETTLE_S)
    assert settled["openness"] == pytest.approx(0.0)
    # Mid-settle of the second target it departs from 0.8, not from 0:
    mid = P.live_expression_display(
        display_at_4s, second, T0 + 4.0, T0 + 4.0 + P.EXPR_SETTLE_S / 2)
    assert 0.0 < mid["openness"] < 0.8


# ------------------------------------------------------------------ #
# Turn grouping: keyed top-three vs legacy single-name rows
# ------------------------------------------------------------------ #
def test_group_turns_pools_keyed_rows_and_keeps_legacy_single():
    rows = [(1.0, "tk1", "A"), (1.0, "tk1", "B"), (2.0, None, "C")]
    obs = P.group_turns(rows)
    keyed = [o for o in obs if o[1] == "tk1"]
    assert len(keyed) == 1 and sorted(keyed[0][2]) == ["A", "B"]
    legacy = [o for o in obs if o[1] is None]
    assert [o[2] for o in legacy] == [["C"]]
    # Legacy rows can never fabricate pair evidence:
    assert P.pattern_pair_summary(P.group_turns(
        [(1.0, None, "A"), (2.0, None, "B"), (3.0, None, "A")])) == []


# ------------------------------------------------------------------ #
# Pair gating: repeated, associated evidence only
# ------------------------------------------------------------------ #
def _obs(turns, now_s=0.0):
    """turns: [(ts, [names])] -> group_turns-shaped observations."""
    return [(float(ts), "k%d" % i, list(names))
            for i, (ts, names) in enumerate(turns)]


def test_single_or_few_cooccurrences_never_mutate():
    # One co-occurrence: far below PAIR_MIN_TURNS.
    obs = _obs([(1.0, ["A", "B"])])
    assert P.pattern_pair_summary(obs) == []
    # PAIR_MIN_TURNS exactly but independent (no lift, low conditional):
    obs = _obs([(1.0, ["A", "B"]), (2.0, ["A", "B"]), (3.0, ["A", "B"]),
                (4.0, ["A"]), (5.0, ["A"]), (6.0, ["B"]), (7.0, ["B"]),
                (8.0, ["A"]), (9.0, ["B"]), (10.0, ["C"])])
    # A in 6/10, B in 6/10, pair in 3/10: lift ~0.83, cond ~0.5.
    assert P.pattern_pair_summary(obs) == []


def test_repeated_associated_pair_is_gated_in():
    obs = _obs([(1.0, ["A", "B"]), (2.0, ["A", "B"]), (3.0, ["A", "B"]),
                (4.0, ["A", "B"]), (5.0, ["A"]), (6.0, ["B"]),
                (7.0, ["C"]), (8.0, ["C"]), (9.0, ["C"]), (10.0, ["C"])])
    s = P.pattern_pair_summary(obs)
    assert len(s) == 1
    top = s[0]
    assert top["pair"] == ["A", "B"]
    assert top["turns"] == 4
    assert top["lift"] >= P.PAIR_MIN_LIFT
    assert 0.0 < top["strength"] <= P.MORPH_MAX_STRENGTH
    assert top["confidence"] > 0.0


def test_independent_frequent_patterns_rejected():
    # Both patterns are frequent but NOT associated: lift < 1.20 and
    # conditional < 0.95 -> no mutation.
    obs = _obs([(float(i), ["G", "H"]) for i in range(1, 5)] +
               [(10.0, ["G"]), (11.0, ["G"]), (12.0, ["G"]),
                (13.0, ["H"]), (14.0, ["H"]), (15.0, ["H"]),
                (16.0, ["X"])])
    assert P.pattern_pair_summary(obs) == []


def test_stale_evidence_decays_below_active_floor():
    now = 100_000_000.0
    old_days = now - 400.0 * 24 * 3600
    # 400-day-old co-occurrences (half-life 14 days -> weight ~1e-9) are
    # outweighed by fresh unrelated turns: recency support collapses and
    # the strength falls below the active floor -> neutral wins.
    rows = []
    for i in range(10):
        rows += [(old_days + i, "k_old%d" % i, n) for n in ("A", "B")]
    # Fresh SINGLE-NAME turns: they dilute the stale pair's recency
    # support without co-occurring themselves (no fresh pair of their
    # own to promote).
    for i in range(10):
        rows += [(now - i, "k_new%d" % i, "Y")]
    morpho = P.morphology_from_rows(rows, now_s=now)
    assert morpho["available"] is False
    assert morpho["pair"] is None and morpho["strength"] == 0.0


def test_summary_is_deterministic_under_row_reordering():
    rows = []
    for i in range(1, 11):
        rows += [(float(i), "k%d" % i, n)
                 for n in (["A", "B"] if i <= 6 else ["A", "C"])]
    base = P.pattern_pair_summary(P.group_turns(rows))
    shuffled = list(rows)
    random.Random(42).shuffle(shuffled)
    again = P.pattern_pair_summary(P.group_turns(shuffled))
    assert base == again
    assert base and base[0]["pair"] in (["A", "B"], ["A", "C"])


def test_morphology_maps_motif_and_bounds_params():
    rows = [(float(i), "k%d" % i, n)
            for i in range(1, 11)
            for n in ("context_richness", "analytical_depth")]
    m = P.morphology_from_rows(rows, now_s=0.0)
    assert m["available"] is True
    assert m["pair"] == ["analytical_depth", "context_richness"]
    assert m["evidence"] == 10
    assert m["strength"] == pytest.approx(P.MORPH_MAX_STRENGTH)
    # Curated motif: layered graphite -> line_density dominates.
    assert m["line_density"] > m["echo_strength"]
    for p in P.MORPH_PARAMS:
        assert 0.0 <= m[p] <= P.MORPH_MAX_STRENGTH
    n = P.neutral_morphology()
    assert P.morphology_from_rows([], now_s=0.0) == n


# ------------------------------------------------------------------ #
# Engine wiring: transient live state + lazy morphology view
# ------------------------------------------------------------------ #
@pytest.fixture(autouse=True)
def _fresh_engine(tmp_path, monkeypatch):
    db = tmp_path / "engine_s16.db"
    import metrics
    metrics.set_db_path(db)
    import engine
    monkeypatch.setattr(engine, "_engine", None)
    from perception import anchors
    monkeypatch.setattr(
        anchors, "get_anchor_vectors",
        lambda backend, **kw: anchors.build_anchor_vectors(backend))
    yield db
    try:
        metrics.stop_worker()
    except Exception:
        pass
    try:
        import engine as _engine_mod
        _engine_mod._engine = None
    except Exception:
        pass


def _eng():
    import engine
    return engine.Engine()


def test_portrait_view_carries_new_keys_neutral_on_fresh_engine():
    view = _eng().portrait_view()
    assert "live_expression" in view and "morphology" in view
    live = view["live_expression"]
    assert live["stage"] == "none" and live["updated_at"] is None
    assert live["resonance"] == 0.5 and live["tension"] == 0.5
    morpho = view["morphology"]
    assert morpho["available"] is False and morpho["pair"] is None
    for p in P.MORPH_PARAMS:
        assert morpho[p] == 0.0


def test_engine_live_expression_updates_settles_and_resets():
    eng = _eng()
    eng._set_live_expression(
        _entries(prompt={"exploration": 0.9, "context_richness": 0.8}),
        paired=False, ts=T0)
    # One settle window later the display is AT the target.
    view = eng._live_expression_display(T0 + P.EXPR_SETTLE_S + 0.1)
    assert view["openness"] > 0.4 and view["stage"] == "prompt"
    # Stage 2 on the same turn refines it (resonance/tension available).
    eng._set_live_expression(
        _entries(prompt={"exploration": 0.9},
                 agent={"exploration": 0.8}, amp=0.3),
        paired=True, ts=T0 + 1.0)
    view = eng._live_expression_display(T0 + 1.0 + P.EXPR_SETTLE_S + 0.1)
    assert view["resonance"] is not None and view["tension"] is not None
    assert view["resonance"] + view["tension"] == pytest.approx(1.0)
    # After hold + 5 release taus the display is back at rest.
    far = eng._live_expression_display(T0 + 1.0 + P.EXPR_SETTLE_S
                                       + P.EXPR_HOLD_S
                                       + 5 * P.EXPR_RELEASE_TAU_S)
    assert far["openness"] == 0.0 and far["intensity"] == 0.0
    # Reset wipes the transient state: fresh process == neutral.
    eng.reset()
    import engine as _engine_mod
    assert eng._live_expression_display(T0 + 100) == \
        _engine_mod.NEUTRAL_LIVE_EXPRESSION
    assert eng._morph_cache is None and eng._morph_cache_key is None


# ------------------------------------------------------------------ #
# Dashboard fallback: neutral live state, reconstructed morphology
# ------------------------------------------------------------------ #
def _api(db):
    import dashboard.plugin_api as api
    importlib.reload(api)
    api.set_db_path(db)
    return api


def _seed(db, rows):
    """rows: (ts, name, prompt_aff, agent_aff, margin, amp, evidence,
    confidence, repeated, rank, turn_key)."""
    conn = sqlite3.connect(str(db))
    try:
        migration.schema_bootstrap(conn)
        for r in rows:
            conn.execute(
                "INSERT INTO pattern_affinity_history "
                "(ts, top_pattern, top_prompt_affinity, "
                "top_agent_affinity, top_margin, top_amplification, "
                "evidence, confidence, repeated, rank, turn_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
        conn.commit()
    finally:
        conn.close()


def _shape(db, monkeypatch):
    api = _api(db)
    monkeypatch.setattr(api, "_portrait_live_view", lambda: None)
    monkeypatch.setattr(api, "_engine_first", lambda *a, **k: (None, None))
    return api._portrait_shape()


def test_fallback_shape_serves_neutral_live_expression(tmp_path,
                                                       monkeypatch):
    db = tmp_path / "fb1.db"
    shape = _shape(db, monkeypatch)
    live = shape["live_expression"]
    assert live["stage"] == "none" and live["updated_at"] is None
    assert live["resonance"] == 0.5 and live["tension"] == 0.5
    assert shape["morphology"]["available"] is False


def test_fallback_reconstructs_morphology_from_history(tmp_path,
                                                        monkeypatch):
    db = tmp_path / "fb2.db"
    # 4 keyed co-occurrences of A+B out of 10 turns (lift ~1.6):
    rows = []
    for i in range(1, 5):
        rows += [(float(i), "A", .5, .7, .1, .1, 1, .4, 0, 1, "tk%d" % i),
                 (float(i), "B", .5, .7, .1, .1, 1, .4, 0, 2, "tk%d" % i)]
    rows += [(5.0, "A", .5, .7, .1, .1, 1, .4, 0, 1, "tk5")]
    rows += [(6.0, "B", .5, .7, .1, .1, 1, .4, 0, 1, "tk6")]
    for i in range(7, 11):
        rows += [(float(i), "C", .5, .7, .1, .1, 1, .4, 0, 1, "tk%d" % i)]
    _seed(db, rows)
    shape = _shape(db, monkeypatch)
    m = shape["morphology"]
    assert m["available"] is True
    assert m["pair"] == ["A", "B"] and m["evidence"] == 4
    assert 0.0 < m["strength"] <= P.MORPH_MAX_STRENGTH
    # Live state is NOT reconstructed from history (plan §3.4):
    assert shape["live_expression"]["stage"] == "none"


def test_fallback_neutral_when_no_associated_evidence(tmp_path,
                                                      monkeypatch):
    db = tmp_path / "fb3.db"
    # 3 co-occurrences, but A/B are frequent-and-independent (no lift,
    # low conditional): the gate must reject it.
    rows = []
    for i in (1, 2, 3):
        rows += [(float(i), "A", .5, .7, .1, .1, 1, .4, 0, 1, "tk%d" % i),
                 (float(i), "B", .5, .7, .1, .1, 1, .4, 0, 2, "tk%d" % i)]
    for i in (4, 5, 6, 7):
        rows += [(float(i), "A", .5, .7, .1, .1, 1, .4, 0, 1, "tk%d" % i)]
    for i in (8, 9, 10):
        rows += [(float(i), "B", .5, .7, .1, .1, 1, .4, 0, 1, "tk%d" % i)]
    _seed(db, rows)
    shape = _shape(db, monkeypatch)
    assert shape["morphology"]["available"] is False
