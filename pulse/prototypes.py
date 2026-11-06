"""Bounded GOOD / FRICTION prototype banks per tool (S4).

Rules:
- Explicit positive feedback -> GOOD label on the preceding
  interaction's capsule.
- Explicit criticism, or user-attributed execution failure -> FRICTION.
- Objective execution problems (tool errors, timeouts) are tracked
  separately as objective-friction. A tool error is NOT by itself user
  dissatisfaction and never enters the FRICTION (user) bank on its own.
- Unlabeled cells stay neutral ``learning`` until ``min_labeled``
  labeled examples exist for that tool (C4 pulse.min_labeled).
- Banks are bounded by ``cap`` (C4 pulse.prototype_cap): the oldest
  prototype is evicted first (FIFO).
"""
from __future__ import annotations

import math
import threading
import time

# S4 task defaults (C4 keys; plugin.yaml is parent-owned — see flags).
DEFAULT_PROTOTYPE_CAP = 8
DEFAULT_MIN_LABELED = 3

OBJECTIVE_STATUSES = frozenset({"error", "timeout", "failed", "failure"})

_POSITIVE = (
    "thank", "great", "perfect", "good job", "well done", "nice",
    "excellent", "brilliant", "exactly what", "works great",
    "love it", "awesome", "fantastic", "keep going", "spot on",
    "thanks", "that worked", "it worked", "fixed it", "that fixed it",
    "that did it", "that did the trick", "great work", "great job",
    "nice work", "nice job", "perfect work", "perfect job",
)
_NEGATIVE = (
    "wrong", "bad", "terrible", "broken", "useless", "horrible",
    "stupid", "hate", "poor", "redo", "do it again", "don't",
    "do not", "stop", "failed", "doesn't work", "not working",
    "waste", "annoying", "frustrat",
    "still broken", "still failing", "still not working",
    "did not work", "does not work", "it failed", "not fixed",
)


def classify_feedback(text) -> str | None:
    """Explicit feedback only. Returns 'good' | 'friction' | None."""
    if not isinstance(text, str):
        return None
    t = text.casefold()
    if any(p in t for p in _NEGATIVE):
        return "friction"
    if any(p in t for p in _POSITIVE):
        return "good"
    return None


def cosine(a, b) -> float:
    if a is None or b is None or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot / (na * nb)))


def strongest_similarity(vector, bank) -> float | None:
    """Max cosine to prototypes in ``bank``; None when the bank is empty
    (absence of evidence — never a negative score)."""
    if vector is None or not bank:
        return None
    return max(cosine(vector, proto) for proto in bank)


class PrototypeStore:
    def __init__(self, cap: int = DEFAULT_PROTOTYPE_CAP,
                 min_labeled: int = DEFAULT_MIN_LABELED):
        self.cap = max(1, int(cap))
        self.min_labeled = max(1, int(min_labeled))
        self._lock = threading.Lock()
        self._banks: dict[str, dict[str, list]] = {}

    def _bank(self, tool: str) -> dict:
        return self._banks.setdefault(
            tool, {"good": [], "friction": [], "objective_friction": []})

    @staticmethod
    def _vector_of(capsule_or_vector):
        if isinstance(capsule_or_vector, dict):
            return capsule_or_vector.get("vector")
        return capsule_or_vector

    # -- ingestion ---------------------------------------------------------
    def add(self, tool: str, capsule_or_vector, label: str):
        """Add a prototype; oldest evicted past ``cap``. Only the vector
        is stored — never the capsule text payload fields beyond it."""
        if label not in ("good", "friction", "objective_friction"):
            raise ValueError(f"unknown prototype label {label!r}")
        vec = self._vector_of(capsule_or_vector)
        if vec is None:
            return None
        vec = list(vec)
        with self._lock:
            bank = self._bank(str(tool))
            bank[label].append(vec)
            del bank[label][:-self.cap]  # FIFO eviction, keeps newest cap
            return vec

    def add_good(self, tool, capsule_or_vector):
        return self.add(tool, capsule_or_vector, "good")

    def add_friction(self, tool, capsule_or_vector):
        return self.add(tool, capsule_or_vector, "friction")

    def add_objective_friction(self, tool, capsule_or_vector):
        return self.add(tool, capsule_or_vector, "objective_friction")

    def record_tool_end(self, capsule: dict) -> str | None:
        """Objective problems -> objective-friction ONLY (never user
        friction). Returns 'objective_friction' or None."""
        status = str(capsule.get("status") or "").lower()
        if status in OBJECTIVE_STATUSES:
            self.add_objective_friction(capsule.get("tool_name"), capsule)
            return "objective_friction"
        return None

    def record_feedback(self, tool: str, capsule_or_vector, text,
                        user_attributed: bool = False) -> str | None:
        """Explicit feedback -> GOOD/FRICTION on the preceding capsule.
        User-attributed execution failure -> FRICTION. Returns the label
        applied, or None for neutral feedback."""
        label = classify_feedback(text)
        if label is None and user_attributed:
            label = "friction"
        if label is None:
            return None
        self.add(tool, capsule_or_vector, label)
        return label

    # -- reads ---------------------------------------------------------------
    def bank(self, tool: str) -> dict:
        with self._lock:
            stored = self._banks.get(str(tool))
            if not stored:
                return {"good": [], "friction": [], "objective_friction": []}
            return {k: list(v) for k, v in stored.items()}

    def labeled_count(self, tool: str) -> int:
        """User-labeled examples (GOOD + FRICTION); objective-friction is
        tracked separately and does not count toward min_labeled."""
        bank = self.bank(tool)
        return len(bank["good"]) + len(bank["friction"])

    def state(self, tool: str) -> str:
        return "ready" if self.labeled_count(tool) >= self.min_labeled \
            else "learning"

    def similarity(self, tool: str, vector, label: str) -> float | None:
        if label not in ("good", "friction", "objective_friction"):
            raise ValueError(f"unknown prototype label {label!r}")
        return strongest_similarity(vector, self.bank(tool)[label])

    def tools(self) -> list[str]:
        with self._lock:
            return sorted(self._banks)
