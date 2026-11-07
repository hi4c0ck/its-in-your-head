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

import hashlib
import json
import logging
import math
import struct
import time
from pathlib import Path

from .anchors import ANCHORS, DIMENSIONS, resolve_data_dir
from .resemblance import cosine

logger = logging.getLogger(__name__)

PATTERN_BANK_VERSION = "1"
PATTERN_CACHE_FILE = "pattern_vectors.json"

# Prompt side reuses the anchor positive banks by reference (same list
# objects) — the strings live in exactly one place (anchors.py).
PATTERN_BANK = (
    {
        "name": "directness",
        "prompt_paraphrases": ANCHORS["directness"]["positive"],
        "agent_paraphrases": [
            "I am answering bluntly with no preamble or pleasantries",
            "I am giving the short bottom line in plain words",
            "I am skipping the intro and leading with the answer",
            "I am not softening it with friendly filler",
        ],
    },
    {
        "name": "context_richness",
        "prompt_paraphrases": ANCHORS["context_richness"]["positive"],
        "agent_paraphrases": [
            "I am taking the supplied logs, trace, and background as given",
            "I am using the setup, constraints, and prior attempts already provided",
            "I am grounding myself in the pasted material before acting",
            "I am treating the attached details as my starting point",
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
            "I am deriving why this happens and proving the mechanism",
            "I am analyzing the trade-offs and weighing the hypotheses with numbers",
            "I am tracing the root cause step by step from first principles",
            "I am explaining the underlying reason, not just giving a label",
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
            "I am staying inside this module only, nothing wider",
            "I am stopping when that part is done and leaving the rest out",
            "I am limiting my change to just this one area",
            "I am not touching anything outside the boundary",
        ],
    },
)

# Confidence fusion constants (pinned; tuned later).
CONFIDENCE_SATURATION_N = 10
CONFIDENCE_ALPHA = 1.0 / math.log(1.0 + CONFIDENCE_SATURATION_N)  # n=10 -> 1.0
CONFIDENCE_HALF_LIFE_S = 7 * 24 * 60 * 60  # 7-day half-life
CONFIDENCE_LAMBDA = math.log(2.0) / CONFIDENCE_HALF_LIFE_S

# Reinforcement step size (pinned, D3 route A): EMA weight of each new
# reaction vector into the agent anchor. Small on purpose — one strong
# observation nudges the interpretation, it does not rewrite it.
REINFORCE_ALPHA = 0.05

# In-memory cache: (model_version, PATTERN_BANK_VERSION, inputs_digest)
# -> bank dict. A model bump must never compare across vector spaces,
# and a change in the custom/reinforced input sets changes the digest,
# so the key always carries both (same discipline as anchors.py file
# cache).
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


def _normalize(v):
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        return [0.0] * len(v)
    return [x / norm for x in v]


def _validate_custom(custom_patterns) -> list:
    """Valid custom entries (name/prompt/agent), invalid ones skipped.

    A bad entry is logged and skipped — it must never break the bank
    build. Duplicate names within the custom set: first wins.
    """
    if not custom_patterns:
        return []
    if not isinstance(custom_patterns, (list, tuple)):
        logger.warning(
            "its-in-your-head: custom_patterns not a list (%r); ignored",
            type(custom_patterns))
        return []
    valid = []
    seen = set()
    for entry in custom_patterns:
        if not isinstance(entry, dict):
            logger.warning(
                "its-in-your-head: custom pattern entry not an object "
                "(%r); skipped", entry)
            continue
        name = entry.get("name")
        prompt = entry.get("prompt", entry.get("prompt_paraphrases"))
        agent = entry.get("agent", entry.get("agent_paraphrases"))
        if not isinstance(name, str) or not name.strip():
            logger.warning(
                "its-in-your-head: custom pattern missing/invalid name "
                "(%r); skipped", entry)
            continue
        if name in seen:
            logger.warning(
                "its-in-your-head: duplicate custom pattern name %r; "
                "skipped", name)
            continue
        bad = False
        for label, lst in (("prompt", prompt), ("agent", agent)):
            if (not isinstance(lst, (list, tuple)) or not lst
                    or not all(isinstance(s, str) and s for s in lst)):
                logger.warning(
                    "its-in-your-head: custom pattern %r has invalid "
                    "%s list; skipped", name, label)
                bad = True
                break
        if bad:
            continue
        seen.add(name)
        valid.append({
            "name": name,
            "prompt": [str(s) for s in prompt],
            "agent": [str(s) for s in agent],
        })
    return valid


def _validate_reinforced(reinforced) -> dict:
    """Valid reinforced bank {name: {prompt_anchor, agent_anchor, evidence}}.

    Invalid entries are logged and skipped; a bad persisted row must
    never break the bank build.
    """
    if not reinforced:
        return {}
    if not isinstance(reinforced, dict):
        logger.warning(
            "its-in-your-head: reinforced bank not a mapping (%r); ignored",
            type(reinforced))
        return {}
    valid = {}
    for name, entry in reinforced.items():
        if not isinstance(name, str) or not name:
            logger.warning(
                "its-in-your-head: reinforced entry with invalid name "
                "(%r); skipped", name)
            continue
        if not isinstance(entry, dict):
            logger.warning(
                "its-in-your-head: reinforced entry %r not an object; "
                "skipped", name)
            continue
        anchors = {}
        bad = False
        for key in ("prompt_anchor", "agent_anchor"):
            vec = entry.get(key)
            if (not isinstance(vec, (list, tuple)) or not vec):
                logger.warning(
                    "its-in-your-head: reinforced entry %r missing "
                    "valid %s; skipped", name, key)
                bad = True
                break
            try:
                anchors[key] = [float(x) for x in vec]
            except (TypeError, ValueError):
                logger.warning(
                    "its-in-your-head: reinforced entry %r has non-numeric "
                    "%s; skipped", name, key)
                bad = True
                break
        if bad:
            continue
        if len(anchors["prompt_anchor"]) != len(anchors["agent_anchor"]):
            logger.warning(
                "its-in-your-head: reinforced entry %r anchor dims "
                "differ; skipped", name)
            continue
        try:
            evidence = int(entry.get("evidence") or 0)
        except (TypeError, ValueError):
            evidence = 0
        anchors["evidence"] = max(0, evidence)
        # Persisted updated_at (epoch float) rides along when present so a
        # later custom override can keep it; absent/invalid -> None.
        try:
            upd = entry.get("updated_at")
            anchors["updated_at"] = float(upd) if upd is not None else None
        except (TypeError, ValueError):
            anchors["updated_at"] = None
        valid[name] = anchors
    return valid


def _inputs_digest(custom_valid, reinforced_valid) -> str:
    """sha256 over the normalized (sorted) custom+reinforced inputs.

    Vectors built under different input sets must never be compared, so
    the digest is part of the cache key: presence of ANY custom or
    reinforced input changes the key.
    """
    payload = {
        "custom": sorted(
            ({"name": e["name"], "prompt": e["prompt"],
              "agent": e["agent"]} for e in custom_valid),
            key=lambda e: e["name"]),
        "reinforced": sorted(
            ({"name": n, "prompt_anchor": v["prompt_anchor"],
              "agent_anchor": v["agent_anchor"],
              "evidence": v["evidence"],
              "updated_at": v.get("updated_at")}
             for n, v in reinforced_valid.items()),
            key=lambda e: e["name"]),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _build_uncached(backend, custom_valid=(), reinforced_valid=None) -> dict:
    """Encode every paraphrase list through the backend, pinned order.

    Merge order: preset base, then reinforced overrides (persisted
    vectors replace the preset anchors for that name), then custom
    entries (same name: custom wins on vectors). A custom entry that
    overrides a name with a persisted reinforced entry INHERITS the
    persisted evidence count (max(persisted, 0)) and updated_at — the
    vectors are the custom ones, the accumulated evidence is not
    dropped. Custom entries with no reinforced entry get evidence 0.
    """
    if reinforced_valid is None:
        reinforced_valid = {}
    bank: dict = {}
    for entry in PATTERN_BANK:  # preset order, never set-order
        prompt_vecs = backend.encode(list(entry["prompt_paraphrases"]))
        agent_vecs = backend.encode(list(entry["agent_paraphrases"]))
        bank[entry["name"]] = {
            "prompt_anchor": _mean_normalized(prompt_vecs),
            "agent_anchor": _mean_normalized(agent_vecs),
            "evidence": 0,
            "updated_at": None,
        }
    for name, entry in reinforced_valid.items():
        bank[name] = {
            "prompt_anchor": list(entry["prompt_anchor"]),
            "agent_anchor": list(entry["agent_anchor"]),
            "evidence": int(entry.get("evidence") or 0),
            "updated_at": entry.get("updated_at"),
        }
    for entry in custom_valid:
        prompt_vecs = backend.encode(list(entry["prompt"]))
        agent_vecs = backend.encode(list(entry["agent"]))
        # Evidence inheritance: a custom override of a reinforced name
        # keeps the persisted evidence/updated_at on the fresh vectors.
        prior = bank.get(entry["name"]) if entry["name"] in reinforced_valid else None
        bank[entry["name"]] = {
            "prompt_anchor": _mean_normalized(prompt_vecs),
            "agent_anchor": _mean_normalized(agent_vecs),
            "evidence": int(prior["evidence"]) if prior else 0,
            "updated_at": prior["updated_at"] if prior else None,
        }
    return bank


def _pattern_cache_path(data_dir) -> Path:
    return Path(resolve_data_dir(data_dir)) / PATTERN_CACHE_FILE


def _load_file_cache(path: Path, model_version: str,
                     inputs_digest: str | None = None) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("pattern_bank_version") != PATTERN_BANK_VERSION:
        return None
    if data.get("model_version") != model_version:
        return None
    if data.get("inputs_digest") != inputs_digest:
        return None
    vectors = data.get("vectors")
    if not isinstance(vectors, dict) or not vectors:
        return None
    return vectors


def _save_file_cache(path: Path, model_version: str, bank: dict,
                     inputs_digest: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pattern_bank_version": PATTERN_BANK_VERSION,
        "model_version": model_version,
        "inputs_digest": inputs_digest,
        "patterns": list(bank),
        "vectors": bank,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)  # atomic on the same filesystem


def build_pattern_bank(backend, model_version: str | None = None,
                       data_dir=None, custom_patterns=None,
                       reinforced=None) -> dict:
    """Pattern name -> {prompt_anchor, agent_anchor, evidence}.

    Cached on (model_version, PATTERN_BANK_VERSION, inputs_digest): a
    model bump, bank bump, or ANY change to the custom/reinforced input
    sets always rebuilds — vectors built under different inputs or
    model versions are never compared or reused. Merge order: preset
    base, then reinforced overrides, then custom entries (custom wins
    on a shared name).
    """
    if model_version is None:
        model_version = getattr(backend, "model_version", "unknown")
    custom_valid = _validate_custom(custom_patterns)
    reinforced_valid = _validate_reinforced(reinforced)
    digest = _inputs_digest(custom_valid, reinforced_valid)
    key = (str(model_version), PATTERN_BANK_VERSION, digest)
    if data_dir is None:
        cached = _BANK_CACHE.get(key)
        if cached is not None:
            return {n: dict(v) for n, v in cached.items()}
        bank = _build_uncached(backend, custom_valid, reinforced_valid)
        _BANK_CACHE[key] = bank
        return {n: dict(v) for n, v in bank.items()}
    path = _pattern_cache_path(data_dir)
    cached = _load_file_cache(path, str(model_version), digest)
    if cached is not None:
        return cached
    bank = _build_uncached(backend, custom_valid, reinforced_valid)
    _save_file_cache(path, str(model_version), bank, digest)
    _BANK_CACHE[key] = bank
    return {n: dict(v) for n, v in bank.items()}


def reinforce(pattern_entry, reaction_vector, alpha=None) -> dict:
    """One EMA step on the AGENT anchor only (D3 route A).

    Repeated high-amplification observations of how the agent
    interprets a pattern gradually move the agent-side interpretation
    anchors toward the observed reaction vectors; the prompt side is
    authored ground truth and is never drifted here.

    updated = normalize((1-alpha) * agent_anchor + alpha * reaction).
    ``evidence`` increments by one. The input entry is not mutated;
    the prompt anchor is carried over unchanged.
    """
    if alpha is None:
        alpha = REINFORCE_ALPHA
    alpha = float(alpha)
    agent = [float(x) for x in pattern_entry["agent_anchor"]]
    reaction = [float(x) for x in reaction_vector]
    if len(agent) != len(reaction):
        raise ValueError("agent anchor / reaction dim mismatch")
    blended = [(1.0 - alpha) * a + alpha * r
               for a, r in zip(agent, reaction)]
    try:
        evidence = int(pattern_entry.get("evidence") or 0)
    except (TypeError, ValueError):
        evidence = 0
    return {
        "prompt_anchor": list(pattern_entry["prompt_anchor"]),
        "agent_anchor": _normalize(blended),
        "evidence": evidence + 1,
    }


def _pack_vector(vector) -> bytes | None:
    if not vector:
        return None
    try:
        return struct.pack(f"<{len(vector)}f",
                           *(float(x) for x in vector))
    except (TypeError, ValueError):
        return None


def _unpack_vector(blob) -> list | None:
    if not blob or len(blob) % 4:
        return None
    try:
        return list(struct.unpack(f"<{len(blob) // 4}f", bytes(blob)))
    except (TypeError, struct.error):
        return None


_PATTERN_BANK_CREATE = """CREATE TABLE IF NOT EXISTS pattern_bank (
    pattern_name TEXT PRIMARY KEY,
    prompt_anchor BLOB NOT NULL,
    agent_anchor BLOB NOT NULL,
    evidence INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
)"""


def persist_reinforced(conn, entries) -> None:
    """Persist reinforced anchors: struct-packed float32 BLOBs, upsert
    per pattern. Derived vectors only; ``pattern_name`` is a bank name
    (preset or custom), never free text; no raw content column exists.

    Pulse persistence discipline: log-and-never-raise — a persistence
    failure must not touch the live pipeline.
    """
    try:
        if not isinstance(entries, dict) or not entries:
            return
        try:
            conn.execute(_PATTERN_BANK_CREATE)
        except Exception:
            pass  # bootstrap normally creates it; keep going anyway
        for name, entry in entries.items():
            if not isinstance(name, str) or not name:
                logger.warning(
                    "its-in-your-head: reinforced persist: invalid name "
                    "%r; skipped", name)
                continue
            if not isinstance(entry, dict):
                continue
            prompt_blob = _pack_vector(entry.get("prompt_anchor"))
            agent_blob = _pack_vector(entry.get("agent_anchor"))
            if prompt_blob is None or agent_blob is None:
                logger.warning(
                    "its-in-your-head: reinforced persist: %r missing "
                    "valid anchors; skipped", name)
                continue
            try:
                evidence = int(entry.get("evidence") or 0)
            except (TypeError, ValueError):
                evidence = 0
            now = time.time()
            existing = None
            try:
                row = conn.execute(
                    "SELECT updated_at FROM pattern_bank "
                    "WHERE pattern_name = ?", (name,)).fetchone()
                existing = float(row[0]) if row else None
            except Exception:
                existing = None
            if existing is not None and now <= existing:
                now = existing + 0.001  # keep updated_at monotonic
            conn.execute(
                "INSERT OR REPLACE INTO pattern_bank "
                "(pattern_name, prompt_anchor, agent_anchor, evidence, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (name, prompt_blob, agent_blob, max(0, evidence), now))
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        logger.exception(
            "its-in-your-head: reinforced pattern persist failed")


def load_reinforced(conn) -> dict:
    """Load persisted reinforced anchors as
    {name: {prompt_anchor, agent_anchor, evidence, updated_at}} (float32
    tolerance). Empty/absent/unreadable -> {}. Log-and-never-raise.
    """
    try:
        rows = conn.execute(
            "SELECT pattern_name, prompt_anchor, agent_anchor, evidence, "
            "updated_at FROM pattern_bank").fetchall()
    except Exception:
        logger.exception(
            "its-in-your-head: reinforced pattern load failed")
        return {}
    out = {}
    for name, prompt_blob, agent_blob, evidence, updated_at in rows:
        prompt = _unpack_vector(prompt_blob)
        agent = _unpack_vector(agent_blob)
        if prompt is None or agent is None:
            continue
        try:
            ev = int(evidence or 0)
        except (TypeError, ValueError):
            ev = 0
        out[str(name)] = {
            "prompt_anchor": prompt,
            "agent_anchor": agent,
            "evidence": ev,
            "updated_at": updated_at,
        }
    return out


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
