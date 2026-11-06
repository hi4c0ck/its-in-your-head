"""Deterministic adaptation rule engine (S7).

Pure, inspectable rules. No file I/O, no network, no RNG, no memory-provider
retrieval, no LLM calls. The engine emits AT MOST ONE instruction from a
fixed template; the disabled-by-default gate lives in the caller
(injection.build_injection), not here.

Inputs:
- prompt_features: dict with optional 'vector' / 'text' / 'domain' / 'tool',
  or a raw text string (encoded in-memory via the S2 Tier-0 backend).
- pulse_state: per-tool friction evidence. Accepted shapes:
  * a pulse.PrototypeStore (bank/labeled_count/tools),
  * a dict mapping tool -> evidence dict, or {"tools": {...}} /
    {"per_tool": {...}} / {"matrix": [rows]} wrappers, optionally with
    "prototypes"/"store" holding a PrototypeStore and "capsules"/"goal_vectors"
    holding per-tool capsule vectors for domain resemblance,
  * a list of matrix rows (pulse.matrix.build_matrix output).
- transitions_signal: label string, dict with 'label', or a
  perception.transitions.Transition. A 'new_topic' signal suppresses firing
  unless the domain match is strong (see _SUPPRESS_LABELS).

A tool qualifies when ALL of:
1. domain match — the prompt resembles a goal/capsule vector for the tool
   (cosine >= pulse.thresholds.goal) OR explicitly names the tool/domain;
2. friction confidence — strongest similarity to the tool's FRICTION bank
   >= pulse.thresholds.friction (C4 defaults);
3. min_labeled — labeled (GOOD+FRICTION) count >= pulse.min_labeled (C4);
4. at least one GOOD prototype exists for the tool (so the fixed
   'Prefer <tool>' instruction is grounded in labeled evidence).
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

# C4 defaults (core/settings.py DEFAULTS; duplicated here as literals so
# this module stays importable without the core lane).
DEFAULT_FRICTION_THRESHOLD = 0.4
DEFAULT_GOAL_THRESHOLD = 0.8
DEFAULT_GOOD_THRESHOLD = 0.6
DEFAULT_MIN_LABELED = 3

# Fixed template — the ONLY instruction shape this engine can emit.
TEMPLATE = "Prefer {tool} for this domain."

# Transitions that suppress firing outright.
_SUPPRESS_LABELS = frozenset({"new_topic", "ambiguous"})
# Labels that count as corroborating friction signals in evidence.
_FRICTION_LABELS = frozenset({"correction", "positive_feedback"})

_MAX_DOMAIN_CHARS = 32

# The single inspectable rule definition.
RULE_DEFINITIONS = (
    {
        "id": "domain-friction-prefer-good",
        "name": "domain-friction-prefer-good",
        "match": (
            "domain resemblance >= pulse.thresholds.goal AND friction "
            "similarity >= pulse.thresholds.friction AND labeled >= "
            "pulse.min_labeled AND >=1 GOOD prototype"
        ),
        "template": TEMPLATE,
        "evidence": ("tool", "friction", "labeled", "good_count",
                     "objective_friction", "transition_label"),
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


def _transition_label(signal) -> str | None:
    if signal is None:
        return None
    if isinstance(signal, str):
        return signal or None
    if isinstance(signal, dict):
        return signal.get("label") or None
    return getattr(signal, "label", None)


def _encode_text(text: str):
    try:
        try:
            from ..perception import encoder as _encoder
        except Exception:
            from perception import encoder as _encoder  # type: ignore[no-redef]
        backend = _encoder.Tier0SketchBackend()
        vecs = backend.encode([text])
        return list(vecs[0]) if vecs else None
    except Exception:
        return None


def _prompt_vector(prompt_features):
    if isinstance(prompt_features, str):
        return _encode_text(prompt_features), "", ""
    if not isinstance(prompt_features, dict):
        return None, "", ""
    vec = prompt_features.get("vector")
    if vec is None and prompt_features.get("text"):
        vec = _encode_text(str(prompt_features["text"]))
    domain = _short_domain(
        prompt_features.get("domain") or prompt_features.get("tool")
        or prompt_features.get("tool_name") or "")
    tool_hint = str(prompt_features.get("tool")
                    or prompt_features.get("tool_name") or "")
    return (list(vec) if vec is not None else None), domain, tool_hint


def _evidence_by_tool(pulse_state) -> dict:
    """Normalize any accepted pulse_state shape to tool -> evidence dict."""
    if pulse_state is None:
        return {}
    # PrototypeStore directly.
    if hasattr(pulse_state, "tools") and hasattr(pulse_state, "bank"):
        out = {}
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
        capsules = pulse_state.get("capsules") or pulse_state.get("goal_vectors") or {}
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


def _friction_similarity(ev: dict, vector):
    """Strongest FRICTION-bank similarity; None when the bank is empty."""
    if vector is None:
        return None
    bank = ev.get("_friction_bank") or ev.get("friction_bank")
    if bank:
        return max((_cosine(vector, v) for v in bank), default=None)
    for key in ("friction", "friction_value", "friction_similarity"):
        val = ev.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if isinstance(val, (int, float)):
            return max(0.0, min(1.0, float(val)))
    return None


def _goal_similarity(ev: dict, vector) -> float | None:
    if vector is None:
        return None
    vecs = ev.get("_capsule_vectors") or ev.get("capsule_vectors")
    if vecs:
        return max((_cosine(vector, v) for v in vecs), default=None)
    for key in ("goal", "goal_value", "goal_similarity", "resemblance"):
        val = ev.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if isinstance(val, (int, float)):
            return max(0.0, min(1.0, float(val)))
    return None


def _labeled_count(ev: dict) -> int:
    if isinstance(ev.get("labeled"), (int, float)):
        return int(ev["labeled"])
    if isinstance(ev.get("labeled_count"), (int, float)):
        return int(ev["labeled_count"])
    good = ev.get("good_count")
    fric = ev.get("friction_count")
    if isinstance(good, (int, float)) and isinstance(fric, (int, float)):
        return int(good) + int(fric)
    gb = ev.get("_good_bank")
    fb = ev.get("_friction_bank")
    if gb is not None or fb is not None:
        return len(gb or []) + len(fb or [])
    return 0


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


def _objective_count(ev: dict) -> int:
    if isinstance(ev.get("objective_friction"), (int, float)):
        return int(ev["objective_friction"])
    if isinstance(ev.get("objective_count"), (int, float)):
        return int(ev["objective_count"])
    ob = ev.get("_objective_bank")
    return len(ob) if ob is not None else 0


def evaluate_rules(settings, prompt_features, pulse_state,
                   transitions_signal=None) -> list:
    """Return [] or [one match]. Pure + deterministic."""
    fric_thr = float(_setting(
        settings, "pulse.thresholds.friction", DEFAULT_FRICTION_THRESHOLD))
    goal_thr = float(_setting(
        settings, "pulse.thresholds.goal", DEFAULT_GOAL_THRESHOLD))
    min_labeled = int(_setting(
        settings, "pulse.min_labeled", DEFAULT_MIN_LABELED))
    vector, domain_hint, tool_hint = _prompt_vector(prompt_features)
    evidence = _evidence_by_tool(pulse_state)
    if not evidence:
        return []
    label = _transition_label(transitions_signal)

    candidates = []
    for tool in sorted(evidence):
        ev = evidence[tool]
        labeled = _labeled_count(ev)
        if labeled < min_labeled:
            continue
        good_n = _good_count(ev)
        if good_n < 1:
            continue
        friction = _friction_similarity(ev, vector)
        if friction is None or friction < fric_thr:
            continue
        # Domain match.
        goal = _goal_similarity(ev, vector)
        named = bool(tool_hint and tool_hint == tool)
        dom_named = bool(domain_hint and domain_hint == tool)
        if not (named or dom_named or (goal is not None and goal >= goal_thr)):
            continue
        candidates.append({
            "rule_id": RULE_DEFINITIONS[0]["id"],
            "tool": tool,
            "domain": _short_domain(domain_hint or tool),
            "friction": float(friction),
            "goal": (float(goal) if goal is not None else None),
            "labeled": labeled,
            "good_count": good_n,
            "objective_friction": _objective_count(ev),
            "transition_label": label,
        })
    if not candidates:
        return []
    # Deterministic best-tool selection: friction desc, then tool asc.
    candidates.sort(key=lambda c: (-c["friction"], c["tool"]))
    best = candidates[0]
    # new_topic suppresses the (single) rule unless friction is strong.
    if label in _SUPPRESS_LABELS and best["friction"] < 1.0:
        return []
    best["instruction"] = TEMPLATE.format(tool=best["tool"])
    best["evidence"] = {
        "tool": best["tool"],
        "friction": best["friction"],
        "labeled": best["labeled"],
        "good_count": best["good_count"],
        "objective_friction": best["objective_friction"],
        "transition_label": best["transition_label"],
        "goal": best["goal"],
    }
    return [best]


# Alias for call sites that prefer a verb name.
match_rules = evaluate_rules
