"""Slash commands: /mirror and its subcommands (C7 Lane C, S5).

Registration: register_commands(ctx) calls ctx.register_command("mirror", ...)
once; subcommands (pulse|portrait|drift|context|reset|adaptation ...) are parsed
from trailing args. Output is compact plain text (the user-facing surface),
never JSON dumps. S7: adaptation on/off toggle adaptation.enabled
(persisted via ctx.set_config when available, else ephemeral), status
reports the toggle + last applied injection, rules lists the
deterministic rule definitions.

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


def _adaptation_set(enabled: bool) -> str:
    """Set adaptation.enabled via ctx.set_config when available, else
    ephemeral (POST /settings persistence semantics)."""
    value = bool(enabled)
    word = "on" if value else "off"
    ctx = _ctx
    setter = getattr(ctx, "set_config", None)
    if callable(setter):
        try:
            setter("adaptation.enabled", value)
            return f"adaptation: {word} (persisted)"
        except Exception:
            pass
    if _api is not None:
        try:
            _api._ephemeral_settings["adaptation.enabled"] = value
        except Exception:
            pass
    return f"adaptation: {word} (ephemeral)"


def _adaptation_rules_text() -> str:
    try:
        try:
            from .adaptation import rules as _rules
        except Exception:
            from adaptation import rules as _rules  # type: ignore[no-redef]
        defs = _rules.list_rule_definitions()
    except Exception:
        defs = []
    if not defs:
        return "adaptation rules: none"
    lines = ["adaptation rules:"]
    for d in defs:
        lines.append(f"  - {d.get('name')}: {d.get('match')}")
        lines.append(f"    template: {d.get('template')}")
        ev = d.get("evidence")
        if ev:
            lines.append(f"    evidence: {', '.join(ev)}")
    return "\n".join(lines)


def _adaptation_status_text() -> str:
    current = _current_settings()
    enabled = current.get("adaptation.enabled", False)
    state = "on" if enabled else "off"
    last = None
    count = 0
    try:
        try:
            from .adaptation import injection as _inj
        except Exception:
            from adaptation import injection as _inj  # type: ignore[no-redef]
        last = _inj.last_application()
        count = _inj.application_count()
    except Exception:
        pass
    lines = [f"adaptation: {state} (injected {count} time(s))"]
    if last is not None:
        lines.append(f"  last: {last.get('rule_id')} -> {last.get('instruction')}")
    else:
        lines.append("  last: none")
    return "\n".join(lines)


def _adaptation_text(sub: str) -> str:
    if sub == "on":
        return _adaptation_set(True)
    if sub == "off":
        return _adaptation_set(False)
    if sub == "rules":
        return _adaptation_rules_text()
    return _adaptation_status_text()


def _set_encoder_setting(value: str) -> str:
    name = "perception.encoder"
    ctx = _ctx
    setter = getattr(ctx, "set_config", None)
    if callable(setter):
        try:
            setter(name, value)
            return f"{name}: {value} (persisted)"
        except Exception:
            pass
    if _api is not None:
        try:
            _api._ephemeral_settings[name] = value
        except Exception:
            pass
    return f"{name}: {value} (ephemeral)"


def _get_encoder_setting() -> str:
    try:
        current = _current_settings()
        return str(current.get("perception.encoder", "tier-0"))
    except Exception:
        return "tier-0"


def _engine_client():
    try:
        try:
            from .perception import worker_client as _wc
        except Exception:
            from perception import worker_client as _wc  # type: ignore[no-redef]
    except Exception:
        return None
    try:
        return _wc.get_shared_client()
    except Exception:
        try:
            return _wc.OnnxWorkerClient()
        except Exception:
            return None


def _engine_status_text() -> str:
    current = _get_encoder_setting()
    active_tier = "Tier 1" if current == "tier-1-onnx" else "Tier 0"
    variant = "int8"
    try:
        try:
            from .perception import worker_client as _wc
        except Exception:
            from perception import worker_client as _wc  # type: ignore[no-redef]
        variant = str(_wc.load_manifest().get("default_variant") or "int8")
    except Exception:
        pass
    runtime = "unavailable"
    model = "missing"
    worker = "dead"
    last = "none"
    client = _engine_client()
    if client is not None:
        try:
            st = client.status()
            runtime = st.get("runtime", "unavailable")
            model = st.get("model", "missing")
            worker = st.get("worker", "dead")
            last = st.get("last_failure") or "none"
        except Exception:
            pass
    lines = [
        "engine:",
        f"  Tier 0: tier-0 (sketch, always available){' [active]' if active_tier == 'Tier 0' else ''}",
        f"  Tier 1: tier-1-onnx (ONNX MiniLM){' [active]' if active_tier == 'Tier 1' else ''}",
        f"  Variant: {variant} (default; fp32 graph optional, not installed by default)",
        f"  Active tier: {active_tier}",
        f"  perception.encoder: {current}",
        f"  ONNX runtime: {runtime}",
        f"  Model: {model}",
        f"  Worker: {worker}",
        f"  Last failure: {last}",
    ]
    return "\n".join(lines)


# -- engine enable steps (monkeypatchable for tests) ----------------------------
def _op_platform_check():
    import platform
    import sys
    if platform.system() not in ("Windows", "Linux", "Darwin"):
        raise RuntimeError(f"unsupported platform: {platform.system()}")
    if sys.version_info < (3, 11):
        raise RuntimeError(f"unsupported python: {sys.version_info.major}.{sys.version_info.minor}")
    return f"{platform.system()}/{platform.python_version()}"


def _op_pm_env():
    try:
        import pm
    except Exception as exc:
        raise RuntimeError(f"pm unavailable: {exc}") from exc
    try:
        from pathlib import Path as _P

        from .perception import worker_client as _wc
    except Exception:
        from perception import worker_client as _wc  # type: ignore[no-redef]
    python = pm.client.ensure_project_environment(
        _wc.PM_ENV_NAME, project=_wc.ONNX_WORKER_DIR, explicit=True)
    if not _P(str(python)).is_file():
        raise RuntimeError(f"PM env interpreter not found: {python}")
    return str(python)


def _op_download_verify(interpreter=None):
    try:
        from .perception import encoder as _enc
        from .perception import worker_client as _wc
    except Exception:
        from perception import encoder as _enc  # type: ignore
        from perception import worker_client as _wc  # type: ignore
    model_dir = _wc.resolve_model_dir()
    status, errors = _wc.check_model(model_dir)
    if status == "hash-mismatch":
        raise RuntimeError(f"model hash mismatch: {errors}")
    if status != "verified":
        _enc.download_tier1_model(model_dir, confirm=True)
    status, errors = _wc.check_model(model_dir)
    if status != "verified":
        raise RuntimeError(f"model verification failed: {errors}")
    return str(model_dir)


def _op_smoke(interpreter=None, model_dir=None):
    try:
        from .perception import worker_client as _wc
    except Exception:
        from perception import worker_client as _wc  # type: ignore
    client = _wc.OnnxWorkerClient(interpreter=interpreter, model_dir=model_dir)
    st = client.status()
    if st["runtime"] != "ok":
        raise RuntimeError("runtime unavailable")
    if st["model"] != "verified":
        raise RuntimeError(f"model not verified: {st['model']}")
    pong = client.ping()
    if not getattr(pong, "ok", False):
        raise RuntimeError(f"smoke ping failed: {getattr(pong, 'failure', pong)}")
    emb = client.embed(["hello world"])
    if not getattr(emb, "ok", False):
        raise RuntimeError(f"smoke embed failed: {getattr(emb, 'failure', emb)}")
    # Keep a shared client pointing at the same env for encode().
    try:
        _wc.reset_shared_client()
        _wc.get_shared_client(interpreter=interpreter, model_dir=model_dir)
        _wc.get_shared_client().ping()
    except Exception:
        pass
    return f"ping+embed ok dim={getattr(emb, 'dim', None)}"

def _engine_enable_text() -> str:
    steps = []
    interpreter = None
    model_dir = None
    try:
        plat = _op_platform_check()
        steps.append(f"platform/python: ok ({plat})")
        interpreter = _op_pm_env()
        steps.append("PM env: ok (interpreter ensured)")
        model_dir = _op_download_verify(interpreter)
        steps.append("model download+verify: ok (sha256 verified)")
        smoke = _op_smoke(interpreter, model_dir)
        steps.append(f"smoke (ping+embed): ok ({smoke})")
        steps.append("worker start: ok")
        setting = _set_encoder_setting("tier-1-onnx")
        steps.append(f"flip perception.encoder: {setting}")
        steps.append("engine: enabled Tier 1 (tier-1-onnx)")
    except Exception as exc:
        stopped = steps[-1] if steps else "platform/python"
        lines = ["engine: enable stopped (tier-0 intact)"]
        lines.extend(f"  {s}" for s in steps)
        lines.append(f"  stopped at: {stopped} <- {type(exc).__name__}: {exc}")
        lines.append("  perception.encoder: tier-0 (unchanged)")
        return "\n".join(lines)
    return "\n".join(steps)


def _engine_disable_text() -> str:
    notes = []
    client = _engine_client()
    if client is not None:
        try:
            client.shutdown()
            notes.append("worker: stopped")
        except Exception:
            notes.append("worker: stop failed (ignored)")
    else:
        notes.append("worker: none")
    setting = _set_encoder_setting("tier-0")
    notes.append(f"flip perception.encoder: {setting}")
    notes.append("model: kept (files preserved)")
    notes.append("engine: disabled, Tier 0 active")
    return "\n".join(notes)


def _engine_text(sub: str) -> str:
    if sub == "enable":
        return _engine_enable_text()
    if sub == "disable":
        return _engine_disable_text()
    return _engine_status_text()


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
    if head == "engine":
        sub = args[1] if len(args) > 1 else "status"
        if sub in ("status", "enable", "disable"):
            return _engine_text(sub)
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
