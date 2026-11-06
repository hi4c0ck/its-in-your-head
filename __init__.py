"""It's in Your Head — registration + observer hook bodies.

Contract (official docs, /docs/developer-guide/plugins):
- register(ctx) runs once at startup; a crash disables the plugin, Hermes continues.
- Hook callbacks are fire-and-forget observers accepting **kwargs (the
  dispatcher injects only the context keys a signature names); a crashing
  callback is logged and skipped, the agent continues.
- Hot-path rule: hooks capture + enqueue only; embedding + aggregation live in
  the background worker (metrics.start_worker()).

Locked default: pre_llm_call is observer-only — it is the only hook with
{"context": ...} injection ("proxy") power; returning None leaves turns
untouched. A crash is never propagated from a hook body.

See ARCHITECTURE.md for locked decisions, slot defaults, and doc pointers.
"""
import logging
import time

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from . import metrics
except ImportError:
    import metrics

logger = logging.getLogger(__name__)


def _on_pre_llm_call(**kwargs):
    """Prompt capture (turn event). kwargs include session_id, user_message,
    conversation_history, is_first_turn, model, platform. Enqueue only —
    microseconds on the hot path. Observer-only: returns None (no context
    injection); the {"context": ...} "proxy" power is available here and
    nowhere else."""
    try:
        message = kwargs.get("user_message") or ""
        if not isinstance(message, str):
            message = str(message)
        metrics.enqueue({
            "type": "turn",
            "ts": time.time(),
            "session_id": kwargs.get("session_id"),
            "turn_id": kwargs.get("turn_id"),
            "model": kwargs.get("model"),
            "platform": kwargs.get("platform"),
            "is_first_turn": kwargs.get("is_first_turn"),
            "user_message": message,
            "user_chars": len(message),
        })
    except Exception:
        logger.exception("its-in-your-head: turn capture failed")
    return None


def _on_post_api_request(**kwargs):
    """Per-provider-call metrics. kwargs include usage, api_duration,
    finish_reason, provider, and the turn_id/api_request_id/api_call_count
    correlation fields."""
    try:
        usage = kwargs.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        metrics.enqueue({
            "type": "api",
            "ts": time.time(),
            "session_id": kwargs.get("session_id"),
            "turn_id": kwargs.get("turn_id"),
            "api_request_id": kwargs.get("api_request_id"),
            "api_call_count": kwargs.get("api_call_count"),
            "model": kwargs.get("model"),
            "provider": kwargs.get("provider"),
            "api_duration": kwargs.get("api_duration"),
            "finish_reason": kwargs.get("finish_reason"),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "usage": usage,
        })
    except Exception:
        logger.exception("its-in-your-head: api metrics capture failed")


def _on_api_request_error(**kwargs):
    """Error ticks. kwargs include status_code, retry_count, retryable,
    reason, error ({type, message}) and the correlation fields."""
    try:
        error = kwargs.get("error")
        if not isinstance(error, dict):
            error = {}
        metrics.enqueue({
            "type": "error",
            "ts": time.time(),
            "session_id": kwargs.get("session_id"),
            "turn_id": kwargs.get("turn_id"),
            "api_request_id": kwargs.get("api_request_id"),
            "model": kwargs.get("model"),
            "status_code": kwargs.get("status_code"),
            "retry_count": kwargs.get("retry_count"),
            "retryable": kwargs.get("retryable"),
            "reason": kwargs.get("reason"),
            "error": error,
        })
    except Exception:
        logger.exception("its-in-your-head: error capture failed")


def register(ctx):
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    # Start the background worker. Lazy-DB rule: the DB connection opens on
    # the first event, NEVER at register() time — doctor runs register() under
    # a temp HERMES_HOME and deletes it (WinError 145 cleanup on Windows).
    metrics.start_worker()
    logger.info("its-in-your-head: observer hooks + metrics worker registered")
