"""Deterministic adaptation rule engine (S7, v2 — directional).

Pure, inspectable rules. No file I/O, no network, no RNG, no memory-provider
retrieval, no LLM calls. The engine emits AT MOST ONE instruction per turn
from a fixed per-channel template; the disabled-by-default gate lives in the
caller (injection.build_injection), not here.

Directional GOOD / FRICTION reinforcement: three independently-evaluated
evidence channels, each scoring the prompt against ONE tool's OWN prototype
bank only (never a combined GOOD+FRICTION average, and no positive/negative
cross-erase):

- GOOD                -> positive  ("Prefer ...")
- USER FRICTION       -> negative  ("Do not repeat ...")
- OBJECTIVE FRICTION  -> negative  ("Verify the result of ...")

A channel for tool T qualifies when ALL of:
1. T's own bank holds COMPATIBLE evidence (dimension + provenance match the
   prompt vector — see _live_bank_similarity; a blind Tier-0 into a foreign
   space never counts);
2. the prompt matches that bank at or above the channel threshold;
3. T's own category evidence count >= the category minimum (independent per
   category; the combined pulse.min_labeled is NOT a gate);
4. no conflicting-direction channel qualifies for this prompt.

When a positive and a negative channel both qualify, no clear direction
exists for this prompt -> no instruction (the banks keep accumulating
regardless). GOAL maturity is never a gate: the prompt-to-prototype
similarity is the direct relevance check.
"""
from __future__ import annotations

import math
import re

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from ..pulse import prototypes as _protos
except Exception:
    try:
        from pulse import prototypes as _protos  # type: ignore[no-redef]
    except Exception:
        _protos = None

# C4 defaults (core/settings.py DEFAULTS; literals so this module stays
# importable without the core lane).
DEFAULT_FRICTION_THRESHOLD = 0.4
DEFAULT_GOAL_THRESHOLD = 0.8
DEFAULT_GOOD_THRESHOLD = 0.6
DEFAULT_MIN_LABELED = 3

# Fixed templates — the ONLY instruction shapes this engine can emit.
GOOD_TEMPLATE = "Prefer {tool} for this domain."
USER_FRICTION_TEMPLATE = ("Do not repeat the previous {tool} "
                          "approach for this domain.")
OBJECTIVE_FRICTION_TEMPLATE = ("Verify the result of {tool} before proceeding "
                               "in this domain.")

# Legacy single-template alias (surfaces / callers that reference TEMPLATE).
TEMPLATE = GOOD_TEMPLATE

# Transitions that suppress firing outright.
_SUPPRESS_LABELS = frozenset({"new_topic", "ambiguous"})

_MAX_DOMAIN_CHARS = 32

# Negative-source priority on similarity ties: explicit user feedback
# outranks objective execution evidence.
_SOURCE_RANK = {"user_friction": 1, "objective_friction": 2}

GOOD_RULE = "pulse-good"
USER_FRICTION_RULE = "pulse-user-friction"
OBJECTIVE_FRICTION_RULE = "pulse-objective-friction"

# The three inspectable rule definitions (name/match/template/evidence).
RULE_DEFINITIONS = (
    {
        "id": GOOD_RULE,
        "name": GOOD_RULE,
        "match": ("GOOD similarity >= pulse.thresholds.good AND good support "
                  ">= pulse.good_min_samples AND no qualifying FRICTION "
                  "channel"),
        "template": GOOD_TEMPLATE,
        "evidence": ("tool", "source", "similarity", "support",
                     "transition_label"),
    },
    {
        "id": USER_FRICTION_RULE,
        "name": USER_FRICTION_RULE,
        "match": ("user FRICTION similarity >= pulse.thresholds.friction AND "
                  "friction support >= pulse.user_friction_min_samples AND no "
                  "qualifying GOOD channel"),
        "template": USER_FRICTION_TEMPLATE,
        "evidence": ("tool", "source", "similarity", "support",
                     "transition_label"),
    },
    {
        "id": OBJECTIVE_FRICTION_RULE,
        "name": OBJECTIVE_FRICTION_RULE,
        "match": ("objective FRICTION similarity >= "
                  "pulse.thresholds.friction AND objective support >= "
                  "pulse.objective_friction_min_samples AND no qualifying "
                  "GOOD channel"),
        "template": OBJECTIVE_FRICTION_TEMPLATE,
        "evidence": ("tool", "source", "similarity", "support",
                     "transition_label"),
    },
)


def list_rule_definitions() -> list:
    """Deterministic rule listing (name/match/template/evidence)."""
    return [dict(r) for r in RULE_DEFINITIONS]


def _cosine(a, b) -> float:
    if _protos is not None:
        try:
            return float(_protos.cosine(a, b))
        except Exception:
            pass
    if a is None or b is None or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)) / (na * nb)))


def _setting(settings, key, default):
    if settings is None:
        return default
    if isinstance(settings, dict):
        if key in settings:
            return settings[key]
        # nested shape {"pulse": {"thresholds": {...}, "min_labeled": n}}
        parts = key.split(".")
        cur = settings
        for part in parts:
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur is not None:
            return cur
    get = getattr(settings, "get", None)
    if callable(get):
        try:
            return get(key, default)
        except Exception:
            return default
    return default


def _short_domain(value) -> str:
    """Bounded domain noun: single token, <=32 chars, no prompt echo."""
    if not isinstance(value, str):
        value = str(value or "")
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9_\-/ ]", " ", text)
    words = [w for w in text.split() if w]
    if not words:
        return ""
    return " ".join(words[:2])[:_MAX_DOMAIN_CHARS].strip()


def _transition_label(signal):
    if signal is None:
        return None
    if isinstance(signal, str):
        return signal or None
    if isinstance(signal, dict):
        return signal.get("label") or None
    return getattr(signal, "label", None)


def _clamp01(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


# --- prompt vector + space identity ---------------------------------------
def _prompt_vector(prompt_features):
    """The active-encoder prompt vector (or None) + the explicit tool hint.

    The vector is expected to be produced by the same active encoder / vector
    space as the PULSE prototypes (the seam does this). A raw text string is
    accepted but yields NO vector -> live-bank channels stay quiet (never a
    blind Tier-0 into a foreign space).
    """
    if isinstance(prompt_features, str):
        return None, _short_domain(prompt_features)
    if not isinstance(prompt_features, dict):
        return None, ""
    vec = prompt_features.get("vector")
    vector = list(vec) if vec is not None else None
    tool_hint = str(prompt_features.get("tool")
                    or prompt_features.get("tool_name") or "")
    return vector, tool_hint


def _prompt_space(prompt_features):
    if isinstance(prompt_features, dict):
        return prompt_features.get("vector_space")
    return None


def _prompt_provenance(space):
    """(backend, model_version) from a vector-space identity, else None."""
    if space is None:
        return None
    if isinstance(space, (tuple, list)):
        if len(space) < 1 or space[0] is None:
            return None
        return (space[0], space[1] if len(space) > 1 else None)
    if isinstance(space, dict):
        backend = space.get("id") or space.get("backend")
        if backend is None:
            return None
        return (backend, space.get("model_version"))
    return None


# --- evidence normalization ------------------------------------------------
def _evidence_by_tool(pulse_state) -> dict:
    """Normalize any accepted pulse_state shape to tool -> evidence dict."""
    if pulse_state is None:
        return {}
    # PrototypeStore directly.
    if hasattr(pulse_state, "tools") and hasattr(pulse_state, "bank"):
        out = {}
        has_meta = hasattr(pulse_state, "bank_meta")
        for tool in pulse_state.tools():
            bank = pulse_state.bank(tool)
            out[str(tool)] = {
                "good_count": len(bank.get("good") or []),
                "friction_count": len(bank.get("friction") or []),
                "objective_friction": len(bank.get("objective_friction") or []),
                "labeled": pulse_state.labeled_count(tool),
                "_good_bank": list(bank.get("good") or []),
                "_friction_bank": list(bank.get("friction") or []),
                "_objective_bank": list(bank.get("objective_friction") or []),
                "_bank_meta": pulse_state.bank_meta(tool) if has_meta else None,
            }
        return out
    if isinstance(pulse_state, dict):
        inner = None
        for key in ("tools", "per_tool", "by_tool"):
            if isinstance(pulse_state.get(key), dict):
                inner = pulse_state[key]
                break
        if inner is None and isinstance(pulse_state.get("matrix"), list):
            inner = {}
            for row in pulse_state["matrix"]:
                if isinstance(row, dict) and row.get("tool_name"):
                    inner[str(row["tool_name"])] = row
        if inner is None:
            inner = {k: v for k, v in pulse_state.items()
                     if isinstance(v, dict) and k not in (
                         "prototypes", "store", "settings")}
        out = {}
        store = pulse_state.get("prototypes") or pulse_state.get("store")
        store_ev = _evidence_by_tool(store) if store is not None else {}
        capsules = (pulse_state.get("capsules")
                    or pulse_state.get("goal_vectors") or {})
        for tool, ev in {**store_ev, **inner}.items():
            ev = dict(ev) if isinstance(ev, dict) else {}
            # Merge explicit evidence over store-derived evidence.
            merged = dict(store_ev.get(str(tool), {}))
            merged.update(ev)
            if str(tool) in capsules:
                caps = capsules[str(tool)]
                vecs = []
                for c in caps if isinstance(caps, (list, tuple)) else [caps]:
                    vecs.append(c.get("vector") if isinstance(c, dict) else c)
                merged["_capsule_vectors"] = [v for v in vecs if v is not None]
            out[str(tool)] = merged
        return out
    if isinstance(pulse_state, (list, tuple)):
        out = {}
        for row in pulse_state:
            if isinstance(row, dict) and row.get("tool_name"):
                out[str(row["tool_name"])] = row
        return out
    return {}


# --- per-channel evidence counts (independent, never summed) ---------------
def _good_count(ev: dict) -> int:
    if isinstance(ev.get("good_count"), (int, float)):
        return int(ev["good_count"])
    gb = ev.get("_good_bank")
    if gb is not None:
        return len(gb)
    good = ev.get("good")
    if isinstance(good, (list, tuple)):
        return len(good)
    return 1 if ev.get("has_good_prototype") else 0


def _friction_count(ev: dict) -> int:
    if isinstance(ev.get("friction_count"), (int, float)):
        return int(ev["friction_count"])
    fb = ev.get("_friction_bank")
    if fb is not None:
        return len(fb)
    return 0


def _objective_count(ev: dict) -> int:
    for key in ("objective_friction_count", "objective_friction",
                "objective_count"):
        if isinstance(ev.get(key), (int, float)):
            return int(ev[key])
    ob = ev.get("_objective_bank")
    return len(ob) if ob is not None else 0


def _category_min(settings, category: str) -> int:
    """Per-category evidence minimum; the combined pulse.min_labeled is a
    fallback default only, never a cross-category gate."""
    key = {"good": "pulse.good_min_samples",
           "user_friction": "pulse.user_friction_min_samples",
           "objective_friction":
               "pulse.objective_friction_min_samples"}[category]
    val = _setting(settings, key, None)
    if val is None:
        val = _setting(settings, "pulse.min_labeled", DEFAULT_MIN_LABELED)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return DEFAULT_MIN_LABELED


# --- per-channel similarity (own bank only, compatibility-gated) ---------
def _live_bank_similarity(bank, meta, label, vector, space):
    """Strongest cosine to a live prototype bank, gated on vector-space
    compatibility (§3): dimension must match and a known provenance conflict
    (bank encoded by a different active backend) invalidates the channel — it
    must not silently become a valid reinforcement signal."""
    if vector is None or not bank:
        return None
    dims = {len(v) for v in bank if v is not None}
    if len(dims) != 1 or len(vector) not in dims:
        return None
    prov = (meta or {}).get(label)
    if prov is not None:
        prompt_prov = _prompt_provenance(space)
        if prompt_prov is not None and prompt_prov[0] != prov[0]:
            return None
    return max(_cosine(vector, v) for v in bank if v is not None)


def _good_similarity(ev, vector, space):
    bank = ev.get("_good_bank")
    if bank:
        return _live_bank_similarity(
            bank, ev.get("_bank_meta"), "good", vector, space)
    for key in ("good_similarity", "good_value", "good"):
        val = ev.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if isinstance(val, (int, float)):
            return _clamp01(val)
    return None


def _user_friction_similarity(ev, vector, space):
    bank = ev.get("_friction_bank")
    if bank:
        return _live_bank_similarity(
            bank, ev.get("_bank_meta"), "friction", vector, space)
    for key in ("friction_similarity", "friction_value", "friction"):
        val = ev.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if isinstance(val, (int, float)):
            return _clamp01(val)
    return None


def _objective_similarity(ev, vector, space):
    bank = ev.get("_objective_bank")
    if bank:
        return _live_bank_similarity(
            bank, ev.get("_bank_meta"), "objective_friction", vector, space)
    for key in ("objective_friction_similarity", "objective_value"):
        val = ev.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if isinstance(val, (int, float)):
            return _clamp01(val)
    return None


# --- decision --------------------------------------------------------------
def _decision(candidate: dict, rule_id: str, template: str, label) -> dict:
    tool = candidate["tool"]
    return {
        "rule_id": rule_id,
        "tool": tool,
        "domain": _short_domain(tool),
        "source": candidate["source"],
        "similarity": candidate["similarity"],
        "support": candidate["support"],
        "instruction": template.format(tool=tool),
        "evidence": {
            "tool": tool,
            "source": candidate["source"],
            "similarity": candidate["similarity"],
            "support": candidate["support"],
            "transition_label": label,
        },
    }


def evaluate_rules(settings, prompt_features, pulse_state,
                   transitions_signal=None) -> list:
    """Return [] or [one match]. Pure + deterministic.

    Three independent channels; a conflicting positive+negative pair for the
    same prompt suppresses the injection (no clear direction).
    """
    good_thr = float(_setting(
        settings, "pulse.thresholds.good", DEFAULT_GOOD_THRESHOLD))
    fric_thr = float(_setting(
        settings, "pulse.thresholds.friction", DEFAULT_FRICTION_THRESHOLD))
    vector, _tool_hint = _prompt_vector(prompt_features)
    space = _prompt_space(prompt_features)
    evidence = _evidence_by_tool(pulse_state)
    if not evidence:
        return []
    label = _transition_label(transitions_signal)
    good_min = _category_min(settings, "good")
    uf_min = _category_min(settings, "user_friction")
    of_min = _category_min(settings, "objective_friction")

    positive: list = []
    negative: list = []
    for tool in sorted(evidence):
        ev = evidence[tool]
        g_sim = _good_similarity(ev, vector, space)
        f_sim = _user_friction_similarity(ev, vector, space)
        o_sim = _objective_similarity(ev, vector, space)
        if g_sim is not None and g_sim >= good_thr \
                and _good_count(ev) >= good_min:
            positive.append({"tool": tool, "source": "good",
                             "similarity": g_sim, "support": _good_count(ev)})
        if f_sim is not None and f_sim >= fric_thr \
                and _friction_count(ev) >= uf_min:
            negative.append({"tool": tool, "source": "user_friction",
                             "similarity": f_sim,
                             "support": _friction_count(ev)})
        if o_sim is not None and o_sim >= fric_thr \
                and _objective_count(ev) >= of_min:
            negative.append({"tool": tool, "source": "objective_friction",
                             "similarity": o_sim,
                             "support": _objective_count(ev)})

    # §6: a positive and a negative channel both qualifying for the same
    # prompt is a contradiction -> no clear direction -> suppress (the banks
    # keep accumulating, but this turn gets no instruction).
    if positive and negative:
        return []

    out: list = []
    if positive:
        best = min(positive, key=lambda c: (-c["similarity"], c["tool"]))
        out = [_decision(best, GOOD_RULE, GOOD_TEMPLATE, label)]
    elif negative:
        best = min(negative,
                   key=lambda c: (-c["similarity"],
                                  _SOURCE_RANK.get(c["source"], 9),
                                  c["tool"]))
        if best["source"] == "user_friction":
            out = [_decision(best, USER_FRICTION_RULE,
                             USER_FRICTION_TEMPLATE, label)]
        else:
            out = [_decision(best, OBJECTIVE_FRICTION_RULE,
                            OBJECTIVE_FRICTION_TEMPLATE, label)]

    if out and label in _SUPPRESS_LABELS and out[0]["similarity"] < 1.0:
        return []
    return out


# Alias for call sites that prefer a verb name.
match_rules = evaluate_rules
