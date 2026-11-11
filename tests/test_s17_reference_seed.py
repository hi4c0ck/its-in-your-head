"""S17: per-set reference-seed structure morphology ("expand the algo").

The portrait must be genuinely different for every set, not just surface
drift. The agent projects the rolling reference's pairwise-cosine geometry
onto bounded face-structure offsets (faceWidth/jaw/shoulder/...) so two
sessions render two different pencil faces.

Invariants pinned here:
* deterministic + order-invariant (same reference -> same structure);
* cross-set-different (different reference -> different structure);
* bounded to [-1, 1];
* graceful neutral fallback when no reference is available (seed=False,
  all structure 0.0 -> today's neutral face);
* abstract: a pure function of the L2 vectors, not of any raw text or
  demographic (the vectors are the only input; no path from text to face).

Byte-sized fakes only; no live DB.
"""
import math
import random

from perception import patterns as P  # noqa: E402

# The six portrait dimensions; reference_state is shaped {dim: {"vectors": [...]}}.
SIX = ["directness", "exploration", "analytical_depth",
       "iteration", "scope", "context_richness"]


def _l2(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def _ref(nvec=8, dim=8, seed=1, scale=0.3):
    """A synthetic reference_state: per-dim L2 vectors, clustered per dim."""
    rng = random.Random(seed)
    ref = {}
    for i, name in enumerate(SIX):
        steer = [0.6 if (j % (dim // 2)) == i else -0.6 for j in range(dim)]
        vecs = []
        for _ in range(nvec):
            v = _l2([rng.uniform(-1, 1) for _ in range(dim)])
            v = _l2([a + b * scale for a, b in zip(v, steer)])
            vecs.append(v)
        ref[name] = {"vectors": vecs}
    return ref


# -- reference_centers: mean + L2; empty -> None --------------------------------
def test_centers_empty_is_none():
    assert P.reference_centers(None, SIX) is None
    assert P.reference_centers({}, SIX) is None
    assert P.reference_centers({"directness": {"vectors": []}}, SIX) is None


def test_centers_are_l2_and_per_dim():
    c = P.reference_centers(_ref(), SIX)
    assert c is not None
    assert set(c) == set(SIX)
    for v in c.values():
        norm = math.sqrt(sum(x * x for x in v))
        assert 0.99 < norm < 1.01, f"center not L2: {norm}"


# -- reference_signature: bounded + deterministic + order-invariant -----------
def test_signature_bounded_and_deterministic():
    c = P.reference_centers(_ref(), SIX)
    s1 = P.reference_signature(c, SIX)
    s2 = P.reference_signature(c, SIX)
    assert s1 == s2, "signature must be deterministic"
    for v in s1.values():
        assert -1.0001 <= v <= 1.0001, f"cosine out of bounds: {v}"


def test_structure_order_invariant():
    # The structure (rounded to 4dp) is invariant to the chronological order
    # of the rolling-window vectors: only the SET of vectors matters. Float
    # summation order can nudge the raw center at the last bit, but the
    # rounded structure is stable.
    ref = _ref(seed=5)
    base = P.structure_morphology(ref, SIX)
    for name in ref:
        ref[name]["vectors"] = list(reversed(ref[name]["vectors"]))
    again = P.structure_morphology(ref, SIX)
    assert base == again, "the structure is order-invariant over the vector set"


# -- structure_morphology: the imprint ------------------------------------------
def test_structure_neutral_when_no_reference():
    m = P.structure_morphology(None, SIX)
    assert m["available"] is False
    assert m["seed"] is False
    assert m["centers"] == 0
    for k in P.MORPH_STRUCTURE_PARAMS:
        assert m[k] == 0.0
    merged = P.merge_morphology(P.neutral_morphology(), m)
    assert merged["seed"] is False


def test_structure_deterministic():
    ref = _ref()
    assert P.structure_morphology(ref, SIX) == P.structure_morphology(ref, SIX)


def test_structure_bounded():
    m = P.structure_morphology(_ref(seed=1, scale=0.6), SIX)
    assert m["available"] is True
    assert m["seed"] is True
    for k in P.MORPH_STRUCTURE_PARAMS:
        assert -1.0001 <= m[k] <= 1.0001, f"{k} out of bounds: {m[k]}"


def test_structure_cross_set_different():
    mA = P.structure_morphology(_ref(seed=1, scale=0.6), SIX)
    mB = P.structure_morphology(_ref(seed=2, scale=0.6), SIX)
    diff = [k for k in P.MORPH_STRUCTURE_PARAMS if mA[k] != mB[k]]
    assert diff, "two different sets produced an identical structure"


def test_structure_is_a_pure_function_of_vectors():
    # A text-free perturbation of the vectors changes the structure: flip ONE
    # dim's vectors -> that dim's center flips -> every pairwise cosine that
    # touches it changes. There is no path from raw text or a demographic to a
    # face; the L2 vectors are the only input. (Flipping ALL dims would
    # preserve every cosine, so we flip one.)
    ref = _ref(seed=7, scale=0.5)
    target = "scope"
    flipped = dict(ref)
    flipped[target] = {"vectors": [_l2([-x for x in v]) for v in ref[target]["vectors"]]}
    mA = P.structure_morphology(ref, SIX)
    mB = P.structure_morphology(flipped, SIX)
    assert any(mA[k] != mB[k] for k in P.MORPH_STRUCTURE_PARAMS), \
        "perturbing one dim's vectors must move the structure"
    assert P.structure_morphology(flipped, SIX) == mB  # deterministic


# -- merge_morphology: pair layer + seed layer ----------------------------------
def test_merge_combines_pair_and_seed():
    pair = P.morphology_from_summary([
        {"strength": 0.8, "pair": ["exploration", "iteration"],
         "confidence": 0.7, "turns": 3}])
    assert pair["available"] is True
    seed_m = P.structure_morphology(_ref(seed=3, scale=0.6), SIX)
    out = P.merge_morphology(pair, seed_m)
    assert out["seed"] is True
    assert out["available"] is True
    # the seed layer's structure offsets survive the merge on top of the pair
    for k in P.MORPH_PARAMS:
        assert k in out
    for k in P.MORPH_STRUCTURE_PARAMS:
        assert out[k] == seed_m[k]


def test_merge_all_neutral_is_neutral():
    out = P.merge_morphology(P.neutral_morphology(),
                             P.structure_morphology(None, SIX))
    assert out["available"] is False
    assert out["seed"] is False
    for k in P.MORPH_STRUCTURE_PARAMS:
        assert out[k] == 0.0
