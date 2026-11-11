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
MAX_STEP = 0.12          # interpreted-track hard per-update cap per dimension
INITIAL_VALUE = 0.5      # neutral prior per dimension
DEFAULT_RETAIN_DAYS = 90  # mirrors core/settings.py DEFAULTS; read via C4 key

# --- Session-track (observed) adaptive dynamics (spec §1-4) ---------------- #
# The observed value V follows a RECOMPUTED target on every observation with
# a confidence-gated speed. These are the owner's starting parameters, not a
# new feature scale and not an artificial confidence boost:
ALPHA_MAX = 0.55         # alpha_{d,t} = ALPHA_MAX * Q_{d,t}  (confidence-gated speed)
DELTA_MAX = 0.18         # hard per-observation step cap per dimension
CONF_BETA = 0.25         # displayed (session) confidence EMA: Qs=(1-b)*Qs+b*Q
STRONG_R_DEFAULT = 0.5   # default per-dimension strong-contrast threshold r_d


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
        # Three independent quantities (spec §5): target (where the latest
        # evidence points), value (where the dimension actually is now), and
        # confidence (how well the current direction is confirmed). None
        # substitutes another.
        self.targets = {d: INITIAL_VALUE for d in DIMENSIONS}


def strong_thresholds(strong_r: dict | float | None) -> dict:
    """Per-dimension strong-contrast thresholds r_d (never a forced-uniform
    assignment): a dict is used verbatim, a float applies to every dim, and
    None falls back to the default for each dim."""
    if strong_r is None:
        return {d: STRONG_R_DEFAULT for d in DIMENSIONS}
    if isinstance(strong_r, (int, float)):
        return {d: float(strong_r) for d in DIMENSIONS}
    out = {d: float(strong_r.get(d, STRONG_R_DEFAULT)) for d in DIMENSIONS}
    return out


class Portrait:
    """EMA portrait with observed / interpreted tracks. Features only."""

    def __init__(self, *, retain_days: int | None = None, max_evidence: int | None = None,
                 base_alpha: float = BASE_ALPHA, max_step: float = MAX_STEP,
                 alpha_max: float = ALPHA_MAX, delta_max: float = DELTA_MAX,
                 conf_beta: float = CONF_BETA,
                 strong_r: dict | float | None = None,
                 calibration: Calibration | None = None) -> None:
        self.retain_days = DEFAULT_RETAIN_DAYS if retain_days is None else int(retain_days)
        self.window = evidence_window_for(retain_days, max_evidence=max_evidence)
        self.base_alpha = float(base_alpha)
        self.max_step = float(max_step)          # interpreted-track step cap
        # Session-track (observed) adaptive dynamics (spec §2):
        self.alpha_max = float(alpha_max)        # alpha_{d,t} = alpha_max * Q_{d,t}
        self.delta_max = float(delta_max)        # hard per-observation step cap
        self.conf_beta = float(conf_beta)        # displayed-confidence EMA beta
        self.strong_r = strong_thresholds(strong_r)  # per-dim strong-contrast r_d
        self.calibration = calibration or DEFAULT_CALIBRATION
        self._observed = _Track()
        self._interpreted = _Track()
        # Feature observations only (float dicts) — bounded, FIFO, no text.
        self._evidence: deque = deque(maxlen=self.window)
        # Third view (patterns): latest score_turn output + accumulators.
        self._patterns_latest: dict | None = None
        self._dim_signals_latest: dict | None = None
        self._pattern_evidence: dict = {}
        self._pattern_turns = 0
        self._top_pattern: str | None = None
        self._top_streak = 0

    # -- internal EMA step (interpreted track) --------------------------- #
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

    # -- observed-track adaptive model (spec §1-4) ------------------------ #
    def _target(self, r: float, dim: str) -> float:
        """T_d = clamp(0.5 + R_d / (2*r_d), 0, 1) -- the full-range target.

        A monotone per-dimension calibration of the contrast R_d = S_d - C_d,
        anchored on the dim's strong-contrast threshold r_d: R=+r_d -> 1.0,
        R=0 -> 0.5, R=-r_d -> 0.0. It is RECOMPUTED on every observation --
        no fixed target exists, and a weak/neutral contrast reads ~0.5.
        """
        return _clip01(0.5 + r / (2.0 * self.strong_r[dim]))

    def _confidence(self, r: float, dim: str, *, agreement=None,
                    support=None, provenance=None) -> float:
        """Q_d = g_d(|R_d|, ...) -- reliability of the CURRENT direction.

        Symmetric in |R|: a strong NEGATIVE piece of evidence earns as much
        confidence as a strong positive one. The honest driver is the
        contrast magnitude against the dim's strong threshold; agreement /
        support / provenance are optional [0,1] multipliers that default to
        1.0 (absent -> no invented boost and no invented cut).
        """
        q = _clip01(abs(r) / self.strong_r[dim])
        if agreement is not None:
            q = _clip01(q * _clip01(agreement))
        if support is not None:
            q = _clip01(q * _clip01(support))
        if provenance is not None:
            q = _clip01(q * _clip01(provenance))
        return q

    def _step_observed(self, scores: dict, *, agreement=None, support=None,
                       provenance=None) -> None:
        """One observed-track observation: recompute target + confidence-
        gated value step + displayed-confidence EMA. The three independent
        quantities of spec §5 (target, value, confidence) never substitute
        one another; confidence is applied to the value update exactly once,
        and the desktop geometry receives the already-updated V via
        deviation = 2V - 1 (never re-multiplied by confidence)."""
        track = self._observed
        beta = self.conf_beta
        for dim in DIMENSIONS:
            if dim not in scores:
                continue
            entry = scores[dim]
            if isinstance(entry, dict):
                s = float(entry.get("S", 0.0))
                c = float(entry.get("C", 0.0))
                r = float(entry.get("R", s - c))
            else:
                s = float(entry)
                r = s  # legacy plain-S shape: no negative anchor -> R = S - 0
            target = self._target(r, dim)
            conf = self._confidence(r, dim, agreement=agreement,
                                    support=support, provenance=provenance)
            old = track.values[dim]
            alpha = self.alpha_max * conf                       # alpha_{d,t}
            delta = alpha * (target - old)
            delta = max(-self.delta_max, min(self.delta_max, delta))
            track.values[dim] = _clip01(old + delta)
            track.targets[dim] = target
            track.last_resemblance[dim] = r
            track.confidence[dim] = _clip01(
                (1.0 - beta) * track.confidence[dim] + beta * conf)
        track.evidence_count += 1

    # -- public updates --------------------------------------------------- #
    def update_observed(self, scores: dict, *, agreement=None, support=None,
                       provenance=None) -> None:
        """Apply one S2 resemblance observation through the adaptive model.

        ``scores`` maps dim -> {"S","C","R"} (the S2 ``resemblance_all``
        shape) or dim -> float S value (legacy; C=0 so R=S). Only floats
        are read; no text is accepted or stored. Per dim: target from the
        contrast R, confidence from |R| (optionally modulated by agreement /
        support / provenance), a confidence-gated value step, and the
        displayed-confidence EMA.
        """
        if not isinstance(scores, dict) or not any(d in scores for d in DIMENSIONS):
            return
        self._step_observed(scores, agreement=agreement, support=support,
                            provenance=provenance)
        self._evidence.append(
            {d: float(self._observed.targets[d]) for d in DIMENSIONS if d in scores})

    def update_observed_from_text(self, text: str, backend, anchor_vectors=None,
                                  reference=None, calibration=None) -> None:
        """Encode one prompt via S2 and fold its resemblance in. Text is not stored.

        With ``reference`` (a ``RollingReference``) the prompt is scored against
        the rolling per-dimension reference (seed bank + live reassembled window)
        instead of the frozen bank; without it the frozen-bank path is unchanged."""
        if reference is not None:
            from .resemblance import resemblance_all
            (vec,) = backend.encode([text])
            scores = resemblance_all(vec, reference.score_anchors(anchor_vectors))
        else:
            from .resemblance import resemblance_for_text
            scores = resemblance_for_text(text, backend, anchor_vectors=anchor_vectors)
        self.update_observed(scores)

    def update_interpreted(self, agent_values: dict) -> None:
        """Apply one agent-interpreted observation.

        Accepts plain ``{dim: float}`` (backward compatible) or
        ``{dim: (value, confidence)}`` pairs; values are clipped to
        [0, 1] and confidence is folded into the interpreted track's
        confidence. Unknown dims are ignored. This track never touches
        the observed track.
        """
        if not isinstance(agent_values, dict):
            raise TypeError("agent interpretation must be a plain dict {dim: float}")
        targets, cal_conf = {}, {}
        for dim in DIMENSIONS:
            if dim not in agent_values:
                continue
            entry = agent_values[dim]
            if isinstance(entry, (tuple, list)):
                if len(entry) != 2:
                    raise TypeError(
                        "agent interpretation entries must be float or (value, confidence)")
                targets[dim] = _clip01(entry[0])
                cal_conf[dim] = _clip01(entry[1])
            else:
                targets[dim] = _clip01(entry)
        if not targets:
            return
        self._step(self._interpreted, targets, cal_conf or None, None)

    def update_patterns(self, per_turn_scores: dict) -> None:
        """Fold one ``score_turn`` output into the patterns (third) view.

        Stores the latest output, accumulates per-pattern evidence counts
        (1 + the per-turn evidence field per pattern per turn), and tracks
        the consecutive-turn streak of the top pattern ('repeated' means
        the same top pattern name for 3+ consecutive turns). No text is
        accepted or stored; only the derived score dict.
        """
        if not isinstance(per_turn_scores, dict):
            raise TypeError("per-turn pattern scores must be a dict")
        patterns = per_turn_scores.get("patterns") or []
        if not patterns:
            return
        self._patterns_latest = per_turn_scores
        self._dim_signals_latest = per_turn_scores.get("dim_signals")
        self._pattern_turns += 1
        for entry in patterns:
            name = entry.get("name")
            if name is None:
                continue
            self._pattern_evidence[name] = (
                self._pattern_evidence.get(name, 0) + 1
                + int(entry.get("evidence") or 0))
        top_name = patterns[0].get("name")  # score_turn sorts agent desc
        if top_name is not None and top_name == self._top_pattern:
            self._top_streak += 1
        else:
            self._top_pattern = top_name
            self._top_streak = 1 if top_name is not None else 0

    def _patterns_view(self) -> dict:
        """Third-view shape; empty state is None/False, never neutral."""
        if self._patterns_latest is None:
            return {
                "patterns": None,
                "dim_signals": None,
                "perception_available": False,
                "repeated": False,
                "repeated_pattern": None,
                "pattern_turns": 0,
            }
        repeated = self._top_streak >= 3
        patterns = []
        for entry in self._patterns_latest.get("patterns") or []:
            name = entry.get("name")
            patterns.append({
                "name": name,
                "prompt_affinity": entry.get("prompt_affinity"),
                "agent_affinity": entry.get("agent_affinity"),
                "margin": entry.get("margin"),
                "amplification": entry.get("amplification"),
                "evidence": self._pattern_evidence.get(name, 0),
                "confidence": entry.get("confidence"),
                "repeated": bool(repeated and name == self._top_pattern),
            })
        return {
            "patterns": patterns,
            "dim_signals": self._dim_signals_latest,
            "perception_available": True,
            "repeated": repeated,
            "repeated_pattern": self._top_pattern if repeated else None,
            "pattern_turns": self._pattern_turns,
        }

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
            "targets": dict(track.targets),
            "evidence_count": track.evidence_count,
        }

    def snapshot(self) -> dict:
        """Renderer-ready view: normalized values + confidences, both tracks."""
        snap = {
            "values": dict(self._observed.values),
            "targets": dict(self._observed.targets),
            "confidences": dict(self._observed.confidence),
            "resemblance": dict(self._observed.last_resemblance),
            "observed": self._track_view(self._observed),
            "interpreted": self._track_view(self._interpreted),
            "evidence_count": self._observed.evidence_count,
            "evidence_window": self.window,
            "retain_days": self.retain_days,
        }
        snap.update(self._patterns_view())  # third view, additive only
        return snap

    def normalized(self) -> dict:
        """Flat {dim: value} + {dim: target} + {dim: confidence} for the
        renderer: the three independent quantities, none substituting another."""
        return {"values": dict(self._observed.values),
                "targets": dict(self._observed.targets),
                "confidences": dict(self._observed.confidence)}


def ema_step(old: float, o: float, alpha: float) -> float:
    """One pure EMA step: (1-alpha)*old + alpha*o (clipped to [0,1])."""
    return _clip01((1.0 - alpha) * old + alpha * o)
