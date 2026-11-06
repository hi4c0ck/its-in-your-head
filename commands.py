"""Slash commands: /mirror and its subcommands (C7 Lane C, S5).

Registration: register_commands(ctx) calls ctx.register_command("mirror", ...)
once; subcommands (pulse|portrait|drift|context|reset|adaptation ...) are parsed
from the trailing args. Output is compact plain text (the user-facing surface),
never JSON dumps. Adaptation subcommands are read-only status until S7 lands
the engine: on/off report the C4 adaptation.enabled value without writing.

Data comes from dashboard.plugin_api's sync shape helpers (guarded import —
commands degrade to "no data" text, never raise into the dispatcher).
"""
import logging

logger = logging.getLogger(__name__)

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from .dashboard import plugin_api as _api
except Exception:
    try:
        from dashboard import plugin_api as _api  # type: ignore[no-redef]
    except Exception:
        _api = None

try:
    from .core import settings as _settings
except Exception:
    try:
        from core import settings as _settings  # type: ignore[no-redef]
    except Exception:
        _settings = None

USAGE = (
    "/mirror — usage: /mirror [pulse|portrait|drift|context|reset|"
    "adaptation on|off|status|rules]"
)

_ctx = None  # plugin ctx, remembered for handler closures


def _defaults() -> dict:
    if _settings is not None:
        try:
            return dict(_settings.DEFAULTS)
        except Exception:
            pass
    return {
        "perception.enabled": True,
        "observation.reasoning": True,
        "observation.retain_days": 90,
        "pulse.prototype_cap": 64,
        "pulse.min_labeled": 20,
        "pulse.thresholds.goal": 0.8,
        "pulse.thresholds.good": 0.6,
        "pulse.thresholds.friction": 0.4,
        "adaptation.enabled": False,
        "adaptation.rules": [],
    }


def _current_settings() -> dict:
    defaults = _defaults()
    ctx = _ctx
    get = getattr(ctx, "get_config", None)
    if callable(get):
        out = dict(defaults)
        for key, default in defaults.items():
            try:
                out[key] = get(key, default)
            except Exception:
                pass
        return out
    if _api is not None:
        try:
            return _api._settings_current()
        except Exception:
            pass
    return defaults


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return f"{len(value)} rules" if value else "none"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _metrics_line() -> str:
    if _api is None:
        return "mirror: no backend"
    try:
        rows = _api._query(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0), MAX(ts) "
            "FROM events")
        if not rows:
            return "mirror: no data yet"
        events, total_tokens, _ = rows[0]
        return f"mirror: {events} events, {total_tokens} tokens"
    except Exception:
        return "mirror: no data yet"


def _pulse_text() -> str:
    if _api is None:
        return "pulse: no data yet"
    try:
        shape = _api._pulse_shape()
    except Exception:
        return "pulse: no data yet"
    rows = shape.get("rows") or []
    if not rows:
        return "pulse: no data yet (engine lands in S4)"
    lines = ["pulse: " + " | ".join(str(c) for c in shape.get("columns", []))]
    for row in rows[:8]:
        if isinstance(row, dict):
            lines.append("  " + " | ".join(
                f"{c}={_fmt(row.get(c))}" for c in shape.get("columns", [])))
        else:
            lines.append(f"  {_fmt(row)}")
    return "\n".join(lines)


def _portrait_text() -> str:
    if _api is None:
        return "portrait: no data yet"
    try:
        shape = _api._portrait_shape()
    except Exception:
        return "portrait: no data yet"
    if not shape.get("available"):
        return "portrait: no data yet (engine lands in S3)"
    lines = ["portrait:"]
    for dim, value in (shape.get("dimensions") or {}).items():
        conf = (shape.get("confidence") or {}).get(dim)
        lines.append(f"  {dim}: {_fmt(value)} (confidence {_fmt(conf)})")
    return "\n".join(lines)


def _drift_text() -> str:
    if _api is None:
        return "drift: no history yet"
    try:
        shape = _api._drift_shape()
    except Exception:
        return "drift: no history yet"
    series = shape.get("series") or {}
    if not any(series.values()):
        return "drift: no history yet"
    lines = ["drift:"]
    for dim, values in series.items():
        if values:
            lines.append(f"  {dim}: " + " ".join(_fmt(v) for v in values[-8:]))
    return "\n".join(lines)


def _context_text() -> str:
    if _api is None:
        return "context: no data yet"
    try:
        shape = _api._context_shape()
    except Exception:
        return "context: no data yet"
    lines = ["context:"]
    for row in shape.get("rows", []):
        lines.append(f"  {row.get('item')}: {row.get('visibility')}")
    return "\n".join(lines)


def _reset_text() -> str:
    if _api is None:
        return "reset: no backend"
    try:
        result = _api._reset_dataset()
    except Exception:
        return "reset: failed"
    wiped = result.get("wiped") or {}
    events = wiped.get("events", 0)
    return f"reset: wiped {events} events (config untouched)"


def _adaptation_text(sub: str) -> str:
    current = _current_settings()
    enabled = current.get("adaptation.enabled", False)
    state = "on" if enabled else "off"
    if sub == "rules":
        rules = current.get("adaptation.rules") or []
        if not rules:
            return "adaptation rules: none yet (engine lands in S7)"
        return "adaptation rules:\n" + "\n".join(f"  - {_fmt(r)}" for r in rules)
    if sub in ("on", "off"):
        # Read-only until S7: report the C4 value, do not write.
        return (f"adaptation: currently {state} — "
                f"requested {sub} takes effect when the S7 engine lands")
    # status / unknown subcommand
    return f"adaptation: {state} (engine lands in S7; rules: none yet)"


def mirror_handler(raw_args: str = "", **kwargs) -> str:
    """Handler for /mirror. fn(raw_args: str) -> str (Hermes command API)."""
    try:
        args = str(raw_args or "").strip().lower().split()
    except Exception:
        args = []
    if not args:
        return _metrics_line()
    head = args[0]
    if head == "pulse":
        return _pulse_text()
    if head == "portrait":
        return _portrait_text()
    if head == "drift":
        return _drift_text()
    if head == "context":
        return _context_text()
    if head == "reset":
        return _reset_text()
    if head == "adaptation":
        sub = args[1] if len(args) > 1 else "status"
        if sub in ("on", "off", "status", "rules"):
            return _adaptation_text(sub)
        return USAGE
    return USAGE


# Alias: some dispatchers look up a "handler" attribute by command name.
handler = mirror_handler


def register_commands(ctx) -> list:
    """Register /mirror on the plugin ctx. Returns registered names.
    Tolerant of ctx doubles without register_command (returns [])."""
    global _ctx
    _ctx = ctx
    register = getattr(ctx, "register_command", None)
    if not callable(register):
        return []
    try:
        register(
            "mirror",
            mirror_handler,
            description="It's in Your Head — prompt analytics mirror "
                        "(pulse/portrait/drift/context/reset/adaptation)",
            args_hint="[pulse|portrait|drift|context|reset|adaptation ...]",
        )
    except Exception:
        logger.exception("its-in-your-head: /mirror registration failed")
        return []
    return ["mirror"]
