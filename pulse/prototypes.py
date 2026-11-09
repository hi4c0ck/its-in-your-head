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


def classify_feedback(text) -> str | None:
    """Explicit feedback only. Returns 'good' | 'friction' | None.

    The phrase table lives in ``pulse.feedback.FEEDBACK_RULES`` (single
    source): this delegates to ``detect_feedback`` so both the hot-path
    detector and this classifier share one table and one set of
    guardrails (12-word max, negative precedence, word boundaries).
    """
    from .feedback import detect_feedback

    return detect_feedback(text)


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
                 min_labeled: int = DEFAULT_MIN_LABELED,
                 goal_min_references=None,
                 good_min_samples=None,
                 user_friction_min_samples=None,
                 objective_friction_min_samples=None):
        self.cap = max(1, int(cap))
        self.min_labeled = max(1, int(min_labeled))
        # Per-category readiness minimums (PULSE Stage 2). Each falls
        # back to the legacy min_labeled when unset (None), so old
        # call sites (engine, tests) keep working unchanged.
        def _resolve(value):
            return max(1, int(value if value is not None
                              else self.min_labeled))
        self.goal_min_references = _resolve(goal_min_references)
        self.good_min_samples = _resolve(good_min_samples)
        self.user_friction_min_samples = _resolve(user_friction_min_samples)
        self.objective_friction_min_samples = _resolve(
            objective_friction_min_samples)
        self._lock = threading.Lock()
        self._banks: dict[str, dict[str, list]] = {}
        # v8 vector-space provenance, kept in lockstep with the banks:
        # one (backend, model_version) tag per (tool, label) — the
        # writer persists a tool's whole bank under one active encoder,
        # so a single tag covers the FIFO window. None = unknown space
        # (pre-v8 rows / no encoder identity available).
        self._bank_meta: dict[str, dict[str, tuple | None]] = {}
        # Cross-process bridge: tools whose banks changed since the last
        # persisted sync. The collector's hot-path feedback labels touch
        # only this in-memory set (no I/O); the metrics worker persists
        # the dirty tools on its next pass (see engine._persist_dirty_
        # prototypes / pulse.persistence.persist_tool_banks).
        self._dirty: set[str] = set()

    def _bank(self, tool: str) -> dict:
        return self._banks.setdefault(
            tool, {"good": [], "friction": [], "objective_friction": []})

    @staticmethod
    def _vector_of(capsule_or_vector):
        if isinstance(capsule_or_vector, dict):
            return capsule_or_vector.get("vector")
        return capsule_or_vector

    @staticmethod
    def _provenance_of(capsule_or_vector):
        """v8: the (backend, model_version) encoder space of the input.
        A dict capsule carries the tags the engine attached at capsule
        build time; a bare vector (or an untagged capsule) is unknown
        provenance (None) — scored on dimension match, never counted
        toward readiness support."""
        if isinstance(capsule_or_vector, dict):
            backend = capsule_or_vector.get("backend")
            model_version = capsule_or_vector.get("model_version")
            if backend is not None or model_version is not None:
                return (backend, model_version)
        return None

    # -- ingestion ---------------------------------------------------------
    def add(self, tool: str, capsule_or_vector, label: str,
            provenance: tuple | None = None):
        """Add a prototype; oldest evicted past ``cap``. Only the vector
        is stored — never the capsule text payload fields beyond it.

        ``provenance`` (v8) is a ``(backend, model_version)`` pair for
        the encoder space the vector came from; passed as None (or
        omitted) the bank's provenance stays unknown (legacy rows).
        """
        if label not in ("good", "friction", "objective_friction"):
            raise ValueError(f"unknown prototype label {label!r}")
        vec = self._vector_of(capsule_or_vector)
        if vec is None:
            return None
        vec = list(vec)
        if provenance is None:
            provenance = self._provenance_of(capsule_or_vector)
        with self._lock:
            bank = self._bank(str(tool))
            bank[label].append(vec)
            del bank[label][:-self.cap]  # FIFO eviction, keeps newest cap
            if provenance is not None:
                meta = self._bank_meta.setdefault(str(tool), {})
                meta[label] = tuple(provenance)
            self._dirty.add(str(tool))
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

    def bank_meta(self, tool: str) -> dict:
        """v8: provenance per bank label — {label: (backend,
        model_version) or None}. None = unknown space (legacy vectors);
        the matrix's compatibility gate reads this before scoring and
        readiness support counts."""
        with self._lock:
            meta = self._bank_meta.get(str(tool), {})
            return {label: (tuple(value) if value is not None else None)
                    for label, value in meta.items()}

    def labeled_count(self, tool: str) -> int:
        """User-labeled examples (GOOD + FRICTION); objective-friction is
        tracked separately and does not count toward min_labeled."""
        bank = self.bank(tool)
        return len(bank["good"]) + len(bank["friction"])

    def bank_counts(self, tool: str) -> dict:
        """Independent per-bank evidence counts (never summed across
        sources: user FRICTION and objective FRICTION are separate)."""
        bank = self.bank(tool)
        return {"good": len(bank["good"]),
                "friction": len(bank["friction"]),
                "objective_friction": len(bank["objective_friction"])}

    def readiness_required(self, category: str) -> int:
        """Required evidence count for one readiness category."""
        table = {"goal": self.goal_min_references,
                 "good": self.good_min_samples,
                 "user_friction": self.user_friction_min_samples,
                 "objective_friction": self.objective_friction_min_samples}
        if category not in table:
            raise ValueError(f"unknown readiness category {category!r}")
        return table[category]

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

    # -- cross-process bridge (dirty tracking, in-memory only) -------------
    def dirty_tools(self) -> set[str]:
        """Tools with bank changes since the last persisted sync."""
        with self._lock:
            return set(self._dirty)

    def mark_clean(self, tool: str) -> None:
        with self._lock:
            self._dirty.discard(str(tool))
