"""6-dim portrait vector with per-dimension EMA (S3).

Per dimension d, per update::

    P_new = (1 - alpha) * P_old + alpha * O
    O     = (S_d + 1) / 2          # S_d is the S2 resemblance S value in [-1, 1]

``alpha`` is bounded by the evidence count and by consistency (agreement
between O and the current P, in [0, 1])::

    alpha = BASE_ALPHA * (0.5 + 0.5 * consistency) / (1 + n_capped / window)

where ``n_capped = min(evidence_count, evidence_window)``. Every single
update is additionally clipped to ``MAX_STEP`` per dimension, so one
criticism can never move a dimension by more than the cap, while repeated
independent evidence accumulates beyond it.

Two separate tracks are kept, never merged silently:

- ``observed``    — derived from S2 resemblance scores (``update_observed``).
- ``interpreted`` — a plain ``{dim: float}`` dict the caller provides
  (``update_interpreted``); it is never scraped from anywhere and never
  overwrites the observed track.

Confidence is a *separate* calibrated number per dimension
(``Calibration.confidence(R_d)`` from S2, folded in Bayesian-style), always
in [0, 1] and never the raw cosine / resemblance value.

Bounded evidence window: at most ``evidence_window`` feature observations
(dicts of floats) are retained, FIFO-evicted. The default window is derived
from C4 ``observation.retain_days`` (one evidence slot per retained day) —
the retention semantics apply to evidence *count*, never to raw text.
No raw prompt text or reasoning is stored anywhere in this module.
"""
from __future__ import annotations

from collections import deque

from .anchors import DIMENSIONS
from .resemblance import Calibration, DEFAULT_CALIBRATION

BASE_ALPHA = 0.20
MAX_STEP = 0.12          # hard per-update cap per dimension (stability gate)
INITIAL_VALUE = 0.5      # neutral prior per dimension
DEFAULT_RETAIN_DAYS = 90  # mirrors core/settings.py DEFAULTS; read via C4 key


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def evidence_window_for(retain_days: int | None = None, *, max_evidence: int | None = None) -> int:
    """Bounded evidence window from C4 retain_days (count, not text)."""
    if max_evidence is not None:
        return max(1, int(max_evidence))
    days = DEFAULT_RETAIN_DAYS if retain_days is None else int(retain_days)
    return max(1, days)


def alpha_for(evidence_count: int, consistency: float, window: int,
              base_alpha: float = BASE_ALPHA) -> float:
    """Bounded step size: evidence count + consistency (see module docstring)."""
    n_capped = min(max(0, int(evidence_count)), max(1, int(window)))
    consistency = _clip01(consistency)
    return base_alpha * (0.5 + 0.5 * consistency) / (1.0 + n_capped / max(1, window))


class _Track:
    def __init__(self) -> None:
        self.values = {d: INITIAL_VALUE for d in DIMENSIONS}
        self.confidence = {d: 0.0 for d in DIMENSIONS}
        self.last_resemblance = {d: 0.0 for d in DIMENSIONS}  # R_d, separate number
        self.evidence_count = 0


class Portrait:
    """EMA portrait with observed / interpreted tracks. Features only."""

    def __init__(self, *, retain_days: int | None = None, max_evidence: int | None = None,
                 base_alpha: float = BASE_ALPHA, max_step: float = MAX_STEP,
                 calibration: Calibration | None = None) -> None:
        self.retain_days = DEFAULT_RETAIN_DAYS if retain_days is None else int(retain_days)
        self.window = evidence_window_for(retain_days, max_evidence=max_evidence)
        self.base_alpha = float(base_alpha)
        self.max_step = float(max_step)
        self.calibration = calibration or DEFAULT_CALIBRATION
        self._observed = _Track()
        self._interpreted = _Track()
        # Feature observations only (float dicts) — bounded, FIFO, no text.
        self._evidence: deque = deque(maxlen=self.window)

    # -- internal EMA step shared by both tracks ------------------------- #
    def _step(self, track: _Track, targets: dict, cal_conf: dict | None,
              resemblances: dict | None) -> None:
        for dim in DIMENSIONS:
            if dim not in targets:
                continue
            o = _clip01(targets[dim])
            old = track.values[dim]
            consistency = 1.0 - abs(o - old)
            alpha = alpha_for(track.evidence_count, consistency, self.window,
                              self.base_alpha)
            delta = alpha * (o - old)
            delta = max(-self.max_step, min(self.max_step, delta))
            track.values[dim] = _clip01(old + delta)
            if resemblances and dim in resemblances:
                track.last_resemblance[dim] = float(resemblances[dim])
            if cal_conf is not None and dim in cal_conf:
                c = _clip01(cal_conf[dim])
                # fold calibrated confidence in; stays in [0,1), distinct
                # from both O and the raw resemblance R.
                track.confidence[dim] = _clip01(
                    1.0 - (1.0 - track.confidence[dim]) * (1.0 - 0.5 * c))
        track.evidence_count += 1

    # -- public updates --------------------------------------------------- #
    def update_observed(self, scores: dict) -> None:
        """Apply one S2 resemblance observation.

        ``scores`` maps dim -> {"S": float, "C": float, "R": float} (the S2
        ``resemblance_all`` shape) or dim -> float S value. Only floats are
        read; no text is accepted or stored.
        """
        targets, cal_conf, resemb = {}, {}, {}
        for dim in DIMENSIONS:
            if dim not in scores:
                continue
            entry = scores[dim]
            if isinstance(entry, dict):
                s = float(entry.get("S", 0.0))
                r = float(entry.get("R", s - float(entry.get("C", 0.0))))
            else:
                s, r = float(entry), float(entry)
            targets[dim] = (s + 1.0) / 2.0
            cal_conf[dim] = self.calibration.confidence(r)
            resemb[dim] = r
        if not targets:
            return
        self._step(self._observed, targets, cal_conf, resemb)
        self._evidence.append({d: float(targets[d]) for d in DIMENSIONS if d in targets})

    def update_observed_from_text(self, text: str, backend, anchor_vectors=None,
                                  calibration=None) -> None:
        """Encode one prompt via S2 and fold its resemblance in. Text is not stored."""
        from .resemblance import resemblance_for_text
        scores = resemblance_for_text(text, backend, anchor_vectors=anchor_vectors)
        self.update_observed(scores)

    def update_interpreted(self, agent_values: dict) -> None:
        """Apply one agent-interpreted observation: plain {dim: float} in [0, 1].

        Caller-provided only. Values are clipped to [0, 1]; unknown dims are
        ignored. This track never touches the observed track.
        """
        if not isinstance(agent_values, dict):
            raise TypeError("agent interpretation must be a plain dict {dim: float}")
        targets = {d: _clip01(agent_values[d]) for d in DIMENSIONS if d in agent_values}
        if not targets:
            return
        self._step(self._interpreted, targets, None, None)

    # -- views ------------------------------------------------------------ #
    @property
    def evidence(self) -> list:
        """Retained feature observations (bounded; floats only, no text)."""
        return list(self._evidence)

    def _track_view(self, track: _Track) -> dict:
        return {
            "values": dict(track.values),
            "confidence": dict(track.confidence),
            "resemblance": dict(track.last_resemblance),
            "evidence_count": track.evidence_count,
        }

    def snapshot(self) -> dict:
        """Renderer-ready view: normalized values + confidences, both tracks."""
        return {
            "values": dict(self._observed.values),
            "confidences": dict(self._observed.confidence),
            "resemblance": dict(self._observed.last_resemblance),
            "observed": self._track_view(self._observed),
            "interpreted": self._track_view(self._interpreted),
            "evidence_count": self._observed.evidence_count,
            "evidence_window": self.window,
            "retain_days": self.retain_days,
        }

    def normalized(self) -> dict:
        """Flat {dim: value} + {dim: confidence} for the renderer."""
        return {"values": dict(self._observed.values),
                "confidences": dict(self._observed.confidence)}


def ema_step(old: float, o: float, alpha: float) -> float:
    """One pure EMA step: (1-alpha)*old + alpha*o (clipped to [0,1])."""
    return _clip01((1.0 - alpha) * old + alpha * o)
