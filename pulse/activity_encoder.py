"""Activity capsules (S4).

A capsule is the unit of similarity for PULSE. It carries ONLY:

    tool + sanitized operation + status + duration bucket (+ vector)

Sanitized operation examples: ``read_file`` -> file-path basename,
``terminal`` -> command verb (first token). Full args and full results
are NEVER stored — there is no result-payload field in the schema
(C6 / ARCHITECTURE locked rule: capsules only).

Encoding goes through the S2 ``EmbeddingBackend`` interface and happens
on the post_tool_call / worker side only. ``handle_pre_tool_call`` is
the synchronous LIVE path: registry + session-state counters, and it
never calls ``encode()`` (no backend argument is even accepted).
"""
from __future__ import annotations

import os
import time

try:
    from .tool_registry import ToolRegistry, live_tick
except ImportError:  # pragma: no cover
    from pulse.tool_registry import ToolRegistry, live_tick  # type: ignore[no-redef]

# The complete capsule schema. Anything not listed here does not exist
# on a capsule — in particular no result/output payload field.
CAPSULE_FIELDS = frozenset({
    "tool_name", "operation", "status", "duration_bucket",
    "duration_ms", "text", "vector", "model_version",
    "tool_call_id", "session_id", "turn_id", "task_id", "ts",
})

_PATH_KEYS = ("path", "file", "filepath", "file_path", "filename",
              "name", "uri", "url")
_COMMAND_KEYS = ("command", "cmd", "shell_command")
_QUERY_KEYS = ("query", "pattern", "search", "text", "prompt")


def _basename(value) -> str:
    text = str(value or "").replace("\\", "/").rstrip("/")
    if not text:
        return ""
    base = os.path.basename(text)
    return base[:80] if base else ""


def _first_token(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split()[0][:80]


def _host(value) -> str:
    text = str(value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/")[0][:80]


def sanitize_operation(tool_name, args=None, event: dict | None = None) -> str:
    """Sanitized arg summary: basename / command verb / host / first token.

    Falls back to the bare tool name when no usable arg summary exists.
    Never returns full args or any result content.
    """
    tool = str(tool_name or (event or {}).get("tool_name") or "unknown")
    if not isinstance(args, dict):
        # C6 tool events carry args_chars only, never raw args.
        return tool
    lowered = {str(k).lower(): v for k, v in args.items()}
    # Path-like first (most specific), then command verb.
    for key in _PATH_KEYS:
        if lowered.get(key):
            if key in ("uri", "url"):
                host = _host(lowered[key])
                if host:
                    return f"{tool}:{host}"
            base = _basename(lowered[key])
            if base:
                return f"{tool}:{base}"
    for key in _COMMAND_KEYS:
        verb = _first_token(lowered.get(key))
        if verb:
            return f"{tool}:{verb}"
    for key in _QUERY_KEYS:
        token = _first_token(lowered.get(key))
        if token:
            return f"{tool}:{token}"
    # Generic: no free-text dump — a bounded op count, not the args.
    return f"{tool}:args={min(len(args), 99)}"


def duration_bucket(duration_ms) -> str:
    try:
        ms = float(duration_ms)
    except (TypeError, ValueError):
        return "unknown"
    if ms < 0:
        return "unknown"
    if ms < 100:
        return "instant"
    if ms < 1000:
        return "fast"
    if ms < 10000:
        return "medium"
    return "slow"


def capsule_text(tool_name, operation, status, bucket) -> str:
    return f"{tool_name} {operation} {status} {bucket}"


def build_capsule(event: dict, args: dict | None = None,
                  backend=None) -> dict:
    """Build (and optionally encode) a capsule from a C6 tool_end event.

    ``args`` is accepted only for sanitization into the operation field;
    it is never stored. ``result``/``result_type``/``result_chars`` from
    the event are not copied into the capsule.
    """
    if not isinstance(event, dict):
        raise TypeError("build_capsule expects a C6 tool event dict")
    tool = str(event.get("tool_name") or "unknown")
    status = str(event.get("status") or "ok").lower()
    duration_ms = event.get("duration_ms")
    bucket = duration_bucket(duration_ms)
    operation = sanitize_operation(tool, args=args, event=event)
    text = capsule_text(tool, operation, status, bucket)
    capsule = {
        "tool_name": tool,
        "operation": operation,
        "status": status,
        "duration_bucket": bucket,
        "duration_ms": duration_ms,
        "text": text,
        "vector": None,
        "model_version": None,
        "tool_call_id": event.get("tool_call_id"),
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "task_id": event.get("task_id"),
        "ts": event.get("ts") or time.time(),
    }
    assert set(capsule) == set(CAPSULE_FIELDS)  # schema guard: no extras
    if backend is not None:
        encode_capsule(capsule, backend)
    return capsule


def encode_capsule(capsule: dict, backend) -> dict:
    """Worker-side: encode capsule text via the S2 EmbeddingBackend."""
    (vector,) = backend.encode([capsule["text"]])
    capsule["vector"] = list(vector)
    capsule["model_version"] = getattr(backend, "model_version", None)
    return capsule


def encode_goal_text(text: str, backend) -> list[float]:
    """Goal capsule = last user prompt encoded (worker side)."""
    (vector,) = backend.encode([str(text or "")])
    return list(vector)


# -- hot path (pre_tool_call) vs worker path (post_tool_call) ------------

def handle_pre_tool_call(event: dict, registry: ToolRegistry,
                         session_state=None) -> dict:
    """Synchronous LIVE path: row create/increment + counter bump.

    Deliberately takes NO backend and calls NO encode() — the hot path
    stays counter-only (C5). Encoding happens in handle_post_tool_call.
    """
    row = live_tick(registry, event)
    if session_state is not None:
        try:
            session_state.inc("tool_calls_started")
        except Exception:
            pass
    return row


def handle_post_tool_call(event: dict, registry: ToolRegistry,
                          backend=None, args: dict | None = None) -> dict:
    """Worker-side: registry end-bump + activity capsule (+ encoding)."""
    registry.observe_event(event)
    return build_capsule(event, args=args, backend=backend)
