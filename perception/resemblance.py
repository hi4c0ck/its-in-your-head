"""Resemblance (S_d/C_d/R_d) + separate calibration (S2).

Definitions (per dimension d, vs. the anchor bank):
    S_d = mean cosine to the positive anchor set        in [-1, 1]
    C_d = mean cosine to the negative anchor set        in [-1, 1]
    R_d = S_d - C_d                                   in [-2, 2]

R_d (resemblance) and confidence are SEPARATE numbers, always.
Confidence comes from the calibration harness below — a monotonic mapping
``R_d -> [0, 1]`` fitted on a small labeled fixture corpus. Raw cosine is
never displayed as a percentage (callers show R and a calibrated
confidence with its own label).
"""
from __future__ import annotations

import math

from .anchors import DIMENSIONS, default_bank, get_anchor_vectors

# --------------------------------------------------------------------------- #
# Cosine + resemblance
# --------------------------------------------------------------------------- #

def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0  # empty text / zero vector: no signal, not an error
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot / (na * nb)))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def score_dimension(vector: list[float], positive: list[list[float]],
                    negative: list[list[float]]) -> dict:
    """One dimension. S and C are each a MEAN cosine (order-independent)."""
    s = _mean([cosine(vector, v) for v in positive])
    c = _mean([cosine(vector, v) for v in negative])
    return {"S": s, "C": c, "R": s - c}


def resemblance_all(vector: list[float], anchor_vectors: dict) -> dict[str, dict]:
    """All 6 dimensions, in pinned order."""
    return {
        dim: score_dimension(vector, anchor_vectors[dim]["positive"],
                             anchor_vectors[dim]["negative"])
        for dim in DIMENSIONS
    }


def resemblance_for_text(text: str, backend, anchor_vectors: dict | None = None,
                         bank=None, data_dir=None) -> dict[str, dict]:
    vectors = anchor_vectors if anchor_vectors is not None else \
        get_anchor_vectors(backend, bank=bank, data_dir=data_dir)
    (vec,) = backend.encode([text])
    return resemblance_all(vec, vectors)


# --------------------------------------------------------------------------- #
# Calibration harness (confidence, NOT resemblance)
# --------------------------------------------------------------------------- #

# Fixture: (text, {dim: True if that dimension's trait is clearly present}).
# Kept small and labeled by hand; used only to fit the monotonic mapping.
LABELLED_FIXTURE: list[tuple[str, dict[str, bool]]] = [
    ("Give me the short answer, no preamble.", {"directness": True}),
    ("Walk me through your reasoning step by step with full background.", {"directness": False}),
    ("Here is the full background, logs, config and the exact error.", {"context_richness": True}),
    ("Fix it.", {"context_richness": False}),
    ("Brainstorm every option, even wild ones.", {"exploration": True}),
    ("Just do the one thing, pick the obvious answer.", {"exploration": False}),
    ("Compare pros and cons and weigh the evidence with numbers.", {"analytical_depth": True}),
    ("Quick fix, no deep dive, just patch it.", {"analytical_depth": False}),
    ("Revise that, iterate and refine it step by step.", {"iteration": True}),
    ("One shot only, final answer, no revisions.", {"iteration": False}),
    ("Keep this narrow: only the auth module, one function.", {"scope": True}),
    ("Rework the whole system, overhaul every layer at once.", {"scope": False}),
]


class Calibration:
    """Monotonic R -> [0,1] mapping (logistic); fitted, not hand-waved."""

    def __init__(self, midpoint: float = 0.0, slope: float = 6.0) -> None:
        self.midpoint = midpoint
        self.slope = slope

    def confidence(self, r: float) -> float:
        z = self.slope * (r - self.midpoint)
        return 1.0 / (1.0 + math.exp(-z))  # strictly increasing in R

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Calibration(midpoint={self.midpoint:.4f}, slope={self.slope:.4f})"


DEFAULT_CALIBRATION = Calibration()


def fit_calibration(pairs: list[tuple[float, bool]], slope: float = 6.0) -> Calibration:
    """Fit only the midpoint (midpoint between mean R of pos/neg); slope fixed.

    With no pairs, returns the default. Monotonic by construction.
    """
    pos = [r for r, y in pairs if y]
    neg = [r for r, y in pairs if not y]
    if not pos or not neg:
        return Calibration(0.0, slope)
    midpoint = (_mean(pos) + _mean(neg)) / 2
    return Calibration(midpoint=midpoint, slope=slope)


def fit_from_fixture(backend, anchor_vectors: dict | None = None, **kw) -> Calibration:
    """Build R values for the fixture via the pinned resemblance math, then fit."""
    vectors = anchor_vectors if anchor_vectors is not None else get_anchor_vectors(backend, **kw)
    pairs: list[tuple[float, bool]] = []
    for text, labels in LABELLED_FIXTURE:
        scores = resemblance_for_text(text, backend, anchor_vectors=vectors)
        for dim, label in labels.items():
            pairs.append((scores[dim]["R"], label))
    return fit_calibration(pairs)


def confidence_profile(scores: dict[str, dict], calibration: Calibration | None = None) -> dict[str, float]:
    cal = calibration or DEFAULT_CALIBRATION
    return {dim: cal.confidence(scores[dim]["R"]) for dim in DIMENSIONS}


def portrait_for_text(text: str, backend, anchor_vectors: dict | None = None,
                      calibration: Calibration | None = None, **kw) -> dict:
    """Separate numbers: resemblance (R) and confidence stay distinct keys."""
    scores = resemblance_for_text(text, backend, anchor_vectors=anchor_vectors, **kw)
    cal = calibration or fit_from_fixture(backend, anchor_vectors=anchor_vectors, **kw) \
        if calibration is None else calibration
    conf = confidence_profile(scores, cal)
    return {
        "resemblance": {d: scores[d]["R"] for d in DIMENSIONS},
        "S": {d: scores[d]["S"] for d in DIMENSIONS},
        "C": {d: scores[d]["C"] for d in DIMENSIONS},
        "confidence": conf,  # calibrated [0,1], never a raw cosine
    }
