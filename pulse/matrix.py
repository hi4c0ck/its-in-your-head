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

import math
import time

from .prototypes import cosine, strongest_similarity
from .momentum import surge_values, empty_momentum
from .velocity import PULSE_VELOCITY_WINDOW_S

# PULSE scoring is cross-dim: a 256-d (sketch/fallback) and a 384-d (Tier-1)
# vector meet at the lower dim inside ``pulse.prototypes.cosine`` (via
# perception.canonical.cross_dim_cosine), so every stored vector stays
# reachable from either encoder and no write is ever stored lossily.

# C4 thresholds (pulse.thresholds.*); S1 settings defaults.
DEFAULT_THRESHOLDS = {"goal": 0.8, "good": 0.6, "friction": 0.4}

# P2 accumulate model: GOOD/FRICTION value is a 0-baseline call-accumulation,
# now TIME-DECAYED. Each channel-matched recent capsule contributes a recency
# weight exp(-ln2 * age / PULSE_VALUE_HALF_LIFE_S) that HALVES every
# PULSE_VALUE_HALF_LIFE_S seconds; the value is the MEAN of those weights over
# the matching capsules (0 when there is no bank or no matching call). So the
# value decays on its own as capsules age: a tool that was active but is no
# longer called fades toward 0 after a few half-lives. The count-based FIFO
# deque alone would NOT decay (no new calls -> the matching capsules stay in
# the deque -> the count is static), so recency (the capsules' own ts) is the
# structural driver. This is a DATA decay clock (the capsules' ts + the
# request's now), not a per-tool stateful clock: the pattern layer stays pure
# + stateless. CONCRETE METRIC: a tool's value halves every
# PULSE_VALUE_HALF_LIFE_S -- 1h means 0.98 -> 0.49 after an hour, 0.25 after
# two, ~0 after five.
PULSE_VALUE_HALF_LIFE_S = 3600
# Kept for the legacy saturation shape / call sites that still read a count
# half-life (n=3 -> 0.31, n=6 -> 0.53 at 8). The time-decay above supersedes
# it for the display value; this stays as the count-scale reference.
PULSE_ACCUM_HALF_LIFE = 8

# P2 balancer: a thin evidence bank must not reach "strong" via recency volume.
# The objective-friction bank is a thin, SYSTEM-DETECTED signal (~1-2 samples),
# so it is reported only as a capped HINT that can never read "strong" (below
# the friction threshold) and is NEVER summed into the friction display value —
# the module invariant (user vs objective "never conflated", see header).
PULSE_OBJ_HINT_CAP = 0.30



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
    try:
        candidate = value if value is not None else legacy
        return max(1, int(candidate))
    except (TypeError, ValueError):
        return max(1, int(legacy))


def _lane_accumulation(capsules, bank, threshold: float, half_life: int,
                       now: float | None = None) -> float:
    """P2 channel-gated call-accumulation (0-baseline), TIME-DECAYED.

    Each channel-matched recent capsule contributes a recency weight
    ``exp(-ln2 * age / PULSE_VALUE_HALF_LIFE_S)`` that halves every half-life;
    the value is the MEAN of those weights over the matching capsules (0 when
    there is no labeled bank or no matching call). So the value decays on its
    own as the capsules age: a tool that was active but is no longer called
    fades toward 0 after a few half-lives. This is a DATA decay clock (the
    capsules' ts + the request's ``now``), not a per-tool stateful clock --
    the pattern layer stays pure + stateless. Falls back to the legacy
    count-saturated shape when ``now`` is None.
    """
    if not bank:
        return 0.0
    matching = []
    for capsule in capsules:
        vector = capsule.get("vector") if isinstance(capsule, dict) else None
        if not vector:
            continue
        sim = strongest_similarity(vector, bank)
        if sim is not None and sim >= threshold:
            matching.append(capsule)
    if not matching:
        return 0.0
    if now is None:
        # Legacy count-saturated shape (no time-decay).
        n = len(matching)
        return round(1.0 - math.exp(-n / max(1, int(half_life))), 4)
    # Time-decay: mean recency weight over the matching capsules.
    hl = max(1.0, float(PULSE_VALUE_HALF_LIFE_S))
    ln2 = math.log(2.0)
    weighted = 0.0
    for capsule in matching:
        ts = capsule.get("ts")
        age = max(0.0, float(now) - float(ts)) if ts is not None else 0.0
        weighted += math.exp(-ln2 * age / hl)
    return round(min(1.0, weighted / len(matching)), 4)


# -- v8 vector-space compatibility gate (single authoritative path) ---- #
# Two embedding spaces never compare: a goal vector from encoder A
# scored against capsule vectors from encoder B is a meaningless number
# that can still satisfy a readiness threshold through support counts.
# Rules (review 2026-11-10):
#   * scoring candidates — dimension match with the active space (a
#     mismatched vector is EXCLUDED, not scored 0.0 into the pool);
#   * readiness support — dimension match AND known provenance equal to
#     the active space; unknown provenance (pre-v8 rows, untagged
#     capsules) is unknown evidence: it can still be scored when the
#     dimension matches, but is never counted toward readiness;
#   * no active-space identity supplied (legacy test call sites) ->
#     legacy behavior (dimension gate only, everything counts), so old
#     consumers keep working unchanged.
def _space_dim(space) -> int | None:
    if not isinstance(space, dict):
        return None
    try:
        dim = int(space.get("dim"))
    except (TypeError, ValueError):
        return None
    return dim if dim > 0 else None


def _vec_scores(vec, space) -> bool:
    """May ``vec`` be scored? PULSE scoring is cross-dim (the active goal and
    a stored vector meet at the lower dim inside ``pulse.prototypes.cosine``),
    so ANY non-None vector is reachable regardless of which encoder produced
    it — the dimension is no longer a gate. Only an absent vector is skipped.
    ``space`` is kept in the signature for callers but is not a gate."""
    return vec is not None


def _vec_supports(vec, meta, space) -> bool:
    """May ``vec`` count toward readiness support? Because scoring is
    cross-dim, every stored vector (any encoder, any dim) is reachable, so all
    of it counts as evidence — no data is ever orphaned by an encoder toggle.
    Source provenance (``meta``) is informational, not a gate; ``space`` is
    kept for callers but unused."""
    return vec is not None


def _goal_gate(goal_vector, goal_backend, space):
    """The goal is always usable: cross-dim scoring reaches every stored
    prototype/capsule from a goal of ANY dimension, so there is no gate.
    ``goal_backend`` / ``space`` are provenance, not a gate. An absent goal
    (None) stays None."""
    return goal_vector


def _gated_bank(tool, prototypes, label, space):
    """(score_candidates, support_candidates) for one bank label."""
    vectors = list(prototypes.bank(tool).get(label) or [])
    try:
        meta = prototypes.bank_meta(tool).get(label)  # (id, mv) | None
    except AttributeError:  # pre-v8 store (test fakes)
        meta = None
    scored = [v for v in vectors if _vec_scores(v, space)]
    supported = [v for v in scored if _vec_supports(v, meta, space)]
    return scored, supported


def _gated_capsules(capsules, space):
    """(score_candidates, support_candidates) over capsule dicts;
    per-capsule provenance from the engine-attached tags."""
    scored, supported = [], []
    for c in capsules:
        if not isinstance(c, dict):
            continue
        vec = c.get("vector")
        if not _vec_scores(vec, space):
            continue
        scored.append(vec)
        meta = None
        if c.get("backend") is not None or c.get("model_version") is not None:
            meta = (c.get("backend"), c.get("model_version"))
        if _vec_supports(vec, meta, space):
            supported.append(vec)
    return scored, supported


def build_matrix(registry, prototypes, session_state=None,
                 goal_vector=None, goal_text: str | None = None,
                 backend=None, capsules_by_tool: dict | None = None,
                 thresholds: dict | None = None, vector_space=None,
                 goal_backend=None, event_rates: dict | None = None,
                 upset: dict | None = None) -> list[dict]:
    """Build one row per registry tool (dynamic — no hardcoded list).

    v0.1.10: ``vector_space`` ({"id", "model_version", "dim"}) +
    ``goal_backend`` activate the compatibility gate — scoring and
    readiness support only use vectors comparable in the active
    embedding space (see the gate block above). Omitted -> legacy
    behavior (everything scores, everything counts).

    ``event_rates`` (C.1, additive) is accepted so the shared ``common``
    dict can feed both builders with one shape; the legacy row
    projection is deliberately left untouched (byte-compatible) — the
    velocity-driven LIVE ranking lives in :func:`build_lanes` only.

    ``upset`` (D1-D6, additive): the user-upset accumulator's view
    (F'/tier/M/prognosis) is accepted for the shared ``common`` shape;
    the legacy row projection ignores it — the friction-lane ``user``
    pseudo-entry lives in :func:`build_lanes` only.
    """
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
    goal_vector = _goal_gate(goal_vector, goal_backend, vector_space)
    has_goal = goal_vector is not None
    for tool_row in registry.list_tools():
        tool = tool_row["tool_name"]
        # Legacy row projection (pre-Stage-2 semantics for old
        # consumers; new consumers read per-cell state below).
        state = prototypes.state(tool)
        labeled = prototypes.labeled_count(tool)

        # GOAL: goal capsule vs row prototypes (+ recent capsules when
        # prototypes are still empty, so a relevant goal still scores).
        # Support counts REFERENCE VECTORS, not feedback labels.
        # v0.1.10: gate — score candidates (dimension-comparable) vs
        # support candidates (dimension-comparable AND known provenance
        # in the active space); unknown-provenance vectors can still be
        # scored but never satisfy readiness.
        ref_scored: list = []
        ref_supported: list = []
        for _label in ("good", "friction", "objective_friction"):
            _scored, _supported = _gated_bank(tool, prototypes, _label,
                                               vector_space)
            ref_scored.extend(_scored)
            ref_supported.extend(_supported)
        capsule_scored, capsule_supported = _gated_capsules(
            capsules_by_tool.get(tool, []), vector_space)
        if ref_scored:
            reference, reference_support = ref_scored, ref_supported
        else:
            reference, reference_support = capsule_scored, capsule_supported
        goal_raw = strongest_similarity(goal_vector, reference)
        goal_value = _floor(goal_raw)
        goal_support = _support(len(reference_support), goal_req)
        goal_cell = {"value": goal_value,
                     "state": _state(has_goal, len(reference_support),
                                      goal_req),
                     "level": _level(goal_value, thr["goal"]),
                     "support": goal_support,
                     "support_count": goal_support["count"],
                     "readiness_threshold": goal_support["required"]}

        # GOOD: strongest similarity to the GOOD bank only; support is
        # the GOOD-bank count alone (v0.1.10: gated counts).
        good_scored, good_supported = _gated_bank(tool, prototypes, "good",
                                                   vector_space)
        good_value = _floor(strongest_similarity(goal_vector, good_scored))
        good_support = _support(len(good_supported), good_req)
        good_cell = {"value": good_value,
                     "state": _state(has_goal, len(good_supported),
                                      good_req),
                     "level": _level(good_value, thr["good"]),
                     "support": good_support,
                     "support_count": good_support["count"],
                     "readiness_threshold": good_support["required"]}

        # FRICTION: user and objective sources evaluated independently
        # (never summed); the combined cell follows the DOMINANT
        # source. "user"/"objective" stay numeric-or-None (legacy).
        user_scored, user_supported = _gated_bank(
            tool, prototypes, "friction", vector_space)
        obj_scored, obj_supported = _gated_bank(
            tool, prototypes, "objective_friction", vector_space)
        user_count = len(user_supported)
        obj_count = len(obj_supported)
        friction_user = strongest_similarity(goal_vector, user_scored)
        friction_obj = strongest_similarity(goal_vector, obj_scored)
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


def required_for(prototypes, category: str, legacy: int) -> int:
    """Public per-category readiness minimum (legacy fallback)."""
    attr = {"goal": "goal_min_references", "good": "good_min_samples",
            "user_friction": "user_friction_min_samples",
            "objective_friction": "objective_friction_min_samples"}[category]
    return _required(prototypes, attr, legacy)


# ---------------------------------------------------------------------------
# Stage 3: independent lane selectors (v2 contract).
#
# Each lane ranks its OWN tools and returns ALL eligible tools (the
# desktop picks the visible prefix). Ordering is stable per lane:
# scores are quantized to HYSTERESIS so float noise below that threshold
# never reshuffles; remaining ties break deterministically by support
# ratio, canonical tool name, then tool id. No animation fields anywhere.
# ---------------------------------------------------------------------------

LANE_ORDER = ["live", "goal", "good", "friction"]
HYSTERESIS = 1e-6  # minimum meaningful score change for ranking


def _q(score) -> int:
    """Quantized ranking bucket: changes smaller than HYSTERESIS share
    a bucket, so sub-noise float jitter cannot reshuffle a lane."""
    if score is None:
        return 0
    return int(round(float(score) / HYSTERESIS))


def _sem_sort_key(entry: dict):
    value = entry.get("value")
    support = entry.get("support")
    if isinstance(support, dict):
        ratio = support.get("ratio", 0.0)
    else:  # FRICTION carries per-part support; use the best part ratio.
        ratio = max(((entry.get(p) or {}).get("support") or {}).get("ratio", 0.0)
                    for p in ("user", "objective"))
    return (0 if value is not None else 1, -_q(value), -ratio,
            entry.get("tool_name", ""), entry.get("tool_id", -1))


def _live_sort_key(entry: dict):
    mom = entry.get("momentum") or {}
    act = entry.get("activity") or {}
    vel = entry.get("velocity")  # C.1: only present when event_rates was fed
    vcount = int(vel.get("count") or 0) if vel is not None else 0
    has = bool(act.get("calls_started") or act.get("calls_completed")
               or act.get("errors") or act.get("last_seen") is not None
               or vcount > 0)
    total = (int(act.get("calls_started") or 0)
             + int(act.get("calls_completed") or 0)
             + int(act.get("errors") or 0))
    last = act.get("last_seen")
    lastf = -(float(last) if isinstance(last, (int, float)) else -1.0)
    if vel is not None:
        # C.1: persisted event-rate velocity is the primary dynamic key
        # (fast-decay consistency — a recent batch rises, then cools as
        # events roll out of the window). Cumulative + recency tie-break.
        return (0 if has else 1, -vcount, total * -1, lastf,
                entry.get("tool_name", ""), entry.get("tool_id", -1))
    # Legacy (event_rates=None): capped-capsule surge is the primary key.
    return (0 if has else 1, -_q(mom.get("value", 0.5)), total * -1,
            lastf, entry.get("tool_name", ""), entry.get("tool_id", -1))


def _assign_ranks(entries: list, key_fn) -> list:
    ordered = sorted(entries, key=key_fn)
    for index, entry in enumerate(ordered, start=1):
        entry["rank"] = index
    return ordered


def build_lanes(registry, prototypes, session_state=None,
                goal_vector=None, goal_text: str | None = None,
                backend=None, capsules_by_tool: dict | None = None,
                thresholds: dict | None = None, vector_space=None,
                goal_backend=None, event_rates: dict | None = None,
                now: float | None = None,
                upset: dict | None = None) -> dict:
    """The four independent lane arrays (v2 contract). Pure projection
    over the same inputs as build_matrix — no tables, no I/O beyond
    what build_matrix already does. Returns ALL eligible tools per
    lane. v0.1.10: the v8 compatibility gate (``vector_space`` +
    ``goal_backend``) splits score candidates (dimension-comparable)
    from support candidates (dimension-comparable AND known provenance
    in the active space); evidence counts stay the RAW stored facts.

    C.1 (additive): when ``event_rates`` is not None, each LIVE entry
    gains a ``velocity`` field (persisted tool_start event-rate in a
    rolling window) and the LIVE lane ranks by that velocity first —
    the "fast-decay consistency" signal, free from the events table.
    ``None`` (the default, and the legacy path) leaves the capped-
    capsule momentum sort untouched (byte-identical).

    D1-D6 (additive): when ``upset`` is not None, the friction lane
    gains a ``user`` PSEUDO-entry (tool_name "user", tool_id -1): the
    LLM's own reasoning narrating user upset, read language-agnostically
    (D1). No registry tool stands behind it — no banks, no capsules —
    its value is the accumulator's F' and it appears only from the
    lane tier up (F' >= 0.30, the D4 tier carried in ``upset``). The
    pane's glitch reads the top-level ``upset`` block, never this
    entry. ``None`` (the default, and the legacy path) leaves the lane
    byte-identical."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if goal_vector is None and goal_text is not None and backend is not None:
        from .activity_encoder import encode_goal_text
        goal_vector = encode_goal_text(goal_text, backend)
    capsules_by_tool = capsules_by_tool or {}
    # DATA decay clock: the current time (the request's now, or time.time()
    # if not passed) drives the time-decay of the accumulation value. This is
    # a per-request DATA clock (the capsules' ts age against it), NOT a
    # per-tool stateful clock -- the pattern layer stays pure + stateless.
    now_ts = float(now) if now is not None else time.time()
    legacy_min = int(getattr(prototypes, "min_labeled", 3) or 3)
    goal_req = _required(prototypes, "goal_min_references", legacy_min)
    good_req = _required(prototypes, "good_min_samples", legacy_min)
    user_fric_req = _required(prototypes, "user_friction_min_samples",
                              legacy_min)
    obj_fric_req = _required(prototypes, "objective_friction_min_samples",
                             legacy_min)
    goal_vector = _goal_gate(goal_vector, goal_backend, vector_space)
    has_goal = goal_vector is not None
    live, goal, good, friction = [], [], [], []
    for tool_row in registry.list_tools():
        tool = tool_row["tool_name"]
        tid = tool_row["id"]
        bank = prototypes.bank(tool)
        capsules = [c for c in (capsules_by_tool.get(tool) or [])
                    if isinstance(c, dict) and c.get("vector")]
        ref_from_banks = (bank["good"] + bank["friction"]
                          + bank["objective_friction"])
        # v0.1.10 gate: scored vs support-eligible candidates; the RAW
        # counts stay the evidence facts below.
        ref_scored: list = []
        ref_supported: list = []
        for _label in ("good", "friction", "objective_friction"):
            _scored, _supported = _gated_bank(tool, prototypes, _label,
                                               vector_space)
            ref_scored.extend(_scored)
            ref_supported.extend(_supported)
        capsule_scored, capsule_supported = _gated_capsules(capsules,
                                                             vector_space)
        if ref_from_banks:
            reference, reference_support = ref_scored, ref_supported
            evidence_source = "prototype_banks"
            capsule_count = len(capsules)
        else:
            reference, reference_support = capsule_scored, capsule_supported
            evidence_source = "recent_capsules" if capsules else "none"
            capsule_count = len(capsules)
        goal_value = _floor(strongest_similarity(goal_vector, reference))
        goal_support = _support(len(reference_support), goal_req)
        goal.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "value": goal_value, "threshold": thr["goal"],
            "level": _level(goal_value, thr["goal"]),
            "state": _state(has_goal, len(reference_support), goal_req),
            "support": goal_support,
            "evidence": {"source": evidence_source,
                         "prototype_count": len(ref_from_banks),
                         "capsule_count": capsule_count},
        })
        good_scored, good_supported = _gated_bank(tool, prototypes, "good",
                                                   vector_space)
        good_count = len(bank["good"])  # raw evidence fact
        good_similarity = _floor(strongest_similarity(goal_vector, good_scored))
        # P2: display value = channel-gated accumulation (recent calls that
        # resemble this tool's own labeled good bank). VALUE (how valuable the
        # tool's activity is) and CONFIDENCE (how much evidence supports it --
        # the bank's support below) are DECOUPLED: a few calls above the floor
        # reach a valuable score even on a thin bank; the confidence is a
        # separate field, never multiplied into the value. The single-prompt
        # similarity stays the `similarity` field.
        good_value = round(
            _lane_accumulation(capsules, good_scored, thr["good"],
                               PULSE_ACCUM_HALF_LIFE, now_ts), 4)
        good.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "value": good_value, "threshold": thr["good"],
            "level": _level(good_value, thr["good"]),
            "state": _state(has_goal, len(good_supported), good_req),
            "support": _support(len(good_supported), good_req),
            "similarity": good_similarity,
            "evidence_source": "good_prototypes" if good_count else "none",
        })
        user_scored, user_supported = _gated_bank(
            tool, prototypes, "friction", vector_space)
        obj_scored, obj_supported = _gated_bank(
            tool, prototypes, "objective_friction", vector_space)
        user_count = len(user_supported)
        obj_count = len(obj_supported)
        user_value = _floor(strongest_similarity(goal_vector,
                                                 user_scored))
        obj_value = _floor(strongest_similarity(
            goal_vector, obj_scored))
        user_support = _support(user_count, user_fric_req)
        obj_support = _support(obj_count, obj_fric_req)
        scored = []
        if user_value is not None:
            scored.append(("user", user_value, user_support["ratio"]))
        if obj_value is not None:
            scored.append(("objective", obj_value, obj_support["ratio"]))
        dominant = None
        friction_similarity = None
        friction_state = _state(has_goal, 0, user_fric_req)
        if scored:
            dominant, friction_similarity, _r = max(
                scored, key=lambda s: (s[1], s[2], s[0] == "user"))
            friction_state = _state(
                has_goal,
                user_count if dominant == "user" else obj_count,
                user_fric_req if dominant == "user" else obj_fric_req)
        # P2 balancer: the friction DISPLAY value is the USER-labeled friction
        # accumulation only. Objective friction (a thin system-detected signal)
        # is a SEPARATE capped hint that can never read "strong" and is never
        # summed into the value -- honoring the module invariant ("never
        # conflated"). VALUE and CONFIDENCE are DECOUPLED: the bank's support
        # is a separate field, never multiplied into value, so a few calls
        # above the floor still reach a valuable score on a thin bank. Without
        # user labels the value is 0.0.
        user_acc = _lane_accumulation(
            capsules, user_scored, thr["friction"], PULSE_ACCUM_HALF_LIFE, now_ts)
        user_hint = round(user_acc, 4)
        obj_acc = _lane_accumulation(
            capsules, obj_scored, thr["friction"], PULSE_ACCUM_HALF_LIFE, now_ts)
        obj_hint = round(min(obj_acc, PULSE_OBJ_HINT_CAP), 4)
        friction_value = user_hint
        friction.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "value": friction_value, "threshold": thr["friction"],
            "level": _level(friction_value, thr["friction"]),
            "state": friction_state, "dominant_source": dominant,
            "similarity": friction_similarity,
            # P2 balancer sub-fields: user-labeled accumulation (== value) and
            # the SEPARATE capped objective hint (never "strong", never summed
            # into value -- reported on its own so it reads as a hint).
            "user_accumulation": user_hint,
            "objective_hint": obj_hint,
            "objective_hint_cap": PULSE_OBJ_HINT_CAP,
            "user": {"value": user_value,
                     "level": _level(user_value, thr["friction"]),
                     "state": _state(has_goal, user_count, user_fric_req),
                     "support": user_support},
            "objective": {"value": obj_value,
                          "level": _level(obj_value, thr["friction"]),
                          "state": _state(has_goal, obj_count, obj_fric_req),
                          "support": obj_support},
        })
        live.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "activity": {
                "calls_started": tool_row["calls_started"],
                "calls_completed": tool_row["calls_completed"],
                "errors": tool_row["errors"],
                "first_seen": tool_row["first_seen"],
                "last_seen": tool_row["last_seen"],
                "recent_capsules": len(capsules),
            },
            "momentum": None,  # filled below (shared momentum pass)
        })
    recent_counts = {e["tool_name"]: e["activity"]["recent_capsules"]
                     for e in live}
    cum_counts = {e["tool_name"]: int(e["activity"]["calls_completed"])
                  for e in live}
    momentum = surge_values(recent_counts, cum_counts)
    for entry in live:
        entry["momentum"] = momentum.get(entry["tool_name"],
                                         empty_momentum())
    # C.1: attach the persisted event-rate velocity so the LIVE lane can
    # rank by it (fast-decay consistency). Only when the caller fed
    # event_rates — a None keeps the legacy capped-capsule sort intact.
    if event_rates is not None:
        default_vel = {"count": 0, "per_s": 0.0,
                       "window_s": PULSE_VELOCITY_WINDOW_S}
        for entry in live:
            entry["velocity"] = dict(event_rates.get(entry["tool_name"],
                                                     default_vel))
    # D1-D6: the USER's own friction (the LLM's reasoning narrating that
    # the user is upset — language-agnostic by construction). A pseudo
    # entry: no registry tool, no banks, no capsules — the value is the
    # accumulator's F' and the entry exists only from the D4 lane tier
    # up. The pane's glitch reads the top-level ``upset`` block, never
    # this entry.
    if upset is not None and int(upset.get("tier") or 0) >= 1:
        up_value = round(
            min(1.0, max(0.0, float(upset.get("value") or 0.0))), 4)
        up_turns = int(upset.get("upset_turns") or 0)
        friction.append({
            "tool_id": -1, "tool_name": "user", "rank": 0,
            "value": up_value, "threshold": thr["friction"],
            "level": _level(up_value, thr["friction"]),
            "state": _state(True, up_turns, 1),
            "dominant_source": "user",
            "similarity": up_value,
            "user_accumulation": up_value,
            "objective_hint": None,
            "objective_hint_cap": PULSE_OBJ_HINT_CAP,
            "user": {"value": up_value,
                     "level": _level(up_value, thr["friction"]),
                     "state": _state(True, up_turns, 1),
                     "support": _support(up_turns, 1)},
            "objective": {"value": None, "level": "neutral",
                          "state": "empty",
                          "support": _support(0, 1)},
            "upset": True,
        })
    return {
        "live": _assign_ranks(live, _live_sort_key),
        "goal": _assign_ranks(goal, _sem_sort_key),
        "good": _assign_ranks(good, _sem_sort_key),
        "friction": _assign_ranks(friction, _sem_sort_key),
    }


def lanes_from_rows(rows, thresholds: dict | None = None) -> dict:
    """Derive the v2 lanes from legacy build_matrix rows (used when the
    live engine view predates the lanes field). Evidence detail that
    the legacy projection does not carry collapses honestly:
    support counts come from the per-cell support, capsule/bank split
    is unavailable so it reports 0 with source 'none' when empty."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    rows = rows or []
    live, goal, good, friction = [], [], [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tool = row.get("tool_name") or row.get("tool") or ""
        tid = row.get("tool_id", row.get("id", -1))
        live_row = row.get("live") or {}
        mom = row.get("momentum") or {"momentum": 0.0, "surge": 0.0,
                                      "value": 0.5, "state": "steady"}
        live.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "activity": {
                "calls_started": int(live_row.get("calls_started") or 0),
                "calls_completed": int(live_row.get("calls_completed") or 0),
                "errors": int(live_row.get("errors") or 0),
                "first_seen": live_row.get("first_seen"),
                "last_seen": live_row.get("last_seen"),
                "recent_capsules": 0,
            },
            "momentum": {"momentum": mom.get("momentum", 0.0),
                         "surge": mom.get("surge", mom.get("momentum", 0.0)),
                         "value": mom.get("value", 0.5),
                         "state": mom.get("state", "steady")},
        })
        for lane_key, col, threshold, evidence in (
                (goal, "goal", thr["goal"], "goal"),
                (good, "good", thr["good"], "good")):
            cell = row.get(col) or {}
            support = cell.get("support") or _support(
                int(cell.get("support_count") or 0),
                int(cell.get("readiness_threshold") or 3))
            entry = {
                "tool_id": tid, "tool_name": tool, "rank": 0,
                "value": cell.get("value"), "threshold": threshold,
                "level": cell.get("level") or _level(cell.get("value"),
                                                     threshold),
                "state": cell.get("state") or "learning",
                "support": support,
            }
            if evidence == "goal":
                entry["evidence"] = {
                    "source": ("prototype_banks" if support["count"] else "none"),
                    "prototype_count": support["count"], "capsule_count": 0}
            else:
                entry["evidence_source"] = ("good_prototypes"
                                            if support["count"] else "none")
            lane_key.append(entry)
        fcell = row.get("friction") or {}
        fthr = thr["friction"]
        user_support = fcell.get("user_support") or _support(0, 3)
        obj_support = fcell.get("objective_support") or _support(0, 3)
        user_part = {"value": fcell.get("user"),
                     "level": _level(fcell.get("user"), fthr),
                     "state": fcell.get("user_state") or "learning",
                     "support": user_support}
        obj_part = {"value": fcell.get("objective"),
                    "level": _level(fcell.get("objective"), fthr),
                    "state": fcell.get("objective_state") or "learning",
                    "support": obj_support}
        friction.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "value": fcell.get("value"), "threshold": fthr,
            "level": fcell.get("level") or _level(fcell.get("value"), fthr),
            "state": fcell.get("state") or "learning",
            "dominant_source": fcell.get("dominant_source"),
            "user": user_part, "objective": obj_part,
        })
    return {
        "live": _assign_ranks(live, _live_sort_key),
        "goal": _assign_ranks(goal, _sem_sort_key),
        "good": _assign_ranks(good, _sem_sort_key),
        "friction": _assign_ranks(friction, _sem_sort_key),
    }


def matrix_for_goal(goal_text, registry, prototypes, backend, **kw):
    """Convenience: encode the goal (worker side) then build the matrix."""
    from .activity_encoder import encode_goal_text
    return build_matrix(registry, prototypes, goal_vector=encode_goal_text(goal_text, backend),
                        backend=backend, **kw)
