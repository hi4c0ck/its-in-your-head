"""Per-dimension rolling reference (dynamic reassembly).

Pure / deterministic pins for perception/reference.py: seed, reassembly,
bounded FIFO window, warm-up gate, freshness canary, ood_drift, derived-state
round-trip, and the derived-only / L2-normalized invariants.
"""
import math

from perception.reference import (
    RollingReference, DimWindow, DEFAULT_WINDOW, DEFAULT_MIN_COUNT,
)
from perception.anchors import DIMENSIONS
from perception.encoder import l2_normalize


def _n(*vals):
    return list(l2_normalize(list(vals)))


# --- seed + warm-up ------------------------------------------------------- #

def test_cold_start_seeded_from_bank_not_warmed():
    ref = RollingReference(seed_bank={
        "directness": [_n(1, 0, 0)], "iteration": [_n(0, 1, 0)],
    })
    for d in ref.dims:
        vectors, warmed = ref.reference_for(d)
        # Every dim is present; a seeded dim is NOT warmed (below the floor).
        assert not warmed, d
    vectors, warmed = ref.reference_for("directness")
    assert vectors == [_n(1, 0, 0)]
    assert not warmed


def test_warm_up_requires_live_reassembly_and_min_count():
    ref = RollingReference(seed_bank={"directness": [_n(1, 0, 0)]})
    assert ref.reference_for("directness")[1] is False  # seeded, not warmed
    ref.reassemble("directness", _n(1, 0.1, 0), ts=1.0)
    _, warmed = ref.reference_for("directness")
    assert not warmed, "one live vector is still under the min-count floor"
    for i in range(DEFAULT_MIN_COUNT):
        ref.reassemble("directness", _n(1, 0.2 * i, 0), ts=float(i + 2))
    _, warmed = ref.reference_for("directness")
    assert warmed, "at the floor the dim has reassembled"


def test_unknown_dim_yields_empty_reference():
    ref = RollingReference()
    assert ref.reference_for("nonexistent") == ([], False)


# --- bounded FIFO window + determinism ------------------------------------ #

def test_window_is_bounded_fifo():
    w = DimWindow("x", window_size=3)
    for i in range(5):
        # [i+1, 1, 0] normalizes to a distinct direction each time, so the
        # most-recent entries are identifiable by their x-component.
        w.add([float(i + 1), 1.0, 0.0], ts=float(i))
    assert len(w.vectors()) == 3            # bounded to the window size
    assert w.live_count == 5                # total reassemblies, not retained
    import math
    expected_x = [(i + 1) / math.sqrt((i + 1) ** 2 + 1.0) for i in (2, 3, 4)]
    assert [v[0] for v in w.vectors()] == expected_x  # only the newest 3 kept


def test_deterministic_under_seed_and_reassembly_order():
    seed = {"a": [_n(1, 0, 0)], "b": [_n(0, 1, 0)]}

    def build():
        r = RollingReference(seed_bank=seed)
        r.reassemble("a", _n(0.9, 0.1, 0), ts=1.0)
        r.reassemble("b", _n(0.0, 0.8, 0.2), ts=2.0)
        return r.to_state()
    assert build() == build(), "same seed + same reassemblies -> identical state"


# --- freshness canary ----------------------------------------------------- #

def test_freshness_reports_age_and_seed_state():
    ref = RollingReference(seed_bank={"directness": [_n(1, 0, 0)]})
    f = ref.freshness(now=100.0)["directness"]
    assert f["seeded"] is True and f["live_count"] == 0
    assert f["count"] == 1 and f["age"] >= 0.0
    ref.reassemble("directness", _n(0.8, 0.2, 0), ts=95.0)
    f = ref.freshness(now=100.0)["directness"]
    assert f["seeded"] is False and f["live_count"] == 1
    assert f["freshest_ts"] == 95.0 and f["age"] == 5.0


# --- ood_drift ------------------------------------------------------------- #

def test_ood_drift_is_one_minus_best_dim_affinity():
    ref = RollingReference()
    assert ref.ood_drift({"directness": 0.4, "scope": 0.8}) == 0.2   # 1 - 0.8
    assert ref.ood_drift({"directness": 0.1}) == 0.9                 # far
    assert ref.ood_drift({}) == 1.0                                   # no match at all


# --- derived-state round-trip --------------------------------------------- #

def test_state_round_trip_preserves_window_and_live_count():
    ref = RollingReference(seed_bank={"iteration": [_n(0, 1, 0), _n(0, 1.2, 0)]})
    ref.reassemble("iteration", _n(0.2, 0.9, 0.1), ts=7.0)
    restored = RollingReference(state=ref.to_state())
    src, dst = ref.windows["iteration"], restored.windows["iteration"]
    # Window + metadata survive the derived-state round trip exactly.
    assert src.live_count == dst.live_count == 1
    assert src.seeded == dst.seeded == False
    assert src.freshest_ts == dst.freshest_ts == 7.0
    assert [list(v) for v in src.vectors()] == [list(v) for v in dst.vectors()]


def test_seeded_only_reference_has_zero_live_count():
    ref = RollingReference(seed_bank={"scope": [_n(0, 0, 1)]})
    assert ref.windows["scope"].live_count == 0
    assert ref.windows["scope"].seeded is True
