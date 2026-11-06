"""Deterministic explicit-feedback detection (P1 perception wiring).

Strict guardrails, no LLM, no I/O:
- short-utterance gate: only messages of at most ``MAX_WORDS`` words are
  considered; long task descriptions are never labeled.
- phrase rules only: ``FEEDBACK_RULES`` is an explicit data table of
  (label, phrase) patterns. Ordinary follow-ups, questions and unrelated
  topics match no rule and stay unlabeled (``None``).
- negative/friction patterns are checked before positive ones, so a
  mixed utterance such as "good try, still broken" is friction.
- association is bounded: only the single most recent tool capsule is
  labeled, considered only within the ``RECENT_CAPSULE_WINDOW`` most
  recent capsules across tools. Older history is never relabeled.
- fault isolation: callers wrap detection/association in try/except;
  :func:`apply_feedback` itself never raises and never logs raw text.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MAX_WORDS = 12
RECENT_CAPSULE_WINDOW = 2

# Explicit rule table: (label, phrase). Phrases are matched casefolded
# on word boundaries. Labels are the PrototypeStore bank labels.
FEEDBACK_RULES: list[tuple[str, str]] = [
    # NEGATIVE / FRICTION family (checked first).
    ("friction", "still broken"),
    ("friction", "still failing"),
    ("friction", "still not working"),
    ("friction", "did not work"),
    ("friction", "does not work"),
    ("friction", "doesn't work"),
    ("friction", "not working"),
    ("friction", "no it failed"),
    ("friction", "it failed"),
    ("friction", "that is wrong"),
    ("friction", "that was wrong"),
    ("friction", "this is wrong"),
    ("friction", "this was wrong"),
    ("friction", "that's wrong"),
    ("friction", "that is incorrect"),
    ("friction", "not fixed"),
    # POSITIVE family.
    ("good", "thank you"),
    ("good", "thanks"),
    ("good", "that worked"),
    ("good", "it worked"),
    ("good", "that fixed it"),
    ("good", "fixed it"),
    ("good", "that did it"),
    ("good", "that did the trick"),
    ("good", "good job"),
    ("good", "well done"),
    ("good", "great work"),
    ("good", "great job"),
    ("good", "nice work"),
    ("good", "nice job"),
    ("good", "spot on"),
    ("good", "perfect work"),
    ("good", "perfect job"),
]

# Pre-compiled word-boundary patterns, negative (friction) first.
_COMPILED: list[tuple[str, re.Pattern]] = []
for _label, _phrase in FEEDBACK_RULES:
    _COMPILED.append((_label, re.compile(r"\b" + re.escape(_phrase) + r"\b")))


def detect_feedback(text) -> str | None:
    """Return 'good' | 'friction' | None for one short user message.

    Guardrails: non-str / empty -> None; more than MAX_WORDS words ->
    None (long task descriptions are never classified); no phrase -> None.
    Deterministic and pure (no I/O, no logging of the text).
    """
    if not isinstance(text, str):
        return None
    words = text.split()
    if not words or len(words) > MAX_WORDS:
        return None
    t = text.casefold()
    # Negative outranks positive regardless of table position ordering
    # within a label; the table is already ordered friction-first, and
    # this two-pass scan makes that precedence explicit.
    for label in ("friction", "good"):
        for rule_label, pattern in _COMPILED:
            if rule_label != label:
                continue
            if pattern.search(t):
                return label
    return None


def _recent_capsules(capsules_by_tool, window=RECENT_CAPSULE_WINDOW):
    """Flatten capsules to [(seq, ts, tool, capsule)] sorted oldest->newest.

    Ordering is by capsule ``ts`` when present, with insertion sequence
    as the tie-breaker (capsule lists are appended oldest-first). Only
    capsules carrying a vector can be labeled; vector-less entries are
    skipped rather than counted toward the window.
    """
    if not isinstance(capsules_by_tool, dict):
        return []
    flat = []
    seq = 0
    for tool, caps in capsules_by_tool.items():
        if not isinstance(caps, (list, tuple)):
            continue
        for cap in caps:
            seq += 1
            if not isinstance(cap, dict):
                continue
            if cap.get("vector") is None:
                continue
            try:
                ts = float(cap.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            flat.append((ts, seq, str(cap.get("tool_name") or tool),
                         cap))
    flat.sort(key=lambda item: (item[0], item[1]))
    if window is None:
        return flat
    return flat[-max(1, int(window)):] if flat else []


def apply_feedback(store, capsules_by_tool, text) -> str | None:
    """Detect feedback in ``text`` and label the most recent capsule.

    Returns the label applied ('good' | 'friction') or None. The label
    is applied only to the single most recent capsule within the last
    RECENT_CAPSULE_WINDOW capsules; older capsules are never relabeled.
    Never raises (fault isolation) and never logs raw text.
    """
    try:
        label = detect_feedback(text)
        if label is None or store is None:
            return None
        recent = _recent_capsules(capsules_by_tool)
        if not recent:
            return None
        _ts, _seq, tool, capsule = recent[-1]
        applied = None
        try:
            applied = store.record_feedback(tool, capsule, text)
        except Exception:
            logger.exception(
                "its-in-your-head: feedback record failed")
            return None
        if applied is None:
            # PrototypeStore.record_feedback re-classifies with its own
            # (broader) table; if it did not recognise this detector's
            # phrase, apply the detector's label directly so the two
            # tables cannot silently diverge into a lost label.
            try:
                store.add(tool, capsule, label)
                applied = label
            except Exception:
                logger.exception(
                    "its-in-your-head: feedback add failed")
                return None
        return applied
    except Exception:
        logger.exception("its-in-your-head: feedback association failed")
        return None
