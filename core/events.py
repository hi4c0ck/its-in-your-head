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


_SEMANTIC_ARG_KEYS = frozenset({
    "path", "file", "file_path", "filename", "command", "cmd",
    "query", "q", "url", "uri", "pattern",
})


def _bounded_semantic_token(value) -> str:
    """One bounded, non-value token from a known arg key (basename/verb/host).

    This is derived metadata for trajectory semantics, never a raw argument
    dump: only a single <=48-char identifier survives.
    """
    raw = str(value or "")
    if "://" in raw:
        raw = raw.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
    elif "/" in raw or "\\" in raw:
        raw = raw.replace("\\", "/").rstrip("/").split("/")[-1]
    clean = "".join(c if (c.isalnum() or c in "_-.") else " " for c in raw)
    for token in clean.split():
        if token and token not in (".", ".."):
            return token[:48]
    return ""


def sanitize_operation(tool_name, args) -> str:
    """Bounded, privacy-safe operation description.

    Only argument NAMES and value TYPE tags are emitted — never values,
    paths, prompts or tool output. The result is an intention label for
    trajectory evaluation, not a raw record of what the user said.
    """
    name = str(tool_name or "unknown")
    parts = []
    if isinstance(args, dict):
        # Sort original (key, value) pairs; indexing back by the coerced
        # key would raise KeyError for non-string keys on the hot path.
        items = sorted(args.items(), key=lambda item: str(item[0]))[:6]
        semantic = None
        for raw_key, value in items:
            key = "".join(c if (c.isalnum() or c in "_-") else "_"
                          for c in str(raw_key))[:32]
            parts.append("%s:%s" % (key, type(value).__name__))
            if semantic is None and str(raw_key).lower() in _SEMANTIC_ARG_KEYS:
                semantic = _bounded_semantic_token(value)
        if semantic:
            parts.insert(0, "sem:%s" % semantic)
    elif args is not None:
        parts.append(type(args).__name__)
    return ("%s(%s)" % (name, ",".join(parts)))[:160]


def tool_start(*, tool_name, task_id=None, session_id=None, turn_id=None,
               tool_call_id=None, args_chars=None, operation=None, **kw):
    return _base("tool_start", tool_name=tool_name, task_id=task_id,
                 session_id=session_id, turn_id=turn_id,
                 tool_call_id=tool_call_id, args_chars=args_chars,
                 operation=operation)


def tool_end(*, tool_name, status="ok", duration_ms=None, result_type=None,
             result_chars=None, task_id=None, session_id=None, turn_id=None,
             tool_call_id=None, operation=None, **kw):
    return _base("tool_end", tool_name=tool_name, status=status,
                 duration_ms=duration_ms, result_type=result_type,
                 result_chars=result_chars, task_id=task_id,
                 session_id=session_id, turn_id=turn_id,
                 tool_call_id=tool_call_id, operation=operation)


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
