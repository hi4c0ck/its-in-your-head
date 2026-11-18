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


# v0.1.32: production operation descriptors are deterministic, bounded and
# built ONLY from tool identity, an action class and allowlisted argument
# semantics. No raw argument values, prompts, reasoning or results survive.
_SEMANTIC_ARG_KEYS = frozenset({
    "path", "file", "file_path", "filename",
    "command", "cmd", "query", "q", "url", "uri", "pattern",
})
_PATH_KEYS = frozenset({"path", "file", "file_path", "filename"})
_COMMAND_KEYS = frozenset({"command", "cmd"})
_URL_KEYS = frozenset({"url", "uri"})
_TERM_KEYS = frozenset({"query", "q", "pattern"})
_DENY_TOKENS = frozenset({
    "token", "secret", "password", "passwd", "api", "apikey", "bearer",
    "credential", "credentials", "authorization", "private",
})
_EXT_CLASSES = {
    ".py": "code", ".js": "code", ".ts": "code", ".kt": "code",
    ".rs": "code", ".go": "code", ".java": "code", ".sh": "code",
    ".sql": "data", ".json": "data", ".yaml": "config", ".yml": "config",
    ".toml": "config", ".md": "doc", ".txt": "doc", ".pdf": "doc",
    ".html": "doc", ".css": "style", ".svg": "image", ".png": "image",
    ".jpg": "image", ".jpeg": "image",
}
_EXACT_ACTIONS = {
    "search_files": "search", "web_search": "search", "session_search": "search",
    "grep": "search", "read_file": "read", "read": "read",
    "write_file": "write", "patch": "edit", "edit": "edit",
    "terminal": "execute", "execute_code": "execute", "bash": "execute",
    "browser_exec": "browse", "browser": "browse", "computer_use": "computer",
    "vision_analyze": "vision", "memory": "remember",
    "delegate_task": "delegate", "clarify": "ask",
    "skill_view": "skill", "skill_manage": "skill",
    "cronjob_manage": "schedule", "kanban_create": "coordinate",
    "kanban_comment": "coordinate", "kanban_complete": "coordinate",
}


def _action_for_tool(name: str) -> str:
    """Deterministic action class from tool identity (never from free text)."""
    n = name.lower()
    if n in _EXACT_ACTIONS:
        return _EXACT_ACTIONS[n]
    if "search" in n or "find" in n:
        return "search"
    if "read" in n or "view" in n or "list" in n or "get" in n:
        return "read"
    if "write" in n or "edit" in n or "patch" in n:
        return "edit"
    if "exec" in n or "terminal" in n or "shell" in n:
        return "execute"
    if "browser" in n or "web" in n:
        return "browse"
    if "memory" in n or "remember" in n:
        return "remember"
    if "delegate" in n or "kanban" in n or "cron" in n:
        return "coordinate"
    return "operate"


def _safe_tokens(raw: str, limit: int = 32, max_tokens: int = 3) -> list[str]:
    """Allowlist short identifiers; reject secret-like and hash-like text."""
    clean = "".join(c if (c.isalnum() or c in "_-.") else " " for c in raw)
    out = []
    for part in clean.split():
        low = part.lower()
        if not part or part in (".", "..") or part.startswith("-"):
            continue
        if any(word in low for word in _DENY_TOKENS):
            continue
        if len(part) > limit or sum(c.isdigit() for c in part) > len(part) // 2:
            continue
        out.append(part[:limit])
        if len(out) >= max_tokens:
            break
    return out


def _safe_token(raw: str, limit: int = 32) -> str:
    tokens = _safe_tokens(raw, limit, 1)
    return tokens[0] if tokens else ""


def _path_semantics(value) -> tuple[str, str]:
    raw = str(value or "").replace("\\", "/")
    base = raw.rstrip("/").split("/")[-1]
    token = _safe_token(base, 32)
    dot = base.rfind(".")
    ext = base[dot:].lower() if dot >= 0 else ""
    category = _EXT_CLASSES.get(ext, "file")
    return token, category


def _semantic_parts(raw_key, value) -> list[str]:
    """Return bounded, key-specific semantic tokens only."""
    key = str(raw_key).lower()
    if key in _PATH_KEYS:
        raw = str(value or "").replace("\\", "/")
        parts = raw.rstrip("/").split("/")
        base = parts[-1]
        token, category = _path_semantics(value)
        out = ([f"file:{token}"] if token else []) + [f"kind:{category}"]
        if len(parts) >= 2:
            parent = _safe_token(parts[-2], 32)
            if parent and "." not in parent:
                out.insert(1, f"dir:{parent}")
        return out
    if key in _URL_KEYS:
        raw = str(value or "").split("?", 1)[0].split("#", 1)[0]
        if "://" in raw:
            rest = raw.split("://", 1)[1]
            host, _, tail = rest.partition("/")
        else:
            host, _, tail = raw.partition("/")
        out = []
        token = _safe_token(host, 48)
        if token:
            out.append(f"host:{token}")
        for segment in _safe_tokens(tail.replace("/", " "), 32, 2):
            out.append(f"path:{segment}")
        return out
    if key in _COMMAND_KEYS or key in _TERM_KEYS:
        prefix = "verb" if key in _COMMAND_KEYS else "term"
        raw = str(value or "").replace("\\", "/").replace("/", " ")
        # Prefer specific file-like tokens over generic path/flag words.
        tokens = _safe_tokens(raw, 32, 6)
        tokens.sort(key=lambda t: (("." in t), len(t)), reverse=True)
        return [f"{prefix}:{t}" for t in tokens[:3]]
    return []


def sanitize_operation(tool_name, args) -> str:
    """Bounded, privacy-safe PRODUCTION operation descriptor.

    Shape: ``action tool:<tool> [file/verb/term/host/kind tokens]
    [arg:<key>:<type> ...]``. Only tool identity, a deterministic action
    class, allowlisted bounded tokens and argument key/type tags survive.
    Raw values, prompts, reasoning and results never enter the event.
    """
    name = _safe_token(str(tool_name or "unknown"), 32) or "unknown"
    parts = [f"action:{_action_for_tool(name)}", f"tool:{name}"]
    semantic: list[str] = []
    arg_types: list[str] = []
    if isinstance(args, dict):
        items = sorted(args.items(), key=lambda item: str(item[0]))[:6]
        for raw_key, value in items:
            key = _safe_token(str(raw_key), 32) or "arg"
            arg_types.append(f"arg:{key}:{type(value).__name__}")
            if raw_key in _SEMANTIC_ARG_KEYS or str(raw_key).lower() in _SEMANTIC_ARG_KEYS:
                for token in _semantic_parts(raw_key, value):
                    if token not in semantic and len(semantic) < 6:
                        semantic.append(token)
    parts.extend(semantic)
    parts.extend(arg_types[:6])
    return " ".join(parts)[:160]


_OPERATION_SEMANTIC_MARKERS = ("file:", "verb:", "term:", "host:", "kind:")


def has_operation_semantics(operation: str) -> bool:
    """True when a descriptor carries at least one allowlisted semantic token.

    A descriptor with only action/tool/arg-type tags is ambiguous: it must
    not be scored as objective similarity (that would manufacture drift).
    """
    text = str(operation or "")
    return any(marker in text for marker in _OPERATION_SEMANTIC_MARKERS)


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
