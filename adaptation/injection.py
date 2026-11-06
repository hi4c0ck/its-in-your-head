"""Pure injection builder (S7).

build_injection() is the ONLY caller of the rule engine on the
pre_llm_call path. It returns None unless adaptation.enabled is true AND
exactly one rule matches AND that instruction has not already been
injected for the same turn_id (bounded in-memory dedupe).

On fire it returns {"context": <instruction>, "meta": {...}} so the
pre_llm_call hook can return it verbatim. No file I/O, no network, no RNG,
no memory-provider retrieval, no LLM calls. The instruction text is a
fixed template from rules.TEMPLATE — never raw prompt text.

Every application is recorded to a bounded in-memory log, readable via
get_log() / last_application() / application_count() (the /mirror
adaptation status surface reads these). Injection text may persist in the
per-turn API sidecar — that is UI disclosure, not a persistence claim.
"""
from __future__ import annotations

import threading
import time
from collections import deque

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from . import rules as _rules
except Exception:
    try:
        from adaptation import rules as _rules  # type: ignore[no-redef]
    except Exception:
        import rules as _rules  # type: ignore[no-redef]

MAX_TRACKED_TURNS = 512
MAX_LOG_ENTRIES = 128

_lock = threading.Lock()
_injected_turns: dict = {}          # turn_id -> rule_id (bounded, FIFO)
_log: deque = deque(maxlen=MAX_LOG_ENTRIES)
_last: dict | None = None


def _enabled(settings) -> bool:
    if settings is None:
        return False
    if isinstance(settings, dict):
        if "adaptation.enabled" in settings:
            return bool(settings["adaptation.enabled"])
        nested = settings.get("adaptation")
        if isinstance(nested, dict) and "enabled" in nested:
            return bool(nested["enabled"])
        return False
    get = getattr(settings, "get", None)
    if callable(get):
        try:
            return bool(get("adaptation.enabled", False))
        except Exception:
            return False
    return False


def _turn_id(prompt_features, turn_id):
    if turn_id is not None:
        return turn_id
    if isinstance(prompt_features, dict):
        return prompt_features.get("turn_id")
    return None


def _record(entry: dict) -> None:
    global _last
    with _lock:
        _log.append(entry)
        _last = entry


def record_application(entry: dict) -> None:
    """Logging hook: record an application to the bounded in-memory log."""
    if isinstance(entry, dict):
        _record(dict(entry))


def get_log() -> list:
    with _lock:
        return [dict(e) for e in _log]


def last_application() -> dict | None:
    with _lock:
        return dict(_last) if _last is not None else None


def application_count() -> int:
    with _lock:
        return len(_log)


def injected_this_turn(turn_id) -> bool:
    if turn_id is None:
        return False
    with _lock:
        return turn_id in _injected_turns


def clear_state() -> None:
    """Reset dedupe + log (tests / explicit reset paths)."""
    global _last
    with _lock:
        _injected_turns.clear()
        _log.clear()
        _last = None


def build_injection(settings, prompt_features, pulse_state,
                    transitions_signal=None, turn_id=None) -> dict | None:
    """Pure builder: dict {"context", "meta"} on fire, else None.

    Deterministic for the same inputs (aside from the recorded log entry,
    which carries wall-clock ts and is not part of the return value).
    """
    if not _enabled(settings):
        return None
    tid = _turn_id(prompt_features, turn_id)
    if tid is not None and injected_this_turn(tid):
        return None
    try:
        matches = _rules.evaluate_rules(
            settings, prompt_features, pulse_state, transitions_signal)
    except Exception:
        return None
    if len(matches) != 1:
        return None
    match = matches[0]
    instruction = match.get("instruction")
    if not instruction:
        return None
    if tid is not None:
        with _lock:
            _injected_turns[tid] = match.get("rule_id")
            while len(_injected_turns) > MAX_TRACKED_TURNS:
                _injected_turns.pop(next(iter(_injected_turns)))
    result = {
        "context": instruction,
        "meta": {
            "rule_id": match.get("rule_id"),
            "tool": match.get("tool"),
            "evidence": dict(match.get("evidence") or {}),
            "turn_id": tid,
        },
    }
    _record({
        "ts": time.time(),
        "rule_id": match.get("rule_id"),
        "tool": match.get("tool"),
        "instruction": instruction,
        "evidence": dict(match.get("evidence") or {}),
        "turn_id": tid,
    })
    return result
