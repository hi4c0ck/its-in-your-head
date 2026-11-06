"""Deterministic transition classifier between consecutive user prompts (S3).

Labels: continuation / correction / positive_feedback / new_topic / ambiguous.

Pure functions of (prev prompt features, current prompt features, time gap):
phrase flags + vector cosine + resemblance to continuation vs topic anchors
(S2 encoder). No I/O, no network, no clock reads — the caller supplies the
time gap. Silence / absence of a message is never a rating: an empty or
missing current prompt classifies as ``ambiguous``, never as positive
feedback (there is no silent-approval logic anywhere in this module).

``classify_texts`` is a thin convenience wrapper that encodes two texts with
the S2 Tier-0 backend (in-memory only) and delegates to the pure feature
classifier; it stores nothing.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import encoder as _encoder
from .resemblance import cosine

CONTINUATION = "continuation"
CORRECTION = "correction"
POSITIVE_FEEDBACK = "positive_feedback"
NEW_TOPIC = "new_topic"
AMBIGUOUS = "ambiguous"
LABELS = (CONTINUATION, CORRECTION, POSITIVE_FEEDBACK, NEW_TOPIC, AMBIGUOUS)

# Semantic thresholds on cosine(prev, curr) — Tier-0 sketch cosines run low
# (~0.0-0.9 observed); thresholds are pinned here, not tuned per call.
COS_CONTINUATION = 0.60
COS_NEW_TOPIC = 0.30
ANCHOR_MARGIN = 0.05
# A gap beyond this with sub-continuation similarity reads as a new topic.
LONG_GAP_SECONDS = 86400.0

# Phrase tables. Order of evaluation is precedence, not table order:
# correction > continuation > new_topic > positive_feedback > semantic.
# Continuation outranks positive feedback so that "Now implement it." (and
# "Great, now implement it.") is a follow-up instruction, never praise.
_CORRECTION = (
    r"completely wrong", r"\bwrong\b", r"\bincorrect\b", r"\bnot right\b",
    r"\bnot correct\b", r"\bdoesn'?t work\b", r"\bdoes not work\b",
    r"\bbroken\b", r"\bstart over\b", r"\bredo\b", r"\bthat'?s not\b",
)
_CONTINUATION = (
    r"\bnow implement\b", r"\bimplement it\b", r"\bnow do\b", r"\bdo it\b",
    r"\bdo that\b", r"\bgo ahead\b", r"\bcontinue\b", r"\bproceed\b",
    r"\bnext step\b", r"\bkeep going\b", r"\bnow build\b", r"\bnow write\b",
    r"\bnow fix\b", r"\band then\b", r"\bwhen you'?re done\b",
)
_NEW_TOPIC = (
    r"\banother\s+(project|topic|question|thing|task)\b",
    r"\bwhat about another\b", r"\bnew\s+(project|topic|question|task)\b",
    r"\bdifferent\s+(project|topic|question|task)\b",
    r"\bswitch\s+(to|topics?|gears|subjects?)\b", r"\bunrelated\b",
    r"\bseparate\s+question\b", r"\bnew subject\b", r"\bother topic\b",
)
_POSITIVE = (
    r"\bthank you\b", r"\bthanks\b", r"\bwell done\b", r"\bgood job\b",
    r"\blooks good\b", r"\bexcellent\b", r"\bperfect\b", r"\bgreat\b",
    r"\bawesome\b", r"\bnice\b", r"\bgood work\b", r"\bbrilliant\b",
)

# Anchor texts for resemblance-to-continuation vs resemblance-to-topic.
# Encoded in-memory by classify_texts; never persisted.
CONTINUATION_ANCHORS = (
    "Now implement it.", "Go ahead and do it.", "Continue with the next step.",
    "Proceed with that plan.", "Keep going, now build it.",
)
TOPIC_ANCHORS = (
    "What about another project?", "Switch to a different topic.",
    "Here is a new question on another subject.", "Unrelated: something else entirely.",
)

_FLAG_PATTERNS = {
    CORRECTION: tuple(re.compile(p) for p in _CORRECTION),
    CONTINUATION: tuple(re.compile(p) for p in _CONTINUATION),
    NEW_TOPIC: tuple(re.compile(p) for p in _NEW_TOPIC),
    POSITIVE_FEEDBACK: tuple(re.compile(p) for p in _POSITIVE),
}


def detect_phrases(text: str | None) -> frozenset:
    """Phrase flags for one prompt. Empty/None text -> no flags (never positive)."""
    if not text or not str(text).strip():
        return frozenset()
    t = str(text).casefold()
    return frozenset(
        label for label, pats in _FLAG_PATTERNS.items()
        if any(p.search(t) for p in pats)
    )


@dataclass(frozen=True)
class Transition:
    label: str
    cosine: float
    continuation_score: float | None = None
    topic_score: float | None = None
    time_gap_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "cosine": self.cosine,
            "continuation_score": self.continuation_score,
            "topic_score": self.topic_score,
            "time_gap_seconds": self.time_gap_seconds,
        }


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _anchor_mean(vector, anchor_vectors) -> float:
    if not anchor_vectors:
        return 0.0
    return _mean(cosine(vector, v) for v in anchor_vectors)


def classify_features(prev_vector, curr_vector, time_gap_seconds: float = 0.0, *,
                      prev_flags=frozenset(), curr_flags=frozenset(),
                      continuation_vectors=(), topic_vectors=()) -> Transition:
    """Pure classifier over feature vectors + phrase flags + time gap.

    No I/O. ``prev_vector``/``curr_vector`` are the S2 encoder vectors for
    the two prompts; flags come from ``detect_phrases``. A missing/empty
    current vector (silence) yields ``ambiguous`` — never positive feedback.
    """
    gap = float(time_gap_seconds or 0.0)
    if gap < 0 or not math.isfinite(gap):
        gap = 0.0
    if curr_vector is None or not curr_vector or all(v == 0.0 for v in curr_vector):
        return Transition(AMBIGUOUS, 0.0, None, None, gap)
    if prev_vector is None or not prev_vector or all(v == 0.0 for v in prev_vector):
        prev_vector = curr_vector  # no history: cosine is 1.0, phrases decide

    cos = cosine(list(prev_vector), list(curr_vector))
    cont_score = _anchor_mean(curr_vector, continuation_vectors)
    topic_score = _anchor_mean(curr_vector, topic_vectors)
    flags = frozenset(curr_flags or ())

    # 1. Explicit criticism wins over everything else.
    if CORRECTION in flags:
        return Transition(CORRECTION, cos, cont_score, topic_score, gap)
    # 2. Imperative follow-ups are continuations, NEVER positive feedback.
    if CONTINUATION in flags:
        return Transition(CONTINUATION, cos, cont_score, topic_score, gap)
    # 3. Explicit topic-switch language.
    if NEW_TOPIC in flags:
        return Transition(NEW_TOPIC, cos, cont_score, topic_score, gap)
    # 4. Explicit praise (only when not an imperative follow-up, per rule 2).
    if POSITIVE_FEEDBACK in flags:
        return Transition(POSITIVE_FEEDBACK, cos, cont_score, topic_score, gap)
    # 5. Semantic fallback: anchor resemblance first, then raw similarity.
    if continuation_vectors and topic_vectors:
        if cont_score >= topic_score + ANCHOR_MARGIN and cos >= COS_NEW_TOPIC:
            return Transition(CONTINUATION, cos, cont_score, topic_score, gap)
        if topic_score >= cont_score + ANCHOR_MARGIN and cos < COS_CONTINUATION:
            return Transition(NEW_TOPIC, cos, cont_score, topic_score, gap)
    if cos >= COS_CONTINUATION:
        return Transition(CONTINUATION, cos, cont_score, topic_score, gap)
    if cos < COS_NEW_TOPIC:
        return Transition(NEW_TOPIC, cos, cont_score, topic_score, gap)
    if gap > LONG_GAP_SECONDS:
        return Transition(NEW_TOPIC, cos, cont_score, topic_score, gap)
    return Transition(AMBIGUOUS, cos, cont_score, topic_score, gap)


def classify_texts(prev_text: str | None, curr_text: str | None, backend=None,
                   time_gap_seconds: float = 0.0) -> Transition:
    """Classify between two raw prompts (encoded in-memory, nothing stored).

    Convenience wrapper over ``classify_features`` using the S2 encoder and
    the pinned continuation/topic anchor texts.
    """
    if not curr_text or not str(curr_text).strip():
        # Silence is not approval: no message -> ambiguous, never positive.
        return Transition(AMBIGUOUS, 0.0, None, None, float(time_gap_seconds or 0.0))
    be = backend or _encoder.Tier0SketchBackend()
    texts = [prev_text or "", curr_text, *CONTINUATION_ANCHORS, *TOPIC_ANCHORS]
    vecs = be.encode(texts)
    prev_vec, curr_vec = vecs[0], vecs[1]
    n_cont = len(CONTINUATION_ANCHORS)
    cont_vecs = vecs[2:2 + n_cont]
    topic_vecs = vecs[2 + n_cont:]
    return classify_features(
        prev_vec, curr_vec, time_gap_seconds,
        prev_flags=detect_phrases(prev_text),
        curr_flags=detect_phrases(curr_text),
        continuation_vectors=cont_vecs,
        topic_vectors=topic_vecs,
    )


# Short alias for call sites that prefer a verb name.
classify_transition = classify_texts
