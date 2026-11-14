"""Cross-dim PULSE vector scoring (no data loss, safe encoder toggle).

The PULSE layer stores embedding vectors produced by whichever encoder was
active at write time -- the zero-dep 256-d **sketch** (Tier-0, the stable
floor) or the 384-d **Tier-1 ONNX MiniLM**. Vectors are stored at their
*NATIVE* dimension: nothing is ever downconverted on write, so no resolution
is lost.

The problem this module solves: a stored 384-d vector and a fresh 256-d goal
live in different spaces, so a naive cosine either raises a dimension-mismatch
error or returns 0 (no signal). That made the expanded PULSE view empty and
orphaned data across an encoder toggle.

Fix: score **cross-dim**, *at read time only*. :func:`cross_dim_cosine` meets
at the *lower* of the two dimensions -- the higher-res vector is projected
down by a deterministic seeded sparse random projection (Johnson-Lindenstrauss
style, ~cosine-preserving), the lower-res vector is used as-is. The projection
is pure and ephemeral: the stored original is never touched, so "top quality"
(384-d) data stays intact and BOTH encoders can reach every stored vector,
regardless of which encoder was active when it was written.

Toggling the source encoder is therefore safe: the shared store holds each
vector at its native dim, and the reader projects on demand. Nothing is lost,
nothing crashes.
"""
from __future__ import annotations

import math
import random
import zlib
from collections import OrderedDict

# The stable sketch (Tier-0 / fallback) dimension -- the floor of the PULSE
# meet-dim. 384 (Tier-1 ONNX) is the higher-res upgrade. Track embedder.DIM
# when importable so a sketch-dim bump stays in sync; the 256 literal is the
# documented default and the fallback.
CANONICAL_DIM: int = 256
try:  # hermes namespaced import (gateway) vs top-level import (repo/tests)
    from .. import embedder as _embedder
except Exception:  # pragma: no cover - repo-root fallback
    try:
        import embedder as _embedder  # type: ignore[no-redef]
    except Exception:
        _embedder = None
if _embedder is not None and int(getattr(_embedder, "DIM", CANONICAL_DIM)) > 0:
    CANONICAL_DIM = int(getattr(_embedder, "DIM"))

# The 256-d floor identity (kept for callers that tag the active space).
CANONICAL_SPACE_ID = "canon-256"
# Stable salt for the seeded projection -- never change it or the cross-dim
# geometry shifts on the next load.
_PROJ_SALT = "its-in-your-head-crossdim"

_proj_cache: dict = {}

# Bounded result cache for project_down's PROJECTION branch only
# (the len(v) > target_dim case - the ~O(src_dim * target_dim) pure
# Python path). project_down is a pure, seed-deterministic function
# of (vec, target_dim): the stored original is never touched, so the
# cached value is byte-identical to a recompute across runs and the
# matrix/lanes/live-engine scoring cannot diverge. The PULSE read
# path projects the same stored vectors many times per poll
# (build_matrix + build_lanes + every cross_dim_cosine pair); the
# cache turns that fan-out into one projection per distinct vector.
# Keyed by the exact input tuple; small FIFO cap keeps it bounded on
# a growing store. Identity / shorter-vec branches stay uncached
# (already O(n) cheap) and the projector-matrix cache is unchanged.
_PROJ_VALUE_CACHE: "OrderedDict" = OrderedDict()
_PROJ_VALUE_CACHE_MAX = 256


def _l2_normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n == 0:
        return list(v)
    return [x / n for x in v]


def _projector_rows(src_dim: int, target_dim: int) -> list[list[float]]:
    """Deterministic seeded sparse random projector ``target_dim x src_dim``.

    Entries are +/-1/sqrt(src_dim) with signs drawn from a fixed-seed RNG, so
    the matrix is regenerated identically on every process start (JL style,
    approximately cosine-preserving). No numpy dependency (the zero-dep
    sketch path stays zero-dep).
    """
    key = (src_dim, target_dim)
    rows = _proj_cache.get(key)
    if rows is not None:
        return rows
    seed = zlib.crc32(f"{_PROJ_SALT}:{target_dim}:{src_dim}".encode("utf-8"))
    rng = random.Random(seed)
    scale = 1.0 / math.sqrt(src_dim)
    rows = [
        [rng.choice((-1.0, 1.0)) * scale for _ in range(src_dim)]
        for _ in range(target_dim)
    ]
    _proj_cache[key] = rows
    return rows


def project_down(vec, target_dim: int) -> list[float]:
    """Project ``vec`` down to ``target_dim`` (L2-normalized), read-time only.

    * ``len(vec) == target_dim``: identity + L2-normalize (no projection).
    * ``len(vec) >  target_dim``: seeded sparse random projection to
      ``target_dim``, then L2-normalize. Ephemeral -- the stored vector is
      never rewritten, so no resolution is lost.
    * ``len(vec) <  target_dim``: cannot be upgraded losslessly; L2-normalize
      at its own length and return (in practice this only happens for the
      exact-dim case, which is an identity).
    * empty/None: an all-zero ``target_dim`` vector.

    Pure in ``(vec, target_dim)`` -- deterministic across runs.
    """
    v = [float(x) for x in (vec or [])]
    if target_dim <= 0:
        return []
    if not v:
        return [0.0] * target_dim
    if len(v) == target_dim:
        return _l2_normalize(v)
    if len(v) > target_dim:
        key = (target_dim, tuple(v))
        cached = _PROJ_VALUE_CACHE.get(key)
        if cached is not None:
            _PROJ_VALUE_CACHE.move_to_end(key)
            return list(cached)  # a copy: callers may mutate results
        rows = _projector_rows(len(v), target_dim)
        out = _l2_normalize(
            [sum(r[j] * v[j] for j in range(len(v))) for r in rows])
        _PROJ_VALUE_CACHE[key] = out
        if len(_PROJ_VALUE_CACHE) > _PROJ_VALUE_CACHE_MAX:
            _PROJ_VALUE_CACHE.popitem(last=False)
        # Return a copy on the miss path too: the original contract hands a
        # caller a fresh list, so a caller mutation never reaches the cache.
        return list(out)
    return _l2_normalize(v)


def cross_dim_cosine(a, b) -> float:
    """Cosine of ``a`` and ``b`` meeting at their LOWER dimension.

    The higher-res vector is projected down (read-time, non-destructive) to
    the lower of the two dims; the lower-res vector is used as-is. Same-dim
    input is an exact cosine. Returns 0.0 for any empty input. This is how
    either encoder reaches every stored vector without losing the
    higher-res original -- and without a dimension-mismatch crash.
    """
    av = [float(x) for x in (a or [])]
    bv = [float(x) for x in (b or [])]
    d = min(len(av), len(bv))
    if d <= 0:
        return 0.0
    a2 = project_down(av, d)
    b2 = project_down(bv, d)
    na = math.sqrt(sum(x * x for x in a2))
    nb = math.sqrt(sum(x * x for x in b2))
    if na == 0 or nb == 0:
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a2, b2)) / (na * nb)))


def to_canonical(vec, src_dim: int | None = None) -> list[float]:
    """Project ``vec`` into the 256-d floor space (a read-time helper).

    Kept for callers that want the 256 sketch floor specifically; the general
    cross-dim primitive is :func:`cross_dim_cosine`.
    """
    v = [float(x) for x in (vec or [])]
    return project_down(v, CANONICAL_DIM)


def canonical_space() -> dict:
    """The canonical PULSE vector-space descriptor (always 256-d).

    Passed to the PULSE read/scoring side in place of a raw encoder space, so
    all stored PULSE vectors (regardless of source encoder) score in one
    common space and toggling the encoder never orphans the data.
    """
    return {"id": CANONICAL_SPACE_ID, "dim": CANONICAL_DIM, "model_version": None}


def is_canonical(vec) -> bool:
    """True when ``vec`` already lives in the canonical dimension."""
    return bool(vec) and len(vec) == CANONICAL_DIM
