"""The 4-column PULSE view per tool row (S4).

Columns:
- LIVE     — current counts / last activity (registry rows, merged with
             the S1 session-state snapshot when provided).
- GOAL     — cosine of the goal capsule (last user prompt, encoded via
             the S2 backend) vs the row's prototypes / recent capsules.
- GOOD     — strongest representative similarity to the GOOD bank.
- FRICTION — same for the FRICTION bank (labeled + objective-friction
             are reported as separate sub-values, never conflated).

Neutral rule: an empty GOOD/FRICTION bank renders NEUTRAL — absence of
positive evidence is not negative evidence. Empty banks yield
``value=None, state='neutral'`` and display values are floored at 0.0,
so low similarity never renders as red/negative on an empty bank.
Unlabeled tools report ``state='learning'`` until min_labeled examples.
"""
from __future__ import annotations

from .prototypes import cosine, strongest_similarity

# C4 thresholds (pulse.thresholds.*); S1 settings defaults.
DEFAULT_THRESHOLDS = {"goal": 0.8, "good": 0.6, "friction": 0.4}


def neutral_cell(state: str = "neutral", value=None) -> dict:
    return {"value": value, "state": state, "level": "neutral"}


def _floor(value: float | None) -> float | None:
    """Cap floor: display similarity never drops below neutral 0.0."""
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _level(value, threshold, state) -> str:
    if value is None or state != "ready":
        return "neutral"
    return "strong" if value >= threshold else "weak"


def build_matrix(registry, prototypes, session_state=None,
                 goal_vector=None, goal_text: str | None = None,
                 backend=None, capsules_by_tool: dict | None = None,
                 thresholds: dict | None = None) -> list[dict]:
    """Build one row per registry tool (dynamic — no hardcoded list)."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if goal_vector is None and goal_text is not None and backend is not None:
        from .activity_encoder import encode_goal_text
        goal_vector = encode_goal_text(goal_text, backend)
    capsules_by_tool = capsules_by_tool or {}
    live_snapshot = {}
    if session_state is not None:
        try:
            live_snapshot = session_state.snapshot()
        except Exception:
            live_snapshot = {}

    rows = []
    for tool_row in registry.list_tools():
        tool = tool_row["tool_name"]
        bank = prototypes.bank(tool)
        state = prototypes.state(tool)
        labeled = prototypes.labeled_count(tool)

        # GOAL: goal capsule vs row prototypes (+ recent capsules when
        # prototypes are still empty, so a relevant goal still scores).
        reference = (bank["good"] + bank["friction"]
                     + bank["objective_friction"])
        if not reference:
            reference = [c.get("vector") for c in
                         capsules_by_tool.get(tool, [])
                         if isinstance(c, dict) and c.get("vector")]
        goal_raw = strongest_similarity(goal_vector, reference)
        goal_value = _floor(goal_raw)
        goal_cell = {"value": goal_value,
                     "state": "neutral" if goal_raw is None else state,
                     "level": _level(goal_value, thr["goal"], state)}

        # GOOD / FRICTION: strongest similarity to each bank; an empty
        # bank is NEUTRAL (value None), never negative/red.
        good_raw = strongest_similarity(goal_vector, bank["good"])
        good_value = _floor(good_raw)
        good_cell = {"value": good_value,
                     "state": "neutral" if good_raw is None else state,
                     "level": _level(good_value, thr["good"], state)}

        friction_user = strongest_similarity(goal_vector, bank["friction"])
        friction_obj = strongest_similarity(
            goal_vector, bank["objective_friction"])
        friction_parts = [v for v in (friction_user, friction_obj)
                          if v is not None]
        friction_raw = max(friction_parts) if friction_parts else None
        friction_value = _floor(friction_raw)
        friction_cell = {"value": friction_value,
                         "state": "neutral" if friction_raw is None else state,
                         "level": _level(friction_value, thr["friction"], state),
                         "user": _floor(friction_user),
                         "objective": _floor(friction_obj)}

        live = {
            "calls_started": tool_row["calls_started"],
            "calls_completed": tool_row["calls_completed"],
            "errors": tool_row["errors"],
            "last_seen": tool_row["last_seen"],
            "first_seen": tool_row["first_seen"],
            "global": live_snapshot,
        }
        rows.append({
            "tool_name": tool,
            "tool_id": tool_row["id"],
            "state": state,
            "labeled": labeled,
            "live": live,
            "goal": goal_cell,
            "good": good_cell,
            "friction": friction_cell,
        })
    return rows


def matrix_for_goal(goal_text, registry, prototypes, backend, **kw):
    """Convenience: encode the goal (worker side) then build the matrix."""
    from .activity_encoder import encode_goal_text
    return build_matrix(registry, prototypes, goal_vector=encode_goal_text(goal_text, backend),
                        backend=backend, **kw)
