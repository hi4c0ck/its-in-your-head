"""Preset pattern bank + deterministic amplification scoring (S3, D3).

Small shipped preset seeded from the six pinned portrait dimensions in
``anchors.py``: the prompt side reuses each dimension's positive paraphrase
bank (imported, never duplicated); the agent side is newly authored
first-person phrases describing how the agent reasons when it notices the
pattern. Zero-network, deterministic, no LLM anywhere in this module.

Amplitude is NEVER vector magnitude: only cosines and their differences
(prompt/agent affinities, competitor margin, amplification) are used.
``confidence_from_evidence`` is a first-principles fusion (count saturation
x agreement x exponential recency decay), pinned here and tuned later.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .anchors import ANCHORS, DIMENSIONS, resolve_data_dir
from .resemblance import cosine

PATTERN_BANK_VERSION = "1"
PATTERN_CACHE_FILE = "pattern_vectors.json"

# Prompt side reuses the anchor positive banks by reference (same list
# objects) — the strings live in exactly one place (anchors.py).
PATTERN_BANK = (
    {
        "name": "directness",
        "prompt_paraphrases": ANCHORS["directness"]["positive"],
        "agent_paraphrases": [
            "I am giving the user the shortest direct answer I can",
            "I am skipping preamble and going straight to the result",
            "I am keeping my reply to one tight sentence",
            "I am dropping caveats the user did not ask for",
        ],
    },
    {
        "name": "context_richness",
        "prompt_paraphrases": ANCHORS["context_richness"]["positive"],
        "agent_paraphrases": [
            "I am gathering the full background before acting",
            "I am checking the logs and config the user provided",
            "I am using every constraint the user listed",
            "I am grounding my answer in the supplied detail",
        ],
    },
    {
        "name": "exploration",
        "prompt_paraphrases": ANCHORS["exploration"]["positive"],
        "agent_paraphrases": [
            "I am brainstorming alternatives before deciding",
            "I am listing several options for the user to pick from",
            "I am looking for angles the user may have missed",
            "I am widening the search before narrowing down",
        ],
    },
    {
        "name": "analytical_depth",
        "prompt_paraphrases": ANCHORS["analytical_depth"]["positive"],
        "agent_paraphrases": [
            "I am decomposing the user's request into sub-goals",
            "I am checking invariants before proposing a change",
            "I am comparing trade-offs with numbers",
            "I am weighing the evidence before ranking options",
        ],
    },
    {
        "name": "iteration",
        "prompt_paraphrases": ANCHORS["iteration"]["positive"],
        "agent_paraphrases": [
            "I am revising my previous answer with the new feedback",
            "I am keeping the good parts and tightening the rest",
            "I am taking another pass over the draft",
            "I am polishing the result step by step",
        ],
    },
    {
        "name": "scope",
        "prompt_paraphrases": ANCHORS["scope"]["positive"],
        "agent_paraphrases": [
            "I am keeping the change to the smallest area possible",
            "I am touching only the module the user named",
            "I am limiting the blast radius of my fix",
            "I am staying local and leaving the rest alone",
        ],
    },
)

# Confidence fusion constants (pinned; tuned later).
CONFIDENCE_SATURATION_N = 10
CONFIDENCE_ALPHA = 1.0 / math.log(1.0 + CONFIDENCE_SATURATION_N)  # n=10 -> 1.0
CONFIDENCE_HALF_LIFE_S = 7 * 24 * 60 * 60  # 7-day half-life
CONFIDENCE_LAMBDA = math.log(2.0) / CONFIDENCE_HALF_LIFE_S

# In-memory cache: (model_version, PATTERN_BANK_VERSION) -> bank dict.
# A model bump must never compare across vector spaces, so the key always
# carries the model version (same discipline as anchors.py file cache).
_BANK_CACHE: dict = {}


def _mean_normalized(vectors: list[list[float]]) -> list[float]:
    """Component-wise mean of a paraphrase list, L2-normalized."""
    if not vectors:
        return []
    dim = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in mean))
    if norm == 0:
        return [0.0] * dim
    return [x / norm for x in mean]


def _build_uncached(backend) -> dict:
    """Encode every paraphrase list through the backend, pinned order."""
    bank: dict = {}
    for entry in PATTERN_BANK:  # preset order, never set-order
        prompt_vecs = backend.encode(list(entry["prompt_paraphrases"]))
        agent_vecs = backend.encode(list(entry["agent_paraphrases"]))
        bank[entry["name"]] = {
            "prompt_anchor": _mean_normalized(prompt_vecs),
            "agent_anchor": _mean_normalized(agent_vecs),
        }
    return bank


def _pattern_cache_path(data_dir) -> Path:
    return Path(resolve_data_dir(data_dir)) / PATTERN_CACHE_FILE


def _load_file_cache(path: Path, model_version: str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("pattern_bank_version") != PATTERN_BANK_VERSION:
        return None
    if data.get("model_version") != model_version:
        return None
    names = [e["name"] for e in PATTERN_BANK]
    if data.get("patterns") != names:
        return None
    vectors = data.get("vectors")
    if not isinstance(vectors, dict) or set(vectors) != set(names):
        return None
    return vectors


def _save_file_cache(path: Path, model_version: str, bank: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pattern_bank_version": PATTERN_BANK_VERSION,
        "model_version": model_version,
        "patterns": [e["name"] for e in PATTERN_BANK],
        "vectors": bank,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)  # atomic on the same filesystem


def build_pattern_bank(backend, model_version: str | None = None,
                       data_dir=None) -> dict:
    """Pattern name -> {prompt_anchor, agent_anchor} normalized vectors.

    Cached on the pair (model_version, PATTERN_BANK_VERSION): a model bump
    or bank bump always rebuilds, never reuses vectors from another space.
    With ``data_dir`` the cache is also persisted to disk (mirrors the
    anchors.py file cache); without it the cache is in-memory only.
    """
    if model_version is None:
        model_version = getattr(backend, "model_version", "unknown")
    key = (str(model_version), PATTERN_BANK_VERSION)
    if data_dir is None:
        cached = _BANK_CACHE.get(key)
        if cached is not None:
            return {n: dict(v) for n, v in cached.items()}
        bank = _build_uncached(backend)
        _BANK_CACHE[key] = bank
        return {n: dict(v) for n, v in bank.items()}
    path = _pattern_cache_path(data_dir)
    cached = _load_file_cache(path, str(model_version))
    if cached is not None:
        return cached
    bank = _build_uncached(backend)
    _save_file_cache(path, str(model_version), bank)
    _BANK_CACHE[key] = bank
    return {n: dict(v) for n, v in bank.items()}


def score_turn(prompt_vector, reaction_vector, bank: dict) -> dict:
    """Score one turn against the pattern bank (deterministic, cosine-only).

    Returns {"patterns": [...], "dim_signals": {...}}. Patterns are sorted
    by agent_affinity descending, None-valued entries last. ``evidence`` is
    always 0 here (the engine accumulates) and ``confidence`` is None
    (filled by the caller from accumulated evidence).
    """
    entries = []
    for name, anchors in bank.items():
        prompt_affinity = cosine(list(prompt_vector), list(anchors["prompt_anchor"]))
        if reaction_vector is None:
            agent_affinity = None
        else:
            agent_affinity = cosine(list(reaction_vector), list(anchors["agent_anchor"]))
        entries.append({
            "name": name,
            "prompt_affinity": prompt_affinity,
            "agent_affinity": agent_affinity,
        })

    scored = [e["agent_affinity"] for e in entries if e["agent_affinity"] is not None]
    patterns = []
    for e in entries:
        agent = e["agent_affinity"]
        if agent is None or len(scored) < 2:
            margin = None
        else:
            # max over OTHER patterns: drop one occurrence of self
            rest = list(scored)
            rest.remove(agent)
            margin = agent - max(rest) if rest else None
        amplification = None if (agent is None or e["prompt_affinity"] is None) \
            else agent - e["prompt_affinity"]
        patterns.append({
            "name": e["name"],
            "prompt_affinity": e["prompt_affinity"],
            "agent_affinity": agent,
            "margin": margin,
            "amplification": amplification,
            "evidence": 0,
            "confidence": None,
        })

    patterns.sort(key=lambda p: (p["agent_affinity"] is None,
                                 -(p["agent_affinity"] or 0.0)))

    dim_signals = {
        p["name"]: {"prompt": p["prompt_affinity"], "agent": p["agent_affinity"]}
        for p in patterns
    }
    return {"patterns": patterns, "dim_signals": dim_signals}


def confidence_from_evidence(n, agreement, age_s=None, now_s=None) -> float:
    """First-principles confidence fusion in [0, 1] (pinned, tuned later).

    ``alpha * log(1 + n) * agreement * exp(-lambda * age)`` where
    ``alpha = 1 / log(11)`` (so n=10 saturates at 1.0), agreement is in
    [0, 1], and ``lambda = ln(2) / (7 days in seconds)`` (7-day half-life).

    ``age_s`` is the age of the evidence in seconds; ``now_s`` is an
    optional reference time. If ``age_s`` looks like an epoch timestamp
    (>= 1e8) and ``now_s`` is provided at/after it, the age is computed as
    ``now_s - age_s``; otherwise ``age_s`` is taken as the age directly.
    No canonical formula exists for this fusion — it is calibrated in
    tests and tuned later against observed agreement rates.
    """
    n = max(0, int(n))
    agreement = max(0.0, min(1.0, float(agreement)))
    if age_s is None:
        age = 0.0
    else:
        age = float(age_s)
        if now_s is not None and age >= 1e8 and float(now_s) >= age:
            age = float(now_s) - age
    age = max(0.0, age)
    value = (CONFIDENCE_ALPHA * math.log(1.0 + n) * agreement
             * math.exp(-CONFIDENCE_LAMBDA * age))
    return max(0.0, min(1.0, value))
