"""C6 enqueued-event schemas + builders + validation.

Event types (worker flattens to rows in metrics.py):
  user_turn | turn        prompt capture (legacy alias: turn)
  tool_start              LIVE row create/increment at pre_tool_call
  tool_end                status/duration/result-type capsule at post_tool_call
  reasoning_seg           first reasoning segment per turn (metadata only;
                          raw reasoning text is NEVER persisted, C6)
  stream_end              stream completion + error signals
  session_start | session_end
  api | error             legacy per-call telemetry (pre-S1 types, kept)
Unknown types: the worker accepts them as no-op rows (C6).
"""
import time

EVENT_TYPES = frozenset({
    "user_turn", "turn",
    "tool_start", "tool_end",
    "reasoning_seg", "stream_end",
    "session_start", "session_end",
    "api", "error",
})

REQUIRED_KEYS = {
    "tool_start": ("tool_name",),
    "tool_end": ("tool_name",),
    "reasoning_seg": ("turn_id",),
    "session_start": ("session_id",),
    "session_end": ("session_id",),
}


def validate(ev) -> bool:
    """True if ev is a well-formed enqueued event dict."""
    if not isinstance(ev, dict):
        return False
    etype = ev.get("type")
    if etype not in EVENT_TYPES:
        return False
    for key in REQUIRED_KEYS.get(etype, ()):
        if ev.get(key) is None:
            return False
    return True


def _base(etype, **kw):
    ev = {"type": etype, "ts": time.time()}
    ev.update(kw)
    return ev


# P1.2 (§4.1): a STRICT error-category allowlist. Arbitrary provider/exception
# text (a `type`/`code`/`error` field, a raw string) is NEVER stored — any
# value outside the known set collapses to "unknown" so no external string
# (e.g. "Bearer <token>") can ride into the event + the DB.
_ERROR_CATEGORIES = ("http", "timeout", "rate_limit", "authentication",
                    "connection", "unknown")


def _error_type(error) -> "str | None":
    """§4.1/P1.2: a strict, allowlisted error category. Only the six known
    categories are ever produced; a bounded set of well-known transport/
    provider terms is mapped onto them, and everything else (including any
    free-text like "Bearer ...") is "unknown"."""
    if isinstance(error, dict):
        raw = error.get("type") or error.get("code") or error.get("error")
    else:
        raw = error
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in _ERROR_CATEGORIES:
        return s
    if any(t in s for t in ("timeout", "timed out", "deadline", "504")):
        return "timeout"
    if any(t in s for t in ("rate", "429", "quota", "throttl")):
        return "rate_limit"
    if any(t in s for t in ("auth", "unauthorized", "forbidden", "401",
                            "403", "api key", "api-key", "invalid key",
                            "credentials")):
        return "authentication"
    if any(t in s for t in ("connection", "connect", "network", "refused",
                            "502", "503", "500")):
        return "connection"
    if s.startswith(("http", "status")):
        return "http"
    return "unknown"


# Public alias: the collector + stream_end use this as the §4.1 allowlisted
# error-class extractor.
error_type = _error_type


def tool_start(*, tool_name, task_id=None, session_id=None, turn_id=None,
               tool_call_id=None, args_chars=None, **kw):
    return _base("tool_start", tool_name=tool_name, task_id=task_id,
                 session_id=session_id, turn_id=turn_id,
                 tool_call_id=tool_call_id, args_chars=args_chars)


def tool_end(*, tool_name, status="ok", duration_ms=None, result_type=None,
             result_chars=None, task_id=None, session_id=None, turn_id=None,
             tool_call_id=None, **kw):
    return _base("tool_end", tool_name=tool_name, status=status,
                 duration_ms=duration_ms, result_type=result_type,
                 result_chars=result_chars, task_id=task_id,
                 session_id=session_id, turn_id=turn_id,
                 tool_call_id=tool_call_id)


def reasoning_seg(*, turn_id, session_id=None, reasoning_text="",
                  reasoning_chars=None, reasoning_deltas=0, **kw):
    """reasoning_text travels the queue only; the worker sketches it and
    does not persist it (C6)."""
    text = reasoning_text if isinstance(reasoning_text, str) else ""
    return _base("reasoning_seg", turn_id=turn_id, session_id=session_id,
                 reasoning_text=text,
                 reasoning_chars=reasoning_chars if reasoning_chars is not None
                 else len(text),
                 reasoning_deltas=reasoning_deltas)


def stream_end(*, finished=None, error=None, final_chars=None,
               session_id=None, turn_id=None, **kw):
    # §4.1/§4.2: carry only a bounded error-class category. The arbitrary
    # provider/exception message is NOT retained in the event (never the
    # dict, never its body) — it reaches the DB as NULL via metrics.
    return _base("stream_end", finished=finished,
                 error_type=_error_type(error),
                 final_chars=final_chars, session_id=session_id,
                 turn_id=turn_id)


def session_start(*, session_id=None, **kw):
    return _base("session_start", session_id=session_id)


def session_end(*, session_id=None, **kw):
    return _base("session_end", session_id=session_id)
