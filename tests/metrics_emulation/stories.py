"""Declarative story arcs for the emulation harness.

Every prompt below is a SHORT core phrase taken from the repo's own real
vocabulary:
  - perception/resemblance.py fixture lines 71-82 (axis anchors)
  - pulse/feedback.py FEEDBACK_RULES (good / friction labels)
No invented scoring, no real user text, no personal data.
"""
from __future__ import annotations

# Axis-anchor cores — EXACT fixture strings from perception/resemblance.py:71-82.
DIRECT = "Give me the short answer, no preamble."
CONTEXT = "Here is the full background, logs, config and the exact error."
BROAD = "Brainstorm every option, even wild ones."
NARROW = "Just do the one thing, pick the obvious answer."
ANALYTIC = "Compare pros and cons and weigh the evidence with numbers."
QUICK = "Quick fix, no deep dive, just patch it."
ITERATE = "Revise that, iterate and refine it step by step."
ONESHOT = "One shot only, final answer, no revisions."
TIGHT = "Keep this narrow: only the auth module, one function."
OVERHAUL = "Rework the whole system, overhaul every layer at once."
WALK = "Walk me through your reasoning step by step with full background."

# Explicit feedback cores (pulse/feedback.py FEEDBACK_RULES).
FRICTION = ("still broken", "still failing", "it failed", "do it again", "wrong")
GOOD = ("thanks", "that worked", "good job")

STEADY = (DIRECT, NARROW, QUICK, TIGHT, "fix the login")
GUIDANCE = (CONTEXT, TIGHT, ITERATE, ANALYTIC)
SWING_A = (DIRECT, ONESHOT, QUICK, NARROW)
SWING_B = (WALK, BROAD, OVERHAUL, ITERATE)


def _phase(pid, label, start, end, prompts, tools, statuses=("ok",),
           reasoning=("assistant considered the next step",), corner=False):
    return {
        "id": pid, "label": label, "start": start, "end": end,
        "prompts": list(prompts), "tools": list(tools),
        "statuses": list(statuses), "reasoning": list(reasoning),
        "corner": bool(corner),
    }


STORIES = {
    "friction-recovery": {
        "label": "Steady work -> friction loop -> adjusted -> thanks",
        "drawing": {"template": "neutral-pencil", "ink": False, "animated": True},
        "phases": [
            _phase("default", "steady work", 0.00, 0.34, STEADY,
                   ("read_file", "patch", "read_file", "write_file"), corner=True),
            _phase("friction", "model loops, no result", 0.34, 0.70, FRICTION,
                   ("read_file", "read_file", "terminal", "read_file"),
                   statuses=("error", "error"),
                   reasoning=("assistant repeated the same failed attempt",), corner=True),
            _phase("adjust", "tooling adjusted, clear guidance", 0.70, 0.88, GUIDANCE,
                   ("patch", "write_file", "read_file"),
                   reasoning=("assistant changed approach after the new instruction",), corner=True),
            _phase("complete", "user thanks, complete", 0.88, 1.00, GOOD,
                   ("patch", "read_file"), corner=True),
        ],
        "corners": [0, 26, 27, 40, 55, 56, 70, 71, 79],
    },
    "friction-to-good": {
        "label": "Poor goal + friction -> guidance -> smooth, good mood",
        "drawing": {"template": "angular-pencil", "ink": True, "animated": True},
        "phases": [
            _phase("poor-goal", "harsh start, weak goal", 0.00, 0.20,
                   FRICTION + ("bad",), ("terminal", "read_file"),
                   statuses=("error", "error"),
                   reasoning=("assistant had no clear target",), corner=True),
            _phase("guidance", "user gives good instructions", 0.20, 0.45, GUIDANCE,
                   ("read_file", "patch"), corner=True),
            _phase("smooth", "smooth work, good mood", 0.45, 1.00,
                   STEADY + GUIDANCE + GOOD, ("patch", "write_file", "read_file"),
                   reasoning=("assistant followed the stated constraint",), corner=True),
        ],
        "corners": [0, 15, 16, 35, 36, 60, 79],
    },
    "volatile-amplification": {
        "label": "Default start -> volatile prompting -> repeated portrait drift",
        "drawing": {"template": "soft-flow-study", "ink": False, "animated": True},
        "phases": [
            _phase("baseline", "default start", 0.00, 0.16, STEADY,
                   ("read_file", "patch"), corner=True),
            _phase("volatile", "volatile, alternating prompts", 0.16, 1.00,
                   SWING_A + SWING_B, ("read_file", "patch", "terminal"),
                   statuses=("ok", "error"), corner=True),
        ],
        "corners": [0, 12, 20, 35, 50, 65, 79],
    },
}

STORY_ORDER = ("friction-recovery", "friction-to-good", "volatile-amplification")


def phase_at(story_id: str, turn: int, total: int):
    """Return the phase dict governing ``turn`` (fractions -> integer turns)."""
    phases = STORIES[story_id]["phases"]
    frac = turn / max(1, total - 1)
    for p in phases:
        if p["start"] <= frac < p["end"] or (frac >= 1.0 and p["end"] >= 1.0):
            return p
    return phases[-1]


def corners_for(story_id: str, total: int):
    """Corner turn indices, clamped to the requested length."""
    return sorted({min(total - 1, int(c)) for c in STORIES[story_id]["corners"]
                   if 0 <= c < total or c == total - 1})
