"""Global GOAL trajectory model — pure, deterministic, no I/O and no LLM.

Owner-locked direction (2026-11-18): GOAL is the evaluation of the whole
task trajectory, not a per-tool similarity meter. Three evidence layers:
pre-tool intention, post-tool result, and later retrospective re-evaluation.

Only derived vectors / bounded metadata are used. Raw prompts, reasoning
and tool results never enter this module or persistence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Reuse the already-declared semantic thresholds instead of inventing new
# magic numbers: aligned >= good threshold, drift <= friction threshold.
ALIGN_THRESHOLD = 0.6
INTENTION_CONTEXT_WEIGHT = 0.5
DRIFT_THRESHOLD = 0.4
HISTORY_MAX = 32
OPERATION_WINDOW_MAX = 32
RECOVERY_DRIFT_STREAK = 2  # same "two consecutive" rule as the upset D6 model


def _norm(vector) -> list[float] | None:
    if not vector:
        return None
    try:
        v = [float(x) for x in vector]
    except (TypeError, ValueError):
        return None
    n = math.sqrt(sum(x * x for x in v))
    if n <= 0.0 or not math.isfinite(n):
        return None
    return [x / n for x in v]


def cosine(a, b) -> float | None:
    a, b = _norm(a), _norm(b)
    if a is None or b is None or len(a) != len(b):
        return None
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def blend_intention(prompt_vector, operation_vector) -> list[float] | None:
    """Pre-tool intention = prompt context BLENDED with operation semantics.

    Using the prompt vector alone makes every operation compare the goal
    with itself (cosine 1.0). Using only a generic operation label loses
    task context (everything looks like drift). The bounded blend keeps
    both signals without storing raw text or echoing the objective.
    """
    p, o = _norm(prompt_vector), _norm(operation_vector)
    if o is None:
        return None
    if p is None or len(p) != len(o):
        return o
    weight = INTENTION_CONTEXT_WEIGHT
    return _norm([weight * x + (1.0 - weight) * y for x, y in zip(p, o)])


@dataclass
class ObjectiveVersion:
    version: int
    vector: list[float]
    ts: float
    label: str | None = None


@dataclass
class ObjectiveState:
    version: int = 0
    vector: list[float] | None = None
    ts: float | None = None
    label: str | None = None
    history: list[ObjectiveVersion] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "version": self.version,
            "vector": list(self.vector) if self.vector else None,
            "ts": self.ts,
            "label": self.label,
            "history": [{"version": v.version, "vector": list(v.vector),
                         "ts": v.ts, "label": v.label} for v in self.history],
        }


def update_objective(state: ObjectiveState, prompt_vector, ts: float,
                     label: str | None = None,
                     new_topic: bool = False) -> ObjectiveState:
    """Evolve the global objective without erasing task continuity.

    A follow-up prompt that still matches the objective moves it slowly;
    a clearly new direction moves it faster but the old version is kept.
    """
    new = _norm(prompt_vector)
    if new is None:
        return state
    if state.vector and len(state.vector) == len(new):
        sim = cosine(state.vector, new) or 0.0
        # Linear continuity between the existing aligned/drift thresholds.
        if sim >= ALIGN_THRESHOLD:
            alpha = 0.20
        elif sim <= DRIFT_THRESHOLD:
            alpha = 0.80
        else:
            span = ALIGN_THRESHOLD - DRIFT_THRESHOLD
            alpha = 0.80 - 0.60 * ((sim - DRIFT_THRESHOLD) / span)
        if new_topic:
            alpha = max(alpha, 0.80)
        blended = _norm([alpha * n + (1.0 - alpha) * o
                         for n, o in zip(new, state.vector)])
        vector = blended if blended is not None else new
    else:
        # First objective: preserve the encoder's exact vector (do not
        # re-normalize) so hydration parity with pulse_goal stays exact.
        vector = [float(x) for x in prompt_vector]
    if state.vector is not None:
        state.history.append(ObjectiveVersion(
            state.version, list(state.vector), state.ts or ts, state.label))
        del state.history[:-HISTORY_MAX]
    state.version += 1
    state.vector = vector
    state.ts = float(ts)
    state.label = label
    return state


@dataclass
class OperationRecord:
    call_id: str
    tool: str
    operation: str
    vector: list[float] | None
    ts: float
    objective_version: int
    historical_assessment: float | None
    status: str = "pending"
    ended_ts: float | None = None
    current_assessment: float | None = None
    category: str = "pending"

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id, "tool": self.tool,
            "operation": self.operation, "vector": self.vector,
            "ts": self.ts, "objective_version": self.objective_version,
            "historical_assessment": self.historical_assessment,
            "status": self.status, "ended_ts": self.ended_ts,
            "current_assessment": self.current_assessment,
            "category": self.category,
        }


class TrajectoryEvaluator:
    """Bounded operation history + retrospective re-assessment."""

    def __init__(self, window: int = OPERATION_WINDOW_MAX):
        self.window = max(1, int(window))
        self.pending: dict[str, dict] = {}
        self.operations: list[OperationRecord] = []

    def add_intent(self, call_id: str, tool: str, operation: str,
                   vector, ts: float, objective: ObjectiveState) -> None:
        if not call_id:
            return
        self.pending[str(call_id)] = {
            "call_id": str(call_id), "tool": str(tool or "unknown"),
            "operation": str(operation or tool or ""),
            "vector": list(vector) if vector else None,
            "ts": float(ts),
            "objective_version": objective.version,
            "historical_assessment": _assessment(vector, objective.vector),
        }

    def complete(self, call_id: str, status: str, ts: float,
                 objective: ObjectiveState) -> OperationRecord | None:
        intent = self.pending.pop(str(call_id), None)
        if intent is None:
            return None
        rec = OperationRecord(
            call_id=intent["call_id"], tool=intent["tool"],
            operation=intent["operation"], vector=intent["vector"],
            ts=intent["ts"], objective_version=intent["objective_version"],
            historical_assessment=intent["historical_assessment"],
            status=str(status or "ok"), ended_ts=float(ts))
        rec.current_assessment = _assessment(rec.vector, objective.vector)
        rec.category = _category(rec.historical_assessment, status)
        self.operations.append(rec)
        del self.operations[:-self.window]
        self._retrospective()
        return rec

    def _retrospective(self) -> None:
        """A later aligned op can promote an earlier detour into a
        productive one when it is semantically close to it."""
        for i, rec in enumerate(self.operations):
            if rec.category != "exploring":
                continue
            for later in self.operations[i + 1:]:
                if later.category in ("drifting", "pending"):
                    continue
                sim = cosine(rec.vector, later.vector)
                if sim is not None and sim >= ALIGN_THRESHOLD:
                    rec.category = "productive_detour"
                    rec.current_assessment = max(
                        rec.current_assessment or 0.0,
                        later.current_assessment or 0.0)
                    break

    def reevaluate(self, objective: ObjectiveState) -> None:
        for rec in self.operations:
            rec.current_assessment = _assessment(rec.vector, objective.vector)
        self._retrospective()

    def signal(self) -> dict:
        if not self.operations:
            return {"value": None, "category": "empty", "support": {
                "count": 0, "required": RECOVERY_DRIFT_STREAK, "ratio": 0.0},
                "operation_count": 0, "drift_streak": 0, "detours": 0,
                "version": None, "history_count": 0}
        values = [r.current_assessment or 0.0 for r in self.operations]
        value = round(sum(values) / len(values), 4)
        cats = [r.category for r in self.operations]
        streak = 0
        if cats[-1] == "drifting":
            for cat in reversed(cats):
                if cat != "drifting":
                    break
                streak += 1
        else:
            # Recovery: the sustained drift preceded the latest aligned op.
            for cat in reversed(cats[:-1]):
                if cat != "drifting":
                    break
                streak += 1
        last = self.operations[-1]
        detours = sum(1 for r in self.operations
                      if r.category == "productive_detour")
        if streak >= RECOVERY_DRIFT_STREAK and last.category != "drifting":
            category = "recovering"
        elif streak >= RECOVERY_DRIFT_STREAK:
            category = "drifting"
        elif detours and last.category in ("aligned", "productive_detour"):
            category = "productive_detour"
        elif last.category == "exploring":
            category = "exploring"
        elif last.category == "drifting":
            category = "drifting"
        else:
            category = "aligned"
        count = len(self.operations)
        required = max(1, RECOVERY_DRIFT_STREAK)
        return {
            "value": value,
            "category": category,
            "support": {"count": count, "required": required,
                        "ratio": round(min(1.0, count / required), 4)},
            "operation_count": count,
            "drift_streak": streak,
            "detours": detours,
            "version": last.objective_version,
            "history_count": 0,
        }


def _assessment(vector, objective_vector) -> float | None:
    sim = cosine(vector, objective_vector)
    if sim is None:
        return None
    return round(max(0.0, min(1.0, sim)), 4)


def _category(assessment: float | None, status: str) -> str:
    if assessment is None:
        return "exploring"
    if str(status).lower() not in ("ok", "success", "done"):
        # A failure is objective evidence, not a claim about direction.
        return "exploring" if assessment >= DRIFT_THRESHOLD else "drifting"
    if assessment >= ALIGN_THRESHOLD:
        return "aligned"
    if assessment <= DRIFT_THRESHOLD:
        return "drifting"
    return "exploring"
