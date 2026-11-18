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


# v0.1.31 privacy hardening: production operation descriptors are built ONLY
# from tool identity, a deterministic action class, explicitly approved
# semantic CATEGORIES (the file-extension class allowlist + the url role)
# and argument key/type tags. NO value-derived free text is extracted:
# command/query/pattern values contribute no tokens at all, and path
# basenames, parent directories, URL hosts, URL paths and URL credentials
# are NEVER tokenized — any of them can carry personal or confidential
# fragments. A deny-token filter is NOT a privacy boundary: it rejects
# `--password` but not the value that follows it (`login --password
# hunter2` leaked `verb:hunter2`), so value extraction is removed outright
# instead of filtered. _DENY_TOKENS remains only as identifier hygiene for
# tool names / argument keys, never as a value filter.
_SEMANTIC_ARG_KEYS = frozenset({
    "path", "file", "file_path", "filename", "url", "uri",
})
_PATH_KEYS = frozenset({"path", "file", "file_path", "filename"})
_URL_KEYS = frozenset({"url", "uri"})
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


def _safe_token(raw: str, limit: int = 32) -> str:
    """Identifier hygiene for tool names / argument keys ONLY (never for
    argument values): allowlist short identifiers, reject secret-like and
    hash-like text. This is not a value-privacy boundary — values are not
    tokenized at all (see _semantic_parts)."""
    clean = "".join(c if (c.isalnum() or c in "_-.") else " " for c in raw)
    for part in clean.split():
        low = part.lower()
        if not part or part in (".", "..") or part.startswith("-"):
            continue
        if any(word in low for word in _DENY_TOKENS):
            continue
        if len(part) > limit or sum(c.isdigit() for c in part) > len(part) // 2:
            continue
        return part[:limit]
    return ""


def _semantic_parts(raw_key, value) -> list[str]:
    """Approved semantic CATEGORIES only — never value-derived free text.

    path-like keys -> the file-extension class (``kind:code`` etc.); the
                      basename and parent directory are NEVER tokenized.
    url-like keys  -> the categorical ``kind:url`` role marker; the host,
                      path, query and any embedded credentials are NEVER
                      parsed into tokens.
    command/query/pattern keys -> nothing (no token extraction at all).
    """
    key = str(raw_key).lower()
    if key in _PATH_KEYS:
        raw = str(value or "").replace("\\", "/")
        base = raw.rstrip("/").split("/")[-1]
        dot = base.rfind(".")
        ext = base[dot:].lower() if dot >= 0 else ""
        return [f"kind:{_EXT_CLASSES.get(ext, 'file')}"]
    if key in _URL_KEYS:
        return ["kind:url"] if str(value or "").strip() else []
    return []


# The CLOSED descriptor schema (producer + trust gate share these sets).
# A canonical descriptor is exactly:
#   action:<class> tool:<name> [kind:<category>] [arg:<key>:<type> ...]
# with <=1 kind, <=6 arg tags, <=160 chars, single-space separated, and
# charset alnum + "_-.:" only. is_canonical_operation() parses this grammar
# strictly — a prefix check alone is NOT a trust boundary (forged strings
# like `arg:password:hunter2` or `kind:alice_smith` carry approved prefixes
# with arbitrary content).
_ACTION_CLASSES = frozenset({
    "search", "read", "write", "edit", "execute", "browse", "computer",
    "vision", "remember", "delegate", "ask", "skill", "schedule",
    "coordinate", "operate",
})
_KIND_CLASSES = frozenset(set(_EXT_CLASSES.values()) | {"file", "url"})
_ARG_TYPE_NAMES = frozenset({
    "str", "int", "float", "bool", "list", "dict", "NoneType",
    "tuple", "set", "frozenset", "bytes", "bytearray", "complex",
})
_OPERATION_MAX_CHARS = 160
_ARG_TAG_CAP = 6


def sanitize_operation(tool_name, args) -> str:
    """Bounded, privacy-safe PRODUCTION operation descriptor.

    Shape: ``action:<class> tool:<tool> [kind:<category>]
    [arg:<key>:<type> ...]``. Only tool identity, a deterministic action
    class, approved semantic categories (file-extension class / url role)
    and argument key/type tags survive. NO value-derived free text is
    extracted anywhere: raw argument values, prompts, reasoning and
    results never enter the event. Output is always CANONICAL
    (is_canonical_operation accepts it): type names outside the closed set
    collapse to ``other``, and the 160-char budget drops whole trailing
    tokens — never a mid-token truncation.
    """
    name = _safe_token(str(tool_name or "unknown"), 32) or "unknown"
    parts = [f"action:{_action_for_tool(name)}", f"tool:{name}"]
    semantic: list[str] = []
    arg_types: list[str] = []
    arg_keys: set[str] = set()
    if isinstance(args, dict):
        items = sorted(args.items(), key=lambda item: str(item[0]))[:6]
        for raw_key, value in items:
            key = _safe_token(str(raw_key), 32) or "arg"
            if key in arg_keys:
                continue  # canonical schema forbids duplicate arg keys
            arg_keys.add(key)
            tname = type(value).__name__
            if tname not in _ARG_TYPE_NAMES:
                tname = "other"
            arg_types.append(f"arg:{key}:{tname}")
            if raw_key in _SEMANTIC_ARG_KEYS or str(raw_key).lower() in _SEMANTIC_ARG_KEYS:
                for token in _semantic_parts(raw_key, value):
                    # At most ONE kind token (the first semantic key in
                    # sorted order wins) — the canonical schema allows a
                    # single category per descriptor.
                    if not semantic:
                        semantic.append(token)
    parts.extend(semantic)
    parts.extend(arg_types[:_ARG_TAG_CAP])
    # Token-budget join: action+tool always fit; a token that would exceed
    # the cap is dropped WHOLE so the output stays parseable/canonical.
    out = ""
    for part in parts:
        candidate = part if not out else out + " " + part
        if len(candidate) > _OPERATION_MAX_CHARS:
            break
        out = candidate
    return out


def is_canonical_operation(descriptor) -> bool:
    """Strict parse of the canonical descriptor schema — the trust gate for
    the collector ``operation`` passthrough and the engine's trajectory
    intake. Accepts ONLY strings sanitize_operation() itself could emit:

    - charset alnum + ``_-.:`` + single separating spaces (no cleaning:
      a string needing cleanup is rejected, not repaired);
    - token 0 ``action:<class>`` with class in the closed action set AND
      equal to the deterministic action for the tool field;
    - token 1 ``tool:<name>`` where name passes the same identifier
      hygiene the producer applies (bounded, no deny tokens, no flags);
    - at most one ``kind:<category>`` from the closed kind set, before any
      arg tag;
    - up to 6 ``arg:<key>:<type>`` tags: key passes identifier hygiene,
      type in the closed type-name set (or ``other``), no duplicate keys;
    - total length <= 160.

    Forged-prefix payloads (``arg:password:hunter2``, ``kind:alice_smith``,
    legacy ``verb:``/``term:``/``file:``/``host:`` tokens, free text) all
    fail. Legacy persisted ROWS remain scoreable via has_operation_semantics
    (read side); they are no longer accepted as new input.
    """
    if not isinstance(descriptor, str) or not descriptor:
        return False
    if len(descriptor) > _OPERATION_MAX_CHARS:
        return False
    if not all(c.isalnum() or c in "_-.: " for c in descriptor):
        return False
    tokens = descriptor.split(" ")
    if len(tokens) < 2 or any(not t for t in tokens):
        return False
    action = tokens[0]
    if not action.startswith("action:") or action[7:] not in _ACTION_CLASSES:
        return False
    tool = tokens[1]
    if not tool.startswith("tool:"):
        return False
    name = tool[5:]
    if not name or _safe_token(name, 32) != name:
        return False
    if action[7:] != _action_for_tool(name):
        return False
    kind_seen = False
    arg_seen = False
    arg_keys: set[str] = set()
    for tok in tokens[2:]:
        if tok.startswith("kind:"):
            if kind_seen or arg_seen or tok[5:] not in _KIND_CLASSES:
                return False
            kind_seen = True
        elif tok.startswith("arg:"):
            fields = tok.split(":")
            if len(fields) != 3:
                return False
            key, tname = fields[1], fields[2]
            if not key or _safe_token(key, 32) != key:
                return False
            if tname not in _ARG_TYPE_NAMES and tname != "other":
                return False
            if key in arg_keys or len(arg_keys) >= _ARG_TAG_CAP:
                return False
            arg_keys.add(key)
            arg_seen = True
        else:
            return False
    return True


# Current descriptors emit only ``kind:``; the remaining markers recognize
# LEGACY rows persisted before the value-extraction removal so they stay
# scoreable (scoring never re-emits their text).
_OPERATION_SEMANTIC_MARKERS = ("file:", "verb:", "term:", "host:", "kind:",
                               "path:")


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
