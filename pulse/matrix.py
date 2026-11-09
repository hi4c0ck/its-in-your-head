"""The 4-column PULSE view per tool row (S4).

Columns:
- LIVE     — current counts / last activity (registry rows, merged with
             the S1 session-state snapshot when provided).
- GOAL     — cosine of the goal capsule (last user prompt, encoded via
             the S2 backend) vs the row's prototypes / recent capsules.
- GOOD     — strongest representative similarity to the GOOD bank.
- FRICTION — same for the FRICTION bank (labeled + objective-friction
             are reported as separate sub-values, never conflated).

Readiness is PER CELL (tool x metric): each semantic cell carries its
own ``state`` (empty / learning / provisional / ready) and ``support``
(count / required / ratio) from its own independent evidence bank —
GOAL counts reference vectors (prototype banks, else recent capsules),
GOOD counts GOOD prototypes, FRICTION-user and FRICTION-objective are
counted separately and never summed. LIVE has no learned-sample
threshold (operational data).

``level`` describes the SCORE, ``state`` describes the EVIDENCE: a
computable score stays visible as ``provisional`` below threshold with
its true level (strong/weak) — level is NEVER forced neutral merely
because state is not ready. An empty bank with a present goal vector is
``learning`` (value None); with no goal vector it is ``empty``.

Legacy projection: the row-level ``state`` / ``labeled`` fields keep
their pre-Stage-2 meaning (ready iff labeled >= pulse.min_labeled)
for older consumers; new consumers must read per-cell state.
"""
from __future__ import annotations

from .prototypes import cosine, strongest_similarity
from .momentum import surge_values, empty_momentum

# C4 thresholds (pulse.thresholds.*); S1 settings defaults.
DEFAULT_THRESHOLDS = {"goal": 0.8, "good": 0.6, "friction": 0.4}


def neutral_cell(state: str = "neutral", value=None) -> dict:
    return {"value": value, "state": state, "level": "neutral"}


def _floor(value: float | None) -> float | None:
    """Cap floor: display similarity never drops below neutral 0.0."""
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _level(value, threshold) -> str:
    """Score classification — independent of readiness state. A computable
    score keeps its strong/weak level even while provisional; only a
    missing score is neutral."""
    if value is None:
        return "neutral"
    return "strong" if value >= threshold else "weak"


def _support(count: int, required: int) -> dict:
    """Evidence indicator (NOT a statistical confidence interval)."""
    req = max(1, int(required))
    ratio = min(1.0, int(count) / req)
    return {"count": int(count), "required": req, "ratio": round(ratio, 4)}


def _state(has_context: bool, count: int, required: int) -> str:
    """PulseState from evaluation context + own-bank evidence count."""
    if not has_context:
        return "empty"
    if not count:
        return "learning"
    return "ready" if count >= max(1, int(required)) else "provisional"


def _required(prototypes, attr: str, legacy: int) -> int:
    """Per-category readiness minimum from the store, legacy fallback."""
    value = getattr(prototypes, attr, None)
    return max(1, int(value if value is not None else legacy))


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
    legacy_min = int(getattr(prototypes, "min_labeled", 3) or 3)
    goal_req = _required(prototypes, "goal_min_references", legacy_min)
    good_req = _required(prototypes, "good_min_samples", legacy_min)
    user_fric_req = _required(prototypes, "user_friction_min_samples",
                              legacy_min)
    obj_fric_req = _required(prototypes, "objective_friction_min_samples",
                             legacy_min)
    has_goal = goal_vector is not None
    for tool_row in registry.list_tools():
        tool = tool_row["tool_name"]
        bank = prototypes.bank(tool)
        # Legacy row projection (pre-Stage-2 semantics for old
        # consumers; new consumers read per-cell state below).
        state = prototypes.state(tool)
        labeled = prototypes.labeled_count(tool)

        # GOAL: goal capsule vs row prototypes (+ recent capsules when
        # prototypes are still empty, so a relevant goal still scores).
        # Support counts REFERENCE VECTORS, not feedback labels.
        reference = (bank["good"] + bank["friction"]
                     + bank["objective_friction"])
        if not reference:
            reference = [c.get("vector") for c in
                         capsules_by_tool.get(tool, [])
                         if isinstance(c, dict) and c.get("vector")]
        goal_raw = strongest_similarity(goal_vector, reference)
        goal_value = _floor(goal_raw)
        goal_support = _support(len(reference), goal_req)
        goal_cell = {"value": goal_value,
                     "state": _state(has_goal, len(reference), goal_req),
                     "level": _level(goal_value, thr["goal"]),
                     "support": goal_support,
                     "support_count": goal_support["count"],
                     "readiness_threshold": goal_support["required"]}

        # GOOD: strongest similarity to the GOOD bank only; support is
        # the GOOD-bank count alone.
        good_count = len(bank["good"])
        good_raw = strongest_similarity(goal_vector, bank["good"])
        good_value = _floor(good_raw)
        good_support = _support(good_count, good_req)
        good_cell = {"value": good_value,
                     "state": _state(has_goal, good_count, good_req),
                     "level": _level(good_value, thr["good"]),
                     "support": good_support,
                     "support_count": good_support["count"],
                     "readiness_threshold": good_support["required"]}

        # FRICTION: user and objective sources evaluated independently
        # (never summed); the combined cell follows the DOMINANT
        # source. "user"/"objective" stay numeric-or-None (legacy).
        user_count = len(bank["friction"])
        obj_count = len(bank["objective_friction"])
        friction_user = strongest_similarity(goal_vector, bank["friction"])
        friction_obj = strongest_similarity(
            goal_vector, bank["objective_friction"])
        user_value = _floor(friction_user)
        obj_value = _floor(friction_obj)
        user_state = _state(has_goal, user_count, user_fric_req)
        obj_state = _state(has_goal, obj_count, obj_fric_req)
        user_support = _support(user_count, user_fric_req)
        obj_support = _support(obj_count, obj_fric_req)
        scored = []
        if user_value is not None:
            scored.append(("user", user_value, user_support["ratio"]))
        if obj_value is not None:
            scored.append(("objective", obj_value, obj_support["ratio"]))
        dominant = None
        friction_value = None
        friction_state = _state(has_goal, 0, user_fric_req)
        friction_support = _support(0, user_fric_req)
        if scored:
            # Greater score wins; tie -> greater support ratio;
            # both equal -> user (key puts user first via the flag).
            dominant, friction_value, _ratio = max(
                scored, key=lambda s: (s[1], s[2], s[0] == "user"))
            if dominant == "user":
                friction_state, friction_support = user_state, user_support
            else:
                friction_state, friction_support = obj_state, obj_support
        friction_cell = {"value": friction_value,
                         "state": friction_state,
                         "level": _level(friction_value, thr["friction"]),
                         "user": user_value,
                         "objective": obj_value,
                         "dominant_source": dominant,
                         "user_state": user_state,
                         "objective_state": obj_state,
                         "user_support": user_support,
                         "objective_support": obj_support,
                         "support": friction_support,
                         "support_count": friction_support["count"],
                         "readiness_threshold": friction_support["required"]}

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

    # Momentum (Tier-0 positive feedback): a rich-get-richer surge signal
    # from recent vs cumulative activity, so a tool that is hotter than its
    # long-run baseline pops to the front. Pure + deterministic (no clock).
    recent_counts = {
        tool: sum(1 for c in (capsules_by_tool.get(tool) or [])
                  if isinstance(c, dict) and c.get("vector") is not None)
        for tool in [r["tool_name"] for r in rows]
    }
    cum_counts = {r["tool_name"]: int(r["live"]["calls_completed"])
                  for r in rows}
    momentum = surge_values(recent_counts, cum_counts)
    for row in rows:
        row["momentum"] = momentum.get(row["tool_name"], empty_momentum())

    return rows


def matrix_for_goal(goal_text, registry, prototypes, backend, **kw):
    """Convenience: encode the goal (worker side) then build the matrix."""
    from .activity_encoder import encode_goal_text
    return build_matrix(registry, prototypes, goal_vector=encode_goal_text(goal_text, backend),
                        backend=backend, **kw)
