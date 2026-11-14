"""Unit tests for the canonical PULSE vector space (perception.canonical).

The canonical 256-d space is the common stable base every PULSE vector is
projected into at write time, so toggling the source encoder never orphans
the stored data.
"""
from __future__ import annotations

import math
import random

import pytest

try:
    from perception import canonical
except ImportError:  # pragma: no cover - namespaced (gateway) context
    from hermes_plugin_its_in_your_head_perception import canonical  # type: ignore # noqa: F401


def _cos(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def test_canonical_space_descriptor():
    sp = canonical.canonical_space()
    assert sp["id"] == "canon-256"
    assert sp["dim"] == canonical.CANONICAL_DIM
    assert sp["dim"] == 256
    assert sp["model_version"] is None


def test_identity_for_canonical_dim():
    rng = random.Random(7)
    v = [rng.random() for _ in range(canonical.CANONICAL_DIM)]
    out = canonical.to_canonical(v)
    assert len(out) == canonical.CANONICAL_DIM
    # 256-d input is identity + L2-normalize: direction is preserved.
    assert abs(_cos(out, v) - 1.0) < 1e-9
    assert abs(math.sqrt(sum(x * x for x in out)) - 1.0) < 1e-9


def test_projection_reduces_to_canonical_dim():
    rng = random.Random(8)
    v = [rng.random() for _ in range(384)]
    out = canonical.to_canonical(v)
    assert len(out) == canonical.CANONICAL_DIM


def test_projection_is_deterministic():
    rng = random.Random(9)
    v = [rng.random() for _ in range(384)]
    assert canonical.to_canonical(v) == canonical.to_canonical(v)
    # Same for the 256-d identity path.
    w = [rng.random() for _ in range(256)]
    assert canonical.to_canonical(w) == canonical.to_canonical(w)


def test_projection_preserves_cosine():
    # Johnson-Lindenstrauss: a pair's cosine is approximately preserved when
    # folded from 384 into 256.
    rng = random.Random(11)
    base = [rng.gauss(0, 1) for _ in range(384)]
    other = [b + 0.6 * rng.gauss(0, 1) for b in base]
    pb, po = canonical.to_canonical(base), canonical.to_canonical(other)
    orig = _cos(base, other)
    projected = _cos(pb, po)
    assert abs(orig - projected) < 0.12, (orig, projected)


def test_mixed_sources_are_comparable_and_crash_free():
    # The whole point: a projected-384 and a native-256 vector share the
    # canonical dim, so cosine never raises a dimension mismatch.
    p384 = canonical.to_canonical([0.3] * 384)
    p256 = canonical.to_canonical([0.3] * 256)
    assert len(p384) == len(p256) == canonical.CANONICAL_DIM
    # No exception:
    _cos(p384, p256)


def test_empty_input_is_zero_vector():
    assert canonical.to_canonical([]) == [0.0] * canonical.CANONICAL_DIM
    assert canonical.to_canonical(None) == [0.0] * canonical.CANONICAL_DIM


def test_is_canonical_predicate():
    assert canonical.is_canonical([0.0] * 256) is True
    assert canonical.is_canonical([0.0] * 384) is False
    assert canonical.is_canonical([]) is False


def test_cross_dim_cosine_reaches_foreign_dim():
    """A higher-res (384-d) vector is reachable from a lower-res (256-d) goal
    (they meet at 256), with no crash and a bounded, symmetric result."""
    rng = random.Random(21)
    v384 = [rng.gauss(0, 1) for _ in range(384)]
    v256 = [rng.gauss(0, 1) for _ in range(256)]
    c = canonical.cross_dim_cosine(v256, v384)
    assert -1.0 <= c <= 1.0
    assert abs(c - canonical.cross_dim_cosine(v384, v256)) < 1e-9


def test_cross_dim_cosine_same_dim_is_exact():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert canonical.cross_dim_cosine(a, b) == 0.0
    assert abs(canonical.cross_dim_cosine(a, a) - 1.0) < 1e-9


def test_cross_dim_cosine_empty_is_zero():
    assert canonical.cross_dim_cosine([], [1, 2, 3]) == 0.0
    assert canonical.cross_dim_cosine(None, [1, 2, 3]) == 0.0
    assert canonical.cross_dim_cosine([1, 2, 3], None) == 0.0


def test_project_down_identity_and_reduce():
    rng = random.Random(22)
    v = [rng.random() for _ in range(512)]
    assert len(canonical.project_down(v, 512)) == 512  # identity
    assert len(canonical.project_down(v, 256)) == 256
    assert len(canonical.project_down(v, 128)) == 128
    assert canonical.project_down(v, 256) == canonical.project_down(v, 256)


def test_cross_dim_scoring_does_not_mutate_stored_vectors():
    """The projection is ephemeral: cross-dim scoring must leave the stored
    (higher-res) vector untouched, so no resolution is ever lost."""
    rng = random.Random(23)
    original = [rng.gauss(0, 1) for _ in range(384)]
    snapshot = list(original)
    canonical.cross_dim_cosine(original, [0.5] * 256)
    assert original == snapshot, \
        "cross-dim scoring must not mutate the stored vector"


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


def _manual_projection(vec, target_dim):
    """Independent recompute WITHOUT the result cache (projector rows are
    still shared, but the per-vector multiply-add runs fresh)."""
    v = [float(x) for x in vec]
    rows = canonical._projector_rows(len(v), target_dim)
    out = [sum(r[j] * v[j] for j in range(len(v))) for r in rows]
    n = math.sqrt(sum(x * x for x in out))
    return [0.0] * len(out) if n == 0 else [x / n for x in out]


def test_project_down_result_cache_is_value_pure():
    """The bounded result cache must not change observable values: repeated
    calls return the byte-identical projection, an evicted key recomputes
    to the same value, and callers get a copy (mutation-safe)."""
    import random as _random
    rng = _random.Random(11)
    vec = [rng.random() - 0.5 for _ in range(384)]
    target = 256

    first = canonical.project_down(vec, target)
    assert first == canonical.project_down(vec, target)
    # the cached value equals an independent (uncached) recompute
    assert first == _manual_projection(vec, target)
    # returned lists are copies: mutating the result never poisons the cache
    first[0] += 99.0
    assert canonical.project_down(vec, target) == _manual_projection(vec, target)

    # Evict a key by overflowing the bounded cache, then re-request it:
    # the recomputed value must still equal the manual projection.
    for i in range(canonical._PROJ_VALUE_CACHE_MAX + 4):
        filler = [float(i) * 0.1 + rng.random() for _ in range(384)]
        canonical.project_down(filler, target)
    assert canonical.project_down(vec, target) == _manual_projection(vec, target)
    # short / empty branches stay correct and uncached (cheap O(n))
    assert canonical.project_down([], target) == [0.0] * target
    short = [0.5, 0.5]
    assert len(canonical.project_down(short, target)) == 2
