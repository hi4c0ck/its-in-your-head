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

# L11 data-calibrated constants (live snapshot, 23 pattern_affinity_history
# rows, 2026-11-07). Margin-aware branch only; margin=None keeps the legacy
# constants above exactly.
# Margin gate: 1 - exp(-margin / K). Observed top_margin: min 0.0016 /
# median 0.02304 / max 0.06659. K = median / ln(2) so the median margin
# lands at 0.5x: K = 0.02304 / 0.6931 = 0.0332. Then margin 0.20 -> 0.998x
# (exceeds the ~0.97x target), 0.067 -> 0.867x, 0.002 -> 0.058x.
CONFIDENCE_MARGIN_K = 0.0332
# Alpha fitted so fresh (age<1h) ev=2, agreement=0.5, median margin
# (gate 0.5) displays 0.45: alpha = 0.45 / (ln(3) * 0.5 * 0.5) = 1.639.
# The 7-day half-life (CONFIDENCE_LAMBDA) is unchanged in the calibrated
# branch, preserving the decay intent; at 1-2h ages the decay factor is
# ~0.99, so hours-old rows stay below fresh ones only via the much larger
# agreement/margin spread, never by inversion of the age term itself.
CONFIDENCE_ALPHA_CAL = 1.639


def margin_gate(margin) -> float:
    """Smooth margin gate in [0, 1]: 1 - exp(-max(margin,0)/K)."""
    m = max(0.0, float(margin))
    return 1.0 - math.exp(-m / CONFIDENCE_MARGIN_K)

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

    # P1 mode fix: rank by agent affinity when it exists, else fall back to
    # prompt affinity. In prompt-only mode every agent affinity is None, so
    # the old key (None-last, -(agent or 0)) collapsed all entries to the
    # same score and retained bank order — misleading. Falling back to
    # prompt_affinity ranks the prompt-only list by actual prompt similarity.
    patterns.sort(key=lambda p: (-((p["agent_affinity"] if p["agent_affinity"]
                                    is not None else p["prompt_affinity"])
                                   or 0.0),))

    dim_signals = {
        p["name"]: {"prompt": p["prompt_affinity"], "agent": p["agent_affinity"]}
        for p in patterns
    }
    return {"patterns": patterns, "dim_signals": dim_signals}


def confidence_from_evidence(n, agreement, age_s=None, now_s=None,
                             margin=None, amplification=None) -> float:
    """Confidence fusion in [0, 1]; legacy (margin=None) and L11-calibrated
    (margin given) branches.

    Legacy (``margin is None``) — exact pre-L11 output, preserved for
    backward compatibility with existing engine call sites:
    ``alpha * log(1+n) * agreement * exp(-lambda*age)`` with
    ``alpha = 1/log(11)``, ``lambda = ln(2)/(7 days)``.

    Calibrated (``margin`` given) — live-data justification (23 rows,
    ``data/live-snapshot.db``): top-1 margins are noise-level
    (min 0.0016 / median 0.0230 / max 0.0666) yet legacy confidence has
    no margin sensitivity — ev=2 rows all display 0.229 regardless of
    margin. The calibrated branch therefore multiplies by
    ``margin_gate(margin) = 1 - exp(-margin/K)``, ``K = 0.0332``
    (median/ln2, so the median margin gates at 0.5x; 0.20 -> 0.998x),
    and uses ``CONFIDENCE_ALPHA_CAL = 1.639`` so fresh ev=2 /
    agreement 0.5 / median margin displays ~0.45 (not 0.229). The
    7-day half-life is unchanged, so age decay is monotone (no
    inversion: older evidence never scores above identical fresh
    evidence).

    ``amplification`` is accepted for call-site symmetry but does not
    affect the output: in the live rows negative-amplification rows
    have mean implied agreement 0.431 vs 0.574 for positive rows, but
    medians are identical (0.50), the correlation is weak (r=0.33,
    n=23), and amplification is mechanically agent-minus-prompt
    affinity, so a sign-aware term would double-count; kept symmetric.

    ``age_s``/``now_s`` semantics as before: sub-1e8 ``age_s`` is a
    plain age in seconds (engine passes plain seconds); an epoch-like
    ``age_s`` (>=1e8) with ``now_s`` at/after it is converted to
    ``now_s - age_s``. Result is clipped to [0, 1].
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
    if margin is None:
        value = (CONFIDENCE_ALPHA * math.log(1.0 + n) * agreement
                 * math.exp(-CONFIDENCE_LAMBDA * age))
    else:
        value = (CONFIDENCE_ALPHA_CAL * math.log(1.0 + n) * agreement
                 * margin_gate(margin)
                 * math.exp(-CONFIDENCE_LAMBDA * age))
    return max(0.0, min(1.0, value))


# ============================================================================
# Live expression (transient portrait gesture controls)
#
# Drawing controls, NOT psychological diagnoses: the controls below steer
# how the portrait moves during and just after an interaction. They are
# derived from the existing pattern-affinity numbers only (prompt-side and,
# when available, agent-side), with zero new perception machinery.
#
# Timestamp-based settle/hold/release arithmetic only — no background
# timer, no animation worker, no persisted live state (a process restart
# returns to neutral).
# ============================================================================

# Lifecycle targets (plan §3.3 initial tuning; the pane polls every ~0.85s,
# so the settle window is well under one poll interval).
EXPR_SETTLE_S = 0.4       # smooth arrival (~200-500 ms target)
EXPR_HOLD_S = 3.6        # expression hold, several seconds
EXPR_RELEASE_TAU_S = 2.0 # exponential release back to neutral rest

# Prompt-only confidence is deliberately low: one side of the pair.
EXPR_PROMPT_CONFIDENCE = 0.30
EXPR_PAIRED_CONFIDENCE_BASE = 0.35
EXPR_PAIRED_CONFIDENCE_SPAN = 0.45  # scaled by the top pattern's margin gate

# Curated initial mapping of pattern affinities -> gesture controls
# (plan §3.1: exploratory breadth, analytical directness). Tunable.
LIVE_EXPR_WEIGHTS = {
    "openness": {"exploration": 0.45, "context_richness": 0.35,
                 "scope": 0.20},
    "focus": {"directness": 0.45, "analytical_depth": 0.40,
              "iteration": 0.15},
}


def neutral_live_expression() -> dict:
    """Resting state: controls at neutral, no stage, no timestamp."""
    return {"openness": 0.0, "focus": 0.0, "resonance": 0.5,
            "tension": 0.5, "intensity": 0.0, "confidence": 0.0,
            "stage": "none", "updated_at": None}


def _affinity_control(affinities: dict, weights: dict) -> float:
    """Weighted deviation-from-0.5 of pattern affinities, in [-1, 1].

    Missing patterns are skipped (their weight is renormalized over the
    present ones) — absence of a signal is never a value.
    """
    num = 0.0
    den = 0.0
    for name, w in weights.items():
        a = affinities.get(name)
        if a is None:
            continue
        num += w * (float(a) - 0.5) * 2.0
        den += float(w)
    if den <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, num / den))


def derive_live_expression(entries, paired: bool = False, ts=None) -> dict:
    """Target gesture controls for one scored turn (pure, deterministic).

    ``entries`` is the ``score_turn`` pattern list (all six patterns with
    prompt_affinity / agent_affinity / amplification / margin).

    Stage 1 (``paired=False``, prompt only): openness/focus/intensity come
    from the prompt-side affinities; resonance and tension STAY None —
    the agent side is unavailable, and the missing channel is never
    fabricated. Confidence is the low prompt-only gate.

    Stage 2 (``paired=True``, reasoning available): openness/focus are
    refined by blending the two sides, and resonance/tension derive from
    the mean amplification (agent-minus-prompt divergence) over the
    patterns that carry both affinities. The two channels share one
    divergence signal and sum to 1.0 at neutral, so they stay bounded.
    """
    if not isinstance(entries, (list, tuple)) or not entries:
        target = neutral_live_expression()
        target["updated_at"] = ts
        return target
    prompt_aff = {}
    agent_aff = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if name is None:
            continue
        if e.get("prompt_affinity") is not None:
            prompt_aff[name] = float(e["prompt_affinity"])
        if e.get("agent_affinity") is not None:
            agent_aff[name] = float(e["agent_affinity"])
    openness = _affinity_control(prompt_aff, LIVE_EXPR_WEIGHTS["openness"])
    focus = _affinity_control(prompt_aff, LIVE_EXPR_WEIGHTS["focus"])
    if paired:
        blend = {n: (prompt_aff.get(n, 0.0) + agent_aff.get(n, 0.0)) / 2.0
                 for n in prompt_aff if n in agent_aff}
        openness = 0.5 * openness + 0.5 * _affinity_control(
            blend, LIVE_EXPR_WEIGHTS["openness"])
        focus = 0.5 * focus + 0.5 * _affinity_control(
            blend, LIVE_EXPR_WEIGHTS["focus"])
        amps = [float(e["amplification"]) for e in entries
                if isinstance(e, dict)
                and e.get("amplification") is not None]
        if amps:
            mean_amp = max(-1.0, min(1.0, sum(amps) / len(amps)))
            resonance = round(0.5 + mean_amp, 4)
            tension = round(0.5 - mean_amp, 4)
        else:
            resonance = None
            tension = None
        margin = None
        for e in entries:
            if isinstance(e, dict) and e.get("margin") is not None:
                try:
                    margin = float(e["margin"])
                except (TypeError, ValueError):
                    margin = None
                break
        if margin is not None:
            confidence = (EXPR_PAIRED_CONFIDENCE_BASE
                          + EXPR_PAIRED_CONFIDENCE_SPAN
                          * margin_gate(margin))
        else:
            confidence = EXPR_PAIRED_CONFIDENCE_BASE
        stage = "paired"
    else:
        resonance = None
        tension = None
        confidence = EXPR_PROMPT_CONFIDENCE
        stage = "prompt"
    openness = round(max(-1.0, min(1.0, openness)), 4)
    focus = round(max(-1.0, min(1.0, focus)), 4)
    deviation = max(abs(openness), abs(focus))
    if paired and resonance is not None:
        deviation = max(deviation, abs(resonance - 0.5) * 2.0,
                        abs(tension - 0.5) * 2.0)
    intensity = round(max(0.0, min(1.0, deviation)), 4)
    return {"openness": openness, "focus": focus,
            "resonance": resonance, "tension": tension,
            "intensity": intensity,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "stage": stage, "updated_at": ts}


def _smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, float(x)))
    return x * x * (3.0 - 2.0 * x)


def live_expression_display(base, target, target_ts, now) -> dict:
    """Settle/hold/release interpolation between events (pure).

    ``base`` is the DISPLAY snapshot taken the moment ``target`` was set
    (continuity: a new target settles from the current display value,
    never jumps), ``target_ts`` when it was set, ``now`` the read time.
    Timeline: smoothstep settle over EXPR_SETTLE_S, hold EXPR_HOLD_S,
    then exponential release toward neutral (resonance/tension toward
    their 0.5 rest midpoint). After ~3 release taus the display is
    clamped to rest. No timers: the same inputs always give the same
    display (deterministic; identical inputs never produce jitter).
    """
    neutral = neutral_live_expression()
    if not isinstance(target, dict) or target.get("stage") in (None, "none"):
        return neutral
    b = neutral if not isinstance(base, dict) else base
    now_f = float(now if now is not None else target_ts or 0.0)
    t = max(0.0, now_f - float(target_ts if target_ts is not None else now_f))
    released = False
    if t < EXPR_SETTLE_S:
        frac = _smoothstep(t / EXPR_SETTLE_S)
    elif t < EXPR_SETTLE_S + EXPR_HOLD_S:
        frac = 1.0
    else:
        frac = math.exp(-(t - EXPR_SETTLE_S - EXPR_HOLD_S) / EXPR_RELEASE_TAU_S)
        released = frac < 0.01
        if released:
            frac = 0.0
    # Release floor: a FULLY released expression returns to the NEUTRAL
    # REST, not to the previous expression — otherwise the portrait would
    # remain stuck in an old expression (acceptance: "gradually returns
    # to neutral"). Continuity still holds DURING settle (a new target
    # departs from the current display value); only the end-of-life
    # floor snaps to rest.
    out = {}
    for key in ("openness", "focus", "intensity", "confidence"):
        bv = b.get(key)
        bv = 0.0 if bv is None else float(bv)
        if released:
            bv = 0.0
        gv = target.get(key)
        gv = 0.0 if gv is None else float(gv)
        out[key] = round(bv + (gv - bv) * frac, 4)
    for key in ("resonance", "tension"):
        gv = target.get(key)
        if gv is None:
            out[key] = None
            continue
        bv = b.get(key)
        bv = 0.5 if bv is None else float(bv)
        if released:
            bv = 0.5
        out[key] = round(bv + (float(gv) - bv) * frac, 4)
    out["stage"] = target.get("stage")
    out["updated_at"] = target.get("updated_at")
    return out


# ============================================================================
# Pattern-pair morphology (bounded, evidence-gated drawing mutations)
#
# Recurring pairs of the six preset patterns are discovered from the
# already-persisted ``pattern_affinity_history`` top-three rows (bounded
# 512 rows, shared ``turn_key``, no new table/migration). A pair is
# promoted only on REPEATED co-occurrence that exceeds its expected
# independent rate (lift) — a single turn, or two merely frequent
# patterns, never mutate the drawing. Strength is bounded, accumulates
# slowly with turn count, and decays with recency weighting so stale
# evidence weakens the mutation.
# ============================================================================

PAIR_MIN_TURNS = 3          # distinct co-occurring turns required (a
                            # single turn can never create a mutation)
PAIR_MIN_LIFT = 1.20       # co-occurrence must exceed the independent
                           # expectation (for rare-ish patterns)
PAIR_COND_GATE = 0.95      # conditional co-occurrence P(other|this) gate
                           # for the frequently-occurring patterns where
                           # lift degenerates to ~1 by construction
PAIR_HALF_LIFE_S = 14.0 * 24.0 * 60.0 * 60.0  # recency weighting (days)
PAIR_EVIDENCE_TARGET = 10   # turns for full evidence accumulation
PAIR_SUPPORT_FLOOR = 0.5    # weighted-support level for full recency credit
MORPH_MAX_STRENGTH = 0.60   # bounded maximum influence of any mutation
MORPH_ACTIVE_FLOOR = 0.02  # below this the pair is known but too weak/stale
                           # to touch the drawing (neutral wins)

# Curated initial artistic hypotheses (pair -> drawing motif), NOT facts
# about the user: the pair detector decides whether a combination actually
# recurs; unmapped pairs fall back to the generic motif. Motif values are
# unit magnitudes scaled by the pair's strength.
PAIR_MOTIFS = {
    frozenset(("context_richness", "analytical_depth")): {
        "line_density": 0.35, "contour_asymmetry": 0.05,
        "echo_strength": 0.10, "gesture_bias": 0.0},
    frozenset(("exploration", "scope")): {
        "line_density": 0.10, "contour_asymmetry": 0.25,
        "echo_strength": 0.05, "gesture_bias": 0.30},
    frozenset(("directness", "iteration")): {
        "line_density": 0.05, "contour_asymmetry": 0.10,
        "echo_strength": 0.30, "gesture_bias": -0.20},
}
GENERIC_MOTIF = {
    "line_density": 0.10, "contour_asymmetry": 0.15,
    "echo_strength": 0.10, "gesture_bias": 0.0,
}
MORPH_PARAMS = ("line_density", "contour_asymmetry",
               "echo_strength", "gesture_bias")


def neutral_morphology() -> dict:
    """No learned mutation: a documented empty shape, never fabricated."""
    return {"pair": None, "strength": 0.0, "confidence": 0.0, "evidence": 0,
            "available": False,
            **{p: 0.0 for p in MORPH_PARAMS}}


def group_turns(rows) -> list:
    """Group (ts, turn_key, name) history rows into per-turn observations.

    Returns ``[(ts, turn_key, [names])...]`` sorted by (ts, key): keyed
    turns pool their ranked names under the shared ``turn_key`` (max ts of
    the turn's rows); legacy rows (turn_key None/empty) are EACH their own
    single-name turn — a pair can only co-occur within a keyed top-three
    turn, so legacy data can never fabricate pair evidence.
    """
    keyed: dict = {}
    legacy = []
    for ts, turn_key, name in rows or []:
        if name is None or str(name) == "":
            continue
        key = None if turn_key in (None, "") else str(turn_key)
        if key is None:
            legacy.append((float(ts), None, [str(name)]))
        else:
            entry = keyed.get(key)
            if entry is None:
                keyed[key] = [float(ts), [str(name)]]
            else:
                entry[0] = max(entry[0], float(ts))
                if str(name) not in entry[1]:
                    entry[1].append(str(name))
    out = [(ts, key, sorted(names)) for key, (ts, names) in keyed.items()]
    out.extend(legacy)
    out.sort(key=lambda item: (item[0], str(item[1] or "")))
    return out


def pattern_pair_summary(observations, now_s=None) -> list:
    """Score recurring pattern pairs from per-turn observations (pure).

    ``observations`` is ``group_turns`` output: (ts, turn_key, [names]).
    Co-occurrence is measured over INDEPENDENT turns and compared against
    the product of the individual patterns' turn rates (lift); for
    frequently-occurring patterns lift degenerates, so the conditional
    co-occurrence P(other|this) is the second, equivalent gate. Recency
    weighting (half-life PAIR_HALF_LIFE_S) makes stale evidence lose
    support. Only gated pairs are returned (turns >= PAIR_MIN_TURNS AND
    (lift >= PAIR_MIN_LIFT OR cond >= PAIR_COND_GATE)), strongest first.
    Deterministic: the same rows in any order always produce the same
    summary.
    """
    obs = [o for o in (observations or [])
           if isinstance(o, (list, tuple)) and len(o) == 3
           and o[2]]
    if not obs:
        return []
    now = float(now_s) if now_s is not None else 0.0
    day_s = 24.0 * 60.0 * 60.0
    total_w = 0.0
    name_w: dict = {}
    pair_w: dict = {}
    pair_n: dict = {}
    for ts, _key, names in obs:
        age_days = max(0.0, now - float(ts)) / day_s
        w = 0.5 ** (age_days / (PAIR_HALF_LIFE_S / day_s))
        total_w += w
        uniq = list(dict.fromkeys(names))  # rank order, deduped
        for n in uniq:
            name_w[n] = name_w.get(n, 0.0) + w
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                p = (a, b) if a < b else (b, a)
                pair_w[p] = pair_w.get(p, 0.0) + w
                pair_n[p] = pair_n.get(p, 0) + 1
    if total_w <= 0.0:
        return []
    summary = []
    for pair in sorted(set(list(pair_w) + list(pair_n))):
        co_n = int(pair_n.get(pair, 0))
        if co_n < PAIR_MIN_TURNS:
            continue
        p_a = name_w.get(pair[0], 0.0) / total_w
        p_b = name_w.get(pair[1], 0.0) / total_w
        support = pair_w.get(pair, 0.0) / total_w
        expected = p_a * p_b
        lift = (support / expected) if expected > 0.0 else 0.0
        # Conditional co-occurrence covers the frequent-pattern case where
        # lift cannot exceed ~1 (both patterns in nearly every turn):
        # P(b|a) = support/p_a, P(a|b) = support/p_b. Independent patterns
        # give cond ~= the other's occurrence rate; associated pairs -> 1.
        cond = 0.0
        if p_a > 0.0:
            cond = max(cond, support / p_a)
        if p_b > 0.0:
            cond = max(cond, support / p_b)
        lift_factor = min(1.0, max(0.0, (lift - 1.0) / 1.0))
        cond_factor = min(1.0, max(0.0, (cond - 0.5) / 0.5))
        assoc = max(lift_factor, cond_factor)
        if lift < PAIR_MIN_LIFT and cond < PAIR_COND_GATE:
            continue
        evidence_factor = min(1.0, co_n / PAIR_EVIDENCE_TARGET)
        recency = min(1.0, support / PAIR_SUPPORT_FLOOR)
        strength = round(MORPH_MAX_STRENGTH
                         * evidence_factor * assoc * recency, 4)
        confidence = round(max(0.0, min(1.0,
                                        0.5 * min(1.0, co_n / 6.0)
                                        + 0.5 * assoc)), 4)
        last_ts = max(float(ts) for ts, _k, names in obs
                      if set(names) >= set(pair))
        summary.append({"pair": [pair[0], pair[1]], "turns": co_n,
                       "support": round(support, 4),
                       "lift": round(lift, 4),
                       "cond": round(cond, 4),
                       "strength": strength, "confidence": confidence,
                       "last_seen_ts": last_ts})
    summary.sort(key=lambda e: (-e["strength"], e["pair"][0], e["pair"][1]))
    return summary


def morphology_from_summary(summary) -> dict:
    """The strongest supported pair -> bounded visual mutation parameters."""
    if not summary:
        return neutral_morphology()
    top = summary[0]
    if not isinstance(top, dict) or float(
            top.get("strength", 0.0) or 0.0) < MORPH_ACTIVE_FLOOR:
        return neutral_morphology()  # known but too weak/stale: neutral wins
    pair = [str(x) for x in (top.get("pair") or [])]
    motif = PAIR_MOTIFS.get(frozenset(pair[:2]), GENERIC_MOTIF)
    s = float(top.get("strength", 0.0))
    out = {
        "pair": pair if len(pair) == 2 else None,
        "strength": round(s, 4),
        "confidence": round(float(top.get("confidence", 0.0)), 4),
        "evidence": int(top.get("turns", 0)),
        "available": True,
    }
    for param in MORPH_PARAMS:
        out[param] = round(motif.get(param, 0.0) * s, 4)
    return out


def morphology_from_rows(rows, now_s=None) -> dict:
    """One-call path: history rows -> gated pair summary -> morphology."""
    return morphology_from_summary(
        pattern_pair_summary(group_turns(rows), now_s=now_s))
