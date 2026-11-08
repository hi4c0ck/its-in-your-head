"""It's in Your Head — registration + observer hook wiring.

Hook bodies live in core/collector.py (C7 Lane A); this module only wires
them. See ARCHITECTURE.md for locked decisions and doc pointers.
"""
import logging

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from . import metrics
    from .core import collector
except ImportError:
    import metrics
    from core import collector

logger = logging.getLogger(__name__)


def _import_engine():
    """Import the engine module (hermes namespace vs top-level), guarding the
    isolated-child case where it is unavailable (doctor / no agent half)."""
    try:
        from . import engine
    except ImportError:
        import engine
    return engine


def _live_encoder_provider(ctx):
    """Return a live-settings callable the Engine re-reads on each encode.

    Reads ONLY the persisted ``perception.encoder`` value from the ctx config
    API (``get_config``). Never raises: a missing config API or a read fault
    returns ``{}``, so the Engine degrades to its startup backend name instead
    of crashing the hot encode path. The write side is the dashboard
    ``POST /settings`` -> ``ctx.set_config`` (the shared profile config store),
    so a flip persists and the agent half sees it on its next read."""
    def provider():
        try:
            get = getattr(ctx, "get_config", None)
            if not callable(get):
                return {}
            return {"perception.encoder": get("perception.encoder", "tier-0")}
        except Exception:
            return {}
    return provider


# Re-exported hook bodies (kept at module level for tests/inspection).
_on_pre_llm_call = collector._on_pre_llm_call
_on_post_api_request = collector._on_post_api_request
_on_api_request_error = collector._on_api_request_error
_on_pre_tool_call = collector._on_pre_tool_call
_on_post_tool_call = collector._on_post_tool_call
_on_stream_delta = collector._on_stream_delta
_on_stream_end = collector._on_stream_end
_on_session_start = collector._on_session_start
_on_session_end = collector._on_session_end





def register(ctx):
    collector.set_ctx(ctx)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_stream_delta", _on_stream_delta)
    ctx.register_hook("on_stream_end", _on_stream_end)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    # S5 seam (parent-applied contract flag): /mirror commands + dashboard
    # ctx wiring (GET/POST /settings read/write via ctx config API).
    try:
        from .commands import register_commands
    except ImportError:
        from commands import register_commands
    register_commands(ctx)
    try:
        from .dashboard import plugin_api as _api
    except ImportError:
        from dashboard import plugin_api as _api
    _api.set_ctx(ctx)
    # Live encoder-switch seam: re-read the PERSISTED perception.encoder on every
    # encode so a dashboard flip (POST /settings) re-resolves the backend with
    # no restart and no state wipe. Lazy — no Engine is built here (the lazy-DB
    # rule: register() runs under a temp HERMES_HOME and must stay I/O-free);
    # get_engine() applies the provider to the engine on first use.
    _engine_mod = _import_engine()
    _engine_mod.set_live_settings_provider(_live_encoder_provider(ctx))
    # Start the background worker. Lazy-DB rule: the DB connection opens on
    # the first event, NEVER at register() time — doctor runs register() under
    # a temp HERMES_HOME and deletes it (WinError 145 cleanup on Windows).
    metrics.start_worker()
    logger.info("its-in-your-head: observer hooks + metrics worker registered")
