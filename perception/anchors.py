"""Versioned anchor bank for the 6 pinned portrait dimensions (S2).

Each dimension has 5-10 positive + 5-10 negative paraphrase prompts (real,
varied, terse English). Reference vectors are pre-encoded via the active
backend and cached as JSON in the plugin data dir; the cache is invalidated
when the anchor-bank version OR the backend model_version changes. Build
order is deterministic: pinned dimension order, anchors in listed order.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import encoder

ANCHOR_BANK_VERSION = "1.0.0"
CACHE_FILE = "anchor_vectors.json"

DIMENSIONS = (
    "directness",
    "context_richness",
    "exploration",
    "analytical_depth",
    "iteration",
    "scope",
)

# 7 positive + 7 negative per dimension — real, varied, terse paraphrases.
ANCHORS: dict[str, dict[str, list[str]]] = {
    "directness": {
        "positive": [
            "Only the number, plainly.",
            "Just the answer, nothing extra.",
            "Only the verdict, bluntly, no preamble.",
            "The bottom line only, in plain words.",
            "Short answer only - skip the pleasantries.",
            "One plain line, straight to the point.",
            "Answer briefly, straight up, no fluff.",
        ],
        "negative": [
            "Walk me through your reasoning step by step.",
            "Explain the full background first.",
            "Tell me a long story about it.",
            "Start with a friendly introduction.",
            "Give me every caveat before the answer.",
            "Describe the whole history of this topic.",
            "Ease into it gently with some context.",
        ],
    },
    "context_richness": {
        "positive": [
            "Here is the context: the background, setup, and history, all supplied.",
            "Here is the full background and prior attempts, all supplied up front.",
            "Pasted below: the full trace and the logs we collected.",
            "Attached: the background, versions, and setup details, all given.",
            "Given up front: the environment, OS, constraints, and what was decided.",
            "Supplied material: docs, repro steps, and what was tried before.",
            "Context included: what we tried, what failed, and the current setup.",
        ],
        "negative": [
            "Fix it.",
            "Same issue as before, you know.",
            "It doesn't work.",
            "Do that thing again.",
            "Problem.",
            "Something is broken.",
            "Handle it.",
        ],
    },
    "exploration": {
        "positive": [
            "Brainstorm every option you can think of.",
            "What else could we try? List alternatives.",
            "Think sideways — what are we missing?",
            "Explore creative angles before deciding.",
            "Give me ten ideas, even wild ones.",
            "What if we took a completely different approach?",
            "Cast a wide net and see what turns up.",
        ],
        "negative": [
            "Just do the one thing. No options.",
            "Pick the obvious answer and stop.",
            "Don't overthink it.",
            "Execute the plan as written.",
            "One try, that is all we need.",
            "No more ideas, just ship it.",
            "Stick to the script, nothing else.",
        ],
    },
    "analytical_depth": {
        "positive": [
            "Why does this happen? Derive the mechanism from first principles.",
            "Prove which hypothesis fits with evidence and numbers.",
            "Analyze the trade-offs numerically and compare pros and cons.",
            "Find the root cause: trace the mechanism and stress-test the logic.",
            "Break the failure down into first principles and do a post-mortem.",
            "Analyze the attached material in depth and explain the root cause.",
            "Weigh the evidence, rank the hypotheses by likelihood, and justify the verdict.",
        ],
        "negative": [
            "Just make it go.",
            "Don't analyze, just patch it.",
            "Quick fix, no deep dive.",
            "Ship it, whatever works.",
            "Skip the trade-off talk.",
            "Try random stuff until it passes.",
            "No time for a full analysis.",
        ],
    },
    "iteration": {
        "positive": [
            "Revise that, try again with feedback.",
            "Iterate on the last version, keep the good parts.",
            "One more pass — tighten the wording.",
            "Refine this draft, then refine again.",
            "Take another swing at it.",
            "Loop back and polish the result.",
            "Keep going, improve it step by step.",
        ],
        "negative": [
            "One shot, get it right first time.",
            "Final answer, no revisions.",
            "Don't revisit this again.",
            "Perfect on the first try or nothing.",
            "Single pass only.",
            "Once is enough, move on for good.",
            "No loops, no second drafts.",
        ],
    },
    "scope": {
        "positive": [
            "Just this module, nothing else.",
            "Only this part - leave the rest alone.",
            "Just this one file; stop when it is fixed.",
            "Keep the change inside this area, nothing outside.",
            "Out of scope: everything else stays untouched.",
            "Only this endpoint, nothing wider.",
            "Just this function and its test, nothing else.",
        ],
        "negative": [
            "Rework the whole system around this.",
            "Also fix the dashboard while you are in there.",
            "Take care of everything at once.",
            "Expand this into a full redesign.",
            "Do the whole repo, all modules included.",
            "Big bang change across every layer.",
            "While you are here, overhaul the lot.",
        ],
    },
}

# Labeled fixture corpus for calibration lives with resemblance (imports here
# only for dimension names); kept there to avoid a circular dependency.


class AnchorBank:
    """Immutable-ish container; version pinned so stale caches are obvious."""

    def __init__(self, version: str = ANCHOR_BANK_VERSION,
                 anchors: dict | None = None) -> None:
        self.version = version
        self.anchors = anchors if anchors is not None else ANCHORS

    @property
    def dimensions(self) -> tuple[str, ...]:
        return DIMENSIONS

    def positive(self, dim: str) -> list[str]:
        return list(self.anchors[dim]["positive"])

    def negative(self, dim: str) -> list[str]:
        return list(self.anchors[dim]["negative"])


def default_bank() -> AnchorBank:
    return AnchorBank()


def validate_bank(bank: AnchorBank) -> None:
    for dim in DIMENSIONS:
        pos, neg = bank.positive(dim), bank.negative(dim)
        if not 5 <= len(pos) <= 10:
            raise ValueError(f"{dim}: need 5-10 positive anchors, got {len(pos)}")
        if not 5 <= len(neg) <= 10:
            raise ValueError(f"{dim}: need 5-10 negative anchors, got {len(neg)}")
        if len(set(pos)) != len(pos) or len(set(neg)) != len(neg):
            raise ValueError(f"{dim}: duplicate anchors")
        if not all(s.strip() for s in pos + neg):
            raise ValueError(f"{dim}: blank anchor")


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    """Plugin data dir. Never the plugin install dir (wiped by update)."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("ITS_IN_YOUR_HEAD_DATA_DIR")
    if env:
        return Path(env)
    try:  # pragma: no cover - lazy import; no I/O at module load
        from hermes_cli.plugin_storage import plugin_db  # noqa: F401
        # Storage path itself is derived by the caller when available.
    except Exception:
        pass
    home = Path.home()
    return home / "hermes-plugin-data" / encoder.PLUGIN_ID


def get_cache_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / CACHE_FILE


def build_anchor_vectors(backend, bank: AnchorBank | None = None) -> dict:
    """Deterministic encode of every anchor, in pinned dimension order."""
    bank = bank or default_bank()
    validate_bank(bank)
    out: dict[str, dict[str, list[list[float]]]] = {}
    for dim in bank.dimensions:  # pinned order, never set-order
        out[dim] = {
            "positive": backend.encode(bank.positive(dim)),
            "negative": backend.encode(bank.negative(dim)),
        }
    return out


def cache_payload(backend, bank: AnchorBank, vectors: dict) -> dict:
    return {
        "anchor_bank_version": bank.version,
        "model_version": backend.model_version,
        "dimensions": list(bank.dimensions),
        "vectors": vectors,
    }


def save_cache(cache_path: str | Path, backend, bank: AnchorBank, vectors: dict) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache_payload(backend, bank, vectors), separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(path)  # atomic on the same filesystem


def load_cache_if_valid(
    cache_path: str | Path,
    bank: AnchorBank,
    backend,
) -> dict | None:
    """Return cached vectors, or None (caller must rebuild) on any mismatch."""
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("anchor_bank_version") != bank.version:
        return None
    if data.get("model_version") != backend.model_version:
        return None
    if data.get("dimensions") != list(bank.dimensions):
        return None
    vectors = data.get("vectors")
    if not isinstance(vectors, dict) or set(vectors) != set(bank.dimensions):
        return None
    return vectors


def get_anchor_vectors(
    backend,
    bank: AnchorBank | None = None,
    data_dir: str | Path | None = None,
) -> dict:
    """Cache-through accessor: rebuild only when version/model/dims change."""
    bank = bank or default_bank()
    path = get_cache_path(resolve_data_dir(data_dir))
    cached = load_cache_if_valid(path, bank, backend)
    if cached is not None:
        return cached
    vectors = build_anchor_vectors(backend, bank)
    save_cache(path, backend, bank, vectors)  # re-embed on version bump
    return vectors
