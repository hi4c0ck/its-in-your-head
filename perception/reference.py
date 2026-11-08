"""Per-dimension rolling reference (dynamic reassembly, S3+).

The rolling reference replaces the frozen anchor bank as the observed
track's scoring reference. Per dimension it holds a BOUNDED window of
L2-normalized derived vectors, seeded from the curated bank on cold start
and progressively reassembled from live observed turns: a turn observed to
be strong on a dim folds its own encoded vector into that dim's window,
and old entries age out on the bounded window (FIFO).

Design (owner rulings, 2026-11-08 dynamic-reassembly plan):
- R1: the 6-dimension scaffold stays fixed; only each dim's reference
  vectors reassemble.
- R3: continuous — every qualifying observed turn reassembles; no batch.
- R4: cold start seeds the window from the curated bank so the first run
  is not empty.
- R5: derived vectors only — raw prompt/reasoning text is never held.

Pure + deterministic (no I/O, no clock): the caller supplies timestamps,
and a reference built from the same seed + same reassemblies is
byte-identical, so it is pin-testable and persistable as derived state.
"""
from __future__ import annotations

from collections import deque

from .anchors import DIMENSIONS
from .encoder import l2_normalize

DEFAULT_WINDOW = 8     # bounded entries retained per dim (FIFO)
DEFAULT_MIN_COUNT = 3  # warm-up floor: below this a dim scores vs its seed


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class DimWindow:
    """One dimension's bounded rolling window of L2-normalized vectors."""

    __slots__ = ("dim", "window", "live_count", "freshest_ts")

    def __init__(self, dim: str, window_size: int = DEFAULT_WINDOW,
                 seed_vectors: tuple | list = ()) -> None:
        self.dim = dim
        self.window = deque(maxlen=max(1, int(window_size)))
        self.live_count = 0          # live (non-seed) reassemblies seen
        self.freshest_ts = 0.0
        for v in seed_vectors:
            self._push(v, 0.0, live=False)

    def _push(self, vec, ts: float, live: bool) -> None:
        self.window.append(l2_normalize(vec))
        self.freshest_ts = ts
        if live:
            self.live_count += 1

    @property
    def seeded(self) -> bool:
        """True while the window holds only seed vectors (no live reassembly)."""
        return self.live_count == 0

    def add(self, vec, ts: float) -> None:
        """Fold one live-derived vector into the window (ages the oldest out)."""
        self._push(vec, ts, live=True)

    def vectors(self) -> list:
        return list(self.window)

    def freshness(self, now: float = 0.0) -> dict:
        return {
            "count": len(self.window),
            "seeded": self.seeded,
            "live_count": self.live_count,
            "freshest_ts": self.freshest_ts,
            "age": max(0.0, float(now) - self.freshest_ts),
        }

    # -- derived-state (de)serialization (persistable, derived-only) ------ #
    def to_state(self) -> dict:
        return {
            "vectors": [list(v) for v in self.window],
            "live_count": self.live_count,
            "freshest_ts": self.freshest_ts,
        }

    @staticmethod
    def from_state(dim: str, state: dict, window_size: int = DEFAULT_WINDOW) -> "DimWindow":
        w = DimWindow(dim, window_size)
        for v in state.get("vectors", []):
            w.window.append(l2_normalize(v))
        w.live_count = int(state.get("live_count", 0))
        w.freshest_ts = float(state.get("freshest_ts", 0.0))
        return w


class RollingReference:
    """The 6-dim rolling reference: a bounded window per dimension.

    Seed each dim from the curated bank (list of L2-normalized anchor
    vectors); live observed turns then reassemble it. Scoring callers use
    :meth:`reference_for`, which returns the window vectors plus a warm-up
    flag (under the min-count floor the dim is not yet reassembled, so a
    scorer blends its seed bank rather than rendering 'no data').
    """

    def __init__(self, dims: tuple | list = DIMENSIONS,
                 window_size: int = DEFAULT_WINDOW,
                 seed_bank: dict | None = None,
                 state: dict | None = None) -> None:
        self.dims = tuple(dims)
        self.min_count_default = DEFAULT_MIN_COUNT
        if state:
            self.windows = {}
            for d in self.dims:
                s = state.get(d)
                self.windows[d] = (DimWindow.from_state(d, s, window_size)
                                   if s else DimWindow(d, window_size))
        else:
            seed_bank = seed_bank or {}
            self.windows = {d: DimWindow(d, window_size,
                                         tuple(seed_bank.get(d) or ()))
                            for d in self.dims}

    # -- reassembly ------------------------------------------------------- #
    def reassemble(self, dim: str, vec, ts: float) -> None:
        """Fold one live-derived per-dim vector into that dim's window."""
        window = self.windows.get(dim)
        if window is None:
            return
        window.add(vec, ts)

    # -- scoring reference ------------------------------------------------ #
    def reference_for(self, dim: str, min_count: int | None = None) -> tuple:
        """(vectors, warmed): the window's vectors + whether it has reassembled
        past the warm-up floor. Under the floor, a scorer blends its seed
        bank so the pane never reads 'no data yet' on a fresh profile."""
        window = self.windows.get(dim)
        if window is None:
            return [], False
        vectors = window.vectors()
        floor = DEFAULT_MIN_COUNT if min_count is None else int(min_count)
        warmed = (not window.seeded) and len(vectors) >= floor
        return vectors, warmed

    # -- dynamic-territory reads ----------------------------------------- #
    def freshness(self, now: float = 0.0) -> dict:
        """Per-dim freshness: how live / how stale the rolling reference is.

        This is the dynamic canary the pane reads: a dim that keeps
        reassembling has a small age + non-seed count; a dim that has
        stopped moving ages out. 'How fresh / how wide is my territory',
        not a frozen number."""
        return {d: self.windows[d].freshness(now) for d in self.dims}

    def ood_drift(self, per_dim_affinity: dict) -> float:
        """Out-of-territory drift: 1 - best per-dim affinity to the rolling
        reference. 0.0 = the turn matches at least one dim's recent territory;
        1.0 = it is far from every dim's reference (out-of-scope signal)."""
        best = 0.0
        for dim, aff in (per_dim_affinity or {}).items():
            if dim in self.windows:
                best = max(best, _clip01(aff))
        return round(1.0 - best, 4)

    # -- derived-state ----------------------------------------------------- #
    def to_state(self) -> dict:
        """Derived-only persisted state (per-dim window); reversible/rollback."""
        return {d: self.windows[d].to_state() for d in self.dims}
