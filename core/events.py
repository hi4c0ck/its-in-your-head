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
    err = error if isinstance(error, dict) else {}
    return _base("stream_end", finished=finished,
                 error_type=err.get("type"), error_message=err.get("message"),
                 final_chars=final_chars, session_id=session_id,
                 turn_id=turn_id)


def session_start(*, session_id=None, **kw):
    return _base("session_start", session_id=session_id)


def session_end(*, session_id=None, **kw):
    return _base("session_end", session_id=session_id)
