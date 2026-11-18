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

# Reuse the already-declared semantic thresholds instead of inventing new
# magic numbers: aligned >= good threshold, drift <= friction threshold.
ALIGN_THRESHOLD = 0.6
INTENTION_CONTEXT_WEIGHT = 0.5
DRIFT_THRESHOLD = 0.4
HISTORY_MAX = 32
OPERATION_WINDOW_MAX = 32
RECOVERY_DRIFT_STREAK = 2  # same "two consecutive" rule as the upset D6 model
PENDING_MAX = 64
PENDING_TTL_S = 300.0


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


def blend_intention(prompt_vector, operation_vector,
                    objective_vector=None) -> list[float] | None:
    """Pre-tool intention = prompt context BLENDED with operation semantics.

    The prompt vector alone echoes the objective (cosine 1.0); the operation
    label alone loses task context. A 0.5 blend is safe ONLY when the
    operation itself has some relation to the objective: an unrelated
    operation must not be promoted by prompt-context admixture alone.
    """
    p, o = _norm(prompt_vector), _norm(operation_vector)
    if o is None:
        return None
    if p is None or len(p) != len(o):
        return o
    if objective_vector is not None:
        op_sim = cosine(o, objective_vector)
        # No measurement or a below-drift relation: keep the operation's
        # own semantics instead of rescuing it with the prompt vector.
        if op_sim is None or op_sim < DRIFT_THRESHOLD:
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


def _coerce_objective(value) -> ObjectiveState:
    """Accept an ObjectiveState or the dashboard's plain dict snapshot."""
    if isinstance(value, ObjectiveState):
        return value
    if not isinstance(value, dict):
        return ObjectiveState()
    hist = []
    for h in value.get("history") or []:
        if h.get("vector") is None:
            continue
        hist.append(ObjectiveVersion(
            int(h.get("version") or 0), list(h["vector"]),
            float(h.get("ts") or 0.0), h.get("label")))
    vec = value.get("vector")
    return ObjectiveState(
        version=int(value.get("version") or 0),
        vector=list(vec) if vec is not None else None,
        ts=float(value["ts"]) if value.get("ts") is not None else None,
        label=value.get("label"), history=hist)


def update_objective(state: ObjectiveState, prompt_vector, ts: float,
                     label: str | None = None,
                     new_topic: bool = False) -> ObjectiveState:
    """Evolve the global objective without erasing task continuity."""
    new = _norm(prompt_vector)
    if new is None:
        return state
    if state.vector and len(state.vector) == len(new):
        sim = cosine(state.vector, new) or 0.0
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
        # First objective: preserve the encoder's exact vector.
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
    result_assessment: float | None = None
    current_assessment: float | None = None
    historical_category: str = "pending"
    category: str = "pending"

    @property
    def current_category(self) -> str:
        return self.category

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id, "tool": self.tool,
            "operation": self.operation, "vector": self.vector,
            "ts": self.ts, "objective_version": self.objective_version,
            "historical_assessment": self.historical_assessment,
            "status": self.status, "ended_ts": self.ended_ts,
            "result_assessment": self.result_assessment,
            "current_assessment": self.current_assessment,
            "historical_category": self.historical_category,
            "category": self.category,
            "current_category": self.category,
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
        now = float(ts)
        expired_before = now - PENDING_TTL_S
        for pending_id, pending in list(self.pending.items()):
            if float(pending.get("ts") or 0.0) < expired_before:
                self.pending.pop(pending_id, None)
        key = str(call_id)
        if key not in self.pending and len(self.pending) >= PENDING_MAX:
            oldest = min(self.pending,
                         key=lambda k: float(self.pending[k].get("ts") or 0.0))
            self.pending.pop(oldest, None)
        self.pending[key] = {
            "call_id": key, "tool": str(tool or "unknown"),
            "operation": str(operation or tool or ""),
            "vector": list(vector) if vector else None,
            "ts": now,
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
        # RESULT is a separate evidence layer: a successful call is not
        # progress by itself; only its observed alignment counts.
        rec.result_assessment = _assessment(rec.vector, objective.vector)
        rec.current_assessment = rec.result_assessment
        rec.historical_category = _category(
            rec.historical_assessment, rec.status)
        rec.category = _category(rec.current_assessment, rec.status)
        self.operations.append(rec)
        del self.operations[:-self.window]
        self._retrospective()
        return rec

    def _retrospective(self) -> None:
        """Promote a detour only after CONFIRMED later progress.

        Similarity between two operations alone is not enough: the later
        operation must itself be aligned with the current objective and
        improve on the earlier assessment.
        """
        for i, rec in enumerate(self.operations):
            if rec.category != "exploring":
                continue
            for later in self.operations[i + 1:]:
                if later.category not in ("aligned", "productive_detour"):
                    continue
                if later.current_assessment is None:
                    continue
                if later.current_assessment < ALIGN_THRESHOLD:
                    continue
                if later.current_assessment <= (rec.current_assessment or 0.0):
                    continue
                sim = cosine(rec.vector, later.vector)
                if sim is not None and sim >= ALIGN_THRESHOLD:
                    rec.category = "productive_detour"
                    break

    def reevaluate(self, objective: ObjectiveState) -> None:
        for rec in self.operations:
            rec.current_assessment = _assessment(rec.vector, objective.vector)
            rec.category = _category(rec.current_assessment, rec.status)
        self._retrospective()

    def signal(self, objective: ObjectiveState | None = None) -> dict:
        obj = _coerce_objective(objective) if objective is not None else None
        if not self.operations:
            return {"value": None, "category": "empty", "support": {
                "count": 0, "required": RECOVERY_DRIFT_STREAK, "ratio": 0.0},
                "operation_count": 0, "drift_streak": 0, "detours": 0,
                "version": obj.version if obj else None,
                "history_count": len(obj.history) if obj else 0}
        # Missing measurements are NOT zeroes: they are excluded.
        available = [r.current_assessment for r in self.operations
                     if r.current_assessment is not None]
        value = (round(sum(available) / len(available), 4)
                 if available else None)
        cats = [r.category for r in self.operations]
        streak = 0
        if cats[-1] == "drifting":
            for cat in reversed(cats):
                if cat != "drifting":
                    break
                streak += 1
        else:
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
            "version": obj.version if obj else last.objective_version,
            "history_count": len(obj.history) if obj else 0,
        }


def signal_from_state(objective, operations) -> dict:
    """Shared evaluator for the live engine and the DB fallback.

    Both sides feed the same bounded snapshot and get the same signal
    fields: value, version, history_count, support, drift_streak, detours.
    """
    obj = _coerce_objective(objective)
    ev = TrajectoryEvaluator()
    records = []
    for d in operations or []:
        vec = d.get("vector")
        rec = OperationRecord(
            call_id=str(d.get("call_id") or ""),
            tool=str(d.get("tool") or "unknown"),
            operation=str(d.get("operation") or ""),
            vector=list(vec) if vec is not None else None,
            ts=float(d.get("ts") or 0.0),
            objective_version=int(d.get("objective_version") or 0),
            historical_assessment=d.get("historical_assessment"),
            status=str(d.get("status") or "ok"),
            ended_ts=d.get("ended_ts"),
            result_assessment=d.get("result_assessment"),
            current_assessment=d.get("current_assessment"),
            historical_category=str(d.get("historical_category")
                                     or d.get("category") or "pending"),
            category=str(d.get("current_category") or d.get("category")
                         or "pending"))
        records.append(rec)
    ev.operations = records
    if records:
        ev.reevaluate(obj)
    return ev.signal(obj)


def _assessment(vector, objective_vector) -> float | None:
    sim = cosine(vector, objective_vector)
    if sim is None:
        return None
    # A negative similarity is a MEASUREMENT of opposition; it maps to the
    # bottom of the range. A missing measurement stays None.
    return round(max(0.0, min(1.0, sim)), 4)


def _category(assessment: float | None, status: str) -> str:
    ok = str(status).lower() in ("ok", "success", "done")
    if assessment is None:
        return "exploring"
    if not ok:
        # Failure is objective evidence; it cannot be progress, but a
        # well-directed attempt is not automatically drift either.
        return "exploring" if assessment >= ALIGN_THRESHOLD else "drifting"
    if assessment >= ALIGN_THRESHOLD:
        return "aligned"
    if assessment <= DRIFT_THRESHOLD:
        return "drifting"
    return "exploring"
